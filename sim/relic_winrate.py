#!/usr/bin/env python3
"""用当前真实 GameEngine 比较 11 件开局遗物。

本脚本不把遗物效果复制到模拟器：每个样本都通过 build_learner._play，
由 GameEngine.execute_action 驱动完整开局、局外行动、PvE 与（若达到第7场）
最终死斗。为了遵守“只改变初始遗物”，只接受同时发现指定初始道纹的种子，
并在同一枚种子上分别测试当次开局候选中的每一件遗物。

默认口径：
- fixed_config.pve7_completed：完成 7 场 PvE，作为 PvE 完成率；
- full_win：_play 返回 won=True，即完成 7 场 PvE 且赢下最终死斗；
- cleared_battles：_play 返回的已清除 PvE 场数；死亡场次为 cleared_battles + 1；
- invalid：引擎/驱动异常，不进入胜率和平均场数分母，但单独统计。

运行示例（仓库根目录）：
  PYTHONPATH=. /tmp/linji-venv/bin/python sim/relic_winrate.py --runs 50 --workers 8
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

# 允许从仓库根目录运行，也允许直接 python sim/relic_winrate.py。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.api import GameEngine
import tests.setup_support as setup_support
from sim.build_learner import _play
from sim.win_only_ai import WinOnlyAI


# 固定维度：只是把开局遗物换成当前候选中的另一件。
FIXED_CONFIG = {
    "attributes": {"blood_points": 7, "speed_points": 6, "mana_points": 12},
    "initial_daowen": "杀伐",
    "resonance": "反转",
    "region": "龙心谷",
    "learn": ["庇护", "再生"],
    "spend_shards": True,
    "ai": "sim.relic_winrate.BenchmarkAI (WinOnlyAI + max one Seal activation per battle)",
    "battles": 7,
}

RELICS = [name for name, _effect in GameEngine.RELIC_DEFS]
RELIC_EFFECTS = {name: effect for name, effect in GameEngine.RELIC_DEFS}


class BenchmarkAI(WinOnlyAI):
    """当前 WinOnlyAI 的固定批测壳：每场最多实际发动一次【封印】。

    真实引擎已实现封印怪物的延迟回场；若通用 AI 每回合重复封印，便会形成
    “本回合离场、下回合回场”的无界循环，既不是有效战术也无法测遗物差异。
    该限制与 sim/run_wen_duel.py 的现行真实运行器一致，且对所有遗物完全相同。
    """

    def __init__(self, engine, *args, **kwargs):
        super().__init__(engine, *args, **kwargs)
        self._seal_scope = None
        self._seal_used = False

    def take_action(self):
        scope = (getattr(self.engine.state, "current_battle", 0),
                 bool(getattr(self.engine.state, "in_final_duel", False)))
        if scope != self._seal_scope:
            self._seal_scope = scope
            self._seal_used = False
        self.blocked_daowen_names = {"封印"} if self._seal_used else set()
        before = self.used.get("封印", 0)
        result = super().take_action()
        if self.used.get("封印", 0) > before:
            self._seal_used = True
        return result

# 原仓库已经保存的真实一阶胜者快照，作为每个样本都相同的最终死斗守擂者。
# 若文件不存在，PvE 数据仍可运行，但 full_win 只能在另行提供候选人后测量。
DEFAULT_OPPONENT = ROOT / "data" / "real_winners" / "winner_01.json"

# 每个 worker 进程只保存一份原始函数；目标遗物通过真实公开选择流程选入。
_ORIGINAL_RESOLVE = setup_support.resolve_opening_relic


def _probe_opening(seed: int, db_path: str) -> tuple[list[str], list[str]]:
    """用真实开局 action 探测本种子实际出现的遗物/初始道纹候选。"""
    engine = GameEngine(
        db_path=db_path,
        rng_seed=seed,
        sealed_candidate_path=os.devnull,
        death_book_path=os.devnull,
    )
    result = engine.execute_action("setup_attributes", {
        "name": "贾凡", **FIXED_CONFIG["attributes"]
    })
    if not result.get("success"):
        raise RuntimeError(f"setup_attributes failed: {result}")
    relic_choices = list(result.get("result", {}).get("relic_choices", []))
    # 选择本次候选第一件只为推进真实的“遗物后发现初始道纹”流程；
    # 公开发现的随机候选不依赖所选遗物名称，正式样本仍会重新选择目标遗物。
    if relic_choices:
        chosen = engine.execute_action("choose_discovered_relic", {
            "relic_name": relic_choices[0]
        })
        if not chosen.get("success"):
            raise RuntimeError(f"probe choose relic failed: {chosen}")
    return relic_choices, list(engine.state.pending_initial_daowen_choices)


def _select_seeds(requested: int, start_seed: int, max_scan: int,
                  probe_dir: Path) -> dict[str, list[int]]:
    """收集每件遗物 requested 个“目标遗物+固定初始道纹同时出现”的种子。

    同一枚种子如果候选中有多件目标遗物，会被分别收录；这样在这些遗物之间
    尽量形成配对比较，而不是把候选池外的遗物强行塞进玩家。
    """
    selected = {name: [] for name in RELICS}
    for seed in range(start_seed, start_seed + max_scan):
        db_path = str(probe_dir / f"probe_{seed}.db")
        relic_choices, daowen_choices = _probe_opening(seed, db_path)
        if FIXED_CONFIG["initial_daowen"] not in daowen_choices:
            continue
        for relic in relic_choices:
            if relic in selected and len(selected[relic]) < requested:
                selected[relic].append(seed)
        if all(len(v) >= requested for v in selected.values()):
            break
    return selected


def _worker_init() -> None:
    # 子进程从干净的原始函数开始，避免上一个 target 的闭包残留。
    setup_support.resolve_opening_relic = _ORIGINAL_RESOLVE


def _run_one(task: tuple[str, int, str | None, str]) -> dict[str, Any]:
    relic, seed, opponent_path, run_root = task
    # 只在本 worker 的当前局选择目标遗物；选择仍由 engine action 校验候选列表。
    def choose_target(engine, prefer=None):
        return _ORIGINAL_RESOLVE(engine, prefer=relic)

    setup_support.resolve_opening_relic = choose_target
    run_dir = Path(run_root) / f"{os.getpid()}_{seed}_{relic}"
    run_dir.mkdir(parents=True, exist_ok=True)
    sealed_path = run_dir / "sealed.json"
    if opponent_path:
        shutil.copyfile(opponent_path, sealed_path)
    db_path = run_dir / "engine.db"
    death_book_path = run_dir / "death_book.md"
    # 真实运行器会读取死者之书；批量实验使用空的隔离副本，避免跨样本污染。
    death_book_path.write_text("", encoding="utf-8")
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink):
            result = _play(
                FIXED_CONFIG["initial_daowen"],
                list(FIXED_CONFIG["learn"]),
                FIXED_CONFIG["region"],
                seed=seed,
                battles=FIXED_CONFIG["battles"],
                ai_cls=BenchmarkAI,
                spend_shards=FIXED_CONFIG["spend_shards"],
                resonance=FIXED_CONFIG["resonance"],
                attrs=dict(FIXED_CONFIG["attributes"]),
                lab_paths={
                    "db_path": str(db_path),
                    "sealed_path": str(sealed_path),
                    "death_book_path": str(death_book_path),
                },
            )
        valid = not bool(result.get("invalid"))
        cleared = int(result.get("cleared", 0) or 0)
        pm = result.get("pm") or {}
        return {
            "relic": relic,
            "seed": seed,
            "valid": valid,
            "invalid_reason": result.get("reason", "") if not valid else "",
            "cleared_battles": cleared,
            "pve7_completed": valid and cleared >= FIXED_CONFIG["battles"],
            "full_win": valid and bool(result.get("won")),
            "sealed": bool(result.get("sealed")),
            "final_duel_fought": bool(result.get("duel_fought")),
            "final_duel_won": bool(result.get("duel_won")),
            "death_battle": int(pm["battle"]) if pm and pm.get("battle") else None,
            "killer": pm.get("killer", "") if pm else "",
        }
    except Exception as exc:  # 未捕获异常也必须进入 invalid 分母之外的统计。
        return {
            "relic": relic,
            "seed": seed,
            "valid": False,
            "invalid_reason": f"uncaught {type(exc).__name__}: {exc}",
            "cleared_battles": 0,
            "pve7_completed": False,
            "full_win": False,
            "sealed": False,
            "final_duel_fought": False,
            "final_duel_won": False,
            "death_battle": None,
            "killer": "",
        }
    finally:
        # DB 文件不属于结果；清理批量临时文件，避免跑多批积累。
        for path in run_dir.glob("*"):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            run_dir.rmdir()
        except OSError:
            pass


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row["valid"]]
    invalid = [row for row in rows if not row["valid"]]
    death_rows = [row for row in valid if row.get("death_battle") is not None]
    death_distribution = Counter(str(row["death_battle"]) for row in death_rows)
    reasons = Counter(row.get("invalid_reason", "") for row in invalid)
    cleared = [row["cleared_battles"] for row in valid]
    pve_finished = sum(row["pve7_completed"] for row in valid)
    full_wins = sum(row["full_win"] for row in valid)
    duel_fought = sum(row["final_duel_fought"] for row in valid)
    duel_wins = sum(row["final_duel_won"] for row in valid)
    n = len(valid)
    return {
        "requested_runs": len(rows),
        "valid_runs": n,
        "invalid_runs": len(invalid),
        "invalid_reasons": dict(reasons),
        "pve7_completed_runs": pve_finished,
        "pve7_completion_rate": (pve_finished / n if n else None),
        "full_win_runs": full_wins,
        "full_win_rate": (full_wins / n if n else None),
        "final_duel_fought_runs": duel_fought,
        "final_duel_wins": duel_wins,
        "final_duel_win_rate_among_fought": (duel_wins / duel_fought if duel_fought else None),
        "average_cleared_battles": (sum(cleared) / n if n else None),
        "average_survival_battles": (sum(cleared) / n if n else None),
        "survival_distribution": dict(Counter(str(c) for c in cleared)),
        "death_distribution": dict(sorted(death_distribution.items(), key=lambda kv: int(kv[0]))),
        "deaths_observed": len(death_rows),
        "sealed_without_duel": sum(row["sealed"] for row in valid),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=50,
                        help="每件遗物的有效候选样本数（默认50）")
    parser.add_argument("--start-seed", type=int, default=1)
    parser.add_argument("--max-scan", type=int, default=10000,
                        help="寻找开局候选的最大种子扫描宽度")
    parser.add_argument("--workers", type=int, default=max(1, min(8, os.cpu_count() or 1)))
    parser.add_argument("--opponent", default=str(DEFAULT_OPPONENT),
                        help="最终死斗的固定守擂者快照；传空字符串则不预置")
    parser.add_argument("--output", default=str(ROOT / "data" / "real_runs" / "relic_winrate_20260914.json"))
    args = parser.parse_args()
    if args.runs <= 0:
        raise SystemExit("--runs 必须大于0")

    opponent = Path(args.opponent) if args.opponent else None
    if opponent is not None and not opponent.exists():
        raise SystemExit(f"守擂者快照不存在: {opponent}")

    with (
        tempfile.TemporaryDirectory(prefix="relic_probe_", dir="/tmp") as probe_tmp,
        tempfile.TemporaryDirectory(prefix="relic_runs_", dir="/tmp") as run_tmp,
    ):
        selected = _select_seeds(args.runs, args.start_seed, args.max_scan, Path(probe_tmp))
        shortfall = {name: len(seeds) for name, seeds in selected.items() if len(seeds) < args.runs}
        if shortfall:
            raise SystemExit(f"扫描{args.max_scan}枚种子后候选不足: {shortfall}")
        tasks = [
            (relic, seed, str(opponent) if opponent else None, run_tmp)
            for relic in RELICS for seed in selected[relic]
        ]
        # “spawn”会显式重新导入模块，worker 仍只操作真实引擎；fork 则更快。
        ctx = mp.get_context("fork" if hasattr(os, "fork") else "spawn")
        with ctx.Pool(processes=args.workers, initializer=_worker_init) as pool:
            rows = list(pool.imap_unordered(_run_one, tasks, chunksize=1))

    rows.sort(key=lambda row: (RELICS.index(row["relic"]), row["seed"]))
    by_relic = {
        relic: _summary([row for row in rows if row["relic"] == relic])
        for relic in RELICS
    }
    output = {
        "meta": {
            "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "engine": "engine.api.GameEngine via sim.build_learner._play",
            "relic_definitions_source": "GameEngine.RELIC_DEFS",
            "script": "sim/relic_winrate.py",
            "sample_selection": "only seeds where target relic and fixed initial daowen 杀伐 are both in real discovery candidates",
            "opponent_snapshot": str(opponent.relative_to(ROOT)) if opponent else None,
            "notes": [
                "每个种子在候选列表中的每件目标遗物都各跑一局；其余配置固定。",
                "full_win 是引擎 _play 的 won=True，明确包含最终死斗；pve7_completion_rate 单独表示七场PvE完成率。",
                "invalid 不进入胜率、平均清除场数或平均存活场数分母。",
            ],
        },
        "fixed_config": FIXED_CONFIG,
        "relics": RELIC_EFFECTS,
        "seed_counts": {name: len(seeds) for name, seeds in selected.items()},
        "summary_by_relic": by_relic,
        "runs": rows,
    }
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {out_path}")
    for relic in RELICS:
        s = by_relic[relic]
        print(f"{relic}: valid={s['valid_runs']} pve7={s['pve7_completion_rate']} "
              f"full={s['full_win_rate']} avg={s['average_cleared_battles']} "
              f"invalid={s['invalid_runs']}")


if __name__ == "__main__":
    main()
