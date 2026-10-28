"""地图消耗品密集投放、独占持有与持久化。"""
import pickle
from engine.api import GameEngine
from engine.models import Entity
from engine.world import WorldState, WorldActorState, WorldClock
from engine.world_consumables import scatter_consumables, collect_consumables
from tests.test_open_world import _open_map


def engine(tmp_path, seed=25):
    e = GameEngine(db_path=str(tmp_path / f"consumables-{seed}.db"), save_dir=str(tmp_path),
                   rng_seed=seed, autosave=False)
    e.state.player = Entity("P", "轮回者", blood_limit=100, current_hp=100)
    e.state.phase = "pre_battle"
    return e


def test_dense_random_distribution_repeat_initialize_and_saved_map(tmp_path):
    e = engine(tmp_path)
    assert e.execute_action("world_initialize", {"width": 21, "height": 21})["success"]
    world = WorldState.from_dict(e.state.world_state)
    items = [loc for loc in world.map.locations.values() if loc.get("kind") == "consumable"]
    assert len(items) > 30
    assert len({loc["name"] for loc in items}) > 5
    assert len({(loc["x"], loc["y"]) for loc in world.map.locations.values()}) == len(world.map.locations)
    assert all(world.map.is_passable((loc["x"], loc["y"])) for loc in items)
    assert all(all(actor.position != (loc["x"], loc["y"]) for actor in world.actors.values()) for loc in items)
    assert all(loc["consumable"]["current_uses"] == loc["consumable"]["max_uses"] > 0 for loc in items)
    before = pickle.loads(pickle.dumps(e.state.world_state))
    assert e.execute_action("world_initialize", {})["success"]
    assert e.state.world_state == before
    assert WorldState.from_dict(before).to_dict() == before
    assert any("world_consumable_type" in str(event) for event in e.dice.get_history())


def test_scatter_density_replay_and_no_respawn(tmp_path):
    e = engine(tmp_path)
    world = WorldState(clock=WorldClock(), map=_open_map(21, 21))
    cells = [(x, y) for y in range(21) for x in range(21)]
    assert scatter_consumables(world, e.dice, e._world_consumable_specs(), cells) == 89
    locations = world.map.to_dict()
    assert scatter_consumables(world, e.dice, e._world_consumable_specs(), cells) == 0
    assert world.map.to_dict() == locations
    e2 = engine(tmp_path, seed=26)
    world2 = WorldState(clock=WorldClock(), map=_open_map(21, 21))
    scatter_consumables(world2, e2.dice, e2._world_consumable_specs(), cells)
    assert world2.map.to_dict() != locations


def pickup_world(e):
    world = WorldState(clock=WorldClock(), map=_open_map())
    p = e.state.player
    npc = Entity("搜寻者", "轮回者", blood_limit=20, current_hp=20)
    e.state.enemies = [npc]
    for ent, pos in ((p, (1, 1)), (npc, (2, 1))):
        world.add_actor(WorldActorState(ent.runtime_id, ent.name, "player" if ent is p else "轮回者", *pos,
                                       mode="manual", faction=ent.runtime_id))
    scatter_consumables(world, e.dice, e._world_consumable_specs(), [(3, 1)])
    return world, npc


def test_player_and_npc_exclusive_pickup_and_snapshot(tmp_path):
    e = engine(tmp_path)
    world, npc = pickup_world(e)
    world.actors[npc.runtime_id].x = 3
    picked = collect_consumables(world, e.state)
    assert len(picked) == 1 and not picked[0]["player_visible"]
    assert len(npc.consumables) == 1 and e.state.consumables == []
    assert collect_consumables(world, e.state) == []
    assert e._deserialize_entity_full(e._serialize_entity_full(npc)).consumables == npc.consumables
    restored = pickle.loads(pickle.dumps(e.state))
    assert restored.enemies[0].consumables == npc.consumables
    world.actors[e.state.player.runtime_id].x = 3
    assert collect_consumables(WorldState.from_dict(world.to_dict()), restored) == []


def test_move_collects_not_triggers_event_and_fog_hides_unseen_items(tmp_path):
    e = engine(tmp_path)
    world, npc = pickup_world(e)
    # Keep the enemy far away so this tests pickup rather than encounter handoff.
    world.actors[npc.runtime_id].x, world.actors[npc.runtime_id].y = 6, 6
    world.actors[e.state.player.runtime_id].x = 2
    e.state.world_state = world.to_dict()
    result = e.execute_action("world_move", {"actor_id": e.state.player.runtime_id, "x": 3, "y": 1})
    assert result["success"], result
    assert len(e.state.consumables) == 1
    assert "location_event" not in result["result"]
    assert e.event_pool.current is None
    assert not e.state.world_state["map"]["locations"]["consumable_1"]["active"]


def test_visible_items_known_but_npc_pickup_never_exposes_hidden_locations(tmp_path):
    from engine.world_consumables import discover_consumables
    e = engine(tmp_path)
    world, npc = pickup_world(e)
    p_actor = world.actors[e.state.player.runtime_id]
    p_actor.vision_range = 1
    discover_consumables(world)
    e.state.world_state = world.to_dict()
    assert "consumable_1" not in e.get_state()["state"]["world_state"]["map"]["locations"]
    # A hidden NPC finds and collects the item. Neither location nor pickup event leaks.
    world.actors[npc.runtime_id].x = 3
    collect_consumables(world, e.state)
    e.state.world_state = world.to_dict()
    projected = e.get_state()["state"]["world_state"]
    assert "consumable_1" not in projected["map"]["locations"]
    assert not any(event["type"] == "consumable_collected" for event in projected["event_log"])


def test_items_in_real_vision_are_exposed_without_stepping_onto_them(tmp_path):
    from engine.world_consumables import discover_consumables
    e = engine(tmp_path)
    world, _ = pickup_world(e)
    world.actors[e.state.player.runtime_id].vision_range = 3
    discover_consumables(world)
    e.state.world_state = world.to_dict()
    assert "consumable_1" in e.get_state()["state"]["world_state"]["map"]["locations"]
    assert not e.state.consumables


def test_boundary_and_dead_actor_cannot_collect(tmp_path):
    e = engine(tmp_path)
    world, npc = pickup_world(e)
    world.actors[npc.runtime_id].x = 3
    npc.is_alive = False
    assert collect_consumables(world, e.state) == []
    world.map.locations["consumable_1"]["active"] = False
    npc.is_alive = True
    assert collect_consumables(world, e.state) == []
