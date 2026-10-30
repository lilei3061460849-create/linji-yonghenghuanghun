"""【凋零】一次性当前生命阈值判死与目标、分裂交互回归。"""
from engine.api import GameEngine
from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.gamedata import REGION_EXCLUSIVE_DAOWEN, learnability_error
from engine.models import DaoWen, DaoWenInstance, Entity, StatusEffect


def _engine():
    engine = GameEngine(autosave=False)
    player = Entity(
        "轮回者", "轮回者", blood_limit=200, current_hp=200,
        mana_limit=200, current_mana=200, speed_limit=4, current_speed=4,
        attack_count=2, attack_power=4,
    )
    monster = Entity(
        "靶怪", "怪物", blood_limit=100, current_hp=100,
        mana_limit=100, current_mana=100, speed_limit=2, current_speed=2,
        attack_count=1, attack_power=3,
    )
    engine.state.player = player
    engine.state.friends = []
    engine.state.employees = []
    engine.state.temp_friends = []
    engine.state.enemies = [monster]
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.current_round = 2
    engine.combat._bind_existing_hp_hooks()
    return engine, player, monster


def _give(entity, name, cost_type="消耗", cost_formula="10X"):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type=cost_type,
               cost_formula=cost_formula, effect_formula=""),
        x_value=0, x_free=True,
    )


def _next_round(engine):
    engine.combat.round_start()
    engine.state.combat_subphase = "player_actions"
    engine.state.duel_turn = "player_side"


def _cast(engine, name, x, target_ref="player:0"):
    params = {
        "daowen_name": name, "x": x,
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    }
    if target_ref is not None:
        params["target_ref"] = target_ref
    return engine.execute_action("use_daowen", params)


def test_cost_ownership_and_no_unconfirmed_resonance_edge():
    calc = DaoWenEngine.resolve("凋零", 3)
    assert calc["cost_type"] == "消耗" and calc["cost"] == 30
    assert "duration" not in calc
    assert calc["diaoling_threshold"] == 3
    assert "除[目标]外" in calc["summary"] and "持续" not in calc["summary"]
    assert "凋零" in REGION_EXCLUSIVE_DAOWEN["乱葬岗"]
    known = set(DaoWenEngine.list_all())
    assert learnability_error(
        "凋零", owned={"分裂"}, region="乱葬岗", known=known) is None
    assert "当前副本无法习得" in learnability_error(
        "凋零", owned={"分裂"}, region="龙心谷", known=known)
    for kind in ("转换", "反转", "曲解"):
        assert ResonanceEngine.find_transformation("凋零", kind) is None
        assert ResonanceEngine.find_transformation("分裂", kind) != "凋零"


def test_target_is_required_and_non_self_target_does_not_exempt_caster():
    engine, player, monster = _engine()
    _give(player, "凋零")
    action = next(
        row for row in engine.get_available_actions()["actions"]
        if row.get("action_type") == "use_daowen"
        and row.get("params_schema", {}).get("daowen_name") == "凋零"
    )
    assert {entry["ref"] for entry in action["params_schema"]["target_ref"]} == {
        "player:0", "enemy:0"
    }

    missing = _cast(engine, "凋零", 2, target_ref=None)
    assert missing["success"] is False
    assert "需要显式指定目标" in missing["error"]
    assert player.current_mana == 200
    assert player.actions_used_this_round == 0

    player.current_hp = 1
    monster.current_hp = 50
    result = _cast(engine, "凋零", 2, target_ref="enemy:0")
    assert result["success"] is True, result
    assert player.current_mana == 180
    assert not player.is_alive and player.current_hp == 0  # 施法者并非[目标]，不获豁免
    assert player._death_ctx["source"] == "凋零"
    assert monster.is_alive and monster.current_hp == 50  # 只有[目标]被排除
    assert not monster.has_status("凋零")


def test_cast_checks_every_other_character_but_not_target_or_above_threshold():
    engine, player, monster = _engine()
    friend = Entity("朋友", "朋友", blood_limit=10, current_hp=1)
    employee = Entity("员工", "员工", blood_limit=10, current_hp=2, is_deployed=True)
    creation = Entity("造物", "造物", blood_limit=1, current_hp=1)
    engine.state.friends = [friend]
    engine.state.employees = [employee]
    engine.state.temp_friends = [creation]
    engine.combat._bind_existing_hp_hooks()
    player.current_hp = 1
    monster.current_hp = 2
    monster.shield = 50
    above = Entity("高血怪", "怪物", blood_limit=10, current_hp=3)
    engine.state.enemies.append(above)
    engine.combat._bind_hp_hook(above)
    _give(player, "凋零")

    result = _cast(engine, "凋零", 2)
    assert result["success"] is True, result
    assert player.current_mana == 180
    assert player.is_alive and player.current_hp == 1  # [目标]为自身，因此被排除
    for entity in (monster, friend, employee, creation):
        assert not entity.is_alive
        assert entity.current_hp == 0
        assert entity._death_ctx["source"] == "凋零"
    assert above.is_alive and above.current_hp == 3
    assert monster.shield == 50  # 直接判死，不是可被格挡吸收的伤害
    assert not player.has_status("凋零")
    effects = result["execution"]["effects"]
    executed = {effect["target"] for effect in effects if isinstance(effect, dict)
                and effect.get("type") == "diaoling_execution"}
    assert executed == {monster.name, friend.name, employee.name, creation.name}


def test_later_hp_change_does_not_trigger_until_cast_again():
    engine, player, monster = _engine()
    monster.blood_limit = 10  # 降至3仍为30%，避免低血比例先触发救赎离场
    monster.current_hp = 10
    _give(player, "凋零")
    assert _cast(engine, "凋零", 3)["success"] is True
    assert monster.is_alive and monster.current_hp == 10
    assert not player.has_status("凋零")

    detail = engine.combat._apply_hostile_damage(monster, 7, source=player)
    assert detail["actual_damage"] == 7
    assert monster.is_alive and monster.current_hp == 3  # 达到旧阈值也不再自动判死

    _next_round(engine)  # 基础1出手：下一回合才能再次主动发动
    result = _cast(engine, "凋零", 3)
    assert result["success"] is True, result
    assert not monster.is_alive and monster.current_hp == 0  # 再次主动发动才重新判定
    assert monster._death_ctx["source"] == "凋零"


def test_creations_entering_after_cast_are_not_executed():
    engine, player, monster = _engine()
    monster.current_hp = 50
    _give(player, "凋零")
    assert _cast(engine, "凋零", 1)["success"] is True
    _give(player, "分裂", cost_type="流血", cost_formula="3X")

    _next_round(engine)
    result = _cast(engine, "分裂", 3)
    assert result["success"] is True, result
    assert player.current_hp == 191
    assert len(engine.state.temp_friends) == 3
    assert all(creation.is_alive and creation.current_hp == 1
               for creation in engine.state.temp_friends)
    effects = result["execution"]["effects"]
    assert not [effect for effect in effects if isinstance(effect, dict)
                and effect.get("type") == "diaoling_execution"]


def test_direct_execution_is_not_blocked_by_hp_floor_status():
    engine, player, monster = _engine()
    monster.current_hp = 2
    monster.add_status(StatusEffect("不死", 1, 3, "test"))
    _give(player, "凋零")

    result = _cast(engine, "凋零", 2)
    assert result["success"] is True, result
    assert not monster.is_alive
    assert monster.current_hp == 0
    assert monster._death_ctx["source"] == "凋零"
