"""角色化消耗品结算与NPC战斗决策。绝不交换state.player或共享NPC库存。

怪物显式攻击提交完整结算后再作物品决策，避免自动击杀/改变法力破坏prepare契约。
朋友/员工与地图互战本身自主选动作，在选择攻击前使用；策略不是每回合使用上限。
局外、DM/自定义决策与逃跑不自动发动；万钧印走攻击反应入口。
"""
from __future__ import annotations
import re
from typing import Any
from . import custom_resources
from .models import StatusEffect
from .enums import GamePhase
from .consumables import WING_MODES, WING_STATUS_LIFESTEAL, WING_STATUS_SUPPRESS

TOOL_NAMES = frozenset({"反怪物电击枪", "备用血泵", "强光探照灯", "高压水枪", "储能电池", "急救箱", "干扰仪", "高爆手雷"})
NAMED_COMBAT_ITEMS = frozenset({"定命丸", "灼罪钉", "怒火针", "破障晶", "洗劫面具"})


def consume_tool(combat, item, params, actor, opponents):
    name = item.name
    player = actor
    if not player:
        return {"success": False, "error": "没有玩家"}
    def selected_enemy():
        target = combat._combat_entity_refs().get(params.get("target_ref", ""))
        if target in opponents:
            return target
        legacy = params.get("target", "")
        matches = [entity for entity in opponents if entity.name == legacy]
        return matches[0] if len(matches) == 1 else None
    # 急救箱的“清除一种”必须由AI显式选择，先校验后扣耐久。
    aid_status = None
    if name == "急救箱":
        negative = [s for s in player.status_effects
                    if s.remaining_rounds > 0 or s.name in ("坏死", "退化", "伤痕", "畸变", "蒙蔽")]
        selected = params.get("remove_status")
        if negative:
            if not isinstance(selected, str) or selected not in [s.name for s in negative]:
                return {"success": False,
                        "error": f"急救箱必须显式提交remove_status，可选{[s.name for s in negative]}"}
            aid_status = next(s for s in negative if s.name == selected)
        elif selected not in (None, ""):
            return {"success": False, "error": "当前没有可由急救箱清除的负面状态"}

    # 全部参数合法后统一扣耐久。
    remaining = item.use()
    result: dict[str, Any] = {"tool": name, "uses_remaining": remaining, "is_depleted": item.is_depleted}
    # 1. 反怪物电击枪：对目标 25 伤害，飞行目标额外 +15
    #
    if name == "反怪物电击枪":
        target = selected_enemy()
        if target is None:
            item.current_uses += 1
            return {"success": False, "error": "找不到敌方target_ref"}
        # 只有当前飞行状态算飞行；仅“持有”飞行道纹不算已经飞行。
        flying = target.is_flying or target.has_status("飞行")
        dmg = 25 + (15 if flying else 0)
        detail = combat._apply_hostile_damage(target, dmg, source=player, ctx={
            "timing": combat.state.combat_subphase or combat.state.phase, "source": "反怪物电击枪", "source_type": "consumable",
            "actor": player, "target": target, "mechanic": "damage", "subtype": "consumable",
            "amount": dmg, "tags": {"consumable"},
        })
        result.update({"target": target.name, "damage": dmg, "flying_bonus": 15 if flying else 0, "hp_after": target.current_hp, "detail": detail})
        # 2026-10-03：原“击落”（施【坠落1】）随道纹删除移除。
    # 2. 备用血泵：回复20（走 heal，计入癌变/战终回吐），≤30% 额外30格挡
    elif name == "备用血泵":
        heal_detail = combat.state.apply_heal(player, 20, ctx={
            "timing": combat.state.combat_subphase or combat.state.phase, "source": "备用血泵", "source_type": "consumable",
            "actor": player, "target": player, "mechanic": "heal", "subtype": "consumable",
            "amount": 20, "tags": {"consumable"},
        })
        healed = heal_detail["actual_heal"]
        shield_gained = 0
        if player.current_hp <= player.blood_limit * 0.3:
            player.shield += 30
            shield_gained = 30
        cancer = combat.check_cancer(player)
        result.update({"healed": healed, "hp_after": player.current_hp,
                       "shield_gained": shield_gained, "heal": heal_detail,
                       "cancer": cancer})
    # 3. 强光探照灯：目标蒙蔽2
    elif name == "强光探照灯":
        target = selected_enemy()
        if target is None:
            item.current_uses += 1
            return {"success": False, "error": "找不到敌方target_ref"}
        target.add_status(StatusEffect(name="蒙蔽", value=2, remaining_rounds=-1, source="强光探照灯"))
        result.update({"target": target.name, "蒙蔽": 2})
    # 4. 高压水枪：清除全场敌方所有持续X效果
    elif name == "高压水枪":
        cleared = []
        for m in opponents:
            before = len(m.status_effects)
            m.status_effects = [s for s in m.status_effects if s.remaining_rounds <= 0]
            if len(m.status_effects) != before:
                cleared.append(m.name)
        result.update({"cleared_enemies": cleared})
    # 5. 储能电池：使用后立即获得12法力
    elif name == "储能电池":
        # 2026-10-08：走统一入口，【勾魂】期间不生效
        gained = player.gain_mana(12)
        combat.clamp_immortal_body(player)
        result.update({"mana_gained": gained, "mana_after": player.current_mana})
    # 6. 急救箱：回复25（走 heal）并清一种负面持续
    elif name == "急救箱":
        heal_detail = combat.state.apply_heal(player, 25, ctx={
            "timing": combat.state.combat_subphase or combat.state.phase, "source": "急救箱", "source_type": "consumable",
            "actor": player, "target": player, "mechanic": "heal", "subtype": "consumable",
            "amount": 25, "tags": {"consumable"},
        })
        healed = heal_detail["actual_heal"]
        removed = None
        if aid_status is not None:
            player.status_effects.remove(aid_status)
            removed = aid_status.name
        cancer = combat.check_cancer(player)
        result.update({"healed": healed, "removed_status": removed,
                       "heal": heal_detail, "cancer": cancer})
    # 7. 干扰仪：全场敌方本回合无法发动道纹（加干扰1状态）
    elif name == "干扰仪":
        jammed = []
        for m in opponents:
            m.add_status(StatusEffect(name="干扰", value=1, remaining_rounds=1, source="干扰仪"))
            jammed.append(m.name)
        result.update({"jammed": jammed})
    # 8. 高爆手雷：目标15伤害 + 本回合攻击次数-1
    elif name == "高爆手雷":
        target = selected_enemy()
        if target is None:
            item.current_uses += 1
            return {"success": False, "error": "找不到敌方target_ref"}
        detail = combat._apply_hostile_damage(target, 15, source=player, ctx={
            "timing": combat.state.combat_subphase or combat.state.phase, "source": "高爆手雷", "source_type": "consumable",
            "actor": player, "target": target, "mechanic": "damage", "subtype": "consumable",
            "amount": 15, "tags": {"consumable"},
        })
        # 攻击次数-1：用状态标记，本回合内 _monster_attack_actions 会读取
        target.add_status(StatusEffect(name="手雷减攻", value=1, remaining_rounds=1, source="高爆手雷"))
        result.update({"target": target.name, "damage": 15, "detail": detail, "nade_minus": 1})
    else:
        result.update({"note": "未知工具"})
    return {"success": True, "action": f"使用工具【{name}】", "result": result, "state": combat._get_combat_state()}



def consume_named_combat_item(combat, item, params, actor, *, opponents=None, in_battle=False):
    player = actor
    refs = combat._combat_entity_refs()
    actor_ref = next((ref for ref, entity in refs.items() if entity is actor), None)
    name = item.name
    result: dict[str, Any] = {"item": name}
    if name == "定命丸":
        if (not in_battle and combat.state.phase != GamePhase.IN_COMBAT.value) or not player.is_alive:
            return {"success": False, "error": "定命丸只能由存活角色在战斗中使用"}
        if params.get("target_ref") not in (None, "", actor_ref):
            return {"success": False, "error": "定命丸仅对使用者自身生效"}
        item.use()
        player.status_effects[:] = [s for s in player.status_effects if s.name != "固执"]
        player.add_status(StatusEffect(name="固执", value=1, remaining_rounds=1, source=name))
        result.update({"target": player.name, "duration": 1, "max_life_loss": 1})
    elif name == "灼罪钉":
        target = refs.get(params.get("target_ref", ""))
        if target is None or not target.is_alive:
            return {"success": False, "error": "灼罪钉必须显式指定合法存活target_ref"}
        item.use()
        target.add_status(StatusEffect(name="加害", value=1, remaining_rounds=2, source=name))
        result.update({"target": target.name, "duration": 2, "incoming_damage_bonus": 1})
    elif name == "怒火针":
        target = refs.get(params.get("target_ref", ""))
        if target is None or not target.is_alive:
            return {"success": False, "error": "怒火针必须显式指定合法存活target_ref"}
        item.use()
        target.add_status(StatusEffect(name="愤怒", value=1, remaining_rounds=2, source=name))
        result.update({"target": target.name, "duration": 2, "mana_cost_halved": True})
    elif name == "破障晶":
        if (not in_battle and combat.state.phase != GamePhase.IN_COMBAT.value) or not player.is_alive:
            return {"success": False, "error": "破障晶只能由存活角色在战斗中使用"}
        mode = params.get("mode")
        if mode not in {"发射", "破障"}:
            return {"success": False, "error": "破障晶必须选择mode：发射／破障"}
        if mode == "破障":
            if params.get("target_ref") not in (None, "", actor_ref):
                return {"success": False, "error": "破障模式仅对使用者自身生效"}
            item.use()
            player.status_effects[:] = [s for s in player.status_effects if s.name != "贯穿"]
            player.add_status(StatusEffect("贯穿", 1, value=1, source=name))
            result.update({"mode": mode, "target": player.name, "duration": 1})
        else:
            target = refs.get(params.get("target_ref", ""))
            if (target is None or not target.is_alive
                    or (target not in opponents if opponents is not None else
                        combat.state.on_player_side(target) == combat.state.on_player_side(player))):
                return {"success": False, "error": "发射模式必须指定合法存活敌方target_ref"}
            item.use()
            detail = combat._apply_hostile_damage(target, 15, "无视格挡", player, ctx={
                "timing": combat.state.combat_subphase or combat.state.phase, "source": name, "source_type": "consumable",
                "actor": player, "target": target, "mechanic": "damage", "subtype": "consumable",
                "amount": 15, "tags": {"consumable", "ignore_shield", "ignore_dodge"},
            })
            result.update({"mode": mode, "target": target.name, "damage": detail})
    elif name == "洗劫面具":
        item.use(); combat.grant_bizhong(player, 2)
        result.update({"guaranteed_hits": 2, "remaining": combat.bizhong_remaining(player)})
    else:
        return {"success": False, "error": "非通用战斗消耗品"}
    result.update({"uses_remaining": item.current_uses, "is_depleted": item.is_depleted})
    return {"success": True, "action": f"使用消耗品【{name}】", "result": result}


def consume_ordinary(combat, item, actor):
    # 普通消耗品：扣减耐久；异变类效果走统一入口 add_mutation
    # （裁定⑧= A4全量：任何角色的任何异变来源同一入口，达50层即【迷失】命零；
    #  其余效果仅限已经有机械解析器的回复/异变文本；未知效果在扣耐久前拒绝。）
    effect = item.effect or ""
    heal_match = (re.search(r"恢复(\d+)生命", effect)
                  or re.search(r"\[回复\]\s*(\d+)", effect)
                  or re.search(r"回复(\d+)", effect))
    mut_match = re.search(r"获得异变(\d+)", effect)
    if heal_match is None and mut_match is None:
        return {"success": False, "error": f"消耗品【{item.name}】没有已注册的效果处理器"}
    remaining = item.use()
    mutation_info = None
    heal_info = None
    cancer_info = None
    if actor:
        if heal_match:
            amount = int(heal_match.group(1))
            heal_info = combat.state.apply_heal(actor, amount, ctx={
                "timing": combat.state.phase, "source": item.name, "source_type": "consumable",
                "actor": actor, "target": actor, "mechanic": "heal",
                "subtype": "consumable", "amount": amount, "tags": {"consumable"},
            })
            cancer = combat.check_cancer(actor)
            if cancer:
                cancer_info = cancer
                if not actor.is_alive and not combat.state.last_death_cause:
                    if actor is combat.state.player:
                        combat.state.last_death_cause = "cancer"
    mut_match = re.search(r"获得异变(\d+)", effect)
    if mut_match and actor:
        layers = int(mut_match.group(1))
        mut = actor.add_mutation(layers)
        mutation_info = {
            "mutation_added": mut["mutation_added"],
            "mutation_total": mut["mutation_total"],
            "collapsed": mut["collapsed"],
            "lost": mut.get("lost", False),
        }
        if mut["collapsed"]:
            # 怪物（理论上玩家不会走到这里）：异变爆体命零
            combat._on_entity_death(
                actor,
                ctx=combat._lost_context(actor, {
                    "timing": combat.state.combat_subphase or combat.state.phase,
                    "source": "消耗品", "source_type": "consumable",
                    "actor": actor, "target": actor,
                    "mechanic": "cost", "subtype": "mutation", "amount": layers,
                    "tags": {"consumable", "active_payment"}}, subtype="collapse"))
            if actor is combat.state.player:
                combat.state.last_death_cause = "collapse"
            mutation_info["note"] = (
                f"异变达{mut['mutation_total']}层触发【迷失】，"
                f"{actor.name}异变爆体直接命零")
        elif mut.get("lost"):
            # 非怪物：触发【迷失】——战斗中则变身/命零；局外则直接命零。
            if combat.state.phase == "in_combat":
                res = combat._resolve_mutation_lost(actor, {
                    "timing": combat.state.combat_subphase or combat.state.phase,
                    "source": "消耗品", "source_type": "consumable",
                    "actor": actor, "target": actor,
                    "mechanic": "cost", "subtype": "mutation", "amount": layers,
                    "tags": {"consumable", "active_payment"}})
                # 变身不产生"死亡"，不触发死者之页；命零才走 collapse 死因
                if res["lost"] == "death":
                    if actor is combat.state.player:
                        combat.state.last_death_cause = "collapse"
                mutation_info["note"] = res["note"]
                mutation_info["lost_transform"] = (res["lost"] == "transform")
                mutation_info["collapsed"] = (res["lost"] == "death")
            else:
                # 局外触发【迷失】无队友可战：直接命零。_on_entity_death 本身不置 hp/is_alive，
                # 需调用方先归零（与 add_mutation 怪物路径的口径一致）。
                actor.current_hp = 0
                actor.is_alive = False
                combat._on_entity_death(
                    actor,
                    ctx=combat._lost_context(actor, {
                        "timing": combat.state.combat_subphase or combat.state.phase,
                        "source": "消耗品", "source_type": "consumable",
                        "actor": actor, "target": actor,
                        "mechanic": "cost", "subtype": "mutation", "amount": layers,
                        "tags": {"consumable", "active_payment"}}, subtype="collapse"))
                if actor is combat.state.player:
                    combat.state.last_death_cause = "collapse"
                mutation_info["collapsed"] = True
                mutation_info["note"] = (
                    f"异变达{mut['mutation_total']}层触发【迷失】，"
                    f"身边无可战之敌，{actor.name}直接命零")
    return {
        "success": True,
        "action": f"使用消耗品【{item.name}】",
        "result": {
            "effect": item.effect,
            "uses_remaining": remaining,
            "is_depleted": item.is_depleted,
            "heal": heal_info,
            "cancer": cancer_info,
            "mutation": mutation_info,
            "note": "消耗品效果按其描述结算，使用不消耗出手",
        },
        "state": combat._get_combat_state(),
    }



def consume_infinite_meat(combat, item, actor):
    before_total = actor.total_healed
    before_battle_total = actor.healed_this_battle
    remaining = item.use()
    heal = combat.state.apply_heal(actor, 10, ctx={
        "timing": combat.state.phase, "source": item.name, "source_type": "consumable",
        "actor": actor, "target": actor, "mechanic": "heal",
        "subtype": "infinite_meat", "amount": 10,
        "tags": {"consumable", "exclude_cancer_accumulation"},
    })
    actor.total_healed = before_total
    actor.healed_this_battle = before_battle_total
    return {
        "success": True,
        "action": f"使用消耗品【{item.name}】",
        "result": {"uses_remaining": remaining, "heal": heal,
                   "excluded_from_cancer_accumulation": True},
        "state": combat._get_combat_state(),
    }



def consume_wing(combat, item, params, actor):
    mode = params.get("mode")
    status = {"fly": "飞行", "lifesteal": WING_STATUS_LIFESTEAL,
              "suppress": WING_STATUS_SUPPRESS}.get(mode)
    if mode not in WING_MODES.get(item.name, {}) or status is None:
        return {"success": False, "error": "非法翼模式，逃跑须由战场入口校验"}
    remaining = item.use()
    actor.add_status(StatusEffect(name=status, value=1, remaining_rounds=1, source=item.name))
    result = {"mode": mode, "effect": WING_MODES[item.name][mode],
              "uses_remaining": remaining, "max_uses": item.max_uses, "is_depleted": item.is_depleted}
    if mode == "fly":
        result["flying_rounds"] = 1
    return {"success": True, "action": f"使用消耗品【{item.name}】（{WING_MODES[item.name][mode]}）",
            "result": result, "state": combat._get_combat_state()}


# Selection policy only. Execution has no new round/cooldown/use-limit rule.
# Unknown/custom/DM/pre-battle resources are deliberately not guessed at.
_AID_DEBUFFS = frozenset({"坏死", "勾魂", "退化", "伤痕", "畸变", "蒙蔽", "束缚",
                         "弱化", "干扰", "手雷减攻", "加害", "沉溺"})


def auto_use_consumables(combat, actor, opponents=None):
    """一次自主决策扫描实有物品；每个副本各尝试一次，不占出手/推进时间。

    怪物在本次完整提交后调用；自主盟友/地图NPC在选择动作前调用。
    未在战场的单位、耗尽/被动物品、无合适目标不使用、不扣耐久。
    """
    state = combat.state
    if (actor is None or actor is state.player or not actor.is_alive
            or actor.is_departed or actor.has_retreated or state.is_sealed_away(actor)):
        return []
    if opponents is None:
        if actor not in state.get_all_player_side() + state.get_all_enemy_side():
            return []
        opponents = (state.get_all_enemy_side() if state.on_player_side(actor)
                     else state.get_all_player_side())
    logs = []
    refs = combat._combat_entity_refs()
    for item in list(actor.consumables):
        if not actor.is_alive or actor.is_departed:
            break
        enemies = [e for e in opponents if e is not actor and e.is_alive and not e.is_departed]
        if not enemies:
            break
        if (item.is_depleted or item.name in {
                "万钧印", "自由结晶", "绝息淤泥", "活性土壤", "假钞贴", "赤泉囊", "龙血瓶",
                "镇魂铁钉", "赶尸铃", "黄符", "逆命筹"}
                or custom_resources.get_consumable(item.name) is not None):
            continue
        target = min(enemies, key=lambda e: (e.current_hp, e.runtime_id))
        target_ref = next((ref for ref, entity in refs.items() if entity is target), None)
        params = {"target_ref": target_ref}
        name = item.name
        result = None
        if name in TOOL_NAMES:
            if name == "备用血泵" and (actor.current_hp >= actor.blood_limit or actor.has_status("坏死")):
                continue
            if name == "急救箱":
                debuffs = [s for s in actor.status_effects if s.name in _AID_DEBUFFS]
                # The player tool validator requires an explicit status if *any* sustained effect exists.
                # Never remove a beneficial effect just to satisfy it.
                if actor.status_effects and not debuffs:
                    continue
                if not debuffs and actor.current_hp >= actor.blood_limit:
                    continue
                if debuffs:
                    params["remove_status"] = debuffs[0].name
            if name == "储能电池" and (actor.current_mana >= actor.mana_limit or actor.has_status("勾魂")):
                continue
            if name == "强光探照灯" and target.has_status("蒙蔽"):
                continue
            if name == "高压水枪" and not any(s.remaining_rounds > 0 for e in enemies for s in e.status_effects):
                continue
            if name == "干扰仪" and not any(e.dao_wen and not e.has_status("干扰") for e in enemies):
                continue
            if name in {"反怪物电击枪", "高爆手雷", "强光探照灯"} and target_ref is None:
                continue
            result = consume_tool(combat, item, params, actor, enemies)
        elif name in NAMED_COMBAT_ITEMS:
            if name == "定命丸":
                if actor.has_status("固执"):
                    continue
                params = {}
            elif name == "怒火针":
                if actor.has_status("愤怒") or not actor.dao_wen:
                    continue
                params["target_ref"] = next((ref for ref, e in refs.items() if e is actor), None)
            elif name == "灼罪钉":
                if target_ref is None or target.has_status("加害"):
                    continue
            elif name == "洗劫面具" and combat.bizhong_remaining(actor) > 0:
                continue
            elif name == "破障晶":
                # Piercing is useful before an autonomous attack. Using this after
                # an explicit monster submission still prepares the next attack.
                if actor.has_status("贯穿") or not any(e.shield > 0 for e in enemies):
                    continue
                params = {"mode": "破障"}
            result = consume_named_combat_item(combat, item, params, actor,
                                               opponents=enemies, in_battle=True)
        elif name in WING_MODES:
            if actor.has_status("飞行") or actor.is_flying:
                continue
            result = consume_wing(combat, item, {"mode": "fly"}, actor)
        elif item.kind == "sculpture":
            result = combat.use_sculpture(item, target=target, mode="damage", actor=actor)
        elif item.kind == "infinite_meat":
            if actor.current_hp >= actor.blood_limit or actor.has_status("坏死"):
                continue
            result = consume_infinite_meat(combat, item, actor)
        elif (re.search(r"恢复\d+生命|\[回复\]\s*\d+|回复\d+", item.effect or "")
              and not re.search(r"获得异变\d+", item.effect or "")):
            if actor.current_hp >= actor.blood_limit or actor.has_status("坏死"):
                continue
            result = consume_ordinary(combat, item, actor)
        if result and result.get("success"):
            logs.append({"type": "npc_consumable", "actor": actor.name,
                         "actor_id": actor.runtime_id, "item": name,
                         "uses_remaining": item.current_uses, "detail": result})
    return logs


def tick_world_item_statuses(state, entities, turn):
    """地图互战以世界回合作持续时间；同一世界回合重入不重复递减。

    只管理由本模块战斗物品施加的状态，不擅自推进道纹或玩家战斗。
    时钟在event_modifiers中随正常存档持久化。
    """
    clocks = state.event_modifiers.setdefault("npc_item_world_clocks", {})
    sources = TOOL_NAMES | NAMED_COMBAT_ITEMS | set(WING_MODES)
    for entity in entities:
        if entity is state.player:
            continue
        previous = clocks.get(entity.runtime_id, turn)
        elapsed = max(0, turn - previous)
        clocks[entity.runtime_id] = turn
        if not elapsed:
            continue
        for status in list(entity.status_effects):
            if status.source in sources and status.remaining_rounds > 0:
                status.remaining_rounds = max(0, status.remaining_rounds - elapsed)
                if status.is_expired:
                    entity.status_effects.remove(status)
