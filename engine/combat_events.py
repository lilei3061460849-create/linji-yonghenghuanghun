"""
战斗事件总线数据模型（Event-Driven Combat Architecture）
用于解耦巨石战斗引擎与战报生成器，提供单一事实源不可变事件流。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Dict, List
import time


class CombatEventType(str, Enum):
    BATTLE_START = "battle_start"
    ROUND_START = "round_start"
    ACTION_DECLARED = "action_declared"
    DODGE_DECLARED = "dodge_declared"
    BEFORE_DAMAGE = "before_damage"
    DAMAGE_APPLIED = "damage_applied"
    HEAL_APPLIED = "heal_applied"
    # 血限变化是任务书要求的因果链中间跳（伤害 → 血限下降 → 依赖血限的效果），
    # 必须作为一等事件存在，否则这条链在事件流里是断的。
    BLOOD_LIMIT_CHANGED = "blood_limit_changed"
    STATUS_APPLIED = "status_applied"
    STATUS_EXPIRED = "status_expired"
    ENTITY_RETREATED = "entity_retreated"
    ENTITY_DIED = "entity_died"
    YAOGUAI_DORMANT = "yaoguai_dormant"
    ROUND_END = "round_end"
    BATTLE_END = "battle_end"
    # 消耗品自愈X（2026-10-14 用户令通用化）：目标死亡时触发位在事件流里留痕。
    CONSUMABLE_ZIYU = "consumable_zi_yu"


@dataclass
class CombatEvent:
    """“发生了什么”。

    职责边界（不要与 EffectContext 互相替代）：
      * CombatEvent  —— 客观事实：什么时点、谁对谁、发生了哪一类变化、数值多少。
      * EffectContext —— 因果来源：为什么发生、来自哪个效果、父事件是谁。
    因此 CombatEvent 只“携带” EffectContext 的快照（self.ctx），不复制它的语义。
    """
    event_type: CombatEventType
    battle_no: int = 0
    round_no: int = 0
    actor_name: str = ""
    target_name: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    # EffectContext.to_dict() 的浅层快照；None 表示该事件没有可追溯来源。
    ctx: Optional[Dict[str, Any]] = None
    timestamp: float = field(default_factory=time.time)

    @property
    def event_id(self) -> Optional[str]:
        return (self.ctx or {}).get("event_id")

    @property
    def parent_event_id(self) -> Optional[str]:
        return (self.ctx or {}).get("parent_event_id")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "battle_no": self.battle_no,
            "round_no": self.round_no,
            "actor_name": self.actor_name,
            "target_name": self.target_name,
            "data": self.data,
            "ctx": self.ctx,
            "timestamp": self.timestamp,
        }


# ========== 事件观察者（通用事件基础设施，2026-08-19） ==========
# CombatEngine 把"事件 → 机制分发"注册为观察者，使**所有**经
# GameState.emit_combat_event 发出的事件（含 models.apply_heal 直发的
# HEAL_APPLIED）进入同一条分发路径。观察者是可 pickle 的模块级类实例
# （只存引擎 id 整数），而非闭包/绑定方法：
# - 不进入 GameState.to_dict（动态属性，显式字段列表不序列化）；
# - 存档路径 pickle.dumps(state) 可正常工作（引擎不会被带进存档）；
# - deepcopy 快照复制为"同一 engine_id 的新实例"，仍解析回原引擎；
# - 引擎被回收后分发静默 no-op（弱引用失效）；全新引擎构造时重新绑定。
#   已知边界：引擎对象被回收且 id 被新引擎复用，极旧的残留观察者可能
#   误指向新引擎——需"旧 state 在旧引擎死后仍发事件"才会触发，实际不会发生。

import weakref as _weakref

_ENGINE_REFS: "dict[int, Any]" = {}  # engine_id → weakref.ref(engine)


def _bind_engine_ref(engine) -> int:
    engine_id = id(engine)
    _ENGINE_REFS[engine_id] = _weakref.ref(engine)
    # 惰性清理已死引用，防止长期运行积累
    dead = [eid for eid, ref in _ENGINE_REFS.items() if ref() is None]
    for eid in dead:
        _ENGINE_REFS.pop(eid, None)
    return engine_id


class _StateEventObserver:
    """可 pickle 的事件观察者：observer(event, *, raw_actor, raw_target)。"""

    def __init__(self, engine_id: int):
        self._engine_id = engine_id

    def __call__(self, event, *, raw_actor=None, raw_target=None):
        ref = _ENGINE_REFS.get(self._engine_id)
        engine = ref() if ref is not None else None
        if engine is not None:
            engine.mechanism_bus.dispatch(event, engine,
                                          target=raw_target, actor=raw_actor)


def register_combat_event_observer(state, engine) -> None:
    """为 GameState 注册事件观察者（持有引擎 id，经弱引用表解析引擎）。"""
    state._mechanism_event_observer = _StateEventObserver(_bind_engine_ref(engine))


def get_combat_event_observer(state):
    return getattr(state, "_mechanism_event_observer", None)


def engine_for_state(state):
    """按 state 找回绑定在它上面的 CombatEngine（无则 None）。

    复用既有观察者表（`_ENGINE_REFS`，弱引用），不新增全局注册表：
    状态层的统一入口（如 `GameState.apply_heal`）需要拿到结算上下文
    （`engine/combat.py::CombatEngine.resolution`）时用它，避免把引擎
    反向塞进 GameState（那会让存档把引擎带进去）。
    预演沙盒里 `state` 是深拷贝副本，其观察者是"同一 engine_id 的新实例"，
    因此这里解析回**真实**引擎——正是所需：沙盒内的结算记账落在真实 context 上，
    而 context 本身由 sandbox 快照/恢复（见 engine/sandbox.py）。
    """
    observer = get_combat_event_observer(state)
    if observer is None:
        return None
    ref = _ENGINE_REFS.get(getattr(observer, "_engine_id", -1))
    return ref() if ref is not None else None
