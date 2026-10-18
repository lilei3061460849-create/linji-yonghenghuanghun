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
    world.add_actor(WorldActorState("p", "玩家", "player", 1, 1, faction="player", mode="manual"))
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
    world.add_actor(WorldActorState("p", "玩家", "player", 1, 1, mode="manual"))
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


def test_monsters_are_hostile_to_other_reincarnators_not_only_the_player():
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState("m", "怪物", "enemy", 1, 1,
                                    faction="monster", mode="manual"))
    world.add_actor(WorldActorState("r", "其他轮回者", "enemy", 2, 1,
                                    faction="enemy", mode="manual"))
    encounters = world.detect_adjacent_encounters()
    assert len(encounters) == 1
    assert set(world.encounters[encounters[0]].participants) == {"m", "r"}


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
    assert any(encounter.active for encounter in WorldState.from_dict(
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
    assert result["result"]["spawned_reincarnators"] >= 2
    monsters = [entity for entity in engine.state.enemies if entity.entity_type == "怪物"]
    reincarnators = [entity for entity in engine.state.enemies if entity.entity_type == "轮回者"]
    assert len(monsters) == len(expected_pool)
    assert len(reincarnators) == result["result"]["spawned_reincarnators"]

    world = WorldState.from_dict(engine.state.world_state)
    for monster in monsters:
        actor = world.actors[monster.runtime_id]
        assert actor.faction == "monster"
        assert (actor.x, actor.y) != (1, 1)
    for reincarnator in reincarnators:
        actor = world.actors[reincarnator.runtime_id]
        assert actor.faction == "enemy"
        assert actor.mode == "autonomous"
    positions = [actor.position for actor in world.actors.values() if actor.alive]
    assert len(positions) == len(set(positions))
    event_names = {location["event_name"] for location in world.map.locations.values()
                   if location.get("kind") == "event"}
    assert event_names <= set(engine.event_pool.build_pool("乱葬岗"))
    assert "恶魔医务室" not in event_names


def test_resource_location_is_collected_once_on_arrival_and_updates_shard_peak(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity, GameState

    engine = GameEngine(db_path=str(tmp_path / "world-resource.db"),
                        save_dir=str(tmp_path), rng_seed=79)
    engine.state = GameState(shards=4, shards_peak=8)
    engine.combat.state = engine.state
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30, shards=4)
    engine.state.phase = "pre_battle"
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]

    world = WorldState.from_dict(engine.state.world_state)
    world.map.locations.clear()
    world.map.add_location("cache", {
        "kind": "resource", "name": "碎片残留", "resource_type": "shards",
        "amount": 12, "x": 2, "y": 1, "discovered": False, "active": True,
    })
    engine.state.world_state = world.to_dict()

    first = engine.execute_action("world_move", {"x": 2, "y": 1})
    assert first["success"], first
    assert first["result"]["resource_collected"]["shards"] == 12
    assert engine.state.shards == 16
    assert engine.state.shards_peak == 16
    location = engine.state.world_state["map"]["locations"]["cache"]
    assert location["active"] is False
    assert location["collected"] is True

    # 离开后再回来也不会重复领取。
    assert engine.execute_action("world_move", {"x": 2, "y": 2})["success"]
    back = engine.execute_action("world_move", {"x": 2, "y": 1})
    assert back["success"], back
    assert "resource_collected" not in back["result"]
    assert engine.state.shards == 16


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



def test_square_boundary_shrinks_outer_ring_every_ten_world_turns_and_roundtrips():
    world = WorldState(clock=WorldClock(turn=9), map=_open_map())
    world.add_actor(WorldActorState("p", "玩家", "player", 1, 1,
                                    faction="player", mode="manual"))
    world.map.add_location("edge-event", {
        "kind": "event", "name": "边界事件", "x": 0, "y": 1, "active": True
    })
    world.add_actor(WorldActorState("edge", "边缘单位", "reincarnator", 0, 1,
                                    faction="enemy", mode="manual"))

    # 第10回合结算后，方形地图最外一圈变为不可通行区域。
    result = world.wait("p", 1)
    assert result == 10
    assert world.shrink_count == 1
    assert world.map.terrain_at((0, 1)) == "void"
    assert world.map.terrain_at((1, 1)) == "floor"
    assert not world.map.is_passable((0, 1))
    assert world.map.locations["edge-event"]["active"] is False
    assert any(event["type"] == "actor_caught_by_shrinking_boundary"
               for event in world.event_log)
    restored = WorldState.from_dict(world.to_dict())
    assert restored.shrink_count == 1
    assert restored.map.terrain_at((0, 1)) == "void"



def test_world_resonance_drops_a_real_relic_without_granting_it_immediately(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity, Relic

    engine = GameEngine(db_path=str(tmp_path / "resonance-drop.db"),
                        save_dir=str(tmp_path), rng_seed=91, autosave=False)
    engine.state.player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    engine.state.phase = "pre_battle"
    engine.state.relics_pool = [Relic("测试遗物", "测试效果")]
    engine.state.relic_pool_initialized = True
    assert engine.execute_action("world_initialize", {"width": 9, "height": 9})["success"]

    result = engine.execute_action("pre_battle_action", {"sub_action": "共鸣", "tier": 1})
    assert result["success"], result
    assert result["result"]["acquired"] is False
    assert result["result"]["relic"] == "测试遗物"
    assert result["result"]["path_distance"] <= 10
    assert not any(relic.name == "测试遗物" for relic in engine.state.relics)
    location = engine.state.world_state["map"]["locations"][result["result"]["location_id"]]
    assert location["kind"] == "relic"
    assert location["active"] is True
    assert engine.state.relics_pool == []


def test_player_picks_up_resonance_relic_only_by_entering_its_cell(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "resonance-pickup.db"),
                        save_dir=str(tmp_path), rng_seed=93, autosave=False)
    engine.state.player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    engine.state.phase = "pre_battle"
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]
    world = WorldState.from_dict(engine.state.world_state)
    world.map.locations.clear()
    world.map.add_location("test-relic", {
        "kind": "relic", "name": "测试遗物", "relic_name": "测试遗物",
        "relic_effect": "测试效果", "x": 2, "y": 1,
        "discovered": True, "discovered_by": [engine.state.player.runtime_id],
        "active": True,
    })
    engine.state.world_state = world.to_dict()

    move = engine.execute_action("world_move", {"x": 2, "y": 1})
    assert move["success"], move
    assert move["result"]["relic_collected"]["name"] == "测试遗物"
    assert any(relic.name == "测试遗物" for relic in engine.state.relics)
    assert engine.state.world_state["map"]["locations"]["test-relic"]["active"] is False



def test_hiring_signal_persists_while_player_moves_and_resolves_on_arrival(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "hiring-signal.db"),
                        save_dir=str(tmp_path), rng_seed=97, autosave=False)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.phase = "pre_battle"
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]
    # 先用空副本初始化小地图，再设置专属行动所属副本，避免测试被自动怪物人口干扰。
    engine.state.current_region = "罪孽都市"

    issued = engine.execute_action("pre_battle_action", {
        "sub_action": "雇佣", "name": "测试员工",
        "blood_alloc": 20, "atk_bundles": 0,
    })
    assert issued["success"], issued
    signal_id = issued["result"]["signal_id"]
    assert issued["result"]["status"] == "pending"
    assert engine.state.employees == []

    # 玩家无需停留等待；招募信号在后续世界回合仍然有效。
    assert engine.execute_action("world_move", {"x": 2, "y": 1})["success"]
    signal = next(s for s in engine.state.world_state["hiring_signals"]
                  if s["signal_id"] == signal_id)
    assert signal["status"] == "pending"
    assert engine.execute_action("world_move", {"x": 2, "y": 2})["success"]

    signal = next(s for s in engine.state.world_state["hiring_signals"]
                  if s["signal_id"] == signal_id)
    assert signal["status"] == "completed"
    assert len(engine.state.employees) == 1
    employee = engine.state.employees[0]
    assert employee.name == "测试员工"
    assert employee.runtime_id == signal["responder_actor_id"]
    restored = WorldState.from_dict(engine.state.world_state)
    assert any(actor.actor_id == employee.runtime_id and actor.kind == "employee"
               for actor in restored.actors.values())


def test_learned_daowen_can_be_reserved_for_a_future_friend(tmp_path):
    from engine.api import GameEngine
    from engine.daowen import DaoWenEngine
    from engine.gamedata import learnability_error
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "learned-daowen-reserve.db"),
                        save_dir=str(tmp_path), rng_seed=105, autosave=False)
    engine.state.player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    engine.state.current_region = "罪孽都市"
    engine.state.phase = "pre_battle"
    name = next(name for name in DaoWenEngine.TRANSFORMED_DAOWEN
                if not learnability_error(name, owned=set(),
                                          region=engine.state.current_region,
                                          known=DaoWenEngine.list_all()))

    learned = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "daowen", "tier": 1,
        "names": [name], "recipient": "pending",
    })
    assert learned["success"], learned
    assert name in engine.state.pending_learned_daowen
    assert name not in engine.state.player.dao_wen

    future_friend = Entity("后来结识的朋友", "朋友", blood_limit=30, current_hp=30)
    engine.state.friends.append(future_friend)
    assigned = engine.execute_action("assign_learned_daowen", {
        "name": name, "recipient": future_friend.name,
    })
    assert assigned["success"], assigned
    assert name in future_friend.dao_wen
    assert name not in engine.state.pending_learned_daowen


def test_creation_builds_one_map_being_then_cultivates_unique_preferences(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "created-being.db"),
                        save_dir=str(tmp_path), rng_seed=107, autosave=False)
    engine.state.player = Entity("P", "轮回者", blood_limit=60, current_hp=60,
                                 mana_limit=6, current_mana=6,
                                 speed_limit=4, current_speed=4,
                                 attack_count=2, attack_power=3)
    engine.state.current_region = "乱葬岗"
    engine.state.phase = "pre_battle"
    engine.state.shards = 100
    assert engine.execute_action("world_initialize", {"width": 21, "height": 21})["success"]

    first = engine.execute_action("pre_battle_action", {
        "sub_action": "造生", "name": "测试造物",
    })
    assert first["success"], first
    assert engine.state.shards == 90
    beings = [friend for friend in engine.state.friends
              if getattr(friend, "is_created_being", False)]
    assert len(beings) == 1
    being = beings[0]
    assert being.created_by_runtime_id == engine.state.player.runtime_id
    assert len(being.preferences) == 1
    assert being.runtime_id in engine.state.world_state["actors"]

    second = engine.execute_action("pre_battle_action", {"sub_action": "造生"})
    assert second["success"], second
    assert engine.state.shards == 65
    beings = [friend for friend in engine.state.friends
              if getattr(friend, "is_created_being", False)]
    assert len(beings) == 1
    being = beings[0]
    assert len(being.preferences) == 2
    assert len(set(being.preferences)) == 2


def test_created_being_joins_combat_only_when_explicitly_summoned():
    from engine.models import Entity, GameState

    player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    being = Entity("造物", "朋友", blood_limit=30, current_hp=30)
    being.is_created_being = True
    being.created_by_runtime_id = player.runtime_id
    state = GameState(player=player, friends=[being])

    assert being not in state.get_all_player_side()
    being.is_summoned = True
    assert being in state.get_all_player_side()
    being.is_summoned = False
    assert being not in state.get_all_player_side()


def test_cultivation_points_can_be_assigned_to_created_being(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "created-being-points.db"),
                        save_dir=str(tmp_path), rng_seed=109, autosave=False)
    player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    being = Entity("造物", "朋友", blood_limit=15, current_hp=15)
    being.is_created_being = True
    being.created_by_runtime_id = player.runtime_id
    engine.state.player = player
    engine.state.friends = [being]
    engine.state.attribute_points = 1
    engine.state.phase = "pre_battle"

    result = engine.execute_action("redeem_attribute_points", {
        "target": being.name, "allocations": {"blood_points": 1},
    })
    assert result["success"], result
    assert being.blood_limit == 21
    assert player.blood_limit == 30
    assert engine.state.attribute_points == 0


def test_final_duel_search_starts_when_only_two_reincarnators_remain_even_if_monsters_survive(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "final-duel-search.db"),
                        save_dir=str(tmp_path), rng_seed=103, autosave=False)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    rival = Entity("R", "轮回者", blood_limit=30, current_hp=30)
    monster = Entity("M", "怪物", blood_limit=10, current_hp=10)
    engine.state.enemies = [rival, monster]
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(engine.state.player.runtime_id, "P", "player", 1, 1,
                                    faction="player", mode="manual"))
    world.add_actor(WorldActorState(rival.runtime_id, "R", "enemy", 5, 5,
                                    faction="enemy", mode="autonomous"))
    world.add_actor(WorldActorState(monster.runtime_id, "M", "enemy", 4, 5,
                                    faction="monster", mode="autonomous"))

    result = engine._refresh_world_final_duel(world)
    assert result["active"] is True
    assert set(result["finalists"]) == {engine.state.player.runtime_id, rival.runtime_id}
    assert world.final_duel_search is True
    assert world.final_duel_started is False


def test_final_survivors_converge_to_a_marked_final_encounter():
    world = WorldState(clock=WorldClock(), map=_open_map(),
                       final_duel_search=True,
                       final_duel_finalists=["p", "e"])
    world.add_actor(WorldActorState("p", "玩家", "player", 2, 2,
                                    faction="player", mode="manual"))
    world.add_actor(WorldActorState("e", "最后的轮回者", "reincarnator", 3, 2,
                                    faction="enemy", mode="manual"))
    encounter_ids = world.detect_adjacent_encounters()
    assert len(encounter_ids) == 1
    encounter = world.encounters[encounter_ids[0]]
    assert encounter.final_duel is True
    assert world.final_duel_started is True
    restored = WorldState.from_dict(world.to_dict())
    assert restored.final_duel_search is True
    assert restored.final_duel_finalists == ["p", "e"]
    assert restored.encounters[encounter.encounter_id].final_duel is True
