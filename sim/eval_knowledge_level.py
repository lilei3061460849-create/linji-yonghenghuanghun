#!/usr/bin/env python3
"""新知识库水平测试：同一 种子×副本 网格下对照 4 组 build。

A 新知识 best（gen60）：庇护 + [杀伐, 固执, 再生, 增殖, 透支]
B 旧知识 best 可玩变体（gen70 原 best 含已删除的【缓慢】，去掉后 4 槽）：
  杀伐 + [束缚, 连击, 再生, 庇护]
C 无学习基线：杀伐 + []（只修行）
D 随机对照组：固定随机种子抽一套（杀伐闭环起手 + 候选池5槽）

指标：通关率 / 平均通关场数(7) / 分副本胜率 / 无效局 / 实际发动道纹（含残韵转化获得的新道纹）
"""
import json
import random
import sys
from collections import Counter

sys.path.insert(0, ".")
import sim.build_learner as bl
from engine.daowen import DaoWenEngine

SEEDS = list(range(101, 141))          # 40 个种子
REGIONS = ["扭曲都市", "罪孽都市", "龙心谷"]

BUILD_A = ("庇护", ["杀伐", "固执", "再生", "增殖", "透支"])     # 新知识 best
BUILD_B = ("杀伐", ["束缚", "连击", "再生", "庇护"])            # 旧知识 best（去【缓慢】）
BUILD_C = ("杀伐", [])                                            # 无学习基线
_rng = random.Random(20260822)
STARTS = ["杀伐", "再生", "庇护", "固执", "连击", "波及", "增殖", "透支", "贯穿", "束缚", "封印"]
DaoWenEngine.register_all()
POOL = sorted(DaoWenEngine._registry.keys())
BUILD_D = (_rng.choice(STARTS), _rng.sample(POOL, 5))             # 随机对照

LABELS = {
    "A_新知识best": BUILD_A,
    "B_旧知识best(去缓慢)": BUILD_B,
    "C_无学习基线": BUILD_C,
    f"D_随机对照{BUILD_D[0]}+{'+'.join(BUILD_D[1])}": BUILD_D,
}

print(f"网格：{len(SEEDS)} 种子 × {len(REGIONS)} 副本 × {7}场 × {len(LABELS)} 组 = {len(SEEDS)*len(REGIONS)*len(LABELS)} 局\n")

# 追踪实际战斗内发动的道纹（含残韵转化后新获得的道纹是否被真正使用）
_usage = Counter()
_orig_take_action = bl.TacticalAI.take_action
def _tracked_take_action(self):
    before = dict(self.used)
    r = _orig_take_action(self)
    for k, v in self.used.items():
        if k.startswith(("buff:", "debuff:", "残韵", "救赎", "法器")):
            continue
        if v > before.get(k, 0):
            _usage[k] += v - before.get(k, 0)
    return r
bl.TacticalAI.take_action = _tracked_take_action

summary = {}
for label, (starter, learn) in LABELS.items():
    tele = {}
    wins = cleared_sum = invalid = timeouts = 0
    by_region = Counter()
    region_n = Counter()
    _usage.clear()
    for region in REGIONS:
        for seed in SEEDS:
            r = bl.play(starter, learn, region, seed=seed, battles=7,
                        telemetry=tele, spend_shards=False)
            if bl._is_resource_timeout(r):
                timeouts += 1
                continue
            if r.get("invalid"):
                invalid += 1
                continue
            region_n[region] += 1
            cleared_sum += r["cleared"]
            if r["won"]:
                wins += 1
                by_region[region] += 1
    n = len(SEEDS) * len(REGIONS) - invalid - timeouts
    dao_used = dict(sorted(_usage.items(), key=lambda kv: -kv[1]))
    pre_actions = {k.split("｜")[0]: v for k, v in tele.get("succeeded", {}).items()}
    summary[label] = (wins, n, cleared_sum, invalid, by_region, region_n, dao_used)
    print(f"== {label}  build={starter}+{learn}")
    print(f"   通关率 {wins}/{n} = {wins/max(1,n)*100:.1f}%   平均通关 {cleared_sum/max(1,n):.2f}/7场   无效局 {invalid} 资源超时 {timeouts}")
    for region in REGIONS:
        print(f"   {region}: {by_region[region]}/{region_n[region]} 胜")
    print(f"   战斗内发动道纹: {dao_used if dao_used else '（无）'}")
    print(f"   局外行动: {pre_actions}")
    print()

print("=" * 72)
print("汇总：")
print(f"{'组':<32}{'通关率':>10}{'平均通关':>10}{'无效':>6}")
for label, (wins, n, cleared_sum, invalid, *_r) in summary.items():
    print(f"{label:<32}{wins/max(1,n)*100:>8.1f}%{cleared_sum/max(1,n):>9.2f}{invalid:>6}")
