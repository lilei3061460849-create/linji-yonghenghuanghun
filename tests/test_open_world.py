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


def test_units_may_overlap_and_same_cell_triggers_encounter_but_walls_block():
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(
        "p", "玩家", "轮回者", 1, 1, faction="player", mode="manual"))
    world.add_actor(WorldActorState(
        "m", "怪物", "怪物", 2, 1, faction="monster", mode="manual"))

    # 进入已被怪物占据的格子是合法移动，并立即触发遭遇。
    result = world.move_actor("p", (2, 1))
    assert world.actors["p"].position == world.actors["m"].position == (2, 1)
    assert len(result["new_encounters"]) == 1
    encounter = world.encounters[result["new_encounters"][0]]
    assert encounter.active
    assert set(encounter.participants) == {"p", "m"}

    # 墙壁仍然不可通行；用独立世界避免遭遇状态阻止移动。
    blocked = WorldState(clock=WorldClock(), map=_open_map())
    blocked.map.terrain["2,1"] = "wall"
    blocked.add_actor(WorldActorState("p", "玩家", "轮回者", 1, 1))
    with pytest.raises(ValueError, match="不可通行"):
        blocked.move_actor("p", (2, 1))


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
    original_world = engine.state.world_state.copy()
    reroll = engine.execute_action("world_initialize", {"width": 7, "height": 7, "reset": True})
    assert reroll["success"] and reroll["result"]["already_initialized"]
    assert engine.state.world_state == original_world
    status = engine.execute_action("world_status")
    assert status["success"]
    assert status["result"]["map_size"]["width"] == 7
    assert "map" not in status["result"] and "event_log" not in status["result"]
    assert status["result"]["self"]["position"] == [1, 1]
    menu = engine.get_available_actions()
    assert not any(action.get("params_schema", {}).get("sub_action") == "探索"
                   for action in menu.get("actions", []))
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
    world.add_actor(WorldActorState("p", "玩家", "轮回者", 1, 1, faction="player", mode="manual"))
    world.add_actor(WorldActorState("shop", "过路商人", "merchant", 3, 1,
                                    faction="neutral", mode="manual"))
    first = world.move_actor("p", (2, 1))
    assert len(first["new_interactions"]) == 1
    assert first["new_interactions"][0]["target_actor_id"] == "shop"
    assert first["new_interactions"][0]["available_outcomes"] == ["交谈", "交易", "合作", "攻击"]
    # 离开相邻格不触发；重新进入相邻格才会再次触发。
    second = world.move_actor("p", (2, 2))
    assert second["new_interactions"] == []
    third = world.move_actor("p", (2, 1))
    assert len(third["new_interactions"]) == 1


def test_monsters_never_receive_attack_outcome_against_merchant():
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState("m", "怪物", "怪物", 1, 1, faction="monster"))
    world.add_actor(WorldActorState("shop", "过路商人", "merchant", 3, 1, faction="neutral"))
    result = world.move_actor("m", (2, 1))
    assert len(result["new_interactions"]) == 1
    assert "攻击" not in result["new_interactions"][0]["available_outcomes"]


def test_autonomous_units_act_after_player_movement_and_wait_turns():
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState("p", "玩家", "player", 1, 1, faction="player", mode="manual"))
    world.add_actor(WorldActorState("m", "怪物", "怪物", 5, 5, faction="monster"))
    before = world.actors["m"].position
    result = world.move_actor("p", (1, 2))
    assert result["world_turn"] == 1
    assert world.actors["m"].position != before
    after_move = world.actors["m"].position
    world.wait("p", 2)
    assert world.clock.turn == 3
    assert world.actors["m"].position != after_move


def test_autonomous_units_pause_when_participating_in_active_encounter():
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState("p", "玩家", "player", 1, 1, faction="player", mode="manual"))
    world.add_actor(WorldActorState("m", "怪物", "怪物", 2, 1, faction="monster"))
    encounter = world.start_encounter(["p", "m"], (2, 1))
    before = world.actors["m"].position
    with pytest.raises(ValueError, match="遭遇中"):
        world.wait("p", 1)
    assert world.actors["m"].position == before
    assert encounter.active


def test_world_api_syncs_entity_positions_and_opens_adjacent_merchant_session(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "merchant-world.db"),
                        save_dir=str(tmp_path), rng_seed=41)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.merchant = Entity("过路商人", "特殊角色", is_neutral=True, shards=20)
    engine.state.phase = "pre_battle"
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]

    world = WorldState.from_dict(engine.state.world_state)
    player_id = engine.state.player.runtime_id
    merchant_id = engine.state.merchant.runtime_id
    world.actors[player_id].x, world.actors[player_id].y = 1, 1
    world.actors[merchant_id].x, world.actors[merchant_id].y = 3, 1
    world.actors[merchant_id].mode = "manual"
    engine.state.world_state = world.to_dict()

    moved = engine.execute_action("world_move", {"x": 2, "y": 1})
    assert moved["success"], moved
    assert (engine.state.player.world_x, engine.state.player.world_y) == (2, 1)
    assert (engine.state.merchant.world_x, engine.state.merchant.world_y) == (3, 1)
    assert engine.event_pool.current == "过路商人"


def test_world_map_roundtrip_preserves_location_discovery_and_activity():
    world_map = _open_map()
    world_map.add_location("old_hospital", {
        "kind": "event", "name": "旧医院", "event_name": "恶魔医务室",
        "x": 2, "y": 2, "discovered": True, "active": False,
    })
    restored = WorldMap.from_dict(world_map.to_dict())
    assert restored.locations["old_hospital"]["discovered"] is True
    assert restored.locations["old_hospital"]["active"] is False
    assert restored.location_at((2, 2))[0] == "old_hospital"


def test_arriving_at_event_location_opens_event_instead_of_exploration_action(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "locations.db"),
                        save_dir=str(tmp_path), rng_seed=47)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.phase = "pre_battle"
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]
    world = WorldState.from_dict(engine.state.world_state)
    event_name = next(iter(engine.event_pool.events))
    world.map.locations.clear()
    world.map.add_location("test_location", {
        "kind": "event", "name": event_name, "event_name": event_name,
        "x": 2, "y": 1, "discovered": False, "active": True,
    })
    engine.state.world_state = world.to_dict()

    moved = engine.execute_action("world_move", {"x": 2, "y": 1})
    assert moved["success"], moved
    assert moved["result"]["location_event"] == event_name
    assert engine.event_pool.current == event_name
    location = engine.state.world_state["map"]["locations"]["test_location"]
    assert location["discovered"] is True


def test_cultivation_consumes_world_time_without_legacy_energy_cost(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "timed-action.db"),
                        save_dir=str(tmp_path), rng_seed=53)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=20)
    engine.state.phase = "pre_battle"
    energy_before = engine.state.energy
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]

    result = engine.execute_action("pre_battle_action", {
        "sub_action": "修行", "tier": 1,
        "allocations": {"blood_points": 1, "speed_points": 0, "mana_points": 0},
    })
    assert result["success"], result
    assert engine.state.world_state["clock"]["turn"] == 1
    assert engine.state.energy == energy_before
    assert any(event["type"] == "world_action_completed"
               for event in engine.state.world_state["event_log"])


def test_timed_action_is_interrupted_by_hostile_encounter_before_reward(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "interrupted-action.db"),
                        save_dir=str(tmp_path), rng_seed=59)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=20)
    engine.state.enemies = [Entity("M", "怪物", blood_limit=10, current_hp=10)]
    engine.state.phase = "pre_battle"
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]
    world = WorldState.from_dict(engine.state.world_state)
    player_id = engine.state.player.runtime_id
    enemy_id = engine.state.enemies[0].runtime_id
    world.actors[player_id].x, world.actors[player_id].y = 1, 1
    world.actors[enemy_id].x, world.actors[enemy_id].y = 2, 1
    world.actors[enemy_id].mode = "manual"
    engine.state.world_state = world.to_dict()
    result = engine.execute_action("pre_battle_action", {
        "sub_action": "修行", "tier": 1,
        "allocations": {"blood_points": 1, "speed_points": 0, "mana_points": 0},
    })
    assert result["success"], result
    assert result["action"] == "world_action_interrupted"
    assert result["result"]["reward_applied"] is False
    assert engine.state.attribute_points == 0
    assert any(encounter["active"] for encounter in WorldState.from_dict(
        engine.state.world_state).encounters.values())


def test_world_interact_attack_registers_real_encounter_but_does_not_fake_battle(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "interact-attack.db"),
                        save_dir=str(tmp_path), rng_seed=61)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.enemies = [Entity("R", "轮回者", blood_limit=30, current_hp=30)]
    engine.state.phase = "pre_battle"
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]
    world = WorldState.from_dict(engine.state.world_state)
    player_id = engine.state.player.runtime_id
    target_id = engine.state.enemies[0].runtime_id
    world.actors[player_id].x, world.actors[player_id].y = 1, 1
    world.actors[target_id].x, world.actors[target_id].y = 2, 1
    world.actors[target_id].mode = "manual"
    engine.state.world_state = world.to_dict()

    result = engine.execute_action("world_interact", {
        "target_actor_id": target_id, "choice": "attack",
    })
    assert result["success"], result
    assert result["result"]["battle_started"] is False
    restored = WorldState.from_dict(engine.state.world_state)
    assert restored.encounters[result["result"]["encounter_id"]].active


def test_world_interact_trade_requires_actual_adjacency(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "interact-trade.db"),
                        save_dir=str(tmp_path), rng_seed=67)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.merchant = Entity("过路商人", "特殊角色", is_neutral=True, shards=20)
    engine.state.phase = "pre_battle"
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]
    world = WorldState.from_dict(engine.state.world_state)
    player_id = engine.state.player.runtime_id
    merchant_id = engine.state.merchant.runtime_id
    world.actors[player_id].x, world.actors[player_id].y = 1, 1
    world.actors[merchant_id].x, world.actors[merchant_id].y = 4, 4
    world.actors[merchant_id].mode = "manual"
    engine.state.world_state = world.to_dict()
    far = engine.execute_action("world_interact", {
        "target_actor_id": merchant_id, "choice": "trade",
    })
    assert not far["success"]
    assert engine.event_pool.current is None


def test_actor_sightings_are_stored_in_private_bounded_memory():
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState("p", "玩家", "player", 1, 1, faction="player", mode="manual"))
    world.add_actor(WorldActorState("m", "怪物", "enemy", 2, 1, faction="monster", mode="manual"))
    world.visible_cells("p")
    assert world.actors["p"].memory[-1]["actor_id"] == "m"
    assert world.actors["m"].memory == []
    world.actors["p"].memory = [{"turn": n} for n in range(60)]
    world.visible_cells("p")
    assert len(world.actors["p"].memory) <= 50


def test_world_encounter_uses_real_enemy_and_returns_survivors_to_map(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "world-battle.db"),
                        save_dir=str(tmp_path), rng_seed=71)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    target = Entity("R1", "轮回者", blood_limit=30, current_hp=30)
    reserve = Entity("R2", "轮回者", blood_limit=30, current_hp=30)
    engine.state.enemies = [target, reserve]
    engine.state.phase = "pre_battle"
    assert engine.execute_action("world_initialize", {"width": 9, "height": 9})["success"]

    world = WorldState.from_dict(engine.state.world_state)
    player_id = engine.state.player.runtime_id
    target_id, reserve_id = target.runtime_id, reserve.runtime_id
    world.actors[player_id].x, world.actors[player_id].y = 1, 1
    world.actors[target_id].x, world.actors[target_id].y = 2, 1
    world.actors[reserve_id].x, world.actors[reserve_id].y = 7, 7
    world.actors[target_id].mode = "manual"
    world.actors[reserve_id].mode = "manual"
    engine.state.player.world_x, engine.state.player.world_y = 1, 1
    target.world_x, target.world_y = 2, 1
    reserve.world_x, reserve.world_y = 7, 7
    engine.state.world_state = world.to_dict()

    attacked = engine.execute_action("world_interact", {
        "target_actor_id": target_id, "choice": "attack",
    })
    assert attacked["success"], attacked
    encounter_id = attacked["result"]["encounter_id"]
    started = engine.execute_action("battle_start", {
        "world_encounter_id": encounter_id, "relic_choices": {},
    })
    assert started["success"], started
    assert engine.state.enemies == [target]
    assert engine.state.world_enemy_reserve == [reserve]

    target.is_alive = False
    target.current_hp = 0
    ended = engine.execute_action("battle_end", {})
    assert ended["success"], ended
    assert engine.state.phase == "pre_battle"
    assert engine.state.enemies == [reserve]
    assert engine.state.world_enemy_reserve == []
    assert engine.state.world_encounter_id == ""
    restored = WorldState.from_dict(engine.state.world_state)
    assert restored.encounters[encounter_id].active is False
    assert restored.actors[target_id].alive is False
    assert restored.actors[reserve_id].alive is True


def test_world_initialization_spawns_current_dungeon_monster_population_and_event_sites(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "world-population.db"),
                        save_dir=str(tmp_path), rng_seed=73)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.current_region = "乱葬岗"
    engine.state.phase = "pre_battle"
    expected_pool = engine._monster_pool_for_battle()
    result = engine.execute_action("world_initialize", {"width": 21, "height": 21})
    assert result["success"], result
    assert result["result"]["spawned_monsters"] == len(expected_pool)
    assert len(engine.state.enemies) == len(expected_pool)

    world = WorldState.from_dict(engine.state.world_state)
    for monster in engine.state.enemies:
        actor = world.actors[monster.runtime_id]
        assert actor.faction == "monster"
        assert (actor.x, actor.y) != (1, 1)
    positions = [actor.position for actor in world.actors.values() if actor.alive]
    assert len(positions) == len(set(positions))
    event_names = {location["event_name"] for location in world.map.locations.values()}
    assert event_names <= set(engine.event_pool.build_pool("乱葬岗"))
    assert "恶魔医务室" not in event_names


def test_ai_state_projection_hides_unseen_entities_terrain_and_locations(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "fog-projection.db"),
                        save_dir=str(tmp_path), rng_seed=79)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    seen = Entity("近处敌人", "轮回者", blood_limit=30, current_hp=30)
    hidden = Entity("远处敌人", "轮回者", blood_limit=30, current_hp=30)
    engine.state.enemies = [seen, hidden]
    engine.state.phase = "pre_battle"
    assert engine.execute_action("world_initialize", {"width": 9, "height": 9})["success"]
    world = WorldState.from_dict(engine.state.world_state)
    player_id = engine.state.player.runtime_id
    world.actors[player_id].x, world.actors[player_id].y = 1, 1
    world.actors[seen.runtime_id].x, world.actors[seen.runtime_id].y = 2, 1
    world.actors[hidden.runtime_id].x, world.actors[hidden.runtime_id].y = 7, 7
    world.map.add_location("hidden_site", {
        "kind": "event", "name": "隐藏地点", "event_name": "秘密",
        "x": 2, "y": 2, "discovered": False, "active": True,
    })
    engine._sync_world_entity_positions(world)
    engine.state.world_state = world.to_dict()

    observed = engine.get_state()["state"]
    observed_enemy_ids = {entity["runtime_id"] for entity in observed["enemies"]}
    assert seen.runtime_id in observed_enemy_ids
    assert hidden.runtime_id not in observed_enemy_ids
    assert hidden.runtime_id not in observed["world_state"]["actors"]
    assert "7,7" not in observed["world_state"]["map"]["terrain"]
    assert "hidden_site" not in observed["world_state"]["map"]["locations"]
    # 投影只能改变返回副本，不得删除真实地图里的隐藏单位。
    assert hidden.runtime_id in engine.state.world_state["actors"]
