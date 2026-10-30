"""【飞行⇄反转→压迫】与压迫的纯目标限制回归。"""
from engine.api import GameEngine
from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.gamedata import MONSTER_TRANSFORM_DAOWEN, ORIGINAL_MONSTER_DAOWEN
from engine.models import DaoWen, DaoWenInstance, Entity


def _engine():
    engine = GameEngine(autosave=False)
    player = Entity(
        "轮回者", "轮回者", blood_limit=50, current_hp=50,
        mana_limit=200, current_mana=200, speed_limit=4, current_speed=4,
        attack_count=2, attack_power=4,
    )
    monster = Entity(
        "靶怪", "怪物", blood_limit=10, current_hp=10,
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


def _give(entity, name):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="", cost_formula="", effect_formula=""),
        x_value=0, x_free=True,
    )


def _cast(engine, name, x, target_ref=None):
    params = {"daowen_name": name, "x": x, "dodge": False,
              "blood_shadow": False, "trigger_spell_choices": {}}
    if target_ref is not None:
        params["target_ref"] = target_ref
    return engine.execute_action("use_daowen", params)


def test_flight_yapo_registry_cost_and_bidirectional_reversal():
    flight = DaoWenEngine.resolve("飞行", 3)
    assert flight["cost_type"] == "冷却" and flight["cost"] == 3
    assert flight["duration"] == 3
    yapo = DaoWenEngine.resolve("压迫", 2)
    assert yapo["cost_type"] == "消耗" and yapo["cost"] == 20
    assert yapo["duration"] == 2
    assert "yapo_threshold" not in yapo
    assert "仅能显式选择自身为[目标]" in yapo["summary"]
    assert "持续2回合" in yapo["summary"]
    assert "死亡" not in yapo["summary"]
    assert "飞行" in ORIGINAL_MONSTER_DAOWEN
    assert "压迫" in MONSTER_TRANSFORM_DAOWEN
    assert ResonanceEngine.find_transformation("飞行", "反转") == "压迫"
    assert ResonanceEngine.find_transformation("压迫", "反转") == "飞行"
    assert ResonanceEngine.find_transformation("凋零", "反转") is None


def test_flight_cast_uses_cooldown_and_restores_untargetable_status():
    engine, player, monster = _engine()
    _give(player, "飞行")
    result = _cast(engine, "飞行", 2)
    assert result["success"] is True, result
    assert player.dao_wen["飞行"].cooldown_remaining == 2
    assert player.get_status_value("飞行") == 2
    assert player.has_status("飞行")
    assert engine.combat.is_targetable(monster, player) is False
    assert engine.combat.is_targetable(player, monster) is True


def test_yapo_can_target_any_character_and_lasts_x_rounds():
    engine, player, monster = _engine()
    _give(player, "压迫")
    friend = Entity("朋友", "朋友", blood_limit=10, current_hp=1)
    engine.state.friends = [friend]
    engine.combat._bind_hp_hook(friend)
    monster.current_hp = 3

    # 压迫可以选择任意显式[目标]，不是只能选施法者；X同时决定持续回合。
    result = _cast(engine, "压迫", 2, "enemy:0")
    assert result["success"] is True, result
    assert player.current_mana == 180
    assert not player.has_status("压迫")
    assert monster.has_status("压迫")
    assert monster.get_status_effects("压迫")[0].remaining_rounds == 2

    # 低生命角色不因压迫死亡；压迫没有凋零的阈值判死效果。
    assert friend.is_alive and friend.current_hp == 1
    assert monster.is_alive and monster.current_hp == 3

    # 只有本次[目标]怪物不受这条限制；玩家和朋友都只能显式选择自己。
    assert engine.combat.is_targetable(monster, player)
    assert engine.combat.is_targetable(monster, friend)
    assert engine.combat.is_targetable(monster, monster)
    assert engine.combat.is_targetable(player, player)
    assert engine.combat.is_targetable(friend, friend)
    assert not engine.combat.is_targetable(player, monster)
    assert not engine.combat.is_targetable(player, friend)
    assert not engine.combat.is_targetable(friend, player)
    assert not engine.combat.is_targetable(friend, monster)

    # 后续生命变化不会触发任何压迫判死钩子。
    monster.current_hp = 2
    assert monster.is_alive and monster.current_hp == 2
    assert not any(effect.get("type") == "yapo_execution"
                   for effect in result["execution"]["effects"])

def test_yapo_does_not_kill_new_entry_but_target_rule_applies_immediately():
    engine, player, monster = _engine()
    _give(player, "压迫")
    assert _cast(engine, "压迫", 2, "enemy:0")["success"] is True

    newcomer = Entity("新怪", "怪物", blood_limit=10, current_hp=2)
    engine.state.enemies.append(newcomer)
    engine.combat._bind_hp_hook(newcomer)
    engine.combat.round_start()
    assert newcomer.is_alive and newcomer.current_hp == 2
    assert not engine.combat.is_targetable(newcomer, player)
    assert engine.combat.is_targetable(newcomer, newcomer)
    assert engine.combat.is_targetable(monster, newcomer)
    assert not engine.combat.is_targetable(player, newcomer)


def test_diaoling_stays_instant_and_owns_no_yapo_mechanism():
    engine, player, monster = _engine()
    _give(player, "凋零")
    assert _cast(engine, "凋零", 3, "player:0")["success"] is True
    assert not player.has_status("凋零")
    assert not player.has_status("压迫")

    # 凋零只按施放当次快照；之后生命降到阈值，不会因凋零再次判死。
    engine.combat._apply_hostile_damage(monster, 7, source=player)
    assert monster.is_alive and monster.current_hp == 3
