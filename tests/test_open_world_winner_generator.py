"""Validation tests for auditable full-open-world winner samples."""
import pytest

from sim.produce_open_world_winners import validate_open_world_winner_snapshot


def _valid_snapshot():
    return {
        "player": {"name": "轮回者0001", "entity_type": "轮回者", "is_alive": True},
        "friends": [],
        "employees": [],
        "relics": [],
        "origin": {
            "seed": 1,
            "region": "罪孽都市",
            "generator": "sim/produce_open_world_winners.py",
            "final_duel_won": True,
            "stop_reason": "final duel completed",
            "battle_count": 3,
            "ordinary_battle_count": 2,
            "world_turn": 15,
            "combat_event_count": 42,
            "initial_attributes": {
                "blood_points": 9,
                "speed_points": 8,
                "mana_points": 8,
            },
            "initial_attribute_points": 25,
        },
    }


def test_open_world_winner_contract_accepts_a_traceable_final_duel_winner():
    validate_open_world_winner_snapshot(_valid_snapshot())


def test_open_world_winner_contract_rejects_legacy_candidates_wrapper():
    with pytest.raises(ValueError, match="candidates wrapper"):
        validate_open_world_winner_snapshot({"candidates": {"2": [_valid_snapshot()]}})


def test_open_world_winner_contract_rejects_opponent_winner_and_missing_map_battle():
    snapshot = _valid_snapshot()
    snapshot["origin"]["final_duel_won"] = False
    with pytest.raises(ValueError, match="not won by the player"):
        validate_open_world_winner_snapshot(snapshot)

    snapshot = _valid_snapshot()
    snapshot["origin"]["ordinary_battle_count"] = 0
    with pytest.raises(ValueError, match="real map battle"):
        validate_open_world_winner_snapshot(snapshot)
