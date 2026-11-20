"""机制声明系统的公共接口。

基础抽象：Verb / Mechanism / Trigger / Condition / Target。
当前实际注册项以 `MECHANISMS.names()` 和 `engine/mechanisms/builtins.py` 为准；
不要把历史迁移记录、旧相位清单或本模块说明当成现行机制名录。
数值规则事实源另见 `engine.rule_engine`。

机制声明只描述【何时 / 对谁 / 满足什么条件 / 做什么】。
顺序与生命周期遵循各相位、事件总线和状态序列的正式结算规则。
"""
from .conditions import (  # noqa: F401
    Condition, all_, any_, amount_positive, damage_type_not, entity_type,
    events_this_round, has_status, hp_at_least, is_alive, not_, relic_active,
    entity_has_artifact,
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
