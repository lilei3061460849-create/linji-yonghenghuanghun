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

from engine.ai_tactics import TacticalAI
from sim.duel_pvp import run_duel_pvp
from sim.random_pvp_support import (
    ATTRIBUTE_BUDGET,
    assemble_duel,
    create_random_character,
    legal_attribute_splits,
)


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
