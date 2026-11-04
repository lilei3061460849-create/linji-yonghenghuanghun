#!/usr/bin/env python3
"""对照探针：两套权重到底有没有改变怪物的实际行为？

用途（回答「训练有没有真的改变策略」这个问法本身）：
    用同一批阵容、同一批种子各跑一遍，逐次决策比较「回合/怪物/道纹/X/攻击目标」。
输出四类数：
  · 可对齐决策数——两边都在同一 (回合, 怪物) 上出手的次数；
  · 选择不同的决策数——道纹名、X、攻击目标任一不同；
  · 结果不同的场数——幸存者名单或回合数不同；
  · 各套权重的汇总战绩（赢率 / 自爆率 / 场均输出）。

不改变任何生产规则；只读 `sim/monster_arena.py` 的公开函数。

用法（仓库根目录）：
    python sim/probes/probe_arena_weight_divergence.py --seeds 777,1234,20260101 --matches 8 --monsters 3
    python sim/probes/probe_arena_weight_divergence.py --other /path/to/weights.json
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
import json
import sys
from pathlib import Path

ROOT = _ROOT
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sim"))

import monster_arena as A  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=str, default="777,1234,20260101")
    ap.add_argument("--matches", type=int, default=8)
    ap.add_argument("--monsters", type=int, default=3)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--hp-scale", type=float, default=0.25, dest="hp_scale")
    ap.add_argument("--other", type=Path, default=A.WEIGHTS_PATH,
                    help="对照权重文件（默认对比：人工先验 vs 该文件）")
    args = ap.parse_args()

    seeds = [int(x) for x in args.seeds.split(",") if x.strip()]
    w_base = dict(A.DEFAULT_WEIGHTS)
    w_other = A.load_weights(args.other)

    total = common = diff = matches = result_diff = 0
    base_name = "默认权重（人工先验）"
    other_name = f"对照权重（{args.other.name}）"
    summary = {base_name: {"seats": 0, "wins": 0, "self_kills": 0, "damage": 0},
               other_name: {"seats": 0, "wins": 0, "self_kills": 0, "damage": 0}}
    for sd in seeds:
        lineups = A._lineups(A.monster_pool(), args.monsters, args.matches, sd)
        for k, lineup in enumerate(lineups):
            logs = {}
            outs = {}
            for tag, w in ((base_name, w_base), (other_name, w_other)):
                log: list = []
                res = A.run_match(lineup, w, seed=sd + k, max_rounds=args.rounds,
                                  hp_scale=args.hp_scale, choice_log=log)
                logs[tag], outs[tag] = log, res
                s = summary[tag]
                for i, st in res["stats"].items():
                    s["seats"] += 1
                    s["self_kills"] += 1 if st["self_kill"] else 0
                    s["damage"] += st["dmg_out"]
                s["wins"] += 1 if len(res["survivors"]) == 1 else 0
            matches += 1
            key = lambda row: (row["round"], row["monster"], row["actor"])   # noqa: E731
            la, lb = {key(r): r for r in logs[base_name]}, {key(r): r for r in logs[other_name]}
            for kk in set(la) & set(lb):
                common += 1
                if (la[kk]["daowen"], la[kk]["x"], la[kk]["targets"]) != \
                        (lb[kk]["daowen"], lb[kk]["x"], lb[kk]["targets"]):
                    diff += 1
            total += max(len(la), len(lb))
            if (outs[base_name]["survivors"], outs[base_name]["rounds"]) != \
                    (outs[other_name]["survivors"], outs[other_name]["rounds"]):
                result_diff += 1

    print(f"种子 {seeds}｜{matches} 场 × {args.monsters} 只｜回合上限 {args.rounds}")
    print(f"可对齐决策 {common}／总决策 {total}；其中选择不同 {diff}"
          f"（{(diff / common * 100) if common else 0:.1f}%）")
    print(f"结果不同（幸存者或回合数）{result_diff}／{matches} 场")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()