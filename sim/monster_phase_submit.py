"""兼容薄壳：实现已搬到 `engine/monster_phase_submit.py`。

2026-11-03 为解掉 `webapp → sim` 的生产依赖，把这两个纯函数（只做行动参数拼装，
不碰引擎状态）搬进了 engine/。本文件保留同名导出，让 sim/ 里的历史探针
（`probe_custom_spell_triggers` 等 4 个）与外部一次性脚本继续可用。
新代码请直接 `from engine.monster_phase_submit import ...`。
"""
from __future__ import annotations

from engine.monster_phase_submit import (  # noqa: F401
    attack_actions,
    daowen_choice_from,
    daowen_submission,
    decline_reaction_spell_choices,
    monster_phase_choices,
    single_actor_choices,
)

__all__ = [
    "attack_actions",
    "daowen_choice_from",
    "daowen_submission",
    "decline_reaction_spell_choices",
    "monster_phase_choices",
    "single_actor_choices",
]
