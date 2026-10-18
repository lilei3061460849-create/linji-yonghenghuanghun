"""CombatEngine 分片：伤害上下文、生命损失记录、承露、「失去生命后」统一拦截与绑定、统一死亡判定、
血限变化、敌对伤害、_on_entity_death、分裂克隆、碎片/封印/洗劫等公共辅助

方法体自 engine/combat.py 原样搬来（逐字节），行为与对外契约不变；
CombatEngine 仍是唯一入口（门面类继承本 Mixin）。
"""
from __future__ import annotations
import math
import weakref
from typing import Optional, Any
from ..models import Entity, StatusEffect, GameState, DaoWenInstance, DaoWen, Spell, Consumable
from ..daowen import DaoWenEngine, ResonanceEngine
from ..dice import DiceEngine
from ..enums import (ActionPhase, TriggerTiming, InterruptType, DamageType,
                     EffectScope, EffectPolarity, CostType)
from ..dm_rulings import Interrupt
from ..combat_events import CombatEvent, CombatEventType, register_combat_event_observer
from ..consumables import ZIYU_TRIGGER_TARGET_DEATH, apply_zi_yu_trigger
from ..combat_hooks import CombatHookManager
from ..effect_context import EffectContext, make_context, normalize_context
from ..mechanisms import MECHANISMS, Phase, TriggerBus, TriggerContext
from ..personality import remove_personality
from ..resolution import (KIND_DEATH, KIND_DAMAGE, KIND_SPLIT,
                           note_delta, resolution_frame)
from ..models import MONSTER_MANA_RELIC
from ..rule_engine import DamageResolutionContext, validate_damage_amount


class DamageDeathMixin:
    def _damage_context(
        self, target: Entity, amount: int, damage_type: str,
        source: Optional[Entity], ctx: Optional[EffectContext | dict],
    ) -> tuple[EffectContext, bool]:
        normalized = normalize_context(ctx)
        if normalized is None:
            # 兼容旧调用：生成可追踪上下文并显式标记 legacy，避免后续迁移时静默漏源。
            return make_context(
                timing=self._current_context_timing(),
                source=getattr(source, "name", "legacy_damage"),
                source_type="legacy",
                actor=source, target=target,
                mechanic="damage", subtype=self._damage_context_subtype(damage_type),
                amount=amount, tags={"legacy_context"},
            ), True
        if normalized.mechanic == "damage":
            return normalized, False
        return make_context(
            timing=normalized.timing, source=normalized.source,
            source_type=normalized.source_type, actor=normalized.actor or source,
            target=normalized.target or target, owner=normalized.owner,
            mechanic="damage", subtype=normalized.subtype or self._damage_context_subtype(damage_type),
            amount=normalized.amount if normalized.amount is not None else amount,
            tags=normalized.tags, event_id=normalized.event_id,
            parent_event_id=normalized.parent_event_id,
        ), False

    def _attach_damage_context(self, detail: dict, ctx: EffectContext, legacy_warning: bool) -> dict:
        detail["ctx"] = ctx.to_dict()
        if legacy_warning:
            detail["context_warning"] = "伤害缺少EffectContext；已按legacy来源兼容记录"
        return detail

    def _damage_noop_result(
        self, target: Entity, amount: int, damage_type: str,
        damage_ctx: EffectContext, legacy_ctx: bool, *, reason: str,
        original_damage: Optional[int] = None,
        applied_damage_type: Optional[str] = None,
    ) -> dict:
        """返回不扣血、不耗护盾、不触发伤害事件的显式 no-op 结果。"""
        hp = target.current_hp
        detail = {
            "raw_damage": amount,
            "shield_absorbed": 0,
            "actual_damage": 0,
            "actual_life_loss": 0,
            "hp_before": hp,
            "hp_after": hp,
            "blood_limit_before": target.blood_limit,
            "died": False,
            "damage_type": damage_type,
            "no_op": True,
            "no_op_reason": reason,
            "rule_trace": [],
            "preview_rule_trace": [],
            "damage_resolution_preview": None,
            "damage_resolution": {
                "event_id": damage_ctx.event_id,
                "original_damage": amount if original_damage is None else original_damage,
                "incoming_damage": amount,
                "damage_type": damage_type,
                "applied_damage_type": applied_damage_type or damage_type,
                "attacker": getattr(damage_ctx.actor, "name", None),
                "recipient": getattr(target, "name", None),
                "preview": None,
                "commit": None,
                "outcome": reason,
            },
        }
        return self._attach_damage_context(detail, damage_ctx, legacy_ctx)

    def _write_hp_loss_record(self, entity: Entity, amount: int,
                              parent_ctx: Optional[EffectContext | dict],
                              subtype: str, reaction_logs: Optional[list],
                              *, source_override: Optional[str] = None,
                              source_type_override: Optional[str] = None,
                              actor_override: Optional[Entity] = None,
                              owner_override: Optional[Entity] = None,
                              tags_extra: Optional[set] = None) -> dict:
        """构造一条“实际失去生命”事件并登记到 entity._hp_loss_events。

        仅负责记账，不触发任何反应。触发统一由调用方决定：
          - 既有 ctx-rich 入口（_apply_hostile_damage / _raw_hp_loss / 流血代价……
            已置 _hp_loss_recording>0 抑制兜底）：在 _record_hp_loss_event 里触发；
          - 其余一切降血（血限压顶/衰老/道纹直接减血/直写 current_hp）经
            Entity.__setattr__ 兜底钩子 _on_entity_hp_fallen 触发，再把结果写回本账。

        parent_ctx 既可能是 EffectContext 也可能是 legacy dict（兜底钩子从
        _hp_loss_ctx 取到），统一经 normalize_context 归一，避免 .timing 直接崩溃。
        """
        parent_ctx = normalize_context(parent_ctx) if parent_ctx is not None else None
        ctx = make_context(
            timing=parent_ctx.timing if parent_ctx else self._current_context_timing(),
            source=source_override or (parent_ctx.source if parent_ctx else "legacy_hp_loss"),
            source_type=source_type_override or (parent_ctx.source_type if parent_ctx else "legacy"),
            actor=actor_override or (parent_ctx.actor if parent_ctx else None),
            target=entity,
            owner=owner_override or (parent_ctx.owner if parent_ctx else None),
            mechanic="hp_loss",
            subtype=subtype,
            amount=amount,
            tags=((set(parent_ctx.tags) if parent_ctx else {"legacy_context"}) | set(tags_extra or ())),
            parent_event_id=parent_ctx.event_id if parent_ctx else None,
        )
        record = ctx.to_dict()
        events = getattr(entity, "_hp_loss_events", None)
        if events is None:
            entity._hp_loss_events = []
            events = entity._hp_loss_events
        events.append(record)
        if reaction_logs:
            record["reaction_logs"] = reaction_logs
        return record

    def _record_hp_loss_event(
        self, entity: Entity, amount: int,
        parent_ctx: Optional[EffectContext] = None,
        *, subtype: str = "damage",
        source_override: Optional[str] = None,
        source_type_override: Optional[str] = None,
        actor_override: Optional[Entity] = None,
        owner_override: Optional[Entity] = None,
        tags_extra: Optional[set] = None,
    ) -> Optional[dict]:
        """记录“实际失去生命”事件；不改变既有 hp_lost_this_round 数值来源。

        「失去生命后」反应法术：无论因何失血（攻击/道纹/代价/血限压迫/爆裂反噬……）
        一律触发。攻击路径的失血由 resolve_attack 的反应窗口结算，此处跳过以免双发；
        反应法术自身的结算会把 _resolving_life_lost_reactions 置 >0，避免连锁死循环。
        """
        if entity is None or amount <= 0:
            return None
        # 先触发（沿用既有语义），再把结果写回同一笔失血账。
        if (self._attack_after_window_target is not entity
                and self._resolving_life_lost_reactions == 0):
            logs = self._fire_after_life_lost(entity, parent_ctx)
        else:
            logs = None
        record = self._write_hp_loss_record(
            entity, amount, parent_ctx, subtype, logs,
            source_override=source_override, source_type_override=source_type_override,
            actor_override=actor_override, owner_override=owner_override,
            tags_extra=tags_extra)
        # 蚀命刻尺订阅统一失血账：每次实际失去生命事件，受损角色增加1层衰老。
        # 失血点数不会被误当作层数，且代价/反噬/血限压迫等入口共用这一处。
        if self._any_active_relic("蚀命刻尺"):
            entity.add_status(StatusEffect(
                name="衰老", remaining_rounds=-1, value=1, source="蚀命刻尺"))
        toll = self._settle_chenglu(entity, amount, parent_ctx)
        if toll:
            record["chenglu"] = toll
        return record

    def _settle_chenglu(self, entity: Entity, amount: int,
                           parent_ctx: Optional[EffectContext | dict] = None) -> Optional[dict]:
        """承露盏（遗物）：每累计失去10点生命，获得1点法力。本场累计，战始归零。

        挂在 _record_hp_loss_event 这条**唯一失血总账**上，所以不区分来源：
        透支的流血、血影的流血、挨打、爆裂反噬……一律计入。这正是它与卖血流
        （血炼周天 = 再生⇄透支）配套的地方——透支每轮流血 4X 本来是纯支出，
        现在每满 10 点返还 1 法力。

        余数滚存（chenglu_paid 记已兑换过的总额），所以"失去 7 + 失去 5"
        照样在第 10 点上结一次账，不会因为分笔挨打而永远凑不满。
        不朽之躯的钳制照常生效：返还的法力仍然过 clamp，不能超过[法限]。
        """
        if entity is None or amount <= 0:
            return None
        if not self._relic_active(entity, "承露盏"):
            return None
        entity.hp_lost_this_battle = getattr(entity, "hp_lost_this_battle", 0) + amount
        paid = getattr(entity, "chenglu_paid", 0)
        gain = (entity.hp_lost_this_battle // 10) - (paid // 10)
        if gain <= 0:
            return None
        entity.chenglu_paid = (entity.hp_lost_this_battle // 10) * 10
        before = entity.current_mana
        # 2026-10-08：走统一入口，【勾魂】期间不生效
        actual = entity.gain_mana(gain)
        self.clamp_immortal_body(entity)
        return {"relic": "承露盏", "hp_lost_total": entity.hp_lost_this_battle,
                "mana_gained": actual, "capped": actual < gain}

    # ---- 「失去生命后」统一拦截：绑定与兜底触发 (2026-08-30) ----
    def _hp_record_entities(self) -> list[Entity]:
        ents: list[Entity] = []
        if self.state.player is not None:
            ents.append(self.state.player)
        ents.extend(self.state.enemies)
        ents.extend(self.state.friends)
        ents.extend(self.state.employees)
        ents.extend(self.state.temp_friends)
        return ents

    def _bind_existing_hp_hooks(self) -> None:
        for entity in self._hp_record_entities():
            self._bind_hp_hook(entity)

    def _bind_hp_hook(self, entity: Optional[Entity]) -> None:
        if entity is None:
            return
        try:
            entity._hp_engine_ref = weakref.ref(self)
        except TypeError:
            pass  # 兜底：非可弱引用对象不绑定（正常 CombatEngine 均可弱引用）

    def _engine_owns(self, entity: Entity) -> bool:
        if entity is None:
            return False
        if entity is self.state.player:
            return True
        for lst in (self.state.enemies, self.state.friends,
                    self.state.employees, self.state.temp_friends):
            for e in lst:
                if e is entity:
                    return True
        return False

    def _on_entity_hp_fallen(self, entity: Entity, old: int, new: int) -> None:
        """Entity.__setattr__ 兜底钩子：任何未被既有入口接管的生命下降都走这里。

        被接管（_hp_loss_recording>0）或攻击失血（_attack_after_window_target）或
        反应自身失血（_resolving_life_lost_reactions>0）时提前返回，避免双发。
        未来新增道纹/遗物只要让 current_hp 变小，无需再手工接线。
        """
        if entity is None or not entity.is_alive:
            return
        if self._hp_loss_recording > 0:
            return
        if self._attack_after_window_target is entity:
            return
        if not self._engine_owns(entity):
            return  # 深拷贝快照/外部实体不越界触发
        ctx = self._hp_loss_ctx
        self._hp_loss_ctx = None  # 用完即清，避免残留上下文污染后续未接线的降血
        # 只检测“生命下降就触发”，不关心具体成因。触发后把结果登记到同一笔失血账，
        # 使血限压顶/衰老/道纹直减等“由兜底钩子接管”的降血也可见、可追踪，
        # 与 ctx-rich 路径（_record_hp_loss_event）保持一致。
        logs = self._fire_after_life_lost(entity, ctx)
        if logs:
            self._write_hp_loss_record(entity, old - new, ctx, "fallback_hp_loss", logs)

    # ========== 统一死亡判定 / 统一血限变化 ==========

    def _check_hp_zero_death(
        self, entity: Optional[Entity],
        ctx: Optional[EffectContext | dict] = None,
    ) -> bool:
        """统一“生命归零 → 命零”判定。

        任何使生命可能归零的状态变化（伤害 / 代价 / 血限压迫 / 迷失 / 特殊事件）
        都必须用这一个入口收口，禁止再写 `entity.is_alive = False`。
        返回本次调用是否判定了死亡（已死者返回 False，保持幂等）。

        注意：本方法只负责“判定 + 通知”，不做任何濒死保护——
        濒死/保护（撤退、负岳碑、断尾求生）在伤害管线更早的 mitigation 阶段完成，
        走到这里说明保护已经没有拦住。
        """
        with resolution_frame(self, KIND_DEATH, "命零",
                              getattr(entity, "name", "?")):
            return self._check_hp_zero_death_inner(entity, ctx)

    def _check_hp_zero_death_inner(
        self, entity: Optional[Entity],
        ctx: Optional[EffectContext | dict] = None,
    ) -> bool:
        """命零判定的实现体（对外契约见 _check_hp_zero_death）。"""
        if entity is None or entity.current_hp > 0:
            return False
        # 永久离场（雕塑/癌变/还债/救赎/逃跑）不是命零，绝不在此处宣布死亡；【封印】暂离不走本管线。
        if getattr(entity, "is_departed", False):
            return False
        # 已经走过统一死亡管线（含 Entity.take_damage 先翻了 is_alive 的情况）就不重复触发。
        if getattr(entity, "_death_triggers_emitted", False):
            return False
        self._hp_loss_recording += 1  # 命零置血=死亡收尾，不触发「失去生命后」
        try:
            entity.current_hp = 0
            entity.is_alive = False
        finally:
            self._hp_loss_recording -= 1
        self._on_entity_death(entity, ctx=ctx)
        # 【自愈X】目标死亡时（2026-10-14 用户裁定）：持有者本场出手选中过的目标死亡
        # 才结算（没打过的不算；命中与否不影响“选中”）。挂统一命零管线内（幂等，一尸一结）；
        # 离场/暂离不走本管线，天然排除。
        if entity is not None and entity.runtime_id in (self.state.player_selected_targets or []):
            for log in apply_zi_yu_trigger(self.state.consumables, ZIYU_TRIGGER_TARGET_DEATH):
                if log["restored"]:
                    self._emit(CombatEventType.CONSUMABLE_ZIYU, actor=self.state.player,
                               target=entity, ctx=ctx, **log)
        return True

    def _record_blood_limit_event(
        self, entity: Entity, delta: int,
        parent_ctx: Optional[EffectContext] = None,
        *, source: str = "", source_type: str = "", subtype: str = "",
        actor: Optional[Entity] = None, owner: Optional[Entity] = None,
        tags: Optional[set[str]] = None,
    ) -> Optional[dict]:
        """记录一次血限变化的来源上下文，并发出 BLOOD_LIMIT_CHANGED 事件。

        血限变化是因果链的中间跳（伤害 → 血限下降 → 依赖血限的效果），
        没有它，“血限为什么掉”只能靠 Hook 自己猜。
        """
        if entity is None or delta == 0:
            return None
        if tags is None:
            base = set(parent_ctx.tags) if parent_ctx else {"legacy_context"}
            tags = base | {"blood_limit_loss" if delta < 0 else "blood_limit_gain"}
        ctx = make_context(
            timing=parent_ctx.timing if parent_ctx else self._current_context_timing(),
            source=source or (parent_ctx.source if parent_ctx else "legacy_blood_limit"),
            source_type=source_type or (parent_ctx.source_type if parent_ctx else "legacy"),
            actor=actor if actor is not None else (parent_ctx.actor if parent_ctx else None),
            target=entity,
            owner=owner if owner is not None else (parent_ctx.owner if parent_ctx else None),
            mechanic="blood_limit_change",
            subtype=subtype or ("cut" if delta < 0 else "gain"),
            amount=delta,
            tags=tags,
            parent_event_id=parent_ctx.event_id if parent_ctx else None,
        )
        record = ctx.to_dict()
        events = getattr(entity, "_blood_limit_events", None)
        if events is None:
            entity._blood_limit_events = []
            events = entity._blood_limit_events
        events.append(record)
        self._emit(
            CombatEventType.BLOOD_LIMIT_CHANGED,
            actor=ctx.actor, target=entity, ctx=record,
            delta=delta, blood_limit_after=entity.blood_limit,
        )
        return record

    def _heal_blocked(self, entity: Entity) -> bool:
        """目标是否被禁疗：只有【坏死】这一个「无法获得[回复]」的实现。

        2026-10-08 用户令删除【镇尸】（与【坏死】硬重复）后，禁疗的判定点收敛为单一来源。
        """
        return entity is not None and entity.has_status("坏死")

    def _apply_blood_limit_change(
        self, entity: Entity, delta: int, source: str, polarity: str,
        *, ctx: Optional[EffectContext | dict] = None,
        source_type: str = "", subtype: str = "",
        actor: Optional[Entity] = None, owner: Optional[Entity] = None,
        tags: Optional[set[str]] = None,
        clamp_hp: bool = True, lethal: bool = True,
    ) -> dict:
        """血限变化的统一入口：登记账本 → 记录来源 → 生命封顶 → 统一命零判定。

        数值行为与原来散落的 `_battle_delta + current_hp=min(...) + 手写命零` 完全一致，
        只是把“来源上下文”和“命零判定”固定下来，防止再出现
        “血限压到 0 生命却仍然 is_alive=True”的非法状态。
        """
        parent = normalize_context(ctx)
        applied = self._battle_delta(entity, "blood_limit", delta, source, polarity)
        bl_ctx = self._record_blood_limit_event(
            entity, applied, parent, source=source, source_type=source_type,
            subtype=subtype, actor=actor, owner=owner, tags=tags)
        if clamp_hp:
            # 血限压降导致的当前生命下降同样是失血；统一交给
            # Entity.__setattr__ 的「失去生命后」钩子触发，不再手工接线。
            # 「失去生命前」：血限压迫即将把当前生命压下来。
            if entity.current_hp > entity.blood_limit and self._attack_after_window_target is not entity:
                self._fire_before_life_lost(entity, bl_ctx or parent)
            self._hp_loss_ctx = bl_ctx or parent
            # 2026-10-14：【猩红果实】持有者豁免血限压顶（生命可超血限）。
            if not self.state.hp_overflow_exempt(entity):
                entity.current_hp = min(entity.current_hp, entity.blood_limit)
        died = self._check_hp_zero_death(entity, ctx=bl_ctx or parent) if lethal else False
        return {"applied": applied, "ctx": bl_ctx, "died": died}

    def _apply_hostile_damage(self, target: Entity, amount: int,
                              damage_type: str = DamageType.NORMAL.value,
                              source: Optional[Entity] = None,
                              ctx: Optional[EffectContext | dict] = None) -> dict:
        """
        对target造成外部/敌对伤害的统一入口（通过 HookManager 全生命周期调度）。
        ctx 为兼容层来源上下文，不改变既有伤害结算顺序和返回核心字段。
        """
        amount = validate_damage_amount(amount, "CombatEngine._apply_hostile_damage.amount")
        damage_ctx, legacy_ctx = self._damage_context(target, amount, damage_type, source, ctx)
        if amount < 0:
            return self._damage_noop_result(
                target, amount, damage_type, damage_ctx, legacy_ctx,
                reason="negative_damage_input",
            )
        # 结算生命周期：深度/预算保险丝与可追踪性统一由 ResolutionContext 记账
        # （见 engine/resolution.py）。阈值 64 与既有 MAX_EFFECT_CHAIN_DEPTH 同义，
        # 实测合法峰值 5，故行为不变——只在真出现 A→B→A 循环时截断成可诊断异常。
        hp_before = target.current_hp
        shield_before = getattr(target, "shield", 0)
        with resolution_frame(self, KIND_DAMAGE, damage_ctx.source,
                              getattr(target, "name", "?"), amount):
            self._hp_loss_recording += 1  # 伤害失血由 _record_hp_loss_event 接管，抑制兜底钩子
            try:
                detail = self._apply_hostile_damage_inner(
                    target, amount, damage_type, source, damage_ctx, legacy_ctx)
                # 饥荒的偏爱：造成实际伤害后，削减目标等量法限；当前法力同步封顶。
                # 只在统一敌对伤害入口结算，避免道纹/攻击等不同来源重复触发。
                if (source is not None and "饥荒的偏爱" in getattr(source, "preferences", [])
                        and isinstance(detail, dict) and not detail.get("no_op")):
                    mana_loss = min(max(0, int(getattr(target, "mana_limit", 0))),
                                    max(0, int(detail.get("actual_damage", 0) or 0)))
                    if mana_loss:
                        target.mana_limit = max(0, target.mana_limit - mana_loss)
                        target.current_mana = min(target.current_mana, target.mana_limit)
                        detail["famine_preference"] = {
                            "target": target.name, "mana_limit_reduced": mana_loss,
                            "mana_limit_after": target.mana_limit,
                        }
                return detail
            finally:
                self._hp_loss_recording -= 1
                note_delta(self, "hp", hp_before, target.current_hp)
                # 护盾吸收也要看得见：否则「打了却没掉血」在 trace 上是空白的
                note_delta(self, "shield", shield_before,
                           getattr(target, "shield", 0))

    def _apply_hostile_damage_inner(
        self, target: Entity, amount: int, damage_type: str,
        source: Optional[Entity], damage_ctx: EffectContext, legacy_ctx: bool,
    ) -> dict:
        original_damage = amount
        # 逆血龙鳞：本回合实际失血累计转为本次造成伤害的固定加值。
        if source is not None and self._relic_active(source, "逆血龙鳞"):
            amount += max(0, int(getattr(source, "hp_lost_this_round", 0) or 0))
        amount = validate_damage_amount(
            self.hook_manager.apply_multiplier_adjust(target, amount, damage_type, source, self.state),
            "CombatEngine.apply_multiplier_adjust.result")
        if amount < 0:
            return self._damage_noop_result(
                target, amount, damage_type, damage_ctx, legacy_ctx,
                reason="negative_damage_after_multiplier_adjust",
                original_damage=original_damage,
            )
        # 招架属于受击姿态资源，仍在格挡之前卸力；道纹持续状态则在格挡
        # 之后一起进入同一 X 排序窗口（由 take_damage 的 status_adjuster 调用）。
        amount = validate_damage_amount(
            self._apply_parry_reduction(target, amount, damage_type),
            "CombatEngine._apply_parry_reduction.result")
        if amount < 0:
            return self._damage_noop_result(
                target, amount, damage_type, damage_ctx, legacy_ctx,
                reason="negative_damage_after_parry",
                original_damage=original_damage,
            )
        # 癫狂之脑（通用遗物池，2026-10-03 新增）：你受到的**攻击伤害**翻倍。
        # 只翻攻击行动造成的伤害（subtype=attack；含被万钧印改道的自攻）；
        # 道纹/代价/直接失血不受影响。倍率发生在格挡/护盾之前，故本次伤害的
        # "攻击伤害数值"整体翻倍，千荆甲反噬也按翻倍后的数值等量反射。
        if (damage_ctx.subtype == "attack"
                and self._relic_active(target, "癫狂之脑")):
            amount = validate_damage_amount(
                amount * 2, "CombatEngine.狂脑伤害倍率.result")

        # 1. 伤害重定向 (背负；【嫁祸】2026-10-11 已删除)
        redirected_target = self.hook_manager.apply_redirection(target, damage_type, self.state)
        if redirected_target is not None:
            redirected_ctx = make_context(
                timing=damage_ctx.timing, source=damage_ctx.source,
                source_type=damage_ctx.source_type, actor=damage_ctx.actor,
                target=redirected_target, owner=damage_ctx.owner,
                mechanic="damage", subtype=damage_ctx.subtype, amount=amount,
                tags=set(damage_ctx.tags) | {"redirected"},
                parent_event_id=damage_ctx.event_id,
            )
            return self._apply_hostile_damage(redirected_target, amount, damage_type, source, ctx=redirected_ctx)

        # 2. 受到伤害前反噬 (千荆甲遗物：受到攻击伤害前，攻击者受等量伤害)
        before_res = self.hook_manager.apply_before_damage(target, amount, damage_type, source, self.state)
        # 千荆甲（通用遗物池，2026-10-03 出自被删【爆裂】道纹）：你受到攻击伤害**前**，
        # 攻击者受到等量伤害。只对攻击行动（subtype=attack）生效，非攻击伤害不触发；
        # 反噬本身是"直接失血"，不视为"受到攻击造成的伤害"，因此不会连环触发千荆甲/活血衣。
        # 归因口径沿用 2026-10-03 裁定：这笔失血记【千荆甲】。
        reflect_amount = 0
        if (source is not None and source is not target
                and damage_ctx.subtype == "attack"
                and self._relic_active(target, "千荆甲")):
            reflect_amount = self.hook_manager.reflect_attack_damage(
                target, amount, source, self.state)
        if reflect_amount > 0:
            self._resolve_reflect_aftermath(source, reflect_amount, damage_ctx, target)
            if not source.is_alive:
                # 与旧【爆裂】同口径：反噬把攻击者打死的这一击不再落地
                # （攻击者已命零，本次攻击的伤害被压制为 0）。
                suppressed = self._damage_noop_result(
                    target, amount, damage_type, damage_ctx, legacy_ctx,
                    reason="qianjingjia_suppress",
                    original_damage=original_damage,
                )
                suppressed["qianjingjia_suppress"] = True
                return suppressed

        # 3. 基础扣血。贯穿：你造成的伤害（任意通道）无视格挡；代价仍按代价结算。
        apply_type = damage_type
        if (source is not None and damage_type != "代价"
                and hasattr(source, "has_status") and source.has_status("贯穿")):
            apply_type = "无视格挡"

        # 4. 濒死伤害拦截与保护 (撤退 / 负岳碑 / 断尾求生)。保护需要看到
        # 真正会落地的数值，故在不修改实体的前提下预演一次「格挡→持续状态
        # X 排序」；真实扣血时 take_damage 会按同一函数再结算一次。
        shield_before_preview = getattr(target, "shield", 0)
        if apply_type == "无视格挡" or damage_type == "代价":
            preview_after_shield = amount
            preview_shield_absorbed = 0
        else:
            preview_after_shield = max(0, amount - shield_before_preview)
            preview_shield_absorbed = max(0, min(amount, shield_before_preview))
        preview_rule_context = DamageResolutionContext(
            event_id=damage_ctx.event_id,
            attacker=source,
            recipient=target,
            damage_type=apply_type,
            original_damage=original_damage,
            incoming_damage=amount,
            current_damage=preview_after_shield,
            mode="preview",
            shield_absorbed=preview_shield_absorbed,
            hp_before=target.current_hp,
            tags=frozenset(damage_ctx.tags),
        )
        preview_adjusted = (
            self.hook_manager.apply_incoming_adjust(
                target, preview_after_shield, apply_type, source, self.state,
                resolution_context=preview_rule_context)
            if preview_after_shield > 0 and damage_type != "代价"
            else preview_after_shield
        )
        preview_rule_context.post_rule_damage = preview_adjusted
        # LethalMitigationHook 自己按传入 type 处理格挡；normal 情况补回护盾
        # 即可让它看到同一份 post-shield/post-status 的剩余伤害。
        mitigation_input = (preview_adjusted if apply_type == "无视格挡"
                            else preview_adjusted + getattr(target, "shield", 0))
        mitigation = self.hook_manager.apply_mitigation(target, mitigation_input, apply_type, self)
        if mitigation is not None:
            preview_rule_context.post_rule_damage = preview_adjusted
            mitigation.setdefault("actual_life_loss", 0)
            mitigation.setdefault("rule_trace", [])
            mitigation["damage_resolution_preview"] = preview_rule_context.to_dict()
            mitigation["preview_rule_trace"] = list(preview_rule_context.trace)
            mitigation["damage_resolution"] = {
                "event_id": damage_ctx.event_id,
                "original_damage": original_damage,
                "incoming_damage": amount,
                "damage_type": damage_type,
                "applied_damage_type": apply_type,
                "attacker": getattr(source, "name", None),
                "recipient": getattr(target, "name", None),
                "preview": preview_rule_context.to_dict(),
                "commit": None,
                "outcome": "lethal_mitigation",
            }
            return self._attach_damage_context(mitigation, damage_ctx, legacy_ctx)

        # 2026-10-14 审计F4：【替死鬼】——玩家即将受到致死伤时，将其转移给攻击者。
        # 位置在濒死保护之后：已有保护（撤退/负岳碑/断尾求生）优先，替死鬼是最后兜底。
        # 致死按预演落地值判定（与保护钩子同一份预演）；转移量=预演落地值，
        # 攻击者侧重新走完整管线（其格挡/保护照常生效）；转移伤害 subtype 不再是
        # attack（不触发攻击者【千荆甲】反噬，避免 A→B→A 循环）。
        if (target is self.state.player and target.is_alive
                and source is not None and source is not target and source.is_alive
                and preview_adjusted > 0 and preview_adjusted >= target.current_hp):
            ghost = next((c for c in self.state.consumables
                          if c.name == "替死鬼" and not c.is_depleted), None)
            if ghost is not None:
                ghost.use()
                transfer_ctx = make_context(
                    timing=damage_ctx.timing, source="替死鬼", source_type="consumable",
                    actor=target, target=source, owner=target,
                    mechanic="damage", subtype="tishigui_transfer", amount=preview_adjusted,
                    tags=set(damage_ctx.tags) | {"consumable", "lethal_transfer"},
                    parent_event_id=damage_ctx.event_id,
                )
                transfer = self._apply_hostile_damage(
                    source, preview_adjusted, DamageType.NORMAL.value, target, ctx=transfer_ctx)
                result = self._damage_noop_result(
                    target, amount, damage_type, damage_ctx, legacy_ctx,
                    reason="tishigui_transfer", original_damage=original_damage)
                result["tishigui_transfer"] = {"transferred": preview_adjusted,
                                               "attacker": source.name,
                                               "attacker_detail": transfer}
                return result

        # ---- 「受到伤害前 / 失去生命前」自动反应窗口（非攻击伤害） ----
        # 攻击路径（resolve_attack）由显式反应窗口结算，此处跳过以免双发；只有
        # 非攻击伤害（道纹/反噬等）才在这里自动触发，且无需逐个效果开窗。
        reaction_logs: list = []
        if (self._attack_after_window_target is not target
                and amount > 0 and target.is_alive):
            before = self._fire_auto_reaction(
                target, ActionPhase.BEFORE_DAMAGE_TAKEN.value, damage_ctx)
            if before:
                reaction_logs.extend(before)
            if source is not None and not source.is_alive:
                amount = 0
            # 失去生命前：受到伤害前反应已结算、伤害数值已确定，但生命尚未扣减。
            if amount > 0 and target.is_alive:
                life_before = self._fire_auto_reaction(
                    target, ActionPhase.BEFORE_LIFE_LOST.value, damage_ctx)
                if life_before:
                    reaction_logs.extend(life_before)
                if source is not None and not source.is_alive:
                    amount = 0
        # 【第一杯】：持有者失去的生命翻倍。倍率在这里注入；take_damage 先让
        # 格挡吸收，再把仍存在的伤害交给统一规则链，最后才应用失血倍率并扣血。
        # 预演 context 和正式 context 分开，规则 ID 可在 preview/commit 各执行一次，
        # 但同一 context 内重复执行会被拒绝。
        shield_before_commit = getattr(target, "shield", 0)
        hp_before_commit = target.current_hp
        committed_rule_contexts: list[DamageResolutionContext] = []

        def _commit_incoming_rules(remaining: int) -> int:
            committed_context = DamageResolutionContext(
                event_id=damage_ctx.event_id,
                attacker=source,
                recipient=target,
                damage_type=apply_type,
                original_damage=original_damage,
                incoming_damage=amount,
                current_damage=remaining,
                mode="commit",
                shield_absorbed=max(0, shield_before_commit - getattr(target, "shield", 0)),
                hp_before=hp_before_commit,
                tags=frozenset(damage_ctx.tags),
            )
            committed_rule_contexts.append(committed_context)
            return self.hook_manager.apply_incoming_adjust(
                target, remaining, apply_type, source, self.state,
                resolution_context=committed_context)

        detail = target.take_damage(
            amount, apply_type,
            life_loss_multiplier=self.state.life_loss_multiplier(target),
            status_adjuster=_commit_incoming_rules)
        self._attach_damage_context(detail, damage_ctx, legacy_ctx)
        actual = detail.get("actual_damage", 0)
        actual_life_loss = max(0, detail.get("hp_before", target.current_hp)
                               - detail.get("hp_after", target.current_hp))
        detail["actual_life_loss"] = actual_life_loss
        committed_rule_context = committed_rule_contexts[-1] if committed_rule_contexts else None
        if committed_rule_context is not None:
            committed_rule_context.post_rule_damage = committed_rule_context.current_damage
            committed_rule_context.damage_after_life_loss_multiplier = detail.get("damage_after_life_loss_multiplier", actual)
            committed_rule_context.actual_life_loss = actual_life_loss
            committed_rule_context.hp_before = detail.get("hp_before")
            committed_rule_context.hp_after = detail.get("hp_after")
        detail["damage_resolution_preview"] = preview_rule_context.to_dict()
        detail["preview_rule_trace"] = list(preview_rule_context.trace)
        detail["rule_trace"] = (list(committed_rule_context.trace)
                                if committed_rule_context is not None else [])
        detail["damage_resolution"] = {
            "original_damage": original_damage,
            "incoming_damage": amount,
            "damage_type": damage_type,
            "applied_damage_type": apply_type,
            "attacker": getattr(source, "name", None),
            "recipient": getattr(target, "name", None),
            "preview": preview_rule_context.to_dict(),
            "commit": committed_rule_context.to_dict() if committed_rule_context else None,
        }
        # 活血衣（通用遗物池，2026-10-03 出自被删【活血】道纹）：受到攻击伤害后，
        # 恢复该次伤害一半的生命（向上取整，整数口径）。反噬等直接失血不算"受到攻击"。
        if (damage_ctx.subtype == "attack" and actual > 0
                and self._relic_active(target, "活血衣")):
            heal_n = math.ceil(actual / 2)
            h = self.state.apply_heal(target, heal_n, ctx={
                "timing": self._current_context_timing(), "source": "活血衣", "source_type": "relic",
                "actor": target, "target": target, "owner": target, "mechanic": "heal",
                "subtype": "relic_heal_huoxueyi", "amount": heal_n,
                "tags": {"relic", "after_damage"},
                "parent_event_id": damage_ctx.event_id,
            })
            detail["huoxueyi_heal"] = {"relic": "活血衣", "amount": heal_n,
                                       "actual": h.get("actual_heal"), "ctx": h.get("heal_ctx")}
        # 增生药剂（通用遗物池，2026-10-03 新增）：你每次对自己造成伤害后，恢复10生命。
        # 触发口径＝这份伤害的 actor 就是被打者本人（self_attack / 自伤类），
        # 或显式带 self_damage 标签；反噬等他人造成的直接失血不触发。
        if (actual > 0 and self._relic_active(target, "增生药剂")
                and (damage_ctx.actor is target or "self_damage" in set(damage_ctx.tags or ()))):
            zs_ctx = make_context(
                timing=self._current_context_timing(), source="增生药剂", source_type="relic",
                actor=target, target=target, owner=target, mechanic="heal",
                subtype="relic_heal_zengsheng", amount=10,
                parent_event_id=damage_ctx.event_id, tags={"relic", "self_damage_followup"})
            zh = self.state.apply_heal(target, 10, ctx=zs_ctx)
            detail["zengsheng_heal"] = {"relic": "增生药剂", "amount": 10,
                                        "actual": zh.get("actual_heal"), "ctx": zh.get("heal_ctx")}
        # 「受到伤害后」自动反应窗口：这一击已完整落地（即使被格挡全部吸收也算
        # "受到了伤害"，与"失去生命后"要求 actual_damage>0 严格区分）。
        if (self._attack_after_window_target is not target
                and amount > 0 and target.is_alive):
            damage_after = self._fire_auto_reaction(
                target, ActionPhase.AFTER_DAMAGE_TAKEN.value, damage_ctx)
            if damage_after:
                reaction_logs.extend(damage_after)
        if reaction_logs:
            detail["reaction_logs"] = reaction_logs
        hp_loss_ctx = self._record_hp_loss_event(target, actual, damage_ctx, subtype="damage")
        if hp_loss_ctx:
            detail["hp_loss_ctx"] = hp_loss_ctx
        self._emit(
            CombatEventType.DAMAGE_APPLIED, actor=source, target=target, ctx=detail["ctx"],
            raw_damage=amount, actual_damage=actual,
            actual_life_loss=actual_life_loss,
            shield_absorbed=detail.get("shield_absorbed", 0),
            hp_after=detail.get("hp_after"), damage_type=damage_type,
            rule_trace=detail["rule_trace"],
            preview_rule_trace=detail["preview_rule_trace"],
        )

        # 5. 落地后效果 (逆鳞 / 伤痕 / 寄生 / 负岳索 / 龙族血脉斩杀)
        self.hook_manager.apply_after_damage_pipeline(target, actual, detail.get("shield_absorbed", 0), detail, source, self)
        if target.entity_type == "怪物" and target.is_alive:
            redemption = self.check_redemption(target)
            if redemption:
                detail["redemption"] = redemption

        return detail

    def _on_entity_death(self, entity: Entity, ctx: Optional[EffectContext | dict] = None) -> None:
        """统一死亡触发；重复通知通过实体标记幂等。"""
        if getattr(entity, "_death_triggers_emitted", False):
            return
        parent = normalize_context(ctx)
        # 死因优先级：离场原因 > 调用方显式给出的死亡上下文 subtype > 兜底 hp_zero。
        # （【迷失】【迷失·命零】【凡庸】【尸爆】等特殊死因靠这一步才能留在 _death_ctx 里。）
        subtype = getattr(entity, "departure_reason", "")
        if not subtype and parent is not None and parent.subtype:
            if parent.mechanic == "death":
                subtype = parent.subtype
            elif parent.mechanic in self.NAMED_DEATH_MECHANICS:
                subtype = self.NAMED_DEATH_MECHANICS[parent.mechanic]
        death_ctx = make_context(
            timing=parent.timing if parent else self._current_context_timing(),
            source=parent.source if parent else "legacy_death",
            source_type=parent.source_type if parent else "legacy",
            actor=parent.actor if parent else None,
            target=entity,
            owner=parent.owner if parent else None,
            mechanic="death",
            subtype=subtype or "hp_zero",
            amount=0,
            tags=(set(parent.tags) if parent else {"legacy_context"}),
            parent_event_id=parent.event_id if parent else None,
        )
        entity._death_ctx = death_ctx.to_dict()
        entity._death_triggers_emitted = True
        # 性格特征生命周期（2026-08-26）：命零即随实例清除（幂等）。
        # 挂在统一死亡管线里，AI/事件系统/查询接口此后都读不到该角色性格；
        # 不写模板、不跨实例继承、不留永久人格历史。
        remove_personality(self.state, entity)
        self._emit(
            CombatEventType.ENTITY_DIED, actor=death_ctx.actor, target=entity,
            ctx=entity._death_ctx, entity_type=entity.entity_type,
            cause=death_ctx.subtype,
        )
        if entity.entity_type == "怪物":
            # 乱葬岗·招魂：记录本场已命零怪物尸体
            if not getattr(self.state, "dead_monsters", None):
                self.state.dead_monsters = []
            if entity not in self.state.dead_monsters:
                self.state.dead_monsters.append(entity)
        # 2026-09-17：【分裂】改为即时创生（见 _spawn_fenlie_clones），
        # 原「[命零]时按本体 20% 血限创造复制体」的分支已随重做删除。

    def _spawn_fenlie_clones(self, caster, count: int, clone_hp: int) -> list:
        """【分裂】即时创造复制体：count 个 clone_hp 血限/生命的自身复制体。

        复制体继承本体除【分裂】外的全部道纹（避免无限套娃），阵营与本体一致
        （本体是怪物→进 enemies；否则→进 temp_friends）。返回新建的复制体列表。
        """
        with resolution_frame(self, KIND_SPLIT, "分裂",
                              getattr(caster, "name", "?"), count):
            return self._spawn_fenlie_clones_inner(caster, count, clone_hp)

    def _spawn_fenlie_clones_inner(self, caster, count: int, clone_hp: int) -> list:
        """分裂的实现体（对外契约见 _spawn_fenlie_clones）。"""
        clones = []
        for i in range(max(0, count)):
            clone = Entity(name=f"{caster.name}·裂{i + 1}",
                           entity_type=caster.entity_type,
                           blood_limit=clone_hp, current_hp=clone_hp,
                           attack_count=caster.attack_count,
                           attack_power=caster.attack_power,
                           copy_creator_runtime_id=caster.runtime_id)
            for dw_name, dw_inst in caster.dao_wen.items():
                if dw_name == "分裂":
                    continue      # 复制体无分裂道纹，防止无限分裂
                clone.dao_wen[dw_name] = dw_inst
            self._bind_hp_hook(clone)
            if caster.entity_type == "怪物":
                self.state.enemies.append(clone)
            else:
                self.state.temp_friends.append(clone)
            clones.append(clone)
        return clones

    # ---- F2 全量：罪孽/扭曲专属道纹的公共辅助 ----
    def _resolve_reflect_aftermath(self, attacker: Entity, amount: int,
                                   damage_ctx: EffectContext, reflector: Entity) -> dict:
        """千荆甲反噬落地（直接失血，绕过格挡/护盾/减伤，与旧【爆裂】Hook 同口径）。

        - 【第一杯】失血倍率照常生效；
        - 计入 attacker.hp_lost_this_round（【活血】等按本回合失血结算的效果看得见）；
        - 失血账与死因**记【千荆甲】**（2026-10-03 归因口径，沿用爆裂改记来源的做法）；
        - 不走受到伤害/失去生命的反应窗口（与旧爆裂一致：反噬不是"挨了一下"）。
        """
        prev_hp = attacker.current_hp
        # amount 已含【第一杯】倍率（由 reflect_attack_damage 换算），这里只扣一次。
        loss = amount
        self._hp_loss_recording += 1
        try:
            attacker.current_hp = max(0, attacker.current_hp - loss)
        finally:
            self._hp_loss_recording -= 1
        lost = prev_hp - attacker.current_hp
        if lost > 0:
            attacker.hp_lost_this_round += lost
        # 归因口径沿用 2026-10-03 裁定（爆裂改记来源时的做法）：这笔失血记【千荆甲】，
        # parent 直接挂本次伤害的 event_id，不额外造中间层。
        hp_loss_ctx = self._record_hp_loss_event(
            attacker, lost, damage_ctx, subtype="relic_reflect",
            source_override="千荆甲", source_type_override="relic",
            actor_override=reflector, owner_override=reflector,
            tags_extra={"relic", "reflect"})
        result = {"hp_before": prev_hp, "hp_after": attacker.current_hp,
                  "reflected": lost, "hp_loss_ctx": hp_loss_ctx}
        if attacker.current_hp <= 0:
            self._check_hp_zero_death(attacker, ctx=make_context(
                timing=damage_ctx.timing, source="千荆甲", source_type="relic",
                actor=reflector, target=attacker, owner=reflector, mechanic="death",
                subtype="relic_reflect", tags={"relic", "reflect"},
                parent_event_id=damage_ctx.event_id))
            result["died"] = True
        return result

    def _shards_of(self, entity: Entity) -> int:
        """实体可失去/被夺取的碎片量；假碎片优先，负债不抵消仍可支付的假碎片。"""
        if entity is self.state.player:
            return self.state.fake_shards + max(0, self.state.shards)
        return entity.fake_shards + max(0, entity.shards)

    def _lose_shards_of(self, entity: Entity, amount: int) -> int:
        """实体失去碎片（假碎片优先，玩家走 state.lose_shards）。返回实际失去的真碎片数。"""
        if entity is self.state.player:
            return self.state.lose_shards(amount)
        return entity.lose_shards(amount)

    def _raw_hp_loss(
        self, entity: Entity, amount: int,
        ctx: Optional[EffectContext | dict] = None,
    ) -> dict:
        """直接生命损失（绕过格挡；爆裂反射/遗物灾厄轮盘用），计入失血追踪，含命零判定。"""
        before = entity.current_hp
        reaction_logs: list = []
        # 「失去生命前」自动反应窗口：非攻击直接失血（爆裂/遗物灾厄轮盘等），生命尚未扣减。
        if amount > 0 and entity.is_alive and self._attack_after_window_target is not entity:
            reaction_logs = self._fire_before_life_lost(entity, ctx) or []
        self._hp_loss_recording += 1  # 直接失血由 _record_hp_loss_event 接管，抑制兜底钩子
        try:
            # 【第一杯】：直接失血（爆裂反射/遗物灾厄轮盘/血影等）同样属于「失去的生命」。
            entity.current_hp = max(
                0, entity.current_hp - max(0, amount) * self.state.life_loss_multiplier(entity))
        finally:
            self._hp_loss_recording -= 1
        lost = before - entity.current_hp
        entity.hp_lost_this_round += lost
        parent = normalize_context(ctx)
        hp_loss_ctx = self._record_hp_loss_event(entity, lost, parent, subtype="raw")
        # died 的口径与重构前保持一致：只看生命是否归零（已命零者仍报 True）；
        # 真正的死亡通知交给统一入口，重复通知由 _on_entity_death 幂等吸收。
        died = entity.current_hp <= 0
        if died:
            self._check_hp_zero_death(entity, ctx=hp_loss_ctx or parent)
        result = {"hp_before": before, "hp_after": entity.current_hp, "lost": lost, "died": died}
        if hp_loss_ctx:
            result["hp_loss_ctx"] = hp_loss_ctx
        if reaction_logs:
            result["reaction_logs"] = reaction_logs
        if ctx is None and lost > 0:
            result["context_warning"] = "直接失去生命缺少EffectContext；已按legacy来源兼容记录"
        return result

    def _seal_one_relic(self, target: Entity, rounds: int) -> str:
        """抵扣X：封印目标拥有的一件遗物，持续X回合。返回被封印的遗物名；目标无遗物返回\"\"。

        2026-09-28：旧【抵扣】改名【豪夺】并改为"夺取"，但封印路径仍保留给旧事件/状态使用。
        """
        if target is self.state.player:
            holder = self.state
            owned = [r.name for r in self.state.relics]
        else:
            holder = target
            owned = [r.name for r in getattr(target, "relics", [])]
        # 目标无遗物 → 无效果；已被夺取的遗物也不再属于该目标，不算可封印目标
        if not owned:
            return ""
        stolen_set = {k for k, v in (getattr(holder, "stolen_relics", {}) or {}).items()
                      if v.get("remaining", 0) > 0}
        # 封印第一件未在封印/未被夺取走的遗物
        for rname in owned:
            if holder.sealed_relics.get(rname, 0) <= 0 and rname not in stolen_set:
                holder.sealed_relics[rname] = max(1, rounds)
                return rname
        first = owned[0]
        holder.sealed_relics[first] = max(holder.sealed_relics.get(first, 0), rounds)
        return first

    def _steal_one_relic(
        self, caster: Entity, target: Entity, rounds: int,
    ) -> str:
        """豪夺X（2026-09-28）：消耗5X碎片，夺取[目标]1件遗物持续X回合。
        夺取期间施法者真正持有该遗物（享受其被动），目标暂时失去；回终递减回合，到期归还。
        返回被夺取的遗物名；目标无遗物可夺返回\"\"。
        """
        # 定位遗物容器
        if target is self.state.player:
            tgt_list = self.state.relics
            tgt_sealed = self.state.sealed_relics
            tgt_stolen = self.state.stolen_relics
        else:
            tgt_list = getattr(target, "relics", None) or []
            tgt_sealed = target.sealed_relics
            tgt_stolen = target.stolen_relics
        if caster is self.state.player:
            cst_list = self.state.relics
            cst_stolen = self.state.stolen_relics
        else:
            if not hasattr(caster, "relics"):
                caster.relics = []
            cst_list = caster.relics
            cst_stolen = caster.stolen_relics
        # 过滤出真正"在目标身上、未被封印、未被夺取走"的候选
        sealed_names = {k for k, v in (tgt_sealed or {}).items() if v > 0}
        already_stolen = {k for k, v in (tgt_stolen or {}).items() if v.get("remaining", 0) > 0}
        candidates = [r for r in tgt_list
                      if r.name not in sealed_names and r.name not in already_stolen]
        if not candidates:
            return ""
        stolen = candidates[0]
        # 从目标列表移除
        tgt_list.remove(stolen)
        # 加入施法者列表（标记为"豪夺来的"，到期归还）
        cst_list.append(stolen)
        cst_stolen[stolen.name] = {
            "remaining": max(1, rounds),
            "from_entity_ref": id(target),
            "from_entity_name": target.name,
            "target_is_player": target is self.state.player,
        }
        return stolen.name

    def _tick_sealed_and_stolen_relics(self) -> dict:
        """[回终]处理封印/豪夺遗物的回合递减，到期归还（2026-09-28 加入豪夺归还）。"""
        returned: list[str] = []
        # —— 己方（state）——
        for holder_list, holder_stolen, sealed_attr in (
            (self.state.relics, self.state.stolen_relics, self.state.sealed_relics),
        ):
            # 封印递减
            expired_seals = []
            for name in list(sealed_attr.keys()):
                sealed_attr[name] -= 1
                if sealed_attr[name] <= 0:
                    expired_seals.append(name)
                    del sealed_attr[name]
            # 豪夺递减（玩家侧持有别人遗物）
            expired_steals = []
            for name, info in list(holder_stolen.items()):
                info["remaining"] -= 1
                if info["remaining"] <= 0:
                    expired_steals.append(name)
            # 归还豪夺遗物（简易策略：回到原 owner 列表末尾，且解除对方封印冲突）
            for name in expired_steals:
                info = holder_stolen.pop(name)
                relic_obj = next((r for r in holder_list if r.name == name), None)
                if relic_obj is not None:
                    holder_list.remove(relic_obj)
                    # 还原到原目标
                    if info.get("target_is_player"):
                        self.state.relics.append(relic_obj)
                    else:
                        # 回给对应实体：按 from_entity_name 在 enemies/friends/employees/temp_friends 里找
                        for lst in (self.state.enemies, getattr(self.state, "friends", []),
                                    getattr(self.state, "employees", []),
                                    getattr(self.state, "temp_friends", [])):
                            owner = next((e for e in lst if e.name == info.get("from_entity_name")), None)
                            if owner is not None:
                                if not hasattr(owner, "relics"):
                                    owner.relics = []
                                owner.relics.append(relic_obj)
                                break
                    returned.append(name)
        # —— 每个非玩家实体：递减其自身 sealed_relics / stolen_relics ——
        for entity in self._combat_entity_refs().values():
            if entity is self.state.player:
                continue
            for attr in ("sealed_relics", "stolen_relics"):
                d = getattr(entity, attr, None) or {}
                for name in list(d.keys()):
                    d[name] = (d[name] - 1) if isinstance(d[name], int) else dict(d[name])
                    if isinstance(d[name], int):
                        if d[name] <= 0:
                            del d[name]
                    else:
                        d[name]["remaining"] -= 1
                        if d[name]["remaining"] <= 0:
                            # 简单归还：如果是 stolen 则移回 state.relics（若 from player）
                            info = d.pop(name)
                            if attr == "stolen_relics" and info.get("target_is_player", False):
                                relic_obj = next((r for r in entity.relics if r.name == name), None) \
                                    if hasattr(entity, "relics") else None
                                if relic_obj is not None and relic_obj in entity.relics:
                                    entity.relics.remove(relic_obj)
                                if relic_obj is not None:
                                    self.state.relics.append(relic_obj)
                                returned.append(name)
        return {"expired_seals": expired_seals, "returned_steals": returned}

    def _xijie_steal(self, caster: Entity, target: Entity, damage_amount: int) -> int:
        """洗劫X：造成伤害时夺取[目标]等量[碎片]（假碎片优先由目标侧扣减；夺取量=min(目标碎片,伤害)）"""
        if damage_amount <= 0 or target is caster or not caster.has_status("洗劫"):
            return 0
        avail = self._shards_of(target)
        if avail <= 0:
            return 0  # 若[目标]没有[碎片]则夺取无效
        steal = min(avail, damage_amount)
        self._lose_shards_of(target, steal)
        if caster is self.state.player:
            self.state.shards += steal
        else:
            caster.shards += steal
        return steal

