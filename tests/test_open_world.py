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


def test_boundary_shrink_invalidates_npcs_stale_location_memories():
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.shrink_interval = 1
    world.add_actor(WorldActorState("npc", "流浪者", "轮回者", 2, 2,
                                    faction="enemy", mode="autonomous",
                                    memory=[{
                                        "kind": "location_sighting",
                                        "location_id": "edge-relic",
                                        "location_kind": "relic",
                                        "position": [0, 2],
                                        "active": True,
                                        "turn": 0,
                                    }]))
    world.map.add_location("edge-relic", {
        "kind": "relic", "name": "边界遗物", "x": 0, "y": 2, "active": True,
    })

    world.clock.advance(1)
    result = world._shrink_boundary_if_due()

    assert result is not None
    assert world.map.locations["edge-relic"]["active"] is False
    assert world.actors["npc"].memory[0]["active"] is False
    assert world.actors["npc"].memory[0]["inactive_reason"] == "removed_by_shrink"


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
    assert restored.actors["p"].visited_cells == {"1,1", "2,1"}
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


def test_merchant_trade_action_schema_lists_inventory(tmp_path):
    """Merchant option construction stays compatible with the repo's local Python."""
    from engine.api import GameEngine
    from engine.models import Entity, Relic

    engine = GameEngine(db_path=str(tmp_path / "merchant-actions.db"),
                        save_dir=str(tmp_path), autosave=False)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.merchant = Entity(
        "过路商人", "特殊角色", relics=[Relic("地图遗物", "测试效果")])
    engine.event_pool.current = "过路商人"

    menu = engine.get_available_actions()
    trade = next(action for action in menu["actions"]
                 if action["option"].startswith("遗物交易"))

    assert "地图遗物" in trade["params_schema"]["relic_name"]
    assert "buy可选" in trade["params_schema"]["relic_name"]


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
    assert result["result"]["world_encounter_id"] == result["result"]["encounter_id"]
    restored = WorldState.from_dict(engine.state.world_state)
    assert restored.encounters[result["result"]["world_encounter_id"]].active


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


def test_npc_only_world_encounters_use_combat_engine_and_do_not_stall(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "npc-skirmish.db"),
                        save_dir=str(tmp_path), rng_seed=139, autosave=False)
    player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    rival = Entity("R", "轮回者", blood_limit=30, current_hp=30,
                   attack_count=1, attack_power=20, current_mana=20, mana_limit=20)
    monster = Entity("M", "怪物", blood_limit=5, current_hp=5,
                     attack_count=1, attack_power=1, current_mana=1, mana_limit=1)
    engine.state.player = player
    engine.state.enemies = [rival, monster]
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(player.runtime_id, "P", "player", 1, 1,
                                    faction="player", mode="manual"))
    world.add_actor(WorldActorState(rival.runtime_id, "R", "enemy", 3, 3,
                                    faction="enemy", mode="autonomous"))
    world.add_actor(WorldActorState(monster.runtime_id, "M", "enemy", 4, 3,
                                    faction="monster", mode="autonomous"))
    encounter = world.start_encounter(
        [rival.runtime_id, monster.runtime_id], world.actors[monster.runtime_id].position)

    resolved = engine._resolve_world_npc_encounters(world)
    assert resolved and resolved[0]["encounter_id"] == encounter.encounter_id
    assert not encounter.active
    assert monster.is_alive is False
    assert world.actors[monster.runtime_id].alive is False


def test_autonomous_reincarnator_collects_shard_cache_on_its_cell(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "npc-shards.db"),
                        save_dir=str(tmp_path), rng_seed=127, autosave=False)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.phase = "pre_battle"
    npc = Entity("NPC", "轮回者", blood_limit=30, current_hp=30)
    engine.state.enemies = [npc]
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]
    world = WorldState.from_dict(engine.state.world_state)
    actor = world.actors[npc.runtime_id]
    world.map.add_location("npc-shard-cache", {
        "name": "碎片残留", "kind": "resource", "resource_type": "shards",
        "amount": 7, "x": actor.x, "y": actor.y, "active": True,
    })

    pickups = engine._resolve_world_shard_pickups(world)
    assert any(item["actor_id"] == npc.runtime_id and not item["player_visible"]
               for item in pickups)
    assert npc.shards == 7
    assert npc.shards_peak == 7
    assert world.map.locations["npc-shard-cache"]["active"] is False


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
    # 本用例刻意使用只有一件遗物的定制池，预先给商人独立库存，避免把
    # 「商人必须从真实池抽5件」与「共鸣只投放测试遗物」两个契约混在一起。
    engine.state.merchant = Entity(
        "过路商人", "特殊角色", shards=20,
        relics=[Relic(f"商人遗物{i}", "普通效果") for i in range(5)])
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
    world.add_actor(WorldActorState(
        "npc-memory", "流浪者", "轮回者", 5, 5, faction="neutral", mode="manual",
        memory=[{"kind": "location_sighting", "location_id": "test-relic",
                 "location_kind": "relic", "position": [2, 1], "active": True, "turn": 0}],
    ))
    engine.state.world_state = world.to_dict()

    move = engine.execute_action("world_move", {"x": 2, "y": 1})
    assert move["success"], move
    assert move["result"]["relic_collected"]["name"] == "测试遗物"
    assert any(relic.name == "测试遗物" for relic in engine.state.relics)
    assert engine.state.world_state["map"]["locations"]["test-relic"]["active"] is False
    restored = WorldState.from_dict(engine.state.world_state)
    sighting = restored.actors["npc-memory"].memory[0]
    assert sighting["active"] is False
    assert sighting["inactive_reason"] == "collected"



def test_hiring_signal_fails_instead_of_remaining_pending_when_map_has_no_walkable_tiles(tmp_path):
    from engine.api import GameEngine

    engine = GameEngine(db_path=str(tmp_path / "blocked-hiring.db"),
                        save_dir=str(tmp_path), rng_seed=101, autosave=False)
    world = WorldState.create(width=7, height=7, seed=1)
    world.map.terrain = {key: "wall" for key in world.map.terrain}
    world.hiring_signals.append({
        "signal_id": "signal-blocked", "status": "pending", "arrival_turn": 0,
        "position": [3, 3], "params": {"name": "无法抵达的员工"},
    })

    result = engine._resolve_world_hiring_signals(world)

    assert result == [{
        "signal_id": "signal-blocked", "status": "failed",
        "reason": "地图已无可通行地点，无法完成招募",
    }]
    assert world.hiring_signals[0]["status"] == "failed"
    assert world.hiring_signals[0]["resolved_turn"] == world.clock.turn


def test_hiring_signal_records_failure_if_successful_settlement_creates_no_employee(tmp_path, monkeypatch):
    from engine.api import GameEngine

    engine = GameEngine(db_path=str(tmp_path / "missing-hire.db"),
                        save_dir=str(tmp_path), rng_seed=103, autosave=False)
    world = WorldState.create(width=7, height=7, seed=1)
    world.hiring_signals.append({
        "signal_id": "signal-missing-employee", "status": "pending",
        "arrival_turn": 0, "position": [3, 3],
        "params": {"name": "缺失实体员工"},
    })
    monkeypatch.setattr(engine, "_pre_battle_guyong", lambda params: {"success": True})

    result = engine._resolve_world_hiring_signals(world)

    assert result == [{
        "signal_id": "signal-missing-employee", "status": "failed",
        "reason": "雇佣结算未生成员工实体",
    }]
    assert world.hiring_signals[0]["status"] == "failed"
    assert any(event["type"] == "hiring_signal_failed"
               for event in world.event_log)


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
    name = next(name for name in DaoWenEngine.list_all()
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


def test_ten_thousand_souls_banner_unlocks_creation_outside_graveyard(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity, Relic

    engine = GameEngine(db_path=str(tmp_path / "souls-banner.db"),
                        save_dir=str(tmp_path), rng_seed=111, autosave=False)
    engine.state.player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    engine.state.phase = "pre_battle"
    engine.state.shards = 10
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]
    engine.state.current_region = "扭曲都市"
    engine.state.relics.append(Relic(name="万魂幡", effect="允许在乱葬岗外造生"))

    result = engine.execute_action("pre_battle_action", {
        "sub_action": "造生", "name": "幡下造物",
    })
    assert result["success"], result
    assert any(getattr(friend, "is_created_being", False)
               for friend in engine.state.friends)
    assert engine.state.shards == 0


def test_creation_builds_one_map_being_then_cultivates_stats_and_vision_not_preferences(tmp_path):
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
    assert len(being.dao_wen) == 1
    signature_daowen = next(iter(being.dao_wen))
    assert first["result"]["signature_daowen"] == signature_daowen
    assert being.runtime_id in engine.state.world_state["actors"]

    original_stats = (being.blood_limit, being.mana_limit, being.speed_limit)
    being.current_hp = max(0, being.blood_limit - 3)
    being.current_mana = max(0, being.mana_limit - 1)
    being.current_speed = max(0, being.speed_limit - 1)
    original_resources = (being.current_hp, being.current_mana, being.current_speed)
    second = engine.execute_action("pre_battle_action", {"sub_action": "造生"})
    assert second["success"], second
    assert engine.state.shards == 65
    beings = [friend for friend in engine.state.friends
              if getattr(friend, "is_created_being", False)]
    assert len(beings) == 1
    being = beings[0]
    assert len(being.preferences) == 1
    assert (being.blood_limit, being.mana_limit, being.speed_limit) == tuple(
        value + growth for value, growth in zip(original_stats, (6, 1, 1)))
    assert (being.current_hp, being.current_mana, being.current_speed) == tuple(
        min(limit, current + growth)
        for limit, current, growth in zip(
            (being.blood_limit, being.mana_limit, being.speed_limit),
            original_resources, (6, 1, 1)))
    assert being.created_being_vision_range == 2
    actor_data = engine.state.world_state["actors"][being.runtime_id]
    assert actor_data["vision_range"] == 2
    assert second["result"]["growth"] == {
        "blood_limit": 6, "mana_limit": 1, "speed_limit": 1, "vision_range": 1}
    # 再次造生不新增偏爱，也不会反复抽取/替换专属道纹。
    assert list(being.dao_wen) == [signature_daowen]


def test_reincarnator_trade_requires_fair_offer_and_transfers_atomically(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity, Relic

    engine = GameEngine(db_path=str(tmp_path / "world-trade.db"),
                        save_dir=str(tmp_path), rng_seed=131, autosave=False)
    player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    rival = Entity("R", "轮回者", blood_limit=30, current_hp=30, shards=10)
    engine.state.player = player
    engine.state.phase = "pre_battle"
    engine.state.enemies = [rival]
    engine.state.shards = 20
    engine.state.relics = [Relic(name="玩家遗物", effect="A")]
    rival.relics = [Relic(name="对方遗物", effect="B")]
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(player.runtime_id, "P", "player", 1, 1,
                                    faction="player", mode="manual"))
    world.add_actor(WorldActorState(rival.runtime_id, "R", "enemy", 2, 1,
                                    faction="enemy", mode="autonomous"))
    engine.state.world_state = world.to_dict()

    rejected = engine.execute_action("world_interact", {
        "target_actor_id": rival.runtime_id, "choice": "trade",
        "offer_shards": 0, "request_shards": 5,
    })
    assert rejected["success"] and rejected["result"]["accepted"] is False
    assert engine.state.shards == 20 and rival.shards == 10

    accepted = engine.execute_action("world_interact", {
        "target_actor_id": rival.runtime_id, "choice": "trade",
        "offer_shards": 5, "request_shards": 5,
        "give_relic": "玩家遗物", "request_relic": "对方遗物",
    })
    assert accepted["success"] and accepted["result"]["accepted"] is True
    assert engine.state.shards == 20 and rival.shards == 10
    assert [r.name for r in engine.state.relics] == ["对方遗物"]
    assert [r.name for r in rival.relics] == ["玩家遗物"]


def test_intimidation_temporarily_recruits_rival_then_expires(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "world-intimidation.db"),
                        save_dir=str(tmp_path), rng_seed=137, autosave=False)
    player = Entity("P", "轮回者", blood_limit=30, current_hp=30, attack_power=10)
    rival = Entity("R", "轮回者", blood_limit=30, current_hp=5, attack_power=1)
    engine.state.player = player
    engine.state.phase = "pre_battle"
    engine.state.enemies = [rival]
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(player.runtime_id, "P", "player", 1, 1,
                                    faction="player", mode="manual"))
    world.add_actor(WorldActorState(rival.runtime_id, "R", "enemy", 2, 1,
                                    faction="enemy", mode="autonomous"))
    engine.state.world_state = world.to_dict()
    engine.dice.auto_roll = lambda key, options, context=None: {
        "selected": "success", "record": {"selected_index": 0}
    }

    result = engine.execute_action("world_interact", {
        "target_actor_id": rival.runtime_id, "choice": "intimidate",
    })
    assert result["success"] and result["result"]["accepted"] is True
    assert rival in engine.state.temp_friends
    assert rival not in engine.state.enemies
    assert WorldState.from_dict(engine.state.world_state).actors[rival.runtime_id].faction == "temp_friend"

    waited = engine.execute_action("world_wait", {"turns": 3})
    assert waited["success"], waited
    assert rival not in engine.state.temp_friends
    assert rival in engine.state.enemies
    assert WorldState.from_dict(engine.state.world_state).actors[rival.runtime_id].faction == "enemy"


def test_created_being_can_follow_and_report_exploration_to_owner(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "created-being-behavior.db"),
                        save_dir=str(tmp_path), rng_seed=113, autosave=False)
    player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    being = Entity("造物", "朋友", blood_limit=30, current_hp=30)
    being.is_created_being = True
    being.created_by_runtime_id = player.runtime_id
    engine.state.player = player
    engine.state.phase = "pre_battle"
    engine.state.friends = [being]
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(player.runtime_id, player.name, "player", 1, 1,
                                    faction="player", mode="manual"))
    world.add_actor(WorldActorState(being.runtime_id, being.name, "friend", 5, 5,
                                    faction="friend", mode="autonomous",
                                    behavior="explore", owner_actor_id=player.runtime_id))
    world.map.add_location("phone-booth", {
        "name": "电话亭", "kind": "event", "event_name": "电话亭",
        "x": 5, "y": 4, "active": True,
    })
    being.world_x, being.world_y = 5, 5
    engine.state.world_state = world.to_dict()

    commanded = engine.execute_action("world_command_created_being", {"behavior": "follow"})
    assert commanded["success"], commanded
    before = WorldState.from_dict(engine.state.world_state)
    owner_before = before.actors[player.runtime_id].position
    being_before = before.actors[being.runtime_id].position
    assert before.actors[player.runtime_id].known_cells
    assert before.actors[being.runtime_id].owner_actor_id == player.runtime_id
    status = engine.execute_action("world_status", {})
    assert status["success"], status
    assert any(location["location_id"] == "phone-booth"
               for location in status["result"]["known_locations"])

    waited = engine.execute_action("world_wait", {"turns": 1})
    assert waited["success"], waited
    after = WorldState.from_dict(engine.state.world_state)
    being_after = after.actors[being.runtime_id].position
    assert abs(being_after[0] - owner_before[0]) + abs(being_after[1] - owner_before[1]) < (
        abs(being_before[0] - owner_before[0]) + abs(being_before[1] - owner_before[1]))
    assert after.actors[player.runtime_id].known_cells >= before.actors[player.runtime_id].known_cells
    vision = engine.execute_action("world_vision", {})
    assert vision["success"], vision
    assert "reported_sightings" in vision["result"]


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


def test_replay_driver_explores_unvisited_frontier_when_no_hostile_is_reachable(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity
    from sim.run_open_world_replay import _next_player_step

    engine = GameEngine(db_path=str(tmp_path / "frontier-driver.db"),
                        save_dir=str(tmp_path), rng_seed=105, autosave=False)
    player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.player = player
    engine.state.enemies = []
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(player.runtime_id, "P", "player", 1, 1,
                                    faction="player", mode="manual"))

    destination = _next_player_step(engine, world)

    assert destination is not None
    assert destination != world.actors[player.runtime_id].position
    assert world.map.is_passable(destination)


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


def test_only_surviving_reincarnator_wins_without_a_fake_final_duel(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "solo-survivor.db"),
                        save_dir=str(tmp_path), rng_seed=104, autosave=False)
    player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    dead_rival = Entity("R", "轮回者", blood_limit=30, current_hp=0, is_alive=False)
    engine.state.player = player
    engine.state.enemies = [dead_rival]
    world = WorldState(clock=WorldClock(), map=_open_map(width=21, height=21))
    world.clock.turn = 1
    world.add_actor(WorldActorState(player.runtime_id, "P", "player", 1, 1,
                                    faction="player", mode="manual"))
    world.add_actor(WorldActorState(dead_rival.runtime_id, "R", "enemy", 5, 5,
                                    faction="enemy", mode="autonomous", alive=False))

    result = engine._refresh_world_final_duel(world)

    assert result["complete"] is True
    assert result["sole_survivor"] is True
    assert world.final_duel_started is False
    assert world.final_duel_winner == player.runtime_id
    assert any(event["type"] == "sole_survivor_victory" for event in world.event_log)


def test_replay_validator_accepts_a_recorded_solo_survivor_terminal():
    from sim.run_open_world_replay import validate_complete_playthrough

    payload = {
        "initial_population": {"monsters": 2, "other_reincarnators": 2, "special_locations": 1},
        "final_state": {
            "game_complete": True, "stop_reason": "sole survivor victory",
            "winner_id": "player", "battle_count": 1, "combat_event_count": 2,
            "living_reincarnators_at_end": [{"name": "玩家", "actor_id": "player"}],
        },
        "battles": [{"round_count": 1, "event_count": 2}],
        "driver_timeline": [{"type": "battle_start", "final_duel": False}],
        "world_event_log": [{"type": "sole_survivor_victory", "actor_id": "player"}],
    }

    validate_complete_playthrough(payload)


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


def test_multiple_hostiles_sharing_a_cell_join_one_encounter():
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState("p", "玩家", "轮回者", 2, 2,
                                    faction="player", mode="manual"))
    world.add_actor(WorldActorState("m1", "怪物一", "怪物", 2, 2,
                                    faction="monster", mode="manual"))
    world.add_actor(WorldActorState("m2", "怪物二", "怪物", 2, 2,
                                    faction="monster", mode="manual"))

    encounter_ids = world.detect_adjacent_encounters("p")

    assert len(encounter_ids) == 1
    encounter = world.encounters[encounter_ids[0]]
    assert set(encounter.participants) == {"p", "m1", "m2"}
    assert world.detect_adjacent_encounters("p") == []
    assert sum(
        encounter.active and "p" in encounter.participants
        for encounter in world.encounters.values()
    ) == 1


def test_adjacent_hostile_waits_until_current_encounter_finishes():
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState("p", "玩家", "轮回者", 2, 2,
                                    faction="player", mode="manual"))
    world.add_actor(WorldActorState("m1", "怪物一", "怪物", 2, 2,
                                    faction="monster", mode="manual"))
    world.add_actor(WorldActorState("m2", "怪物二", "怪物", 3, 2,
                                    faction="monster", mode="manual"))

    first = world.detect_adjacent_encounters("p")
    assert len(first) == 1
    assert set(world.encounters[first[0]].participants) == {"p", "m1"}

    world.finish_encounter(first[0], "resolved")
    world.actors["m1"].alive = False
    second = world.detect_adjacent_encounters("p")
    assert len(second) == 1
    assert set(world.encounters[second[0]].participants) == {"p", "m2"}


def test_world_status_exposes_boundary_and_final_search_without_revealing_hidden_positions(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "world-status-final.db"),
                        save_dir=str(tmp_path), rng_seed=149, autosave=False)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.phase = "pre_battle"
    rival = Entity("R", "轮回者", blood_limit=30, current_hp=30)
    engine.state.enemies = [rival]
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]

    world = WorldState.from_dict(engine.state.world_state)
    world.final_duel_search = True
    world.final_duel_finalists = [engine.state.player.runtime_id, rival.runtime_id]
    world.actors[rival.runtime_id].x, world.actors[rival.runtime_id].y = 5, 5
    engine.state.world_state = world.to_dict()

    status = engine.execute_action("world_status", {})
    assert status["success"], status
    result = status["result"]
    assert result["boundary"]["shape"] == "square"
    assert result["boundary"]["next_shrink_turn"] == 10
    assert result["final_duel"]["search_active"] is True
    assert result["final_duel"]["rally_point"] == [3, 3]
    assert "地图中心" in result["final_duel"]["instruction"]
    assert "final_duel_finalists" not in result
    assert [5, 5] not in [actor["position"] for actor in result["visible_actors"]]


def test_world_encounter_handoff_accepts_companions_without_forcing_created_beings_into_combat(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "world-companion-handoff.db"),
                        save_dir=str(tmp_path), rng_seed=151, autosave=False)
    player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    monster = Entity("M", "怪物", blood_limit=10, current_hp=10)
    being = Entity("造物", "朋友", blood_limit=20, current_hp=20)
    being.is_created_being = True
    being.created_by_runtime_id = player.runtime_id
    engine.state.player = player
    engine.state.enemies = [monster]
    engine.state.friends = [being]
    engine.state.phase = "pre_battle"
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]

    world = WorldState.from_dict(engine.state.world_state)
    player_id = player.runtime_id
    monster_id = monster.runtime_id
    being_id = being.runtime_id
    world.actors[player_id].x, world.actors[player_id].y = 2, 2
    world.actors[monster_id].x, world.actors[monster_id].y = 2, 2
    world.actors[being_id].x, world.actors[being_id].y = 2, 2
    engine.state.world_state = world.to_dict()

    encounter_ids = world.detect_adjacent_encounters(player_id)
    assert len(encounter_ids) == 1
    engine.state.world_state = world.to_dict()
    started = engine.execute_action("battle_start", {
        "world_encounter_id": encounter_ids[0], "relic_choices": {},
    })

    assert started["success"], started
    assert engine.state.enemies == [monster]
    assert being not in engine.state.get_all_player_side()


def test_twisted_city_places_modern_landmarks_and_exposes_descriptions_on_discovery(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "twisted-city-landmarks.db"),
                        save_dir=str(tmp_path), rng_seed=157, autosave=False)
    player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    enemy = Entity("M", "怪物", blood_limit=10, current_hp=10)
    engine.state.player = player
    engine.state.phase = "pre_battle"
    engine.state.enemies = [enemy]
    engine.state.current_region = "扭曲都市"
    assert engine.execute_action("world_initialize", {"width": 15, "height": 15})["success"]

    world = WorldState.from_dict(engine.state.world_state)
    landmarks = {
        location["name"]: location for location in world.map.locations.values()
        if location.get("kind") == "landmark"
    }
    assert {"医院", "学校", "停车场", "地铁站"}.issubset(landmarks)
    phone_booth = world.map.locations["绝望来电"]
    assert phone_booth["name"] == "公共电话亭"
    assert phone_booth["event_name"] == "绝望来电"
    storm_site = world.map.locations["猩红暴雨"]
    assert storm_site["name"] == "暴雨避难点"
    assert "血色暴雨" in storm_site["description"]
    assert world.map.locations["祭坛"]["name"] == "碎片祭坛"

    occupied = {
        actor.position for actor in world.actors.values()
        if actor.alive and actor.actor_id != player.runtime_id
    }
    landmark = next(
        location for location in landmarks.values()
        if any(pos not in occupied for pos in world.map.neighbors(
            (int(location["x"]), int(location["y"]))))
    )
    landmark_id = next(
        location_id for location_id, location in world.map.locations.items()
        if location is landmark
    )
    destination = (landmark["x"], landmark["y"])
    neighbor = next(pos for pos in world.map.neighbors(destination) if pos not in occupied)
    player_actor = world.actors[player.runtime_id]
    player_actor.x, player_actor.y = neighbor
    engine.state.world_state = world.to_dict()

    moved = engine.execute_action("world_move", {"x": destination[0], "y": destination[1]})
    assert moved["success"], moved
    status = engine.execute_action("world_status", {})
    assert status["success"], status
    known = next(item for item in status["result"]["known_locations"]
                 if item["location_id"] == landmark_id)
    assert known["name"] == landmark["name"]
    assert known["description"]


def test_autonomous_reincarnator_remembers_visible_resource_and_moves_toward_it():
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(
        "p", "玩家", "player", 1, 1, faction="player", mode="manual"))
    world.add_actor(WorldActorState(
        "r", "流浪轮回者", "enemy", 5, 5, faction="enemy", mode="autonomous"))
    world.map.add_location("cache", {
        "kind": "resource", "name": "碎片残留", "resource_type": "shards",
        "amount": 9, "x": 4, "y": 5, "active": True,
    })

    world.move_actor("p", (1, 2))

    rival = world.actors["r"]
    assert rival.position == (4, 5)
    assert any(entry.get("kind") == "location_sighting"
               and entry.get("location_id") == "cache"
               for entry in rival.memory)


@pytest.mark.parametrize(
    ("behavior", "guard_position", "expected_position"),
    [
        ("follow", None, (5, 4)),
        ("guard", (5, 4), (5, 4)),
    ],
)
def test_created_being_follow_and_guard_orders_override_remembered_loot(
        behavior, guard_position, expected_position):
    world = WorldState(clock=WorldClock(), map=_open_map())
    owner = WorldActorState("p", "玩家", "player", 1, 1,
                            faction="player", mode="manual")
    being = WorldActorState(
        "b", "造物", "friend", 5, 5, faction="friend", mode="autonomous",
        behavior=behavior, owner_actor_id="p", guard_position=guard_position)
    world.add_actor(owner)
    world.add_actor(being)
    # 造物已发现东侧碎片，但显式跟随/守卫命令应优先于自主寻宝。
    being.memory.append({
        "kind": "location_sighting", "location_id": "cache",
        "position": [6, 5], "location_kind": "resource",
        "resource_type": "shards", "active": True, "turn": 0,
    })
    being.known_cells.update(world.map.terrain.keys())
    world._run_autonomous_units()
    assert being.position == expected_position


def test_random_world_generation_keeps_all_passable_cells_connected():
    from collections import deque

    for seed in range(8):
        world_map = WorldMap.generate(width=21, height=21, seed=seed)
        start = (1, 1)
        reached = {start}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for neighbor in world_map.neighbors(current):
                if neighbor not in reached:
                    reached.add(neighbor)
                    queue.append(neighbor)
        passable = {
            (x, y) for y in range(world_map.height) for x in range(world_map.width)
            if world_map.is_passable((x, y))
        }
        assert reached == passable


def test_autonomous_reincarnator_can_buy_one_finite_stock_relic_from_merchant(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity, Relic

    engine = GameEngine(db_path=str(tmp_path / "npc-merchant-trade.db"),
                        save_dir=str(tmp_path), rng_seed=149, autosave=False)
    player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    rival = Entity("R", "轮回者", blood_limit=30, current_hp=30, shards=10)
    merchant = Entity("过路商人", "特殊角色", shards=20,
                      relics=[Relic(name="测试遗物", effect="测试效果")])
    engine.state.player = player
    engine.state.enemies = [rival]
    engine.state.merchant = merchant
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(
        player.runtime_id, "P", "player", 1, 1, faction="player", mode="manual"))
    world.add_actor(WorldActorState(
        rival.runtime_id, "R", "enemy", 2, 2, faction="enemy", mode="autonomous"))
    world.add_actor(WorldActorState(
        merchant.runtime_id, "过路商人", "merchant", 5, 5,
        faction="neutral", mode="autonomous"))

    assert engine._resolve_world_npc_merchant_trades(world) == []
    assert rival.shards == 10 and len(merchant.relics) == 1

    world.actors[merchant.runtime_id].x = 3
    world.actors[merchant.runtime_id].y = 2
    trades = engine._resolve_world_npc_merchant_trades(world)
    assert len(trades) == 1
    assert trades[0]["relic_name"] == "测试遗物"
    assert rival.shards == 0
    assert merchant.shards == 30
    assert [relic.name for relic in rival.relics] == ["测试遗物"]
    assert merchant.relics == []
    assert any(event["type"] == "npc_merchant_trade" for event in world.event_log)


def test_repeated_sightings_in_one_turn_do_not_duplicate_memory_around_location_notes():
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState("p", "玩家", "player", 1, 1, faction="player"))
    world.add_actor(WorldActorState("m", "流浪者", "enemy", 2, 1, faction="enemy"))
    world.visible_cells("p")
    world.actors["p"].memory.append({
        "kind": "location_sighting", "location_id": "cache",
        "position": [1, 2], "turn": world.clock.turn,
    })

    world.visible_cells("p")

    sightings = [entry for entry in world.actors["p"].memory
                 if entry.get("actor_id") == "m" and entry.get("turn") == world.clock.turn]
    assert len(sightings) == 1


def test_entity_death_is_synchronized_to_world_actor_and_prevents_future_movement(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "world-death-sync.db"),
                        save_dir=str(tmp_path), rng_seed=157, autosave=False)
    player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    rival = Entity("R", "轮回者", blood_limit=30, current_hp=30)
    engine.state.player = player
    engine.state.enemies = [rival]
    engine.state.phase = "pre_battle"
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(
        player.runtime_id, "P", "player", 1, 1, faction="player", mode="manual"))
    world.add_actor(WorldActorState(
        rival.runtime_id, "R", "enemy", 3, 3, faction="enemy", mode="autonomous"))

    rival.is_alive = False
    rival.current_hp = 0
    engine._sync_world_entity_positions(world)

    assert world.actors[rival.runtime_id].alive is False
    with pytest.raises(ValueError, match="不存在或已死亡"):
        world.move_actor(rival.runtime_id, (3, 2))


def test_merchant_trade_checks_the_payer_balance_not_the_wrong_side(tmp_path):
    from engine.api import GameEngine
    from engine.events import resolve_option_effect
    from engine.models import Entity, Relic

    engine = GameEngine(db_path=str(tmp_path / "merchant-payer.db"),
                        save_dir=str(tmp_path), rng_seed=163, autosave=False)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.shards = 20
    engine.state.relics = []
    merchant = Entity("过路商人", "特殊角色", shards=0,
                      relics=[Relic(name="商人遗物", effect="测试效果")])
    engine.state.merchant = merchant

    bought = resolve_option_effect("遗物交易：买入", engine, event_name="过路商人",
                                   params={"trade_direction": "buy", "relic_name": "商人遗物",
                                           "price": 15, "confirmed": True})
    assert "error" not in bought
    assert engine.state.shards == 5
    assert merchant.shards == 15
    assert [relic.name for relic in engine.state.relics] == ["商人遗物"]
    assert merchant.relics == []

    merchant.relics.append(Relic(name="第二件遗物", effect="测试效果"))
    engine.state.shards = 10
    cannot_buy = resolve_option_effect("遗物交易：买入", engine, event_name="过路商人",
                                       params={"trade_direction": "buy", "relic_name": "第二件遗物",
                                               "price": 15, "confirmed": True})
    assert "error" in cannot_buy and "你没有足够碎片" in cannot_buy["error"]

    engine.state.relics.append(Relic(name="待售遗物", effect="测试效果"))
    merchant.shards = 5
    cannot_sell = resolve_option_effect("遗物交易：卖出", engine, event_name="过路商人",
                                        params={"trade_direction": "sell", "relic_name": "待售遗物",
                                                "price": 10, "confirmed": True})
    assert "error" in cannot_sell and "商人碎片不足" in cannot_sell["error"]


def test_autonomous_reincarnator_can_sell_a_relic_only_within_merchant_budget(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity, Relic

    engine = GameEngine(db_path=str(tmp_path / "npc-merchant-sell.db"),
                        save_dir=str(tmp_path), rng_seed=167, autosave=False)
    player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    rival = Entity("R", "轮回者", blood_limit=30, current_hp=30,
                   relics=[Relic(name="可售遗物", effect="普通效果")])
    merchant = Entity("过路商人", "特殊角色", shards=20, relics=[])
    engine.state.player = player
    engine.state.enemies = [rival]
    engine.state.merchant = merchant
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(
        player.runtime_id, "P", "player", 1, 1, faction="player", mode="manual"))
    world.add_actor(WorldActorState(
        rival.runtime_id, "R", "enemy", 2, 2, faction="enemy", mode="autonomous"))
    world.add_actor(WorldActorState(
        merchant.runtime_id, "过路商人", "merchant", 3, 2,
        faction="neutral", mode="autonomous"))

    trades = engine._resolve_world_npc_merchant_trades(world)
    assert len(trades) == 1
    assert trades[0]["direction"] == "sell"
    assert rival.shards == 10 and merchant.shards == 10
    assert rival.relics == []
    assert [relic.name for relic in merchant.relics] == ["可售遗物"]

    merchant.shards = 5
    rival.shards = 0
    rival.relics.append(Relic(name="另一件可售遗物", effect="普通效果"))
    blocked = engine._resolve_world_npc_merchant_trades(world)
    assert blocked == []
    assert rival.shards == 0 and merchant.shards == 5
    assert [relic.name for relic in rival.relics] == ["另一件可售遗物"]


def test_world_move_does_not_report_hidden_npc_merchant_trades(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "hidden-npc-trade.db"),
                        save_dir=str(tmp_path), rng_seed=173, autosave=False)
    player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    rival = Entity("R", "轮回者", blood_limit=30, current_hp=30)
    merchant = Entity("过路商人", "特殊角色", shards=20)
    engine.state.player = player
    engine.state.enemies = [rival]
    engine.state.merchant = merchant
    engine.state.phase = "pre_battle"
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(
        player.runtime_id, "P", "player", 1, 1, faction="player", mode="manual"))
    world.add_actor(WorldActorState(
        rival.runtime_id, "R", "enemy", 5, 5, faction="enemy", mode="manual"))
    world.add_actor(WorldActorState(
        merchant.runtime_id, "过路商人", "merchant", 5, 4,
        faction="neutral", mode="manual"))
    engine.state.world_state = world.to_dict()
    engine._resolve_world_npc_merchant_trades = lambda _: [{
        "actor_id": rival.runtime_id, "direction": "buy",
        "relic_name": "隐藏遗物", "price": 10,
    }]

    moved = engine.execute_action("world_move", {"x": 1, "y": 2})

    assert moved["success"], moved
    assert "npc_merchant_trades" not in moved["result"]


def test_npc_pathfinding_does_not_use_unseen_terrain():
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(
        "r", "流浪轮回者", "enemy", 5, 5, faction="enemy", mode="autonomous"))
    actor = world.actors["r"]
    actor.known_cells = {"5,5", "4,5", "5,4", "5,6", "6,5", "2,5"}

    assert world.map.find_path((5, 5), (2, 5)) is not None
    assert world._find_known_path("r", (5, 5), (2, 5)) is None





def test_trade_does_not_count_temporary_transfer_balance_as_shard_peak(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity, GameState

    engine = GameEngine(db_path=str(tmp_path / "world-trade-peak.db"),
                        save_dir=str(tmp_path), rng_seed=149, autosave=False)
    engine.state = GameState(shards=10, shards_peak=10)
    engine.combat.state = engine.state
    player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    rival = Entity("R", "轮回者", blood_limit=30, current_hp=30, shards=10)
    rival.shards_peak = 10
    engine.state.player = player
    engine.state.phase = "pre_battle"
    engine.state.enemies = [rival]

    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(player.runtime_id, "P", "player", 1, 1,
                                    faction="player", mode="manual"))
    world.add_actor(WorldActorState(rival.runtime_id, "R", "enemy", 2, 1,
                                    faction="enemy", mode="autonomous"))
    engine.state.world_state = world.to_dict()

    result = engine.execute_action("world_interact", {
        "target_actor_id": rival.runtime_id, "choice": "trade",
        "offer_shards": 10, "request_shards": 10,
    })

    assert result["success"] and result["result"]["accepted"] is True
    # 双方最终余额均未变化；交易中先入账的临时值不得伪造历史最高持有量。
    assert engine.state.shards == 10
    assert rival.shards == 10
    assert engine.state.shards_peak == 10
    assert rival.shards_peak == 10


def test_final_duelist_explores_known_frontiers_after_reaching_center():
    world = WorldState(clock=WorldClock(), map=_open_map(9, 9))
    world.add_actor(WorldActorState(
        "player", "玩家", "轮回者", 1, 1, faction="player", mode="manual"))
    world.add_actor(WorldActorState(
        "rival", "最后的对手", "轮回者", 4, 4, faction="enemy", mode="autonomous"))
    world.final_duel_search = True
    world.final_duel_finalists = ["player", "rival"]

    for _ in range(12):
        world.clock.advance(1)
        world._run_autonomous_units()

    rival = world.actors["rival"]
    # 搜索不应在中心周围循环；它应持续扩展自己的已知地图并走访更多格子。
    assert len(rival.visited_cells) > 8
    assert max(max(abs(WorldMap.parse(cell)[0] - 4), abs(WorldMap.parse(cell)[1] - 4))
               for cell in rival.visited_cells) > 1



def test_distant_final_duelist_reaches_rally_area_and_starts_final_encounter():
    world = WorldState(clock=WorldClock(), map=_open_map(9, 9))
    world.add_actor(WorldActorState(
        "player", "玩家", "player", 4, 4, faction="player", mode="manual"))
    world.add_actor(WorldActorState(
        "rival", "最后的对手", "轮回者", 8, 8, faction="enemy", mode="autonomous"))
    world.final_duel_search = True
    world.final_duel_finalists = ["player", "rival"]

    for _ in range(12):
        if any(encounter.active and encounter.final_duel
               for encounter in world.encounters.values()):
            break
        world.wait("player", 1)

    final_encounters = [encounter for encounter in world.encounters.values()
                        if encounter.active and encounter.final_duel]
    assert len(final_encounters) == 1
    assert set(final_encounters[0].participants) == {"player", "rival"}
    assert world.final_duel_started is True

def test_final_duelist_tracks_only_last_known_opponent_position():
    world = WorldState(clock=WorldClock(), map=_open_map(9, 9))
    world.add_actor(WorldActorState(
        "player", "玩家", "轮回者", 1, 1, faction="player", mode="manual"))
    rival = WorldActorState(
        "rival", "最后的对手", "轮回者", 4, 4, faction="enemy", mode="autonomous")
    rival.known_cells.update({"4,4", "3,4", "2,4"})
    rival.memory.append({
        "turn": 2, "actor_id": "player", "name": "玩家",
        "kind": "player", "position": [2, 4],
    })
    world.add_actor(rival)
    world.final_duel_search = True
    world.final_duel_finalists = ["player", "rival"]

    world.clock.advance(1)
    world._run_autonomous_units()

    assert world.actors["rival"].position == (3, 4)

def test_traveling_merchant_is_neutral_immediately_when_created(tmp_path):
    from engine.api import GameEngine

    engine = GameEngine(db_path=str(tmp_path / "merchant-neutral.db"),
                        save_dir=str(tmp_path), rng_seed=181, autosave=False)

    merchant = engine._initialize_merchant_inventory()

    assert merchant.is_neutral is True
    assert merchant.shards == 20
    assert len(merchant.relics) == 5




def test_entity_shard_peak_is_monotonic_through_the_active_setattr_hook():
    from engine.models import Entity

    entity = Entity("欠债单位", "轮回者", shards=30)
    assert entity.shards_peak == 30
    entity.shards = -5
    assert entity.shards_peak == 30
    entity.shards_peak = 1
    assert entity.shards_peak == 30
    entity.shards = 45
    assert entity.shards_peak == 45


def test_npc_merchant_completes_at_most_one_trade_per_world_turn(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity, Relic

    engine = GameEngine(db_path=str(tmp_path / "npc-merchant-one-trade.db"),
                        save_dir=str(tmp_path), rng_seed=191, autosave=False)
    player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    first = Entity("甲", "轮回者", blood_limit=30, current_hp=30, shards=20)
    second = Entity("乙", "轮回者", blood_limit=30, current_hp=30, shards=20)
    merchant = Entity("过路商人", "特殊角色", shards=0, relics=[
        Relic(name="遗物甲", effect="普通效果"),
        Relic(name="遗物乙", effect="普通效果"),
    ])
    engine.state.player = player
    engine.state.enemies = [first, second]
    engine.state.merchant = merchant
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(
        player.runtime_id, "P", "player", 0, 0, faction="player", mode="manual"))
    world.add_actor(WorldActorState(
        first.runtime_id, "甲", "enemy", 3, 2, faction="enemy", mode="autonomous"))
    world.add_actor(WorldActorState(
        second.runtime_id, "乙", "enemy", 2, 3, faction="enemy", mode="autonomous"))
    world.add_actor(WorldActorState(
        merchant.runtime_id, "过路商人", "merchant", 3, 3,
        faction="neutral", mode="autonomous"))

    trades = engine._resolve_world_npc_merchant_trades(world)
    # 即使调用方在同一世界回合重复检查，也不能重复成交。
    assert engine._resolve_world_npc_merchant_trades(world) == []

    assert len(trades) == 1
    assert merchant.shards == 10
    assert len(merchant.relics) == 1
    assert sum(len(entity.relics) for entity in (first, second)) == 1
    # NPC交易优先级由地图位置决定，不受随机runtime_id影响。
    assert first.shards == 10
    assert second.shards == 20


def test_autonomous_npc_order_is_stable_when_runtime_ids_change():
    def make_world(alpha_id, beta_id):
        world = WorldState.create(width=12, height=12, seed=77)
        world.map = _open_map(12, 12)
        world.add_actor(WorldActorState(
            "player", "玩家", "player", 0, 0, faction="player", mode="manual"))
        world.add_actor(WorldActorState(
            alpha_id, "甲", "enemy", 7, 7, faction="enemy", mode="autonomous"))
        world.add_actor(WorldActorState(
            beta_id, "乙", "enemy", 9, 9, faction="enemy", mode="autonomous"))
        world._run_autonomous_units()
        return {actor.name: actor.position for actor in world.actors.values()}

    first = make_world("z-alpha", "a-beta")
    second = make_world("a-alpha", "z-beta")
    assert first == second


def test_world_initialize_spawns_exactly_five_relics_on_neutral_merchant(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "world-merchant-init.db"),
                        save_dir=str(tmp_path), rng_seed=223, autosave=False)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.enemies = [Entity("M", "怪物", blood_limit=10, current_hp=10)]
    engine.state.current_region = "扭曲都市"
    engine.state.phase = "pre_battle"

    result = engine.execute_action("world_initialize", {"width": 7, "height": 7})

    assert result["success"], result
    merchant = engine.state.merchant
    assert merchant is not None
    assert len(merchant.relics) == 5
    world = WorldState.from_dict(engine.state.world_state)
    actor = world.actors[merchant.runtime_id]
    assert actor.kind == "merchant"
    assert actor.faction == "neutral"
    assert actor.mode == "autonomous"
    assert len({relic.name for relic in merchant.relics}) == 5



def test_world_learning_action_advances_three_turns_before_granting_daowen(tmp_path):
    from engine.api import GameEngine
    from engine.daowen import DaoWenEngine
    from engine.gamedata import learnability_error
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "world-learning.db"),
                        save_dir=str(tmp_path), rng_seed=113, autosave=False)
    engine.state.player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    engine.state.current_region = "罪孽都市"
    engine.state.phase = "pre_battle"
    energy_before = engine.state.energy
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]
    name = next(name for name in DaoWenEngine.list_all()
                if not learnability_error(name, owned=set(),
                                          region=engine.state.current_region,
                                          known=DaoWenEngine.list_all()))

    result = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "daowen", "tier": 1,
        "names": [name], "recipient": "player",
    })

    assert result["success"], result
    assert engine.state.world_state["clock"]["turn"] == 3
    assert name in engine.state.player.dao_wen
    events = engine.state.world_state["event_log"]
    started = next(event for event in events if event["type"] == "world_action_started"
                   and event.get("details", {}).get("action") == "学习")
    completed = next(event for event in events if event["type"] == "world_action_completed"
                     and event.get("details", {}).get("action") == "学习")
    assert started["details"]["duration"] == 3
    assert completed["details"]["elapsed_turns"] == 3
    assert engine.state.energy == energy_before


def test_final_duel_counts_a_reincarnator_who_became_a_friend(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "allied-finalist.db"),
                        save_dir=str(tmp_path), rng_seed=137, autosave=False)
    player = Entity("玩家", "轮回者", blood_limit=30, current_hp=30)
    rival = Entity("旧盟友", "轮回者", blood_limit=30, current_hp=30)
    engine.state.player = player
    engine.state.friends = [rival]
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(
        player.runtime_id, player.name, "player", 1, 1,
        faction="player", mode="manual"))
    world.add_actor(WorldActorState(
        rival.runtime_id, rival.name, "friend", 5, 5,
        faction="friend", mode="manual", owner_actor_id=player.runtime_id))

    result = engine._refresh_world_final_duel(world)

    assert result["active"] is True
    assert world.final_duel_finalists == [player.runtime_id, rival.runtime_id]
    assert rival not in engine.state.friends
    assert rival in engine.state.enemies
    assert world.actors[rival.runtime_id].kind == "enemy"
    assert world.actors[rival.runtime_id].faction == "enemy"
    assert world.actors[rival.runtime_id].mode == "autonomous"


def test_final_duel_search_marks_survivor_as_departed_from_world_map(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "final-duel-exit.db"),
                        save_dir=str(tmp_path), rng_seed=139, autosave=False)
    player = Entity("玩家", "轮回者", blood_limit=30, current_hp=30)
    rival = Entity("败者", "轮回者", blood_limit=30, current_hp=0)
    rival.is_alive = False
    engine.state.player = player
    engine.state.enemies = [rival]
    engine.state.phase = "pre_battle"
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(
        player.runtime_id, player.name, "player", 1, 1,
        faction="player", mode="manual", alive=True))
    world.add_actor(WorldActorState(
        rival.runtime_id, rival.name, "enemy", 5, 5,
        faction="enemy", mode="autonomous", alive=False))
    world.final_duel_search = True
    world.final_duel_finalists = [player.runtime_id, rival.runtime_id]

    result = engine._refresh_world_final_duel(world)

    assert result["complete"] is True
    assert result["winner"] == player.runtime_id
    assert player.is_alive
    assert world.actors[player.runtime_id].alive is False
    assert any(event["type"] == "final_duel_winner_departed"
               for event in world.event_log)
    engine.state.world_state = world.to_dict()
    status = engine.execute_action("world_status", {})
    assert status["success"], status
    assert status["result"]["final_duel"]["winner_name"] == player.name


def test_timed_action_does_not_award_reward_after_final_duel_completes(tmp_path, monkeypatch):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "final-duel-interrupt.db"),
                        save_dir=str(tmp_path), rng_seed=157)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=20)
    engine.state.phase = "pre_battle"
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]
    player_id = engine.state.player.runtime_id
    points_before = engine.state.attribute_points

    def finish_final_duel(world):
        world.final_duel_complete = True
        world.final_duel_winner = player_id
        world.actors[player_id].alive = False
        return {"active": False, "complete": True, "winner": player_id}

    monkeypatch.setattr(engine, "_refresh_world_final_duel", finish_final_duel)
    result = engine.execute_action("pre_battle_action", {
        "sub_action": "修行", "tier": 1,
        "allocations": {"blood_points": 1, "speed_points": 0, "mana_points": 0},
    })

    assert result["success"], result
    assert result["action"] == "world_action_interrupted"
    assert result["result"]["reason"] == "final_duel_completed"
    assert result["result"]["reward_applied"] is False
    assert engine.state.attribute_points == points_before
    settled = WorldState.from_dict(engine.state.world_state)
    assert settled.final_duel_complete is True
    assert any(event["type"] == "world_action_interrupted"
               and event["details"].get("reason") == "final_duel_completed"
               for event in settled.event_log)


def test_world_fact_log_records_death_and_departure_once(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "world-outcomes.db"),
                        save_dir=str(tmp_path), rng_seed=163)
    player = Entity("玩家", "轮回者", blood_limit=30, current_hp=30)
    dead = Entity("已死轮回者", "轮回者", blood_limit=20, current_hp=0)
    dead.is_alive = False
    departed = Entity("离场轮回者", "轮回者", blood_limit=20, current_hp=20)
    departed.is_departed = True
    departed.departure_reason = "逃离副本"
    engine.state.player = player
    engine.state.enemies = [dead, departed]
    world = WorldState(clock=WorldClock(), map=_open_map())
    world.add_actor(WorldActorState(
        player.runtime_id, player.name, "player", 1, 1,
        faction="player", mode="manual"))
    world.add_actor(WorldActorState(
        dead.runtime_id, dead.name, "enemy", 2, 1,
        faction="enemy", mode="autonomous", alive=False))
    world.add_actor(WorldActorState(
        departed.runtime_id, departed.name, "enemy", 3, 1,
        faction="enemy", mode="autonomous", alive=True))

    engine._sync_world_entity_positions(world)
    engine._sync_world_entity_positions(world)

    deaths = [event for event in world.event_log
              if event["type"] == "actor_died" and event["actor_id"] == dead.runtime_id]
    departures = [event for event in world.event_log
                  if event["type"] == "actor_departed"
                  and event["actor_id"] == departed.runtime_id]
    assert len(deaths) == 1
    assert deaths[0]["details"]["name"] == dead.name
    assert deaths[0]["details"]["position"] == [2, 1]
    assert len(departures) == 1
    assert departures[0]["details"]["reason"] == "逃离副本"
    assert world.actors[dead.runtime_id].alive is False
    assert world.actors[departed.runtime_id].alive is False

    # 战终时死亡参与者会从state.enemies移除，仍需通过结算快照写入账本。
    removed_from_roster = Entity("已从名单移除", "轮回者", blood_limit=10, current_hp=0)
    removed_from_roster.is_alive = False
    world.add_actor(WorldActorState(
        removed_from_roster.runtime_id, removed_from_roster.name, "enemy", 4, 1,
        faction="enemy", mode="autonomous", alive=False))
    engine._record_world_entity_outcomes(world, extra_entities=[removed_from_roster])
    assert any(event["type"] == "actor_died"
               and event["actor_id"] == removed_from_roster.runtime_id
               for event in world.event_log)


def test_deleted_affix_action_and_choice_endpoint_stay_unavailable(tmp_path):
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "removed-affix.db"),
                        save_dir=str(tmp_path), rng_seed=167)
    engine.state.player = Entity("玩家", "轮回者", blood_limit=30, current_hp=30)
    engine.state.phase = "pre_battle"

    affix = engine.execute_action("pre_battle_action", {
        "sub_action": "附煞", "mode": "发现",
    })
    legacy_choice = engine.execute_action("choose_sha_qi", {
        "sha_qi": "法煞", "daowen_name": "测试道纹",
    })

    assert affix["success"] is False
    assert ("附煞已删除" in affix["error"]
            or "未知局外行动" in affix["error"])
    assert legacy_choice["success"] is False



def test_world_vision_range_expands_with_manhattan_distance_and_respects_walls():
    world_map = _open_map()
    world_map.terrain[world_map.key((2, 1))] = "wall"
    world = WorldState(clock=WorldClock(), map=world_map)
    world.add_actor(WorldActorState("scout", "造物", "friend", 1, 1,
                                    faction="friend", vision_range=2))
    visible = set(world.visible_cells("scout"))
    assert (1, 1) in visible
    assert (2, 1) in visible
    # 墙后方不会被透视，即使目标仍处于数值半径之内。
    assert (3, 1) not in visible
    # 另一方向的第二格仍然可见。
    assert (1, 3) in visible
"""Deterministic end-to-end proof for the canonical open-world loop."""

from sim.run_open_world_replay import SEED, run_playthrough


def test_seeded_open_world_run_reaches_final_duel_and_exports_engine_events():
    replay = run_playthrough(SEED, include_winner_snapshot=True,
                             blood_points=11, speed_points=6, mana_points=8)
    final = replay["final_state"]

    assert final["game_complete"] is True
    assert final["stop_reason"] == "final duel completed"
    assert final["winner_id"]
    assert final["winner_name"]
    player_won = final["winner_id"] == final["player"]["runtime_id"]
    assert final["player"]["alive"] is player_won
    assert final["world_turn"] > 0
    if player_won:
        winner_snapshot = replay["winner_snapshot"]
        assert winner_snapshot["player"]["name"] == final["winner_name"]
        assert winner_snapshot["player"]["entity_type"] == "轮回者"
        assert winner_snapshot["player"]["is_alive"] is True
        assert len(winner_snapshot["player"].get("dao_wen") or {}) >= 2
        assert "candidates" not in winner_snapshot
    else:
        assert "winner_snapshot" not in replay

    # The run must use the ordinary tier-1 population and a rules-valid setup,
    # not a handful of hand-placed enemies or an over-budget test character.
    assert replay["run_config"]["region"] == "罪孽都市"
    assert replay["initial_population"]["monsters"] >= 1
    assert replay["initial_population"]["other_reincarnators"] >= 2
    assert replay["initial_population"]["special_locations"] >= 1
    setup = replay["driver_timeline"][0]["setup"]
    assert setup["attributes"]["total"] == 25
    assert any(item["kind"] == "daowen" and item["success"]
               for item in setup["growth_actions"])
    assert setup["spell_capabilities"]

    assert final["battle_count"] == len(replay["battles"]) >= 1
    assert any(entry.get("type") == "battle_start" and not entry.get("final_duel", False)
               for entry in replay["driver_timeline"]), "the map run must include a normal encounter"
    assert final["combat_event_count"] == sum(
        battle["event_count"] for battle in replay["battles"])
    final_battle = replay["battles"][-1]
    assert final_battle["outcome"] == ("player_won" if player_won else "opponent_won")
    final_duel_entries = [entry for entry in replay["driver_timeline"]
                          if entry["type"] == "final_duel"]
    assert len(final_duel_entries) == 1
    assert final_duel_entries[0]["result"]["winner"] == ("challenger" if player_won else "defender")
    assert final_battle["round_count"] >= 1
    assert final_battle["event_count"] >= 1
    assert all(event["battle_no"] == final_battle["battle_no"]
               for event in final_battle["events"])
    assert max(event["round_no"] for event in final_battle["events"]) == final_battle["round_count"]

    completions = [event for event in replay["world_event_log"]
                   if event["type"] == "final_duel_completed"]
    assert len(completions) == 1
    assert completions[0]["details"]["winner"] == final["winner_id"]
    merged = replay["timeline"]
    assert [event["sequence"] for event in merged] == list(range(len(merged)))
    assert [event["world_turn"] for event in merged] == sorted(
        event["world_turn"] for event in merged)
    assert sum(event["source"] == "world_fact" for event in merged) == len(
        replay["world_event_log"])
    assert sum(event["source"] == "combat_event" for event in merged) == final["combat_event_count"]
    assert any(event["source"] == "combat_event" and event["battle_no"] == final_battle["battle_no"]
               for event in merged)
    assert final["living_reincarnators_at_end"] == [{
        "name": final["winner_name"], "actor_id": final["winner_id"],
        "entity_alive": True, "map_present": False,
        "position": final["living_reincarnators_at_end"][0]["position"],
        "winner_departed": True,
    }]

    repeated = run_playthrough(SEED, blood_points=11, speed_points=6, mana_points=8)
    assert repeated["final_state"]["world_turn"] == final["world_turn"]
    assert repeated["final_state"]["player"]["hp"] == final["player"]["hp"]
    assert repeated["final_state"]["winner_name"] == final["winner_name"]
    assert [(battle["round_count"], battle["event_count"], battle["outcome"])
            for battle in repeated["battles"]] == [
        (battle["round_count"], battle["event_count"], battle["outcome"])
        for battle in replay["battles"]]


def test_replay_waits_for_rivals_when_no_hostile_is_reachable():
    from sim.run_open_world_replay import run_playthrough, validate_complete_playthrough

    replay = run_playthrough(
        seed=12, region="龙心谷", player_name="测试轮回者",
        blood_points=11, speed_points=6, mana_points=8,
    )

    assert replay["final_state"]["stop_reason"] in {
        "final duel completed", "sole survivor victory",
    }
    validate_complete_playthrough(replay)


def test_final_duel_loss_still_settles_world_before_death_legacy_interrupt():
    replay = run_playthrough(SEED, blood_points=13, speed_points=6, mana_points=6)
    final = replay["final_state"]

    assert final["game_complete"] is True
    assert final["stop_reason"] == "final duel completed"
    assert final["player"]["alive"] is False
    assert final["winner_id"] != final["player"]["runtime_id"]
    assert replay["battles"][-1]["outcome"] == "opponent_won"
    assert replay["battles"][-1]["round_count"] == 7
    assert any(entry["type"] == "pending_postgame_interrupt"
               and entry["interrupt"]["interrupt_type"] == "死之传承"
               for entry in replay["driver_timeline"])
    assert any(event["type"] == "final_duel_completed"
               and event["details"]["winner"] == final["winner_id"]
               for event in replay["world_event_log"])
