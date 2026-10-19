"""Validation tests for auditable full-open-world winner samples."""
import pytest

from sim.produce_open_world_winners import validate_open_world_winner_snapshot


def _valid_snapshot():
    return {
        "player": {"name": "轮回者0001", "entity_type": "轮回者", "is_alive": True,
                   "dao_wen": {"庇护": 0, "再生": 0}, "spells": ["生生不息"]},
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



def test_generator_writes_snapshot_paired_replay_and_hash_report(tmp_path, monkeypatch):
    import json
    import sys
    from sim import produce_open_world_winners as generator

    payload = {
        "seed": 7,
        "run_config": {"region": "罪孽都市"},
        "final_state": {
            "game_complete": True,
            "stop_reason": "final duel completed",
            "winner_name": "测试轮回者",
            "winner_id": "player-1",
            "world_turn": 12,
            "battle_count": 2,
            "combat_event_count": 9,
            "player": {"name": "测试轮回者", "runtime_id": "player-1"},
        },
        "driver_timeline": [
            {"setup": {"attributes": {
                "blood_points": 9, "speed_points": 8, "mana_points": 8, "total": 25,
            }}},
            {"type": "battle_start", "final_duel": False},
            {"type": "final_duel", "final_duel": True},
        ],
        "winner_snapshot": {
            "player": {"name": "测试轮回者", "entity_type": "轮回者", "is_alive": True,
                       "dao_wen": {"庇护": 0, "再生": 0}, "spells": ["生生不息"]},
            "friends": [], "employees": [], "relics": [],
        },
    }
    monkeypatch.setattr(generator, "WINNER_DIR", tmp_path)
    monkeypatch.setattr(generator, "run_playthrough", lambda **kwargs: payload)
    monkeypatch.setattr(generator, "validate_complete_playthrough", lambda replay: None)
    monkeypatch.setattr(sys, "argv", [
        "produce_open_world_winners.py", "--count", "1", "--max-games", "1", "--start-seed", "7",
    ])

    generator.main()

    snapshot = json.loads((tmp_path / "winner_01.json").read_text(encoding="utf-8"))
    replay = json.loads((tmp_path / "winner_01_replay.json").read_text(encoding="utf-8"))
    report = json.loads((tmp_path / "sampling_report.json").read_text(encoding="utf-8"))
    assert snapshot["origin"]["final_duel_won"] is True
    assert "candidates" not in snapshot
    assert "winner_snapshot" not in replay
    assert report["winners_generated_this_run"] == 1
    assert report["samples"][0]["replay_file"] == "winner_01_replay.json"
    assert len(report["samples"][0]["sha256"]) == 64
    assert len(report["samples"][0]["replay_sha256"]) == 64
