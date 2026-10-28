"""【分裂】双参数造物规则。

分裂X/Y：流血X+Y，每次发动只创造一个 X 攻击力、Y 血限的造物。
造物当前生命=Y，具有1次普攻，无道纹、不继承施法者面板/偏爱/遗物，战终消散。

运行方式：python -m pytest tests/test_fenlie_split.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.daowen import DaoWenEngine
from engine.models import DaoWen, DaoWenInstance, Entity, Relic, StatusEffect


def _engine(actor_is_monster: bool = False):
    engine = GameEngine(autosave=False)
    player = Entity("轮回者", "轮回者", blood_limit=200, current_hp=200,
                    mana_limit=99, current_mana=99,
                    speed_limit=9, current_speed=9,
                    attack_count=9, attack_power=99)
    monster = Entity("怪", "怪物", blood_limit=300, current_hp=300,
                     mana_limit=10, current_mana=10,
                     speed_limit=2, current_speed=2,
                     attack_count=2, attack_power=10)
    engine.state.player = player
    engine.state.friends = []
    engine.state.employees = []
    engine.state.enemies = [monster]
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.current_round = 2
    actor = monster if actor_is_monster else player
    actor.dao_wen["分裂"] = DaoWenInstance(
        DaoWen(name="分裂", formula="", cost_type="流血",
               cost_formula="X+Y", effect_formula="创造一个X攻击力/Y血限的造物"),
        x_value=0, x_free=True)
    return engine, player, monster, actor


def _cast(engine, actor, *, x=3, y=2, actor_ref=""):
    extra = {"actor_ref": actor_ref} if actor_ref else {}
    return engine.execute_action("use_daowen", {
        **extra, "daowen_name": "分裂", "x": x, "y": y,
        "target_ref": "enemy:0" if actor_ref else "player:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })


def test_cost_is_x_plus_y_and_each_cast_has_one_creation():
    for x, y in ((1, 1), (3, 2), (2, 5), (4, 3)):
        calc = DaoWenEngine.resolve("分裂", x, y=y)
        assert calc["cost_type"] == "流血"
        assert calc["cost_hp"] == x + y
        assert calc["split_creations"] == 1
        assert calc["creation_attack_power"] == x
        assert calc["creation_blood_limit"] == y
        assert calc["creation_attack_count"] == 1


def test_y_defaults_to_one():
    calc = DaoWenEngine.resolve("分裂", 3)
    assert calc["cost_hp"] == 4
    assert calc["creation_attack_power"] == 3
    assert calc["creation_blood_limit"] == 1


@pytest.mark.parametrize("x,y", [(-1, 1), (1, 0), (1, -1), (True, 1), (1, True)])
def test_split_rejects_negative_or_boolean_parameters(x, y):
    with pytest.raises(ValueError, match="分裂[XY]必须是"):
        DaoWenEngine.resolve("分裂", x, y=y)


def test_decay_can_reduce_effective_attack_x_to_zero_after_valid_submission():
    caster = Entity("施法者", "轮回者", blood_limit=20, current_hp=20)
    caster.add_status(StatusEffect(name="退化", value=1, remaining_rounds=1))
    calc = DaoWenEngine.resolve("分裂", 1, y=2, caster=caster)
    assert calc["x"] == 0
    assert calc["cost_hp"] == 2
    assert calc["creation_attack_power"] == 0
    assert calc["creation_blood_limit"] == 2


def test_cast_pays_bleed_and_creates_exactly_one_creation_with_effective_stats():
    engine, player, _, _ = _engine()
    result = _cast(engine, player, x=3, y=2)
    assert result["success"] is True, result
    assert player.current_hp == 195
    assert player.blood_limit == 200
    assert len(engine.state.temp_friends) == 1

    creation = engine.state.temp_friends[0]
    assert creation.entity_type == "造物"
    assert creation.blood_limit == creation.current_hp == 2
    assert creation.mana_limit == creation.current_mana == 3
    assert creation.speed_limit == creation.current_speed == 1
    assert creation.attack_power == 3 and creation.attack_count == 1
    assert creation.effective_attack_power() == 3
    assert creation.effective_attack_count() == 1
    assert creation.copy_creator_runtime_id == player.runtime_id
    assert creation.is_created_being is False  # 分裂造物不是【造生】妖怪


def test_creation_has_no_daowen_and_does_not_inherit_caster_build():
    engine, player, _, _ = _engine()
    player.dao_wen["庇护"] = DaoWenInstance(
        DaoWen(name="庇护", formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=0, x_free=True)
    player.preferences = ["痛苦的偏爱", "时间的偏爱"]
    player.relics = [Relic(name="测试遗物", effect="不应继承")]
    player.artifacts = ["测试法器"]

    result = _cast(engine, player, x=7, y=11)
    assert result["success"] is True, result
    creation = engine.state.temp_friends[0]
    assert creation.dao_wen == {}
    assert creation.preferences == []
    assert creation.relics == []
    assert creation.artifacts == []
    assert creation.effective_attack_power() == 7
    assert creation.effective_attack_count() == 1
    assert creation.blood_limit == creation.current_hp == 11


def test_monster_split_adds_one_creation_to_enemy_side():
    engine, _, monster, _ = _engine(actor_is_monster=True)
    enemies_before = len(engine.state.enemies)
    creation = engine.combat._spawn_fenlie_creation(monster, 7, 30)
    assert len(engine.state.enemies) == enemies_before + 1
    assert engine.state.enemies[-1] is creation
    assert creation not in engine.state.temp_friends
    assert creation.entity_type == "造物"
    assert creation.effective_attack_power() == 7
    assert creation.effective_attack_count() == 1
    assert creation.blood_limit == creation.current_hp == 30
    assert creation.dao_wen == {} and creation.preferences == []


def test_repeated_casts_create_distinct_units_one_per_call():
    engine, _, monster, _ = _engine(actor_is_monster=True)
    first = engine.combat._spawn_fenlie_creation(monster, 2, 3)
    second = engine.combat._spawn_fenlie_creation(monster, 4, 5)
    assert first is not second
    assert first.name != second.name
    assert first.effective_attack_power() == 2
    assert second.effective_attack_power() == 4


def test_no_creation_on_caster_death_without_casting():
    engine, player, _, _ = _engine()
    player.current_hp = 0
    player.is_alive = False
    engine.combat._on_entity_death(player, ctx={})
    assert engine.state.temp_friends == []


def test_bleed_is_refused_atomically_when_current_hp_cannot_pay_x_plus_y():
    engine, player, _, _ = _engine()
    player.current_hp = 4  # X=3、Y=2，需支付5
    result = _cast(engine, player, x=3, y=2)
    assert result["success"] is False
    assert "流血" in result.get("error", "")
    assert player.current_hp == 4
    assert engine.state.temp_friends == []


def test_paying_last_hp_kills_caster_but_paid_creation_still_enters():
    engine, player, _, _ = _engine()
    player.current_hp = 5
    result = _cast(engine, player, x=3, y=2)
    assert result["success"] is True, result
    assert player.current_hp == 0 and player.is_alive is False
    assert len(engine.state.temp_friends) == 1


def test_creation_disperses_at_battle_end():
    engine, player, monster, _ = _engine()
    result = _cast(engine, player, x=3, y=2)
    assert result["success"] is True, result
    assert len(engine.state.temp_friends) == 1
    monster.current_hp = 0
    monster.is_alive = False
    ended = engine.execute_action("battle_end", {})
    assert ended["success"] is True, ended
    assert engine.state.temp_friends == []
