"""凡庸复用法术中断指纹时不能让自己的计数造成假进展。"""
from types import SimpleNamespace

from engine.models import Entity
from engine.spell_execution import battlefield_fingerprint


def test_mediocrity_fingerprint_ignores_only_own_clock():
    actor = Entity(name="角色", entity_type="轮回者", blood_limit=100, current_hp=100)
    enemy = Entity(name="目标", entity_type="怪物", blood_limit=100, current_hp=100)
    engine = SimpleNamespace(state=SimpleNamespace(player=actor, enemies=[enemy]))
    before = battlefield_fingerprint(engine, exclude_mediocrity=True)
    actor.no_action_rounds += 1
    enemy.no_damage_rounds += 1
    assert battlefield_fingerprint(engine, exclude_mediocrity=True) == before
    enemy.shield += 1
    assert battlefield_fingerprint(engine, exclude_mediocrity=True) != before


def test_spell_fingerprint_still_tracks_existing_counters():
    actor = Entity(name="角色", entity_type="轮回者", blood_limit=100, current_hp=100)
    engine = SimpleNamespace(state=SimpleNamespace(player=actor))
    before = battlefield_fingerprint(engine)
    actor.no_action_rounds += 1
    assert battlefield_fingerprint(engine) != before


def test_mediocrity_five_no_change_rounds_and_reset():
    from tests.test_redemption_and_setup import _engine, _ready_combat
    engine = _ready_combat(_engine("mediocrity_new_tick"))
    actor = engine.state.player
    actor.no_net_change_rounds = 3
    actor.net_change_this_round = True
    assert engine.combat._tick_mediocrity_counters(actor) is None
    assert actor.no_net_change_rounds == 0
    actor.net_change_this_round = False
    for _ in range(4):
        assert engine.combat._tick_mediocrity_counters(actor) is None
    assert "连续五回合" in engine.combat._tick_mediocrity_counters(actor)


def test_mediocrity_tracks_real_focus_without_crediting_other_actor():
    from tests.test_redemption_and_setup import _engine, _ready_combat
    engine = _ready_combat(_engine("mediocrity_new_focus"))
    actor = engine.state.player
    actor.current_mana = 1
    monster = Entity(name="未行动怪", entity_type="怪物", blood_limit=100, current_hp=100,
                     attack_count=1, attack_power=1)
    engine.state.enemies[:] = [monster]
    actor.net_change_this_round = False
    result = engine.execute_action("focus", {})
    assert result["success"], result
    assert actor.net_change_this_round
    assert not monster.net_change_this_round


def test_mediocrity_empty_focus_does_not_count_as_progress():
    from tests.test_redemption_and_setup import _engine, _ready_combat
    engine = _ready_combat(_engine("mediocrity_empty_focus"))
    actor = engine.state.player
    actor.current_mana = actor.mana_limit
    actor.net_change_this_round = False
    result = engine.execute_action("focus", {})
    assert result["success"], result
    assert not actor.net_change_this_round
