"""微光者（[朋友]/[员工]）发动道纹时自选 X 的决策器（实现已迁到 production）。

所有角色（含微光者）面板与怪物同格式，**不再写死 X**，
X 由发动时自选，上限只受[法限]或代价限制。

2026-10-02 架构收口：实现搬到 ``engine/ai_rules.py``——因为是 **engine/api.py**
在结算微光者道纹时调用它（生产代码不得 import 模拟层）。本模块只做再导出，
保持既有 sim 脚本/测试的 import 路径不变。

**为什么不直接套用怪物侧的 pick_monster_daowen_x**（两个独立阻断，仍然成立）：

1. 资源模型不同。怪物出厂自带遗物【某人的偏爱】，[回始]获得法限等量法力与异变（怪物免于迷失），因此怪物侧
   的问题是"*这回合*花多少最值"——下回合池子又满了，可以放心花。
   微光者按 AI_EXPERIENCE.md:1254「轮回者与微光者不持有，仍是一池制，[回始]
   不回填」，是**整场预算**：直接套用每回合最大化的逻辑，第一回合就会把整池
   砸光，之后整场再无道纹可用。

2. 视角错位。怪物侧靠 TacticalAI._split_diff 把引擎 diff 翻到行动者视角，而它
   只在 diff["player"] 与 diff["enemies"] 里按名字找自己，**从不看
   diff["friends"]**。朋友 actor 会静默退化成"挑战者视角"，把玩家当成自己打分。

因此不用预演评分，改用**整场预算分配**（见 engine/ai_rules.py）：对每种代价类型
按"不回填"的口径留出余量，再在可负担区间内取档位。判据是**它不会在第一回合
把池子花光**。
"""
from __future__ import annotations

from engine.ai_rules import (  # noqa: F401  （再导出：sim 侧既有 import 路径）
    COOLDOWN_COST_MAX_X,
    DEFAULT_DURATION_HORIZON,
    HP_SPEND_RATIO,
    MANA_POOL_SPEND_RATIO,
    MUTATION_SAFE_RATIO,
    X_PROBE_CAP,
    pick_ally_daowen_x,
)


# 原始常量说明（值定义见 engine/ai_rules.py）
# 实现与常量值都在 engine/ai_rules.py（生产模块）；本模块只再导出，
# 详见那里每个常量/函数的文档串。
