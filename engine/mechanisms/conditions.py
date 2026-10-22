"""Condition：最小条件组合子。

条件 = 触发器快照(TriggerContext) → bool，可组合（all_ / any_ / not_）。
MVP 只实现迁移【加害】与验收示例真正需要的组合子；不要一次设计几十个。

例（审计报告验收场景）：
    all_(events_this_round(DAMAGE_APPLIED), events_this_round(HEAL_APPLIED))
    = "本回合既受到过伤害、又获得过回复"——不需要在 combat.py 里写专用 if。
"""
from __future__ import annotations

from typing import Callable

from ..combat_events import CombatEventType
from .triggers import TriggerContext

Condition = Callable[[TriggerContext], bool]


def has_status(name: str, *, of: str = "target") -> Condition:
    """实体拥有指定状态。"""
    def cond(ctx: TriggerContext) -> bool:
        entity = ctx.resolve(of)
        return (entity is not None
                and hasattr(entity, "has_status")
                and bool(entity.has_status(name)))
    return cond


def side_has(name: str, *, of: str = "target") -> Condition:
    """实体所属轮回者持有指定遗物/法器/血脉（复用 GameState.side_has 语义）。"""
    def cond(ctx: TriggerContext) -> bool:
        entity = ctx.resolve(of)
        state = ctx.state or getattr(ctx.combat, "state", None)
        if entity is None or state is None:
            return False
        checker = getattr(state, "side_has", None)
        return bool(checker(entity, name)) if checker is not None else False
    return cond


def is_alive(*, of: str = "target") -> Condition:
    def cond(ctx: TriggerContext) -> bool:
        entity = ctx.resolve(of)
        return entity is not None and bool(getattr(entity, "is_alive", False))
    return cond


def entity_type(name: str, *, of: str = "target") -> Condition:
    def cond(ctx: TriggerContext) -> bool:
        entity = ctx.resolve(of)
        return entity is not None and getattr(entity, "entity_type", "") == name
    return cond


def hp_at_least(n: int, *, of: str = "target") -> Condition:
    def cond(ctx: TriggerContext) -> bool:
        entity = ctx.resolve(of)
        return entity is not None and getattr(entity, "current_hp", 0) >= n
    return cond


def events_this_round(event_type: CombatEventType, *, of: str = "target") -> Condition:
    """本回合内、该实体作为事件目标（承受方）的事件是否存在。

    语义口径："受到过伤害 / 获得过回复"指作为承受方，不含作为行为发起方。
    事实源是 state.combat_events（现有单一事实源），不另立机制私有账本。
    """
    def cond(ctx: TriggerContext) -> bool:
        entity = ctx.resolve(of)
        if entity is None:
            return False
        state = ctx.state or getattr(ctx.combat, "state", None)
        events = getattr(state, "combat_events", None)
        if not events:
            return False
        round_no = getattr(state, "current_round", None)
        name = getattr(entity, "name", "")
        for event in events:
            if event.event_type != event_type:
                continue
            if round_no is not None and event.round_no != round_no:
                continue
            if name and event.target_name == name:
                return True
        return False
    return cond


def relic_active(name: str, *, of: str = "target") -> Condition:
    """实体所属轮回者实际持有指定遗物，且该遗物未被封印（抵扣X）。

    语义严格委托 CombatEngine._relic_active（持有检查 + sealed_relics 检查），
    **不复制、不重新解释** sealed_relics 规则——单一事实源在引擎。
    仅在触发器上下文携带 combat 时可用；无 combat 的上下文返回 False
    （绝不在 Condition 层另写一套封印判断）。
    """
    def cond(ctx: TriggerContext) -> bool:
        entity = ctx.resolve(of)
        combat = ctx.combat
        if entity is None or combat is None:
            return False
        checker = getattr(combat, "_relic_active", None)
        if checker is None:
            return False
        return bool(checker(entity, name))
    return cond


def amount_positive() -> Condition:
    """数值修正相位的 amount > 0 前置（迁移【加害】所需，与旧 JiahaiHook 同义）。"""
    return lambda ctx: ctx.amount > 0


def damage_type_not(damage_type: str) -> Condition:
    """伤害类型排除（迁移【加害】所需：代价不受增幅，与旧 JiahaiHook 同义）。"""
    return lambda ctx: ctx.damage_type != damage_type


def all_(*conds: Condition) -> Condition:
    def cond(ctx: TriggerContext) -> bool:
        return all(c(ctx) for c in conds)
    return cond


def any_(*conds: Condition) -> Condition:
    def cond(ctx: TriggerContext) -> bool:
        return any(c(ctx) for c in conds)
    return cond


def not_(cond: Condition) -> Condition:
    return lambda ctx: not cond(ctx)


def entity_has_relic(name: str, *, of: str = "target") -> Condition:
    """只查实体自己的遗物列表，不混入阵营遗物或封存规则。"""
    def cond(ctx: TriggerContext) -> bool:
        entity = ctx.resolve(of)
        return entity is not None and any(r.name == name and getattr(r, "enabled", True) for r in getattr(entity, "relics", ()))
    return cond


def player_run_relic_active(name: str) -> Condition:
    """原 process_relics 的本局玩家遗物集合，逐字保留持有/封存边界。"""
    return lambda ctx: (ctx.target is ctx.state.player
                       and any(r.name == name and getattr(r, "enabled", True) for r in ctx.state.relics)
                       and ctx.state.sealed_relics.get(name, 0) <= 0)
