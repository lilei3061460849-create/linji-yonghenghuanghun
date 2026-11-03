"""sim 内被复制了多份的公共助手（2026-11-03 体检去重）。

背景：体检发现 `best_cultivate_tier` 有 7 份、`_resolve_monster_turn_smart` 有 6 份、
`_learn` 有 7 份（还夹着一份自己调用自己的死代码），散在 sim/ 的 8 个脚本里。
它们不是"不同策略"，而是同一天复制出去的同一种写法——改一处、忘六处，
跑出来的对比数据就没人能解释。

本模块只放**逐字相同**的那几份；差分过的不合并（见文件末尾说明）。
sim/ 的定位不变：一次性探针与实验脚本，只是别再各自写一份。
"""
from __future__ import annotations


def best_cultivate_tier(shards):
    if shards >= 150:
        return 6, 150, 6
    if shards >= 100:
        return 5, 100, 5
    if shards >= 65:
        return 4, 65, 4
    if shards >= 35:
        return 3, 35, 3
    if shards >= 15:
        return 2, 15, 2
    return 1, 0, 1


def _resolve_monster_turn_smart(engine):
    prepared = engine.execute_action("prepare_monster_phase", {})
    if not prepared.get("success"):
        return prepared
    choices = []
    p = engine.state.player
    max_d = p.current_speed if p else 2
    dodge_budget = 0
    sim_shield = p.shield if p else 0
    for actor in prepared["result"]["actors"]:
        dao = None
        action_count = actor["base_attack_actions"]
        hit_count = actor["base_hits_per_attack"]
        from sim.duel_common import _pick_monster_daowen
        has_guanchuan = False
        will_activate_nilin = False
        refs = engine.combat._combat_entity_refs()
        monster = refs.get(actor["actor_ref"])
        per_hit = monster.attack_power if monster is not None else 0
        option = _pick_monster_daowen(engine, actor) if actor["daowen_options"] else None
        if option is not None:
            if option.get("name") == "贯穿":
                has_guanchuan = True
            elif option.get("name") == "逆鳞":
                will_activate_nilin = True
            elif option.get("name") == "变形" and monster is not None:
                hit_count = monster.attack_power
                per_hit = monster.attack_count
            elif option.get("name") == "减速":
                max_d = max(0, max_d - option.get("x_value", option.get("x", 5)))
            dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {holder: {sp["spell_name"]: {"use": False} for sp in spells}
                                             for holder, spells in option.get("trigger_spell_options", {}).items()}}
            if option["requires_target"]:
                dao["target_ref"] = option["target_options"][0]["ref"]
            if option["dodge_submission"] == "per_target":
                from sim.monster_targets import pick_wave_dodge_targets
                dao["dodge_targets"] = pick_wave_dodge_targets(option)
        if monster is not None and (monster.has_status("贯穿") or has_guanchuan):
            has_guanchuan = True
        nilin_bonus = (monster.get_status_value("逆鳞") or 0) if (monster is not None and monster.has_status("逆鳞")) else 0
        from engine.ai_tactics import choose_attack_target
        target_ref = choose_attack_target(actor["attack_target_options"], refs)
        target_option = next((o for o in actor["attack_target_options"] if o["ref"] == target_ref), None)   # 无合法攻击目标时为None（引擎prepare已置base_attack_actions=0）
        attacks = []
        for _ in range(action_count):
            hits = []
            for _ in range(hit_count):
                should_dodge = False
                if target_ref == "player:0":
                    if dodge_budget < max_d and (has_guanchuan or nilin_bonus > 0 or per_hit >= 12 or (action_count * hit_count >= 3 and sim_shield < per_hit)):
                        should_dodge = True
                        dodge_budget += 1
                    elif sim_shield >= per_hit:
                        sim_shield -= per_hit
                        should_dodge = False
                    elif dodge_budget < max_d and per_hit >= 6:
                        should_dodge = True
                        dodge_budget += 1
                from sim.build_learner import _decline_spells
                hits.append({"target_ref": target_ref, "dodge": should_dodge, "blood_shadow": False,
                             "spell_choices": _decline_spells(target_option)})
            attacks.append({"hits": hits})
        choices.append({"actor_ref": actor["actor_ref"], "daowen": dao, "attack_actions": attacks})
    return engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"],
        "choices": choices,
    })


def learn_spell_probe_shortcut(e, definition):
    """探针专用：直接写入玩家法术列表（跳过 define_spell 的 1 次出手消耗）。

    局外【学习】自定义法术入口已于 2026-09-16 取消（法术一律在战斗中用
    define_spell 自创）；探针只关心触发管线是否接线，故此捷径保留"学习"语义：
    仍然走同一个 _build_custom_spell/parse_spell_definition 校验与 wired 标注。
    """
    built = e._build_custom_spell(e.state.player, definition)
    if "error" in built:
        return {"success": False, "error": built["error"]}
    e.state.player.spells.append(built["spell"])
    parsed = built["parsed"]
    wired = parsed.trigger in e.combat._WIRED_TRIGGERS
    result = {"spell": built["spell"].to_dict(), "wired": wired, "cost": "0（探针捷径）"}
    if not wired:
        result["warning"] = (f"触发时机【{parsed.trigger}】已通过句式校验，"
                             "但该时机暂未接入战斗结算管线，本法术目前不会实际触发")
    return {"success": True, "action": "自定义法术(探针)", "result": result}

# ---------------------------------------------------------------------------
# 刻意**不**合并的两份 `_resolve_monster_turn_smart`：
#   * `sim/manual_handplay.py`：闪避阈值写成 per_hit >= 14 / >= 7（别处 12 / 6），
#     且没有"变形"与"减速"分支；
#   * `sim/multi_reincarnation_duel.py`：同上，另有一处魔法数字。
# 这两份是不同时期的手调策略，合并会改变它们在批量跑局里的闪避率 → 直接改变
# 产出的统计数据。是否统一成一套阈值属于**策略裁定**，不是纯重构，故保留原样，
# 只在各自文件里标注"另有公共版本见 sim/common.py"。
# ---------------------------------------------------------------------------
