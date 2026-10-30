"""CombatEngine 分片：怪物回合两阶段：激活记账/准备枚举/静态校验/选择解析/快照与回滚/执行

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
from ..combat_hooks import CombatHookManager
from ..effect_context import EffectContext, make_context, normalize_context
from ..mechanisms import MECHANISMS, Phase, TriggerBus, TriggerContext
from ..resolution import KIND_EFFECT, resolution_frame
from ..personality import remove_personality
from ..models import MONSTER_MANA_RELIC


class MonsterPhaseMixin:
    # ========== 怪物回合（两阶段显式决策） ==========
    # 怪物已激活的道纹 / 已进化的怪物（均按战斗重置）
    _monster_activated: dict = {}
    _monster_evolved: set = set()  # 进化（原初X）：本场已进化的怪物 id 集合
    _monster_daowen_round_used: dict = {}  # 本回合已发动的道纹（DM裁定2026-08-18：跨回合可重复发动）

    def reset_monster_activation(self):
        """战始重置怪物激活状态与战斗遗物状态"""
        # 承露盏记账按**每场**口径归零（换场时不经 [回始]）。
        for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            e.hp_lost_this_battle = 0
            e.chenglu_paid = 0
            e.heavy_shackles_action_penalty = 0
            e.hostile_targeters_this_battle.clear()
        self._monster_activated = {}
        self._monster_evolved = set()  # 进化（原初X）：每场战斗限一次
        self._monster_daowen_round_used = {}
        self._resonance_rewrites = {}
        self._spell_stack = []

    def _monster_round_used(self, monster: Entity) -> set:
        """该怪物本回合已发动的道纹集合（换回合自动清空）。

        DM裁定（2026-08-18）：怪物可在不同回合重复发动同一
        道纹（冷却类由 can_use 管辖），每回合每道纹至多一次。
        2026-09-16 用户令：原「重复发动则 X 累加 +2×副本阶级」的递增机制已废止，
        重复发动按同一 X 计费。
        _monster_activated 保留为持续激活口径，不再作发动门禁。
        """
        rec = self._monster_daowen_round_used.get(id(monster))
        if rec is None or rec[0] != self.state.current_round:
            rec = (self.state.current_round, set())
            self._monster_daowen_round_used[id(monster)] = rec
        return rec[1]
    def consume_resonance_rewrite(self, entity: Entity, source: str) -> Optional[str]:
        bucket = self._resonance_rewrites.get(id(entity)) or {}
        dest = bucket.pop(source, None)
        if dest and not bucket:
            self._resonance_rewrites.pop(id(entity), None)
        return dest

    def _monster_attack_actions(self, m: Entity, activated: set) -> int:
        """怪物攻击出手数 = 1 + 疯狂X(自身状态)。

        疯狂2026-08-17全局裁定：发动方把疯狂状态盖到所有角色，怪物从自身状态读+X。
        发动当回合的状态在resolve阶段才落下，prepare在本回合道纹结算前已快照出手数，
        因此疯狂自下回合生效的时序不变。
        高爆手雷修改的是每轮攻击中的"攻击次数"，不再同时削减攻击出手数。
        2026-10-03：【狂暴】道纹删除，原"+1 出手"加成随之移除。
        """
        # 新旧入口共用同一出手预算：疯狂只修正预算，不再额外生成攻击组。
        return 1 if m.actions_used_this_round < self.single_round_action_count(m) else 0

    def _combat_entity_refs(self) -> dict[str, Entity]:
        """为两阶段决策提供本场稳定的显式目标引用，避免同名实体歧义。

        被【封印】暂离的实体不在场上：既不能当目标（不可被选、不可被打），
        也不能当行动者（不能出手、不能发动道纹）。2026-10-08 起封印可作用于
        任意目标含自己，这条过滤是"暂离"语义的唯一把关点——所有两阶段决策
        （怪物阶段、玩家攻击准备、道纹目标绑定）都从这里取 refs。
        """
        refs: dict[str, Entity] = {}
        if (self.state.player and self.state.player.is_alive
                and not self.state.is_sealed_away(self.state.player)):
            refs["player:0"] = self.state.player
        for prefix, entities in (
            ("friend", self.state.friends),
            ("employee", self.state.employees),
            ("temp_friend", self.state.temp_friends),
            ("enemy", self.state.enemies),
        ):
            for i, entity in enumerate(entities):
                if not entity.is_alive or entity.has_retreated:
                    continue
                if prefix == "employee" and not entity.is_deployed:
                    continue
                if self.state.is_sealed_away(entity):
                    continue  # 【封印】暂离中：不在场上，不可选也不可行动
                self._bind_hp_hook(entity)  # 确认战斗实体已绑定「失去生命后」兜底钩子（幂等）
                refs[f"{prefix}:{i}"] = entity
        return refs

    def _daowen_requires_target(self, name: str) -> bool:
        import inspect
        DaoWenEngine.register_all()
        func = DaoWenEngine._registry.get(name)
        return bool(func and "target" in inspect.signature(func).parameters)

    def prepare_monster_phase(self) -> dict:
        """只枚举合法选择，不决定道纹、目标或闪避，也不改变战斗数值。"""
        refs = self._combat_entity_refs()
        player_refs = [
            {"ref": ref, "name": e.name}
            for ref, e in refs.items()
            if self.state.on_player_side(e)
        ]
        all_targets = [{"ref": ref, "name": e.name} for ref, e in refs.items()]
        # 白板回合已于 2026-09-15 用户令删除：怪物（含[战始]首发、波次增援、[封印]回场）
        # 进场当回合即可发动道纹，不再有"登场回合只能普攻"的限制。
        actors = []
        skipped = []
        for index, monster in enumerate(self.state.enemies):
            if not monster.is_alive or monster.removed_without_kill:
                continue
            actor_ref = f"enemy:{index}"
            if self.chenni_round_blocked(monster):
                skipped.append({"actor_ref": actor_ref, "monster": monster.name,
                                "reason": "沉溺：本次回合已中断"})
                continue
            if not self.can_act(monster):
                skipped.append({"actor_ref": actor_ref, "monster": monster.name, "reason": "无法行动"})
                continue
            if monster.actions_used_this_round >= self.single_round_action_count(monster):
                skipped.append({"actor_ref": actor_ref, "monster": monster.name,
                                "reason": "本回合出手已用完"})
                continue
            activated = self._monster_activated.get(id(monster), set())
            round_used = self._monster_round_used(monster)
            daowen_options = []
            # 2026-09-15 用户令：删除白板限制——增援怪/回场怪进场当回合即可发动道纹
            # （R1 首发怪同理）。spawned_round 仍作为出生回合的记录字段保留，但不再
            # 参与任何"当回合不能发动道纹"的判定。
            # 养蛊场：怪物互斗时，道纹与普攻的合法目标都是「除自己以外的其它存活怪物」
            # （不含玩家侧；正式玩法下该名单为空，走下面的原有分支）。
            # 通用规则：怪物的合法目标＝所有与它阵营不同的参战者。
            # 二元战斗下这正好是玩家侧；养蛊场里每只怪物自成一营，其它怪物自然入列。
            hostile_rivals = [
                target for target in all_targets
                if refs[target["ref"]] is not monster
                and refs[target["ref"]].is_alive
                and self.state.are_hostile(monster, refs[target["ref"]])
                and self.is_targetable(monster, refs[target["ref"]])
            ]
            if not monster.has_status("干扰"):
                for name, inst in monster.dao_wen.items():
                    if (name in round_used or not inst.can_use()
                            or name not in DaoWenEngine.list_all()):
                        continue
                    rewritten_as = (self._resonance_rewrites.get(id(monster)) or {}).get(name)
                    effective_name = rewritten_as or name
                    # 唤潮与蜃景是开放地图专用道纹，不能作为战斗阶段的怪物行动。
                    if effective_name in {"唤潮", "蜃景"}:
                        continue
                    requires_target = self._daowen_requires_target(effective_name)
                    # 通用目标规则：道纹只能落在「自身」或「敌对参战者」身上。
                    # 二元战斗下敌对者就是玩家侧；养蛊场里每只怪物自成一营，
                    # 其它怪物自动成为敌对者。不再有「没有敌对者就退回全表」的兜底。
                    legal_targets = (
                        [target for target in all_targets
                         if refs[target["ref"]] is monster
                         or self.state.are_hostile(monster, refs[target["ref"]])]
                        if requires_target else [])
                    if effective_name == "冰冻":
                        legal_targets = [option for option in legal_targets
                                         if refs[option["ref"]].dao_wen]
                    if "龙威" in self.state.dragon_traits and self.state.player and self.state.player.is_alive:
                        legal_targets = [target for target in legal_targets
                                         if (not self.state.on_player_side(refs[target["ref"]])
                                             or target["ref"] == "player:0")]
                    if requires_target and not legal_targets:
                        continue
                    # 波及X：必须显式提交X个互不重复的合法目标。DM裁定2026-08-23：
                    # 面板X超过当前合法目标数时按目标数**自适应降X**（有效X=
                    # min(面板X, 合法目标数)），与玩家侧 _max_legal_daowen_x 的
                    # 目标数封顶口径一致——永不因目标不足而不可结算/死锁；仅当
                    # 合法目标数为0时本道纹才真正无法发动，prepare才过滤。
                    # （取代2026-08-22 BUG-01的"不足X即过滤"方案：过滤让面板波及
                    # 怪在solo场上1/111场才开得出火，属于非符合预期效果。）
                    dodge_target_options: list[dict] = []
                    if effective_name == "波及":
                        dodge_target_options = [target for target in all_targets
                                                if target["ref"] != actor_ref
                                                and self.is_targetable(monster, refs[target["ref"]])]
                        if not dodge_target_options:
                            continue
                    # 过滤当前付不起数值代价的候选（改写后代价可能超出怪物资源）
                    preview_target = (refs[legal_targets[0]["ref"]] if legal_targets else monster)
                    # 2026-09-16 用户令：面板不写死 X 时，X 由发动方自选，
                    # 「上限只受法限或者代价限制」。这里探出可负担上限，
                    # 连 X=1 都付不起 → 本道纹此刻不可发动，prepare 过滤。
                    # 探测只用 X 取代价类型（冷却与否与 X 无关），X=1 即可
                    # （2026-10-09 用户令：波及 X≥2 下限 repealed，X=1 合法，X_MIN 已拔除）。
                    _probe_x = 1
                    cooldown_limited = (
                        DaoWenEngine.resolve(effective_name, _probe_x,
                                             target=preview_target, caster=monster)
                        .get("cost_type") == "冷却")
                    if cooldown_limited:
                        # 怪物不得把冷却型道纹的 X 调大来延长战斗控制效果。
                        # 2026-10-05 用户令成文（规则正文·怪物准则·共用面板）：怪物使用
                        # 冷却代价类的道纹 X 值只能≤1，一律按 X=1 结算。
                        max_x = 1
                        effective_x = 1
                    elif getattr(inst, "x_free", False):
                        max_x = self._monster_max_daowen_x(
                            monster, effective_name, preview_target,
                            hard_cap=len(dodge_target_options) if effective_name == "波及" else None)
                        # 2026-10-09 用户令：波及 X 下限 repealed，X=1 合法——
                        # 合法目标只有 1 个时波及降 X 到 1 照常给出（单目标全值）。
                        if max_x < 1:
                            continue
                        effective_x = max_x
                    else:
                        if cooldown_limited:
                            effective_x = 1
                        if (name == "消灾" and monster.fake_shards < 50 * inst.x_value
                                and monster.shards < 5 * inst.x_value):
                            continue
                        effective_x = inst.x_value
                    # 【波及】实际标得到的目标数 = min(面板X, 合法目标数)（DM裁定
                    # 2026-08-23 自适应降 X）。2026-10-09 用户令：X 下限 repealed——
                    # 降到 X=1 仍可发动（单目标全值），0 个合法目标时上面
                    # `if not dodge_target_options: continue` 已拦截，故此处无需再过滤。
                    # 面板 x 仍是展示口径，wave_effective_x 才是实际标数。
                    if effective_name in {"流动", "遗忘"}:
                        wave_target_ids = {id(entity) for entity in self._wave_targets(monster)}
                        def _enough_status_options(option):
                            source = refs[option["ref"]]
                            active_count = sum(not status.is_expired
                                               for status in source.status_effects)
                            if active_count < effective_x:
                                return False
                            if effective_name == "遗忘":
                                return True
                            return any(other is not source and id(other) not in wave_target_ids
                                       and other.is_alive
                                       and (other is monster or self.is_targetable(monster, other))
                                       for other in refs.values())
                        legal_targets = [option for option in legal_targets
                                         if _enough_status_options(option)]
                        if not legal_targets:
                            continue
                    preview_calc = DaoWenEngine.resolve(
                        effective_name, effective_x, target=preview_target, caster=monster)
                    if not self._monster_can_pay_calc_cost(monster, preview_calc):
                        continue
                    wave_effective_x = 0
                    if effective_name == "波及":
                        wave_effective_x = min(effective_x, len(dodge_target_options))
                    daowen_options.append({
                        "name": name,
                        "resolves_as": effective_name,
                        # 2026-09-16 用户令：x_free 时面板没有 X，改由发动方在
                        # [1, max_x] 内自选；x 字段保留为上限值以便旧调用方读取。
                        "x": effective_x,
                        "x_free": bool(getattr(inst, "x_free", False)),
                        "max_x": max_x if getattr(inst, "x_free", False) else 0,
                        # 2026-10-09：min_x 字段随 X_MIN 装置一并移除（下限恒为 1）。
                        "wave_effective_x": wave_effective_x,
                        "requires_target": requires_target,
                        "target_options": legal_targets,
                        "dodge_submission": ("per_target" if effective_name == "波及"
                                             else ("single_if_hostile" if requires_target else "none")),
                        "dodge_target_options": dodge_target_options,
                        "trigger_spell_options": self.prepare_daowen_trigger_spells(monster),
                        "extra_choice_schema":self.daowen_extra_choice_schema(monster,effective_name,refs),
                    })
            attack_targets = (list(hostile_rivals) if hostile_rivals else [
                target for target in player_refs
                if self.is_targetable(monster, refs[target["ref"]])
            ])
            # 【龙威】是规则约束而非策略默认：敌方只能把持有者列为合法攻击目标。
            if "龙威" in self.state.dragon_traits and self.state.player and self.state.player.is_alive:
                attack_targets = [target for target in attack_targets if target["ref"] == "player:0"]
            for target_option in attack_targets:
                entity = refs[target_option["ref"]]
                blood_pact_options = self.blood_shadow_cost_share_options(entity)
                # 2026-10-25 用户令：怪物攻击与玩家攻击同一套逐击契约，
                # 目标侧可选防御也照同一套字段暴露（闪避/招架都要付当前速度）。
                target_option["can_dodge"] = entity.current_speed > 0
                target_option["can_parry"] = entity.current_speed > 0
                target_option["can_blood_shadow"] = (
                    self.state.side_has(entity, "血影")
                    and (entity.current_hp > 10 or bool(blood_pact_options)))
                target_option["blood_shadow_cost_share_target_options"] = blood_pact_options
                target_option["spell_options"] = self.prepare_spell_reactions(entity, monster)
                target_option["dodge_relic_target_options"] = [
                    candidate for candidate in all_targets
                    if self.state.on_player_side(refs[candidate["ref"]]) != self.state.on_player_side(entity)
                ] if self.state.side_has(entity, "回锋刀") else []
            # 没有任何合法攻击目标（如solo对手飞行而怪物不飞）→ 本回合不出手。
            # 否则怪物阶段无法被满足：每击都必须引用合法目标，提交永远失败
            # （与【波及】目标数限制同族：prepare不得给出无法满足的义务）。
            base_actions = (0 if not attack_targets
                            else self._monster_attack_actions(monster, activated))
            if base_actions <= 0 and not daowen_options:
                skipped.append({"actor_ref": actor_ref, "monster": monster.name,
                                "reason": "没有可支付的道纹或合法攻击目标"})
                continue
            # 2026-10-25 用户令：怪物攻击并入玩家同一套攻击机制——
            # 击数＝当前速度（[攻击次数]＝[当前速度]，对全体同口径），
            # 每击伤害由攻击者在 damage_budget 内自由分配，目标可逐击闪避或招架。
            hit_count = max(0, monster.effective_attack_count())
            damage_budget = hit_count * monster.effective_attack_power()
            actors.append({
                "actor_ref": actor_ref,
                "monster": monster.name,
                "daowen_required": bool(daowen_options) and not self._monster_free_actions(),
                "free_actions": self._monster_free_actions(),
                "daowen_options": daowen_options,
                "attack_target_options": attack_targets,
                "base_attack_actions": base_actions,
                "hit_count": hit_count,
                "damage_budget": damage_budget,
                "hits_schema": [{
                    "target_ref": [o["ref"] for o in attack_targets],
                    "damage": "非负整数；所有击的总和不得超过damage_budget",
                    "dodge": "boolean", "parry": "boolean", "blood_shadow": "boolean",
                    "dodge_relic_target_ref": "持有回锋刀且闪避时必填",
                    "spell_choices": "按目标spell_options完整提交",
                } for _ in range(hit_count)],
                # 兼容旧键：2026-10-25 起与 hit_count 同值，仅供旧推演稿读取。
                "base_hits_per_attack": hit_count,
                "dodge_must_be_explicit": True,
                # 只暴露实际适用的致死进度；怪物不再显示迷失倒计时。
                "lethal_counters": {k: list(v) for k, v in monster.lethal_counters().items()},
                "lethal_progress": monster.lethal_progress(),
            })
        return {"round": self.state.current_round, "actors": actors, "skipped": skipped}

    def _monster_can_pay_calc_cost(self, caster: Entity, calc: dict) -> bool:
        """怪物候选预检与实际支付共用资源校验；血契额外支付不在此预检。"""
        if caster is None or caster.entity_type != "怪物":
            return True
        for key, resource in (
                ("cost_hp", "生命"), ("cost_blood_limit", "血限"),
                ("cost_mana_limit", "枯竭"), ("cost_mana_cap", "法限"),
                ("cost_speed_limit", "速限"), ("cost_speed", "速度"),
                ("cost_mutation", "异变")):
            amount = int(calc.get(key, 0) or 0)
            if not amount:
                continue
            try:
                self.validate_numeric_cost(caster, resource, amount)
            except ValueError:
                return False
        if calc.get("forget_daowen", 0) > len(getattr(caster, "dao_wen", {}) or {}):
            return False
        if calc.get("cost_type") == "消耗" and calc.get("cost", 0) > 0:
            try:
                self.validate_numeric_cost(caster, "法力", caster.mana_cost(calc["cost"]))
            except ValueError:
                return False
        for key, resource in (("cost_real_shards", "碎片"), ("cost_fake_shards", "假碎片"),
                              ("cost_shards", "碎片")):
            amount = int(calc.get(key, 0) or 0)
            if amount:
                try:
                    self.validate_numeric_cost(caster, resource, amount)
                except ValueError:
                    return False
        return True

    # 2026-09-16 用户令：面板不再写死 X，改由怪物 AI 在发动时自选，
    # 「上限只受法限或者代价限制」。试探上限时逐个 X 递增，取第一个付不起的 X 之前的值。
    # 代价随 X 单调递增（杀伐 X² 之类的超线性亦然），故首次失败即可停止，不必二分。
    _DAOWEN_X_PROBE_CAP = 30

    def _monster_max_daowen_x(self, monster: Entity, effective_name: str, target: Entity,
                              hard_cap: int | None = None) -> int:
        """求该道纹此刻可负担的最大 X。返回 0 表示连 X=1 都付不起（prepare 应过滤掉）。"""
        cap = hard_cap if hard_cap is not None else self._DAOWEN_X_PROBE_CAP
        best = 0
        # 探测从 X=1 起步（2026-10-09 用户令：波及 X≥2 下限 repealed，X=1 合法，
        # X_MIN 装置已整套拔除；上限若连 X=1 都够不到则直接判不可发动）。
        x_floor = 1
        if cap < x_floor:
            return 0
        for x in range(x_floor, max(0, cap) + 1):
            try:
                calc = DaoWenEngine.resolve(effective_name, x, target=target, caster=monster)
            except ValueError:
                # X_LIMITS 之类的硬性上限（如【失忆】X≤当前道纹数量）会在此抛错
                break
            if not self._monster_can_pay_calc_cost(monster, calc):
                break
            # 异变是累加计数；怪物不受迷失生存线约束，仍保留通用试探封顶值。
            # 怪物阶段中的非怪物角色仍不得主动选择会触发迷失的 X。
            if calc.get("cost_type") == "异变" and monster.can_trigger_mutation_lost():
                headroom = (Entity.MUTATION_COLLAPSE_THRESHOLD
                            - getattr(monster, "mutation_count", 0))
                if calc.get("cost_mutation", 0) >= headroom:
                    break
            best = x
        return best

    def _resolve_monster_daowen_choice(
        self, monster: Entity, choice: dict, refs: dict[str, Entity], activated: set,
        prepared_option: dict,
    ) -> dict:
        name = choice.get("name", "")
        inst = monster.dao_wen.get(name)
        if (inst is None or name in self._monster_round_used(monster)
                or not inst.can_use()):
            raise ValueError(f"{monster.name}不能发动道纹【{name}】")
        if name not in DaoWenEngine.list_all():
            raise ValueError(f"未知道纹【{name}】")
        rewritten_as = (self._resonance_rewrites.get(id(monster)) or {}).get(name)
        effective_name = rewritten_as or name
        requires_target = self._daowen_requires_target(effective_name)
        target_ref = choice.get("target_ref", "")
        if requires_target:
            target = refs.get(target_ref)
            if target is None:
                raise ValueError(f"道纹【{effective_name}】必须提交合法target_ref")
            if target is not monster and not self.is_targetable(monster, target):
                raise ValueError(f"目标{target.name}当前不可被{monster.name}选中")
        else:
            if target_ref:
                raise ValueError(f"道纹【{effective_name}】不接受target_ref")
            target = monster

        target, tamper_rule = self.resolve_tampered_target(monster, target, effective_name, choice, refs)

        # 2026-09-16 用户令：面板未写死 X（x_free）时，X 由发动方在提交里自选，
        # 「上限只受法限或者代价限制」——这里按 prepare 同一口径重新探一次上限并校验，
        # 防止提交方给出此刻已付不起的 X（资源在 prepare 之后可能已被消耗）。
        # 探测只用 X 取代价类型（冷却与否与 X 无关），X=1 即可
        # （2026-10-09 用户令：波及 X≥2 下限 repealed，X=1 合法，X_MIN 已拔除，
        #  探测再也不会撞「X=1低于下限」守卫）。
        _probe_x = 1
        cooldown_limited = (
            DaoWenEngine.resolve(effective_name, _probe_x, target=target, caster=monster)
            .get("cost_type") == "冷却")
        if cooldown_limited:
            effective_x = 1  # 怪物冷却代价类道纹 X≤1（规则正文·怪物准则·共用面板）
        elif getattr(inst, "x_free", False):
            submitted_x = choice.get("x")
            # 提交方没有给 X 时**回退到可负担上限**而非报错。
            # sim/ 下有几十处怪物阶段驱动各自拼装提交字典，面板去掉 X 后它们
            # 不会凭空多出一个 x 字段；若此处硬报错，迁面板就等于让整个
            # 模拟器与手操流程当场跑不起来。回退保证"改面板不会改坏"，
            # 想要更精细取值的 AI 自行提交 x 即可（见 sim/duel_common.py）。
            # 只把"没有这个字段"当作未提交；负数/0 是明确的非法输入，必须拒绝，
            # 不能拿任何整数值当哨兵（否则 -1 会被静默当成"回退到上限"）。
            missing = submitted_x is None
            if not missing and (not isinstance(submitted_x, int)
                                or isinstance(submitted_x, bool)):
                raise ValueError(f"道纹【{name}】的x必须是整数")
            hard_cap = (len(prepared_option.get("dodge_target_options", []))
                        if effective_name == "波及" else None)
            max_x = self._monster_max_daowen_x(monster, effective_name, target,
                                               hard_cap=hard_cap)
            if missing:                    # 提交方未给 X → 回退到上限
                effective_x = max_x
            elif not 1 <= submitted_x <= max_x:
                raise ValueError(
                    f"道纹【{name}】X={submitted_x}超出可负担范围1~{max_x}")
            else:
                effective_x = submitted_x
            if effective_x < 1:
                raise ValueError(f"道纹【{name}】此刻无可负担的X（上限{max_x}）")
        else:
            effective_x = inst.x_value

        freeze_choices = None
        if effective_name == "冰冻":
            effective_x, freeze_choices = self.prepare_bingdong(
                target, effective_x, choice.get("freeze_daowen"))
            if effective_x == 0:
                return {"monster": monster.name, "daowen_skipped": name,
                        "resolves_as": effective_name, "reason": "目标没有道纹，冰冻X自动降为0"}

        # 先完成完整闪避提交的静态校验，再支付任何代价或改变激活状态。
        calc = DaoWenEngine.resolve(effective_name, effective_x, target=target, caster=monster)
        if effective_name == "冰冻":
            calc["freeze_daowen"] = freeze_choices
        if effective_name == "催化":
            self.prepare_catalysis(calc,monster,target,choice,refs)
        if effective_name == "回响":
            self.prepare_huixiang(calc, monster, target, choice, refs)
        if effective_name in {"流动", "遗忘"}:
            self.prepare_liudong_yiwang(effective_name, calc, monster, target, choice, refs)
        # 动态代价校验：残韵改写/状态变化后怪物可能付不起代价（如速度归零后
        # 【洞察】(速度3)）——本次视为无法发动并跳过，不硬报错、不占出手。
        if not self._monster_can_pay_calc_cost(monster, calc):
            return {"monster": monster.name, "daowen_skipped": name,
                    "resolves_as": effective_name, "reason": "无法支付代价"}
        # 2026-09-16 用户令：怪物支付法力（旧条文"不支付法力"已废止）。
        # 付不起的情况已由上一闸门挡掉，这里只做实际支付。
        # 注意：法力同时就是[攻击力]，此刻支付会削弱本回合**之后**的普攻——
        # 攻击与道纹的先后顺序由提交方（AI/操作者）决定，这正是"先攻后纹还是先纹后攻"的取舍。
        hostile = self.state.on_player_side(target) != self.state.on_player_side(monster)
        dodge = choice.get("dodge")
        blood_shadow = choice.get("blood_shadow", False)
        aoe_dodge_choices: list[tuple[Entity, bool, dict]] = []
        # 必中：次数型（bizhong_remaining）+ 新必中X 真self-buff（remaining_rounds>0）
        def _has_real_buff(ent):
            s = next((s for s in ent.status_effects if s.name == "必中"), None)
            return bool(s and s.remaining_rounds > 0)
        must_hit_preview = (self.bizhong_remaining(monster) > 0 or _has_real_buff(monster))
        if effective_name == "波及":
            # 波及X：选择X个[目标]建立/解除波及效果（持续∞）。每个目标显式提交闪避。
            submitted_dodges = choice.get("dodge_targets")
            # DM裁定2026-08-23自适应降X：以prepare快照的wave_effective_x为准
            # （min(面板X, 合法目标数)），驱动与校验始终同一口径。
            mark_count = int(prepared_option.get("wave_effective_x")
                             or calc.get("mark_targets", effective_x))
            if not isinstance(submitted_dodges, list) or len(submitted_dodges) != mark_count:
                raise ValueError(f"道纹【波及】必须为{mark_count}个目标显式提交dodge_targets")
            expected_ref_list = [
                target.get("ref") for target in prepared_option.get("dodge_target_options", [])
            ]
            expected_refs = set(expected_ref_list)
            received: dict[str, dict] = {}
            for entry in submitted_dodges:
                if (not isinstance(entry, dict) or not isinstance(entry.get("dodge"), bool)
                        or not isinstance(entry.get("blood_shadow"), bool)
                        or not isinstance(entry.get("target_ref"), str)
                        or entry["dodge"] and entry["blood_shadow"]):
                    raise ValueError("dodge_targets每项必须包含target_ref与布尔值dodge/blood_shadow")
                ref = entry["target_ref"]
                if ref in received or ref not in expected_refs:
                    raise ValueError("波及dodge_targets必须覆盖X个不重复的合法目标")
                received[ref] = entry
            for entry in submitted_dodges:
                ref = entry["target_ref"]
                entity = refs.get(ref)
                if entity is None or not entity.is_alive or entity is monster:
                    raise ValueError("prepare中的波及目标已失效，请重新prepare_monster_phase")
                want_dodge = entry["dodge"]
                if want_dodge and not must_hit_preview and entity.current_speed < 1:
                    raise ValueError(f"{entity.name}速度不足，不能选择闪避")
                if entry["blood_shadow"] and (must_hit_preview or not self.state.side_has(entity, "血影")
                                                or entity.current_hp <= 10):
                    raise ValueError(f"{entity.name}不能使用血影")
                if want_dodge and not must_hit_preview and self.state.side_has(entity, "回锋刀"):
                    allowed = {t_opt["ref"] for t_opt in prepared_option["target_options"]
                               if not self.state.on_player_side(refs[t_opt["ref"]])}
                    if entry.get("dodge_relic_target_ref") not in allowed:
                        raise ValueError("回锋刀触发必须显式提交合法目标")
                aoe_dodge_choices.append((entity, want_dodge, entry))
            if dodge not in (None, False):
                raise ValueError("波及使用dodge_targets，不接受dodge=true")
        elif requires_target and hostile:
            if not isinstance(dodge, bool) or not isinstance(blood_shadow, bool):
                raise ValueError(f"道纹【{effective_name}】必须显式提交布尔值dodge/blood_shadow")
            if dodge and blood_shadow:
                raise ValueError("不能同时闪避并使用血影")
            if dodge and not must_hit_preview and target.current_speed < 1:
                raise ValueError(f"{target.name}速度不足，不能选择闪避")
            if blood_shadow and (must_hit_preview or not self.state.side_has(target, "血影")
                                 or target.current_hp <= 10):
                raise ValueError(f"{target.name}不能使用血影")
            if dodge and not must_hit_preview and self.state.side_has(target, "回锋刀"):
                allowed = {entry["ref"] for entry in prepared_option["target_options"]
                           if self.state.on_player_side(refs[entry["ref"]]) != self.state.on_player_side(target)}
                if choice.get("dodge_relic_target_ref") not in allowed:
                    raise ValueError("回锋刀触发必须显式提交合法目标")
            if choice.get("dodge_targets") not in (None, []):
                raise ValueError(f"道纹【{effective_name}】不接受dodge_targets")
        else:
            if (dodge not in (None, False) or blood_shadow not in (None, False)
                    or choice.get("dodge_targets") not in (None, [])):
                raise ValueError(f"道纹【{effective_name}】当前结算不接受闪避提交")

        trigger_choices = choice.get("trigger_spell_choices", {})
        # 执行阶段：守夜灯法力已在[敌回始]实际授予，无需预计算（extra_mana 默认0）。
        self.validate_daowen_trigger_spells(monster, trigger_choices, refs)
        trigger_logs = self.resolve_daowen_trigger_spells(monster, trigger_choices, refs)
        if not monster.is_alive or self.state.is_sealed_away(monster) or monster.is_departed or monster.has_retreated:
            return {"monster": monster.name, "daowen_activated": name,
                    "interrupted_by_spell": True, "trigger_spell_logs": trigger_logs}
        if not self._monster_can_pay_calc_cost(monster,calc):
            return {"monster":monster.name,"daowen_skipped":name,"reason":"反应后无法支付代价",
                    "trigger_spell_logs":trigger_logs}
        if calc.get("cost_type")=="消耗" and calc.get("cost",0)>0:
            _mana_paid, mana_error = self.pay_daowen_mana(monster, calc, target)
            if mana_error:
                return {"monster": monster.name, "daowen_skipped": name,
                        "reason": mana_error, "trigger_spell_logs": trigger_logs}

        # 残韵改写只替换本次结算：不支付源道纹代价，也不将源道纹记为已激活。
        if rewritten_as:
            consumed = self.consume_resonance_rewrite(monster, name)
            if consumed != rewritten_as:
                raise ValueError("残韵改写已变化，请重新prepare_monster_phase")
        # 原始怪物道纹与封印照常支付异变；只有非怪物角色可能触发迷失。
        elif ((name in self.ORIGINAL_MONSTER_DAOWEN and calc.get("cost_type") != "冷却")
              or name == "封印"):
            mutation_cost = (self.YUANCHU_COST_RATE * effective_x
                             if name in self.ORIGINAL_MONSTER_DAOWEN else effective_x)
            paid = monster.add_mutation(mutation_cost)
            if paid.get("lost"):
                self._resolve_mutation_lost(monster, {
                    "timing": "monster_action", "source": name, "source_type": "daowen",
                    "actor": monster, "target": monster, "mechanic": "cost",
                    "subtype": "mutation", "amount": mutation_cost,
                    "tags": {"daowen", "active_payment"}})
                return {"monster": monster.name, "lost": name,
                        "note": "支付异变后触发【迷失】"}

        if not rewritten_as:
            activated.add(name)
            self._monster_round_used(monster).add(name)
            # 2026-09-16 用户令：道纹递增（升级）机制已废止。X 恒为面板/借用时写定的值，
            # 重复发动按同一 X 计费，不再随发动次数累加。
        monster.mark_daowen_used(effective_name)
        monster.actions_used_this_round += 1
        self.consume_tamper(monster,tamper_rule)
        self.pay_daowen_cooldown(effective_name,calc,monster)

        aoe_targets_override = None
        if effective_name == "波及":
            # 波及X：按显式提交逐目标建立/解除波及标记（持续∞，[战终]清除）。
            wave_marked: list[str] = []
            wave_unmarked: list[str] = []
            for entity, want_dodge, entry in aoe_dodge_choices:
                if must_hit_preview:
                    # 次数型必中扣1次，状态型必中（monster.has_status("必中")）不扣
                    if self.bizhong_remaining(monster) > 0:
                        self.consume_bizhong(monster)
                    marked = self._toggle_wave_mark(entity, monster, activation_x=effective_x)
                    (wave_marked if marked else wave_unmarked).append(entity.name)
                elif entry["blood_shadow"]:
                    self.pay_numeric_cost(
                        entity, "生命", 10,
                        cost_share_target_ref=entry.get("cost_share_target_ref", ""),
                        cost_context={"timing": "reaction", "source": "血影", "source_type": "relic", "tags": {"active_payment"}})
                elif want_dodge:
                    self._spend_dodge_speed(entity, entry.get("dodge_relic_target_ref"))
                else:
                    marked = self._toggle_wave_mark(entity, monster, activation_x=effective_x)
                    (wave_marked if marked else wave_unmarked).append(entity.name)
            execution = self.apply_daowen_effect(effective_name, calc, monster, target)
            execution["wave_marked"] = wave_marked
            execution["wave_unmarked"] = wave_unmarked
            return {"monster": monster.name, "daowen_activated": name, "x": effective_x,
                    "resolves_as": effective_name, "resonance_rewrite": bool(rewritten_as),
                    "target": target.name, "execution": execution,
                    "trigger_spell_logs": trigger_logs}
        elif requires_target and hostile:
            if must_hit_preview:
                if self.bizhong_remaining(monster) > 0:
                    self.consume_bizhong(monster)
            elif blood_shadow:
                self.pay_daowen_numeric_cost(effective_name,calc,monster)
                self.pay_numeric_cost(
                    target, "生命", 10,
                    cost_share_target_ref=choice.get("cost_share_target_ref", ""),
                    cost_context={"timing": "reaction", "source": "血影", "source_type": "relic", "tags": {"active_payment"}})
                return {"monster": monster.name, "daowen_activated": name,
                        "resolves_as": effective_name, "target": target.name, "blood_shadow": True,
                        "trigger_spell_logs": trigger_logs}
            elif dodge:
                self.pay_daowen_numeric_cost(effective_name,calc,monster)
                self._spend_dodge_speed(target, choice.get("dodge_relic_target_ref"))
                return {"monster": monster.name, "daowen_activated": name,
                        "resolves_as": effective_name, "target": target.name, "dodged": True,
                        "trigger_spell_logs": trigger_logs}

        execution = self.apply_daowen_effect(
            effective_name, calc, monster, target,
            aoe_targets_override=aoe_targets_override,
        )
        return {"monster": monster.name, "daowen_activated": name, "x": effective_x,
                "resolves_as": effective_name, "resonance_rewrite": bool(rewritten_as),
                "target": target.name, "execution": execution,
                "trigger_spell_logs": trigger_logs}

    def _validate_monster_daowen_schema(
        self, monster: Entity, choice: dict, refs: dict[str, Entity],
        prepared_option: dict, pending_shouyedeng: int = 0,
    ) -> None:
        """道纹选择的静态 schema 校验（零副作用）。

        只校验与执行状态无关的结构/引用/布尔提交；依赖执行后状态的数值
        （目标当前速度、血影所需生命、本回合已使用集合）留给执行阶段动态
        校验 + 快照回滚兜底，避免把"道纹执行会改变的状态"提前固化。
        """
        name = choice.get("name", "")
        inst = monster.dao_wen.get(name)
        if (inst is None or name in self._monster_round_used(monster)
                or not inst.can_use()):
            raise ValueError(f"{monster.name}不能发动道纹【{name}】")
        if name not in DaoWenEngine.list_all():
            raise ValueError(f"未知道纹【{name}】")
        rewritten_as = (self._resonance_rewrites.get(id(monster)) or {}).get(name)
        effective_name = rewritten_as or name
        requires_target = self._daowen_requires_target(effective_name)
        target_ref = choice.get("target_ref", "")
        if requires_target:
            target = refs.get(target_ref)
            if target is None:
                raise ValueError(f"道纹【{effective_name}】必须提交合法target_ref")
            if target is not monster and not self.is_targetable(monster, target):
                raise ValueError(f"目标{target.name}当前不可被{monster.name}选中")
        else:
            if target_ref:
                raise ValueError(f"道纹【{effective_name}】不接受target_ref")
            target = monster

        target, _ = self.resolve_tampered_target(monster, target, effective_name, choice, refs)
        if effective_name == "冰冻":
            # 怪物冷却型道纹固定X=1；目标0种时prepare不会给出该选项。
            self.prepare_bingdong(target, 1, choice.get("freeze_daowen"))
        hostile = self.state.on_player_side(target) != self.state.on_player_side(monster)
        dodge = choice.get("dodge")
        blood_shadow = choice.get("blood_shadow", False)
        if effective_name == "波及":
            submitted_dodges = choice.get("dodge_targets")
            # DM裁定2026-08-23自适应降X：与执行阶段同一口径（prepare快照）。
            mark_count = int(prepared_option.get("wave_effective_x") or inst.x_value)
            if not isinstance(submitted_dodges, list) or len(submitted_dodges) != mark_count:
                raise ValueError(f"道纹【波及】必须为{mark_count}个目标显式提交dodge_targets")
            expected_refs = {
                t_opt.get("ref") for t_opt in prepared_option.get("dodge_target_options", [])
            }
            received: dict[str, dict] = {}
            for entry in submitted_dodges:
                if (not isinstance(entry, dict) or not isinstance(entry.get("dodge"), bool)
                        or not isinstance(entry.get("blood_shadow"), bool)
                        or not isinstance(entry.get("target_ref"), str)
                        or entry["dodge"] and entry["blood_shadow"]):
                    raise ValueError("dodge_targets每项必须包含target_ref与布尔值dodge/blood_shadow")
                ref = entry["target_ref"]
                if ref in received or ref not in expected_refs:
                    raise ValueError("波及dodge_targets必须提交X个不重复的合法目标")
                received[ref] = entry
            if dodge not in (None, False):
                raise ValueError("波及使用dodge_targets，不接受dodge=true")
        elif requires_target and hostile:
            if not isinstance(dodge, bool) or not isinstance(blood_shadow, bool):
                raise ValueError(f"道纹【{effective_name}】必须显式提交布尔值dodge/blood_shadow")
            if dodge and blood_shadow:
                raise ValueError("不能同时闪避并使用血影")
            if choice.get("dodge_targets") not in (None, []):
                raise ValueError(f"道纹【{effective_name}】不接受dodge_targets")
        else:
            if (dodge not in (None, False) or blood_shadow not in (None, False)
                    or choice.get("dodge_targets") not in (None, [])):
                raise ValueError(f"道纹【{effective_name}】当前结算不接受闪避提交")

        trigger_choices = choice.get("trigger_spell_choices", {})
        # 2026-10-02：法力预算不再参与静态校验（资源问题一律是执行期中断），
        # 因此不再需要把守夜灯的预付法力传进来。
        self.validate_daowen_trigger_spells(monster, trigger_choices, refs)

    def _monster_free_actions(self) -> bool:
        """怪物行动自由化开关（默认关，见 GameState.monster_free_actions）。

        统一交替出手后不再另设怪物自由化开关；兼容快照始终允许在
        攻击、道纹或跳过中选一个真实行动，预算仍由通用面板决定。
        """
        return True

    def _check_monster_daowen_object(
        self, monster, dao_choice: dict, expected_actor: dict, refs: dict,
        pending_shouyedeng: int = 0, *, label: str = "daowen",
    ) -> None:
        """校验一个 daowen 提交对象（第一发与第二发道纹共用同一套静态校验）。"""
        options = {o["name"] for o in expected_actor["daowen_options"]}
        if dao_choice.get("name") not in options:
            raise ValueError(f"{monster.name}提交的道纹不在prepare合法选项中（{label}）")
        prepared_option = next(
            option for option in expected_actor["daowen_options"]
            if option["name"] == dao_choice["name"]
        )
        if (prepared_option["requires_target"]
                and dao_choice.get("target_ref") not in {
                    target["ref"] for target in prepared_option["target_options"]
                }):
            raise ValueError(f"{monster.name}提交的道纹目标不在prepare合法选项中（{label}）")
        self._validate_monster_daowen_schema(
            monster, dao_choice, refs, prepared_option,
            pending_shouyedeng=pending_shouyedeng,
        )

    def _validate_monster_phase_static(
        self, submitted: dict[str, dict], prepared: dict,
    ) -> None:
        """怪物阶段全部输入的静态 schema 校验（零副作用）。

        在任何执行（龙息/守夜灯/道纹/攻击）之前拦截与执行状态无关的非法输入：
          - 道纹选择/目标引用/闪避提交结构（_validate_monster_daowen_schema）
          - attack_actions 数量（vs prepare 快照）
          - 每次命中的 target_ref/dodge/blood_shadow 布尔/血影资格/回锋刀目标/法术提交

        依赖执行后状态的数量校验（hits 命中数、目标当前速度、目标存活性）不在此
        判定——它们必须按执行时的真实状态校验（如【变形】会改变命中数），失败由
        resolve_monster_phase 的快照回滚保证零副作用。
        """
        expected = {actor["actor_ref"]: actor for actor in prepared["actors"]}
        refs = self._combat_entity_refs()
        # 守夜灯自 2026-09-13 改为[回始]授予，进入怪物阶段时法力已经在池里，
        # 静态校验无需再预付（再预付就是重复计算）。
        pending_shouyedeng = 0
        for actor_ref, choice in submitted.items():
            monster = refs.get(actor_ref)
            if monster is None or not monster.is_alive:
                continue  # 与执行循环一致：死斗部分提交/已死者跳过
            expected_actor = expected[actor_ref]

            free = self._monster_free_actions()
            dao_choice = choice.get("daowen")
            dao_choice_2 = choice.get("daowen_2")
            options = {o["name"] for o in expected_actor["daowen_options"]}
            if not free and dao_choice_2 is not None:
                raise ValueError(f"{monster.name}本局未开启怪物自由行动，不接受daowen_2")
            if free:
                if dao_choice_2 is not None and not isinstance(dao_choice_2, dict):
                    raise ValueError(f"{monster.name}的daowen_2必须是对象或null")
                if isinstance(dao_choice_2, dict):
                    if not isinstance(dao_choice, dict):
                        raise ValueError(f"{monster.name}提交daowen_2前必须先提交daowen")
                    if dao_choice_2.get("name") == dao_choice.get("name"):
                        raise ValueError(f"{monster.name}同一回合不能重复发动同一种道纹")
            if options and not isinstance(dao_choice, dict) and not free:
                raise ValueError(f"{monster.name}必须从合法选项中提交一个daowen对象")
            if not options:
                if dao_choice is not None:
                    raise ValueError(f"{monster.name}本次没有合法道纹选项，daowen必须为null")
                if dao_choice_2 is not None:
                    raise ValueError(f"{monster.name}本次没有合法道纹选项，daowen_2必须为null")
            if isinstance(dao_choice, dict):
                self._check_monster_daowen_object(
                    monster, dao_choice, expected_actor, refs, pending_shouyedeng)
            if isinstance(dao_choice_2, dict):
                self._check_monster_daowen_object(
                    monster, dao_choice_2, expected_actor, refs, pending_shouyedeng,
                    label="daowen_2")

            attack_actions = choice.get("attack_actions")
            expected_actions = expected_actor["base_attack_actions"]
            if free:
                budget = self.single_round_action_count(monster)
                if monster.actions_used_this_round >= budget:
                    raise ValueError(f"{monster.name}本回合的出手已经用完（{budget}）")
                # 一次兼容提交只能承载一个真实行动；多步施法走cast窗口栈。
                if not isinstance(attack_actions, list):
                    raise ValueError("attack_actions必须是列表")
                if expected_actions <= 0:
                    if attack_actions:
                        raise ValueError(f"{monster.name}本回合没有合法攻击目标，attack_actions必须为空")
                    attack_slots = 0
                else:
                    if len(attack_actions) not in (0, expected_actions):
                        raise ValueError(
                            f"{monster.name}的attack_actions只能是0或{expected_actions}个")
                    attack_slots = len(attack_actions) // expected_actions
                dao_slots = sum(1 for obj in (dao_choice, dao_choice_2)
                                if isinstance(obj, dict))
                if attack_slots + dao_slots != 1:
                    raise ValueError(
                        f"{monster.name}每次必须提交且只能提交1个行动"
                        f"（收到攻击{attack_slots}＋道纹{dao_slots}）")
            elif not isinstance(attack_actions, list) or len(attack_actions) != expected_actions:
                raise ValueError(f"{monster.name}必须提交{expected_actions}个attack_actions")
            legal_attack_options = {
                target["ref"]: target for target in expected_actor["attack_target_options"]
            }
            for attack_action in attack_actions:
                if (not isinstance(attack_action, dict)
                        or not isinstance(attack_action.get("hits"), list)):
                    raise ValueError("每个attack_action必须包含hits列表")
                for hit in attack_action["hits"]:
                    if (not isinstance(hit, dict) or not isinstance(hit.get("dodge"), bool)
                            or not isinstance(hit.get("blood_shadow"), bool)):
                        raise ValueError("每次攻击必须显式提交target_ref、dodge与blood_shadow")
                    if hit["dodge"] and hit["blood_shadow"]:
                        raise ValueError("同一次判定不能同时闪避并使用血影")
                    if hit.get("target_ref") not in legal_attack_options:
                        raise ValueError("怪物攻击目标不在prepare合法选项中")
                    target = refs.get(hit.get("target_ref", ""))
                    if target is None or not self.state.are_hostile(monster, target):
                        raise ValueError("怪物攻击target_ref必须是prepare列出的敌对目标")
                    if not self.is_targetable(monster, target):
                        raise ValueError(f"{target.name}当前不可被{monster.name}选中")
                    option = legal_attack_options[hit["target_ref"]]
                    if hit["blood_shadow"] and not option.get("can_blood_shadow"):
                        raise ValueError(f"{target.name}不能使用血影")
                    if hit["dodge"] and self.state.side_has(target, "回锋刀"):
                        allowed = {entry["ref"] for entry in option["dodge_relic_target_options"]}
                        if hit.get("dodge_relic_target_ref") not in allowed:
                            raise ValueError("回锋刀触发必须显式提交合法目标")
                    self.validate_spell_reaction_submission(
                        target, monster, hit.get("spell_choices"), refs,
                    )

    def _monster_phase_snapshot(self) -> dict:
        """怪物阶段执行前快照：state（含实体/事件流/碎片/消耗品）+ 引擎侧怪物状态。"""
        import copy
        return {
            "state": copy.deepcopy(self.state),
            "activated": copy.deepcopy(self._monster_activated),
            "round_used": copy.deepcopy(self._monster_daowen_round_used),
            "rewrites": copy.deepcopy(self._resonance_rewrites),
            "dice": copy.deepcopy(self.dice),
            "split_spawned": getattr(self, "_split_clones_spawned", 0),
        }

    def _monster_phase_restore(self, snap: dict) -> None:
        """恢复执行前快照：任何非法 resolve 输入/执行中异常都不得留下战斗副作用。

        原地恢复 self.state 的内容（保持 combat.state 与 api 层 engine.state 是
        同一对象引用），并把实体/列表替换为快照副本——外部代码在失败后应重新
        从 state 读取实体，不得继续使用失败前的旧引用。
        """
        state = self.state
        restored = snap["state"]
        state.__dict__.clear()
        state.__dict__.update(restored.__dict__)
        self._monster_activated = snap["activated"]
        self._monster_daowen_round_used = snap["round_used"]
        self._resonance_rewrites = snap["rewrites"]
        self.dice = snap["dice"]
        self._split_clones_spawned = snap["split_spawned"]

    def resolve_monster_phase(self, choices: list[dict], prepared: dict) -> list[dict]:
        """怪物阶段的公开入口（开帧后转实现体 `_resolve_monster_phase_impl`）。"""
        with resolution_frame(self, KIND_EFFECT, "怪物阶段"):
            return self._resolve_monster_phase_impl(choices, prepared)

    def _resolve_monster_phase_impl(self, choices: list[dict], prepared: dict) -> list[dict]:
        """严格按传入的prepare快照验证并结算；任何非法输入由API事务整体回滚。"""
        if not isinstance(choices, list):
            raise ValueError("choices必须是列表")
        if not isinstance(prepared, dict) or not isinstance(prepared.get("actors"), list):
            raise ValueError("prepared必须是prepare_monster_phase返回的合法快照")
        expected = {actor["actor_ref"]: actor for actor in prepared["actors"]}
        submitted: dict[str, dict] = {}
        for choice in choices:
            if not isinstance(choice, dict):
                raise ValueError("每个怪物选择必须是对象")
            ref = choice.get("actor_ref", "")
            if ref in submitted:
                raise ValueError(f"重复提交怪物选择: {ref}")
            submitted[ref] = choice
        # 死斗交替（对称）：守擂侧每步只结算1个actor，其余本步不动
        # （逐出手交替与挑战者侧一致，修复守擂方机制性必胜）。
        # 养蛊场：允许逐个 actor 提交（与死斗同样按「每步只结算 1 个 actor」处理），
        # 否则先手方会连带结算全场，训练出来的策略会带上出手顺序红利。
        if len(submitted) != 1 or set(submitted) - set(expected):
            raise ValueError(
                f"交替出手中每次必须且只能提交1个快照内角色的1个行动；"
                f"收到{sorted(submitted)}，可选{sorted(expected)}")
        # 事务一致性（2026-08-19）：先完成全部静态 schema 校验（零副作用），
        # 再执行任何龙息/守夜灯/道纹/攻击。依赖执行后状态的动态校验
        # （hits 命中数、目标速度/存活）在执行阶段进行，失败即快照回滚，
        # 保证"任何非法 resolve 输入不得留下任何战斗副作用"。
        self._validate_monster_phase_static(submitted, prepared)
        snapshot = self._monster_phase_snapshot()
        try:
            return self._execute_monster_phase(submitted, prepared, expected)
        except Exception:
            self._monster_phase_restore(snapshot)
            raise

    def _execute_monster_phase(
        self, submitted: dict[str, dict], prepared: dict, expected: dict[str, dict],
    ) -> list[dict]:
        """校验全部通过后的怪物阶段执行（原 resolve_monster_phase 执行体，语义不变）。"""
        refs = self._combat_entity_refs()
        results: list[dict] = []
        # 守夜灯：用户裁定 2026-09-13 改为[回始]授予且不再清空，
        # 故怪物阶段不再有「[敌回始]授予 / [敌回终]清空」这一对动作。
        # 2026-10-03：【爆裂】删除后不再有敌回终专属递减拍（_tick_baolie 已移除）。
        results.extend(prepared["skipped"])
        for actor_ref in submitted:  # 死斗部分提交：只结算本步提交的actor
            monster = refs.get(actor_ref)
            if monster is None or not monster.is_alive:
                continue
            choice = submitted[actor_ref]
            breath = self.apply_opposing_longxi(monster)
            if breath:
                results.append({"monster": monster.name, **breath})
                if not monster.is_alive:
                    continue
            activated = self._monster_activated.setdefault(id(monster), set())
            # 攻击出手数以“道纹结算前”的已激活集合为准：疯狂是[回始]持续效果，
            # 本回合刚发动时从下回合起生效，prepare列出的 base_attack_actions 也是按
            # 结算前状态给出的——两处必须一致，否则按 prepare 提交必然失败。
            activated_before = set(activated)

            free = self._monster_free_actions()
            dao_choice = choice.get("daowen")
            dao_choice_2 = choice.get("daowen_2")
            options = {o["name"] for o in expected[actor_ref]["daowen_options"]}
            if options and not isinstance(dao_choice, dict) and not free:
                raise ValueError(f"{monster.name}必须从合法选项中提交一个daowen对象")
            if not options and dao_choice is not None:
                raise ValueError(f"{monster.name}本次没有合法道纹选项，daowen必须为null")
            for dao_obj in (dao_choice, dao_choice_2):
                if not isinstance(dao_obj, dict):
                    continue
                prepared_option = next(
                    option for option in expected[actor_ref]["daowen_options"]
                    if option["name"] == dao_obj["name"]
                )
                dao_result = self._resolve_monster_daowen_choice(
                    monster, dao_obj, refs, activated, prepared_option,
                )
                results.append(dao_result)
                if not monster.is_alive:
                    break
            if not monster.is_alive:
                continue

            attack_actions = choice.get("attack_actions")
            # 自由行动回合推进标记（2026-10-03）：本模式下每只怪一回合提交 1~2 个槽
            # （攻击组/道纹各占 1 槽），引擎的子阶段路由按「是否已出手」判断回合是否走完；
            # 若本回合只交了道纹而没交攻击，actions_used_this_round 不会自增，
            # 会把回合卡在玩家行动阶段。这里在该怪结算完成后统一标记为「本回合已走完」
            # （≥2 槽）。只在自由行动模式下生效，默认关时一行不改。
            if free and isinstance(dao_choice, dict):
                monster.actions_used_this_round = max(monster.actions_used_this_round, 1)
            # 出手数按prepare快照校验：2026-08-17疯狂全局裁定后，状态在本actor
            # 道纹结算中即盖到全场，若此处按当前状态重算会把"自下回合生效"提前到
            # 本回合，导致按prepare提交必然失败；快照即契约（两处必须一致）。
            expected_actions = expected[actor_ref]["base_attack_actions"]
            if free:
                # 自由行动：0 / 1 / 2 次攻击（每次＝base_attack_actions组命中），
                # 槽预算已由静态校验判定；这里只复核长度形状与快照一致。
                if not isinstance(attack_actions, list):
                    raise ValueError("attack_actions必须是列表")
                if expected_actions <= 0:
                    if attack_actions:
                        raise ValueError(f"{monster.name}本回合没有合法攻击目标，attack_actions必须为空")
                elif (len(attack_actions) % expected_actions
                        or len(attack_actions) // expected_actions > 1):
                    raise ValueError(
                        f"{monster.name}的attack_actions只能是0或{expected_actions}个")
            elif not isinstance(attack_actions, list) or len(attack_actions) != expected_actions:
                raise ValueError(f"{monster.name}必须提交{expected_actions}个attack_actions")
            # 出手数按 prepare 快照校验（快照即契约）；但**击数与伤害预算在出手时取当前面板**
            # ——[攻击次数]＝[当前速度]、[攻击力]＝[当前法力] 都是实时量，本 actor 的道纹
            # 结算可能已经改过速度/法力（如【变形】把两者互换），照玩家侧同一口径重取。
            defense_spend: dict[str, int] = {}
            # 防御额度按提交前的速度快照计（闪避/招架在结算中会实时扣速度，
            # 若按实时值再比一次就会把自己花掉的速度重复计入）。
            speed_snapshot: dict[str, int] = {ref: e.current_speed for ref, e in refs.items()}
            for action_index, attack_action in enumerate(attack_actions):
                if not isinstance(attack_action, dict) or not isinstance(attack_action.get("hits"), list):
                    raise ValueError("每个attack_action必须包含hits列表")
                # 伤害预算按**每次攻击出手**独立计算（与玩家侧一次攻击一份预算同口径）；
                # 自由行动下的第二次攻击是另一份预算，不与前一份叠加。
                allocated_total = 0
                hits_per_action = max(0, monster.effective_attack_count())
                damage_budget = hits_per_action * monster.effective_attack_power()
                hits = attack_action["hits"]
                if len(hits) != hits_per_action:
                    raise ValueError(f"{monster.name}每个攻击出手必须提交{hits_per_action}次命中选择")
                monster.actions_used_this_round += 1
                legal_attack_options = {
                    target["ref"]: target for target in expected[actor_ref]["attack_target_options"]
                }
                for hit_index, hit in enumerate(hits):
                    if not isinstance(hit, dict):
                        raise ValueError("每次攻击必须是包含target_ref的对象")
                    # 2026-10-25 用户令：与玩家攻击同一套逐击字段——
                    # 每击显式提交 damage（伤害分配）与 parry（目标招架）。
                    # 旧推演稿未带这两项时按「等伤一刀、不招架」补齐（与玩家侧同口径）。
                    hit.setdefault("damage", monster.effective_attack_power())
                    hit.setdefault("parry", False)
                    if (not isinstance(hit.get("damage"), int) or isinstance(hit.get("damage"), bool)
                            or hit["damage"] < 0):
                        raise ValueError("每击damage必须是非负整数")
                    if (not isinstance(hit.get("dodge"), bool) or not isinstance(hit.get("parry"), bool)
                            or not isinstance(hit.get("blood_shadow"), bool)):
                        raise ValueError("每次攻击必须显式提交target_ref、dodge、parry与blood_shadow")
                    if sum((hit["dodge"], hit["parry"], hit["blood_shadow"])) > 1:
                        raise ValueError("同一次判定只能选择闪避、招架、血影之一")
                    allocated_total += hit["damage"]
                    if allocated_total > damage_budget:
                        raise ValueError(
                            f"{monster.name}分配总伤害{allocated_total}超过上限{damage_budget}")
                    if hit.get("target_ref") not in legal_attack_options:
                        raise ValueError("怪物攻击目标不在prepare合法选项中")
                    target = refs.get(hit.get("target_ref", ""))
                    if target is None or not self.state.are_hostile(monster, target):
                        raise ValueError("怪物攻击target_ref必须是prepare列出的敌对目标")
                    # 静态预校验后，同阶段先前的道纹可能已让预选目标暂离/失去可选资格
                    # （例如怪物先封印玩家，再结算自己预选的玩家普攻）。这与预选目标
                    # 已命零相同：该击跳过，不把整次合法阶段提交回滚成无效行动。
                    if not self.is_targetable(monster, target):
                        results.append({"attacker": monster.name, "target": target.name,
                                        "skipped": "预选目标已不可选中",
                                        "hit_index": hit_index + 1})
                        continue
                    if not target.is_alive:
                        results.append({"attacker": monster.name, "target": target.name,
                                        "skipped": "预选目标已命零", "hit_index": hit_index + 1})
                        continue
                    _s = next((s for s in monster.status_effects if s.name == "必中"), None)
                    _real_buff = bool(_s and _s.remaining_rounds > 0)
                    must_hit = (self.bizhong_remaining(monster) > 0 or _real_buff)
                    # 次数型必中余号：每次物理判定消耗1次（consume_bizhong 内部无余号时返回False）
                    if self.bizhong_remaining(monster) > 0:
                        self.consume_bizhong(monster)
                    if hit["dodge"] and not must_hit and target.current_speed < 1:
                        raise ValueError(f"{target.name}速度不足，不能选择闪避")
                    if hit["parry"] and not must_hit and target.current_speed < 1:
                        raise ValueError(f"{target.name}速度不足，不能选择招架")
                    # 一次攻击内同一目标的闪避与招架总数不得超过其当前速度（与玩家侧同口径）。
                    if (hit["dodge"] or hit["parry"]) and not must_hit:
                        defense_spend[hit["target_ref"]] = (
                            defense_spend.get(hit["target_ref"], 0) + 1)
                        if defense_spend[hit["target_ref"]] > speed_snapshot.get(hit["target_ref"], 0):
                            raise ValueError(f"{target.name}速度不足以完成全部防御")
                    option = legal_attack_options[hit["target_ref"]]
                    if hit["blood_shadow"] and not option.get("can_blood_shadow"):
                        raise ValueError(f"{target.name}不能使用血影")
                    if hit["dodge"] and not must_hit and self.state.side_has(target, "回锋刀"):
                        allowed = {entry["ref"] for entry in option["dodge_relic_target_options"]}
                        if hit.get("dodge_relic_target_ref") not in allowed:
                            raise ValueError("回锋刀触发必须显式提交合法目标")
                    self.validate_spell_reaction_submission(
                        target, monster, hit.get("spell_choices"), refs,
                    )
                    # 2026-10-11：【无神】已删除，怪物攻击不再有"目标强制改为自身"的重定向
                    # （连带去掉当年为重定向准备的空反应提交分支）。
                    attack_target = target
                    reaction_choices = hit.get("spell_choices")
                    resolved = self.resolve_attack(
                        monster, attack_target, dodge=hit["dodge"], parry=hit["parry"],
                        allocated_damage=hit["damage"],
                        blood_shadow=hit["blood_shadow"],
                        spell_choices=reaction_choices, entity_refs=refs,
                        dodge_relic_target_ref=hit.get("dodge_relic_target_ref"),
                        cost_share_target_ref=hit.get("cost_share_target_ref", ""),
                    )
                    resolved.update({"hit_index": hit_index + 1, "hit_total": hits_per_action,
                                     "attack_action_index": action_index + 1,
                                     "new_action": (hit_index == 0)})
                    results.append(resolved)
                    if not monster.is_alive:
                        break
                if not monster.is_alive:
                    break
        # Only after all submitted attacks/casts: automatic items cannot invalidate
        # their explicit target/hit/budget snapshots. Still inside the transaction.
        from ..npc_consumables import auto_use_consumables
        for actor_ref in submitted:
            results.extend(auto_use_consumables(self, refs.get(actor_ref)))
        return results

