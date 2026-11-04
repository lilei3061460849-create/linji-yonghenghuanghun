#!/usr/bin/env python3
"""对抗性配对探针：训练权重 / 默认权重 / 纯随机，三者同场对打。

为什么需要它（2026-10-03 复盘）：
    「所有席位都用同一套权重」的评测里，每场最多只有 1 个唯一幸存者，
    所以 Σ(各席胜场) ＝ 分出唯一幸存者的场次数——这个总和由「战斗打不打得出结果」决定，
    不由「谁打得聪明」决定。在这种口径下训练权重**不可能**把平均赢率抬起来，
    只能重新分配赢家。真正的问法是：**两套权重同场对打，谁拿走更多胜场。**

三个对照：
  · 训练权重 vs 默认权重——训练到底有没有练出优势；
  · 默认权重 vs 纯随机——「选得聪明」在这套沙盒里值多少；
  · 阵容身份榜——同一套权重下，各怪物的每席胜率（看结局是不是主要由「你是哪只怪」决定）。

设计（消除座位/先手红利）：
    每个阵容跑 k 次（k＝怪物数），第 i 次让「被考察方」坐第 i 个席位、其余席位用对手策略。
    被考察方轮换过所有座位，聚合后比较每席胜率。基准（无优势）＝同池唯一幸存者席位数占比。

用法（仓库根目录）：
    python sim/probes/probe_arena_head2head.py --seeds 777,1234,20260101 --matches 12 --monsters 3
    python sim/probes/probe_arena_head2head.py --seeds 20261003 --matches 12 --monsters 3
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
import collections
import json
import math
import sys
from pathlib import Path

ROOT = _ROOT
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sim"))

import monster_arena as A  # noqa: E402

# 策略 =（权重, 决策模式）。random 模式忽略权重、等概率挑合法候选。
POLICIES = {"train": ("weights", "best"), "default": ("prior", "best"), "random": ("prior", "random")}


def _run(lineup, seed, rounds, hp_scale, sets, modes, free_actions=False):
    return A.run_match(lineup, dict(A.DEFAULT_WEIGHTS), seed=seed, max_rounds=rounds,
                       weight_sets=sets, choice_modes=modes, hp_scale=hp_scale,
                       free_actions=free_actions)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=str, default="777,1234,20260101")
    ap.add_argument("--matches", type=int, default=12)
    ap.add_argument("--monsters", type=int, default=3)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--hp-scale", type=float, default=0.25, dest="hp_scale")
    ap.add_argument("--weights", type=Path, default=A.WEIGHTS_PATH, help="被考察权重文件")
    ap.add_argument("--vs", type=Path, default=None,
                    help="对手权重文件（默认＝人工先验 DEFAULT_WEIGHTS）")
    ap.add_argument("--free-actions", action="store_true", dest="free_actions",
                    help="在「怪物行动自由化」规则下对打（默认关＝原契约）")
    args = ap.parse_args()

    seeds = [int(x) for x in args.seeds.split(",") if x.strip()]
    w = {"weights": A.load_weights(args.weights), "prior": dict(A.DEFAULT_WEIGHTS)}
    if args.vs is not None:
        w["prior"] = A.load_weights(args.vs)
    empty = {"seats": 0, "wins": 0, "fitness": 0.0, "self_kills": 0}

    pairs = [("train", "default"), ("default", "random")]
    out = {}
    for a, b in pairs:
        ta, tb = collections.Counter(empty), collections.Counter(empty)
        resolved = runs = 0
        for sd in seeds:
            for k, lineup in enumerate(A._lineups(A.monster_pool(), args.monsters, args.matches, sd)):
                for seat in range(len(lineup)):
                    sets = [w[POLICIES[a][0]] if i == seat else w[POLICIES[b][0]]
                            for i in range(len(lineup))]
                    modes = [POLICIES[a][1] if i == seat else POLICIES[b][1] for i in range(len(lineup))]
                    res = _run(lineup, sd + k, args.rounds, args.hp_scale, sets, modes,
                               args.free_actions)
                    runs += 1
                    winner = res["survivors"][0] if len(res["survivors"]) == 1 else None
                    resolved += 1 if winner is not None else 0
                    for i, st in res["stats"].items():
                        t = ta if i == seat else tb
                        t["seats"] += 1
                        t["self_kills"] += 1 if st["self_kill"] else 0
                        t["fitness"] += A.fitness(res, i, sets[i])
                        if winner == i:
                            t["wins"] += 1
        n = ta["seats"] + tb["seats"]
        p_null = resolved / n if n else 0.0
        sd_a = math.sqrt(p_null * (1 - p_null) / ta["seats"]) if ta["seats"] else 0.0
        row = {"a": a, "b": b, "runs": runs, "resolved": resolved,
               "seats_a": ta["seats"], "wins_a": ta["wins"],
               "seats_b": tb["seats"], "wins_b": tb["wins"],
               "win_rate_a": round(ta["wins"] / ta["seats"], 4) if ta["seats"] else 0.0,
               "win_rate_b": round(tb["wins"] / tb["seats"], 4) if tb["seats"] else 0.0,
               "fitness_a": round(ta["fitness"] / ta["seats"], 4) if ta["seats"] else 0.0,
               "fitness_b": round(tb["fitness"] / tb["seats"], 4) if tb["seats"] else 0.0,
               "self_kills_a": ta["self_kills"], "self_kills_b": tb["self_kills"],
               "null_win_rate": round(p_null, 4), "sigma": round(sd_a, 4)}
        out[f"{a} vs {b}"] = row
        print(f"【{a} vs {b}】{runs} 场（分出唯一幸存者 {resolved}）｜每席胜率："
              f"{a} {row['win_rate_a']:.3f}（{ta['wins']}/{ta['seats']} 席） "
              f"vs {b} {row['win_rate_b']:.3f}（{tb['wins']}/{tb['seats']} 席）｜席均适应度 "
              f"{row['fitness_a']:.3f} vs {row['fitness_b']:.3f}｜同池基准 {row['null_win_rate']:.3f}"
              f"｜偏差 {(row['win_rate_a'] - row['win_rate_b']) / sd_a if sd_a else 0:+.2f}σ")

    # 阵容身份榜：同一套默认权重、全部席位 best，看各怪物的每席胜率
    per = collections.defaultdict(lambda: {"seats": 0, "wins": 0})
    for sd in seeds:
        for k, lineup in enumerate(A._lineups(A.monster_pool(), args.monsters, args.matches, sd)):
            res = _run(lineup, sd + k, args.rounds, args.hp_scale,
                       [w["prior"]] * len(lineup), ["best"] * len(lineup), args.free_actions)
            winner = res["survivors"][0] if len(res["survivors"]) == 1 else None
            for i, m in enumerate(res["lineup"]):
                per[m]["seats"] += 1
                if winner == i:
                    per[m]["wins"] += 1
    ranked = sorted(per.items(), key=lambda kv: -kv[1]["wins"] / kv[1]["seats"])
    print("\n【阵容身份榜（同权重同策略，只看『你是哪只怪』）】")
    for name, d in ranked:
        print(f"  {name}: {d['wins']}/{d['seats']} = {d['wins'] / d['seats']:.3f}")
    print(json.dumps({"head2head": out,
                      "identity_win_rate": {k: round(v["wins"] / v["seats"], 4)
                                            for k, v in ranked}}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()