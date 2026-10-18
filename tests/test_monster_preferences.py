"""怪物固有偏爱按副本阶位生成的回归测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.dice import DiceEngine
from engine.models import Entity, MONSTER_MANA_RELIC
from engine.monsters import MONSTER_PREFERENCE_POOL, assign_monster_preferences


def test_monster_gets_exactly_one_distinct_preference_per_tier():
    for tier in range(1, len(MONSTER_PREFERENCE_POOL) + 1):
        entity = Entity(name=f"测试怪物{tier}", entity_type="怪物")
        preferences = assign_monster_preferences(entity, tier, DiceEngine(seed=tier))
        assert len(preferences) == tier
        assert len(set(preferences)) == tier
        assert set(preferences).issubset(set(MONSTER_PREFERENCE_POOL))
        assert entity.to_dict()["preferences"] == preferences
        # 痛苦的偏爱保留旧的[回始]效果，但不再默认人人固定拥有。
        assert any(r.name == MONSTER_MANA_RELIC for r in entity.relics) == (
            "痛苦的偏爱" in preferences
        )


def test_preference_assignment_is_reproducible_and_never_duplicates():
    first = Entity(name="同名怪物", entity_type="怪物")
    second = Entity(name="同名怪物", entity_type="怪物")
    one = assign_monster_preferences(first, 4, DiceEngine(seed=123))
    two = assign_monster_preferences(second, 4, DiceEngine(seed=123))
    assert one == two
    assert len(set(one)) == 4

def test_greed_preference_halves_mana_cost_with_rounding_up():
    entity = Entity(name="贪婪者", entity_type="怪物", mana_limit=10, current_mana=10)
    entity.preferences = ["贪婪的偏爱"]
    assert entity.spend_mana(3)
    assert entity.current_mana == 8


def test_time_preference_round_cooldown_blocks_until_countdown_finishes():
    from engine.models import DaoWen

    instance = __import__("engine.models", fromlist=["DaoWenInstance"]).DaoWenInstance(
        DaoWen(name="测试", formula="", cost_type="消耗",
               cost_formula="X", effect_formula=""),
        cooldown_rounds_remaining=2,
    )
    assert not instance.can_use()
    instance.cooldown_rounds_remaining -= 1
    assert not instance.can_use()
    instance.cooldown_rounds_remaining -= 1
    assert instance.can_use()

def test_famine_preference_reduces_target_mana_limit_by_actual_damage(tmp_path):
    from engine.api import GameEngine

    engine = GameEngine(
        db_path=str(tmp_path / "rulings.db"),
        save_dir=str(tmp_path / "saves"),
        autosave=False,
        rng_seed=17,
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death_book.md"),
    )
    target = Entity(name="目标", entity_type="轮回者",
                    blood_limit=30, current_hp=30, mana_limit=8, current_mana=8)
    attacker = Entity(name="饥荒怪", entity_type="怪物",
                      blood_limit=30, current_hp=30, mana_limit=5, current_mana=5)
    attacker.preferences = ["饥荒的偏爱"]
    engine.state.player = target
    engine.state.enemies = [attacker]
    detail = engine.combat._apply_hostile_damage(
        target, 3, source=attacker,
        ctx={"timing": "test", "source": attacker.name, "source_type": "monster",
             "actor": attacker, "target": target, "mechanic": "damage",
             "subtype": "test", "amount": 3, "tags": {"test"}},
    )
    assert target.mana_limit == 5
    assert target.current_mana == 5
    assert detail["famine_preference"]["mana_limit_reduced"] == 3
