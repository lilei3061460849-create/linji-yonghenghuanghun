"""Trigger：机制触发点词汇表与事件分发。

设计约束（MVP）：
- 复用现有 CombatEventType / TriggerTiming / EffectContext，**不新建第二套事件枚举**。
- CombatEvent 的职责不变：记录/传播"发生了什么"。
  TriggerBus 只负责"哪些机制关心这个事件"，不是万能中心。
- Phase（相位触发点）对应战斗管线里已有的字面结算步骤，与 CombatHookManager
  的分发方法一一对应，不是新事件。MVP 只接线 INCOMING_ADJUST（【加害】迁移用），
  其余相位常量只是词汇表，未接线前禁止使用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..combat_events import CombatEvent, CombatEventType
from ..resolution import KIND_TRIGGER, resolution_frame, resolution_of


class Phase:
    """战斗管线内的字面结算相位（不是事件）。

    每个常量对应 CombatHookManager 的一个既有分发方法；MVP 只接线 INCOMING_ADJUST。
    新相位必须先确认管线里对应分发点唯一，才能登记并接线。
    """

    MULTIPLIER_ADJUST = "damage_multiplier_adjust"   # 伤害乘区（龙族血脉）——已接线（原 Hook 桥）
    INCOMING_ADJUST = "damage_incoming_adjust"       # 伤害加减区（加害→龙鳞）——已接线
    BEFORE_DAMAGE = "before_damage"                  # 受到伤害前（爆裂反噬）——未接线
    AFTER_DAMAGE = "after_damage"                    # 伤害落地后——未接线
    BATTLE_START = "battle_start"                    # 战始——已接线（process_relics 战始段）
    BATTLE_END = "battle_end"                        # 战终——已接线（process_relics 战终遗物段，
                                                     # 2026-09-17 为【缄默面具】改版新增）
    ROUND_START = "round_start"                      # 回始——已接线（combat.round_start 回始效果循环顶部）
    ROUND_START_SETTLE = "round_start_settle"        # 回始·全场结算（2026-10-03 接线）。
                                                     # 锚点：回始效果循环之后、current_round+=1 之前
                                                     # （原F2账本结算位置；原道纹【赌命】转为遗物【灾厄轮盘】后仍在此触发）。
                                                     # 分发语义=机制优先：
                                                     # 每个机制对**全部实体**结算完再轮到下一个，
                                                     # 同一窗口内按来源道纹序列位置排序（顺序即规则）。
    ROUND_END = "round_end"                          # 回终——已接线（round_end 第一逐实体循环顶部、
                                                     # 凡庸 tick 之前）。锚定语义：凡庸之后的回终机制
                                                     # 禁止注册本相位（会改变既有顺序），见机制迁移台账。
    ROUND_START_PREPARE = "round_start_prepare"  # 回始最前段：首回合事件格挡→皮衣
    ROUND_END_POST_MEDIOCRITY = "round_end_post_mediocrity"  # 凡庸后，逐实体血族→赤族
    ROUND_END_TRACKING = "round_end_tracking"  # status/龙躯 tick 后，皮衣→原追踪清理
    BATTLE_START_AFTER_CHOICES = "battle_start_after_choices"  # 烙痕钉后，原盟友逐实体循环
    ROUND_END_RECONCILE = "round_end_reconcile"      # 回终·清账（2026-10-03 接线）。
                                                     # 锚点：回终第一逐实体循环内、status tick 之后
                                                     # （原 F2「状态消失即清账」位置）。只清账、不结算，
                                                     # 时机必须晚于状态到期判定，否则会多结算一轮。


@dataclass(frozen=True)
class Trigger:
    """机制的【什么时候】：事件触发点或管线相位触发点。"""

    kind: str  # "event" | "phase"
    key: Any   # CombatEventType（kind=event）或 Phase 常量（kind=phase）

    @classmethod
    def event(cls, event_type: CombatEventType) -> "Trigger":
        return cls(kind="event", key=event_type)

    @classmethod
    def phase(cls, phase: str) -> "Trigger":
        return cls(kind="phase", key=phase)

    def matches_event(self, event_type: CombatEventType) -> bool:
        return self.kind == "event" and self.key == event_type

    def matches_phase(self, phase: str) -> bool:
        return self.kind == "phase" and self.key == phase


@dataclass
class TriggerContext:
    """一次触发的事实快照。机制的条件与效果只通过它读取战场，不直接翻引擎内部。"""

    combat: Any = None              # CombatEngine；相位路径（Hook 桥）可能为 None
    state: Any = None               # GameState；相位路径必有
    event: Optional[CombatEvent] = None
    phase: str = ""                 # Phase.*；事件路径为空
    target: Any = None              # 触发主体（受伤者 / 事件目标）
    source: Any = None              # 行为发起者（攻击者 / 事件行为者）
    amount: int = 0
    damage_type: str = ""
    # 选择器的「选择依据」等附加事实（如 RNG 的 roll/of）：由 TargetSelector 写入、
    # 机制效果读取，避免为了写战报把随机选择重投一次（见 targets.roll_pick）。
    picks: dict = field(default_factory=dict)

    def resolve(self, of: str) -> Any:
        """把条件/目标里的"对谁"词汇解析成实体。"""
        if of in ("target", "self"):
            return self.target
        if of in ("source", "actor"):
            return self.source
        if of == "player":
            # 通用"玩家"词汇：供"事件/相位 → 玩家获益"类机制复用（如焦黑发丝）。
            state = self.state or getattr(self.combat, "state", None)
            return getattr(state, "player", None) if state is not None else None
        return None


class TriggerBus:
    """事件 → 机制 的订阅分发。

    - 只分发"有订阅者的事件类型"；无订阅者时 dispatch 立即返回，行为零变化。
    - 分发是同步的：与现有内联机制代码的时间顺序语义一致。
    - MVP 生产代码没有任何事件订阅者（【加害】走相位路径）；接入点是
      CombatEngine._emit。经 state.emit_combat_event 直发的 HEAL_APPLIED 等
      尚未接入总线，属于后续迁移阶段（见审计报告 Phase 1）。
    """

    def __init__(self):
        self._listeners: dict[CombatEventType, list] = {}

    def register(self, mechanism) -> None:
        """订阅一个事件型机制。同 priority 保持注册先后（排序稳定）。"""
        if mechanism.when.kind != "event":
            raise ValueError(f"机制[{mechanism.name}]不是事件机制，不能注册到 TriggerBus")
        listeners = self._listeners.setdefault(mechanism.when.key, [])
        if mechanism not in listeners:
            listeners.append(mechanism)
            listeners.sort(key=lambda m: m.priority)

    def unregister(self, mechanism) -> None:
        listeners = self._listeners.get(mechanism.when.key)
        if listeners and mechanism in listeners:
            listeners.remove(mechanism)
            if not listeners:
                self._listeners.pop(mechanism.when.key, None)

    def listeners_for(self, event_type: CombatEventType) -> list:
        return list(self._listeners.get(event_type, ()))

    def dispatch(self, event: CombatEvent, combat, *, target=None, actor=None) -> list:
        """对一条已登记事件做机制分发；无订阅者时零开销返回。

        target/actor 由调用方（CombatEngine._emit）在持有实体对象时显式传入——
        事件只存名字，而死者（is_alive=False）无法从存活池按名解析回来。
        """
        # 寂灭面具订阅“命零”事件并抑制该时点的触发型效果；死亡事实本身仍保留在事件流，
        # 统一死亡管线、死者记录和非事件型结算不因此跳过。
        if (event.event_type == CombatEventType.ENTITY_DIED
                and (combat._relic_active(target, "寂灭面具")
                     or combat._any_active_relic("寂灭面具"))):
            return []
        listeners = self._listeners.get(event.event_type)
        if not listeners:
            return []   # 无订阅者：零开销快路径（保持在开帧之前）
        # 触发也是结算的一部分：它可能产生新效果（失去生命→再生→连击→…），
        # 因此同样计入深度与预算，并可在 trace 里看到「谁订阅、谁生效」。
        with resolution_frame(combat, KIND_TRIGGER,
                                  getattr(event.event_type, "name", event.event_type)):
            resolution = resolution_of(combat)
            results = []
            def ordering_key(mechanism):
                owner = actor if getattr(mechanism, "status_owner", "target") == "source" else target
                return mechanism.ordering_key(owner)

            # 事件类型是自然时机；仅在这同一事件窗口内读取状态排序键。
            for mechanism in sorted(list(listeners), key=ordering_key):
                ctx = self._context_for(event, combat, target=target, actor=actor)
                if mechanism.condition is not None and not mechanism.condition(ctx):
                    continue
                if resolution is not None:
                    resolution.note_trigger(mechanism.name)
                targets = mechanism.target.select(ctx) if mechanism.target is not None else []
                results.append((mechanism.name, mechanism.effect(ctx, targets)))
            return results

    @staticmethod
    def _resolve_ref(ref, combat):
        """把实体对象/名字字符串统一解析成实体：实体原样返回；名字按名查找。

        事件路径下 raw refs 可能因 ctx 序列化退化为名字字符串（如 heal 的
        ctx.actor 来自 dict 时），这里与既有"死者按名解析"逻辑同族——
        按名查找对已离场实体返回 None（安全降级）。
        """
        if ref is None or combat is None or not isinstance(ref, str):
            return ref
        finder = getattr(combat, "_find_named", None)
        if finder is None:
            return None
        return finder(ref)

    @staticmethod
    def _context_for(event: CombatEvent, combat, *, target=None, actor=None) -> TriggerContext:
        state = getattr(combat, "state", None)
        # 显式实体优先；缺失时退回按名解析（仅对仍在存活池中的实体可靠）。
        target = TriggerBus._resolve_ref(target, combat)
        if target is None and event.target_name:
            target = TriggerBus._resolve_ref(event.target_name, combat)
        actor = TriggerBus._resolve_ref(actor, combat)
        if actor is None and event.actor_name:
            actor = TriggerBus._resolve_ref(event.actor_name, combat)
        data = event.data or {}
        return TriggerContext(
            combat=combat,
            state=state,
            event=event,
            target=target,
            source=actor,
            amount=int(data.get("amount", 0) or 0),
            damage_type=str(data.get("damage_type", "") or ""),
        )
