#!/usr/bin/env python3
"""
原型验证：给怪物配备"反制型遗物"，能否打破公式化打法？

背景
----
dominance_test 已证明当前存在公式化最优解：
  庇护 + 再生 + 僵化 是不可绕过的地基（封禁后胜率 0%），
  且三个副本冠军是同一套。

用户提议：给每个怪物配有用的遗物/消耗品，取代早前那套突兀的先天能力机制，
以此避免玩家无脑公式化。本脚本用**可测量的方式**检验该提议。

做法
----
不改动引擎本体，用 monkeypatch 在出怪时给怪物挂载遗物效果，
遗物只做一件事：**针对性地反制某一条公式化支柱**。

  破盾类(贯穿) → 反制【庇护】：伤害无视格挡
  禁疗类(坏死) → 反制【再生】：目标无法获得回复
  净化类        → 反制【僵化】：怪物免疫攻击力锁定
  混合类        → 三者随机

检验指标
--------
1. 公式化 build 的胜率是否被显著拉低
2. 拉低之后，**其他 build 是否翻身**（这才是关键：
   若所有 build 一起降到 0%，只是变难，不是变得需要应变）
3. 各副本/各配置下的最优解是否开始分化

用法：
    python3 sim/monster_relic_probe.py --games 80
"""
import argparse
import importlib.util
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_s = importlib.util.spec_from_file_location("bl", os.path.join(ROOT, "sim", "build_learner.py"))
bl = importlib.util.module_from_spec(_s)
_s.loader.exec_module(bl)
_s2 = importlib.util.spec_from_file_location("dt", os.path.join(ROOT, "sim", "dominance_test.py"))
dt = importlib.util.module_from_spec(_s2)
_s2.loader.exec_module(dt)

from engine import monsters as M
from engine.models import StatusEffect

_orig_make = M.make_monster_entity

# 怪物遗物库：名称 → 挂载效果的函数。
# 全部复用引擎已实装的状态，不新增机制。
MONSTER_RELICS = {
    "裂盾之牙": ("贯穿", "持有者攻击无视格挡 → 反制【庇护】"),
    "腐血之種": ("坏死", "被其命中者无法获得回复 → 反制【再生】"),
    "定身符印": ("免疫僵化", "免疫攻击力锁定类效果 → 反制【僵化】"),
}


def attach(monster, kind: str):
    """给怪物挂上遗物效果（用引擎既有状态实现）。"""
    if kind == "贯穿":
        monster.add_status(StatusEffect(name="贯穿", value=1, remaining_rounds=-1,
                                        source="怪物遗物·裂盾之牙"))
    elif kind == "坏死":
        monster._relic_necrosis = True      # 命中后给玩家挂坏死
    elif kind == "免疫僵化":
        monster._relic_immune_lock = True
    return monster


def patched_factory(mode: str, rng: random.Random):
    def make(monster_def):
        m = _orig_make(monster_def)
        if mode == "none":
            return m
        if mode == "mixed":
            kind = rng.choice(["贯穿", "坏死", "免疫僵化"])
        else:
            kind = mode
        return attach(m, kind)
    return make


def patch_necrosis_on_hit():
    """让"腐血之種"在命中玩家后施加坏死（反制再生）。"""
    from engine import combat as C
    orig = C.CombatEngine.resolve_attack

    def wrapper(self, attacker, target, *a, **k):
        r = orig(self, attacker, target, *a, **k)
        if getattr(attacker, "_relic_necrosis", False) and r.get("hp_lost", 0) > 0:
            if not target.has_status("坏死"):
                target.add_status(StatusEffect(name="坏死", value=1, remaining_rounds=2,
                                               source="怪物遗物·腐血之種"))
        return r
    C.CombatEngine.resolve_attack = wrapper
    return orig


def patch_lock_immunity():
    """让"定身符印"免疫僵化类锁定（反制僵化）。"""
    from engine import combat as C
    orig = C.CombatEngine.apply_daowen_effect

    def wrapper(self, name, calc, caster, target, *a, **k):
        if name in ("僵化", "定型") and getattr(target, "_relic_immune_lock", False):
            return {"daowen": name, "effects": [], "immune": "定身符印：免疫锁定"}
        return orig(self, name, calc, caster, target, *a, **k)
    C.CombatEngine.apply_daowen_effect = wrapper
    return orig


BUILDS = {
    "公式化冠军(僵化+庇护+再生)": ("杀伐", ["僵化", "庇护", "加害", "缓慢", "再生"]),
    "无僵化(庇护+再生)":         ("杀伐", ["庇护", "再生", "加害", "裂变", "伤痕"]),
    "破格挡流(贯穿+杀伐)":       ("杀伐", ["贯穿", "庇护", "再生", "连击", "加害"]),
    "切割控制流":                ("杀伐", ["切割", "僵化", "庇护", "再生", "缓慢"]),
    "纯输出流":                  ("杀伐", ["连击", "冲击", "加害", "裂变", "伤痕"]),
}


def bench(mode, games, seed):
    rng = random.Random(seed)
    M.make_monster_entity = patched_factory(mode, rng)
    out = {}
    for name, (st, lr) in BUILDS.items():
        r = dt.evaluate(st, lr, games, random.Random(seed + 1))
        out[name] = r["winrate"] if r else 0.0
    M.make_monster_entity = _orig_make
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=80)
    ap.add_argument("--seed", type=int, default=20260811)
    a = ap.parse_args()

    patch_necrosis_on_hit()
    patch_lock_immunity()

    modes = [("none", "对照组：怪物无遗物"),
             ("贯穿", "全员【裂盾之牙】反制庇护"),
             ("坏死", "全员【腐血之種】反制再生"),
             ("免疫僵化", "全员【定身符印】反制僵化"),
             ("mixed", "混合：每怪随机一件")]

    print("=" * 92)
    print("怪物遗物对公式化打法的影响（各组同 build、同局数，仅怪物配置不同）")
    print("=" * 92)
    header = f"{'build':<30}" + "".join(f"{m[0][:8]:>13}" for m in modes)
    print(header)

    results = {}
    for mode, desc in modes:
        results[mode] = bench(mode, a.games, a.seed)

    for b in BUILDS:
        row = f"{b:<30}"
        for mode, _ in modes:
            row += f"{results[mode][b]*100:>12.1f}%"
        print(row)

    print("\n图例：")
    for mode, desc in modes:
        print(f"  {mode:<10}{desc}")

    print("\n" + "=" * 92)
    print("分析")
    print("=" * 92)
    base = results["none"]
    champ = "公式化冠军(僵化+庇护+再生)"
    for mode, desc in modes[1:]:
        r = results[mode]
        champ_drop = base[champ] - r[champ]
        # 该模式下的最优 build
        best_b = max(r, key=r.get)
        others = [v for k, v in r.items() if k != champ]
        print(f"\n【{desc}】")
        print(f"  冠军胜率 {base[champ]*100:.1f}% → {r[champ]*100:.1f}%  ({-champ_drop*100:+.1f})")
        print(f"  该配置下最优：{best_b} ({r[best_b]*100:.1f}%)")
        if best_b != champ:
            print("  ✅ 最优解发生转移 —— 玩家必须换套路")
        elif max(others) >= r[champ] - 0.05:
            print("  ◐ 冠军仍第一，但已有并列方案")
        else:
            print("  ✗ 冠军依旧独大")

    print("\n" + "=" * 92)
    champs = {max(results[m], key=results[m].get) for m, _ in modes}
    print(f"不同怪物配置下的最优 build 种类数：{len(champs)}")
    for c in champs:
        print(f"    - {c}")
    if len(champs) > 1:
        print("\n✅ 结论：怪物遗物确实使最优解随敌方配置变化 → 能达成'随机应变'的目的。")
    else:
        print("\n⚠️ 结论：无论怪物带什么遗物，最优解不变 → 该机制未能打破公式化，")
        print("   原因通常是：反制强度不足，或玩家核心道纹本身没有替代品。")


if __name__ == "__main__":
    main()
