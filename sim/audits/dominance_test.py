#!/usr/bin/env python3
"""
支配性检验：判断游戏是否存在"固定最优解公式化打法"。

设计问题的判定标准
------------------
若存在一套 build，在**全部副本**上都以显著优势碾压其他 build，
且移除其核心后无人能打，则说明设计收敛到唯一解（有问题）。
若最优解随副本/局面变化，或多套差异很大的 build 胜率相当，
则说明需要随机应变（符合预期）。

四项检验
--------
A. 精英复检（winner's curse）
   学习期的高分可能只是运气。用**全新随机种子 + 大样本**重测，
   看排名是否稳定。分数大幅回落 = 原本就是噪声。

B. 跨副本一致性
   同一 build 在三个副本分别测。若处处第一 → 公式化；
   若各副本冠军不同 → 需要应变。

C. 核心封禁反事实（最关键）
   禁用出现频率最高的"疑似核心"道纹，看其余组合能否达到同等胜率。
   仍能打通 → 核心非必需，存在多解；
   全线崩溃 → 核心不可替代，即公式化。

D. 多样性检验
   统计"达到高胜率"的 build 有多少套、彼此差异多大。

用法：
    python3 sim/audits/dominance_test.py --games 120
    python3 sim/audits/dominance_test.py --games 200 --top 8
"""

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
import importlib.util
import itertools
import json
import math
import os
import random
import sys
from collections import Counter

ROOT = _ROOT
sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location(
    "bl", os.path.join(ROOT, "sim", "build_learner.py"))
bl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bl)

REGIONS = bl.REGIONS


def wilson(wins: int, n: int, z: float = 1.96) -> tuple:
    """Wilson 95% 置信区间。小样本下比正态近似可靠。"""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def evaluate(starter, learn, games, rng, region=None, banned=frozenset()):
    """在全新随机种子上评估一套 build。返回 (胜率, 胜, 有效局, 区间)。"""
    if banned & (set(learn) | {starter}):
        return None
    wins = valid = 0
    cleared = 0
    for _ in range(games):
        rg = region or rng.choice(REGIONS)
        seed = rng.randrange(1, 2 ** 31 - 1)
        r = bl.play(starter, learn, rg, seed, rng=rng)
        if bl._skip_resource_timeout(r, context=__name__):
            continue
        if r.get("invalid"):
            continue
        valid += 1
        cleared += r["cleared"]
        if r["won"]:
            wins += 1
    if not valid:
        return None
    return {"winrate": wins / valid, "wins": wins, "n": valid,
            "ci": wilson(wins, valid), "avg_cleared": cleared / valid}


def load_elites(top: int):
    with open(os.path.join(ROOT, "data", "build_knowledge.json"), encoding="utf-8") as f:
        k = json.load(f)
    seen = set()
    out = []
    for h in sorted(k["history"], key=lambda x: -x["score"]):
        key = (h["starter"], tuple(sorted(h["learn"])))
        if key in seen:
            continue
        seen.add(key)
        out.append((h["starter"], h["learn"], h["score"]))
        if len(out) >= top:
            break
    return out, k


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=120, help="每套build复检局数")
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--seed", type=int, default=20260811)
    a = ap.parse_args()
    rng = random.Random(a.seed)

    elites, k = load_elites(a.top)

    print("=" * 78)
    print("A. 精英复检（全新随机种子，检验学习期高分是否只是运气）")
    print("=" * 78)
    print(f"{'build':<44}{'学习期':>7}{'复检胜率':>10}{'95%CI':>16}")
    rechecked = []
    for st, lr, old in elites:
        r = evaluate(st, lr, a.games, rng)
        if not r:
            continue
        rechecked.append((r["winrate"], st, lr, old, r))
        name = f"【{st}】{'+'.join(lr)}"
        print(f"{name:<44}{old:>7.2f}{r['winrate']*100:>9.1f}%"
              f"   [{r['ci'][0]*100:.0f}-{r['ci'][1]*100:.0f}%]")
    rechecked.sort(reverse=True)

    print()
    print("=" * 78)
    print("B. 跨副本一致性（同一 build 在三副本分别复检）")
    print("=" * 78)
    best_per_region = {}
    header = f"{'build':<44}" + "".join(f"{rg:>11}" for rg in REGIONS)
    print(header)
    for _, st, lr, _old, _ in rechecked[:4]:
        row = f"【{st}】{'+'.join(lr)}"
        cells = []
        for rg in REGIONS:
            r = evaluate(st, lr, max(40, a.games // 2), rng, region=rg)
            wr = r["winrate"] if r else 0
            cells.append(f"{wr*100:>10.1f}%")
            cur = best_per_region.get(rg)
            if not cur or wr > cur[0]:
                best_per_region[rg] = (wr, f"{st}+{'+'.join(lr)}")
        print(f"{row:<44}" + "".join(cells))
    print("\n各副本最强：")
    champs = set()
    for rg, (wr, nm) in best_per_region.items():
        print(f"  {rg}: {nm}  ({wr*100:.1f}%)")
        champs.add(nm)

    print()
    print("=" * 78)
    print("C. 核心封禁反事实（最关键：禁用高频核心后还能不能打）")
    print("=" * 78)
    # 找出精英里出现频率最高的道纹
    freq = Counter()
    for _, st, lr, _o, _r in rechecked:
        freq.update(set(lr) | {st})
    core = [n for n, c in freq.most_common(3)]
    print(f"精英组合中出现频率最高的道纹（疑似核心）：{freq.most_common(5)}")
    print(f"→ 封禁 {core}，用剩余道纹随机组建 build 重测\n")

    banned = set(core)
    pool = [c for c in bl.CANDIDATES if c not in banned]
    alt_results = []
    for i in range(8):
        st = rng.choice([s for s in bl.STARTERS if s not in banned] or bl.STARTERS)
        lr = rng.sample([p for p in pool if p != st], bl.BUILD_SIZE)
        r = evaluate(st, lr, max(40, a.games // 2), rng)
        if r:
            alt_results.append((r["winrate"], st, lr, r))
    alt_results.sort(reverse=True)
    for wr, st, lr, r in alt_results[:5]:
        print(f"  {wr*100:>5.1f}%  [{r['ci'][0]*100:.0f}-{r['ci'][1]*100:.0f}%]"
              f"  【{st}】{'+'.join(lr)}")

    best_with = rechecked[0][0] if rechecked else 0
    best_without = alt_results[0][0] if alt_results else 0

    print()
    print("=" * 78)
    print("结论")
    print("=" * 78)
    print(f"  含核心的最强 build 胜率：{best_with*100:.1f}%")
    print(f"  封禁核心后最强 build 胜率：{best_without*100:.1f}%")
    gap = best_with - best_without
    print(f"  差距：{gap*100:+.1f} 个百分点")
    print()

    # 判定
    spread = [w for w, *_ in rechecked]
    top_tier = [w for w in spread if w >= best_with - 0.10]
    print(f"  与最强者胜率相差10个百分点以内的 build 数量：{len(top_tier)} / {len(spread)}")
    print(f"  各副本冠军是否同一套：{'是' if len(champs) == 1 else '否'}（{len(champs)}套）")
    print()
    if gap > 0.35 and len(champs) == 1:
        print("  ⚠️ 判定：存在公式化最优解。核心不可替代且通吃所有副本 → 设计需调整。")
    elif gap > 0.35:
        print("  ⚠️ 判定：核心道纹不可替代（封禁后崩盘），但各副本最优解不同。")
        print("     属于'必备核心 + 副本适配'，介于公式化与随机应变之间。")
    elif len(champs) == 1 and len(top_tier) <= 2:
        print("  ⚠️ 判定：单一 build 通吃所有副本，可选方案过少 → 偏公式化。")
    else:
        print("  ✅ 判定：不存在唯一最优解。封禁核心后仍有可行解，")
        print("     且各副本/各组合表现分散 → 需要随机应变，符合设计预期。")


if __name__ == "__main__":
    main()