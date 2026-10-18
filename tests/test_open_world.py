"""开放世界核心状态回归测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from engine.world import WorldActorState, WorldClock, WorldMap, WorldState


def _open_map(width=7, height=7):
    world_map = WorldMap(width, height, terrain={
        f"{x},{y}": "floor" for y in range(height) for x in range(width)
    })
    return world_map


def test_world_clock_advances_only_by_valid_nonnegative_integer():
    world = WorldState(clock=WorldClock(), map=_open_map())
    assert world.clock.turn == 0
    with pytest.raises(ValueError):
        world.clock.advance(-1)
    with pytest.raises(ValueError):
        world.clock.advance(True)


def test_movement_is_adjacent_passable_and_time_costs_one_turn():
    world = WorldState.create(width=7, height=7, seed=1)
    world.map = _open_map()
    world.add_actor(WorldActorState("p", "玩家", "轮回者", 1, 1, faction="player"))
    result = world.move_actor("p", (2, 1))
    assert result["world_turn"] == 1
    assert world.actors["p"].position == (2, 1)
    with pytest.raises(ValueError, match="上下左右相邻"):
        world.move_actor("p", (4, 1))


def test_units_cannot_overlap_and_blocked_terrain_rejects_move():
    world = WorldState(clock=WorldClock(),
                       map=_open_map())
    world.map.terrain["2,1"] = "wall"
    world.add_actor(WorldActorState("p", "玩家", "轮回者", 1, 1))
    world.add_actor(WorldActorState("m", "怪物", "怪物", 2, 1))
    with pytest.raises(ValueError, match="占据"):
        world.move_actor("p", (2, 1))
    world.map.terrain["2,1"] = "wall"
    with pytest.raises(ValueError, match="不可通行"):
        world.move_actor("p", (2, 1))


def test_vision_is_four_orthogonal_cells_and_private_per_actor():
    world = WorldState(clock=__import__("engine.world", fromlist=["WorldClock"]).WorldClock(),
                       map=_open_map())
    world.add_actor(WorldActorState("p", "玩家", "轮回者", 3, 3))
    world.add_actor(WorldActorState("m", "怪物", "怪物", 5, 5))
    visible = set(world.visible_cells("p"))
    assert visible == {(3, 3), (3, 2), (3, 4), (2, 3), (4, 3)}
    assert world.map.key((5, 5)) not in world.actors["p"].known_cells
    assert world.map.key((3, 3)) not in world.actors["m"].known_cells


def test_pathfinding_routes_around_walls_and_detects_unreachable_goal():
    world_map = _open_map(5, 5)
    world_map.terrain["2,1"] = "wall"
    path = world_map.find_path((1, 1), (3, 1))
    assert path is not None
    assert path[0] == (1, 1) and path[-1] == (3, 1)
    assert (2, 1) not in path
    world_map.terrain = {f"{x},2": "wall" for x in range(5)}
    world_map.terrain.update({f"{x},{y}": "floor" for y in range(5) for x in range(5)
                              if y != 2})
    assert world_map.find_path((1, 1), (1, 3)) is None


def test_world_state_roundtrip_preserves_clock_map_actors_and_fact_log():
    world = WorldState(clock=__import__("engine.world", fromlist=["WorldClock"]).WorldClock(),
                       map=_open_map())
    world.add_actor(WorldActorState("p", "玩家", "轮回者", 1, 1))
    world.move_actor("p", (2, 1))
    restored = WorldState.from_dict(world.to_dict())
    assert restored.clock.turn == 1
    assert restored.actors["p"].position == (2, 1)
    assert restored.event_log == world.event_log
    assert restored.map.to_dict() == world.map.to_dict()


def test_encounter_lifecycle_is_recorded_and_cannot_finish_twice():
    world = WorldState(clock=__import__("engine.world", fromlist=["WorldClock"]).WorldClock(),
                       map=_open_map())
    world.add_actor(WorldActorState("p", "玩家", "轮回者", 1, 1))
    world.add_actor(WorldActorState("m", "怪物", "怪物", 2, 1))
    encounter = world.start_encounter(["p", "m"], (2, 1))
    assert encounter.active
    world.finish_encounter(encounter.encounter_id, "escaped")
    assert not encounter.active
    with pytest.raises(ValueError, match="不存在或已经结束"):
        world.finish_encounter(encounter.encounter_id, "escaped")


def test_game_engine_exposes_world_actions_and_serializes_state(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "world.db"),
                        save_dir=str(tmp_path), rng_seed=19)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.phase = "pre_battle"
    initialized = engine.execute_action("world_initialize", {"width": 7, "height": 7})
    assert initialized["success"], initialized
    status = engine.execute_action("world_status")
    assert status["success"]
    assert status["result"]["map_size"]["width"] == 7
    assert "map" not in status["result"] and "event_log" not in status["result"]
    assert status["result"]["self"]["position"] == [1, 1]
    player_id = status["result"]["self"]["actor_id"]
    moved = engine.execute_action("world_move", {"actor_id": player_id, "x": 1, "y": 0})
    assert moved["success"], moved
    assert engine.state.world_state["clock"]["turn"] == 1
    assert engine.state.to_dict()["world_state"]["clock"]["turn"] == 1


def test_moving_next_to_hostile_unit_creates_one_encounter_but_merchant_stays_neutral():
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState("p", "玩家", "轮回者", 1, 1, faction="player"))
    world.add_actor(WorldActorState("m", "怪物", "怪物", 3, 1, faction="monster"))
    world.add_actor(WorldActorState("shop", "商人", "merchant", 1, 3, faction="neutral"))
    result = world.move_actor("p", (2, 1))
    assert len(result["new_encounters"]) == 1
    assert world.encounters[result["new_encounters"][0]].participants == ["p", "m"]
    assert world.detect_adjacent_encounters() == []


def test_wait_requires_positive_turns():
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState("p", "玩家", "轮回者", 1, 1))
    with pytest.raises(ValueError, match="正整数"):
        world.wait("p", 0)
    assert world.wait("p", 2) == 2


def test_world_api_cannot_query_or_move_another_actor(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "privacy.db"),
                        save_dir=str(tmp_path), rng_seed=23)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.phase = "pre_battle"
    engine.state.enemies = [Entity("M", "怪物", blood_limit=10, current_hp=10)]
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]
    hidden_id = next(k for k, v in engine.state.world_state["actors"].items()
                     if v["kind"] == "enemy")
    status = engine.execute_action("world_status", {"actor_id": hidden_id})
    assert not status["success"]
    move = engine.execute_action("world_move", {"actor_id": hidden_id, "x": 2, "y": 1})
    assert not move["success"]


def test_entering_adjacency_triggers_neutral_interaction_once():
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState("p", "玩家", "轮回者", 1, 1, faction="player"))
    world.add_actor(WorldActorState("shop", "过路商人", "merchant", 3, 1, faction="neutral"))
    first = world.move_actor("p", (2, 1))
    assert len(first["new_interactions"]) == 1
    assert first["new_interactions"][0]["target_actor_id"] == "shop"
    assert first["new_interactions"][0]["available_outcomes"] == ["交谈", "交易", "合作", "攻击"]
    # 仍处于同一交互范围时移动，不重复触发；离开后重新进入才会再次触发。
    second = world.move_actor("p", (3, 0))
    assert second["new_interactions"] == []


def test_monsters_never_receive_attack_outcome_against_merchant():
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState("m", "怪物", "怪物", 1, 1, faction="monster"))
    world.add_actor(WorldActorState("shop", "过路商人", "merchant", 3, 1, faction="neutral"))
    result = world.move_actor("m", (2, 1))
    assert len(result["new_interactions"]) == 1
    assert "攻击" not in result["new_interactions"][0]["available_outcomes"]
