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
