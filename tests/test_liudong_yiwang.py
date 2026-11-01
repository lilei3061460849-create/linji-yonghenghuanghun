"""沉沦海【流动】【遗忘】运行时与状态对象迁移。"""
from engine.api import GameEngine
from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.models import DaoWen, DaoWenInstance, Entity, StatusEffect


def _engine():
    engine = GameEngine(autosave=False)
    player = Entity("轮回者", "轮回者", blood_limit=80, current_hp=80,
                    mana_limit=100, current_mana=100, speed_limit=3, current_speed=3)
    source = Entity("来源", "怪物", blood_limit=50, current_hp=50,
                    mana_limit=30, current_mana=30, speed_limit=2, current_speed=2)
    destination = Entity("接收者", "怪物", blood_limit=45, current_hp=45,
                         mana_limit=20, current_mana=20, speed_limit=2, current_speed=2)
    engine.state.player = player
    engine.state.enemies = [source, destination]
    engine.state.friends = []
    engine.state.employees = []
    engine.state.temp_friends = []
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.combat._bind_existing_hp_hooks()
    return engine, player, source, destination


def _give(entity, name, mana=None):
    entity.dao_wen[name] = DaoWenInstance(DaoWen(
        name=name, formula="", cost_type="", cost_formula="", effect_formula=""),
        x_value=0, x_free=True)
    if mana is not None:
        entity.mana_limit = mana
        entity.current_mana = mana


def _cast(engine, name, x, *, source_ref="enemy:0", **extra):
    params = {"daowen_name": name, "x": x, "target_ref": source_ref,
              "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}}
    params.update(extra)
    return engine.execute_action("use_daowen", params)


def test_liudong_yiwang_registry_costs():
    target = Entity("目标", "怪物")
    flow = DaoWenEngine.resolve("流动", 2, target=target)
    forget = DaoWenEngine.resolve("遗忘", 2, target=target)
    assert flow["cost_type"] == "消耗" and flow["cost"] == 20
    assert flow["transfer_status_count"] == 2
    assert forget["cost_type"] == "消耗" and forget["cost"] == 40
    assert forget["remove_status_count"] == 2


def test_yiwang_removes_exact_selected_status_and_pays_mana():
    engine, player, source, _ = _engine()
    _give(player, "遗忘")
    source.add_status(StatusEffect("坏死", value=2, remaining_rounds=3, source="test"))
    source.add_status(StatusEffect("全力", value=1, remaining_rounds=4, source="test"))
    result = _cast(engine, "遗忘", 1, status_indices=[0])
    assert result["success"] is True, result
    assert player.current_mana == 80
    assert player.actions_used_this_round == 1
    assert [status.name for status in source.status_effects] == ["全力"]
    assert result["execution"]["effects"][0]["type"] == "statuses_removed"


def test_liudong_moves_original_status_object_to_other_target():
    engine, player, source, destination = _engine()
    _give(player, "流动")
    status = StatusEffect("坏死", value=2, remaining_rounds=3, source="test",
                          effect_data={"marker": "保留"})
    source.add_status(status)
    result = _cast(engine, "流动", 1, status_indices=[0], flow_target_ref="enemy:1")
    assert result["success"] is True, result
    assert player.current_mana == 90
    assert source.status_effects == []
    assert destination.status_effects == [status]
    assert destination.status_effects[0] is status
    assert status.remaining_rounds == 3 and status.effect_data["marker"] == "保留"


def test_yiwang_insufficient_statuses_rejected_before_payment():
    engine, player, source, _ = _engine()
    _give(player, "遗忘")
    source.add_status(StatusEffect("坏死", remaining_rounds=3))
    result = _cast(engine, "遗忘", 2)
    assert result["success"] is False
    assert player.current_mana == 100 and player.actions_used_this_round == 0
    assert [status.name for status in source.status_effects] == ["坏死"]


def test_yiwang_exact_count_can_auto_select_but_ambiguity_cannot():
    engine, player, source, _ = _engine()
    _give(player, "遗忘")
    source.add_status(StatusEffect("坏死", remaining_rounds=3))
    assert _cast(engine, "遗忘", 1)["success"] is True

    engine, player, source, _ = _engine()
    _give(player, "遗忘")
    source.add_status(StatusEffect("坏死", remaining_rounds=3))
    source.add_status(StatusEffect("全力", remaining_rounds=2))
    result = _cast(engine, "遗忘", 1)
    assert result["success"] is False
    assert player.current_mana == 100 and len(source.status_effects) == 2


def test_liudong_rejects_same_source_and_destination_without_payment():
    engine, player, source, _ = _engine()
    _give(player, "流动")
    source.add_status(StatusEffect("坏死", remaining_rounds=3))
    result = _cast(engine, "流动", 1, status_indices=[0], flow_target_ref="enemy:0")
    assert result["success"] is False
    assert player.current_mana == 100 and player.actions_used_this_round == 0
    assert source.has_status("坏死")


def test_instant_spell_liudong_uses_same_transfer_path():
    engine, player, source, destination = _engine()
    _give(player, "流动")
    source.add_status(StatusEffect("坏死", value=2, remaining_rounds=3))
    execution = engine.combat.build_instant_execution(
        player, "发动流动X于目标", source,
        [{"x": 1, "status_indices": [0], "flow_target_ref": "enemy:1", "dodge": False}],
        engine.combat._combat_entity_refs())
    results = execution.run_all()
    assert results[0].status.value == "completed"
    assert player.current_mana == 90
    assert not source.has_status("坏死") and destination.has_status("坏死")


def test_yiwang_wave_uses_per_source_status_indices_without_multiplying_cost():
    engine, player, source, wave_target = _engine()
    _give(player, "遗忘")
    source.add_status(StatusEffect("坏死", remaining_rounds=3))
    wave_target.add_status(StatusEffect("全力", remaining_rounds=2))
    wave_target.add_status(StatusEffect("波及", remaining_rounds=-1, source=player.name))
    result = _cast(engine, "遗忘", 1,
                   status_indices={"enemy:0": [0], "enemy:1": [0]})
    assert result["success"] is True, result
    assert player.current_mana == 80
    assert not source.has_status("坏死") and not wave_target.has_status("坏死")
    assert not wave_target.has_status("全力")
    assert wave_target.has_status("波及")
    removed = [effect for effect in result["execution"]["effects"]
               if effect["type"] == "statuses_removed"]
    assert len(removed) == 2


def test_yiwang_removing_ice_releases_frozen_daowen_and_paused_effects():
    engine, player, source, _ = _engine()
    _give(player, "冰冻")
    _give(player, "遗忘")
    _give(source, "飞行")
    player.add_status(StatusEffect("飞行", remaining_rounds=3))
    source.add_status(StatusEffect("飞行", remaining_rounds=3))
    frozen = _cast(engine, "冰冻", 1, freeze_daowen=["飞行"])
    assert frozen["success"] is True, frozen
    assert source.dao_wen["飞行"].ice_frozen and not source.has_status("飞行")
    player.actions_used_this_round = 0
    engine.state.duel_turn = "player_side"
    removed = _cast(engine, "遗忘", 1, status_indices=[0])
    assert removed["success"] is True, removed
    assert not source.dao_wen["飞行"].ice_frozen
    assert source.has_status("飞行")
    assert source.paused_status_effects == []


def test_liudong_transferring_ice_reconciles_source_and_destination():
    engine, player, source, destination = _engine()
    _give(player, "流动")
    _give(source, "杀伐")
    _give(destination, "杀伐")
    source.dao_wen["杀伐"].ice_frozen = True
    source.add_status(StatusEffect(
        "冰冻", value=1, remaining_rounds=-1,
        effect_data={"source_daowen": "冰冻", "frozen_daowen": ["杀伐"]}))
    source.paused_status_effects = [StatusEffect(
        "杀伐", remaining_rounds=2, effect_data={"source_daowen": "杀伐"})]
    destination.add_status(StatusEffect(
        "杀伐", remaining_rounds=4, effect_data={"source_daowen": "杀伐"}))

    result = _cast(engine, "流动", 1, status_indices=[0], flow_target_ref="enemy:1")
    assert result["success"] is True, result
    assert not source.dao_wen["杀伐"].ice_frozen
    assert source.has_status("杀伐") and source.paused_status_effects == []
    assert destination.dao_wen["杀伐"].ice_frozen
    assert destination.has_status("冰冻") and not destination.has_status("杀伐")
    assert [status.name for status in destination.paused_status_effects] == ["杀伐"]


def test_monster_phase_yiwang_selects_status_and_pays_mana():
    from tests.monster_phase_support import resolve_monster_phase
    engine, player, monster, _ = _engine()
    engine.state.enemies = [monster]
    _give(monster, "遗忘", mana=20)
    player.add_status(StatusEffect("坏死", remaining_rounds=3))
    prepared = engine.combat.prepare_monster_phase()
    option = next(option for actor in prepared["actors"]
                  for option in actor["daowen_options"] if option["resolves_as"] == "遗忘")
    assert [target["ref"] for target in option["target_options"]] == ["player:0"]
    result = resolve_monster_phase(engine.combat, {"enemy:0": "遗忘"},
                                   target_refs={"enemy:0": "player:0"})
    assert "error" not in result, result
    assert monster.current_mana == 0
    assert not player.has_status("坏死")


def test_monster_phase_liudong_uses_explicit_destination_and_status():
    from tests.monster_phase_support import resolve_monster_phase
    engine, player, monster, _ = _engine()
    engine.state.enemies = [monster]
    _give(monster, "流动", mana=10)
    player.add_status(StatusEffect("坏死", remaining_rounds=2))
    prepared = engine.combat.prepare_monster_phase()
    option = next(option for actor in prepared["actors"]
                  for option in actor["daowen_options"] if option["resolves_as"] == "流动")
    schema = option["extra_choice_schema"]
    assert schema["flow_target_ref"]["options"]["player:0"] == ["enemy:0"]
    result = resolve_monster_phase(engine.combat, {"enemy:0": "流动"},
                                   target_refs={"enemy:0": "player:0"})
    assert "error" not in result, result
    assert monster.current_mana == 0
    assert not player.has_status("坏死") and monster.has_status("坏死")
    assert monster.get_status_effects("坏死")[0].remaining_rounds == 2


def test_new_sin_city_nodes_fill_existing_loop_slots_without_losing_spur():
    assert ResonanceEngine.find_transformation("假钞", "曲解", "交易") == "交易"
    assert ResonanceEngine.find_transformation("交易", "转换", "赦免") == "赦免"
    assert ResonanceEngine.find_transformation("点金", "反转", "烁金") == "烁金"
    assert ResonanceEngine.find_transformation("勾魂", "曲解", "凋零") == "凋零"
    assert ResonanceEngine.find_transformation("凋零", "曲解", "招魂") == "招魂"
    assert ResonanceEngine.find_transformation("勾魂", "曲解", "（待补）") is None
