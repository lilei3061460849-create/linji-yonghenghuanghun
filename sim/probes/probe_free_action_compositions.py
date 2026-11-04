#!/usr/bin/env python3
"""固定行动构成的对照探针（规则实验：怪物行动自由化）。

问题（2026-10-03 用户提出）：不再强制怪物发动道纹之后，怪物还会不会用道纹？

办法：在同一条规则下（每回合 2 个槽），让每只怪物**固定**采用一种行动构成，
其余一切照常（同一批阵容、种子、道纹与伤害规则），比较：

    攻击×2      两个槽都用来打人
    一攻一道纹  1 组攻击 + 1 个道纹（＝旧契约的构成）
    道纹×2      两个槽都用来发动道纹（该怪有 ≥2 个合法道纹时才可能）

输出每组的赢率、席均适应度、自爆、场均输出、平均回合与行动构成计数。

用法（仓库根目录）：
    python sim/probes/probe_free_action_compositions.py --seeds 777,1234,20260101 --matches 8 --monsters 3
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

COMPOSITIONS = ["attack2", "mixed", "dao2"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=str, default="777,1234,20260101")
    ap.add_argument("--matches", type=int, default=8)
    ap.add_argument("--monsters", type=int, default=3)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--hp-scale", type=float, default=0.25, dest="hp_scale")
    ap.add_argument("--weights", type=Path, default=A.WEIGHTS_PATH)
    args = ap.parse_args()

    seeds = [int(x) for x in args.seeds.split(",") if x.strip()]
    weights = A.load_weights(args.weights)
    agg = {c: {"seats": 0, "wins": 0, "fitness": 0.0, "self_kills": 0, "damage": 0,
               "rounds": 0.0, "comp": {}} for c in COMPOSITIONS}
    for sd in seeds:
        for k, lineup in enumerate(A._lineups(A.monster_pool(), args.monsters, args.matches, sd)):
            for comp in COMPOSITIONS:
                res = A.run_match(lineup, weights, seed=sd + k, max_rounds=args.rounds,
                                  hp_scale=args.hp_scale, free_actions=True,
                                  choice_modes=[comp] * len(lineup))
                for key, val in (res.get("composition") or {}).items():
                    agg[comp]["comp"][key] = agg[comp]["comp"].get(key, 0) + val
                for i, st in res["stats"].items():
                    a = agg[comp]
                    a["seats"] += 1
                    a["self_kills"] += 1 if st["self_kill"] else 0
                    a["damage"] += st["dmg_out"]
                    a["fitness"] += A.fitness(res, i, weights)
                if len(res["survivors"]) == 1:
                    agg[comp]["wins"] += 1
                agg[comp]["rounds"] += res["rounds"]

    print(f"种子 {seeds}｜{len(seeds) * args.matches} 组阵容 × {args.monsters} 只"
          f"（权重：{args.weights.name}）")
    out = {}
    for comp in COMPOSITIONS:
        a = agg[comp]
        n = a["seats"] or 1
        m = a["rounds"] / (len(seeds) * args.matches)
        out[comp] = {"win_rate": round(a["wins"] / (len(seeds) * args.matches), 3),
                     "seat_fitness": round(a["fitness"] / n, 3),
                     "self_kills": a["self_kills"],
                     "avg_damage_per_seat": round(a["damage"] / n, 1),
                     "avg_rounds": round(m, 1), "composition": a["comp"]}
        print(f"  {comp:>6}: 分出胜负 {a['wins']}/{len(seeds) * args.matches} 场"
              f"｜席均适应度 {out[comp]['seat_fitness']:.3f}｜自爆 {a['self_kills']}"
              f"｜每席输出 {out[comp]['avg_damage_per_seat']:.1f}｜平均回合 {out[comp]['avg_rounds']}"
              f"｜构成 {a['comp']}")
    # 同场对打（座位轮换，抵消座位/先手红利）：attack2 vs mixed、attack2 vs dao2
    h2h = {}
    for a_comp, b_comp in (("attack2", "mixed"), ("attack2", "dao2")):
        tally = {a_comp: {"seats": 0, "wins": 0}, b_comp: {"seats": 0, "wins": 0}}
        resolved = runs = 0
        for sd in seeds:
            for k, lineup in enumerate(A._lineups(A.monster_pool(), args.monsters, args.matches, sd)):
                for seat in range(len(lineup)):
                    modes = [a_comp if i == seat else b_comp for i in range(len(lineup))]
                    res = A.run_match(lineup, weights, seed=sd + k, max_rounds=args.rounds,
                                      hp_scale=args.hp_scale, free_actions=True, choice_modes=modes)
                    runs += 1
                    winner = res["survivors"][0] if len(res["survivors"]) == 1 else None
                    resolved += 1 if winner is not None else 0
                    for i in res["stats"]:
                        tag = a_comp if i == seat else b_comp
                        tally[tag]["seats"] += 1
                        if winner == i:
                            tally[tag]["wins"] += 1
        h2h[f"{a_comp} vs {b_comp}"] = {
            "runs": runs, "resolved": resolved,
            "win_rate_a": round(tally[a_comp]["wins"] / tally[a_comp]["seats"], 4),
            "win_rate_b": round(tally[b_comp]["wins"] / tally[b_comp]["seats"], 4),
            "seats_a": tally[a_comp]["seats"], "wins_a": tally[a_comp]["wins"],
            "seats_b": tally[b_comp]["seats"], "wins_b": tally[b_comp]["wins"]}
        print(f"  【同场对打】{a_comp} vs {b_comp}：{runs} 场（分出唯一幸存者 {resolved}）｜"
              f"每席胜率 {h2h[f'{a_comp} vs {b_comp}']['win_rate_a']:.3f}"
              f"（{tally[a_comp]['wins']}/{tally[a_comp]['seats']}） vs "
              f"{h2h[f'{a_comp} vs {b_comp}']['win_rate_b']:.3f}"
              f"（{tally[b_comp]['wins']}/{tally[b_comp]['seats']}）")
    out["head2head"] = h2h
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()