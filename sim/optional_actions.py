#!/usr/bin/env python3
"""可选遗物/法器策略的 sim 侧入口（实现已迁到 production）。

2026-10-02 架构收口：正式运行时（engine/ai_player.py 的 PlaceholderBackend）
也在调用这些策略，因此**实现**搬到 ``engine/ai_rules.py``——生产不得 import
模拟层。本模块只做再导出，保持既有 sim 脚本/测试的 import 路径不变
（方向：sim → production）。

本模块独有的 ``start_battle`` / ``start_round`` 组合入口仍在这里（模拟流程糖）。

原实现背景：此前所有模拟/手操/生产脚本把可选战始遗物（猩红果实/苍白之花，
【三相残韵盘】已于2026-10-13删除）一律 use:False 拒绝，回始遗物（血契/余火印，余火印已于2026-10-14删除）
也从不使用，终音法器（黑金名片/罪业金库/教父左轮/烬翼/鲜血之翼/共心环）没有
任何发动策略——这些机制"存在但不可用"。这些函数实现**显式决策**
（数据全部取自引擎实时状态）：
- battle_start_relic_choices：猩红果实 / 苍白之花（苍白之花已重做为被动，战始无需决策）
- round_start_relic_choices：回锋刀（余火印已于2026-10-14删除；血契的旧回始效果已删除，仅保留代价平分）
- 战始窗口法器：共心环（共享龙心）、黑金名片（敌方血限减半）
- 回始窗口法器：罪业金库（碎片→格挡）
- 战斗内法器：教父左轮（免费必中伤害）、鲜血之翼（流血→飞行）

决策原则：能用且划算才用；不划算就显式拒绝（引擎强制逐件显式提交）。
"""
from __future__ import annotations

import math
from typing import Optional

from engine.ai_rules import (  # noqa: F401  （再导出：sim 侧既有 import 路径）
    battle_start_relic_choices,
    round_start_relic_choices,
    try_select_shared_dragon_heart,
    try_use_black_card,
    try_use_crime_vault,
    try_fire_godfather_revolver,
)


# ---------------------------------------------------------------------------
# 组合入口：战始 / 回始（供各脚本统一替换手写 use:False）
# ---------------------------------------------------------------------------

def start_battle(engine):
    """战始 + 战始窗口法器。返回 (battle_start结果, 法器结果列表)。"""
    logs: list[dict] = []
    bs = engine.execute_action("battle_start",
                               {"relic_choices": battle_start_relic_choices(engine)})
    if not bs.get("success"):
        return bs, logs
    for fn in (try_select_shared_dragon_heart, try_use_black_card):
        r = fn(engine)
        if r and r.get("success"):
            logs.append(r)
    return bs, logs


def start_round(engine):
    """回始窗口法器 + round_start。返回 (round_start结果, 法器结果列表)。"""
    logs: list[dict] = []
    for fn in (try_use_crime_vault,):
        r = fn(engine)
        if r and r.get("success"):
            logs.append(r)
    rs = engine.execute_action("round_start",
                               {"relic_choices": round_start_relic_choices(engine)})
    return rs, logs
