"""
战斗声明式钩子系统（Combat Declarative Hook System）
彻底解耦 combat.py 中的硬编码条件分支，将遗物、道纹与法器重构为可独立插拔的生命周期 Listener。
"""
from __future__ import annotations
import math
from copy import copy
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
from .combat_events import CombatEvent, CombatEventType
from .effect_context import make_context, normalize_context
from .mechanisms.registry import MECHANISMS, MechanismHookAdapter
from .mechanisms.triggers import Phase


@runtime_checkable
class CombatHook(Protocol):
    """战斗生命周期钩子协议。

    priority：无发动 X 元数据的 Hook 的兼容后备顺序（数字小的先执行）。

    道纹建立的持续状态在同一个自然结算窗口不再由本字段决定：发动 X 大者
    先，X 相同按施加先后；状态中间把数值压成 0 也不会短路后续状态。静态
    priority 仅保留给非状态 Hook、旧档和审计输出，不能覆盖状态排序键。
    """

    priority: int = 100

    def on_multiplier_adjust(self, target: Any, amount: int, damage_type: str, source: Optional[Any], state: Any) -> int:
        return amount

    def on_incoming_adjust(self, target: Any, amount: int, damage_type: str, source: Optional[Any], state: Any) -> int:
        return amount

    def on_before_damage(self, target: Any, amount: int, damage_type: str, attacker: Optional[Any], state: Any) -> Dict[str, Any]:
        return {}

    def on_after_damage(self, target: Any, actual_damage: int, shield_absorbed: int, detail: Dict[str, Any], attacker: Optional[Any], state: Any) -> Dict[str, Any]:
        return {}

    def on_dodge(self, entity: Any, state: Any) -> Dict[str, Any]:
        return {}

    def on_round_start(self, entity: Any, is_enemy_turn: bool, state: Any) -> Dict[str, Any]:
        return {}


class QianjingjiaHook:
    """千荆甲（通用遗物池，2026-10-03 出自被删【爆裂】道纹）的数值实现。

    与旧【爆裂】Hook 同口径：直接改 attacker.current_hp，不走 take_damage，
    因此无视格挡/护盾；【第一杯】失血倍率经 state.life_loss_multiplier 生效。
    触发条件（"只有攻击行动"与"持有者是否激活"）由 CombatEngine 侧判定，
    本 Hook 只负责把"等量伤害"换成"实际扣掉的生命"。
    反噬本身是直接失血，不会再触发千荆甲（防 A→B→A 反弹）。
    """
    priority = 40

    def reflect_attack_damage(self, target: Any, amount: int, attacker: Any,
                              state: Any) -> int:
        """返回本次应反噬的数值（含【第一杯】倍率），**不动生命**。

        扣血与记账唯一入口是 CombatEngine._resolve_reflect_aftermath——
        两处都改 HP 会双倍扣（2026-10-03 实测踩过：45HP 挨 10 点反噬掉 20）。
        """
        if attacker is None or attacker is target or amount <= 0:
            return 0
        return amount * state.life_loss_multiplier(attacker)


class BifenglingHook:
    """避风铃闪避句：每次闪避后获得3点格挡。归零+15走失速总线。

    ⚠️ 双实现登记（P1）：引擎真实分发路径是
    `CombatEngine._note_dodge()`（闪避 +3）与 `CombatEngine._trigger_bifengling_zero()`（归零 +15）。
    `CombatHookManager.apply_dodge()` **没有任何引擎调用点**，只有单元测试直接调用它。
    在把闪避真正迁到 Hook 总线之前，禁止在 combat.py 里接线 apply_dodge()，否则会 +3 两次。
    """
    priority = 50

    def on_dodge(self, entity: Any, state: Any) -> Dict[str, Any]:
        checker = getattr(state, "_relic_active", None) or getattr(state, "side_has", None)
        if not entity or checker is None or not checker(entity, "避风铃"):
            return {}
        entity.shield += 3
        return {"shield_gained": 3, "total_shield": entity.shield}


class DamageRedirectionHook:
    """背负：伤害重定向逻辑（【嫁祸】2026-10-11 已删除，效果与遗物【贪婪契约】重复）"""
    priority = 70
    # 由 CombatEngine 通过 apply_redirection() 显式分发，不参与通用遍历。
    explicit_dispatch = True

    def find_redirection_target(self, target: Any, damage_type: str, state: Any) -> Optional[Any]:
        if damage_type == "代价" or not target:
            return None
        all_entities = (state.get_all_player_side() + state.get_all_enemy_side()) if hasattr(state, "get_all_player_side") else []
        # 背负：目标的伤害由背负者承担（_beifu_target 同样存 runtime_id）
        for ent in all_entities:
            if ent is target:
                continue
            if hasattr(ent, "_beifu_left") and getattr(ent, "_beifu_left", 0) > 0:
                if getattr(ent, "_beifu_target", None) == getattr(target, "runtime_id", None):
                    ent._beifu_left -= 1
                    if ent._beifu_left <= 0:
                        target.status_effects = [s for s in target.status_effects if s.name != "被背负"]
                        if hasattr(ent, "_beifu_target"):
                            delattr(ent, "_beifu_target")
                    return ent
        return None


class LethalMitigationHook:
    """撤退、负岳碑与断尾求生：濒死保护与伤害吸收"""
    priority = 80
    # 由 CombatEngine 通过 apply_mitigation() 显式分发，不参与通用遍历。
    explicit_dispatch = True

    def check_mitigation(self, target: Any, amount: int, damage_type: str, combat: Any) -> Optional[Dict[str, Any]]:
        if damage_type == "代价" or not target or not getattr(target, "is_alive", False):
            return None

        # 1. 朋友/员工撤退与负岳碑
        if getattr(target, "entity_type", "") in ("朋友", "员工") and not getattr(target, "has_retreated", False):
            remaining_after_shield = (amount if damage_type == "无视格挡"
                                      else max(0, amount - getattr(target, "shield", 0))) if amount > 0 else 0
            if remaining_after_shield >= target.current_hp and target.current_hp > 0:
                player = combat.state.player
                target_ref = next((ref for ref, entity in combat._combat_entity_refs().items() if entity is target), "")
                if (target_ref in combat.state.fuyuebei_declared and "负岳碑" in combat.state.artifacts_owned
                        and player is not None and player.current_hp > 20):
                    combat.state.fuyuebei_declared.remove(target_ref)
                    share_map = combat.state.event_modifiers.get("fuyuebei_cost_share_refs", {})
                    payment = combat.pay_numeric_cost(
                        player, "流血", 20,
                        cost_share_target_ref=share_map.pop(target_ref, ""),
                        cost_context={"timing": "reaction", "source": "负岳碑", "source_type": "artifact", "tags": {"active_payment"}})
                    return {
                        "raw_damage": amount, "shield_absorbed": 0, "actual_damage": 0,
                        "hp_before": target.current_hp, "hp_after": target.current_hp,
                        "blood_limit_before": target.blood_limit, "died": False,
                        "damage_type": damage_type, "retreated": False,
                        "fuyuebei_toll_paid": 20, "fuyuebei_cost": payment,
                    }
                target.has_retreated = True
                return {
                    "raw_damage": amount, "shield_absorbed": 0, "actual_damage": 0,
                    "hp_before": target.current_hp, "hp_after": target.current_hp,
                    "blood_limit_before": target.blood_limit, "died": False,
                    "damage_type": damage_type, "retreated": True,
                }

        # 2. 断尾求生
        if (getattr(target, "is_alive", False) and getattr(combat.state, "_relic_active", combat.state.side_has)(target, "断尾求生")
                and combat.state.side_tail_declared(target)):
            remaining_after_shield = (amount if damage_type == "无视格挡"
                                      else max(0, amount - getattr(target, "shield", 0))) if amount > 0 else 0
            if remaining_after_shield >= target.current_hp and target.current_hp > 0:
                sacrificed = combat.state.side_tail_declared(target)
                combat.state.remove_side_relic(target, sacrificed)
                combat.state.clear_side_tail_declared(target)
                return {
                    "raw_damage": amount, "shield_absorbed": 0, "actual_damage": 0,
                    "hp_before": target.current_hp, "hp_after": target.current_hp,
                    "blood_limit_before": target.blood_limit, "died": False,
                    "damage_type": damage_type, "tail_sacrificed": sacrificed,
                }
        return None


class AfterDamageEffectsHook:
    """逆鳞、伤痕、寄生、负岳索与龙族血脉斩杀：伤害落地后综合处理"""
    priority = 90
    # 由 CombatEngine 通过 apply_after_damage_pipeline() 显式分发。
    # 通用的 apply_after_damage() 会跳过本 Hook —— 否则两条分发路径会让
    # 伤痕/寄生/负岳索/龙族血脉斩杀各触发两次。
    explicit_dispatch = True

    def on_after_damage(self, target: Any, actual_damage: int, shield_absorbed: int, detail: Dict[str, Any], attacker: Optional[Any], combat: Any) -> Dict[str, Any]:
        if actual_damage <= 0 or not target:
            return {}

        res = {}
        damage_ctx = normalize_context(detail.get("ctx"))
        damage_event_id = damage_ctx.event_id if damage_ctx else None

        # 噬伤血囊订阅全场伤害落地信号：每个有效持有者回复该次实际伤害的20%。
        # 沿用原【寄生】的向上取整口径；治疗仍走 GameState.apply_heal，遵守坏死等统一规则。
        if actual_damage > 0 and combat._any_active_relic("噬伤血囊"):
            holders = []
            for container_name in ("player", "friends", "employees", "temp_friends", "enemies"):
                value = getattr(combat.state, container_name, None)
                if isinstance(value, list):
                    holders.extend(value)
                elif value is not None:
                    holders.append(value)
            seen_holders = set()
            for holder in holders:
                if id(holder) in seen_holders:
                    continue
                seen_holders.add(id(holder))
                if (holder.is_alive and combat._relic_active(holder, "噬伤血囊")
                        and not holder.has_status("坏死")):
                    heal_n = math.ceil(actual_damage * 0.2)
                    if heal_n > 0:
                        healed = combat.state.apply_heal(holder, heal_n, ctx={
                            "timing": damage_ctx.timing if damage_ctx else "",
                            "source": "噬伤血囊", "source_type": "relic",
                            "actor": holder, "target": holder, "owner": holder,
                            "mechanic": "heal", "subtype": "relic_damage_conversion",
                            "amount": heal_n, "tags": {"relic", "after_damage"},
                            "parent_event_id": damage_event_id,
                        })
                        res.setdefault("噬伤血囊", []).append({
                            "holder": holder.name, "requested": heal_n, **healed})

        # 【血翼·噬血】（消耗品模式，2026-10-23）：本回合你造成的伤害给予你等量回复。
        # 订阅同一条伤害落地信号；只认攻击者自己身上带【噬血】状态的情况。
        if (actual_damage > 0 and attacker is not None
                and getattr(attacker, "is_alive", False)
                and attacker.has_status("噬血")
                and not attacker.has_status("坏死")):
            healed = combat.state.apply_heal(attacker, actual_damage, ctx={
                "timing": damage_ctx.timing if damage_ctx else "",
                "source": "血翼", "source_type": "consumable",
                "actor": attacker, "target": attacker, "owner": attacker,
                "mechanic": "heal", "subtype": "wing_lifesteal",
                "amount": actual_damage, "tags": {"consumable", "after_damage", "lifesteal"},
                "parent_event_id": damage_event_id,
            })
            res.setdefault("血翼·噬血", []).append({
                "holder": attacker.name, "requested": actual_damage, **healed})

        # 遗物【共生血囊】：由持有该遗物的创建者所创造的分裂复制体造成伤害后，
        # 复制体先回复等同实际失血的生命；超过复制体生命上限的部分再回复创建者。
        # 以实际生命减少量为基数，避免把超过目标剩余生命的过量伤害计入。
        creator_id = getattr(attacker, "copy_creator_runtime_id", "") if attacker is not None else ""
        if creator_id and actual_damage > 0:
            candidates = []
            for container_name in ("player", "friends", "employees", "temp_friends", "enemies"):
                value = getattr(combat.state, container_name, None)
                if isinstance(value, list):
                    candidates.extend(value)
                elif value is not None:
                    candidates.append(value)
            creator = next((entity for entity in candidates
                            if getattr(entity, "runtime_id", "") == creator_id), None)
            # `actual_damage` 是扣除格挡后的伤害，不保证目标有这么多当前生命；
            # 用伤害事件的实际前后生命差兜底，避免对低血目标把超杀部分算成吸血。
            if "actual_life_loss" in detail:
                actual_life_loss = max(0, int(detail["actual_life_loss"]))
            else:
                hp_before = int(detail.get("hp_before", target.current_hp))
                hp_after = int(detail.get("hp_after", target.current_hp))
                actual_life_loss = max(0, hp_before - hp_after)
            if (creator is not None and creator.is_alive and creator.current_hp > 0
                    and actual_life_loss > 0
                    and combat._relic_active(creator, "共生血囊")
                    and not attacker.has_status("坏死")):
                clone_heal = combat.state.apply_heal(attacker, actual_life_loss, ctx={
                    "timing": damage_ctx.timing if damage_ctx else "",
                    "source": "共生血囊", "source_type": "relic", "actor": attacker,
                    "target": attacker, "owner": creator, "mechanic": "heal",
                    "subtype": "split_clone_lifesteal", "amount": actual_life_loss,
                    "tags": {"relic", "after_damage", "split_clone"},
                    "parent_event_id": damage_event_id,
                })
                overflow = max(0, int(clone_heal.get("overheal", 0)))
                creator_heal = None
                if overflow > 0 and creator.is_alive and not creator.has_status("坏死"):
                    creator_heal = combat.state.apply_heal(creator, overflow, ctx={
                        "timing": damage_ctx.timing if damage_ctx else "",
                        "source": "共生血囊", "source_type": "relic", "actor": attacker,
                        "target": creator, "owner": creator, "mechanic": "heal",
                        "subtype": "split_clone_overflow", "amount": overflow,
                        "tags": {"relic", "after_damage", "split_clone", "overflow"},
                        "parent_event_id": damage_event_id,
                    })
                detail["split_clone_lifesteal"] = {
                    "creator": creator.name,
                    "damage_basis": actual_life_loss,
                    "clone_heal": clone_heal,
                    "creator_overflow_heal": creator_heal,
                }

        # 致死时挂在死亡上下文下的父事件。默认是本次伤害；
        # 若死因其实是血限被压（伤痕），则改挂那次血限变化，形成
        # 伤害 → 血限下降 → 命零 的三层链。
        lethal_ctx = damage_ctx
        # 逆鳞层数
        if hasattr(target, "has_status") and target.has_status("逆鳞"):
            target._nilin = getattr(target, "_nilin", 0) + actual_damage
            detail["nilin_stack_added"] = actual_damage
            detail["nilin_total"] = target._nilin

        # 伤痕扣血限
        if hasattr(target, "has_status") and target.has_status("伤痕"):
            xv = target.get_status_value("伤痕") or 0
            delta = max(1, target.blood_limit - xv) - target.blood_limit
            # lethal=False：命零判定仍统一留到本方法末尾，保持原有触发次序。
            shanghen = combat._apply_blood_limit_change(
                target, delta, "伤痕", "debuff", ctx=damage_ctx,
                source_type="daowen", subtype="scar", actor=attacker,
                tags={"daowen", "after_damage", "blood_limit_loss"},
                lethal=False)
            detail["shanghen_ctx"] = shanghen["ctx"]
            if target.current_hp <= 0:
                # 保持原次序：此处先置 is_alive，
                # 真正的死亡通知统一留到本方法末尾的 _check_hp_zero_death。
                target.is_alive = False
                detail["died"] = True
                detail["hp_after"] = 0
                lethal_ctx = normalize_context(shanghen["ctx"]) or lethal_ctx
            detail["shanghen_blood_loss"] = xv

        # 寄生吸血
        if hasattr(target, "has_status") and target.has_status("寄生"):
            xv = target.get_status_value("寄生") or 0
            drain = math.ceil(actual_damage * 20 * xv / 100)
            src_name = next((s.source for s in getattr(target, "status_effects", []) if s.name == "寄生" and not getattr(s, "is_expired", False)), "")
            healer = combat._find_named(src_name)
            if healer is not None and getattr(healer, "is_alive", False) and drain > 0 and not healer.has_status("坏死"):
                h = combat.state.apply_heal(healer, drain, ctx={
                    "timing": damage_ctx.timing if damage_ctx else "",
                    "source": "寄生", "source_type": "daowen", "actor": healer, "target": healer,
                    "owner": healer, "mechanic": "heal", "subtype": "parasite", "amount": drain,
                    "tags": {"daowen", "after_damage"}, "parent_event_id": damage_event_id,
                })
                detail["jisheng_heal"] = {"healer": healer.name, **h}
                cancer = combat.check_cancer(healer)
                if cancer:
                    detail["jisheng_cancer"] = cancer

        # 负岳索：[战始]选择一名朋友/员工；其首次受到伤害时，你[回复]等量生命。
        # 状态挂在被保护者身上，但回复对象是遗物持有者（玩家），不是受伤目标本人。
        if hasattr(target, "has_status") and target.has_status("负岳索"):
            target.status_effects = [s for s in target.status_effects if s.name != "负岳索"]
            healer = getattr(combat.state, "player", None)
            if healer is not None and getattr(healer, "is_alive", False):
                healed = combat.state.apply_heal(healer, actual_damage, ctx={
                    "timing": damage_ctx.timing if damage_ctx else "",
                    "source": "负岳索", "source_type": "relic", "actor": healer, "target": healer,
                    "owner": healer, "mechanic": "heal", "subtype": "fuyuesuo", "amount": actual_damage,
                    "tags": {"relic", "after_damage"}, "parent_event_id": damage_event_id,
                })
                detail["fuyuesuo_heal"] = {"healer": healer.name, **healed}

        # 龙族血脉攻击怪物直接命零
        _bloodline_checker = getattr(combat.state, "_relic_active", None) or getattr(combat.state, "side_has", None)
        if (attacker is not None and _bloodline_checker is not None and _bloodline_checker(attacker, "龙族血脉")
                and getattr(target, "entity_type", "") == "怪物" and getattr(target, "is_alive", False)):
            target.current_hp = 0
            target.is_alive = False
            detail.update({"died": True, "hp_after": 0, "dragon_bloodline_kill": True})
            lethal_ctx = make_context(
                timing=damage_ctx.timing if damage_ctx else "",
                source="龙族血脉", source_type="relic", actor=attacker, target=target,
                owner=attacker, mechanic="execute", subtype="dragon_bloodline_kill",
                amount=0, tags={"relic", "after_damage", "execute"},
                parent_event_id=damage_event_id,
            )
            detail["dragon_bloodline_kill_ctx"] = lethal_ctx.to_dict()

        if detail.get("died"):
            combat._check_hp_zero_death(target, ctx=lethal_ctx)

        return res


class CombatHookManager:
    """钩子管理器：集中注册与生命周期分发。

    两类分发：
      * 通用遍历（apply_multiplier_adjust / apply_incoming_adjust / apply_before_damage /
        apply_after_damage / apply_dodge）——按 priority 升序执行。
      * 显式分发（apply_redirection / apply_mitigation / apply_after_damage_pipeline）——
        由 CombatEngine 在伤害管线的固定位置调用。被显式分发的 Hook 标记
        `explicit_dispatch = True`，通用遍历会跳过它们，杜绝“同一效果触发两次”。
    """

    def __init__(self):
        # 顺序即规则：这里的相对次序是重构前字面注册顺序的如实固化，
        # 现在由 priority 显式表达（见 CombatHook.priority）。
        self.redirection_hook = DamageRedirectionHook()
        self.mitigation_hook = LethalMitigationHook()
        self.after_damage_hook = AfterDamageEffectsHook()
        # 已迁移到声明层的伤害持续状态经适配器挂到同一条 Hook 路径。其实际
        # 执行顺序不是静态 priority：每次伤害窗口都读取状态的发动 X 与施加序号。
        mechanism_hooks: List[Any] = [
            MechanismHookAdapter(mechanism)
            for phase in (Phase.MULTIPLIER_ADJUST, Phase.INCOMING_ADJUST)
            for mechanism in MECHANISMS.phase_mechanisms(phase)
        ]
        # 注意：必须复用上面这三个**同一实例**，不能再 new 一份，
        # 否则注册表与显式分发路径持有的是两个对象，状态与去重都会失真。
        self._hooks: List[Any] = self._sorted([
            *mechanism_hooks,
            QianjingjiaHook(),
            BifenglingHook(),
            self.redirection_hook,
            self.mitigation_hook,
            self.after_damage_hook,
        ])

    @staticmethod
    def _priority_of(hook: Any) -> int:
        return getattr(hook, "priority", 100)

    @classmethod
    def _sorted(cls, hooks: List[Any]) -> List[Any]:
        # 稳定排序：同 priority 保持注册先后，行为与重构前一致。
        return sorted(hooks, key=cls._priority_of)

    @classmethod
    def _is_explicit(cls, hook: Any) -> bool:
        return bool(getattr(hook, "explicit_dispatch", False))

    def hooks(self) -> List[Any]:
        """按实际执行顺序返回全部已注册 Hook（供审计/测试断言顺序）。"""
        return list(self._hooks)

    def register_hook(self, hook: Any) -> None:
        if hook not in self._hooks:
            self._hooks.append(hook)
            self._hooks = self._sorted(self._hooks)

    def apply_redirection(self, target: Any, damage_type: str, state: Any) -> Optional[Any]:
        return self.redirection_hook.find_redirection_target(target, damage_type, state)

    def apply_mitigation(self, target: Any, amount: int, damage_type: str, combat: Any) -> Optional[Dict[str, Any]]:
        return self.mitigation_hook.check_mitigation(target, amount, damage_type, combat)

    def apply_after_damage_pipeline(self, target: Any, actual_damage: int, shield_absorbed: int, detail: Dict[str, Any], attacker: Optional[Any], combat: Any) -> Dict[str, Any]:
        return self.after_damage_hook.on_after_damage(target, actual_damage, shield_absorbed, detail, attacker, combat)

    def apply_multiplier_adjust(self, target: Any, amount: int, damage_type: str, source: Optional[Any], state: Any) -> int:
        for hook in self._hooks:
            if self._is_explicit(hook):
                continue
            if hasattr(hook, "on_multiplier_adjust"):
                amount = hook.on_multiplier_adjust(target, amount, damage_type, source, state)
        return amount

    def apply_incoming_adjust(self, target: Any, amount: int, damage_type: str,
                              source: Optional[Any], state: Any, *,
                              resolution_context: Any = None) -> int:
        """结算本次伤害窗口的持续状态/数值规则。

        初始输入≤0仍按旧契约不启动状态链；一旦正伤害进入链，内部中间值
        即使变为0或负数，后续规则仍读取同一上下文的最新当前值，不短路。
        resolution_context 由调用方区分 preview 与 commit；旧五参数调用兼容。
        """
        from uuid import uuid4
        from .rule_engine import (
            DamageResolutionContext, RuleError, _trace_number, validate_damage_amount,
        )

        amount = validate_damage_amount(amount, "incoming_adjust.amount")
        candidates = [
            hook for hook in self._hooks
            if not self._is_explicit(hook) and hasattr(hook, "on_incoming_adjust")
        ]
        if amount < 0:
            # 保持旧 Hook API：负的初始输入不启动规则链，并原样返回。
            # CombatEngine 的真实扣血入口另将负原始伤害作为 no-op 处理，不会反向治疗。
            return amount
        if amount == 0:
            if resolution_context is not None:
                resolution_context.set_current_damage(0)
                resolution_context.post_rule_damage = 0
            return 0
        if resolution_context is None:
            # 兼容旧五参数调用仍使用唯一结算上下文和事件 ID；否则多个规则
            # 会各自按局部 amount 计算，破坏同一受击链的串行当前值。
            resolution_context = DamageResolutionContext(
                event_id=f"incoming-adjust:{uuid4().hex}",
                attacker=source,
                recipient=target,
                damage_type=damage_type,
                original_damage=amount,
                incoming_damage=amount,
                current_damage=amount,
                mode="commit",
            )
        if not isinstance(resolution_context, DamageResolutionContext):
            raise RuleError("incoming_adjust.resolution_context 类型无效")
        if resolution_context.recipient is not target or resolution_context.damage_type != damage_type:
            raise RuleError("incoming_adjust 上下文的受击者/伤害类型与调用参数不一致")

        def incoming_key(hook: Any) -> tuple:
            if isinstance(hook, MechanismHookAdapter):
                return hook.incoming_order_key(target)
            # 非持续状态 Hook 没有发动 X，放在状态窗口后按原 priority 运行。
            return (2, self._priority_of(hook), type(hook).__name__)

        # 整个 Hook 窗口先在副本上执行；若后续某条规则/校验失败，已运行规则的
        # 当前值、执行标记、DEFER 与 trace 一并丢弃，不留下半结算 context。
        working_context = copy(resolution_context)
        working_context.trace = list(resolution_context.trace)
        working_context.deferred_effects = list(resolution_context.deferred_effects)
        working_context._executed_rule_ids = set(resolution_context._executed_rule_ids)

        for hook in sorted(candidates, key=incoming_key):
            working_context.set_current_damage(amount)
            if (isinstance(hook, MechanismHookAdapter)
                    and hook.mechanism.rule_definition is not None):
                amount = hook.on_incoming_adjust(
                    target, amount, damage_type, source, state,
                    rule_context=working_context)
            else:
                amount = hook.on_incoming_adjust(target, amount, damage_type, source, state)
            amount = validate_damage_amount(amount, "incoming_adjust.result")
        # 伤害链允许中间负值参与后续运算，但不能以负伤害治疗受击者。
        if amount < 0:
            working_context.trace.append({
                "mode": working_context.mode,
                "status": "normalized",
                "reason": "negative_damage_floor",
                "before": _trace_number(amount),
                "after": 0,
                "explanation": f"规则窗口结束时将负伤害 {_trace_number(amount)} 归一为 0；不产生治疗",
            })
            amount = 0
        working_context.set_current_damage(amount)
        working_context.post_rule_damage = amount

        # 单一提交点：保留原有 context/list 引用，原子替换本窗口新增数据。
        resolution_context.set_current_damage(amount)
        resolution_context.post_rule_damage = amount
        resolution_context.trace[:] = working_context.trace
        resolution_context.deferred_effects[:] = working_context.deferred_effects
        resolution_context._executed_rule_ids.clear()
        resolution_context._executed_rule_ids.update(working_context._executed_rule_ids)
        return amount

    def reflect_attack_damage(self, target: Any, amount: int, attacker: Any, state: Any) -> int:
        """千荆甲反噬：转发给 QianjingjiaHook（显式分发，不走通用遍历）。"""
        res = 0
        for hook in self._hooks:
            if hasattr(hook, "reflect_attack_damage"):
                res += hook.reflect_attack_damage(target, amount, attacker, state)
        return res

    def apply_before_damage(self, target: Any, amount: int, damage_type: str, attacker: Optional[Any], state: Any) -> Dict[str, Any]:
        result = {}
        for hook in self._hooks:
            if self._is_explicit(hook):
                continue
            if hasattr(hook, "on_before_damage"):
                res = hook.on_before_damage(target, amount, damage_type, attacker, state)
                if res:
                    result.update(res)
        return result

    def apply_after_damage(self, target: Any, actual_damage: int, shield_absorbed: int, detail: Dict[str, Any], attacker: Optional[Any], state: Any) -> Dict[str, Any]:
        """通用 after-damage 遍历。

        AfterDamageEffectsHook 标了 explicit_dispatch，会被跳过——
        它由 apply_after_damage_pipeline() 负责，绝不能在这里再跑一遍。
        """
        result = {}
        for hook in self._hooks:
            if self._is_explicit(hook):
                continue
            if hasattr(hook, "on_after_damage"):
                res = hook.on_after_damage(target, actual_damage, shield_absorbed, detail, attacker, state)
                if res:
                    result.update(res)
        return result

    def apply_dodge(self, entity: Any, state: Any) -> Dict[str, Any]:
        result = {}
        for hook in self._hooks:
            if self._is_explicit(hook):
                continue
            if hasattr(hook, "on_dodge"):
                res = hook.on_dodge(entity, state)
                if res:
                    result.update(res)
        return result

