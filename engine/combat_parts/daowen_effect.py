"""CombatEngine 分片：apply_daowen_effect：道纹效果唯一结算入口（机制总线/事件流/爆裂/触发法术都在其中）

方法体自 engine/combat.py 原样搬来（逐字节），行为与对外契约不变；
CombatEngine 仍是唯一入口（门面类继承本 Mixin）。
"""
from __future__ import annotations

import math
import weakref
import uuid
from typing import Optional, Any
from ..models import Entity, StatusEffect, GameState, DaoWenInstance, DaoWen, Spell, Consumable
from ..daowen import DaoWenEngine, ResonanceEngine
from ..dice import DiceEngine
from ..enums import (ActionPhase, TriggerTiming, InterruptType, DamageType,
                     EffectScope, EffectPolarity, CostType)
from ..dm_rulings import Interrupt
from ..combat_events import CombatEvent, CombatEventType, register_combat_event_observer
from ..combat_hooks import CombatHookManager
from ..effect_context import EffectContext, make_context, normalize_context
from ..mechanisms import MECHANISMS, Phase, TriggerBus, TriggerContext
from ..personality import remove_personality
from ..resolution import KIND_EFFECT, resolution_frame
from ..models import MONSTER_MANA_RELIC
from ..mechanisms.verbs import apply_verb


class DaowenEffectMixin:
    def _settle_diandang_discovery(self, caster, kinds: int, result: dict) -> None:
        """【典当X】发现获得X种道纹（2026-10-13）。

        发现池 = 局外【学习】口径（gamedata.learnable_daowen_pool），逐种抽取、
        每种3选1（骰子流名可复现）。调用时失忆代价已结算（当掉的道纹可重新被发现）。
        轮回者→pending 待选（choose_diandang_daowen 逐组领取）；非轮回者
        （怪物经【原初】借用发动，没有发现界面）→即时随机授予。
        授予一律受【承载上限】约束：额度已满则剩余种数不再产出（残韵超额不授予同口径）。
        """
        from ..gamedata import learnable_daowen_pool
        kinds = max(0, int(kinds))
        if kinds <= 0:
            return
        seq = int(self.state.event_modifiers.get("diandang_cast_seq", 0)) + 1
        self.state.event_modifiers["diandang_cast_seq"] = seq
        pending_now: list[list[str]] = []
        granted_now: list[str] = []
        for kind in range(kinds):
            pool = learnable_daowen_pool(
                owned=set(caster.dao_wen),
                region=self.state.current_region,
                known=DaoWenEngine.list_all())
            if not pool:
                break
            if len(caster.dao_wen) >= caster.daowen_capacity:
                result["effects"].append({"type": "diandang_capped", "caster": caster.name})
                break
            remaining = list(pool)
            group: list[str] = []
            for slot in range(min(3, len(remaining))):
                roll = self.dice.auto_roll(
                    f"diandang_b{self.state.current_battle}_r{self.state.current_round}"
                    f"_c{seq}_k{kind + 1}_{slot + 1}",
                    remaining,
                    context=f"【典当】{caster.name}发现第{kind + 1}种道纹候选{slot + 1}",
                )
                group.append(remaining.pop(roll["record"]["selected_index"]))
            if caster is self.state.player:
                self.state.pending_diandang_discoveries.append(group)
                pending_now.append(group)
            else:
                pick = group[0]
                caster.dao_wen[pick] = DaoWenInstance(DaoWen(
                    name=pick, formula="", cost_type="消耗",
                    cost_formula="X", effect_formula=""))
                granted_now.append(pick)
        if pending_now:
            result["effects"].append({"type": "diandang_pending", "caster": caster.name,
                                      "groups": pending_now})
        if granted_now:
            result["effects"].append({"type": "diandang_granted", "caster": caster.name,
                                      "granted": granted_now})

    def prepare_huixiang(self, calc: dict, caster: Entity, target: Entity,
                         choices: Optional[dict] = None, refs: Optional[dict] = None) -> None:
        """在支付前锁定每个受影响者的一种持续状态；歧义不得偷偷选择第一项。

        status_index 是目标公开状态列表的零基索引；波及各目标可在
        status_indices 中按稳定 target_ref 分别指定。只有唯一有效状态时才省略选择。
        对象身份只在本次结算内使用，不作为存档或持续状态字段。
        """
        choices = choices or {}
        refs = refs if refs is not None else self._combat_entity_refs()
        indices = choices.get("status_indices", {})
        if not isinstance(indices, dict) or any(ref not in refs for ref in indices):
            raise ValueError("回响status_indices必须按当前合法target_ref指定状态索引")
        explicit = {refs[ref].runtime_id: index for ref, index in indices.items()}
        primary_index = choices.get("status_index", calc.get("status_index"))
        recipients = [target]
        waves = self._wave_targets(caster)
        if waves:
            recipients = ([target] if target is not caster and target.is_alive else []) + waves
        selected = {}
        for recipient in recipients:
            if recipient is None or not recipient.is_alive:
                raise ValueError("回响需要存活目标")
            if recipient.runtime_id in selected:
                continue
            index = explicit.get(recipient.runtime_id,
                                 primary_index if recipient is target else None)
            if index is None:
                candidates = [i for i, status in enumerate(recipient.status_effects)
                              if not status.is_expired]
                if len(candidates) != 1:
                    raise ValueError(f"{recipient.name}有{len(candidates)}种有效持续状态，请明确status_index")
                index = candidates[0]
            if (isinstance(index, bool) or not isinstance(index, int)
                    or not 0 <= index < len(recipient.status_effects)
                    or recipient.status_effects[index].is_expired):
                raise ValueError(f"{recipient.name}的回响status_index不是有效持续状态")
            status = recipient.status_effects[index]
            token = getattr(status, "_huixiang_token", None)
            if token is None:
                token = uuid.uuid4().hex
                status._huixiang_token = token
            selected[recipient.runtime_id] = token
        calc["huixiang_selected"] = selected

    @staticmethod
    def _daowen_status(*, daowen_order: int, **kwargs) -> StatusEffect:
        """道纹施加持续状态的唯一构造口：记录来源道纹在序列中的位置。"""
        return StatusEffect(daowen_order=daowen_order, **kwargs)

    def apply_daowen_effect(
        self, name: str, calc: dict, caster: Entity, target: Entity,
        dragon_heart_use: int = 0, *, cost_share_target_ref: str = "",
        aoe_targets_override: Optional[list[Entity]] = None,
    ) -> dict:
        """道纹效果结算的**唯一公开入口**（规则层只认这一个名字）。

        包装层只做一件事：开一个结算帧，让这次道纹结算成为链上可追踪的一段。
        实现体见 `_apply_daowen_effect_impl`（逐字节未改）。
        """
        with resolution_frame(self, KIND_EFFECT, "道纹", name,
                              getattr(caster, "name", "?"),
                              "->", getattr(target, "name", "?")):
            return self._apply_daowen_effect_impl(
                name, calc, caster, target, dragon_heart_use,
                cost_share_target_ref=cost_share_target_ref,
                aoe_targets_override=aoe_targets_override)

    def _apply_daowen_effect_impl(
        self, name: str, calc: dict, caster: Entity, target: Entity,
        dragon_heart_use: int = 0, *, cost_share_target_ref: str = "",
        aoe_targets_override: Optional[list[Entity]] = None,
    ) -> dict:
        """应用道纹效果；aoe_targets_override用于绑定两阶段决策的目标快照。"""
        if name == "净化":
            kinds = set(calc.get("cost_reductions", {}))
            if len(kinds) != 1 or not kinds <= {"衰老", "枯竭", "萎缩", "疲惫", "异变"}:
                raise ValueError("净化仅允许减少一种数值类代价，不支持失忆或冷却")
        if name == "回响" and "huixiang_selected" not in calc:
            self.prepare_huixiang(calc, caster, target)
        result = {"daowen": name, "effects": []}
        # 永恒的偏爱只延长该持有者本次发动后新增/刷新的持续状态，
        # 不会把其他角色施加给他的负面状态错误地永久化。
        preference_entities = list({
            entity.runtime_id: entity
            for entity in (self.state.get_all_player_side() + self.state.get_all_enemy_side()
                           + list(getattr(self.state, "temp_friends", []) or []))
        }.values())
        preference_status_before = {
            entity.runtime_id: {
                id(status): status.remaining_rounds for status in entity.status_effects
            } for entity in preference_entities
        }
        # 衰变沙漏订阅道纹发动结算：全场每次发动的本次效果数值-1，最低为0。
        # 重新按减1后的X解析本次效果字段，避免只改 calc["x"] 却漏掉已预计算的伤害/回复等数值。
        # 代价已在前置阶段按原声明支付，因此保留原 calc 中所有 cost* 字段，不改变代价规则。
        if self._any_active_relic("衰变沙漏"):
            original_calc = dict(calc)
            original_x = max(0, int(original_calc.get("x", 0) or 0))
            reduced_x = max(0, original_x - 1)
            if reduced_x != original_x:
                try:
                    reduced_calc = DaoWenEngine.resolve(
                        name, reduced_x, y=original_calc.get("y", 1),
                        target=target, caster=caster, _skip_status_decay=True)
                except (TypeError, ValueError, KeyError):
                    reduced_calc = {}
                if reduced_calc:
                    calc = dict(original_calc)
                    for key, value in reduced_calc.items():
                        if not str(key).startswith("cost"):
                            calc[key] = value
                else:
                    calc = dict(original_calc)
                calc["x"] = reduced_x
        x = calc.get("x", 0)
        # Entity.dao_wen 是角色当前道纹序列的有序映射；状态继承发动道纹的位置，
        # 后续结算不再从 X 值、状态施加时间或刷新时间推断顺序。
        daowen_sequence = list(caster.dao_wen)
        daowen_order = daowen_sequence.index(name) if name in caster.dao_wen else len(daowen_sequence)
        if name == "固执":
            target = caster
        # 本次道纹结算的根上下文。由它派生的血限/生命/命零变化都以此为父事件，
        # 这样「杀伐 → 伤害 → 血限下降 → 命零」在链上是连续的。
        daowen_ctx = make_context(
            timing=self._current_context_timing(),
            source=name, source_type="daowen",
            actor=caster, target=target, owner=caster,
            mechanic="daowen_resolution", subtype=name, amount=x,
            tags={"daowen"},
        )
        result["daowen_ctx"] = daowen_ctx.to_dict()

        # ---- 波及X（2026-08-21）：你发动的道纹同时作用于所有拥有波及效果的目标 ----
        # 数值型效果的总数值在所有目标（本次[目标]+波及目标，均排除施法者自身）间平分，
        # 余数随机分配；状态类效果对波及目标原样生效。多目标不复制或增加总数值。
        # 目标可选的道纹（如【变形】）未指定目标时兜底为施法者，避免 [None]
        # 2026-10-09 用户令：波及 X≥2 下限 repealed——effective 非空即走分路，
        # 不再有「不足 2 个不分」的特判：平分份数=目标数，单目标时 _divide_flat(v,1)=[v]，
        # 全值落于该目标，与多目标完全同一条链路。
        wave_status_targets: list[Entity] = [target if target else caster]
        wave_pieces: dict[str, list[int]] = {}
        has_wave_targets = False
        if name != "波及":
            wave_targets = self._wave_targets(caster)
            if wave_targets:
                has_wave_targets = True
                effective: list[Entity] = []
                # 波及：本次直接目标与所有波及目标共同分担数值效果；
                # 状态类效果则分别作用于这些目标，不把施法者计入目标组。
                for wt in ([target] if target is not caster and target.is_alive else []) + wave_targets:
                    if wt.is_alive and wt not in effective:
                        effective.append(wt)
                if effective:
                    wave_status_targets = effective
                    numeric_keys = [k for k in self.WAVE_NUMERIC_KEYS if k in calc]
                    if numeric_keys:
                        wave_pieces = {k: self._divide_flat(calc[k], len(effective))
                                       for k in numeric_keys}
                        result["wave_spread"] = {
                            "targets": [e.name for e in effective],
                            "pieces": {k: list(v) for k, v in wave_pieces.items()},
                        }
                    else:
                        result["wave_spread"] = {
                            "targets": [e.name for e in effective], "status_only": True}

        # 【冷却X】代价：规则正文「冷却X：使用后该道纹记为【X(0)/Y】，[战终]后已完成
        # 战斗场数+1，达到Y时才能再次使用」。此前从未写入 cooldown_remaining，
        # 导致 固执/束缚/畸变/全速 可在同一场里无限重复发动（束缚因此支配全局）。
        if calc.get("cost_type") == "冷却":
            inst = caster.dao_wen.get(name)
            if inst is not None:
                cooldown = max(0, int(calc.get("cost", 0)))
                if "时间的偏爱" in getattr(caster, "preferences", []):
                    inst.cooldown_rounds_remaining = max(
                        inst.cooldown_rounds_remaining, cooldown)
                    result["cooldown_rounds_set"] = inst.cooldown_rounds_remaining
                else:
                    inst.cooldown_remaining = max(inst.cooldown_remaining, cooldown)
                    result["cooldown_set"] = inst.cooldown_remaining

        # 【唯一】代价：规则正文「唯一：使用后，本次轮回中无法再次使用」。
        # 此前该代价种类只有一行名字映射、没有任何结算逻辑，等于空定义；
        # 现在与【冷却X】同处落账——唯一是轮回级一次性，跨战斗场数不恢复。
        if calc.get("cost_type") == "唯一":
            inst = caster.dao_wen.get(name)
            if inst is not None:
                inst.spent_unique = True
                result["unique_spent"] = True

        # 蒙蔽(施法者伤害类道纹归零) / 坏死(目标禁疗)
        mengbi_blocked = caster.has_status("蒙蔽") and ("target_damage" in calc or "aoe_damage" in calc)
        if mengbi_blocked:
            for s in caster.status_effects:
                if s.name == "蒙蔽" and s.value > 0:
                    s.value -= 1
                    if s.value <= 0: caster.status_effects.remove(s)
                    break
            result["mengbi_blocked"] = True
        huaisi_block = self._heal_blocked(target) and "target_heal" in calc

        # ---- 逆鳞加成（F2）：施法者若有层数，下次伤害+层数后清空 ----
        nilin_bonus = 0
        if hasattr(caster, "_nilin") and getattr(caster, "_nilin", 0) > 0 and any(k in calc for k in ("target_damage", "total_damage", "aoe_damage", "hp_percent_loss")):
            nilin_bonus = caster._nilin
            caster._nilin = 0
            result["nilin_bonus"] = nilin_bonus
            # 状态层数虽清空，但 status 本身仍按 duration 存在（仅清空计数）

        # ---- 伤害类 ----
        if "target_damage" in calc:
            base = calc["target_damage"] + (nilin_bonus if nilin_bonus else 0)
            dmg_amount = 0 if mengbi_blocked else base
            if "target_damage" in wave_pieces:
                # 波及：修正后总数值平分（余数随机分配），逆鳞已计入首段总值。
                pieces = self._divide_flat(dmg_amount, len(wave_status_targets))
                wave_pieces["target_damage"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    piece = 0 if mengbi_blocked else piece
                    dmg = self._apply_hostile_damage(
                        wt, piece, source=caster,
                        ctx={"timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                             "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                             "mechanic": "damage", "subtype": "daowen", "amount": piece,
                             "tags": {"daowen", "wave"}})
                    result["effects"].append({"type": "damage", "target": wt.name, **dmg})
                    if dmg.get("actual_damage", 0) > 0:
                        caster.damage_dealt_this_round += dmg["actual_damage"]
                nilin_bonus = 0
            else:
                dmg = self._apply_hostile_damage(
                    target, dmg_amount, source=caster,
                    ctx={"timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                         "source": name, "source_type": "daowen", "actor": caster, "target": target,
                         "mechanic": "damage", "subtype": "daowen", "amount": dmg_amount,
                         "tags": {"daowen"}})
                result["effects"].append({"type": "damage", "target": target.name, **dmg})
                if dmg.get("actual_damage", 0) > 0:
                    caster.damage_dealt_this_round += dmg["actual_damage"]
        if name == "连击" or ("hits" in calc and calc.get("damage_per_hit") == 1 and "target_damage" not in calc):
            hits = calc.get("hits", 1)
            if "hits" in wave_pieces:
                # 波及：总命中次数平分，每个目标的份额每次1点伤害。
                for wt, piece in zip(wave_status_targets, wave_pieces["hits"]):
                    total_act = 0
                    total_abs = 0
                    for _ in range(piece):
                        if not wt.is_alive:
                            break
                        hit_amount = 0 if mengbi_blocked else 1
                        dmg_i = self._apply_hostile_damage(
                            wt, hit_amount, source=caster,
                            ctx={"timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                                 "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                                 "mechanic": "damage", "subtype": "daowen_hit", "amount": hit_amount,
                                 "tags": {"daowen", "multi_hit", "wave"}})
                        total_act += dmg_i.get("actual_damage", 0)
                        total_abs += dmg_i.get("shield_absorbed", 0)
                    dmg = {"raw_damage": piece, "actual_damage": total_act, "shield_absorbed": total_abs,
                           "hp_after": wt.current_hp, "died": not wt.is_alive}
                    result["effects"].append({"type": "damage", "target": wt.name, **dmg})
                    if total_act > 0:
                        caster.damage_dealt_this_round += total_act
            else:
                total_act = 0
                total_abs = 0
                for _ in range(hits):
                    if not target.is_alive:
                        break
                    hit_amount = 0 if mengbi_blocked else 1
                    dmg_i = self._apply_hostile_damage(
                        target, hit_amount, source=caster,
                        ctx={"timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                             "source": name, "source_type": "daowen", "actor": caster, "target": target,
                             "mechanic": "damage", "subtype": "daowen_hit", "amount": hit_amount,
                             "tags": {"daowen", "multi_hit"}})
                    total_act += dmg_i.get("actual_damage", 0)
                    total_abs += dmg_i.get("shield_absorbed", 0)
                dmg = {"raw_damage": hits, "actual_damage": total_act, "shield_absorbed": total_abs,
                       "hp_after": target.current_hp, "died": not target.is_alive}
                result["effects"].append({"type": "damage", "target": target.name, **dmg})
                if total_act > 0:
                    caster.damage_dealt_this_round += total_act
        elif "total_damage" in calc and "target_damage" not in calc:  # 其他多段
            add = nilin_bonus
            nilin_bonus = 0
            chunk = calc["total_damage"] + add
            dmg_amount = 0 if mengbi_blocked else chunk
            if "total_damage" in wave_pieces:
                pieces = self._divide_flat(dmg_amount, len(wave_status_targets))
                wave_pieces["total_damage"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    piece = 0 if mengbi_blocked else piece
                    dmg = self._apply_hostile_damage(wt, piece, source=caster, ctx={
                        "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                        "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                        "mechanic": "damage", "subtype": "daowen", "amount": piece,
                        "tags": {"daowen", "wave"},
                    })
                    if add:
                        dmg["nilin_bonus"] = add
                        add = 0
                    result["effects"].append({"type": "damage", "target": wt.name, **dmg})
                    if dmg.get("actual_damage", 0) > 0:
                        caster.damage_dealt_this_round += dmg["actual_damage"]
            else:
                dmg = self._apply_hostile_damage(target, dmg_amount, source=caster, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": target,
                    "mechanic": "damage", "subtype": "daowen", "amount": dmg_amount, "tags": {"daowen"},
                })
                if add:
                    dmg["nilin_bonus"] = add
                result["effects"].append({"type": "damage", "target": target.name, **dmg})
                if dmg.get("actual_damage", 0) > 0:
                    caster.damage_dealt_this_round += dmg["actual_damage"]

        if "aoe_damage" in calc:
            a = 0 if mengbi_blocked else calc["aoe_damage"]
            # 逆鳞加成仅作用于首个目标的首段伤害
            if nilin_bonus:
                a += nilin_bonus
                result["nilin_bonus"] = nilin_bonus
                nilin_bonus = 0
            if aoe_targets_override is not None:
                # 两阶段怪物决策必须只结算prepare时列出的目标，且已闪避目标已由调用方剔除。
                aoe_targets = [e for e in aoe_targets_override if e.is_alive]
            elif caster in self.state.get_all_player_side():
                aoe_targets = self.state.get_all_enemy_side()
            else:
                aoe_targets = self.state.get_all_player_side()
            for enemy in aoe_targets:
                # 对首个敌人附加剩余加成（若前未消耗）
                dmg_a = a
                if nilin_bonus and enemy is aoe_targets[0]:
                    dmg_a += nilin_bonus
                    nilin_bonus = 0
                dmg = self._apply_hostile_damage(enemy, dmg_a, source=caster, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": enemy,
                    "mechanic": "damage", "subtype": "aoe_daowen", "amount": dmg_a, "tags": {"daowen", "aoe"},
                })
                result["effects"].append({"type": "aoe_damage", "target": enemy.name, **dmg})
                if dmg.get("actual_damage", 0) > 0:
                    caster.damage_dealt_this_round += dmg["actual_damage"]
        if "hp_percent_loss" in calc:
            if "hp_percent_loss" in wave_pieces:
                # 波及：总数值=各目标按当前生命×百分比之和，再平分。
                total = sum(math.ceil(wt.current_hp * calc["hp_percent_loss"] / 100)
                            for wt in wave_status_targets) + (nilin_bonus if nilin_bonus else 0)
                if nilin_bonus:
                    result["nilin_bonus"] = nilin_bonus
                    nilin_bonus = 0
                pieces = self._divide_flat(total, len(wave_status_targets))
                wave_pieces["hp_percent_loss"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    dmg = self._apply_hostile_damage(wt, piece, source=caster, ctx={
                        "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                        "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                        "mechanic": "damage", "subtype": "percent", "amount": piece,
                        "tags": {"daowen", "percent", "wave"},
                    })
                    result["effects"].append({"type": "pct_damage", "target": wt.name, **dmg})
                    if dmg.get("actual_damage", 0) > 0:
                        caster.damage_dealt_this_round += dmg["actual_damage"]
            else:
                d = math.ceil(target.current_hp * calc["hp_percent_loss"] / 100) + (nilin_bonus if nilin_bonus else 0)
                if nilin_bonus:
                    result["nilin_bonus"] = nilin_bonus
                    nilin_bonus = 0
                dmg = self._apply_hostile_damage(target, d, source=caster, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": target,
                    "mechanic": "damage", "subtype": "percent", "amount": d, "tags": {"daowen", "percent"},
                })
                result["effects"].append({"type": "pct_damage", "target": target.name, **dmg})
                if dmg.get("actual_damage", 0) > 0:
                    caster.damage_dealt_this_round += dmg["actual_damage"]

        # ---- 再生的攻击力代价：发动者失去攻击力，随后按正常目标结算回复 ----
        if calc.get("cost_type") == "攻击力" and calc.get("cost_attack", 0) > 0:
            amount = int(calc["cost_attack"])
            available = max(0, int(caster.effective_attack_power()))
            if amount > available:
                raise ValueError(f"再生攻击力代价需要{amount}点，当前有效攻击力仅{available}")
            self._battle_delta(caster, "attack_power", -amount, name, EffectPolarity.DEBUFF.value)
            result["effects"].append({"type": "attack_power_cost", "target": caster.name,
                                      "paid": amount, "attack_power": caster.attack_power})
        # ---- 回复类 ----
        if "target_heal" in calc:
            if "target_heal" in wave_pieces:
                pieces = self._divide_flat(calc["target_heal"], len(wave_status_targets))
                wave_pieces["target_heal"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    if self._heal_blocked(wt):
                        result["effects"].append(
                            {"type": "heal", "target": wt.name, "blocked_by": "坏死"})
                        continue
                    result["effects"].append({"type": "heal", "target": wt.name, **self.state.apply_heal(wt, piece, ctx={
                        "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                        "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                        "owner": caster, "mechanic": "heal", "subtype": "daowen", "amount": piece,
                        "tags": {"daowen", "wave"},
                    })})
            elif not huaisi_block:
                heal_amount = calc["target_heal"]
                result["effects"].append({"type": "heal", "target": target.name, **self.state.apply_heal(target, heal_amount, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": target,
                    "owner": caster, "mechanic": "heal", "subtype": "daowen", "amount": heal_amount,
                    "tags": {"daowen"},
                })})
        if "heal_percent" in calc:
            if "heal_percent" in wave_pieces:
                # 波及：总数值=各目标按血限×百分比之和，再平分。
                total = sum(math.ceil(wt.blood_limit * calc["heal_percent"] / 100)
                            for wt in wave_status_targets)
                pieces = self._divide_flat(total, len(wave_status_targets))
                wave_pieces["heal_percent"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    if self._heal_blocked(wt):
                        result["effects"].append(
                            {"type": "heal_pct", "target": wt.name, "blocked_by": "坏死"})
                        continue
                    result["effects"].append({"type": "heal_pct", "target": wt.name, **self.state.apply_heal(wt, piece, ctx={
                        "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                        "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                        "owner": caster, "mechanic": "heal", "subtype": "daowen_pct", "amount": piece,
                        "tags": {"daowen", "wave"},
                    })})
            elif not self._heal_blocked(target):
                h = math.ceil(target.blood_limit * calc["heal_percent"] / 100)
                result["effects"].append({"type": "heal_pct", "target": target.name, **self.state.apply_heal(target, h, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": target,
                    "owner": caster, "mechanic": "heal", "subtype": "daowen_pct", "amount": h,
                    "tags": {"daowen"},
                })})
        if name == "净化" and calc.get("cost_reductions"):
            # 只逆转已承担的数值类代价；不返还失忆道纹，不改变冷却。
            reductions = calc["cost_reductions"]
            for wt in wave_status_targets:
                if not wt.is_alive:
                    continue
                reductions = dict(calc["cost_reductions"])
                ledger = getattr(wt, "reversible_cost_layers", {})
                applied = {}
                for kind in list(reductions):
                    owned = wt.mutation_count if kind == "异变" else ledger.get(kind, 0)
                    reductions[kind] = min(reductions[kind], max(0, owned))
                    if kind != "异变":
                        ledger[kind] = max(0, owned - reductions[kind])
                if reductions.get("衰老", 0) > 0:
                    amount = int(reductions["衰老"])
                    wt.blood_limit += amount
                    applied["衰老"] = amount
                if reductions.get("枯竭", 0) > 0:
                    amount = int(reductions["枯竭"])
                    wt.mana_limit += amount
                    applied["枯竭"] = amount
                if reductions.get("萎缩", 0) > 0:
                    amount = int(reductions["萎缩"])
                    wt.speed_limit += amount
                    applied["萎缩"] = amount
                if reductions.get("疲惫", 0) > 0:
                    amount = int(reductions["疲惫"])
                    wt.current_speed = min(wt.speed_limit, wt.current_speed + amount)
                    applied["疲惫"] = amount
                if reductions.get("异变", 0) > 0:
                    amount = int(reductions["异变"])
                    pay = wt.add_mutation(-amount)
                    applied["异变"] = {"requested": amount, "mutation_total": pay["mutation_total"]}
                    if wt.entity_type == "怪物":
                        redemption = self.check_redemption(wt)
                        if redemption:
                            result["effects"].append(redemption)
                result["effects"].append({"type": "cost_purified", "target": wt.name,
                                          "requested": reductions, "applied": applied})
        if "mutation_reduction" in calc:
            if "mutation_reduction" in wave_pieces:
                pieces = self._divide_flat(calc["mutation_reduction"], len(wave_status_targets))
                wave_pieces["mutation_reduction"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    pay = wt.add_mutation(-piece)
                    result["effects"].append({
                        "type": "mutation_reduction", "target": wt.name,
                        "reduced": piece, "mutation_total": pay["mutation_total"],
                    })
                    if wt.entity_type == "怪物":
                        redemption = self.check_redemption(wt)
                        if redemption:
                            result["effects"].append(redemption)
            else:
                reduced = int(calc["mutation_reduction"])
                pay = target.add_mutation(-reduced)
                result["effects"].append({
                    "type": "mutation_reduction", "target": target.name,
                    "reduced": reduced, "mutation_total": pay["mutation_total"],
                })
                if target.entity_type == "怪物":
                    redemption = self.check_redemption(target)
                    if redemption:
                        result["effects"].append(redemption)

        if "target_heal" in calc or "heal_percent" in calc:
            for cancer_target in wave_status_targets:
                cancer = self.check_cancer(cancer_target)
                if cancer:
                    result["effects"].append(cancer)

        # ---- 格挡/血限 ----
        if "target_shield" in calc:
            if "target_shield" in wave_pieces:
                pieces = self._divide_flat(calc["target_shield"], len(wave_status_targets))
                wave_pieces["target_shield"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    wt.gain_shield(piece)
                    result["effects"].append({"type": "shield", "target": wt.name, "amount": piece})
            else:
                s = calc["target_shield"]; target.gain_shield(s)
                result["effects"].append({"type": "shield", "target": target.name, "amount": s})
        if "shield_drain" in calc:  # 通用格挡扣减
            lost = min(target.shield, calc["shield_drain"]); target.shield -= lost
            result["effects"].append({"type": "shield_drain", "target": target.name, "lost": lost})
        if "blood_limit_reduction" in calc:
            _hp_before = target.current_hp
            # clamp_hp/lethal 关掉：本效果的生命封顶要在 hp_reduction 之后统一做一次。
            _blr = self._apply_blood_limit_change(
                target, -calc["blood_limit_reduction"], name, EffectPolarity.DEBUFF.value,
                ctx=daowen_ctx, source_type="daowen", subtype="blood_limit_reduction",
                actor=caster, owner=caster, clamp_hp=False, lethal=False)
            # 规则正文"[血限]及当前生命同时 -4X"：两者是各自独立的扣减。
            # 此前实现只做 current_hp=min(current_hp, blood_limit)（血限压顶），
            # 对残血目标等于毫无效果。合并成一次写入：既保持与两步扣减相同的终值，
            # 又让 Entity.__setattr__ 的「失去生命后」钩子恰好触发一次。
            if "hp_reduction" in calc:
                # 【第一杯】：这是规则明确写出的「当前生命 -NX」，属于失去生命，
                # 持有者翻倍（血限本身的扣减不翻倍——压顶是封顶后果，不是失血）。
                _target_hp = target.current_hp - calc["hp_reduction"] * self.state.life_loss_multiplier(target)
            else:
                _target_hp = target.current_hp
            self._hp_loss_ctx = daowen_ctx
            # 2026-10-14：【猩红果实】持有者豁免血限压顶（生命可超血限）。
            if self.state.hp_overflow_exempt(target):
                target.current_hp = max(0, _target_hp)
            else:
                target.current_hp = max(0, min(_target_hp, target.blood_limit))
            self._check_hp_zero_death(target, ctx=_blr["ctx"] or daowen_ctx)
            _hp_cut_tmp = _hp_before - target.current_hp
            # 血限压迫导致的当前生命减少，同样属于"使敌对角色生命减少"，
            # 必须计入本回合伤害统计，否则纯压血限流派会被【凡庸】判定为无所作为而自爆。
            _hp_cut = _hp_before - target.current_hp
            if _hp_cut > 0 and target is not caster:
                caster.damage_dealt_this_round += _hp_cut
            result["effects"].append({"type": "blood_limit_reduction", "target": target.name,
                                      "new_blood_limit": target.blood_limit,
                                      "hp_reduced": _hp_cut})
        if "blood_limit_increase" in calc:
            if "blood_limit_increase" in wave_pieces:
                pieces = self._divide_flat(calc["blood_limit_increase"], len(wave_status_targets))
                wave_pieces["blood_limit_increase"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    # 不朽之躯（初拥之夜遗物）：血限无法增加，对该实体的增殖等血限增长一律归零
                    if self.state.side_has(wt, "不朽之躯"):
                        result["effects"].append({"type": "blood_limit_increase", "target": wt.name,
                                                   "increase": 0, "blocked_by": "不朽之躯"})
                        continue
                    increase = self._apply_blood_limit_change(
                        wt, piece, name, EffectPolarity.BUFF.value,
                        ctx=daowen_ctx, source_type="daowen", subtype="blood_limit_increase",
                        actor=caster, owner=caster, clamp_hp=False, lethal=False)["applied"]
                    result["effects"].append({"type": "blood_limit_increase", "target": wt.name,
                                              "increase": increase})
            else:
                # 不朽之躯（初拥之夜遗物）：血限无法增加，对该实体的增殖等血限增长一律归零
                if self.state.side_has(target, "不朽之躯"):
                    result["effects"].append({"type": "blood_limit_increase", "target": target.name,
                                               "increase": 0, "blocked_by": "不朽之躯"})
                else:
                    increase = self._apply_blood_limit_change(
                        target, calc["blood_limit_increase"], name, EffectPolarity.BUFF.value,
                        ctx=daowen_ctx, source_type="daowen", subtype="blood_limit_increase",
                        actor=caster, owner=caster, clamp_hp=False, lethal=False)["applied"]
                    result["effects"].append({"type": "blood_limit_increase", "target": target.name,
                                              "increase": increase})
        # ---- 攻击面板修改 ----
        _panel_keys = ("attack_boost", "attack_reduction", "attack_fixed", "attack_count_fixed")
        # 波及扩散：attack_boost/reduction 数值平分；attack_fixed/attack_count_fixed
        # （固定面板为状态类）对波及目标原样生效。
        # 目标可选的道纹（如【变形】）未指定目标时兜底为施法者，避免 [None]
        # 2026-10-09：单标记也走波及面板（has_wave_targets 即有效目标非空），
        # 不再用「>1」门槛——否则 1 标记时状态类面板会回退打给主目标/施法者，
        # 与多标记"面板落在波及目标上"的语义不一致。
        panel_targets = wave_status_targets if (
            any(k in wave_pieces for k in ("attack_boost", "attack_reduction"))
            or (any(k in calc for k in ("attack_fixed", "attack_count_fixed"))
                and has_wave_targets)) else [target if target else caster]
        for panel_idx, panel_target in enumerate(panel_targets):
            panel_locked = panel_target.has_status("定型") and any(k in calc for k in _panel_keys)
            if panel_locked:
                result["effects"].append({"type": "dingxing_block", "target": panel_target.name})
            if (not panel_locked) and "attack_boost" in calc:
                piece = (wave_pieces.get("attack_boost") or [calc["attack_boost"]])[panel_idx]
                self._battle_delta(
                    panel_target, "attack_power", piece,
                    name, EffectPolarity.BUFF.value)
                result["effects"].append({"type": "attack_boost", "target": panel_target.name,
                                          "attack_power": panel_target.attack_power})
            # 【全力】2026-09-17 用户令重做：攻击力锁定 = [法限]，持续X。
            # 走状态层（models.py::effective_attack_power 读取），不再写遗留字段
            # attack_power——那样对不写穿的轮回者无效。
            if (not panel_locked) and calc.get("attack_power_to_mana_limit"):
                panel_target.add_status(self._daowen_status(daowen_order=daowen_order,
                    name="全力", value=1,
                    remaining_rounds=calc.get("duration", x), source=caster.name))
                result["effects"].append({
                    "type": "attack_power_to_mana_limit", "target": panel_target.name,
                    "attack_power": panel_target.effective_attack_power(),
                    "duration": calc.get("duration", x)})
            if (not panel_locked) and "attack_reduction" in calc:
                amount = (wave_pieces.get("attack_reduction") or [calc["attack_reduction"]])[panel_idx]
                delta = max(0, panel_target.attack_power - amount) - panel_target.attack_power
                self._battle_delta(
                    panel_target, "attack_power", delta, name, EffectPolarity.DEBUFF.value)
                result["effects"].append({"type": "attack_reduction", "target": panel_target.name,
                                          "attack_power": panel_target.attack_power})
            if (not panel_locked) and "attack_fixed" in calc:
                self._battle_delta(
                    panel_target, "attack_power", calc["attack_fixed"] - panel_target.attack_power,
                    name, EffectPolarity.NEUTRAL.value)
                result["effects"].append({"type": "attack_fixed", "target": panel_target.name,
                                          "attack_power": panel_target.attack_power})
            # 【全速】2026-09-17 用户令（原名【迟滞】）：攻击次数锁定 = [速限]，持续X。
            # 走状态层（models.py::effective_attack_count 读取），不再写遗留字段
            # attack_count——那样对不写穿的轮回者无效。
            if (not panel_locked) and calc.get("attack_count_to_speed_limit"):
                panel_target.add_status(self._daowen_status(daowen_order=daowen_order,
                    name="全速", value=1,
                    remaining_rounds=calc.get("duration", x), source=caster.name))
                result["effects"].append({
                    "type": "attack_count_to_speed_limit", "target": panel_target.name,
                    "attack_count": panel_target.effective_attack_count(),
                    "duration": calc.get("duration", x)})
            if (not panel_locked) and "attack_count_fixed" in calc:
                self._battle_delta(
                    panel_target, "attack_count", calc["attack_count_fixed"] - panel_target.attack_count,
                    name, EffectPolarity.NEUTRAL.value)
                result["effects"].append({"type": "attack_count_fixed", "target": panel_target.name,
                                          "attack_count": panel_target.attack_count})
        bianxing_blocked = False
        if name == "变形":
            # 2026-09-17 用户令：变形改为可选目标，不指定时作用于施法者。
            # 【定型】的判定对象随之改为**被变形者**（旧版固定查施法者，
            # 在"目标是别人"的场景下会误判）。
            _bx_target = target if target else caster
            if _bx_target.has_status("定型"):
                bianxing_blocked = True
                result["effects"].append({"type": "dingxing_block", "target": _bx_target.name})
            else:
                # 2026-09-17 用户令重做：改为**[目标]当前速度 ↔ 当前法力互换**，
                # 互换后各自被上限钳制（clamp_immortal_body：当前速度≤[速限]、
                # 当前法力≤[法限]），被钳掉的部分**凭空消失**，不返还。
                #   例：敌方 20/3/10（血限/速度/法力，速限3）→ 互换得 速度10、法力3
                #       → 速度被速限钳回 3 → 结果 20/3/3：目标凭空失去 7 点法力。
                # 旧版「自身攻击力与攻击次数互换」写遗留字段 attack_power/attack_count，
                # 属性模型统一后（攻击力=当前法力、攻击次数=当前速度）对轮回者无效，
                # 且只能对自己用。新版可指定目标，不指定时默认自身。
                swap_target = target if target else caster
                if not hasattr(swap_target, "_bianxing_original"):
                    swap_target._bianxing_original = (swap_target.current_speed,
                                                       swap_target.current_mana)
                swap_target.current_speed, swap_target.current_mana = (
                    swap_target.current_mana, swap_target.current_speed)
                # 互换后立即钳制：超出上限的部分直接蒸发（全局钳制规则）
                self.clamp_immortal_body(swap_target)
                result["effects"].append({"type": "swap", "target": swap_target.name,
                                          "current_speed": swap_target.current_speed,
                                          "current_mana": swap_target.current_mana})

        # ---- 速度修改 ----
        if "speed_boost" in calc:
            if "speed_boost" in wave_pieces:
                pieces = self._divide_flat(calc["speed_boost"], len(wave_status_targets))
                wave_pieces["speed_boost"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    gained = self._gain_speed(wt, piece, ctx={
                        "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                        "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                        "owner": caster, "mechanic": "speed_change", "subtype": "current_speed",
                        "amount": piece, "tags": {"daowen", "wave"},
                    })
                    result["effects"].append({"type": "speed_boost", "target": wt.name,
                                              "speed": wt.current_speed, "gained": gained})
            else:
                gained = self._gain_speed(target, calc["speed_boost"], ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": target,
                    "owner": caster, "mechanic": "speed_change", "subtype": "current_speed",
                    "amount": calc["speed_boost"], "tags": {"daowen"},
                })
                result["effects"].append({"type": "speed_boost", "target": target.name,
                                          "speed": target.current_speed, "gained": gained})
        if "speed_halved" in calc:
            # 速度减半类效果（原【减速】，2026-10-25 遗物化）：对波及目标原样生效。
            for wt in wave_status_targets:
                lost = wt.current_speed - math.ceil(wt.current_speed / 2)
                self._lose_current_speed(wt, lost, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                    "owner": caster, "mechanic": "speed_change", "subtype": "current_speed",
                    "amount": -lost, "tags": {"daowen"},
                })
                result["effects"].append({"type": "speed_halved", "target": wt.name,
                                          "speed": wt.current_speed})
        if "speed_penalty" in calc and (name != "抢劫" or self._shards_of(target) <= 0):
            self._lose_current_speed(target, calc["speed_penalty"], ctx={
                "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                "source": name, "source_type": "daowen", "actor": caster, "target": target,
                "owner": caster, "mechanic": "speed_change", "subtype": "current_speed",
                "amount": -calc["speed_penalty"], "tags": {"daowen"},
            })
            result["effects"].append({"type": "speed_penalty", "target": target.name,
                                      "lost": calc["speed_penalty"], "speed": target.current_speed})

        # 点金的【失忆X】代价：永久失去自身X种道纹，再获得碎片。
        # 当前统一执行接口没有额外的选纹决策槽，按持有顺序优先失去点金以外的道纹；
        # 当前统一执行接口没有额外的选纹决策槽，按持有顺序优先失去发动道纹以外的道纹；
        # 数量不足时发动失败应由上层预检，本处只结算已确认的代价。
        if "forget_daowen" in calc:
            count = int(calc["forget_daowen"])
            owned = list(caster.dao_wen)
            chosen = [n for n in owned if n != name][:count]
            if len(chosen) < count and name in caster.dao_wen:
                chosen.append(name)
            for forgotten in chosen[:count]:
                caster.forget_for_cost(forgotten)
            result["effects"].append({"type": "amnesia", "lost_daowen": chosen[:count]})

        # 【典当】：失忆结算之后，发现获得X种道纹（学习口径池+承载上限）。
        if "discover_daowen" in calc:
            self._settle_diandang_discovery(caster, calc["discover_daowen"], result)

        # ---- 碎片系（罪孽）----
        if "shard_steal" in calc and self._shards_of(target) > 0:
            # 抢劫是“有碎片则夺取，若无碎片才失速”的二选一；不得把不足额偷取扩成负债。
            gained = min(self._shards_of(target), calc["shard_steal"])
            self._lose_shards_of(target, gained)
            if caster is self.state.player:
                self.state.shards += gained
            else:
                caster.shards += gained
            result["effects"].append({"type": "shard_steal", "target": target.name, "gained": gained})
        if "shard_debt" in calc:
            # 2026-10-14【勒索】：与抢劫同价同量形状不同——碎片是销毁（谁都不获得），
            # 不足部分记负债（真碎片扣负，可触发【还债】）。走 shards 动词统一口径
            # （= 旧逼债块字面约定：够则扣，不够则假碎片优先+差额扣负）。
            need = calc["shard_debt"]
            paid = apply_verb(self, "shards", {
                "target": target, "delta": -need,
                "ctx": {"source": "勒索", "source_type": "daowen", "actor": caster,
                        "target": target, "mechanic": "cost", "subtype": "lesuo",
                        "amount": -need, "tags": {"daowen", "shards"}},
            })
            if paid["paid"]:
                result["effects"].append({"type": "lesuo", "target": target.name,
                                          "lost_shards": need})
            else:
                result["effects"].append({"type": "lesuo_debt", "target": target.name,
                                          "obligation": need, "shards_now": paid["shards_now"],
                                          "debt_now": paid["debt"]})
        if "shard_gain" in calc:  # 点金：支付失忆X后获得80X真碎片
            if caster is self.state.player:
                self.state.shards += calc["shard_gain"]
            else:
                caster.shards += calc["shard_gain"]
            result["effects"].append({"type": "shard_gain", "gained": calc["shard_gain"]})
        if "fake_shards" in calc:  # 假钞：获得10X假碎片（假碎片与真碎片分离存储）
            if caster is self.state.player:
                self.state.fake_shards += calc["fake_shards"]
            else:
                caster.fake_shards = getattr(caster, "fake_shards", 0) + calc["fake_shards"]
            result["effects"].append({"type": "fake_shards", "gained": calc["fake_shards"]})
        if "cost_shards" in calc:
            spent = self._lose_shards_of(self.state.player, calc["cost_shards"]) if caster is self.state.player else 0
            result["effects"].append({"type": "cost_shards", "spent": spent})

        # ---- 数值代价：统一走代价总线；【共罪】可按显式引用共同承担 ----
        cost_spec = None
        if "cost_hp" in calc:
            cost_spec = ("流血", calc["cost_hp"], "bleed_cost")
        elif "cost_blood_limit" in calc:
            cost_spec = ("衰老", calc["cost_blood_limit"], "aging_cost")
        elif "cost_mana_limit" in calc:
            # 2026-10-14：【透支】多代价（枯竭通道）。此前没有道纹用枯竭作代价，
            # 本分支是第一条；走代价总线（【苍白之花】豁免/【共罪】平分照常）。
            cost_spec = ("枯竭", calc["cost_mana_limit"], "exhaust_cost")
        elif "cost_speed" in calc:
            cost_spec = ("疲惫", calc["cost_speed"], "fatigue_cost")
        elif "cost_mutation" in calc and caster.entity_type != "怪物":
            # 怪物原始道纹在两阶段怪物流程中已先支付异变5X；此处只补轮回者/同伴的同类代价。
            cost_spec = ("异变", calc["cost_mutation"], "mutation_cost")
        if cost_spec is not None:
            cost_type, amount, effect_type = cost_spec
            timing = "monster_action" if caster.entity_type == "怪物" else "player_action"
            payment = self.pay_numeric_cost(
                caster, cost_type, amount,
                cost_share_target_ref=cost_share_target_ref,
                dragon_heart_use=dragon_heart_use,
                cost_context={"timing": timing, "source": name, "source_type": "daowen", "tags": {"active_payment"}},
            )
            cost_effect = {"type": effect_type, **payment}
            if cost_type == "流血":
                # 保留既有公开字段，同时以owner/shared_with暴露共罪拆分详情。
                cost_effect["actual_damage"] = payment["actual_paid"]
                cost_effect["hp_after"] = caster.current_hp
            result["effects"].append(cost_effect)
        elif cost_share_target_ref:
            raise ValueError("该道纹没有可由【共罪】共同承担的数值代价")
        if "mana_gain" in calc:
            # 2026-10-08：走统一入口，【勾魂】期间不生效（实际获得量可能与声明量不同）
            gained = caster.gain_mana(calc["mana_gain"])
            self.clamp_immortal_body(caster)
            result["effects"].append({"type": "mana_gain", "source": caster.name,
                                      "mana_gained": gained,
                                      "mana_declared": calc["mana_gain"]})

        # ---- 乱葬岗（二阶）专属道纹效果 ----
        # 2026-10-08 用户令删除【镇尸】：与【坏死】是同一效果的两套实现，
        # 保留【坏死】为「无法获得[回复]」的唯一入口。
        if name == "勾魂" and calc.get("no_mana_gain"):
            # 勾魂X：持续X回合**无法获得[法力]**（不扣已有法力）。
            # 2026-10-08 用户令：撤销 2026-09-09 的「法力消耗翻倍」，恢复原效果。
            # 判定只有一处：Entity.can_gain_mana() —— 所有法力增益来源都过 gain_mana()。
            for st_target in wave_status_targets:
                st_target.add_status(self._daowen_status(daowen_order=daowen_order, name="勾魂", value=1,
                                                  remaining_rounds=calc.get("duration", x),
                                                  source=caster.name))
                result["effects"].append({
                    "type": "gouhun", "target": st_target.name,
                    "no_mana_gain": True,
                    "duration": calc.get("duration", x)})
        if name == "冥气" and calc.get("speed_loss_speed_limit"):
            for st_target in wave_status_targets:
                st_target.add_status(self._daowen_status(daowen_order=daowen_order, name="冥气", value=calc["speed_loss_speed_limit"],
                                                  remaining_rounds=calc.get("duration", 1),
                                                  source=caster.name))
                result["effects"].append({"type": "mingqi", "target": st_target.name,
                                          "speed_loss_speed_limit": calc["speed_loss_speed_limit"],
                                          "duration": calc.get("duration", 1)})
        if name == "缄默" and calc.get("silence_death_triggers"):
            for st_target in wave_status_targets:
                st_target.add_status(self._daowen_status(daowen_order=daowen_order, name="缄默", value=1,
                                                  remaining_rounds=calc.get("duration", 1),
                                                  source=caster.name))
                result["effects"].append({"type": "qianmo", "target": st_target.name,
                                          "duration": calc.get("duration", 1)})
        if name == "分裂" and calc.get("split_clones"):
            # 2026-09-17 用户令重做：分裂X/Y 改为**即时**创造 X 个 10Y 血限的
            # 自身复制体（代价衰老＝X×10Y＝造出的总血限）。旧版把创造挂在
            # [命零]上（_pending_split_clones），且血限按本体 20% 浮动——本体
            # 血限越高白赚越多，代价【冷却】又与产出无关，可无限白嫖。
            # 旧版还有 entity_type != "怪物" 的过滤，导致怪物永远不分裂，
            # 与「乱葬岗·分裂」的设计不符；现对任意实体类型一视同仁。
            clones = self._spawn_fenlie_clones(caster, calc["split_clones"],
                                               calc.get("clone_hp", 10))
            result["effects"].append({"type": "fenlie", "clones": len(clones),
                                      "clone_hp": calc.get("clone_hp", 10),
                                      "names": [c.name for c in clones]})
        if name == "招魂" and calc.get("revive_temp_friend"):
            # 唤回1具已击灭的怪物尸体作临时朋友（生命20X）
            dead = [e for e in self.state.dead_monsters if e.entity_type == "怪物"]
            if dead:
                corpse = dead[-1]
                from ..models import Entity
                revived = Entity(name=f"{corpse.name}（魂）", entity_type="临时朋友",
                                 blood_limit=calc["temp_hp"], current_hp=calc["temp_hp"],
                                 attack_count=corpse.attack_count, attack_power=corpse.attack_power)
                for dw_name, dw_inst in corpse.dao_wen.items():
                    revived.dao_wen[dw_name] = dw_inst
                self.state.temp_friends.append(revived)
                result["effects"].append({"type": "zhaohun", "name": revived.name,
                                          "hp": calc["temp_hp"], "corpse": corpse.name})
            else:
                result["effects"].append({"type": "zhaohun", "note": "没有可唤回的怪物尸体"})

        # ---- 特殊 ----
        # 当前没有任何已注册道纹产出 self_attack_count，本分支暂时无人触发。
        # 保留它作为可复用通道（波及平分也已接线），将来若有道纹走
        # "目标自打"效果可直接用。
        if "self_attack_count" in calc:  # 目标自打X次
            if "self_attack_count" in wave_pieces:
                pieces = self._divide_flat(calc["self_attack_count"], len(wave_status_targets))
                wave_pieces["self_attack_count"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    for _ in range(piece):
                        result["effects"].append({"type": "self_attack", "target": wt.name,
                            **self._apply_hostile_damage(wt, wt.attack_power, source=wt, ctx={
                                "timing": "player_action" if caster is self.state.player else "monster_action",
                                "source": name, "source_type": "daowen", "actor": wt, "target": wt,
                                "mechanic": "damage", "subtype": "self_attack", "amount": wt.attack_power,
                                "tags": {"daowen", "self_damage", "wave"},
                            })})
            else:
                for _ in range(calc["self_attack_count"]):
                    result["effects"].append({"type": "self_attack", "target": target.name,
                        **self._apply_hostile_damage(target, target.attack_power, source=target, ctx={
                            "timing": "player_action" if caster is self.state.player else "monster_action",
                            "source": name, "source_type": "daowen", "actor": target, "target": target,
                            "mechanic": "damage", "subtype": "self_attack", "amount": target.attack_power,
                            "tags": {"daowen", "self_damage"},
                        })})
        if calc.get("delay_monster_reentry"):
            # 【封印X】：目标由 use_daowen 的显式 target_ref/target 绑定，进入暂离队列。
            # 暂离不写离场/死亡上下文，也不产生碎片分类。
            #
            # 2026-10-08 用户令：**任意目标**都可以被封印——敌人、[朋友]、[员工]、
            # 临时朋友、敌对轮回者，以及施法者自己（延后 X 回合再入场）。
            # 校验随之放开：只要"还活着、还在场上、没处于暂离中"即可，
            # 不再要求 entity_type == "怪物"，也不再要求属于 enemies。
            # 但明确不允许封印已经永久离场/撤退的单位（那不是"暂离"的对象）。
            if target is None or not target.is_alive:
                raise ValueError("【封印】的目标必须仍存活")
            if target.is_departed or target.has_retreated:
                raise ValueError(f"【封印】的目标{target.name}已永久离场，不是暂离的对象")
            if self.state.is_sealed_away(target):
                raise ValueError(f"【封印】的目标{target.name}已处于暂离中")
            try:
                self.state.seal_home_of(target)
            except ValueError as exc:
                raise ValueError(f"【封印】的目标必须仍在场上：{exc}") from None
            reentry = self._delay_entity_reentry(target, calc.get("delay_rounds", x))
            result["effects"].append({
                "type": "seal", "target": target.name,
                "delay_rounds": reentry["delay_rounds"],
                "return_round": reentry["return_round"],
                "home": reentry["home"],
                "index": reentry["index"],
                "note": f"{target.name}延后{reentry['delay_rounds']}回合，于第{reentry['return_round']}回合始再入场",
            })

        # ---- 旧必中（施法者自身次数型buff）入口已在 2026-09-28 改为自身持续X回合状态：
        #      保留兼容分支，防止老存档调用 calc["guaranteed_hits"]。
        if "guaranteed_hits" in calc:
            self.grant_bizhong(caster, int(calc["guaranteed_hits"]))
            result["effects"].append({"type": "bizhong", "target": caster.name,
                                      "count": calc["guaranteed_hits"],
                                      "remaining": self.bizhong_remaining(caster)})
        # 必中X（2026-10-12 用户令增强，取代 2026-09-28 的 self-buff 口径）：
        # 规则文一直是「你选中的［目标］无法…」，故正式口径是给**目标**挂 debuff，
        # 期间该目标无法进行回合外行动（闪避 / 招架 / 反应型法术），统一由
        # CombatEngine.out_of_turn_blocked() 把关。
        #
        # target=None 时退回 2026-09-28 的 self-buff 老口径（自身姿态，期间你选中的
        # 任何目标都无法闪避）。保留这条分支是为了不打断仍在无目标调用本效果键的
        # 内部路径与历史用例；道纹发动本身按规则文要求指定目标。
        if "bizhong_target_debuff" in calc:
            rounds = int(calc["bizhong_target_debuff"])
            bearer = target if target is not None else caster
            existed = next((s for s in bearer.status_effects if s.name == "必中"), None)
            if existed is not None:
                existed.remaining_rounds = max(existed.remaining_rounds or 0, rounds)
                existed.value = rounds
                # 【必中】历史上是覆盖式刷新而非 add_status 的数值叠加，
                # 但它仍是一轮新的发动：同样采用最后一次施加的 X/来源/顺序。
                existed.refresh_order_from(self._daowen_status(
                    daowen_order=daowen_order, name="必中", remaining_rounds=rounds,
                    value=rounds, source=caster.name))
            else:
                bearer.add_status(self._daowen_status(daowen_order=daowen_order,
                    name="必中", remaining_rounds=rounds, value=rounds,
                    source=caster.name))
            result["effects"].append({
                "type": "bizhong_target" if target is not None else "bizhong_self",
                "target": bearer.name, "rounds": rounds})

        if ("duration" in calc and calc.get("duration") is not None
              and not (name == "变形" and bianxing_blocked)
              # 波及标记由 use_daowen/怪物结算逐目标处理，不走通用状态块
              # 乱葬岗道纹已在上方乱葬岗段自行 add_status，跳过通用状态处理避免重复叠加
              and name not in ("勾魂", "冥气", "缄默", "波及", "共罪")):
            duration = calc["duration"] if calc["duration"] != 0 else -1
            effect_target = target if target else caster
            # 自身作用型道纹（变形/超频等）作用于施法者
            # 【点金】即时结算失忆代价与碎片收益，不挂持续状态。
            # 故从自身作用名单移除；状态【洗劫】本身保留，仍由【帮派令】发放。
            # 2026-09-17 用户令：【超频】改为自由选择目标（选到谁给谁加速），不再属于
            # "自身作用型"。它原本留在本名单里也无效——其 calc 无 duration 键，
            # 进不了本状态块，实际效果一直是下方数值段给 target 加速。
            # 2026-09-17 用户令：【变形】改为可自由选择目标（不指定时默认自身），
            # 故移出"自身作用型"名单，状态随之挂到目标身上（到期还原也落在目标）。
            self_targeted = name in ("飞行", "必中", "固执", "贯穿")
            status_name = "无限血限" if calc.get("infinite_blood_limit") else name
            if self_targeted:
                et = caster
                et.add_status(self._daowen_status(daowen_order=daowen_order, name=status_name, remaining_rounds=duration, value=x, source=caster.name))
                result["effects"].append({"type": "status_added", "target": et.name,
                                          "status": status_name, "duration": duration, "value": x})
            else:
                # 波及：状态类效果对每个拥有波及效果的目标（含本次[目标]）原样生效。
                for et in wave_status_targets:
                    if not et.is_alive:
                        continue
                    et.add_status(self._daowen_status(daowen_order=daowen_order, name=status_name, remaining_rounds=duration, value=x, source=caster.name))
                    result["effects"].append({"type": "status_added", "target": et.name,
                                              "status": status_name, "duration": duration, "value": x})

        if name == "回响":
            amounts = wave_pieces.get("extend_duration", [calc["extend_duration"]])
            for recipient, amount in zip(wave_status_targets, amounts):
                selected_id = calc.get("huixiang_selected", {}).get(recipient.runtime_id)
                status = None if selected_id is None else next(
                    (s for s in recipient.status_effects
                     if getattr(s, "_huixiang_token", None) == selected_id and not s.is_expired), None)
                if status is None:
                    result["effects"].append({"type": "duration_extension_missed",
                                              "target": recipient.name})
                    continue  # 前置反应若移除了原状态，不改投后来新增的其他状态。
                before = status.remaining_rounds
                if not status.is_permanent:
                    status.remaining_rounds += amount
                result["effects"].append({"type": "duration_extended", "target": recipient.name,
                                          "status": status.name, "before": before,
                                          "after": status.remaining_rounds, "amount": amount})

        if name == "大梦" and calc.get("extra_target_rounds"):
            # 大梦：目标额外经历回合，推进目标回合事件、凡庸与自身限时状态。
            for st_target in wave_status_targets:
                if not st_target.is_alive:
                    continue
                advanced = self.advance_target_rounds(st_target, int(calc["extra_target_rounds"]), source="大梦")
                result["effects"].extend(advanced)

        if name == "共罪":
            # 重新发动时以新目标组替换旧组；目标ID写入状态，随战斗快照保存。
            import json
            target_ids = list(calc.get("blood_pact_target_ids") or [str(target.runtime_id)])
            caster.status_effects[:] = [st for st in caster.status_effects if st.name != "共罪"]
            if target_ids:
                caster.add_status(self._daowen_status(
                    daowen_order=daowen_order, name="共罪", value=len(target_ids),
                    remaining_rounds=-1, source=json.dumps(target_ids, ensure_ascii=False)))
            result["effects"].append({"type": "blood_pact", "targets": [
                entity.name for entity in self._blood_pact_group(caster)],
                "duration": "∞"})

        # ---- 旧遗物状态兼容：逆鳞/伤痕；背负道纹已删除 ----
        # （【嫁祸】2026-10-11 已删除，效果与遗物【贪婪契约】重复）
        # 逆鳞X：目标每失去1HP积1层，下次伤害+全部层后清空，持续X（已通过 duration 加状态，此处初始化计数）
        if name == "逆鳞":
            for st_target in wave_status_targets:
                if not hasattr(st_target, "_nilin"):
                    st_target._nilin = 0
                result["effects"].append({"type": "nilin_setup", "target": st_target.name, "x": x})
        # 伤痕X：目标每次掉血后血限-X，永久（已通过 duration 加伤痕状态，此处仅补日志）
        elif name == "伤痕":
            for st_target in wave_status_targets:
                result["effects"].append({"type": "shanghen", "target": st_target.name, "x": x})

        # 抵扣X（旧名）：封印[目标]拥有的一件遗物，持续X
        if name == "抵扣":
            for st_target in wave_status_targets:
                sealed = self._seal_one_relic(st_target, x)
                result["effects"].append({"type": "dikou", "target": st_target.name,
                                          "sealed": sealed or None, "rounds": x})
        # 豪夺X（2026-09-28）：消耗5X碎片，夺取[目标]1件遗物持续X回合（夺取者持有并享受被动）
        elif name == "豪夺":
            for st_target in wave_status_targets:
                stolen = self._steal_one_relic(caster, st_target, x)
                result["effects"].append({"type": "haoduo", "target": st_target.name,
                                          "stolen": stolen or None, "rounds": x})

        # 波及X：目标标记由 use_daowen 的逐目标提交建立/解除；此处只记结果。
        if name == "波及":
            result["effects"].append({"type": "boba_register", "target": target.name, "x": x})

        if "永恒的偏爱" in getattr(caster, "preferences", []):
            for entity in preference_entities:
                before = preference_status_before.get(entity.runtime_id, {})
                for status in entity.status_effects:
                    old_rounds = before.get(id(status))
                    changed_or_added = old_rounds is None or old_rounds != status.remaining_rounds
                    if (changed_or_added and status.source == caster.name
                            and status.remaining_rounds > 0):
                        status.remaining_rounds = -1
                        result["effects"].append({
                            "type": "eternal_preference", "target": entity.name,
                            "status": status.name, "remaining_rounds": "∞",
                        })

        return result



    # 法术流程注册表；计算层只描述步骤，不再替持有者选择X、目标或闪避。
