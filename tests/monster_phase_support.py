"""测试专用：为两阶段怪物API构造完整显式选择；不属于生产决策层。"""
from __future__ import annotations


def _decline_spells(option):
    return {
        timing: {spell["spell_name"]: {"use": False} for spell in option.get("spell_options", {}).get(timing, [])}
        for timing in ("before", "after")
    }


def resolve_monster_phase(combat, daowen_choices=None, *, dodge=False, target_refs=None):
    """直接调用CombatEngine的prepare/resolve，供计算单元测试复用。

    daowen_choices: {actor_ref或怪物名: 道纹名/None}；未提供时选第一个合法项。
    target_refs: {actor_ref或怪物名: 目标ref}，默认取prepare列出的第一个合法目标。
    """
    daowen_choices = daowen_choices or {}
    target_refs = target_refs or {}
    prepared = combat.prepare_monster_phase()
    choices = []
    for actor in prepared["actors"]:
        key = actor["actor_ref"]
        monster = actor["monster"]
        requested = daowen_choices.get(key, daowen_choices.get(monster, "__first__"))
        dao = None
        action_count = actor["base_attack_actions"]
        hit_count = actor["base_hits_per_attack"]
        if requested == "__first__" and actor["daowen_options"]:
            requested = actor["daowen_options"][0]["name"]
        if requested is not None and requested != "__first__":
            option = next(o for o in actor["daowen_options"] if o["name"] == requested)
            dao = {"name": requested, "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {
                       holder_ref: {spell["spell_name"]: {"use": False} for spell in spells}
                       for holder_ref, spells in option.get("trigger_spell_options", {}).items()
                   }}
            if option["requires_target"]:
                dao["target_ref"] = target_refs.get(
                    key, target_refs.get(monster, option["target_options"][0]["ref"]))
            if option["dodge_submission"] == "per_target":
                # 波及X：恰好提交X个目标（候选全量提交会在候选>X时被拒，2026-08-22）
                from sim.monster_targets import pick_wave_dodge_targets
                dao["dodge_targets"] = pick_wave_dodge_targets(option)
            elif option["resolves_as"] == "冰冻":
                ice_schema = option.get("extra_choice_schema", {}).get("freeze_daowen", {})
                dao["freeze_daowen"] = list(ice_schema.get("options", {}).get(
                    dao.get("target_ref"), []))[:1]  # 怪物冷却型道纹固定X=1
            elif option["resolves_as"] in {"流动", "遗忘"}:
                extra_schema = option.get("extra_choice_schema", {})
                status_options = extra_schema.get("status_indices", {}).get("options", {})
                dao["status_indices"] = {
                    ref: [entry["index"] for entry in entries[:int(option.get("x", 1))]]
                    for ref, entries in status_options.items()
                }
                if option["resolves_as"] == "流动":
                    destination_options = extra_schema.get("flow_target_ref", {}).get(
                        "options", {}).get(dao.get("target_ref"), [])
                    if not destination_options:
                        raise ValueError("流动prepare没有可用接收目标")
                    dao["flow_target_ref"] = destination_options[0]
            elif option["resolves_as"] == "变形":
                enemy_index = int(key.split(":", 1)[1])
                hit_count = combat.state.enemies[enemy_index].attack_power
        attack_target = actor["attack_target_options"][0]["ref"]
        target_option = next(target for target in actor["attack_target_options"]
                             if target["ref"] == attack_target)
        # 当前每次提交恰1个行动：选择道纹时不得再捆绑一组攻击。
        attacks = [{"hits": [{"target_ref": attack_target, "dodge": bool(dodge),
                               "blood_shadow": False,
                               "spell_choices": _decline_spells(target_option)}
                              for _ in range(hit_count)]}
                   for _ in range(action_count if dao is None else 0)]
        choices.append({"actor_ref": key, "daowen": dao, "attack_actions": attacks})
    return combat.resolve_monster_phase(choices, prepared=prepared)
