"""开放世界回放校验器（`sim/run_open_world_replay.py`）的契约测试。

背景（2026-11-03）：CI 的 `open-world-full-playthrough-replay` 长期必红。
根因是校验器自相矛盾——
`validate_complete_playthrough` 先断言 `game_complete is True`，再允许
`stop_reason == "crown lost"`；而 `game_complete` 只在**玩家自己**把
【最终的冠冕】带出副本时为真。于是"冠冕被别人带走、玩家困死"这一
**合法的世界终局**（用户 2026-10-31 裁定）永远过不去。

本文件用合成 payload 锁住修正后的语义：
合法终局（玩家胜 / 玩家败）都通过；"跑到一半就停"必须被拒。
"""
from __future__ import annotations

import copy

import pytest

from sim.run_open_world_replay import LEGAL_TERMINAL_REASONS, validate_complete_playthrough


def _battle(rounds: int = 4, events: int = 30) -> dict:
    return {"round_count": rounds, "event_count": events}


def _payload(stop_reason: str, *, game_complete: bool) -> dict:
    """构造一个"其余各项都合法"的 payload，只让终止原因/玩家胜负可变。"""
    return {
        "final_state": {
            "player": {"name": "测试轮回者", "alive": game_complete},
            "game_complete": game_complete,
            "winner_name": "测试轮回者" if game_complete else None,
            "stop_reason": stop_reason,
            "crown_exit": {
                "actor_name": "测试轮回者" if game_complete else "过路商人",
                "reason": "最终的冠冕",
                "player_trapped": not game_complete,
            },
            "crown_taken_by": "测试轮回者" if game_complete else "过路商人",
            "battle_count": 1,
            "combat_event_count": 30,
            "living_reincarnators_at_end": [],
        },
        "initial_population": {"monsters": 12, "other_reincarnators": 2,
                              "special_locations": 88},
        "battles": [_battle()],
        "driver_timeline": [{"type": "battle_start"}],
        "world_event_log": [{"type": "crown_left_world"}],
    }


def test_legal_terminal_reasons_are_only_the_two_crown_endings():
    assert LEGAL_TERMINAL_REASONS == frozenset({"crown carried out", "crown lost"})


def test_player_win_passes():
    validate_complete_playthrough(_payload("crown carried out", game_complete=True))


def test_player_loss_by_crown_theft_is_a_legal_ending():
    """回归：冠冕被他人带出、玩家 `player_trapped` 属合法终局，不得判为"不完整"。"""
    validate_complete_playthrough(_payload("crown lost", game_complete=False))


@pytest.mark.parametrize("reason", [
    "player died",
    "player died in map encounter",
    "safety ceiling reached at world turn 120; run incomplete",
    "run incomplete",
    "sole survivor victory",   # 旧世界终局模型，已随冠冕改制作废
    "",
])
def test_non_legal_terminations_are_rejected(reason):
    with pytest.raises(RuntimeError, match="incomplete"):
        validate_complete_playthrough(_payload(reason, game_complete=False))


def test_crown_carried_out_requires_game_complete_flag():
    payload = _payload("crown carried out", game_complete=True)
    payload["final_state"]["game_complete"] = False
    with pytest.raises(RuntimeError, match="game_complete"):
        validate_complete_playthrough(payload)


def test_missing_crown_exit_is_rejected():
    payload = _payload("crown lost", game_complete=False)
    payload["final_state"]["crown_exit"] = {}
    with pytest.raises(RuntimeError, match="crown exit"):
        validate_complete_playthrough(payload)


def test_missing_world_event_log_exit_is_rejected():
    payload = _payload("crown lost", game_complete=False)
    payload["world_event_log"] = []
    with pytest.raises(RuntimeError, match="world event log"):
        validate_complete_playthrough(payload)


def test_battle_count_must_match_exported_replays():
    payload = _payload("crown lost", game_complete=False)
    payload["final_state"]["battle_count"] = 2
    with pytest.raises(RuntimeError, match="battle count"):
        validate_complete_playthrough(payload)


def test_run_without_combat_is_rejected():
    payload = _payload("crown lost", game_complete=False)
    payload["driver_timeline"] = []
    with pytest.raises(RuntimeError, match="ordinary map encounter"):
        validate_complete_playthrough(payload)


def test_multiple_living_reincarnators_at_end_is_rejected():
    payload = _payload("crown lost", game_complete=False)
    payload["final_state"]["living_reincarnators_at_end"] = [{"name": "a"}, {"name": "b"}]
    with pytest.raises(RuntimeError, match="multiple living reincarnators"):
        validate_complete_playthrough(payload)


def test_validator_does_not_mutate_payload():
    payload = _payload("crown lost", game_complete=False)
    before = copy.deepcopy(payload)
    validate_complete_playthrough(payload)
    assert payload == before
