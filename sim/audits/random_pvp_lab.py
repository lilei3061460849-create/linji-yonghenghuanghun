#!/usr/bin/env python3
"""随机轮回者 PvP 实验室。

双方均由真实开局 action 构建：属性点预算相同（25），血限/法限/速限随机分配；
遗物与初始道纹从各自真实开局发现候选中随机选择。随后将两名真实 Entity
放入最终死斗状态，交给现有 run_duel_pvp 对称交替驱动，不伪造攻击/伤害。
"""
from __future__ import annotations

import argparse
import copy
import json
import random
import tempfile
import sys
import os as _os
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[2])
if not _os.path.isdir(_os.path.join(_ROOT, "engine")):
    raise RuntimeError(f"找不到仓库根目录：{_ROOT}")
sys.path.insert(0, _ROOT)

from engine.api import GameEngine
from engine.ai_tactics import TacticalAI
from engine.enums import GamePhase
from sim.duel_pvp import run_duel_pvp

ATTRIBUTE_BUDGET = 25
REGION = "龙心谷"
RESONANCES = ("转换", "反转", "曲解")


def legal_attribute_splits() -> list[tuple[int, int, int]]:
    """合法的（血限属性点、速限属性点、法限属性点）分配，总额固定25。"""
    return [
        (blood, speed, mana)
        for blood in range(1, ATTRIBUTE_BUDGET + 1)
        for speed in range(2, ATTRIBUTE_BUDGET - blood + 1, 2)
        for mana in range(2, ATTRIBUTE_BUDGET - blood - speed + 1, 2)
        if blood + speed + mana == ATTRIBUTE_BUDGET
    ]


def _require_success(result: dict, step: str) -> dict:
    if not result.get("success"):
        raise RuntimeError(f"{step}失败：{result}")
    return result


def create_random_character(
    seed: int, name: str, workdir: Path,
    attribute_points: tuple[int, int, int] | None = None,
) -> tuple[GameEngine, dict]:
    """使用公开开局 action 创建角色；随机只负责从合法发现候选中选取。"""
    rng = random.Random(seed)
    blood_points, speed_points, mana_points = (
        attribute_points if attribute_points is not None
        else rng.choice(legal_attribute_splits())
    )
    engine = GameEngine(
        db_path=str(workdir / f"{name}.sqlite"),
        rng_seed=seed,
        sealed_candidate_path=str(workdir / f"{name}-sealed.json"),
        death_book_path=str(workdir / f"{name}-death-book.md"),
    )
    setup = _require_success(engine.execute_action("setup_attributes", {
        "name": name,
        "blood_points": blood_points,
        "speed_points": speed_points,
        "mana_points": mana_points,
    }), f"{name}属性分配")

    relic_choices = list(setup["result"]["relic_choices"])
    if not relic_choices:
        raise RuntimeError(f"{name}开局遗物候选为空")
    relic_name = rng.choice(relic_choices)
    _require_success(engine.execute_action("choose_discovered_relic", {
        "relic_name": relic_name,
    }), f"{name}选择开局遗物")

    daowen_choices = list(engine.state.pending_initial_daowen_choices)
    if not daowen_choices:
        raise RuntimeError(f"{name}初始道纹候选为空")
    daowen_name = rng.choice(daowen_choices)
    _require_success(engine.execute_action("setup_choose_initial_daowen", {
        "daowen_name": daowen_name,
    }), f"{name}选择初始道纹")

    resonance = rng.choice(RESONANCES)
    _require_success(engine.execute_action("setup_choose_resonance", {
        "resonance_type": resonance,
    }), f"{name}选择残韵")
    _require_success(engine.execute_action("setup_choose_region", {
        "region": REGION,
    }), f"{name}选择副本")

    player = engine.state.player
    # 兼容 PvP 双侧残韵读取：挑战者走 State 兼容账本，守擂者走 Entity 独立账本。
    player.resonance = dict(engine.state.resonance)
    build = {
        "name": name,
        "attribute_points": {
            "blood": blood_points,
            "speed": speed_points,
            "mana": mana_points,
            "total": blood_points + speed_points + mana_points,
        },
        "stats": {
            "blood_limit": player.blood_limit,
            "mana_limit": player.mana_limit,
            "speed_limit": player.speed_limit,
            "action_count": player.action_count,
        },
        "relic": relic_name,
        "relic_choices": relic_choices,
        "daowen": daowen_name,
        "daowen_choices": daowen_choices,
        "resonance": resonance,
    }
    return engine, build


def _initiative_key(entity) -> tuple[int, int, int, int]:
    return (entity.speed_limit, entity.mana_limit, entity.blood_limit, entity.current_hp)


def assemble_duel(challenger_engine: GameEngine, defender_engine: GameEngine,
                  seed: int) -> str:
    """把两个独立合法开局角色组装为 PvP 状态；返回先手侧。"""
    challenger = challenger_engine.state.player
    defender = copy.deepcopy(defender_engine.state.player)
    if challenger is None or defender is None:
        raise RuntimeError("随机角色构建不完整")

    defender.name = defender.name or "随机守擂者"
    defender.resonance = dict(defender_engine.state.resonance)
    defender.relics = copy.deepcopy(defender_engine.state.relics)

    state = challenger_engine.state
    state.enemies = [defender]
    state.opponent_relics = copy.deepcopy(defender_engine.state.relics)
    state.in_final_duel = True
    state.phase = GamePhase.IN_COMBAT.value
    state.combat_subphase = "await_round_start"
    state.duel_rounds_started = 0

    left = _initiative_key(challenger)
    right = _initiative_key(defender)
    if left == right:
        first = random.Random(seed ^ 0xD0E1).choice(("player_side", "opponent_side"))
        state.duel_tie_alternating = True
    else:
        first = "player_side" if left > right else "opponent_side"
        state.duel_tie_alternating = False
    state.duel_round_first = first
    state.duel_turn = first
    return first


def run_one(seed: int, max_rounds: int, max_wall_seconds: float) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"random-pvp-{seed}-") as tmp:
        workdir = Path(tmp)
        # 同预算、不同三维：从全部合法分配中无放回抽两套，避免偶然抽成完全镜像。
        split_rng = random.Random(seed ^ 0xB17)
        challenger_points, defender_points = split_rng.sample(legal_attribute_splits(), 2)
        challenger_engine, challenger_build = create_random_character(
            seed * 2 + 1, "随机挑战者", workdir, challenger_points)
        defender_engine, defender_build = create_random_character(
            seed * 2 + 2, "随机守擂者", workdir, defender_points)
        first = assemble_duel(challenger_engine, defender_engine, seed)
        log: list[str] = []
        result = run_duel_pvp(
            challenger_engine,
            max_rounds=max_rounds,
            max_steps=400,
            max_wall_seconds=max_wall_seconds,
            use_tactical=True,
            ai_cls=TacticalAI,
            log=log,
        )
        return {
            "seed": seed,
            "ai_policy": "TacticalAI (both sides use the same policy)",
            "attribute_point_budget": ATTRIBUTE_BUDGET,
            "first_mover": first,
            "challenger": challenger_build,
            "defender": defender_build,
            "duel": result,
            "battle_log": log,
            "battle_channel": copy.deepcopy(challenger_engine.state.battle_channel),
            "combat_event_count": len(challenger_engine.state.combat_events),
            "action_history": copy.deepcopy(challenger_engine.get_action_history()),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="随机轮回者真实 PvP 对战实验室")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20261118)
    parser.add_argument("--max-rounds", type=int, default=40)
    parser.add_argument("--max-wall-seconds", type=float, default=30.0)
    parser.add_argument("--output-dir", default="artifacts/random-pvp")
    args = parser.parse_args()
    if args.runs < 1 or args.runs > 100:
        raise SystemExit("--runs 必须在1至100之间")
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    results = []
    for index in range(args.runs):
        seed = args.seed + index
        try:
            row = run_one(seed, args.max_rounds, args.max_wall_seconds)
            results.append(row)
            duel = row["duel"]
            print(f"[DUEL] seed={seed} winner={duel.get('winner')} "
                  f"rounds={duel.get('rounds')} error={duel.get('error', False)}")
            print(f"       challenger={row['challenger']['attribute_points']} "
                  f"{row['challenger']['relic']} / {row['challenger']['daowen']}")
            print(f"       defender={row['defender']['attribute_points']} "
                  f"{row['defender']['relic']} / {row['defender']['daowen']}")
            if duel.get("error"):
                print(f"[ISSUE] seed={seed}: {duel.get('reason')}")
        except Exception as exc:
            results.append({"seed": seed, "error": True, "exception": repr(exc)})
            print(f"[ERROR] seed={seed}: {exc!r}")

    report = {
        "runs": args.runs,
        "completed": sum(1 for x in results if "duel" in x),
        "errors": [x for x in results if x.get("error")],
        "results": results,
    }
    (output / "random_pvp_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 随机轮回者 PvP 实验报告", "",
             f"- 请求对局：{args.runs}",
             f"- 完成对局：{report['completed']}",
             f"- 程序异常：{len(report['errors'])}", ""]
    for row in results:
        if "duel" not in row:
            lines.append(f"## Seed {row['seed']}：构建/运行异常")
            lines.append(f"- {row.get('exception', 'unknown')}")
            continue
        d = row["duel"]
        lines += [
            f"## Seed {row['seed']}",
            f"- AI：{row.get('ai_policy', '未指定')}",
            f"- 先手：{row['first_mover']}",
            f"- 胜者：{d.get('winner') or '未判定'}；回合：{d.get('rounds')}",
            f"- 结论：{d.get('reason', '')}",
            f"- 挑战者：{row['challenger']['stats']}；属性点：{row['challenger']['attribute_points']}；"
            f"遗物：{row['challenger']['relic']}；道纹：{row['challenger']['daowen']}",
            f"- 守擂者：{row['defender']['stats']}；属性点：{row['defender']['attribute_points']}；"
            f"遗物：{row['defender']['relic']}；道纹：{row['defender']['daowen']}",
            f"- 对局错误：{d.get('error', False)}",
            "",
        ]
        lines.extend(row.get("battle_log", []))
        lines.append("")
    (output / "random_pvp_report.md").write_text("\n".join(lines), encoding="utf-8")
    if report["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
