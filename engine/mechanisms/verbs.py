"""Verb：基础行为动词注册表。

"同一种基础行为只实现一次"。每个动词的实现体都是现有统一结算入口，
**不新增任何游戏逻辑**。机制层只通过 apply_verb 执行，禁止直接改
current_hp / current_mana / is_alive。

没有真正统一入口的行为不强行注册：
- mana：2026-08-19 起已注册（三个现存站点语义可被逐字覆盖：洞察/勾魂/缄默面具）。
- redirect / mitigate / reflect / split / spawn：特殊控制流，MVP 不迁移
  （审计报告 §3.3 的"命名动词"清单，后续阶段处理）。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

VerbFn = Callable[[Any, dict, Optional[dict]], Any]

_VERBS: dict[str, VerbFn] = {}


def register_verb(name: str, fn: VerbFn) -> None:
    if name in _VERBS:
        raise ValueError(f"动词[{name}]重复注册")
    _VERBS[name] = fn


def get_verb(name: str) -> VerbFn:
    try:
        return _VERBS[name]
    except KeyError:
        raise ValueError(f"未注册动词: {name}") from None


def apply_verb(combat, name: str, spec: dict, ctx=None) -> Any:
    """机制层唯一允许的执行入口：按动词名调用现有统一结算。"""
    return get_verb(name)(combat, spec, ctx)


def verb_names() -> list[str]:
    return sorted(_VERBS)


def _ctx_of(spec: dict, fallback_ctx) -> Optional[dict]:
    return spec.get("ctx", fallback_ctx)


# ---- 各动词实现体 = 现有统一入口的薄包装（零新逻辑） ----


def _verb_damage(combat, spec, ctx):
    return combat._apply_hostile_damage(
        spec["target"], spec["amount"],
        damage_type=spec.get("damage_type", "普通"),
        source=spec.get("source"),
        ctx=_ctx_of(spec, ctx),
    )


def _verb_heal(combat, spec, ctx):
    return combat.state.apply_heal(spec["target"], spec["amount"], ctx=_ctx_of(spec, ctx))


def _verb_hp_loss(combat, spec, ctx):
    return combat._raw_hp_loss(spec["target"], spec["amount"], ctx=_ctx_of(spec, ctx))


def _verb_blood_limit(combat, spec, ctx):
    return combat._apply_blood_limit_change(
        spec["target"], spec["delta"], spec.get("source", "mechanism"),
        spec.get("polarity", "debuff"),
        ctx=_ctx_of(spec, ctx),
        source_type=spec.get("source_type", ""),
        subtype=spec.get("subtype", ""),
        actor=spec.get("actor"),
        owner=spec.get("owner"),
        tags=spec.get("tags"),
        clamp_hp=spec.get("clamp_hp", True),
        lethal=spec.get("lethal", True),
    )


def _verb_cost(combat, spec, ctx):
    return combat.pay_numeric_cost(
        spec["payer"], spec["cost_type"], spec["amount"],
        cost_share_target_ref=spec.get("cost_share_target_ref", ""),
        dragon_heart_use=spec.get("dragon_heart_use", 0),
        cost_context=spec.get("cost_context", _ctx_of(spec, ctx)),
        forced=spec.get("forced", False),
    )


def _verb_status(combat, spec, ctx):
    from ..models import StatusEffect
    target = spec["target"]
    effect = StatusEffect(
        name=spec["name"],
        remaining_rounds=spec.get("duration", -1),
        value=spec.get("value", spec.get("amount", 0)),
        source=spec.get("source", "mechanism"),
    )
    target.add_status(effect)  # 统一状态授予入口
    return {"status": effect.name, "value": effect.value,
            "target": getattr(target, "name", "")}


def _verb_rounds(combat, spec, ctx):
    """修改目标某个有限状态的剩余回合数，供持续效果组合复用。"""
    target = spec["target"]
    status_name = spec["status_name"]
    delta = int(spec.get("delta", 0))
    status = next((s for s in target.status_effects if s.name == status_name), None)
    if status is None or status.remaining_rounds <= 0:
        return {"target": getattr(target, "name", ""), "status": status_name,
                "delta": 0, "remaining_rounds": None, "applied": False}
    status.remaining_rounds += delta
    return {"target": getattr(target, "name", ""), "status": status_name,
            "delta": delta, "remaining_rounds": status.remaining_rounds, "applied": True}


def _verb_speed(combat, spec, ctx):
    delta = spec.get("delta", spec.get("amount", 0))
    if delta > 0:
        return combat._gain_speed(spec["target"], delta, ctx=_ctx_of(spec, ctx))
    if delta < 0:
        return combat._lose_current_speed(spec["target"], -delta, ctx=_ctx_of(spec, ctx))
    return 0


def _verb_shield(combat, spec, ctx):
    target = spec["target"]
    target.gain_shield(spec.get("amount", spec.get("value", 0)))
    return {"target": getattr(target, "name", ""), "shield": target.shield}


def _verb_mutation(combat, spec, ctx):
    """施加异变；非怪物达阈值交给统一迷失结算，怪物仅累计层数。"""
    target = spec["target"]
    layers = spec.get("layers", spec.get("amount", 0))
    mut = target.add_mutation(layers)
    if mut.get("lost"):
        combat._resolve_mutation_lost(target, ctx)
    return mut


def _verb_mana(combat, spec, ctx):
    """当前法力的统一增减动词（2026-08-19 起成为统一入口）。

    语义 = 现有全部法力站点的字面约定：
      - 获得（delta>0）：current_mana += delta，随后 clamp_immortal_body
        （不朽之躯钳制——所有既有获取点均如此，如洞察/缄默面具/回始法力/共罪）；
      - 失去（delta<0）：实际失去 = min(当前法力, -delta)，下限 0
        （勾魂语义；失去不受不朽之躯钳制——该遗物只限制"获得"）；
      - delta==0：无操作。
    返回 {delta: 实际带符号变化, gained, lost, current_mana}。
    """
    target = spec["target"]
    delta = int(spec.get("delta", spec.get("amount", 0)))
    if delta > 0:
        # 2026-10-08：走统一入口，【勾魂】（无法获得[法力]）期间增益不生效
        target.gain_mana(delta)
        combat.clamp_immortal_body(target)
        return {"delta": delta, "gained": delta, "lost": 0,
                "current_mana": target.current_mana}
    if delta < 0:
        lost = min(target.current_mana, -delta)
        target.current_mana -= lost
        return {"delta": -lost, "gained": 0, "lost": lost,
                "current_mana": target.current_mana}
    return {"delta": 0, "gained": 0, "lost": 0, "current_mana": target.current_mana}


def _verb_shards(combat, spec, ctx):
    """碎片统一增减动词（2026-10-03 迁移【逼债】时新增；【逼债】虽已于
    2026-10-13 删除，动词作为通用碎片口径保留；见机制迁移台账）。

    语义 = 旧 F2 逼债块的字面约定（逐字复刻，不改数值）：
      - delta < 0（失去 X）：
          * 可支付量（`_shards_of`：假碎片 + max(0, 真碎片)）够 → 走统一扣除
            `_lose_shards_of`（玩家走 state.lose_shards，其余走 entity.lose_shards）；
          * 不够 → **假碎片优先**扣完，差额从真碎片里扣（允许扣成负数=负债）。
            旧口径的「负债不抵消仍可支付的假碎片」正是这一步的产物。
      - delta > 0（获得 X）：真碎片 +X（玩家走 state.shards，其余走 entity.shards）。
    返回 {delta, lost, gained, paid, shards_now, debt}；负债口径 debt = max(0, -shards_now)。
    """
    target = spec["target"]
    delta = int(spec.get("delta", spec.get("amount", 0)))
    is_player = target is combat.state.player
    if delta > 0:
        if is_player:
            combat.state.shards += delta
            now = combat.state.shards
        else:
            target.shards += delta
            now = target.shards
        return {"delta": delta, "lost": 0, "gained": delta, "paid": True,
                "shards_now": now, "debt": max(0, -now)}
    if delta == 0:
        return {"delta": 0, "lost": 0, "gained": 0, "paid": True,
                "shards_now": (combat.state.shards if is_player else target.shards), "debt": 0}

    need = -delta
    if combat._shards_of(target) >= need:
        lost = combat._lose_shards_of(target, need)
        now = combat.state.shards if is_player else target.shards
        return {"delta": -lost, "lost": lost, "gained": 0, "paid": True,
                "shards_now": now, "debt": max(0, -now)}
    # 无力支付：假碎片优先，差额记负债（真碎片扣负）
    if is_player:
        use_fake = min(combat.state.fake_shards, need)
        combat.state.fake_shards -= use_fake
        combat.state.shards -= (need - use_fake)
        now = combat.state.shards
    else:
        use_fake = min(target.fake_shards, need)
        target.fake_shards -= use_fake
        target.shards -= (need - use_fake)
        now = target.shards
    return {"delta": -need, "lost": use_fake + (need - use_fake), "gained": 0, "paid": False,
            "shards_now": now, "debt": max(0, -now)}


def _verb_depart(combat, spec, ctx):
    reason = spec.get("reason", "mechanism")
    spec["target"].depart_battle(reason)
    return {"target": getattr(spec["target"], "name", ""), "departure_reason": reason}


def _verb_execute(combat, spec, ctx):
    # 统一命零判定+死亡通知（非离场类"直接宣布死亡"）。
    return combat._check_hp_zero_death(spec["target"], ctx=_ctx_of(spec, ctx))


register_verb("damage", _verb_damage)
register_verb("heal", _verb_heal)
register_verb("hp_loss", _verb_hp_loss)
register_verb("blood_limit", _verb_blood_limit)
register_verb("cost", _verb_cost)
register_verb("status", _verb_status)
register_verb("rounds", _verb_rounds)
register_verb("speed", _verb_speed)
register_verb("shield", _verb_shield)
register_verb("mutation", _verb_mutation)
register_verb("mana", _verb_mana)
register_verb("shards", _verb_shards)
register_verb("depart", _verb_depart)
register_verb("execute", _verb_execute)


def _verb_direct_damage(combat, spec, ctx):
    """模型直通扣除；外层负责原有反应/来源/支付协议，不偷偷走伤害管线。"""
    target = spec["target"]
    return target.take_damage(spec["amount"], spec.get("damage_type", "普通"),
                              life_loss_multiplier=combat.state.life_loss_multiplier(target))


def _verb_mapping(combat, spec, ctx):
    mapping, key = spec["mapping"], spec["key"]
    operation = spec["op"]
    if operation == "pop":
        return mapping.pop(key, spec.get("default", 0))
    if operation == "set":
        mapping[key] = spec["value"]
    elif operation == "add":
        mapping[key] = mapping.get(key, 0) + spec["value"]
    else:
        raise ValueError(f"未知 mapping 操作：{operation}")
    return mapping[key]


def _verb_reset_fields(combat, spec, ctx):
    import copy
    target = spec["target"]
    for name, value in spec["fields"].items():
        if not name.startswith("_") or hasattr(target, name):
            setattr(target, name, copy.deepcopy(value))


register_verb("direct_damage", _verb_direct_damage)
register_verb("mapping", _verb_mapping)
register_verb("reset_fields", _verb_reset_fields)
