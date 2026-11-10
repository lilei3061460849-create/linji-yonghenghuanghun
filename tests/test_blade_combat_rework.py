"""白刃战分刀、逐击招架与法术阶级出手成本。"""
from engine.models import Entity
from tests.test_reincarnator_basic_attack import _engine
from tests.attack_support import resolve_attack


def _target(e, hp=200, speed=4):
    m = Entity(name="持刀者", entity_type="怪物", blood_limit=hp, current_hp=hp,
               attack_count=speed, attack_power=1, speed_limit=speed,
               current_speed=speed, mana_limit=1, current_mana=1)
    e.state.enemies[:] = [m]
    return m


def test_attack_damage_can_be_freely_split_with_empty_swings():
    e = _engine("split", blood_points=1, speed_points=8, mana_points=16)  # 8刀，总伤64
    m = _target(e)
    r = resolve_attack(e, damages=[0, 2, 10, 20, 0, 4, 8, 20])
    assert r["success"], r
    assert m.current_hp == 136
    assert [x["allocated_damage"] for x in r["result"]["hits"]] == [0, 2, 10, 20, 0, 4, 8, 20]


def test_attack_rejects_damage_over_snapshot_budget():
    e = _engine("over_split", blood_points=1, speed_points=8, mana_points=16)
    _target(e)
    r = resolve_attack(e, damages=[0, 0, 0, 65, 0, 0, 0, 0])
    assert not r["success"] and "超过上限64" in r["error"]


def test_parry_real_hit_refunds_speed_but_empty_swing_does_not():
    e = _engine("parry_bluff", blood_points=1, speed_points=8, mana_points=16)
    m = _target(e, speed=4)
    # 直接验证逐击公开语义：真刀净耗0，空刀净耗1。
    real = e.combat.resolve_attack(e.state.player, m, parry=True, allocated_damage=8)
    assert real["parry_success"] and m.current_hp == 200 and m.current_speed == 4
    empty = e.combat.resolve_attack(e.state.player, m, parry=True, allocated_damage=0)
    assert not empty["parry_success"] and m.current_speed == 3


def test_dodge_still_cancels_attack_for_a_certain_speed_cost():
    e = _engine("dodge_certain", blood_points=1, speed_points=8, mana_points=16)
    m = _target(e, speed=4)
    r = e.combat.resolve_attack(e.state.player, m, dodge=True, allocated_damage=20)
    assert r["dodge_success"] and m.current_hp == 200 and m.current_speed == 3
