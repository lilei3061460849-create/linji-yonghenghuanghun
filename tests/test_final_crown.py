"""【最终的冠冕】世界事件（2026-10-31 用户裁定）。

原规则：完成第7场后自动进入第8场强制死斗（封存候选/擂台循环）——**已删除**。
现规则：冠冕是地图上的争夺目标：

  · 副本开始后第 `CROWN_SPAWN_WORLD_TURNS` 个世界回合，在地图随机可通行格刷新；
  · 任何角色踩中即持有（不区分玩家/NPC）；
  · 持有者连续存活 `CROWN_HOLD_WORLD_TURNS` 个世界回合 → 自动脱离副本；
  · 持有者死亡 → 冠冕掉回地图，计时清零。

本文件只锁这两件事：冠冕的刷新/持有/计时/脱离链路，以及「战终不再触发死斗」。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.world import CROWN_HOLD_WORLD_TURNS, CROWN_SPAWN_WORLD_TURNS
from tests.setup_support import finish_initial_daowen


def _engine(tmp_path, suffix: str = "crown") -> GameEngine:
    e = GameEngine(db_path=str(tmp_path / f"{suffix}.db"),
                   sealed_candidate_path=str(tmp_path / f"{suffix}.json"),
                   rng_seed=7)
    assert e.execute_action("setup_attributes", {
        "name": "持冠者", "blood_points": 11, "speed_points": 8, "mana_points": 6})["success"]
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.relics = []          # 隔离与本文件无关的被动
    return e


def _world_engine(tmp_path, suffix: str = "crown_world", keep_npc: bool = False) -> GameEngine:
    """开局并初始化开放世界；清掉地图上的其它单位，避免遭遇打断计时。

    keep_npc=True 时保留恰好一名非玩家单位（用于验证 NPC 持冠/掉冠）。
    """
    e = _engine(tmp_path, suffix)
    assert e.execute_action("world_initialize", {"width": 7, "height": 7})["success"]
    world = e.state.world_state
    player_id = next(aid for aid, actor in world["actors"].items()
                     if actor["kind"] == "player")
    kept = ""
    for actor_id, actor in list(world["actors"].items()):
        if actor_id == player_id:
            continue
        if keep_npc and not kept:
            kept = actor_id
            continue
        actor["alive"] = False
    # 地图上随机布置的剧情事件会把玩家卡在「事件未结算」，与本文件的计时无关；摘掉。
    event_names = set(e.event_pool.events)
    world["map"]["locations"] = {
        lid: location for lid, location in world["map"]["locations"].items()
        if location.get("event_name", location.get("name", lid)) not in event_names
    }
    e.state.enemies.clear()
    return e


def _player_actor(e: GameEngine) -> dict:
    return next(actor for actor in e.state.world_state["actors"].values()
                if actor["kind"] == "player")


def _place_crown_next_to_player(e: GameEngine) -> tuple[int, int]:
    """夹具：把冠冕已经刷新好的事实摆在玩家旁边（验证「踩中即持有」）。"""
    player = _player_actor(e)
    world = e.state.world_state
    world["crown_spawn_turn"] = world["clock"]["turn"]
    world["crown_spawned"] = True
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        x, y = int(player["x"]) + dx, int(player["y"]) + dy
        terrain = world["map"]["terrain"].get(f"{x},{y}")
        if terrain is None or terrain in {"wall", "building", "void", "water_current"}:
            continue
        location_id = f"crown-fixture"
        world["map"]["locations"][location_id] = {
            "kind": "crown", "name": "最终的冠冕",
            "x": x, "y": y, "active": True,
        }
        world["crown_location_id"] = location_id
        return x, y
    raise AssertionError("玩家四周没有可放置冠冕的空地")


# ---------------------------------------------------------------------------
# 1. 刷新：到点才出现，且落点是可通行格
# ---------------------------------------------------------------------------

def test_crown_spawns_only_after_the_configured_delay(tmp_path):
    e = _world_engine(tmp_path, "spawn")
    first = e.execute_action("world_wait", {"turns": 1})["result"]
    crown = first["final_crown"]
    assert crown["spawned"] is False, "第一个世界回合不该立刻刷新"
    assert crown["spawn_turn"] == 1 + CROWN_SPAWN_WORLD_TURNS

    while True:
        waited = e.execute_action("world_wait", {"turns": 1})
        assert waited["success"], waited
        crown = waited["result"]["final_crown"]
        if crown["spawned"]:
            break
        assert waited["result"]["world_turn"] < crown["spawn_turn"], "到点后必须刷新"

    world = e.state.world_state
    location = world["map"]["locations"][world["crown_location_id"]]
    assert location["kind"] == "crown" and location["name"] == "最终的冠冕"
    assert location["active"] is True
    assert crown["position"] == [location["x"], location["y"]]
    terrain = world["map"]["terrain"][f"{location['x']},{location['y']}"]
    assert terrain not in {"wall", "building", "void", "water_current"}, \
        f"冠冕不得刷在不可通行格：{terrain}"


# ---------------------------------------------------------------------------
# 2. 持有：踩上去就拿到
# ---------------------------------------------------------------------------

def test_stepping_on_the_crown_cell_transfers_holding(tmp_path):
    e = _world_engine(tmp_path, "pickup")
    x, y = _place_crown_next_to_player(e)
    assert e.state.world_state["crown_holder_id"] == ""

    moved = e.execute_action("world_move", {"x": x, "y": y})
    assert moved["success"], moved
    world = e.state.world_state
    player_actor = _player_actor(e)
    assert world["crown_holder_id"] == player_actor["actor_id"]
    location = world["map"]["locations"][world["crown_location_id"]]
    assert location["active"] is False
    assert world["crown_taken_turn"] == world["clock"]["turn"]


# ---------------------------------------------------------------------------
# 3. 计时：持有并活满 5 个世界回合 → 自动脱离副本
# ---------------------------------------------------------------------------

def test_holding_five_world_turns_exits_the_dungeon(tmp_path):
    e = _world_engine(tmp_path, "exit")
    x, y = _place_crown_next_to_player(e)
    assert e.execute_action("world_move", {"x": x, "y": y})["success"]
    taken_turn = e.state.world_state["clock"]["turn"]

    for _ in range(CROWN_HOLD_WORLD_TURNS - 1):
        waited = e.execute_action("world_wait", {"turns": 1})
        assert waited["success"], waited
        assert waited["result"].get("dungeon_exit") is None, "未满计时不得脱离副本"
        held = waited["result"]["final_crown"]["held_world_turns"]
        assert held == e.state.world_state["clock"]["turn"] - taken_turn

    final = e.execute_action("world_wait", {"turns": 1})
    assert final["success"], final
    payload = final["result"]["dungeon_exit"]
    assert payload["reason"] == "最终的冠冕"
    assert payload["held_world_turns"] == CROWN_HOLD_WORLD_TURNS
    assert payload["world_turn"] == taken_turn + CROWN_HOLD_WORLD_TURNS

    world = e.state.world_state
    assert world["crown_exited"] is True
    assert e.state.dungeon_exit == payload
    assert e.state.phase == "pre_battle", "脱离副本后回到局外阶段"
    assert not [m for m in e.state.enemies if m.is_alive]
    assert _player_actor(e)["alive"] is False, "离场者不再是地图上的活动单位"
    # 记录写进世界事实账本
    assert any(entry["type"] == "dungeon_exited" for entry in world["event_log"])


# ---------------------------------------------------------------------------
# 4. 持有者死亡 → 冠冕掉回地图、计时清零
# ---------------------------------------------------------------------------

def test_holder_death_drops_the_crown_and_resets_the_timer(tmp_path):
    e = _world_engine(tmp_path, "drop", keep_npc=True)
    world = e.state.world_state
    x, y = _place_crown_next_to_player(e)
    npc_id = next(aid for aid, actor in world["actors"].items() if actor["kind"] != "player")
    live = e._world_from_state()
    live.actors[npc_id].x, live.actors[npc_id].y = x, y
    e._resolve_world_crown(live)            # 该回合：NPC 踩中 → 持有
    e.state.world_state = live.to_dict()
    assert e.state.world_state["crown_holder_id"] == npc_id
    e.state.world_state["actors"][npc_id]["alive"] = False   # 持有者死亡

    waited = e.execute_action("world_wait", {"turns": 1})
    assert waited["success"], waited
    world = e.state.world_state
    assert world["crown_holder_id"] == "", "持有者已死，冠冕必须掉回地图"
    assert world["crown_taken_turn"] == 0
    dropped = world["map"]["locations"][world["crown_location_id"]]
    assert dropped["kind"] == "crown" and dropped["active"] is True
    assert waited["result"].get("dungeon_exit") is None, "死亡不算「活过5个世界回合」"


# ---------------------------------------------------------------------------
# 5. 第7场战终不再触发死斗
# ---------------------------------------------------------------------------

def test_battle_end_on_battle_seven_no_longer_starts_a_duel(tmp_path):
    e = _engine(tmp_path, "seventh")
    e.state.current_battle = 7
    e.state.enemies.clear()
    e.state.phase = "in_combat"
    result = e.execute_action("battle_end", {})
    assert result["success"], result
    assert "final_crown" not in (result.get("result") or {}), \
        "战终不得再自动开启第8场死斗"
    assert e.state.in_final_duel is False
    assert e.state.player is not None, "角色不再被自动封存/替换"
