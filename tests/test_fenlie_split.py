"""【分裂】X个1/1/1造物规则。

分裂X：流血3X，每次发动创造X个1血限/1速限/1法限的造物。
攻击面板按通用规则换算，造物每回合最多出手1次；不携带道纹、不继承施法者构筑，[战终]消散。

运行方式：python -m pytest tests/test_fenlie_split.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.daowen import DaoWenEngine
from engine.models import DaoWen, DaoWenInstance, Entity, Relic, StatusEffect
from tests.attack_support import resolve_attack


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
    engine.state.temp_friends = []
    engine.state.enemies = [monster]
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.current_round = 2
    actor = monster if actor_is_monster else player
    actor.dao_wen["分裂"] = DaoWenInstance(
        DaoWen(name="分裂", formula="", cost_type="流血",
               cost_formula="3X", effect_formula="创造X个1/1/1造物"),
        x_value=0, x_free=True)
    return engine, player, monster, actor


def _cast(engine, *, x=3, actor_ref=""):
    extra = {"actor_ref": actor_ref} if actor_ref else {}
    return engine.execute_action("use_daowen", {
        **extra, "daowen_name": "分裂", "x": x,
        "target_ref": "enemy:0" if actor_ref else "player:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })


def _assert_minimal_creation(creation, creator):
    assert creation.entity_type == "造物"
    assert creation.blood_limit == creation.current_hp == 1
    assert creation.mana_limit == creation.current_mana == 1
    assert creation.speed_limit == creation.current_speed == 1
    assert creation.attack_power == 1 and creation.attack_count == 1
    assert creation.effective_attack_power() == 1
    assert creation.effective_attack_count() == 1
    assert creation.action_count == 1
    assert creation.copy_creator_runtime_id == creator.runtime_id
    assert creation.is_created_being is False  # 分裂造物不是【造生】妖怪
    assert creation.dao_wen == {}
    assert creation.preferences == []
    assert creation.relics == []
    assert creation.artifacts == []


def test_cost_is_3x_and_x_is_the_number_of_minimal_creations():
    for x in (1, 2, 3, 5):
        calc = DaoWenEngine.resolve("分裂", x)
        assert calc["cost_type"] == "生命"
        assert calc["cost_hp"] == 3 * x
        assert calc["split_creations"] == x
        assert calc["creation_blood_limit"] == 1
        assert calc["creation_speed_limit"] == 1
        assert calc["creation_mana_limit"] == 1
        assert calc["creation_attack_power"] == 1
        assert calc["creation_attack_count"] == 1
        assert "y" not in calc


@pytest.mark.parametrize("x", [-1, True, 1.5, "3"])
def test_split_rejects_invalid_effective_x(x):
    with pytest.raises(ValueError, match="分裂X必须是非负整数"):
        DaoWenEngine.resolve("分裂", x)


def test_decay_can_reduce_effective_count_to_zero_after_valid_submission():
    caster = Entity("施法者", "轮回者", blood_limit=20, current_hp=20)
    caster.add_status(StatusEffect(name="退化", value=2, remaining_rounds=1))
    calc = DaoWenEngine.resolve("分裂", 2, caster=caster)
    assert calc["x"] == 0
    assert calc["cost_hp"] == 0
    assert calc["split_creations"] == 0


def test_cast_pays_bleed_and_creates_exactly_x_minimal_creations():
    engine, player, _, _ = _engine()
    result = _cast(engine, x=3)
    assert result["success"] is True, result
    assert player.current_hp == 191
    assert player.blood_limit == 200
    assert len(engine.state.temp_friends) == 3
    assert len({creation.runtime_id for creation in engine.state.temp_friends}) == 3
    assert len({creation.name for creation in engine.state.temp_friends}) == 3
    for creation in engine.state.temp_friends:
        _assert_minimal_creation(creation, player)
    effect = next(effect for effect in result["execution"]["effects"]
                  if isinstance(effect, dict) and effect.get("type") == "fenlie")
    assert effect["creations"] == 3
    assert len(effect["names"]) == 3


def test_creation_can_attack_once_but_cannot_act_twice_in_one_round():
    engine, player, monster, _ = _engine()
    # 单行动契约：造物只有 1 次出手。给施法者补一次出手，让本侧不至于因造物打完就整轮结束，
    # 这样第二次尝试撞到的是「出手已用完」，而不是回终门禁。
    player.add_status(StatusEffect(name="疯狂", value=1, remaining_rounds=-1, source="test"))
    assert _cast(engine, x=1)["success"] is True
    creation = engine.state.temp_friends[0]
    hp_before = monster.current_hp

    # 施法者已让出优先权；敌方跳过后才轮到新造物攻击。
    assert engine.execute_action("skip", {"actor_ref": "enemy:0"})["success"]
    first = resolve_attack(engine, creation.name)
    assert first["success"] is True, first
    assert creation.actions_used_this_round == creation.action_count == 1
    assert monster.current_hp == hp_before - 1  # 1法限按通用规则给出1攻击力
    second = resolve_attack(engine, creation.name)
    assert second["success"] is False
    assert "出手" in second.get("error", "")


def test_creations_do_not_inherit_caster_build():
    engine, player, _, _ = _engine()
    player.dao_wen["庇护"] = DaoWenInstance(
        DaoWen(name="庇护", formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=0, x_free=True)
    player.preferences = ["痛苦的偏爱", "时间的偏爱"]
    player.relics = [Relic(name="测试遗物", effect="不应继承")]
    player.artifacts = ["测试法器"]

    result = _cast(engine, x=2)
    assert result["success"] is True, result
    for creation in engine.state.temp_friends:
        _assert_minimal_creation(creation, player)


def test_monster_split_adds_x_minimal_creations_to_enemy_side():
    engine, _, monster, _ = _engine(actor_is_monster=True)
    enemies_before = len(engine.state.enemies)
    creations = engine.combat._spawn_fenlie_creations(monster, 3)
    assert len(creations) == 3
    assert len(engine.state.enemies) == enemies_before + 3
    assert engine.state.enemies[-3:] == creations
    assert all(creation not in engine.state.temp_friends for creation in creations)
    for creation in creations:
        _assert_minimal_creation(creation, monster)


def test_repeated_casts_append_distinct_units_x_per_call():
    engine, _, monster, _ = _engine(actor_is_monster=True)
    first = engine.combat._spawn_fenlie_creations(monster, 2)
    second = engine.combat._spawn_fenlie_creations(monster, 3)
    assert len(first) == 2 and len(second) == 3
    assert not ({entity.runtime_id for entity in first}
                & {entity.runtime_id for entity in second})
    assert len({entity.name for entity in first + second}) == 5


def test_no_creation_on_caster_death_without_casting():
    engine, player, _, _ = _engine()
    player.current_hp = 0
    player.is_alive = False
    engine.combat._on_entity_death(player, ctx={})
    assert engine.state.temp_friends == []


def test_bleed_is_refused_atomically_when_current_hp_cannot_pay_3x():
    engine, player, _, _ = _engine()
    player.current_hp = 8  # X=3需支付9
    result = _cast(engine, x=3)
    assert result["success"] is False
    assert "生命" in result.get("error", "")
    assert player.current_hp == 8
    assert engine.state.temp_friends == []


def test_paying_last_hp_kills_caster_but_all_paid_creations_still_enter():
    engine, player, _, _ = _engine()
    player.current_hp = 9
    result = _cast(engine, x=3)
    assert result["success"] is True, result
    assert player.current_hp == 0 and player.is_alive is False
    assert len(engine.state.temp_friends) == 3
    for creation in engine.state.temp_friends:
        _assert_minimal_creation(creation, player)


def test_all_creations_disperse_at_battle_end():
    engine, player, monster, _ = _engine()
    result = _cast(engine, x=3)
    assert result["success"] is True, result
    assert len(engine.state.temp_friends) == 3
    monster.current_hp = 0
    monster.is_alive = False
    ended = engine.execute_action("battle_end", {})
    assert ended["success"] is True, ended
    assert engine.state.temp_friends == []
