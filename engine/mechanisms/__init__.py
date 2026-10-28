"""最小可行机制系统（MVP）。

五个基础抽象：Verb / Mechanism / Trigger / Condition / Target。
目标：普通新机制尽量只描述【什么时候 / 对谁 / 满足什么条件 / 做什么】，
而不是给核心管线加新的 if。当前统一数值规则第一阶段：【加害】【龙鳞】【固执】（受击伤害链）；
【自愈】【衰败】【洞察·结算】【勾魂】【狂暴·标记】【畸变·标记】（ROUND_START 相位，
priority 10/20/30/40/50/60——回始循环已全部声明化；洞察/勾魂经统一 mana 动词）、
【畸变·结算】（ROUND_END 相位，priority 10，锚定凡庸 tick 之前）、【焦黑发丝】
（ENTITY_DIED 事件，首个生产事件订阅者）、【洗劫·夺碎片】（DAMAGE_APPLIED 事件，
孤儿诊断字段 xijie_stolen 已正式废弃）、【帮派令】（BATTLE_START 相位 +
relic_active 条件——证明 Relic 可以成为普通 Mechanism 声明）、【缄默面具】
（BATTLE_START 相位 priority 5，激活后续法限增量翻倍；其【禁代价】静态校验规则
保留在 api.py，属另一字面规则）、【灾厄轮盘·结算（遗物被动）】
（ROUND_START_SETTLE 相位，relic_active 门控，使用 RNG 目标 roll_pick）。
原逼债、清算结算与对账机制已随道纹删除。

刻意边界（不要做成框架）：无 DSL、无 JSON 配置、无脚本系统、无 Action Queue、
无通用推理引擎、无冲突自动解决、无反射。机制声明就是 Python 数据结构。

顺序即规则：带来源持续状态的机制按状态记录的道纹序列位置结算；没有序列锚点时才使用 priority 后备。
不得用迁移前的固定循环顺序覆盖角色当前配置的道纹序列。
"""
from .conditions import (  # noqa: F401
    Condition, all_, any_, amount_positive, damage_type_not, entity_type,
    events_this_round, has_status, hp_at_least, is_alive, not_, relic_active,
    side_has,
)
from .registry import (  # noqa: F401
    MECHANISMS, Mechanism, MechanismHookAdapter, MechanismRegistry,
)
from .targets import (  # noqa: F401
    ALL, ALL_ALLIES, ALL_ENEMIES, DEAD_ENTITY, RANDOM_ENEMY, SELF, SOURCE,
    TARGET, TargetSelector, custom,
)
from .triggers import Phase, Trigger, TriggerBus, TriggerContext  # noqa: F401
from .verbs import apply_verb, get_verb, register_verb, verb_names  # noqa: F401

# 导入即注册 builtins.py 中全部声明式机制；数值规则事实源另见 engine.rule_engine。
from . import builtins  # noqa: E402,F401
