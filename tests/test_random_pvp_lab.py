import random

from sim.audits.random_pvp_lab import classify_outcome
from sim.random_pvp_support import (
    assemble_duel,
    ARCHETYPES,
    create_random_character,
    legal_attribute_splits,
)


def test_random_attribute_splits_all_use_same_25_point_budget():
    splits = legal_attribute_splits()
    assert splits
    assert all(sum(row) == 25 for row in splits)
    assert all(blood >= 1 and speed >= 2 and mana >= 2
               for blood, speed, mana in splits)
    assert all(speed % 2 == 0 and mana % 2 == 0
               for _blood, speed, mana in splits)


def test_random_pvp_builds_two_real_opening_characters(tmp_path):
    left_points, right_points = random.Random(303).sample(legal_attribute_splits(), 2)
    left, left_build = create_random_character(
        101, "随机挑战者", tmp_path, left_points)
    right, right_build = create_random_character(
        202, "随机守擂者", tmp_path, right_points)

    for build in (left_build, right_build):
        assert build["attribute_points"]["total"] == 25
        assert build["relic"] in build["relic_choices"]
        assert build["daowen"] in build["daowen_choices"]
        assert build["stats"]["blood_limit"] > 0
    assert left_build["attribute_points"] != right_build["attribute_points"]

    first = assemble_duel(left, right, seed=303)
    assert left.state.in_final_duel is True
    assert len(left.state.enemies) == 1
    assert left.state.enemies[0].entity_type == "轮回者"
    assert {r.name for r in left.state.opponent_relics} == {
        r.name for r in right.state.relics
    }
    assert first in {"player_side", "opponent_side"}
    assert left.state.duel_turn == first
    assert left.state.phase == "in_combat"
    assert left.state.combat_subphase == "await_round_start"



def test_round_limit_is_not_counted_as_a_defender_win():
    result = classify_outcome({
        "winner": "defender",
        "rounds": 40,
        "reason": "回合上限",
        "error": False,
    })
    assert result["winner"] is None
    assert result["legacy_winner"] == "defender"
    assert result["unresolved"] is True
    assert result["outcome_status"] == "round_limit"


def test_sculpture_loss_is_a_decisive_but_distinct_outcome():
    result = classify_outcome({
        "winner": "challenger",
        "rounds": 4,
        "reason": "守擂主将化雕塑（攻次与攻力双0离场，判负；非被击杀）",
        "error": False,
    })
    assert result["winner"] == "challenger"
    assert result["outcome_status"] == "sculpture_loss"


def test_archetypes_have_equal_resource_counts(tmp_path):
    assert len(ARCHETYPES) == 2
    for i, (name, profile) in enumerate(ARCHETYPES.items()):
        engine, build = create_random_character(900+i, '流派'+str(i), tmp_path, profile['attribute_points'], name)
        assert sum(profile['attribute_points']) == 25
        assert len(profile['daowen']) == 3
        assert build['daowen_count'] == 4
        assert build['relic_count'] == 2
        assert set(profile['daowen']).issubset(engine.state.player.dao_wen)
