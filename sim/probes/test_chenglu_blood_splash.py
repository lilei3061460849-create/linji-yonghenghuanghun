#!/usr/bin/env python3
"""测试【承露盏】+【再生/透支/杀伐】+三阶法术【血溅五步】。

这是针对一个明确构筑的真实引擎测试，不是遗物池粗扫：
1. 开局真实发现并选择【承露盏】；
2. 开局初始道纹固定选【杀伐】（否则开局3点精力不足以在首战前同时学完
   再生、透支和三阶自创法术）；
3. 首个局外窗口强制按【再生】→【透支】学习；
4. 三者齐备后，通过 GameEngine 的 custom_spell 审核/批准流程学习【血溅五步】；
5. 之后用当前完整 _play 战斗流程测试七场 PvE。

运行：
  PYTHONPATH=. /tmp/linji-venv/bin/python sim/probes/test_chenglu_blood_splash.py --runs 50 --workers 8
"""
from __future__ import annotations

# --- 2026-11-04：脚本放进子目录后，不再靠"往上数几层"猜仓库根 ---
# 向上找到同时含 engine/ 与 sim/ 的目录就是仓库根；再附带 sim/ 让老式裸模块名
# import（如 `import balance_sim`）继续可用。搬动本文件不需要再改这几行。
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.abspath(__file__))
while _ROOT != _os.path.dirname(_ROOT) and not _os.path.isdir(_os.path.join(_ROOT, "engine")):
    _ROOT = _os.path.dirname(_ROOT)
for _p in (_ROOT, _os.path.join(_ROOT, "sim"), _os.path.join(_ROOT, "tests")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)


import argparse
import contextlib
import datetime as dt
import json
import math
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = _ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.api import GameEngine
import sim.build_learner as build_learner
import tests.setup_support as setup_support
from sim.build_learner import _play
from sim.relic_winrate import BenchmarkAI

TARGET_RELIC = "承露盏"
INITIAL_DAOWEN = "杀伐"
# 一阶怪物面板口径（唯一事实源见 sim/audit_monsters.py）：
# 2026-09-14 曾试压到 35 点，改回 60 点并原样恢复 60 点时代的 36 只面板。
# 本文件只做结果标注，不参与战斗结算；跑完请用 `python sim/audit_monsters.py` 复核面板合规。
TIER1_MONSTER_ATTRIBUTE_BUDGET = 60
ATTRS = {"blood_points": 7, "speed_points": 6, "mana_points": 12}
REGION = "龙心谷"
RESONANCE = "反转"
LEARN = ["再生", "透支"]
BLOOD_SPLASH = {
    "name": "血溅五步",
    "required_daowen": ["再生", "透支", "杀伐"],
    "trigger_condition": "失去生命后",
    # DSL 要求每一步显式写目标身份；“攻击者”就是本次受击的怪物。
    # 触发时法力≥2才走再生→杀伐→透支；法力不足时跳过杀伐，
    # 走再生→透支把法力循环起来。
    "effect_flow": "若自身 法力 大于等于 2 则 发动再生X于自身；发动杀伐X于攻击者；发动透支X于自身 否则 发动再生X于自身；发动透支X于自身→循环",
}
DEFAULT_OPPONENT = ROOT / "data" / "real_winners" / "winner_01.json"
_ORIGINAL_RESOLVE = setup_support.resolve_opening_relic
_ORIGINAL_CHOOSE = build_learner.choose_pre_battle


def _assert_panel_budget_matches_docs() -> int:
    """口径哨兵：结果里标注的一阶面板预算必须等于真面板的最大成本。

    2026-09-15 教训：本文件曾把预算写死成 35，于是 60 点池的复测产物里同时出现
    60 与 35 两个口径。现在改为真解析 36 只一阶面板，标注预算与实际不符就直接拒绝跑。
    """
    import sim.balance_sim as balance_sim
    monsters = [m for m in balance_sim.parse_monsters()
                if m["region"] in {"扭曲都市", "罪孽都市", "龙心谷"}]
    if len(monsters) != 36:
        raise SystemExit(f"一阶怪物池应解析出36只，实得{len(monsters)}；先跑 python sim/audit_monsters.py")
    def _cost(m: dict) -> int:
        return math.ceil(m["hp"] / 6) + 2 * m["ac"] + 2 * m["ap"]
    top = max(monsters, key=_cost)
    if _cost(top) > TIER1_MONSTER_ATTRIBUTE_BUDGET:
        raise SystemExit(
            f"口径不一致：{top['name']} 面板成本 {_cost(top)} > 标注预算 {TIER1_MONSTER_ATTRIBUTE_BUDGET}；"
            f"先跑 python sim/audit_monsters.py 复核面板")
    return _cost(top)


def _probe(seed: int, db_path: str) -> tuple[list[str], list[str]]:
    e = GameEngine(db_path=db_path, rng_seed=seed,
                   sealed_candidate_path=os.devnull, death_book_path=os.devnull)
    r = e.execute_action("setup_attributes", {"name": "贾凡", **ATTRS})
    if not r.get("success"):
        raise RuntimeError(r)
    relics = list(r["result"]["relic_choices"])
    if TARGET_RELIC not in relics:
        return relics, []
    r = e.execute_action("choose_discovered_relic", {"relic_name": TARGET_RELIC})
    if not r.get("success"):
        raise RuntimeError(r)
    return relics, list(e.state.pending_initial_daowen_choices)


def _select_seeds(runs: int, start: int, max_scan: int, probe_dir: Path) -> list[int]:
    out = []
    for seed in range(start, start + max_scan):
        _relics, daowen = _probe(seed, str(probe_dir / f"{seed}.db"))
        if INITIAL_DAOWEN in daowen:
            out.append(seed)
        if len(out) >= runs:
            return out
    return out


def _worker_init() -> None:
    setup_support.resolve_opening_relic = _ORIGINAL_RESOLVE
    build_learner.choose_pre_battle = _ORIGINAL_CHOOSE


def _run_one(task: tuple[int, str | None, str]) -> dict[str, Any]:
    seed, opponent_path, run_root = task

    def choose_relic(engine, prefer=None):
        return _ORIGINAL_RESOLVE(engine, prefer=TARGET_RELIC)

    # 只对首个局外窗口注入用户指定的两个道纹学习动作；动作本身仍经引擎校验。
    opening_plan = [
        ("学习", {"sub": "daowen", "tier": 1, "name": "再生"}),
        ("学习", {"sub": "daowen", "tier": 1, "name": "透支"}),
    ]

    def choose_opening_plan(engine, todo, battle_no, rng, policy):
        if opening_plan:
            action, params = opening_plan.pop(0)
            return action, params
        return _ORIGINAL_CHOOSE(engine, todo, battle_no, rng, policy)

    setup_support.resolve_opening_relic = choose_relic
    build_learner.choose_pre_battle = choose_opening_plan
    run_dir = Path(run_root) / f"{os.getpid()}_{seed}"
    run_dir.mkdir(parents=True, exist_ok=True)
    sealed = run_dir / "sealed.json"
    if opponent_path:
        shutil.copyfile(opponent_path, sealed)
    book = run_dir / "death_book.md"
    book.write_text("", encoding="utf-8")
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink):
            result = _play(
                INITIAL_DAOWEN,
                list(LEARN),
                REGION,
                seed=seed,
                battles=7,
                spend_shards=False,
                policy=None,
                resonance=RESONANCE,
                attrs=dict(ATTRS),
                spell_plan=[dict(BLOOD_SPLASH)],
                ai_cls=BenchmarkAI,
                # 终止归因需要引擎提供死亡上下文；否则“本场未清除”会被
                # 运行器误写成“死亡场次”。
                death_trace=True,
                lab_paths={
                    "db_path": str(run_dir / "engine.db"),
                    "sealed_path": str(sealed),
                    "death_book_path": str(book),
                },
            )
        valid = not bool(result.get("invalid"))
        pm = result.get("pm") or {}
        trace = result.get("death_trace") or {}
        death_observed = bool(trace.get("death_subtype"))
        sculpture_observed = (not death_observed
                              and any(str(item).endswith("雕塑")
                                      for item in trace.get("unused_items", [])))
        termination_battle = pm.get("battle") if pm else None
        termination_kind = ("death" if death_observed else
                            "sculpture" if sculpture_observed else
                            "nondeath_termination" if termination_battle is not None else "unknown")
        return {
            "seed": seed,
            "valid": valid,
            "invalid_reason": result.get("reason", "") if not valid else "",
            "cleared_battles": int(result.get("cleared", 0) or 0),
            "pve7_completed": valid and int(result.get("cleared", 0) or 0) >= 7,
            "full_win": valid and bool(result.get("won")),
            "final_duel_fought": bool(result.get("duel_fought")),
            "final_duel_won": bool(result.get("duel_won")),
            # death_battle 只记录确有死亡上下文的局；此前直接使用 pm.battle，
            # 会把40回合未清场但玩家仍存活的局误计为死亡。
            "death_battle": trace.get("battle") if death_observed else None,
            "death_subtype": trace.get("death_subtype", "") if death_observed else "",
            "death_source": trace.get("death_source", "") if death_observed else "",
            "termination_battle": termination_battle,
            "termination_kind": termination_kind,
            "sculpture_termination": sculpture_observed,
            "termination_primary": "sculpture" if sculpture_observed else trace.get("primary", ""),
            "killer": pm.get("killer", "") if pm else "",
            "spell_plan": "血溅五步",
        }
    except Exception as exc:
        return {
            "seed": seed, "valid": False,
            "invalid_reason": f"uncaught {type(exc).__name__}: {exc}",
            "cleared_battles": 0, "pve7_completed": False, "full_win": False,
            "final_duel_fought": False, "final_duel_won": False,
            "death_battle": None, "death_subtype": "", "death_source": "",
            "termination_battle": None, "termination_kind": "unknown",
            "sculpture_termination": False, "termination_primary": "",
            "killer": "", "spell_plan": "血溅五步",
        }
    finally:
        setup_support.resolve_opening_relic = _ORIGINAL_RESOLVE
        build_learner.choose_pre_battle = _ORIGINAL_CHOOSE
        for path in run_dir.glob("*"):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            run_dir.rmdir()
        except OSError:
            pass


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [r for r in rows if r["valid"]]
    invalid = [r for r in rows if not r["valid"]]
    n = len(valid)
    cleared = [r["cleared_battles"] for r in valid]
    deaths = [r for r in valid if r.get("death_battle") is not None]
    nondeath_terminations = [r for r in valid
                             if r.get("termination_kind") == "nondeath_termination"]
    sculptures = [r for r in valid if r.get("sculpture_termination")]
    duel = [r for r in valid if r["final_duel_fought"]]
    return {
        "requested_runs": len(rows),
        "valid_runs": n,
        "invalid_runs": len(invalid),
        "invalid_reasons": dict(Counter(r["invalid_reason"] for r in invalid)),
        "pve7_completed_runs": sum(r["pve7_completed"] for r in valid),
        "pve7_completion_rate": sum(r["pve7_completed"] for r in valid) / n if n else None,
        "full_win_runs": sum(r["full_win"] for r in valid),
        "full_win_rate": sum(r["full_win"] for r in valid) / n if n else None,
        "final_duel_fought_runs": len(duel),
        "final_duel_wins": sum(r["final_duel_won"] for r in duel),
        "average_cleared_battles": sum(cleared) / n if n else None,
        "average_survival_battles": sum(cleared) / n if n else None,
        "survival_distribution": dict(Counter(str(c) for c in cleared)),
        "termination_distribution": dict(Counter(r.get("termination_kind", "unknown")
                                                   for r in valid)),
        "termination_battle_distribution": dict(sorted(
            Counter(str(r["termination_battle"]) for r in valid
                    if r.get("termination_battle") is not None).items(),
            key=lambda item: int(item[0]))),
        "death_distribution": dict(sorted(Counter(str(r["death_battle"]) for r in deaths).items(),
                                           key=lambda item: int(item[0]))),
        "death_subtype_distribution": dict(Counter(r.get("death_subtype", "") for r in deaths)),
        "death_source_distribution": dict(Counter(r.get("death_source", "") for r in deaths)),
        "nondeath_termination_distribution": dict(Counter(
            r.get("termination_primary", "") for r in nondeath_terminations)),
        "deaths_observed": len(deaths),
        "sculpture_terminations_observed": len(sculptures),
        "nondeath_terminations_observed": len(nondeath_terminations),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--start-seed", type=int, default=1)
    ap.add_argument("--max-scan", type=int, default=10000)
    ap.add_argument("--workers", type=int, default=max(1, min(8, os.cpu_count() or 1)))
    ap.add_argument("--opponent", default=str(DEFAULT_OPPONENT))
    default_out = ROOT / "data" / "real_runs" / (
        f"chenglu_blood_splash_tier1_{TIER1_MONSTER_ATTRIBUTE_BUDGET}_"
        f"{dt.date.today().strftime('%Y%m%d')}.json")
    ap.add_argument("--output", default=str(default_out))
    args = ap.parse_args()
    opponent = Path(args.opponent) if args.opponent else None
    if opponent is not None and not opponent.exists():
        raise SystemExit(f"守擂者快照不存在: {opponent}")
    top_cost = _assert_panel_budget_matches_docs()

    with tempfile.TemporaryDirectory(prefix="chenglu_probe_", dir="/tmp") as probe_tmp, \
            tempfile.TemporaryDirectory(prefix="chenglu_runs_", dir="/tmp") as run_tmp:
        seeds = _select_seeds(args.runs, args.start_seed, args.max_scan, Path(probe_tmp))
        if len(seeds) < args.runs:
            raise SystemExit(f"有效开局候选不足：{len(seeds)}/{args.runs}")
        tasks = [(seed, str(opponent) if opponent else None, run_tmp) for seed in seeds]
        ctx = mp.get_context("fork" if hasattr(os, "fork") else "spawn")
        with ctx.Pool(args.workers, initializer=_worker_init) as pool:
            rows = list(pool.imap_unordered(_run_one, tasks, chunksize=1))

    rows.sort(key=lambda r: r["seed"])
    result = {
        "meta": {
            "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "engine": "engine.api.GameEngine via sim.build_learner._play",
            "script": "sim/probes/test_chenglu_blood_splash.py",
            "opponent_snapshot": str(opponent.relative_to(ROOT)) if opponent else None,
            "definition": "valid run; pve7=7场PvE完成；full_win=_play won=True（包含最终死斗）",
            "tier1_panel_budget": TIER1_MONSTER_ATTRIBUTE_BUDGET,
            "tier1_panel_max_cost": top_cost,
            "tier1_panel_note": ("恢复60点：原样恢复60点时代的36只面板，"
                                 "9只单段高攻怪血限-6以合规；口径见 sim/audit_monsters.py"),
        },
        "fixed_config": {
            "relic": TARGET_RELIC,
            "tier1_monster_attribute_budget": TIER1_MONSTER_ATTRIBUTE_BUDGET,
            "monster_attribute_pricing_formula": "ceil(血限/6)+2×攻击次数+2×攻击力",
            "attributes": ATTRS,
            "initial_daowen": INITIAL_DAOWEN, "learn": LEARN,
            "spell": BLOOD_SPLASH, "region": REGION,
            "resonance": RESONANCE,
            "ai": "BenchmarkAI = WinOnlyAI + each battle max one Seal activation",
            "spend_shards": False,
            "opening_plan": ["学习再生", "学习透支", "学习血溅五步"],
        },
        "summary": summarize(rows),
        "runs": rows,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {out}")
    print(json.dumps(result["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()