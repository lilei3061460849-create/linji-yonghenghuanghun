import random

from sim.audits.random_pvp_lab import (
    assemble_duel,
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
