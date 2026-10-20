"""Regression tests for real-winner tier-two handplay setup and rest contracts."""
from types import SimpleNamespace

import pytest

from sim.handplay_dungeon_with_winner import (
    _choose_rest_plan,
    _prepare_before_battle,
    manual_player_turn,
    play_dungeon,
)


def test_rest_plan_matches_current_blood_limit_percentages_and_budget():
    # A real generated winner is blood-limit 66, HP 18, with 22 shards. Tier 3
    # would heal 40 but costs 25, so two affordable tier-2 rests fill the 48-HP gap.
    assert _choose_rest_plan(blood_limit=66, current_hp=18, shards=22) == (2, 27)
    assert _choose_rest_plan(blood_limit=66, current_hp=45, shards=12) == (2, 27)
    # Free tier 1 remains legal even while in shard debt.
    assert _choose_rest_plan(blood_limit=66, current_hp=58, shards=-5) == (1, 14)
    assert _choose_rest_plan(blood_limit=66, current_hp=30, shards=35) == (3, 40)


def test_rejected_rest_fails_fast_without_replaying_the_same_action():
    player = SimpleNamespace(blood_limit=66, current_hp=18, dao_wen={})
    state = SimpleNamespace(player=player, energy=3, shards=22)

    class RejectingEngine:
        def __init__(self):
            self.state = state
            self.calls = []

        def execute_action(self, action, params):
            self.calls.append((action, params))
            return {"success": False, "error": "deliberate test rejection"}

    engine = RejectingEngine()
    with pytest.raises(RuntimeError, match="局外休整2档失败"):
        _prepare_before_battle(engine)

    assert len(engine.calls) == 1
    assert engine.calls[0][1]["heal_allocations"] == [
        {"target_ref": "player:0", "amount": 27}
    ]


def test_real_winner_without_kill_daowen_uses_two_phase_basic_attack():
    player = SimpleNamespace(
        is_alive=True, action_count=1, current_hp=60, blood_limit=60,
        current_mana=4, shield=0, dao_wen={},
        effective_attack_power=lambda: 4,
    )
    target = SimpleNamespace(
        is_alive=True, name="test target", current_hp=100,
        attack_count=0, attack_power=0,
    )
    target_option = {
        "ref": "enemy:0", "spell_options": {"before": [], "after": []},
    }

    class BasicAttackEngine:
        def __init__(self):
            self.state = SimpleNamespace(player=player, enemies=[target])
            self.combat = SimpleNamespace(
                _combat_entity_refs=lambda: {"player:0": player, "enemy:0": target}
            )

        def execute_action(self, action, params):
            if action == "prepare_attack":
                return {"success": True, "result": {
                    "token": "attack-token", "hit_count": 2,
                    "target_options": [target_option],
                }}
            assert action == "resolve_attack"
            assert params["token"] == "attack-token"
            assert [hit["damage"] for hit in params["hits"]] == [4, 4]
            assert all(hit["target_ref"] == "enemy:0" for hit in params["hits"])
            target.current_hp -= 8
            return {"success": True, "result": {
                "hits": [{"damage_dealt": 4}, {"damage_dealt": 4}],
            }}

    log = []
    result = manual_player_turn(BasicAttackEngine(), log)

    assert len(result) == 1
    assert target.current_hp == 92
    assert any("普攻2击" in line for line in log)


def test_tier_two_loader_rejects_legacy_candidates_wrapper_before_setup(tmp_path):
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text('{"candidates": {"2": []}}', encoding="utf-8")

    with pytest.raises(ValueError, match="candidates wrapper"):
        play_dungeon(str(legacy_path), battles=1, seed=7)
