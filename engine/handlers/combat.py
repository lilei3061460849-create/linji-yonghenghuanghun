"""战斗回合行动：战始/战终、攻击准备与结算、怪物阶段、两阶段提交、回合、重开。

2026-11-03 从 `engine/api.py` **原样搬出**（只搬位置，不改逻辑），作为
`GameEngine` 的 mixin 挂回；`@ACTION_REGISTRY.action(...)` 声明归属，
`_dispatch_action` 查表即命中。
"""
from __future__ import annotations

import copy
import math
import uuid

from ..action_registry import ACTION_REGISTRY
from ..combat_events import register_combat_event_observer
from ..consumables import ZIYU_TRIGGER_BATTLE_END, apply_zi_yu_trigger, note_action_targets
from ..effect_context import make_context, normalize_context
from ..enums import (CombatSubphase, EffectPolarity, EffectScope, GamePhase,
                     TriggerTiming)
from ..models import Entity, StatusEffect
from ..npc_consumables import auto_use_consumables
from ..yaoguai import find_owned_yaoguai, owned_yaoguai


class CombatHandlersMixin:
    """战斗回合行动处理器。宿主类：`GameEngine`。"""

    @ACTION_REGISTRY.action("prepare_attack", group="combat")
    def _action_prepare_attack(self, params: dict) -> dict:
        """第一阶段：绑定一次行动的逐击目标、闪避、血影和法术反应选项。"""
        if self.state.pending_attack:
            return {"success": False, "error": "已有待提交攻击"}
        refs = self.combat._combat_entity_refs()
        actor_ref = params.get("actor_ref") or "player:0"
        attacker = refs.get(actor_ref)
        if attacker is None or not attacker.is_alive:
            return {"success": False, "error": "actor_ref不是当前存活行动者"}
        if self.combat._spell_stack and self.combat.out_of_turn_blocked(attacker):
            return {"success": False, "error": f"{attacker.name}被【必中】封锁回合外行动，无法插队"}
        duel_error = self._check_duel_turn_or_error(attacker)
        if duel_error:
            return duel_error
        if not self.combat.can_act(attacker):
            return {"success": False, "error": f"{attacker.name}当前无法行动"}
        if attacker.actions_used_this_round >= self._action_budget_of(attacker):
            return {"success": False, "error": f"{attacker.name}本回合出手已用完"}

        target_refs = [ref for ref, entity in refs.items()
                       if self.state.on_player_side(entity) != self.state.on_player_side(attacker)
                       and self.combat.is_targetable(attacker, entity)]
        if (self.state.on_enemy_side(attacker) and "龙威" in self.state.dragon_traits
                and "player:0" in refs):
            target_refs = ["player:0"]
        if (self.state.on_player_side(attacker) and "龙威" in self.state.opponent_dragon_traits):
            opponent_ref = next((ref for ref, entity in refs.items()
                                 if entity.entity_type == "轮回者" and self.state.on_enemy_side(entity)), None)
            if opponent_ref:
                target_refs = [opponent_ref]
        if not target_refs and attacker.effective_attack_count() > 0:
            return {"success": False, "error": "没有合法攻击目标"}

        target_options = []
        for ref in target_refs:
            target = refs[ref]
            blood_pact_options = self.combat.blood_shadow_cost_share_options(target)
            target_options.append({
                "ref": ref, "name": target.name,
                "can_dodge": target.current_speed > 0,
                "can_parry": target.current_speed > 0,
                "can_blood_shadow": (self.state._relic_active(target, "血影")
                                     and (target.current_hp > 10 or bool(blood_pact_options))),
                "blood_shadow_cost_share_target_options": blood_pact_options,
                "spell_options": self.combat.prepare_spell_reactions(target, attacker),
                "dodge_relic_target_options": [
                    {"ref": r, "name": e.name} for r, e in refs.items()
                    if self.state.on_player_side(e) != self.state.on_player_side(target) and e.is_alive
                ] if self.state._relic_active(target, "回锋刀") else [],
            })
        options = {
            "actor_ref": actor_ref,
            "actor": attacker.name,
            # DM裁定 2026-09-10：轮回者的击数=当前速度（换算仅限轮回者）
            "hit_count": attacker.effective_attack_count(),
            "damage_budget": attacker.effective_attack_count() * attacker.effective_attack_power(),
            "target_options": target_options,
            "hits_schema": [{
                "target_ref": [o["ref"] for o in target_options],
                "damage": "非负整数；所有击的总和不得超过damage_budget",
                "dodge": "boolean", "parry": "boolean", "blood_shadow": "boolean",
                "dodge_relic_target_ref": "持有回锋刀且闪避时：多目标必填，唯一候选可省略（引擎自动取缺省目标）",
                "spell_choices": "按目标spell_options完整提交",
            } for _ in range(attacker.effective_attack_count())],
        }
        token = uuid.uuid4().hex
        self.state.pending_attack = {
            "token": token, "round": self.state.current_round, "options": options,
            # 逐击推进用：已结算几击、已落地的选择、本击是否已声明、出手是否已扣。
            "hit_index": 0, "hits_done": [], "declared": None, "action_spent": False,
        }
        return {"success": True, "action": "准备攻击", "result": {"token": token, **options}}

    # ---------------------------------------------------------------- 普攻逐击
    # 规则正文·战斗行动：「攻击（占1次主动出手）：声明瞬间锁定击数与总伤害上限，
    # 逐击分配非负整数伤害……每击按『攻击者确定伤害→目标选择承受/招架/闪避→
    # 公开伤害→结算』推进。」这是通用规则，不分玩家/怪物、轮回者/微光者：
    # 所以逐击状态挂在 `state.pending_attack` 上，由引擎自己的三个动作推进，
    # 任何调用方（网页、AI、怪物阶段）走的都是同一条路。
    #
    # * `attack_declare_hit`：攻击方定这一击的目标与 X（还不结算）；
    # * `attack_respond_hit`：防守方选硬扛/招架/闪避/血影，立刻结算这一击；
    # * `resolve_attack`：老的一次性批量提交，保留兼容（等价于连续提交多击）。

    def _attack_defender_options(self, pending: dict, ref: str) -> dict:
        """防守方在这一击上能做什么（逐击询问用，不含任何预选）。"""
        options = pending["options"]
        entry = {item["ref"]: item for item in options["target_options"]}.get(ref) or {}
        refs = self.combat._combat_entity_refs()
        target = refs.get(ref)
        attacker = refs.get(options["actor_ref"])
        speed = int(getattr(target, "current_speed", 0) or 0)
        index = int(pending.get("hit_index", 0))
        bizhong = int(self.combat.bizhong_remaining(attacker)) if attacker else 0
        return {
            "target_ref": ref,
            "target": entry.get("name"),
            "can_tank": True,
            "can_parry": bool(entry.get("can_parry")) and speed > 0,
            "can_dodge": bool(entry.get("can_dodge")) and speed > 0,
            "can_blood_shadow": bool(entry.get("can_blood_shadow")),
            # 【必中】剩余次数覆盖本击时，闪避无效（与结算同一判定）。
            "dodge_locked": bizhong > index,
            "speed_left": speed,
            "blood_shadow_cost_share_target_options":
                entry.get("blood_shadow_cost_share_target_options") or [],
        }

    def _attack_pending_view(self, pending: dict) -> dict:
        """这一手的进度：第几击、还剩几击、伤害预算与已分配。"""
        options = pending["options"]
        done = list(pending.get("hits_done") or [])
        index = int(pending.get("hit_index", 0))
        return {
            "hit_index": index,
            "hit_count": int(options["hit_count"]),
            "remaining": max(0, int(options["hit_count"]) - index),
            "damage_budget": int(options["damage_budget"]),
            "damage_allocated": sum(int(hit.get("damage", 0)) for hit in done),
            "declared": copy.deepcopy(pending.get("declared") or {}),
        }

    def _apply_attack_hits(self, pending: dict, hits: list) -> dict:
        """结算 pending 攻击里的接下来若干击；击数没用完就保持 pending 打开。"""
        options = pending["options"]
        refs = self.combat._combat_entity_refs()
        attacker = refs.get(options["actor_ref"])
        if attacker is None or not attacker.is_alive:
            return {"success": False, "error": "攻击者已失效"}
        legal = {entry["ref"]: entry for entry in options["target_options"]}
        offset = int(pending.get("hit_index", 0))
        hit_count = int(options["hit_count"])
        if not isinstance(hits, list):
            return {"success": False, "error": "hits必须是列表"}
        # 0 击的攻击（雕塑等）仍然占一次出手：允许空列表收尾，但不能拿它跳过余击。
        if not hits and hit_count - offset > 0:
            return {"success": False, "error": "至少要提交1击"}
        if len(hits) > hit_count - offset:
            return {"success": False,
                    "error": f"本次出手只剩{hit_count - offset}击可提交"}
        done = pending.setdefault("hits_done", [])
        speed_spend: dict[str, int] = {}
        blood_spend: dict[str, int] = {}
        for index, hit in enumerate(hits):
            absolute = offset + index + 1
            if not isinstance(hit, dict) or hit.get("target_ref") not in legal:
                return {"success": False, "error": f"第{absolute}击目标不在prepare快照中"}
            # 旧客户端未提交新字段时保持旧等伤分刀；新客户端可显式空刀/集中伤害。
            hit.setdefault("damage", attacker.effective_attack_power())
            hit.setdefault("parry", False)
            if (not isinstance(hit.get("damage"), int) or isinstance(hit.get("damage"), bool)
                    or hit["damage"] < 0):
                return {"success": False, "error": "每击damage必须是非负整数"}
            if (not isinstance(hit.get("dodge"), bool) or not isinstance(hit.get("parry"), bool)
                    or not isinstance(hit.get("blood_shadow"), bool)):
                return {"success": False, "error": "每击必须显式提交布尔值dodge、parry与blood_shadow"}
            if sum((hit["dodge"], hit["parry"], hit["blood_shadow"])) > 1:
                return {"success": False, "error": "同一次判定只能选择闪避、招架、血影之一"}
            ref = hit["target_ref"]
            target = refs.get(ref)
            if target is None:
                # 目标已经命零离场：这一击记成空刀，不再校验防御选择。
                continue
            if hit["dodge"] and self.combat.bizhong_remaining(attacker) <= offset + index:
                speed_spend[ref] = speed_spend.get(ref, 0) + 1
                if speed_spend[ref] > target.current_speed:
                    return {"success": False, "error": f"{target.name}速度不足以完成全部闪避"}
                if self.state._relic_active(target, "回锋刀"):
                    rr = hit.get("dodge_relic_target_ref")
                    allowed = {x["ref"] for x in legal[ref]["dodge_relic_target_options"]}
                    # 2026-11-02：1v1 只有一个敌对目标时无需再选，引擎按缺省目标
                    # 自动结算（见 CombatEngine._default_huifeng_target）；多目标才
                    # 要求显式提交，避免把 5 点伤害打到错误的对手身上。
                    if not rr and len(allowed) == 1:
                        pass
                    elif rr not in allowed:
                        return {"success": False,
                                "error": "回锋刀触发必须显式提交合法dodge_relic_target_ref"}
            if hit["blood_shadow"]:
                if not legal[ref]["can_blood_shadow"]:
                    return {"success": False, "error": f"{target.name}不能使用血影"}
                try:
                    _, owner_part, ally, ally_part = self.combat.validate_numeric_cost(
                        target, "生命", 10, hit.get("cost_share_target_ref", ""))
                except ValueError as exc:
                    return {"success": False, "error": str(exc)}
                for bearer, part in ((target, owner_part), (ally, ally_part)):
                    if bearer is None or part <= 0:
                        continue
                    key = id(bearer)
                    blood_spend[key] = blood_spend.get(key, 0) + part
                    if blood_spend[key] >= bearer.current_hp:
                        return {"success": False,
                                "error": f"{bearer.name}生命不足以支付全部血影分担"}
            self.combat.validate_spell_reaction_submission(
                target, attacker, hit.get("spell_choices"), refs,
            )
        # 伤害预算按「整手」累计，而不是只看本次提交：逐击提交也一样。
        allocated_damage = sum(int(hit.get("damage", 0)) for hit in done) + \
            sum(hit["damage"] for hit in hits)
        if allocated_damage > int(options["damage_budget"]):
            return {"success": False,
                    "error": f"分配总伤害{allocated_damage}超过上限{options['damage_budget']}"}
        # 闪避和招架都预付1速度；招架真刀会在单击结算后返还。
        # 逐击提交时，先前那几击已经真的花掉速度（current_speed 已减少），
        # 所以这里只校验本次提交，不会重复扣。
        for ref in legal:
            defense_spend = sum(1 for hit in hits if hit["target_ref"] == ref
                                and (hit["dodge"] or hit["parry"]))
            target = refs.get(ref)
            if target is not None and defense_spend > target.current_speed:
                return {"success": False, "error": f"{target.name}速度不足以完成全部防御"}

        duel_error = self._check_duel_turn_or_error(attacker)
        if duel_error:
            return duel_error
        caozuo_error = self.combat.caozuo_check(attacker, "攻击", hits)
        if caozuo_error:
            return caozuo_error
        spent_now = False
        if not pending.get("action_spent"):
            # 出手只扣一次：第一击落下时占掉这次主动出手，后面几击是同一次出手。
            budget_error = self._consume_action_or_error(attacker)
            if budget_error:
                return budget_error
            pending["action_spent"] = True
            spent_now = True
        # 「目标死亡时」按是否被选中判定，而不是是否命中；必须在结算前记账，
        # 否则首击击杀目标时，死亡钩子先于此处记账执行，会漏掉自愈触发。
        note_action_targets(self.state, attacker,
                            [refs.get(hit["target_ref"]) for hit in hits])
        results = []
        for index, hit in enumerate(hits):
            target = refs.get(hit["target_ref"])
            absolute = offset + index + 1
            if target is None or not target.is_alive:
                results.append({"hit_index": absolute, "skipped": "目标已命零"})
                continue
            result = self.combat.resolve_attack(
                attacker, target, dodge=hit["dodge"], parry=hit["parry"],
                allocated_damage=hit["damage"], blood_shadow=hit["blood_shadow"],
                spell_choices=hit.get("spell_choices") or {}, entity_refs=refs,
                dodge_relic_target_ref=hit.get("dodge_relic_target_ref"),
                cost_share_target_ref=hit.get("cost_share_target_ref", ""),
            )
            result["hit_index"] = absolute
            results.append(result)
        done.extend(hits)
        pending["hit_index"] = offset + len(hits)
        pending["declared"] = None
        finished = pending["hit_index"] >= hit_count
        view = self._attack_pending_view(pending)
        if finished:
            self.state.pending_attack = {}
            # 一手打完才交优先权。整手一次提交时由 _execute_action_core 统一推进
            # （本次已扣出手 → 不要在这里再推一次，否则会原地换回去）。
            if not spent_now:
                self._advance_duel_turn()
            if (self.state.in_final_duel
                    and not self._duel_side_can_act("player_side")
                    and not self._duel_side_can_act("opponent_side")):
                self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
        # 老文案「结算一轮攻击」= 整手一次提交；逐击提交带击数，方便战报看清进度。
        label = (f"{attacker.name}结算一轮攻击"
                 if finished and len(done) == hit_count
                 else f"{attacker.name}攻击结算{len(hits)}击")
        return {"success": True, "action": label,
                "result": {"attacker": attacker.name, "hits": results,
                           "finished": finished, **view}}

    @ACTION_REGISTRY.action("attack_declare_hit", group="combat")
    def _action_attack_declare_hit(self, params: dict) -> dict:
        """逐击第一阶段：攻击方只声明「这一击打谁、打多少」，伤害尚未公开结算。"""
        pending = self.state.pending_attack
        if not pending:
            return {"success": False, "error": "请先调用prepare_attack"}
        token = params.get("token")
        if (token is not None and token != pending.get("token")) or \
                pending.get("round") != self.state.current_round:
            return {"success": False, "error": "攻击token无效或已过期"}
        if pending.get("declared"):
            return {"success": False, "error": "这一击已声明，等目标选择承受/招架/闪避"}
        options = pending["options"]
        offset = int(pending.get("hit_index", 0))
        if offset >= int(options["hit_count"]):
            return {"success": False, "error": "本次出手的击数已用尽"}
        legal = {entry["ref"]: entry for entry in options["target_options"]}
        ref = str(params.get("target_ref") or "")
        if ref not in legal:
            return {"success": False, "error": "目标不在本次出手的合法列表里"}
        try:
            damage = int(params.get("damage", 0))
        except (TypeError, ValueError):
            return {"success": False, "error": "本击伤害X必须是整数"}
        if damage < 0:
            return {"success": False, "error": "本击伤害X不能是负数"}
        allocated = sum(int(hit.get("damage", 0)) for hit in pending.get("hits_done") or [])
        if allocated + damage > int(options["damage_budget"]):
            return {"success": False,
                    "error": f"本击后总伤害{allocated + damage}超过本回合伤害预算{options['damage_budget']}"}
        pending["declared"] = {"target_ref": ref, "damage": damage}
        return {"success": True, "action": "声明本击",
                "result": {"hit_index": offset + 1,
                           **self._attack_pending_view(pending),
                           "target": self._attack_defender_options(pending, ref)}}

    @ACTION_REGISTRY.action("attack_respond_hit", group="combat")
    def _action_attack_respond_hit(self, params: dict) -> dict:
        """逐击第二阶段：目标选择承受/招架/闪避/血影，本击立刻公开结算。"""
        pending = self.state.pending_attack
        if not pending:
            return {"success": False, "error": "没有进行中的攻击"}
        declared = pending.get("declared")
        if not declared:
            return {"success": False,
                    "error": "先由攻击方分配伤害（声明这一击的目标与 X）"}
        choice = str(params.get("choice") or "")
        if choice not in {"tank", "parry", "dodge", "blood_shadow"}:
            return {"success": False, "error": "应对方式只能是 硬扛/招架/闪避/血影"}
        ref = declared["target_ref"]
        legal = {entry["ref"]: entry for entry in pending["options"]["target_options"]}
        entry = legal.get(ref) or {}
        defender = self._attack_defender_options(pending, ref)
        if choice == "dodge" and not defender["can_dodge"]:
            return {"success": False,
                    "error": f"{entry.get('name')}当前不能闪避"
                             f"（剩余速度 {defender['speed_left']}）"}
        if choice == "parry" and not defender["can_parry"]:
            return {"success": False,
                    "error": f"{entry.get('name')}当前不能招架"
                             f"（剩余速度 {defender['speed_left']}）"}
        if choice == "blood_shadow" and not defender["can_blood_shadow"]:
            return {"success": False, "error": f"{entry.get('name')}现在不能使用血影"}
        spell_choices = {
            timing: {spell["spell_name"]: {"use": False} for spell in spells}
            for timing, spells in (entry.get("spell_options") or {}).items()}
        hit = {"target_ref": ref, "damage": declared["damage"],
               "dodge": choice == "dodge", "parry": choice == "parry",
               "blood_shadow": choice == "blood_shadow",
               "spell_choices": copy.deepcopy(spell_choices)}
        for key in ("cost_share_target_ref", "dodge_relic_target_ref"):
            if params.get(key):
                hit[key] = params[key]
        applied = self._apply_attack_hits(pending, [hit])
        if not applied.get("success"):
            return applied
        applied["result"]["defender"] = defender
        applied["result"]["choice"] = choice
        return applied

    @ACTION_REGISTRY.action("resolve_attack", group="combat")
    def _action_resolve_attack(self, params: dict) -> dict:
        """批量提交：一次交出接下来若干击的完整选择（旧客户端/AI 仍可整手提交）。"""
        pending = self.state.pending_attack
        if not pending:
            return {"success": False, "error": "请先调用prepare_attack"}
        if params.get("token") != pending.get("token") or \
                pending.get("round") != self.state.current_round:
            return {"success": False, "error": "攻击token无效或已过期"}
        hits = params.get("hits")
        if not isinstance(hits, list):
            return {"success": False, "error": "hits必须是列表"}
        return self._apply_attack_hits(pending, hits)

    @ACTION_REGISTRY.action("focus", group="combat")
    def _action_focus(self, params: dict) -> dict:
        """聚能：消耗1次主动出手，恢复50%已损法力，向上取整。"""
        actor_ref = params.get("actor_ref", "player:0")
        refs = self.combat._combat_entity_refs()
        actor = refs.get(actor_ref)
        if actor is None or not actor.is_alive:
            return {"success": False, "error": "actor_ref不是当前存活行动者"}
        duel_err = self._check_duel_turn_or_error(actor)
        if duel_err:
            return duel_err
        if not self.combat.can_act(actor):
            return {"success": False, "error": f"{actor.name}当前无法行动"}
        # 出手机会校验
        budget_err = self._consume_action_or_error(actor)
        if budget_err:
            return budget_err
        # 恢复已损法力的50%，向上取整；满法力时聚能不产生资源。
        import math
        missing_mana = max(0, actor.mana_limit - actor.current_mana)
        gain = math.ceil(missing_mana * 50 / 100)
        if gain <= 0:
            return {"success": True, "action": f"{actor.name}聚能",
                    "result": {"actor": actor.name, "mana_gained": 0,
                               "mana_before": actor.current_mana,
                               "mana_after": actor.current_mana,
                               "note": f"{actor.name}当前法力已满，聚能未获法力"}}
        mana_before = actor.current_mana
        # 2026-10-08：走统一入口，【勾魂】（无法获得[法力]）期间增益不生效
        gained = actor.gain_mana(gain)
        return {"success": True, "action": f"{actor.name}聚能",
                "result": {"actor": actor.name, "mana_gained": gained,
                           "mana_before": mana_before,
                           "mana_after": actor.current_mana}}

    @ACTION_REGISTRY.action("declare_parry", group="combat")
    def _action_declare_parry(self, params: dict) -> dict:
        """兼容旧调用；招架现已改为每一击在 resolve_attack 中提交。"""
        return {"success": False,
                "error": "招架不再是回合姿态；请在resolve_attack的对应hit中提交parry=true"}

    # ---------- 2026-09-28 新回合动作：cast/focus/rest ----------

    @ACTION_REGISTRY.action("declare_wish", group="combat")
    def _action_declare_wish(self, params: dict) -> dict:
        """声明许愿（消耗玩家1出手）：轮回者向"某人"祈求。

        愿望没有固定的可行范围，也不存在"无法实现"的愿望；"某人"会以能够
        实现愿望、但最符合其扭曲本质的方式实现；代价与扭曲方式由愿望本身
        决定，不预先公开。引擎只提交现场状态，实现方式与代价由 DM 裁定。
        """
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        wish_text = params.get("wish_text", "")
        if not isinstance(wish_text, str) or not wish_text.strip():
            return {"success": False, "error": "必须写明许下的愿望（wish_text）"}
        wish_text = wish_text.strip()
        target = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
        if params.get("target_ref") and not target:
            return {"success": False, "error": "target_ref不是当前合法目标"}

        budget_error = self._consume_action_or_error(player)
        if budget_error:
            return budget_error

        interrupt = self.combat.initiate_wish(player, wish_text, target)
        self._pending_interrupts.append(interrupt)

        return {
            "success": True,
            "action": "声明许愿",
            "interrupt": interrupt.to_dict(),
            "instruction": "需要DM裁定「某人」以何种扭曲方式实现愿望及轮回者付出的代价"
        }

    ESCAPE_ACTION_COST = 5

    @ACTION_REGISTRY.action("declare_escape", group="combat")
    def _action_declare_escape(self, params: dict) -> dict:
        """逃跑（2026-10-13 改制）：轮回者行动，一次付清5次出手，
        以当前状态强制脱离本场战斗，不再走DM追击裁定。

        前置：非最终死斗；未持有生效中的【不离】（所有角色无法离场）；
        本轮剩余出手≥5（原子消费，不足则一次不扣）。
        脱战清理口径沿用原【买路财】撤退：敌方清空、造物解散、员工撤部署、
        朋友/员工撤退标记重置、精力回满、阶段回 pre_battle；不产生击杀碎片奖励。
        """
        escaper = self.state.player
        if not escaper:
            return {"success": False, "error": "没有玩家"}
        blocked = self._escape_blocked_reason()
        if blocked:
            return {"success": False, "error": blocked}

        budget_error = self._consume_actions_or_error(
            escaper, self.ESCAPE_ACTION_COST, action_label="逃跑")
        if budget_error:
            return budget_error

        self._force_disengage(reason="逃跑")
        return {
            "success": True, "action": "逃跑·强制脱战",
            "result": {"actions_paid": self.ESCAPE_ACTION_COST,
                       "actions_used": escaper.actions_used_this_round,
                       "note": "本场战斗结束，未击败任何怪物，不产生击杀碎片奖励"},
        }

    @ACTION_REGISTRY.action("declare_evolution", group="combat")
    def _action_declare_evolution(self, params: dict) -> dict:
        """怪物进化：发动【原初X】借用原始怪物道纹（引擎直接结算，无需DM中断）"""
        monster_name = params.get("monster", "")
        # 同名重复抽 + 封印尸体仍留在 state.enemies：必须跳过已命零/已移出的，
        # 否则会命中第一具尸体并报「已命零」，活着的同名困境怪永远进化不了。
        monster = next((e for e in self.state.enemies
                        if e.name == monster_name and self.state.enemy_combat_active(e)), None)

        if not monster:
            return {"success": False, "error": f"找不到存活的怪物: {monster_name}"}

        daowen_name = params.get("daowen", "")
        try:
            x = int(params.get("x", 1))
        except (TypeError, ValueError):
            return {"success": False, "error": "X必须为整数"}

        return self.combat.execute_evolution(monster, daowen_name, x)

    @ACTION_REGISTRY.action("prepare_monster_phase", group="combat")
    def _action_prepare_monster_phase(self, params: dict) -> dict:
        """第一阶段：只返回合法选项，绝不替AI选择道纹、目标或闪避。

        只枚举当前敌方优先权下的一个真实行动；侧回终由中央行动路径
        在该方确实结束或耗尽本回合行动能力时结算，不在准备阶段提前触发。
        """
        if self.state.pending_monster_phase:
            return {"success": False, "error": "已有待提交的怪物阶段决策"}
        # 空优先权＝尚无优先权记录（手工构造的战斗、旧存档、回始前的中间态）：
        # 既然此刻是在准备敌方行动，就按「敌方持有优先权」记录，避免出现
        # 「当前轮到，」这类无主语拒绝；正常战斗在 battle_start 已写入 duel_turn。
        if not self.state.duel_turn:
            self.state.duel_turn = "opponent_side"
        # 敌方这一侧根本没法出手（没有参战者、已被清场、或本回合余手用尽）：
        # 没有可准备的行动，按【跳过】结束敌方回合即可。若不先判这一步，
        # `_normalize_priority()` 会把优先权归位给玩家，紧接着的闸门就以
        # 「当前轮到player_side」拒绝——而调用方（驱动/夹具）在「场上已无怪」时
        # 准备怪物阶段是完全合法的收尾步骤。
        if not self._duel_side_can_act("opponent_side"):
            # 优先权确实在敌方这一侧、而敌方拿不出行动：按【跳过】把它的回合收掉，
            # 由 _advance_priority_skip 统一处理子阶段（双方都无余手 → await_round_end）。
            self.state.duel_turn = "opponent_side"
            self._advance_priority_skip()
            return {"success": True, "action": "敌方无可用出手，按【跳过】结束其回合",
                    "skipped": True,
                    "result": {"token": None, "spell_logs": [], "skipped": [], "actors": [],
                               "note": "敌方这一侧没有可行动者；无需resolve_monster_phase"},
                    "instruction": "继续当前优先权，或双方余手用尽后进入回终"}
        # 优先权只属于还能出手的一方：对方没有参战者/已用尽出手时自动归位。
        self._normalize_priority()
        # 优先权闸门对所有战斗一致：只有敌方持有优先权时才能准备怪物行动。
        # 观众（`in_combat=False`）不产生出手权，因此它的场景天然不会把优先权
        # 交到玩家侧——不需要按模式开口子。
        if (self.state.combat_subphase == CombatSubphase.AWAIT_ROUND_END.value
                and not self._duel_side_can_act("player_side")
                and not self._duel_side_can_act("opponent_side")):
            return {"success": True, "action": "双方均已无可用出手，进入回终",
                    "skipped": True,
                    "result": {"token": None, "spell_logs": [], "skipped": [], "actors": []}}
        if (self.state.duel_turn != "opponent_side"
                or "opponent_side" in self.combat._ended_turn_sides):
            return {"success": False,
                    "error": f"交替出手：当前轮到{self.state.duel_turn}，不能准备怪物行动"}
        # 侧回终不在prepare/每次单行动后重复触发，统一由中央行动路径按
        # “该侧确实失去本回合行动能力”结算。
        spell_logs = []
        options = self.combat.prepare_monster_phase()
        if not options["actors"]:
            self._advance_priority_skip()
            return {"success": True, "action": "敌方无合法行动并跳过", "skipped": True,
                    "result": {"token": None, "spell_logs": spell_logs,
                               "skipped": options["skipped"], "actors": [],
                               "note": "没有可支付道纹或合法攻击目标；按【跳过】结束敌方回合"},
                    "instruction": "无需resolve_monster_phase；继续当前优先权或回终"}
        token = uuid.uuid4().hex
        self.state.pending_monster_phase = {"token": token, "round": self.state.current_round,
                                            "options": options}
        self.state.combat_subphase = CombatSubphase.MONSTER_ACTIONS.value
        return {"success": True, "action": "准备怪物阶段",
                "result": {"token": token, "spell_logs": spell_logs, **options},
                "instruction": "请为每个actors条目提交完整选择后调用resolve_monster_phase"}

    @ACTION_REGISTRY.action("resolve_monster_phase", group="combat")
    def _action_resolve_monster_phase(self, params: dict) -> dict:
        """第二阶段：验证完整选择与一次性token，通过后统一结算。

        提交失败时战斗状态由事务整体回滚（零副作用），pending保持有效：
        AI用同一token修正后重交即可，失败提交不得把战斗锁死在怪物阶段
        。仅当prepare快照本身失效（回合变化）时
        回滚pending并还原子阶段，让"重新prepare"成为可执行的恢复路径。
        """
        pending = self.state.pending_monster_phase
        if not pending:
            return {"success": False, "error": "请先调用prepare_monster_phase"}
        if params.get("token") != pending.get("token"):
            return {"success": False, "error": "怪物阶段token无效或已过期",
                    "recoverable": True, "token": pending.get("token"),
                    "instruction": "请使用上方当前有效token重新调用resolve_monster_phase"}
        if pending.get("round") != self.state.current_round:
            # 快照已跨回合失效：普通重提无意义，回滚pending让重新prepare可执行
            self._roll_back_monster_phase_pending()
            return {"success": False, "error": "回合已变化，请重新prepare_monster_phase",
                    "recoverable": True,
                    "instruction": "回合已变化，怪物阶段pending已回滚；"
                                   "请重新调用prepare_monster_phase获取最新token与合法选项"}
        try:
            results = self.combat.resolve_monster_phase(
                params.get("choices"), prepared=pending["options"],
            )
        except Exception as exc:
            # 状态已回滚、pending保持有效：同token修正后重交即可（不要重新prepare）
            return {"success": False, "error": str(exc), "recoverable": True,
                    "token": pending.get("token"),
                    "instruction": "提交被拒绝且已整体回滚（零副作用），token仍有效；"
                                   "请修正选择后用同一token重新调用resolve_monster_phase"}
        self.state.pending_monster_phase = {}
        # 【敌回终】由中央侧回终结算在该方真正失去行动能力时触发；
        # 单个怪物行动结束不等于整个敌方回合结束。
        enemy_round_end_spells = []
        # 一次 resolve 只结算 1 个行动者的 1 个行动（严格交替），所以结算后的子阶段
        # 只由一条通用规则决定：任一侧还有能出手的参战者，就回到 player_actions
        # 继续交替；双方都没有余手才进入 await_round_end。
        # 死斗、养蛊场、普通战斗共用这一条——不再按模式分支。
        if self._duel_side_can_act("player_side") or self._duel_side_can_act("opponent_side"):
            self.state.combat_subphase = CombatSubphase.PLAYER_ACTIONS.value
        else:
            self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
        player_dead = (self.state.player is None) or (not self.state.player.is_alive)
        return {
            "success": True,
            "action": "结算怪物阶段",
            "result": {"entries": len(results), "player_dead": player_dead,
                       "player_hp": self.state.player.current_hp if self.state.player else 0,
                       "details": results, "spell_logs": enemy_round_end_spells},
        }

    @ACTION_REGISTRY.action("monster_phase", group="combat")
    def _action_monster_phase(self, params: dict) -> dict:
        """拒绝旧版固定策略入口，防止绕过两阶段AI显式决策。"""
        return {"success": False,
                "error": "monster_phase已停用；请依次调用prepare_monster_phase与resolve_monster_phase"}

    @ACTION_REGISTRY.action("resolve_ally_phases", group="combat")
    def _action_resolve_ally_phases(self, params: dict) -> dict:
        """一名无命令友方自主结算一手；不得在单次调用中批量行动。"""
        enemies = [e for e in self.state.enemies if e.is_alive]
        if not enemies:
            return {"success": True, "action": "朋友/员工自主行动",
                    "result": {"acted_count": 0, "allies": []}}
        candidate = None
        for prefix, entities in (("friend", self.state.friends),
                                 ("employee", self.state.employees)):
            for index, ally in enumerate(entities):
                if not ally.is_alive or ally.has_retreated:
                    continue
                if prefix == "employee" and not ally.is_deployed:
                    continue
                if self.combat.chenni_round_blocked(ally) or not self.combat.can_act(ally):
                    continue
                if ally.actions_used_this_round >= self._action_budget_of(ally):
                    continue
                candidate = (prefix, index, ally)
                break
            if candidate is not None:
                break
        if candidate is None:
            return {"success": True, "action": "朋友/员工自主行动",
                    "result": {"acted_count": 0, "allies": []}}

        prefix, index, ally = candidate
        ally_ref = f"{prefix}:{index}"
        target = min(enemies, key=lambda e: e.current_hp)
        entry = {"ally": ally.name, "actions": [],
                 "consumables": auto_use_consumables(self.combat, ally)}
        cast = None
        for name, inst in ally.dao_wen.items():
            # 背负：自我伤害类，自主角色不得自行发动。
            # 透支：只把资源换成法力、不改变战场，自主角色不为此花掉唯一次出手
            # （2026-11-01 出生自带给每个人之后，不排除会挤掉真正的攻击）。
            if not ally.can_use_daowen(name) or name in {"背负", "透支"}:
                continue
            daowen_x = self._ally_daowen_x(ally, name, target)
            if daowen_x < 1:
                continue
            use = self._execute_nested_action("use_daowen", {
                "actor_ref": ally_ref, "daowen_name": name, "x": daowen_x,
                "target_ref": f"enemy:{self.state.enemies.index(target)}",
                "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}})
            if use.get("success"):
                cast = {"kind": "daowen", "name": name, "target": target.name,
                        "x": daowen_x, "detail": use.get("result")}
                break
        if cast is None:
            prepared = self._execute_nested_action(
                "prepare_attack", {"actor_ref": ally_ref})
            if not prepared.get("success"):
                return prepared
            option = next(
                (o for o in prepared["result"]["target_options"]
                 if o["ref"] == f"enemy:{self.state.enemies.index(target)}"),
                prepared["result"]["target_options"][0])
            hits = [{
                "target_ref": option["ref"], "dodge": False, "blood_shadow": False,
                "spell_choices": {
                    timing: {sp["spell_name"]: {"use": False}
                             for sp in option.get("spell_options", {}).get(timing, [])}
                    for timing in ("before", "after", "damage_after", "life_before")
                },
            } for _ in range(prepared["result"]["hit_count"])]
            resolved = self._execute_nested_action("resolve_attack", {
                "token": prepared["result"]["token"], "hits": hits})
            if not resolved.get("success"):
                return resolved
            cast = {"kind": "attack", "target": target.name,
                    "detail": resolved.get("result")}
        entry["actions"].append(cast)
        return {"success": True, "action": "朋友/员工自主行动",
                "result": {"acted_count": 1, "allies": [entry]}}

    @ACTION_REGISTRY.action("command_ally", group="combat")
    def _action_command_ally(self, params: dict) -> dict:
        """轮回者用语言命令[朋友]/[员工]行动（攻击/发动道纹/护卫/黄符施法）。

        指令格式（白名单，非命中即拒绝，不猜测）：
          「攻击 <目标名>」「打 <目标名>」 → 一轮攻击
          「发动 <道纹名>」「用 <道纹名> [打/对 <目标名>]」 → 发动道纹
          「发动 <法术名> [打 <目标名>]」+ spell_steps → 施放【黄符】授予的法术
            （2026-10-14 审计F4；法术须经黄符获授且在本场有效）
          「护卫 [X]」「保护我 [X]」「挡伤 [X]」「替我挡 [X]」 → 无消耗强制护卫：
            该[朋友]/[员工]替轮回者承担下X次受到的伤害（X默认1，1~9）。
            机制=对盟友强制施加背负标记（复用护卫标记的伤害重定向），
            不消耗盟友出手、不消耗法力，视为轮回者的指挥能力。
        目标名缺省时取当前存活敌人中当前生命最少者。
        """
        ally_ref = params.get("ally_ref", "")
        instruction = params.get("instruction", "")
        if not isinstance(instruction, str) or not instruction.strip():
            return {"success": False,
                    "error": "指令为空；合法格式：攻击 <目标名> / 发动 <道纹名> / 护卫 [X]"}
        instruction = instruction.strip()
        refs = self.combat._combat_entity_refs()
        ally = refs.get(ally_ref)
        if ally is None or not ally.is_alive:
            return {"success": False, "error": "ally_ref必须指向一名存活[朋友]/[员工]"}
        if ally.entity_type not in ("朋友", "员工"):
            return {"success": False, "error": "只有[朋友]/[员工]可被语言命令"}
        if ally.has_retreated:
            return {"success": False, "error": f"{ally.name}已撤退"}

        # ---- 护卫指令：无消耗强制挡伤（对盟友施加背负标记，复用重定向逻辑） ----
        for verb in self._ALLY_GUARD_VERBS:
            if instruction.startswith(verb):
                rest = instruction[len(verb):].strip()
                x = 1
                if rest:
                    try:
                        x = int(rest)
                    except (TypeError, ValueError):
                        return {"success": False,
                                "error": f"护卫次数必须是1~9的整数：{rest!r}"}
                if not (1 <= x <= 9):
                    return {"success": False, "error": "护卫次数必须是1~9的整数"}
                player = self.state.player
                if player is None or not player.is_alive:
                    return {"success": False, "error": "轮回者不在场，无法护卫"}
                # 强制施加背负：盟友下X次替轮回者承担伤害（无消耗，不占盟友出手）
                # 存 runtime_id 而非实体引用
                ally._beifu_left = max(getattr(ally, "_beifu_left", 0) or 0, x)
                ally._beifu_target = player.runtime_id
                ally.add_status(StatusEffect(name="背负", value=x, remaining_rounds=-1,
                                            source=player.name))
                player.add_status(StatusEffect(name="被背负", value=x, remaining_rounds=-1,
                                               source=ally.name))
                return {"success": True, "action": f"命令{ally.name}护卫轮回者",
                        "result": {"ally": ally.name, "guard_times": x,
                                   "note": f"{ally.name}将替轮回者承担下{x}次受到的伤害"
                                           f"（无消耗强制施加，剩余{ally._beifu_left}次）"}}

        enemies = [e for e in self.state.enemies if e.is_alive]
        if not enemies:
            return {"success": False, "error": "场上没有存活敌人"}

        def _resolve_target(name_hint: str = "", allow_allies: bool = False):
            """解析命令目标。allow_allies=False（攻击指令）只允许敌方；
            allow_allies=True（道纹指令）允许全场存活单位（轮回者/朋友/员工/怪物），
            使「发动再生 对 轮回者」这类治疗命令可行。
            「轮回者/我/我自己」是玩家别名（道纹指令保护队友时用）。"""
            if allow_allies and name_hint in ("轮回者", "我", "我自己"):
                pl = self.state.player
                return pl if (pl and pl.is_alive) else None
            if name_hint:
                pool = list(refs.values()) if allow_allies else list(enemies)
                matches = [e for e in pool if e.name == name_hint and e.is_alive]
                return matches[0] if len(matches) == 1 else None
            return min(enemies, key=lambda e: e.current_hp)

        # ---- 攻击指令：必须指令开头就是攻击动词（"攻击 X"或"打 X"） ----
        for verb in self._ALLY_ATTACK_VERBS:
            if instruction.startswith(verb):
                rest = instruction.split(verb, 1)[1].strip()
                target = _resolve_target(rest)
                if target is None:
                    return {"success": False,
                            "error": f"找不到目标「{rest}」；合法格式：攻击 <目标名>"}
                prepared = self._execute_nested_action("prepare_attack", {"actor_ref": ally_ref})
                if not prepared.get("success"):
                    return prepared
                option = next((o for o in prepared["result"]["target_options"]
                               if o["ref"] == f"enemy:{self.state.enemies.index(target)}"), None)
                if option is None:
                    option = prepared["result"]["target_options"][0]
                hits = [{"target_ref": option["ref"], "dodge": False, "blood_shadow": False,
                         "spell_choices": {timing: {sp["spell_name"]: {"use": False}
                                                    for sp in option.get("spell_options", {}).get(timing, [])}
                                           for timing in ("before", "after", "damage_after", "life_before")}}
                        for _ in range(prepared["result"]["hit_count"])]
                resolved = self._execute_nested_action("resolve_attack", {
                    "token": prepared["result"]["token"], "hits": hits})
                return {"success": True, "action": f"命令{ally.name}攻击{target.name}",
                        "result": {"ally": ally.name, "instruction": instruction,
                                   "attacked": target.name, "detail": resolved.get("result")}}
        # ---- 道纹指令：必须指令开头就是道纹动词；中间的"打/对"是目标分隔符 ----
        for verb in self._ALLY_DAOWEN_VERBS:
            if instruction.startswith(verb):
                rest = instruction.split(verb, 1)[1].strip()
                daowen = rest.split()[0] if rest else ""
                target_name = ""
                for sep in ("打", "对"):
                    if sep in rest:
                        target_name = rest.split(sep, 1)[1].strip()
                        break
                if daowen not in ally.dao_wen:
                    # 2026-10-14 审计F4：【黄符】施法——「发动 <法术名>」。
                    # 须经黄符获授（授权在本场有效）；steps 由 spell_steps 结构化提交。
                    granted = next((sp for sp in ally.spells if sp.name == daowen), None)
                    if granted is not None and (ally.runtime_id, daowen) in self.state.huangfu_grants():
                        steps = params.get("spell_steps")
                        if not isinstance(steps, list) or not steps:
                            return {"success": False,
                                    "error": f"命令{ally.name}施放「{daowen}」须用spell_steps提交每步决策"}
                        target = _resolve_target(target_name, allow_allies=True)
                        if target is None:
                            return {"success": False,
                                    "error": f"找不到目标「{target_name}」；合法格式：发动 <法术名> 打 <目标名>"}
                        spell_target_ref = next((ref for ref, ent in refs.items() if ent is target), "")
                        cast = self._execute_nested_action("cast", {
                            "actor_ref": ally_ref, "huangfu_spell": daowen,
                            "target_ref": spell_target_ref, "steps": steps,
                            "max_iterations": params.get("spell_max_iterations")})
                        if not cast.get("success"):
                            return cast
                        return {"success": True, "action": f"命令{ally.name}施放{daowen}（黄符）",
                                "result": {"ally": ally.name, "instruction": instruction,
                                           "spell": daowen, "target": target.name,
                                           "detail": cast.get("result")}}
                    return {"success": False,
                            "error": f"{ally.name}未持有道纹「{daowen}」；持有：{list(ally.dao_wen)}"}
                # 道纹指令允许指定我方目标（再生/庇护保护队友），按全场解析
                target = _resolve_target(target_name, allow_allies=True)
                if target is None:
                    return {"success": False,
                            "error": f"找不到目标「{target_name}」；合法格式：发动 <道纹名> 打 <目标名>"
                                     "（保护类道纹可指向轮回者/队友）"}
                target_ref = next((ref for ref, ent in refs.items() if ent is target), "")
                daowen_x = self._ally_daowen_x(ally, daowen, target)
                if daowen_x < 1:
                    return {"success": False,
                            "error": f"{ally.name}此刻不宜发动「{daowen}」"
                                     f"（一池制余量不足或代价付不起）"}
                use = self._execute_nested_action("use_daowen", {
                    "actor_ref": ally_ref, "daowen_name": daowen, "x": daowen_x,
                    "target_ref": target_ref,
                    "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}})
                if not use.get("success"):
                    return use
                return {"success": True, "action": f"命令{ally.name}发动{daowen}",
                        "result": {"ally": ally.name, "instruction": instruction,
                                   "daowen": daowen, "target": target.name,
                                   "detail": use.get("result")}}
        return {"success": False,
                "error": "无法解析指令；合法格式：攻击 <目标名> 或 发动 <道纹名> [打 <目标名>]"}

    @ACTION_REGISTRY.action("round_start", group="combat")
    def _action_round_start(self, params: dict) -> dict:
        """回始；完全平局死斗在每个新回合交换该回合首手方。"""
        relic_choices = params.get("relic_choices", {})
        self.combat.validate_round_start_relic_choices(relic_choices)
        result = self.combat.round_start(relic_choices)
        # 时间的偏爱把冷却改为战斗回合计时：每次回始递减，归零后即可发动。
        round_entities = {entity.runtime_id: entity for entity in (
            self.state.get_all_player_side() + self.state.get_all_enemy_side()
            + list(getattr(self.state, "temp_friends", []) or []))}.values()
        for entity in round_entities:
            if self.combat.chenni_round_blocked(entity):
                continue
            for instance in entity.dao_wen.values():
                if getattr(instance, "cooldown_rounds_remaining", 0) > 0:
                    instance.cooldown_rounds_remaining -= 1
        # 全局法术【回始】：普通战斗与死斗双方共用同一个回始，因此持有者扫描
        # 覆盖玩家侧与死斗对手侧；死斗对手视角下这就是其"敌回始"，见下方
        # _action_prepare_monster_phase/_action_resolve_monster_phase 的普通
        # 战斗敌回始接线，以及 round_start 本身对死斗对手来说是"己方回始"。
        result["spell_logs"] = self._resolve_global_trigger_spells_for_action(
            TriggerTiming.ROUND_START.value, params)
        self.state.combat_subphase = CombatSubphase.PLAYER_ACTIONS.value
        if self.state.in_final_duel and self.state.duel_tie_alternating:
            if self.state.duel_rounds_started > 0:
                self.state.duel_round_first = (
                    "opponent_side" if self.state.duel_round_first == "player_side" else "player_side"
                )
            self.state.duel_rounds_started += 1
            self.state.duel_turn = self.state.duel_round_first
            result["duel_round_first"] = self.state.duel_round_first
            result["duel_tie_alternating"] = True
        return {"success": True, "action": "回始", "result": result}

    @ACTION_REGISTRY.action("round_end", group="combat")
    def _action_round_end(self, params: dict) -> dict:
        """回终"""
        # 全局法术【回终】必须在 combat.round_end() 真正清空法力/格挡之前结算——
        # "回终"是"回合即将结束"的那一刻，法术若挂在这个时点，理应还能花费
        # 本回合剩余的法力（否则「回终」这个时机会因为法力恒为0而变成实质上
        # 的伪接线，等同于没有真正实现）。这与"受到伤害前"必须在伤害落地前
        # 触发是同一个道理：时点名字里的"终/前"表示的是决策窗口的位置，
        # 不是"所有清算都做完之后才轮到法术"。
        spell_logs = self._resolve_global_trigger_spells_for_action(
            TriggerTiming.ROUND_END.value, params)
        result = self.combat.round_end(
            params.get("blood_lineage_cost_share_target_ref", ""))
        result["spell_logs"] = spell_logs
        self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_START.value

        # 提取多路径胜利结果（已由 combat.round_end 结算）
        alt_paths = [e for e in result.get("effects", [])
                     if isinstance(e, dict) and e.get("type") in
                     ("sculpture", "proliferation", "cancer", "debt_bind", "redemption")]

        # 检查怪物困境
        difficulties = []
        for monster in self.state.enemies:
            if monster.is_alive:
                diff = self.combat.check_monster_difficulty(monster)
                if diff:
                    difficulties.append(diff)

        return {
            "success": True,
            "action": "回终",
            "result": result,
            "victory_paths": alt_paths,
            "monster_difficulties": difficulties,
            "note": "多路径胜利（雕塑/癌变/还债）已结算；消耗品可在后续回合使用"
        }

    @ACTION_REGISTRY.action("restart_battle", group="combat")
    def _action_restart_battle(self, params: dict) -> dict:
        """恢复到本场战始前并按当前规则重新开始；不产生战斗奖励或战终结算。"""
        snapshot = self._battle_restart_snapshot
        if not snapshot:
            return {"success": False, "error": "本场没有可用的战始快照，无法重来"}
        self.state = copy.deepcopy(snapshot["state"])
        self.dice = copy.deepcopy(snapshot["dice"])
        self.combat.state = self.state
        self.combat.dice = self.dice
        register_combat_event_observer(self.state, self.combat)
        self._restore_combat_runtime(copy.deepcopy(snapshot["combat_runtime"]))
        # 重来本场时丢弃旧战斗遗留的中断、pending 和上次结果。
        self._pending_interrupts = []
        self._last_result = None
        start_params = copy.deepcopy(snapshot.get("battle_start_params") or {})
        result = self._action_battle_start(start_params)
        if not result.get("success"):
            return result
        result["action"] = "重来本场战斗"
        result["restart"] = True
        return result

    @ACTION_REGISTRY.action("battle_start", group="combat")
    def _action_battle_start(self, params: dict) -> dict:
        """战始：配方式出怪(总数N=随机(1,上界)、首发S=随机(1,N)、增援R_i/T_i 随机；
        上界：一阶=战斗场数-3最低1，二阶及以上=12；允许重复抽选同一怪物种族)→结算战始遗物。
        配方口径唯一事实源见 `engine/monsters.py::roll_spawn_plan`。
        战斗背景：文档"战斗背景：（名称与影响）"仅为战斗推演格式里的占位提示，
        正文未定义任何具体名称与机制效果，故本引擎不做机制化处理，留给叙事层自由发挥。"""
        from ..monsters import (make_monster_entity, roll_spawn_plan,
                               scale_monster_def_for_endless, assign_monster_preferences)
        relic_choices = params.get("relic_choices", {})
        # 自定义启用（2026-10-22 用户令）：[战始]可提交 active_relics 选定
        # 本场生效的遗物（持有占页，生效数不另限），必须在战始遗物结算之前先生效。
        if params.get("active_relics") is not None:
            ok, error = self.state.set_active_relics(params.get("active_relics"))
            if not ok:
                return {"success": False, "error": error}
        # 先完成全部静态校验，再抽怪；非法遗物参数不得消耗正式随机源。
        self.combat.validate_battle_start_relic_choices(relic_choices)
        world_encounter_id = params.get("world_encounter_id")
        world_battle = bool(world_encounter_id)
        # 演武场「直接对战」：调用方（webapp/lab.py）已造好对手实体，随战始注入，
        # 跳过随机出怪。只给工具层用，正式对局永远不传这个参数。
        injected_enemies = list(params.get("inject_enemies") or [])
        if injected_enemies and world_battle:
            return {"success": False,
                    "error": "inject_enemies 不能与 world_encounter_id 同时使用"}
        if any(not getattr(entity, "is_alive", False) for entity in injected_enemies):
            return {"success": False, "error": "inject_enemies 含已死亡单位"}
        world_participant_enemies = []
        if world_battle:
            try:
                world = self._world_from_state()
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            encounter = world.encounters.get(world_encounter_id)
            player_id = getattr(self.state.player, "runtime_id", "") if self.state.player else ""
            if encounter is None or not encounter.active or player_id not in encounter.participants:
                return {"success": False, "error": "world_encounter_id必须指向包含当前轮回者的有效地图遭遇"}
            enemy_by_id = {entity.runtime_id: entity for entity in self.state.enemies}
            companion_ids = {
                entity.runtime_id
                for group_name in ("friends", "employees", "temp_friends")
                for entity in (getattr(self.state, group_name, []) or [])
            }
            participant_ids = [actor_id for actor_id in encounter.participants if actor_id != player_id]
            if (not participant_ids
                    or any(actor_id not in enemy_by_id and actor_id not in companion_ids
                           for actor_id in participant_ids)):
                return {"success": False,
                        "error": "地图遭遇含有未映射到轮回者、队友或敌方实体的参与者"}
            # 友方地图单位可被同场遭遇记录，但由现有战斗阵营规则决定是否实际参战；
            # 只有敌方实体从敌方列表中移入本次战斗，避免把队友误当敌人。
            world_participant_enemies = [
                enemy_by_id[actor_id] for actor_id in participant_ids
                if actor_id in enemy_by_id
            ]
            if not world_participant_enemies:
                return {"success": False, "error": "地图遭遇没有可交给战斗引擎的敌方实体"}
            if any(not entity.is_alive for entity in world_participant_enemies):
                return {"success": False, "error": "地图遭遇包含已死亡的敌方单位"}
            self.state.world_enemy_reserve = [
                entity for entity in self.state.enemies
                if entity.runtime_id not in set(participant_ids)
            ]
            self.state.world_encounter_id = world_encounter_id
            self.state.world_encounter_entities = list(world_participant_enemies)
        # 妖怪默认留在地图上，只有战始显式选择召唤且位于五格可通行路径内才参战。
        created_beings = owned_yaoguai(self.state)
        for being in created_beings:
            being.is_summoned = False
        if params.get("summon_created_being", False):
            if not world_battle:
                return {"success": False, "error": "妖怪召唤仅适用于开放世界地图遭遇"}
            if not created_beings:
                return {"success": False, "error": "当前没有可召唤的存活妖怪"}
            if "summon_created_being_target" in params:
                being = find_owned_yaoguai(
                    self.state, params.get("summon_created_being_target"))
            else:
                being = next((entity for entity in created_beings if entity.is_alive), None)
            if being is None:
                return {"success": False, "error": "召唤目标未唯一指定你的妖怪"}
            if not being.is_alive:
                return {"success": False, "error": "目标妖怪已离场或休眠，无法召唤"}
            player_actor = world.actors.get(player_id)
            being_actor = world.actors.get(being.runtime_id)
            if player_actor is None or being_actor is None or not being_actor.alive:
                return {"success": False, "error": "妖怪不在地图上，无法召唤"}
            path = world.map.find_path(player_actor.position, being_actor.position)
            if path is None or len(path) - 1 > 5:
                return {"success": False, "error": "妖怪必须位于五格以内的可通行路径上才能召唤"}
            if any(encounter.active and being.runtime_id in encounter.participants
                   for encounter in world.encounters.values()):
                return {"success": False, "error": "妖怪正在独立遭遇中，暂时无法召唤"}
            being.is_summoned = True
            world.record("created_being_summoned", player_id, created_being=being.name,
                         created_being_id=being.runtime_id, path_distance=len(path) - 1)
            self.state.world_state = world.to_dict()
        # 在战始产生随机怪物前保存快照；重来会恢复随机源并再次按当前代码抽取。
        self._battle_restart_snapshot = {
            "state": copy.deepcopy(self.state),
            "dice": copy.deepcopy(self.dice),
            "combat_runtime": copy.deepcopy(self._snapshot_combat_runtime()),
            "battle_start_params": copy.deepcopy(params),
        }
        self.state.phase = GamePhase.IN_COMBAT.value
        self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_START.value
        self.state.current_battle += 1
        # 记录[战始]生命，作为[战终]清除"回复"类增益后的生命下限
        for entity in (([self.state.player] if self.state.player else [])
                       + self.state.friends + self.state.employees + self.state.temp_friends):
            entity.battle_start_hp = entity.current_hp
            entity.healed_this_battle = 0
            entity.total_healed = 0
            entity.no_action_rounds = 0
            entity.no_damage_rounds = 0
        self.state.scoped_effect_ledger = []
        # 2026-10-14：【增殖】战终待决按场重置（逃跑/重开等未结算路径不得带入下场）。
        # 事件流按场重置：只在本场战斗内可观测，避免长模拟无界增长。
        self.state.combat_events = []
        self.state.current_round = 0
        self.combat.reset_monster_activation()
        self.combat._priority_passes = 0
        self.combat._ended_turn_sides.clear()
        self.state.duel_turn = "player_side"
        self.state.shared_dragon_heart_type = ""  # 共心环：每场需重新选定
        self.state.godfather_revolver_uses = 0  # 教父左轮：每场6发，[战始]回满即计数归零

        region = self.state.current_region
        pool = self._monster_pool_for_battle()
        endless_cycle = int(getattr(self.state, "endless_cycle", 0) or 0)
        self.state.enemies.clear()
        # 自愈X「目标死亡时」：新战斗选中集合清零（上场选过的死了不算）。
        self.state.player_selected_targets.clear()
        self.state.monster_reinforcements = []
        self.state.delayed_monster_reentries = []
        drawn_names = []
        queued_names = []
        draw_count = 0
        spawn_plan = None
        if world_battle:
            self.state.enemies.extend(world_participant_enemies)
            draw_count = len(world_participant_enemies)
            drawn_names = [entity.name for entity in world_participant_enemies]
            spawn_plan = {"total": draw_count, "first_count": draw_count,
                          "queue_rounds": [], "waves": [], "world_encounter_id": world_encounter_id}
        elif injected_enemies:
            self.state.enemies.extend(injected_enemies)
            draw_count = len(injected_enemies)
            drawn_names = [entity.name for entity in injected_enemies]
            spawn_plan = {"total": draw_count, "first_count": draw_count,
                          "queue_rounds": [], "waves": []}
        elif pool:
            spawn_plan = roll_spawn_plan(self.dice, self.state.current_battle, self._current_tier())
            draw_count = spawn_plan["total"]
            drawn_defs = []
            for i in range(draw_count):
                roll = self.dice.auto_roll(f"monster_draw_{self.state.current_battle}_{i}", pool,
                                            context=f"出怪(第{self.state.current_battle}场,第{i + 1}只)")
                drawn_defs.append(scale_monster_def_for_endless(roll["selected"], endless_cycle))
            first_count = min(int(spawn_plan["first_count"]), draw_count)
            queue_rounds = spawn_plan["queue_rounds"]
            for i, monster_def in enumerate(drawn_defs):
                if i < first_count:
                    m = make_monster_entity(monster_def)
                    assign_monster_preferences(m, self._current_tier(), self.dice)
                    m.spawned_round = 1
                    self.combat.init_monster_shards(m)
                    self.state.enemies.append(m)
                    drawn_names.append(monster_def["name"])
                else:
                    entry = dict(monster_def)
                    entry["arrive_round"] = int(queue_rounds[i - first_count])
                    self.state.monster_reinforcements.append(entry)
                    queued_names.append(entry["name"])

        # 事件登记的下一场修正全部在战始一次性消费。
        modifiers = self.state.event_modifiers
        reveal_full_information = bool(modifiers.pop("next_battle_full_information", False))
        if not world_battle and modifiers.pop("bounty_extra_monster", False) and pool:
            roll = self.dice.auto_roll(
                f"bounty_monster_{self.state.current_battle}", pool,
                context="通缉悬赏榜额外帮派怪物",
            )
            bonus = make_monster_entity(scale_monster_def_for_endless(roll["selected"], endless_cycle))
            assign_monster_preferences(bonus, self._current_tier(), self.dice)
            self.combat.init_monster_shards(bonus)
            self.state.enemies.append(bonus)
            drawn_names.append(bonus.name + "(悬赏额外)")
        next_fake = modifiers.pop("next_battle_fake_shards", 0)
        if next_fake:
            self.state.fake_shards += next_fake
        if modifiers.get("loan_active"):
            self.state.shards -= 10
            if self.state.shards < 0 and self.state.player:
                interest = math.ceil(abs(self.state.shards) / 10) * 5
                self.state.player.blood_limit = max(0, self.state.player.blood_limit - interest)
                # 2026-10-14：【猩红果实】持有者豁免血限压顶（生命可超血限）。
                if not self.state.hp_overflow_exempt(self.state.player):
                    self.state.player.current_hp = min(self.state.player.current_hp, self.state.player.blood_limit)
        red_spring = modifiers.get("red_spring_battle_losses", 0)
        if red_spring > 0 and self.state.player:
            self.combat._raw_hp_loss(self.state.player, 4, ctx={
                "timing": "battle_start", "source": "赤泉囊", "source_type": "consumable",
                "actor": self.state.player, "target": self.state.player, "mechanic": "hp_loss",
                "subtype": "battle_start_loss", "amount": 4, "tags": {"consumable", "battle_start"},
            })
            modifiers["red_spring_battle_losses"] = red_spring - 1

        # 事件登记的"下一场额外出现的怪物"（如龙心谷"追求者·拿走口粮"）
        forced = [] if world_battle else list(self.state.forced_monsters_next_battle)
        if not world_battle:
            self.state.forced_monsters_next_battle = []
        for fm in forced:
            m = make_monster_entity(scale_monster_def_for_endless(fm, endless_cycle))
            assign_monster_preferences(m, self._current_tier(), self.dice)
            self.combat.init_monster_shards(m)
            self.state.enemies.append(m)
            drawn_names.append(fm["name"] + "(额外出现)")
        arena_percent = modifiers.pop("arena_health_percent", 0)
        if arena_percent:
            for monster in self.state.enemies:
                gain = math.ceil(monster.blood_limit * arena_percent / 100)
                self.state.apply_scoped_delta(
                    monster, "blood_limit", gain,
                    scope=EffectScope.BATTLE.value, polarity=EffectPolarity.BUFF.value,
                    source="地下角斗场")
                monster.current_hp += gain

        # DM裁定 2026-09-09：法力不再每[回始]回填，改为**一池制**——[战始]给满
        # 等同[法限]的一池，整场只出不进，[战终]复原（与[速度]同口径）。
        # 先赋值再结算战始遗物，遗物额外法力因此叠在满池之上，不会被赋值冲掉。
        if self.state.player and self.state.player.is_alive:
            self.state.player.current_mana = self.state.player.mana_limit
        exhaust_logs = self.combat.settle_battle_start_exhaust()
        relic_logs = self.combat.process_relics(TriggerTiming.BATTLE_START, {"relic_choices": relic_choices})

        artifact_logs = self._apply_terminal_artifacts_on_battle_start()

        # 全局法术【战始】：必须在战始遗物结算之后才检查，
        # 否则法力已被本action开头清零、任何消耗法力的战始法术都会法力不足——
        # 与"战始遗物在0上叠加"的既有顺序一致，法术同样吃得到本场战始的
        # 法力加成。
        spell_logs = self._resolve_global_trigger_spells_for_action(
            TriggerTiming.BATTLE_START.value, params)

        waves = (spawn_plan["waves"] if spawn_plan else [])
        return {
            "success": True, "action": "战始",
            "battle_number": self.state.current_battle,
            "region": region,
            "draw_count": draw_count,                 # 本场怪物总数 N（配方口径）
            "first_wave_count": (spawn_plan["first_count"] if spawn_plan else 0),
            "spawn_plan": spawn_plan,                  # 配方明细：N/S/每波进场回合与数量
            "enemies": drawn_names,
            "queued_reinforcements": queued_names,
            "reinforcement_waves": waves,
            "full_information": ([enemy.to_dict() for enemy in self.state.enemies]
                                 if reveal_full_information else None),
            "relic_logs": relic_logs,
            "spell_logs": spell_logs,
            "artifact_logs": artifact_logs,
            "exhaust_logs": exhaust_logs,
            "instruction": ((f"首发{len(drawn_names)}只怪物已进场"
                             + (f"；另有{len(queued_names)}只增援待命（"
                                + "、".join(f"R{w['arrive_round']}进{w['count']}只" for w in waves)
                                + "）" if queued_names else "（无增援）")
                             + "；请补充选择本场战斗背景(纯叙事，不影响数值)并结算其余[战始]效果")),
        }

    # ==================== 战终与终局边界 ====================
    # 现行默认终局是开放地图世界事件【最终的冠冕】（engine/handlers/world.py 按世界回合
    # 排期、刷新、计时与脱离），与战斗场数无关：本战终不自动触发封存或死斗。
    # 旧第8场死斗链（_trigger_final_crown / resolve_final_duel）仅作显式兼容路径保留。

    @ACTION_REGISTRY.action("battle_end", group="combat")
    def _action_battle_end(self, params: dict) -> dict:
        """战终；仍有未移出的存活敌人时不得跳过战斗直接结算（统一判定见 GameState.battle_won）。"""
        world_battle = bool(getattr(self.state, "world_encounter_id", ""))
        living = [e.name for e in self.state.active_enemies()]
        escaping = self.state.event_modifiers.pop("escape_at_battle_end", False)
        if living and not escaping:
            return {"success": False, "error": f"仍有存活敌人，不能结算战终: {living}"}
        queued = list(getattr(self.state, "monster_reinforcements", []) or [])
        if queued and not escaping:
            return {"success": False, "error": f"仍有{len(queued)}只怪物增援未进场，不能结算战终: {[m.get('name', '?') for m in queued]}"}
        delayed = list(getattr(self.state, "delayed_monster_reentries", []) or [])
        if delayed and not escaping:
            return {"success": False, "error": f"仍有{len(delayed)}个单位处于【封印】暂离，不能结算战终: {[e['monster'].name for e in delayed]}"}
        if escaping:
            for enemy in self.state.enemies:
                if enemy.is_alive:
                    leave_ctx = make_context(
                        timing="battle_end", source="绝息淤泥", source_type="consumable",
                        target=enemy, mechanic="leave", subtype="escape",
                        amount=0, tags={"leave", "no_shards"},
                    )
                    enemy._leave_ctx = leave_ctx.to_dict()
                    enemy.depart_battle("逃跑")
        # 员工经济系统·工资结算门槛：先按"存活+已部署+非还债"员工计算工资写入待决列表；
        # 任何一名待决(值不为None，代表尚未pay/refuse)即阻塞后续战终结算。
        self._compute_pending_wages()
        still_pending = {k: v for k, v in self.state.pending_wage_decisions.items() if v is not None}
        if still_pending:
            return {
                "success": True,
                "action": "战终工资待决",
                "completed": False,
                "instruction": "请先为以下员工逐个调用 pay_employee_wage(name, decision=pay/refuse)，再重新调用battle_end",
                "pending_wage_decisions": still_pending,
            }
        self.state.pending_wage_decisions = {}
        # 死亡员工也必须先参与统一战终清理与作用域回滚，再从名单移除。
        departed_employees = [e for e in self.state.employees
                              if not e.is_alive and not e.is_debt_bound]

        relic_end = self.combat.process_relics(TriggerTiming.BATTLE_END)
        # 【交易】发现的遗物只在本场战斗内有效：战终结算完再移除。
        cleared_battle_relics = self._clear_battle_only_relics()
        # 2026-11-01 重构1（方案A 收口）：战斗中的临时突破（【赦免】强制启用、
        # 【交易】发现、【豪夺】夺取）在战终、临时遗物清除与到期归还之后压回
        # ceil(血限/30)：按持有顺序保留前 N 件为启用，其余转 enabled=False。
        disabled_relics = self.state.enforce_enabled_relic_limit()
        # 全局法术【战终】：必须在本函数下方"清除局内状态/法力相关计数"之前
        # 结算，否则法术依赖的道纹/法力/状态在校验时已经被清空，永远无法
        # 满足条件（与遗物战终结算 relic_end 同一时序原则：先结算再清理）。
        spell_logs = self._resolve_global_trigger_spells_for_action(
            TriggerTiming.BATTLE_END.value, params)
        # 碎片奖励计算（雕塑/癌变/还债/永久离场的怪物不视为击杀；【封印】暂离后回场，最终命零时正常产出碎片）
        # 奖励公式用的是[战始][血限]快照(battle_start_blood_limit)，不是当前血限(增殖等会改变当前血限)
        shard_reward = 0
        removed = []
        death_rewards = []
        for monster in self.state.enemies:
            if monster.is_departed or monster.is_sculptured or monster.removed_without_kill \
                    or monster.is_proliferated or monster.is_debt_bound:
                way = (monster.departure_reason or
                       ("雕塑" if monster.is_sculptured else
                        "救赎" if getattr(monster, "_redeemed", False) else
                        "癌变" if monster.is_proliferated else
                        "还债" if monster.is_debt_bound else "永久离场"))
                leave_parent = normalize_context(getattr(monster, "_leave_ctx", None))
                leave_ctx = make_context(
                    timing="battle_end", source=way, source_type="system",
                    target=monster, mechanic="combat_resolution", subtype="leave_no_shards",
                    amount=0, tags={"leave", "no_shards"},
                    parent_event_id=leave_parent.event_id if leave_parent else None,
                )
                removed.append({"name": monster.name, "way": way, "ctx": leave_ctx.to_dict()})
                continue
            if not monster.is_alive:
                reward = math.ceil(monster.battle_start_blood_limit * 0.02) + len(monster.dao_wen) * 5
                shard_reward += reward
                death_parent = normalize_context(getattr(monster, "_death_ctx", None))
                reward_ctx = make_context(
                    timing="battle_end", source="命零碎片奖励", source_type="system",
                    target=monster, mechanic="combat_resolution", subtype="death_shard_reward",
                    amount=reward, tags={"death", "shards"},
                    parent_event_id=death_parent.event_id if death_parent else None,
                )
                death_rewards.append({"name": monster.name, "reward": reward, "ctx": reward_ctx.to_dict()})

        # 2026-09-16 裁定（清单 B4）：【碎片】命零公式以正文为准，禁止改写。
        # shard_reward 恒等于 Σ(⌈战始血限×2%⌉ + 道纹数×5)，任何副本/事件增益
        # 都不得乘进或并入这一笔；事件收益改为下面单独列项的「战终奖金」，
        # 名目与来源可逐条核对。
        modifiers = self.state.event_modifiers
        event_bonuses: list[dict] = []
        if modifiers.pop("arena_double_loot", False):
            # 地下角斗场「赢了全拿」：不改命零公式，另发等额奖金。
            event_bonuses.append({"name": "角斗场奖金", "amount": shard_reward,
                                  "source": "地下角斗场"})
        bounty = modifiers.pop("bounty_reward", 0)
        if bounty:
            event_bonuses.append({"name": "悬赏金", "amount": bounty,
                                  "source": "通缉悬赏榜"})
        if modifiers.pop("arena_bet_three_rounds", False) and self.state.current_round <= 3:
            event_bonuses.append({"name": "三回合彩头", "amount": 45,
                                  "source": "地下角斗场"})
        bonus_total = sum(b["amount"] for b in event_bonuses)
        self.state.shards += shard_reward + bonus_total
        modifiers.pop("scarlet_fruit_active", False)  # 旧战终血限+2效果已废止
        # 旧存档兼容：新版【增殖】是战斗内状态，不再有战终待决血限增长。
        modifiers.pop("zengzhi_pending", None)
        zengzhi_settled = []
        modifiers.pop("pale_flower_active", False)   # 2026-10-11：旧战终精力+1 已随重做废弃
        modifiers.pop("brand_nail_target_ref", None)
        modifiers.pop("huifeng_target_ref", None)
        modifiers.pop("huifeng_target_ref_opponent", None)

        # 【自愈X】战终结算（2026-10-14 用户令通用化：无限肉块 80% 迁入时机表）。
        zi_yu_logs = apply_zi_yu_trigger(self.state.consumables, ZIYU_TRIGGER_BATTLE_END)

        # [战终]对所有角色统一清除局内回复、格挡与状态；不得只清轮回者。
        all_characters = (([self.state.player] if self.state.player else [])
                          + self.state.friends + self.state.employees
                          + self.state.temp_friends + self.state.enemies)
        persistent_scopes = {
            EffectScope.COST.value, EffectScope.RUN.value, EffectScope.PERMANENT.value,
        }
        runtime_counter_attrs = (
            "_jisu_dodges", "_dongcha_pending", "_bizhong_left", "_nilin",
            "_beifu_left", "_beifu_target",   # 2026-10-11：_jiahuo_* 随【嫁祸】删除
            "_death_triggers_emitted", "_shouyedeng_granted", "_huifeng_target_ref",
            "_chenni_blocked_world_round",
        )
        for entity in all_characters:
            entity.clear_shield()
            if entity.healed_this_battle > 0 and not getattr(entity, "is_dormant", False):
                entity.current_hp = (max(1, entity.current_hp - entity.healed_this_battle)
                                     if entity.is_alive else 0)
            # 休眠妖怪的怪物死亡恢复是跨战斗保留的唤醒进度，不按普通局内回复扣除。
            entity.healed_this_battle = 0
            # 癌变累计回复量已裁定为局内减益追踪，每场必须归零。
            entity.total_healed = 0
            self.combat.release_battle_ice(entity)
            entity.status_effects = [s for s in entity.status_effects
                                     if s.scope in persistent_scopes]
            # 账本清空一律走唯一入口（引擎/机制两层的口径都从 ledger 模块取）
            entity.current_speed = entity.speed_limit
            # DM裁定 2026-09-09：法力与速度同口径——只在[战终]复原（[回始]不再回填）。
            if entity.entity_type == "轮回者":
                entity.current_mana = entity.mana_limit
            entity.is_flying = False
            entity.hp_lost_this_round = 0
            entity.actions_used_this_round = 0
            entity.heavy_shackles_action_penalty = 0
            entity.hostile_targeters_this_battle.clear()
            entity.mana_inflicted_this_round = 0
            entity.damage_dealt_this_round = 0
            entity.no_action_rounds = 0
            entity.no_damage_rounds = 0
            for attr in runtime_counter_attrs:
                if hasattr(entity, attr):
                    delattr(entity, attr)
            if hasattr(entity, "_bianxing_original"):
                entity.attack_power, entity.attack_count = entity._bianxing_original
                delattr(entity, "_bianxing_original")
        scoped_rollbacks = self.state.rollback_scoped_effects(EffectScope.BATTLE.value)
        self.state.rollback_scoped_effects(EffectScope.ROUND.value)
        # 生命周期（Part 6）：battle 作用域的法术绑定随战终失效。
        # 必须在【战终】全局法术结算之后执行（上面 spell_logs），
        # 否则战终法术自己要用的 battle 法术会先被清掉。
        spell_bindings_removed = self.combat.clear_battle_scoped_spells()
        self.state.sealed_relics = {}
        self.state.fuyuebei_declared = []
        modifiers.pop("fuyuebei_cost_share_refs", None)

        # 体外心脏：临时翻倍的血限[战终]还原为基准值，当前生命同步封顶。
        if (self.state.player and "体外心脏" in self.state.artifacts_owned
                and self._artifact_base_blood_limit > 0):
            self.state.player.blood_limit = self._artifact_base_blood_limit
            # 2026-10-14：【猩红果实】持有者豁免血限压顶（生命可超血限）。
            if not self.state.hp_overflow_exempt(self.state.player):
                self.state.player.current_hp = min(self.state.player.current_hp, self.state.player.blood_limit)
            self._artifact_base_blood_limit = 0

        # 初拥【真理】已采用 DaoWenInstance.cooldown_remaining，随下方通用道纹冷却递减；
        # truth_eye_cooldown 仅作为旧 pickle 迁移字段，不再参与新存档结算。

        # 道纹【冷却X】：规则正文「[战终]后已完成战斗场数+1，达到Y时才能再次使用」。
        # 对所有持有道纹的角色统一递减，归零即恢复可用。
        for _ent in ([self.state.player] if self.state.player else []) \
                + self.state.friends + self.state.employees:
            for _inst in _ent.dao_wen.values():
                if _inst.cooldown_remaining > 0:
                    _inst.cooldown_remaining -= 1

        # 员工经济系统·死亡离队：完成统一清理后计入黑名单并移除，避免遗漏回滚或重复计数。
        for emp in departed_employees:
            if emp in self.state.employees:
                self.state.employees.remove(emp)
                self._blacklist_departure("死亡离队")

        # 造物消失
        self.state.temp_friends.clear()

        # 出战支援：每场战斗单独部署，战终后存活员工回到"待命"状态，下一场需重新派遣
        # （先记录本场参战者供成长判定；若重置后再判 is_deployed，则参战者全被误判为待命而永不成长）
        _battled_ids = {id(emp) for emp in self.state.employees
                        if emp.is_alive and not emp.is_debt_bound and emp.is_deployed}

        # 2026-09-17 用户令：[员工]**出场**并**存活**满 EMPLOYEE_PROMOTION_BATTLES
        # 场战斗后转为[朋友]。口径与 _battled_ids 一致——本场确实参战(is_deployed)
        # 且[战终]仍存活；待命未上场不计，阵亡者已被上面的 departed_employees 移除。
        # 还债员工(is_debt_bound)不晋升：其身份是债务约束而非雇佣关系。
        promoted = []
        for emp in list(self.state.employees):
            if id(emp) not in _battled_ids:
                continue
            emp.survived_battles_as_employee = getattr(
                emp, "survived_battles_as_employee", 0) + 1
            if emp.survived_battles_as_employee < Entity.EMPLOYEE_PROMOTION_BATTLES:
                continue
            self.state.employees.remove(emp)
            emp.entity_type = "朋友"
            emp.is_deployed = True          # [朋友]无待命概念，直接计入战场
            self.state.friends.append(emp)
            promoted.append({"name": emp.name,
                             "battles": emp.survived_battles_as_employee})

        for emp in self.state.employees:
            if emp.is_alive and not emp.is_debt_bound:
                emp.is_deployed = False
                emp.deployed_at_round = 0

        # 撤退：仅"无法再次加入本场战斗"，战终后重置，可正常参加下一场
        for ally in self.state.friends + self.state.employees + self.state.temp_friends:
            ally.has_retreated = False

        # 伙伴成长：每存活过一场战斗的[朋友]/[员工]，攻击次数+1（上限9）；达到9后改为攻击力+1。
        # 未部署的员工不参战，不成长；已命零/已离队的不成长。
        grown = []
        for ally in self.state.friends + self.state.employees:
            if not ally.is_alive or ally.is_debt_bound:
                continue
            if ally.entity_type == "员工" and id(ally) not in _battled_ids and ally not in self.state.friends:
                # 待命员工未上场，不累计战斗历练（以重置前记录的参战集合为准）
                continue
            if ally.attack_count < 9:
                ally.attack_count += 1
                grown.append(f"{ally.name}:攻击次数{ally.attack_count}")
            else:
                ally.attack_power += 1
                grown.append(f"{ally.name}:攻击力{ally.attack_power}")

        # 恢复精力（无尽模式下基础值逐轮递减）。
        # 2026-10-11：【苍白之花】重做为「当前法力可超出[法限]」的被动，战终精力+1 已废止。
        self.state.energy = self.state.energy_budget

        # 普通战斗清空敌人；开放世界遭遇则把存活参与者放回地图，并恢复未参战的敌方单位。
        if world_battle:
            world = self._world_from_state()
            encounter_id = self.state.world_encounter_id
            encounter = world.encounters.get(encounter_id)
            participants = list(getattr(self.state, "world_encounter_entities", []) or [])
            participant_by_id = {entity.runtime_id: entity for entity in participants}
            reserve = list(getattr(self.state, "world_enemy_reserve", []) or [])
            player_id = getattr(self.state.player, "runtime_id", "") if self.state.player else ""
            for actor_id in (encounter.participants if encounter else []):
                actor = world.actors.get(actor_id)
                if actor is None:
                    continue
                if actor_id == player_id and self.state.player is not None:
                    actor.alive = self.state.player.is_alive
                elif actor_id in participant_by_id:
                    entity = participant_by_id[actor_id]
                    actor.alive = bool(entity.is_alive and not entity.is_departed)
                    if entity.world_x is not None and entity.world_y is not None:
                        actor.x, actor.y = int(entity.world_x), int(entity.world_y)
                else:
                    actor.alive = False
            for companion in self.state.friends + self.state.employees + self.state.temp_friends:
                companion_actor = world.actors.get(companion.runtime_id)
                if companion_actor is not None:
                    companion_actor.alive = bool(companion.is_alive and not companion.is_departed)
                    if companion.world_x is not None and companion.world_y is not None:
                        companion_actor.x, companion_actor.y = int(companion.world_x), int(companion.world_y)
                if getattr(companion, "is_created_being", False):
                    companion.is_summoned = False
            if encounter is not None and encounter.active:
                outcome = "player_dead" if not self.state.player or not self.state.player.is_alive else "resolved"
                world.finish_encounter(encounter_id, outcome)
            self.state.enemies = [entity for entity in reserve if entity.is_alive]
            self.state.enemies.extend(
                entity for entity in participants if entity.is_alive and not entity.is_departed)
            self.state.world_enemy_reserve = []
            self.state.world_encounter_entities = []
            self.state.world_encounter_id = ""
            # 战斗结束后立即检查仍与存活单位同格/相邻的敌人；每名单位只进入一场活跃遭遇，
            # 因此多敌围堵会按遭遇顺序继续，不会留下重复活跃的战斗锁。
            world.detect_adjacent_encounters()
            # NPC战斗可能刚好淘汰了倒数第二名轮回者；此时进入自然会合阶段，
            # 但仍保留地图上的怪物，让它们在会合期间继续构成威胁。
            # 死亡参与者已从state.enemies移除，但仍必须写入世界事实账本。
            self._record_world_entity_outcomes(world, extra_entities=participants + reserve)
            self.state.world_state = world.to_dict()
        else:
            self.state.enemies.clear()
        self.state.delayed_monster_reentries = []

        self.state.phase = "pre_battle"

        # 捕获战终结果数值，因【最终的冠冕】可能在下面把self.state整个替换为新轮回者的空白状态
        battle_end_result = {
            "shard_reward": shard_reward,
            # 命零公式产出的碎片（正文口径，不含任何副本/事件增益）
            "event_bonuses": event_bonuses,       # 事件奖金，单独列项，不并入命零公式
            "event_bonus_shards": bonus_total,
            "total_shards": self.state.shards,
            "energy_restored": self.state.energy_budget,
            "cleared_temp_friends": True,
            "ally_growth": grown,
            "removed_via_alt_path": removed,
            "death_shard_rewards": death_rewards,
            "relic_end_logs": relic_end,
            "battle_only_relics_cleared": cleared_battle_relics,
            "relics_disabled_over_limit": disabled_relics,
            "zengzhi_settled": zengzhi_settled,
            "zi_yu_logs": zi_yu_logs,
            "spell_logs": spell_logs,
            "scoped_effects_rolled_back": scoped_rollbacks,
            "spell_bindings_removed": spell_bindings_removed,
            "employee_promotions": promoted,   # [员工]出场存活满3场 → 转为[朋友]
            "player_dead": (not self.state.player.is_alive) if self.state.player else False,
        }

        # 【最终的冠冕】不再由第7场战终强制触发（用户裁定：删除第8场强制死斗）。
        # 它改为世界事件：由 `_resolve_world_crown` 在世界回合推进时按时间随机刷新，
        # 持有者连续存活满 5 个世界回合即自动脱离副本（见 engine/world.py 常量）。

        return {
            "success": True,
            "action": "战终",
            "result": battle_end_result,
        }

    # ==================== 死者之页 / 死者之书 ====================
