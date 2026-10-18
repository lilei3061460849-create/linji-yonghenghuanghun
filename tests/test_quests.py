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


def test_game_engine_drives_quest_progress_only_from_real_world_actions(tmp_path, monkeypatch):
    from engine.api import GameEngine
    from engine.models import Entity
    import engine.api as api_module

    definitions = [{
        "id": "visit_old_crossing",
        "title": "测试：抵达旧路口",
        "objectives": [{
            "id": "visit",
            "event": "actor_moved",
            "match": {"actor_id": "$player", "details.destination": [2, 1]},
        }],
        "rewards": {"shards": 7, "attribute_points": 2},
    }]
    monkeypatch.setattr(api_module, "load_quest_definitions", lambda: definitions)
    engine = GameEngine(db_path=str(tmp_path / "quest.db"),
                        save_dir=str(tmp_path), rng_seed=31)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.phase = "pre_battle"
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]
    accepted = engine.execute_action("quest_accept", {"quest_id": "visit_old_crossing"})
    assert accepted["success"], accepted
    assert engine.state.quest_state["instances"]["visit_old_crossing"]["status"] == "active"

    # 没有移动时不能由任务接口直接提交目标完成。
    assert engine.execute_action("quest_claim", {"quest_id": "visit_old_crossing"})["success"] is False
    moved = engine.execute_action("world_move", {"x": 2, "y": 1})
    assert moved["success"], moved
    assert engine.state.quest_state["instances"]["visit_old_crossing"]["status"] == "completed"

    claimed = engine.execute_action("quest_claim", {"quest_id": "visit_old_crossing"})
    assert claimed["success"], claimed
    assert engine.state.shards == 27
    assert engine.state.attribute_points == 2
    assert engine.execute_action("quest_claim", {"quest_id": "visit_old_crossing"})["success"] is False


def test_real_combat_event_bus_fact_advances_quest(tmp_path, monkeypatch):
    from engine.api import GameEngine
    from engine.combat_events import CombatEvent, CombatEventType
    from engine.models import Entity
    import engine.api as api_module

    definitions = [{
        "id": "defeat_named_monster",
        "title": "测试：击败指定怪物",
        "objectives": [{
            "id": "kill",
            "event": "entity_died",
            "match": {"details.target_name": "M"},
        }],
        "rewards": {"shards": 1},
    }]
    monkeypatch.setattr(api_module, "load_quest_definitions", lambda: definitions)
    engine = GameEngine(db_path=str(tmp_path / "combat-quest.db"),
                        save_dir=str(tmp_path), rng_seed=41)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.enemies = [Entity("M", "怪物", blood_limit=10, current_hp=10)]
    engine.state.phase = "pre_battle"
    assert engine.execute_action("quest_accept", {"quest_id": "defeat_named_monster"})["success"]
    fact = CombatEvent(event_type=CombatEventType.ENTITY_DIED,
                       actor_name="P", target_name="M", data={"cause": "damage"})
    engine._record_combat_quest_facts([fact])
    assert engine.state.quest_state["instances"]["defeat_named_monster"]["status"] == "completed"
