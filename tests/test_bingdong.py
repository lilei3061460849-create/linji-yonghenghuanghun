"""沉沦海【冰冻】：冷却X、自动降X、冻结道纹与暂停已有效果。"""
from engine.api import GameEngine
from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.enums import EffectScope
from engine.models import DaoWen, DaoWenInstance, Entity, StatusEffect


def _engine():
    engine = GameEngine(autosave=False)
    player = Entity("轮回者", "轮回者", blood_limit=50, current_hp=50,
                    mana_limit=50, current_mana=50, speed_limit=3, current_speed=3)
    target = Entity("目标", "怪物", blood_limit=40, current_hp=40,
                    mana_limit=20, current_mana=20, speed_limit=2, current_speed=2)
    engine.state.player = player
    engine.state.enemies = [target]
    engine.state.friends = []
    engine.state.employees = []
    engine.state.temp_friends = []
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.combat._bind_existing_hp_hooks()
    return engine, player, target


def _give(entity, name):
    entity.dao_wen[name] = DaoWenInstance(DaoWen(
        name=name, formula="", cost_type="", cost_formula="", effect_formula=""),
        x_value=0, x_free=True)


def _cast(engine, x, choices, target_ref="enemy:0"):
    return engine.execute_action("use_daowen", {
        "daowen_name": "冰冻", "x": x, "target_ref": target_ref,
        "freeze_daowen": choices, "dodge": False, "blood_shadow": False,
        "trigger_spell_choices": {},
    })


def test_bingdong_registry_and_sinking_sea_loop():
    target = Entity("目标", "怪物")
    calc = DaoWenEngine.resolve("冰冻", 3, target=target)
    assert calc["cost_type"] == "冷却" and calc["cost"] == 3
    assert calc["duration"] == -1 and calc["freeze_count"] == 3
    assert ResonanceEngine.find_transformation("遗忘", "曲解") == "冰冻"
    assert ResonanceEngine.find_transformation("冰冻", "曲解", "大梦") == "大梦"
    assert ResonanceEngine.find_transformation("大梦", "曲解", "冰冻") == "冰冻"
    assert ResonanceEngine.find_transformation("遗忘", "曲解", "大梦") is None


def test_bingdong_clamps_x_before_cooldown_and_pauses_existing_effects():
    engine, player, target = _engine()
    _give(player, "冰冻")
    _give(target, "杀伐")
    _give(target, "飞行")
    player.add_status(StatusEffect("飞行", value=1, remaining_rounds=3, source="test"))
    target.add_status(StatusEffect("飞行", value=2, remaining_rounds=3, source="test"))

    # 提交X=5，目标实际只有2种：支付、冷却与冻结数量必须一起降到2。
    result = _cast(engine, 5, ["杀伐", "飞行"])
    assert result["success"] is True, result
    assert result["calculation"]["x"] == 2
    assert result["calculation"]["cost"] == 2
    assert player.dao_wen["冰冻"].cooldown_remaining == 2
    assert player.actions_used_this_round == 1
    assert target.dao_wen["杀伐"].ice_frozen
    assert target.dao_wen["飞行"].ice_frozen
    assert not target.dao_wen["杀伐"].can_use()
    assert not target.dao_wen["飞行"].can_use()

    # 既有飞行状态进入暂停区，不再让目标获得飞行保护，也不再倒计。
    assert not target.has_status("飞行")
    assert [status.name for status in target.paused_status_effects] == ["飞行"]
    assert engine.combat.is_targetable(Entity("非飞行观察者", "怪物"), target)
    target.tick_status_effects()
    assert target.paused_status_effects[0].remaining_rounds == 3
    ice = target.get_status_effects("冰冻")[0]
    assert ice.remaining_rounds == -1
    assert ice.effect_data["frozen_daowen"] == ["杀伐", "飞行"]
    exported = target.to_dict()
    assert exported["ice_frozen_daowen"] == ["杀伐", "飞行"]
    assert [status["name"] for status in exported["paused_status_effects"]] == ["飞行"]
    import pickle
    restored = pickle.loads(pickle.dumps(target))
    assert restored.dao_wen["杀伐"].ice_frozen
    assert restored.paused_status_effects[0].name == "飞行"


def test_bingdong_requires_exact_distinct_owned_choices_without_payment():
    engine, player, target = _engine()
    _give(player, "冰冻")
    _give(target, "杀伐")
    _give(target, "再生")
    rejected = _cast(engine, 2, ["杀伐", "杀伐"])
    assert rejected["success"] is False
    assert player.dao_wen["冰冻"].cooldown_remaining == 0
    assert player.actions_used_this_round == 0
    assert not target.dao_wen["杀伐"].ice_frozen


def test_bingdong_target_with_zero_daowen_auto_skips_without_cost():
    engine, player, target = _engine()
    _give(player, "冰冻")
    result = _cast(engine, 4, [])
    assert result["success"] is True and result["skipped"] is True
    assert result["result"]["x"] == 0
    assert player.dao_wen["冰冻"].cooldown_remaining == 0
    assert player.actions_used_this_round == 0


def test_bingdong_pauses_bizhong_counter_without_erasing_it():
    engine, player, target = _engine()
    _give(player, "冰冻")
    _give(target, "必中")
    engine.combat.grant_bizhong(target, 2)
    assert engine.combat.bizhong_remaining(target) == 2
    assert _cast(engine, 1, ["必中"])["success"] is True
    assert engine.combat.bizhong_remaining(target) == 0
    assert not engine.combat.consume_bizhong(target)
    assert target._bizhong_left == 2
    engine.combat.release_battle_ice(target)
    assert engine.combat.bizhong_remaining(target) == 2


def test_bingdong_pauses_effective_stat_overrides_not_only_status_display():
    engine, player, target = _engine()
    _give(player, "冰冻")
    _give(target, "全力")
    target.current_mana = 5
    base_power = target.effective_attack_power()
    calc = DaoWenEngine.resolve("全力", 3, target=target, caster=target)
    engine.combat.apply_daowen_effect(caster=target, target=target, name="全力", calc=calc)
    assert target.effective_attack_power() > base_power
    assert _cast(engine, 1, ["全力"])["success"] is True
    assert target.effective_attack_power() == base_power
    assert not target.has_status("全力")
    assert target.paused_status_effects[0].name == "全力"


def test_monster_phase_bingdong_uses_cooldown_x1_and_freezes_player_daowen():
    from tests.monster_phase_support import resolve_monster_phase
    engine, player, monster = _engine()
    _give(monster, "冰冻")
    _give(player, "杀伐")
    _give(player, "再生")
    prepared = engine.combat.prepare_monster_phase()
    option = next(option for actor in prepared["actors"]
                  for option in actor["daowen_options"] if option["resolves_as"] == "冰冻")
    assert {target["ref"] for target in option["target_options"]} == {"player:0", "enemy:0"}
    result = resolve_monster_phase(engine.combat, {"enemy:0": "冰冻"},
                                   target_refs={"enemy:0": "player:0"})
    assert "error" not in result, result
    assert monster.dao_wen["冰冻"].cooldown_remaining == 1
    assert player.dao_wen["杀伐"].ice_frozen
    assert not player.dao_wen["再生"].ice_frozen


def test_tactical_ai_submits_bingdong_choices_for_stable_target():
    from engine.ai_tactics import TacticalAI
    engine, player, target = _engine()
    _give(player, "冰冻")
    _give(target, "杀伐")
    _give(target, "再生")
    ai = TacticalAI(engine)
    params = ai._params("冰冻", 5, target.name)
    assert params["target_ref"] == "enemy:0"
    assert params["freeze_daowen"] == ["杀伐", "再生"]


def test_instant_spell_bingdong_uses_same_clamped_cost_and_effect():
    engine, player, target = _engine()
    _give(player, "冰冻")
    _give(target, "杀伐")
    _give(target, "再生")
    execution = engine.combat.build_instant_execution(
        player, "发动冰冻X于目标", target,
        [{"x": 5, "freeze_daowen": ["杀伐", "再生"], "dodge": False}],
        engine.combat._combat_entity_refs())
    results = execution.run_all()
    assert results[0].status.value == "completed"
    assert results[0].x == 2
    assert player.dao_wen["冰冻"].cooldown_remaining == 2
    assert target.dao_wen["杀伐"].ice_frozen
    assert target.dao_wen["再生"].ice_frozen


def test_instant_spell_bingdong_zero_daowen_target_skips_without_cooldown():
    engine, player, target = _engine()
    _give(player, "冰冻")
    execution = engine.combat.build_instant_execution(
        player, "发动冰冻X于目标", target,
        [{"x": 3, "freeze_daowen": [], "dodge": False}],
        engine.combat._combat_entity_refs())
    results = execution.run_all()
    assert results[0].status.value == "skipped"
    assert results[0].x == 0
    assert player.dao_wen["冰冻"].cooldown_remaining == 0


def test_battle_end_api_releases_ice_on_all_remaining_characters():
    engine, player, target = _engine()
    engine.state.enemies = []
    engine.state.friends = [target]
    _give(player, "冰冻")
    _give(target, "杀伐")
    assert _cast(engine, 1, ["杀伐"], target_ref="friend:0")["success"] is True
    assert target.dao_wen["杀伐"].ice_frozen
    result = engine.execute_action("battle_end", {})
    assert result["success"] is True, result
    assert not target.dao_wen["杀伐"].ice_frozen
    assert target.paused_status_effects == []
    assert not target.has_status("冰冻")


def test_release_battle_ice_restores_daowen_and_paused_statuses():
    engine, player, target = _engine()
    _give(player, "冰冻")
    _give(target, "飞行")
    player.add_status(StatusEffect("飞行", value=1, remaining_rounds=2, source="test"))
    target.add_status(StatusEffect("飞行", value=1, remaining_rounds=2,
                                   source="test", scope=EffectScope.RUN.value))
    assert _cast(engine, 1, ["飞行"])["success"] is True
    assert not target.has_status("飞行")
    engine.combat.release_battle_ice(target)
    assert target.dao_wen["飞行"].can_use()
    assert target.has_status("飞行")
    assert target.paused_status_effects == []
    assert target.get_status_effects("飞行")[0].remaining_rounds == 2
