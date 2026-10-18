"""开放世界任务闭环测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from engine.quests import QuestEngine, QuestError, validate_definitions


@pytest.fixture
def definitions():
    return [
        {
            "id": "find_route",
            "title": "找到旧路口",
            "description": "测试任务，不属于正式剧情内容。",
            "objectives": [
                {"id": "visit", "event": "actor_moved",
                 "match": {"actor_id": "$player", "details.destination": [2, 1]},
                 "required": 1}
            ],
            "rewards": {"shards": 7, "attribute_points": 2},
            "next_quests": ["report_back"],
        },
        {
            "id": "report_back",
            "title": "回报消息",
            "prerequisites": ["find_route"],
            "objectives": [
                {"id": "talk", "event": "npc_interacted",
                 "match": {"details.npc_id": "guide"}, "required": 1}
            ],
            "rewards": {"shards": 3},
        },
    ]


def test_task_acceptance_checks_prerequisites_and_giver(definitions):
    engine = QuestEngine(definitions)
    state = engine.initial_state()
    with pytest.raises(QuestError, match="不存在"):
        engine.accept(state, "missing")
    engine.accept(state, "find_route", world_turn=4)
    with pytest.raises(QuestError, match="不能重复接取"):
        engine.accept(state, "find_route", world_turn=4)
    with pytest.raises(QuestError, match="前置条件"):
        engine.accept(state, "report_back", world_turn=4)


def test_task_progress_requires_matching_engine_fact_and_completes_once(definitions):
    engine = QuestEngine(definitions)
    state = engine.initial_state()
    engine.accept(state, "find_route", world_turn=0)
    player_id = "p-1"
    wrong = {"type": "actor_moved", "actor_id": "npc-2",
             "details": {"destination": [2, 1]}}
    assert engine.process_fact(state, wrong, world_turn=1, player_actor_id=player_id) == []
    fact = {"type": "actor_moved", "actor_id": player_id,
            "details": {"destination": [2, 1]}}
    generated = engine.process_fact(state, fact, world_turn=1, player_actor_id=player_id)
    assert state["instances"]["find_route"]["status"] == "completed"
    assert any(event["type"] == "quest_completed" for event in generated)
    assert "quest_completed:find_route" in state["story_flags"]
    assert engine.process_fact(state, fact, world_turn=2, player_actor_id=player_id) == []


def test_rewards_are_claimed_once_and_unlock_task_chain(definitions):
    engine = QuestEngine(definitions)
    state = engine.initial_state()
    engine.accept(state, "find_route", world_turn=0)
    engine.process_fact(state, {"type": "actor_moved", "actor_id": "p",
                                "details": {"destination": [2, 1]}},
                        world_turn=1, player_actor_id="p")
    rewards = engine.claim(state, "find_route", world_turn=1)
    assert rewards == {"shards": 7, "attribute_points": 2}
    with pytest.raises(QuestError, match="不能领取奖励"):
        engine.claim(state, "find_route", world_turn=1)
    listing = engine.list_tasks(state, world_turn=1)
    assert [q["id"] for q in listing["available"]] == ["report_back"]


def test_failure_event_and_deadline_fail_active_tasks(definitions):
    definitions[0]["fail_on"] = ["player_died"]
    definitions[0]["deadline_turns"] = 2
    engine = QuestEngine(definitions)
    state = engine.initial_state()
    engine.accept(state, "find_route", world_turn=3)
    failed = engine.process_fact(state, {"type": "player_died"}, world_turn=4)
    assert failed[0]["type"] == "quest_failed"
    assert state["instances"]["find_route"]["status"] == "failed"

    state = engine.initial_state()
    engine.accept(state, "find_route", world_turn=3)
    assert engine.advance_time(state, 5) == []
    assert engine.advance_time(state, 6)[0]["type"] == "quest_failed"


def test_definition_validation_rejects_duplicate_objectives_and_invalid_rewards(definitions):
    broken = [dict(definitions[0], objectives=[
        {"id": "x", "event": "actor_moved"},
        {"id": "x", "event": "actor_moved"},
    ])]
    with pytest.raises(QuestError, match="唯一"):
        validate_definitions(broken)
    broken = [dict(definitions[0], rewards={"fragments_from_nothing": -3})]
    with pytest.raises(QuestError, match="未支持"):
        validate_definitions(broken)
