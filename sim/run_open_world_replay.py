#!/usr/bin/env python3
"""Run and export one deterministic, engine-backed open-world game.

The player is created through the canonical 25-point setup actions. World population,
map events, encounters and combat all come from the real engine; the script only
chooses deterministic player movement/actions and delegates final PvP to duel_pvp.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.api import GameEngine
from engine.replay import build_replay_payload
from engine.world import WorldState
from sim.build_learner import _resolve_pending_event
from sim.duel_pvp import run_duel_pvp
from sim.produce_real_winners import _resolve_monster_turn_with_spells

SEED = 1
MAX_WORLD_TURNS = 120  # Safety ceiling only; hitting it is an incomplete run, never a win.
OUT = ROOT / "data" / "open_world_replay"
PLAYER_NAME = "测试轮回者"


def call(engine: GameEngine, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    result = engine.execute_action(name, params or {})
    if not result.get("success"):
        raise RuntimeError(f"{name} failed: {result}")
    return result


def encounter_for_player(engine: GameEngine):
    world = WorldState.from_dict(engine.state.world_state)
    pid = engine.state.player.runtime_id
    return next((encounter for encounter in world.encounters.values()
                 if encounter.active and pid in encounter.participants), None)


def _next_location_step(world: WorldState, actor_id: str, kinds: set[str],
                        location_id: str | None = None) -> tuple[int, int] | None:
    """Return the first step toward the nearest active location of one of the requested kinds."""
    actor = world.actors.get(actor_id)
    if actor is None or not actor.alive:
        return None
    candidates = []
    for current_id, location in world.map.locations.items():
        if location_id is not None and current_id != location_id:
            continue
        if location.get("kind") not in kinds or not location.get("active", True):
            continue
        if location.get("collected") or location.get("resolved"):
            continue
        try:
            target = (int(location["x"]), int(location["y"]))
        except (KeyError, TypeError, ValueError):
            continue
        path = world.map.find_path(actor.position, target)
        if path and len(path) > 1:
            candidates.append((len(path), str(current_id), path[1]))
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1]))[2]


def decline_spells(target_option: dict[str, Any]) -> dict[str, dict[str, dict[str, bool]]]:
    options = target_option.get("spell_options") or {}
    return {timing: {spell["spell_name"]: {"use": False}
                     for spell in (options.get(timing) or [])}
            for timing in ("before", "after")}


def _save_battle_replay(engine: GameEngine, encounter_id: str, outcome: str) -> dict[str, Any]:
    replay = build_replay_payload(engine.state, battle_no=engine.state.current_battle)
    replay["world_encounter_id"] = encounter_id
    replay["outcome"] = outcome
    return replay


def fight(engine: GameEngine, encounter_id: str, timeline: list[dict[str, Any]],
          replays: list[dict[str, Any]]) -> str:
    """Resolve one active map encounter, using the existing engines for both battle types."""
    world = WorldState.from_dict(engine.state.world_state)
    encounter = world.encounters[encounter_id]
    start = call(engine, "battle_start", {"world_encounter_id": encounter_id})
    timeline.append({"type": "battle_start", "world_turn": world.clock.turn,
                     "battle": engine.state.current_battle,
                     "encounter_id": encounter_id, "final_duel": encounter.final_duel,
                     "result": start})

    if encounter.final_duel:
        # A final duel uses the existing alternating PvP driver. Calling the normal
        # player-only loop here violates duel_turn and would create a false failure.
        duel_log: list[str] = []
        from engine.ai_tactics import TacticalAI
        duel = run_duel_pvp(engine, max_rounds=60, max_steps=400, log=duel_log,
                            max_wall_seconds=30, ai_cls=TacticalAI)
        winner = duel.get("winner")
        outcome = ("player_won" if winner == "challenger" else
                   "opponent_won" if winner == "defender" else "duel_incomplete")
        replays.append(_save_battle_replay(engine, encounter_id, outcome))
        timeline.append({"type": "final_duel", "world_turn": world.clock.turn,
                         "battle": engine.state.current_battle,
                         "result": duel, "driver_log": duel_log})
        if winner not in {"challenger", "defender"}:
            return "duel_incomplete"
        ended = call(engine, "battle_end")
        timeline.append({"type": "battle_end", "world_turn": world.clock.turn,
                         "result": ended})
        # Death-legacy review is post-game DM content, not a combat action. Preserve
        # the actual world winner and record the pending proposal without inventing
        # or auto-approving a legacy in this deterministic gameplay run.
        for interrupt in engine._pending_interrupts:
            timeline.append({"type": "pending_postgame_interrupt",
                             "interrupt": interrupt.to_dict()})
        return outcome

    call(engine, "round_start")
    guard = 0
    while (engine.state.player.is_alive
           and any(enemy.is_alive and not enemy.is_departed for enemy in engine.state.enemies)):
        guard += 1
        if guard > 30:
            raise RuntimeError("combat exceeded 30 rounds")
        while (engine.state.player.is_alive
               and engine.state.player.actions_used_this_round
                   < engine._action_budget_of(engine.state.player)
               and any(enemy.is_alive and not enemy.is_departed for enemy in engine.state.enemies)):
            prepared = call(engine, "prepare_attack")["result"]
            refs = engine.combat._combat_entity_refs()
            target = next((candidate for candidate in prepared["target_options"]
                           if refs.get(candidate["ref"]) is not None
                           and refs[candidate["ref"]].is_alive), None)
            if target is None:
                break
            remaining = int(prepared["damage_budget"])
            hits = []
            for _ in range(int(prepared["hit_count"])):
                damage = min(max(0, int(engine.state.player.effective_attack_power())), remaining)
                remaining -= damage
                hits.append({"target_ref": target["ref"], "damage": damage,
                             "dodge": False, "parry": False, "blood_shadow": False,
                             "spell_choices": decline_spells(target)})
            resolved = call(engine, "resolve_attack", {"token": prepared["token"], "hits": hits})
            timeline.append({"type": "player_attack", "battle": engine.state.current_battle,
                             "round": engine.state.current_round, "result": resolved})
            if engine.state.pending_redemption:
                redemption = call(engine, "resolve_redemption", {"option": "终结"})
                timeline.append({"type": "redemption", "result": redemption})
        if not engine.state.player.is_alive or not any(
                enemy.is_alive and not enemy.is_departed for enemy in engine.state.enemies):
            break
        monster_result = _resolve_monster_turn_with_spells(engine)
        if not monster_result.get("success"):
            raise RuntimeError(f"monster phase failed: {monster_result}")
        timeline.append({"type": "monster_phase", "battle": engine.state.current_battle,
                         "round": engine.state.current_round, "result": monster_result})
        if engine.state.pending_redemption:
            redemption = call(engine, "resolve_redemption", {"option": "终结"})
            timeline.append({"type": "redemption", "result": redemption})
        if not engine.state.player.is_alive or not any(
                enemy.is_alive and not enemy.is_departed for enemy in engine.state.enemies):
            break
        call(engine, "round_end")
        call(engine, "round_start")
    if engine.state.pending_redemption:
        redemption = call(engine, "resolve_redemption", {"option": "终结"})
        timeline.append({"type": "redemption", "result": redemption})

    outcome = ("player_dead" if not engine.state.player.is_alive else
               "player_won" if not any(enemy.is_alive and not enemy.is_departed
                                       for enemy in engine.state.enemies) else "stopped")
    replays.append(_save_battle_replay(engine, encounter_id, outcome))
    timeline.append({"type": "battle_snapshot", "world_turn": world.clock.turn,
                     "battle": engine.state.current_battle, "outcome": outcome,
                     "event_count": replays[-1]["event_count"],
                     "round_count": replays[-1]["round_count"]})
    if outcome == "player_won":
        ended = call(engine, "battle_end")
        timeline.append({"type": "battle_end", "world_turn": world.clock.turn,
                         "result": ended})
    return outcome


def _setup_player(engine: GameEngine, *, blood_points: int = 13,
                  speed_points: int = 6, mana_points: int = 6) -> dict[str, Any]:
    """Use public setup actions and validate the supplied 25-point attribute split."""
    setup = call(engine, "setup_attributes", {
        "name": PLAYER_NAME, "blood_points": blood_points,
        "speed_points": speed_points, "mana_points": mana_points,
    })
    relic_choices = setup["result"]["relic_choices"]
    relic_choice = relic_choices[0]
    relic = call(engine, "choose_discovered_relic", {"relic_name": relic_choice})
    daowen_choice = engine.state.pending_initial_daowen_choices[0]
    daowen = call(engine, "setup_choose_initial_daowen", {"daowen_name": daowen_choice})
    resonance = call(engine, "setup_choose_resonance", {"resonance_type": "转换"})
    region = call(engine, "setup_choose_region", {"region": "罪孽都市"})
    return {"attributes": {"blood_points": blood_points, "speed_points": speed_points,
                            "mana_points": mana_points,
                            "total": blood_points + speed_points + mana_points},
            "relic_candidates": relic_choices, "relic_choice": relic_choice,
            "relic_result": relic, "daowen_candidates": relic["result"]["daowen_choices"],
            "daowen_choice": daowen_choice, "daowen_result": daowen,
            "resonance_result": resonance, "region_result": region}


def _next_player_step(engine: GameEngine, world: WorldState) -> tuple[int, int] | None:
    """Deterministically approach the nearest reachable rival (or a hostile if needed)."""
    player_id = engine.state.player.runtime_id
    player_actor = world.actors[player_id]
    entity_by_id = {entity.runtime_id: entity for entity in engine.state.enemies
                    if entity.is_alive and not entity.is_departed}

    if world.final_duel_search:
        rival_id = next((actor_id for actor_id in world.final_duel_finalists
                         if actor_id != player_id and actor_id in world.actors
                         and world.actors[actor_id].alive), None)
        destination = world.actors[rival_id].position if rival_id else (
            world.map.width // 2, world.map.height // 2)
        path = world.map.find_path(player_actor.position, destination)
        if path and len(path) > 1:
            return path[1]
        # Both finalists independently converge on the center, as the world rule specifies.
        destination = (world.map.width // 2, world.map.height // 2)
        path = world.map.find_path(player_actor.position, destination)
        return path[1] if path and len(path) > 1 else None

    rival_candidates = []
    for actor_id, entity in entity_by_id.items():
        if entity.entity_type != "轮回者":
            continue
        actor = world.actors.get(actor_id)
        if actor is None or not actor.alive:
            continue
        path = world.map.find_path(player_actor.position, actor.position)
        if path:
            rival_candidates.append((len(path), entity.name, path))
    if rival_candidates:
        return min(rival_candidates, key=lambda item: (item[0], item[1]))[2][1]

    hostile_paths = []
    for actor_id, entity in entity_by_id.items():
        actor = world.actors.get(actor_id)
        if actor is None or not actor.alive:
            continue
        path = world.map.find_path(player_actor.position, actor.position)
        if path:
            hostile_paths.append((len(path), entity.name, path))
    if hostile_paths:
        return min(hostile_paths, key=lambda item: (item[0], item[1]))[2][1]
    return None


def run_playthrough(seed: int = SEED, max_world_turns: int = MAX_WORLD_TURNS,
                    *, blood_points: int = 9, speed_points: int = 8,
                    mana_points: int = 8) -> dict[str, Any]:
    """Return the full machine-readable game record; no output files are written here."""
    with tempfile.TemporaryDirectory(prefix="open-world-replay-") as temp_dir:
        engine = GameEngine(
            db_path=os.path.join(temp_dir, "rulings.db"),
            save_dir=os.path.join(temp_dir, "saves"),
            sealed_candidate_path=os.path.join(temp_dir, "sealed.json"),
            death_book_path=os.path.join(temp_dir, "death_book.md"),
            rng_seed=seed,
        )
        setup = _setup_player(engine, blood_points=blood_points,
                              speed_points=speed_points, mana_points=mana_points)
        init = call(engine, "world_initialize", {"width": 21, "height": 21})
        world = WorldState.from_dict(engine.state.world_state)
        timeline: list[dict[str, Any]] = [{
            "type": "world_initialized", "seed": seed, "setup": setup,
            "result": init, "player": engine.state.player.name,
        }]
        replays: list[dict[str, Any]] = []
        stop_reason = ""

        # 让完整局实际覆盖局外世界功能，而不只在地图上追敌：
        # 共鸣把真实遗物投放到地图，雇佣生成持续存在的响应信号。
        resonance_action = call(engine, "pre_battle_action", {
            "sub_action": "共鸣", "tier": 1,
        })
        timeline.append({"type": "world_feature_action",
                         "world_turn": WorldState.from_dict(engine.state.world_state).clock.turn,
                         "action": "共鸣", "result": resonance_action})
        hiring_action = call(engine, "pre_battle_action", {
            "sub_action": "雇佣", "name": "回放雇员",
            "blood_alloc": 20, "atk_bundles": 0,
        })
        timeline.append({"type": "world_feature_action",
                         "world_turn": WorldState.from_dict(engine.state.world_state).clock.turn,
                         "action": "雇佣", "result": hiring_action})

        while engine.state.player.is_alive:
            world = WorldState.from_dict(engine.state.world_state)
            if world.final_duel_complete:
                stop_reason = "final duel completed"
                break
            if engine.event_pool.current is not None:
                event_name = engine.event_pool.current
                event_result = _resolve_pending_event(engine)
                timeline.append({"type": "map_event_resolution",
                                 "world_turn": world.clock.turn,
                                 "event": event_name, "result": event_result})
                if not event_result.get("success"):
                    stop_reason = f"map event could not resolve: {event_name}"
                    break
                if event_result.get("player_dead"):
                    stop_reason = f"player died during map event: {event_name}"
                    break
                continue
            if world.clock.turn >= max_world_turns:
                stop_reason = f"safety ceiling reached at world turn {max_world_turns}; run incomplete"
                break
            # 雇佣信号抵达后，完成其真实道纹选择，避免员工停在半结算状态。
            if engine.state.pending_daowen_choices:
                employee_name = sorted(engine.state.pending_daowen_choices)[0]
                choices = engine.state.pending_daowen_choices[employee_name]
                if choices:
                    chosen = call(engine, "choose_hired_daowen", {
                        "name": employee_name, "daowen": choices[0],
                    })
                    timeline.append({"type": "world_feature_action",
                                     "world_turn": world.clock.turn,
                                     "action": "choose_hired_daowen",
                                     "result": chosen})
                    continue

            encounter = encounter_for_player(engine)
            if encounter is not None:
                outcome = fight(engine, encounter.encounter_id, timeline, replays)
                if outcome == "player_dead":
                    stop_reason = "player died in map encounter"
                    break
                if outcome == "duel_incomplete":
                    stop_reason = "final duel driver did not resolve a winner"
                    break
                if outcome == "stopped":
                    stop_reason = "map combat did not reach a legal battle end"
                    break
                continue
            # 固定种子单局必须真实结算资源、共鸣遗物拾取和地图事件。
            # 最终死斗一旦启动，仍由上面的正式规则优先处理，不为审计而推迟终局。
            move_entries = [item for item in timeline if item.get("type") == "world_move"]
            resource_collected = any(
                ((item.get("result") or {}).get("result") or {}).get("resource_collected")
                for item in move_entries
            )
            relic_collected = any(
                ((item.get("result") or {}).get("result") or {}).get("relic_collected")
                for item in move_entries
            )
            event_resolved = any(
                item.get("type") == "map_event_resolution"
                and (item.get("result") or {}).get("success")
                for item in timeline
            )
            resonance_result = ((resonance_action.get("result") or {}).get("result") or {})
            resonance_location_id = resonance_result.get("location_id")
            destination = None
            if not resource_collected:
                destination = _next_location_step(world, engine.state.player.runtime_id,
                                                  {"resource"})
            if destination is None and not relic_collected and resonance_location_id:
                destination = _next_location_step(
                    world, engine.state.player.runtime_id, {"relic"},
                    location_id=str(resonance_location_id))
            if destination is None and not event_resolved:
                destination = _next_location_step(world, engine.state.player.runtime_id,
                                                  {"event"})
            if destination is None:
                destination = _next_player_step(engine, world)
            if destination is None:
                stop_reason = "no reachable living rival or hostile unit"
                break
            moved = call(engine, "world_move", {
                "actor_id": engine.state.player.runtime_id,
                "x": destination[0], "y": destination[1],
            })
            move_result = moved["result"]
            timeline.append({"type": "world_move", "world_turn": move_result.get("world_turn"),
                             "destination": list(destination), "result": moved})

        world = WorldState.from_dict(engine.state.world_state)
        if world.final_duel_complete:
            stop_reason = "final duel completed"
        elif not engine.state.player.is_alive:
            stop_reason = stop_reason or "player died"
        move_entries = [item for item in timeline if item.get("type") == "world_move"]
        feature_coverage = {
            "resource_collected": any(
                ((item.get("result") or {}).get("result") or {}).get("resource_collected")
                for item in move_entries
            ) or any(location.get("kind") == "resource" and location.get("collected")
                     for location in world.map.locations.values()),
            "resonance_relic_dropped": bool(
                ((resonance_action.get("result") or {}).get("result") or {}).get("location_id")),
            "resonance_relic_picked_up": any(
                ((item.get("result") or {}).get("result") or {}).get("relic_collected")
                for item in move_entries
            ),
            "hiring_signal_completed": any(
                signal.get("status") == "completed" for signal in world.hiring_signals),
            "map_event_resolved": any(
                item.get("type") == "map_event_resolution"
                and (item.get("result") or {}).get("success")
                for item in timeline),
            "npc_skirmish_resolved": any(
                event.get("type") == "npc_skirmish_resolved" for event in world.event_log),
        }
        final = {
            "player": {"name": engine.state.player.name,
                       "runtime_id": engine.state.player.runtime_id,
                       "alive": engine.state.player.is_alive,
                       "hp": engine.state.player.current_hp,
                       "blood_limit": engine.state.player.blood_limit},
            "winner_id": world.final_duel_winner if world.final_duel_complete else None,
            "winner_name": (world.actors[world.final_duel_winner].name
                            if world.final_duel_complete and world.final_duel_winner in world.actors
                            else None),
            "game_complete": world.final_duel_complete,
            "world_turn": world.clock.turn,
            "stop_reason": stop_reason or "run incomplete",
            "living_reincarnators_at_end": [
                {"name": actor.name, "actor_id": actor.actor_id,
                 "entity_alive": True, "map_present": actor.alive,
                 "position": list(actor.position),
                 "winner_departed": (world.final_duel_complete
                                     and actor.actor_id == world.final_duel_winner)}
                for actor in world.actors.values()
                if actor.kind in {"player", "enemy", "friend", "employee", "temp_friend"}
                and any(entity.runtime_id == actor.actor_id and entity.entity_type == "轮回者"
                        and entity.is_alive and not entity.is_departed
                        for entity in ([engine.state.player] + engine.state.enemies
                                       + engine.state.friends + engine.state.employees
                                       + engine.state.temp_friends))],
            "battle_count": len(replays),
            "combat_event_count": sum(replay["event_count"] for replay in replays),
        }
        return {
            "title": "开放世界完整单局回放",
            "schema_version": 1,
            "seed": seed,
            "run_config": {"region": "罪孽都市", "map_size": [21, 21],
                            "max_world_turns": max_world_turns,
                            "max_world_turns_is_safety_only": True},
            "initial_population": {
                "monsters": init["result"]["spawned_monsters"],
                "other_reincarnators": init["result"]["spawned_reincarnators"],
                "special_locations": init["result"]["event_locations"],
            },
            "map": {"width": world.map.width, "height": world.map.height,
                    "final_ascii": map_text(world),
                    "locations": [{"location_id": location_id, **dict(location)}
                                  for location_id, location in world.map.locations.items()]},
            "final_state": final,
            "feature_coverage": feature_coverage,
            "timeline": _chronological_timeline(world.event_log, timeline, replays),
            "driver_timeline": timeline,
            "world_event_log": world.event_log,
            "battles": replays,
        }


def _chronological_timeline(world_log: list[dict[str, Any]],
                           actions: list[dict[str, Any]],
                           battles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge world facts, player decisions and actual combat events by world time."""
    # Battle numbers are the engine's stable identifiers. The battle_start action is
    # the authoritative bridge from a combat event stream back to a world turn.
    battle_turns = {
        int(item.get("battle", 0)): int(item.get("world_turn", 0))
        for item in actions if item.get("type") == "battle_start"
    }
    merged: list[tuple[int, int, int, dict[str, Any]]] = []
    for event in world_log:
        turn = int(event.get("world_turn", 0))
        event_id = int(event.get("event_id", 0))
        merged.append((turn, 0, event_id, {
            "source": "world_fact", "world_turn": turn,
            "event_id": event_id, "event_type": event.get("type"),
            "actor_id": event.get("actor_id"), "details": event.get("details", {}),
        }))

    ignored_action_types = {"player_attack", "monster_phase", "redemption"}
    for index, action in enumerate(actions):
        action_type = action.get("type", "")
        if action_type in ignored_action_types:
            continue
        turn = int(action.get("world_turn", 0))
        phase = 1
        if action_type in {"battle_snapshot", "battle_end", "final_duel",
                           "pending_postgame_interrupt"}:
            phase = 3
        elif action_type == "battle_start":
            phase = 1
        merged.append((turn, phase, index, {
            "source": "world_action", "world_turn": turn,
            "action_type": action_type, "action": action,
        }))

    for battle in battles:
        battle_no = int(battle.get("battle_no", 0))
        turn = battle_turns.get(battle_no, 0)
        for event in battle.get("events", []):
            merged.append((turn, 2, int(event.get("sequence", 0)), {
                "source": "combat_event", "world_turn": turn,
                "battle_no": battle_no,
                "battle_round": int(event.get("round_no", 0)),
                "combat_sequence": int(event.get("sequence", 0)),
                "event": event,
            }))

    merged.sort(key=lambda item: (item[0], item[1], item[2]))
    return [{"sequence": index, **item[3]} for index, item in enumerate(merged)]


def map_text(world: WorldState) -> str:
    chars = {"floor": ".", "ruin": ":", "wall": "#", "building": "B", "void": " "}
    occupied = {}
    for actor in world.actors.values():
        if actor.alive:
            occupied[actor.position] = "P" if actor.kind == "player" else (
                "M" if actor.faction == "monster" else "$" if actor.faction == "neutral" else "R")
    return "\n".join("".join(occupied.get((x, y), chars.get(world.map.terrain_at((x, y)), "?"))
                              for x in range(world.map.width))
                      for y in range(world.map.height))


def write_replay(payload: dict[str, Any], out_dir: Path = OUT) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "open_world_full_replay.json"
    markdown_path = out_dir / "open_world_full_replay.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
                         encoding="utf-8")
    summary = payload["final_state"]
    rounds = [replay["round_count"] for replay in payload["battles"]]
    lines = ["# 开放世界完整单局回放", "",
             f"- 固定种子：{payload['seed']}",
             f"- 副本/地图：{payload['run_config']['region']} / "
             f"{payload['map']['width']}×{payload['map']['height']}",
             f"- 初始人口：怪物 {payload['initial_population']['monsters']}，"
             f"其他轮回者 {payload['initial_population']['other_reincarnators']}",
             f"- 世界回合：{summary['world_turn']}",
             f"- 战斗场数：{summary['battle_count']}",
             f"- 各战斗回合数：{rounds}",
             f"- 战斗事件数：{summary['combat_event_count']}",
             f"- 游戏完成：{summary['game_complete']}",
             f"- 胜者：{summary['winner_name']}",
             f"- 结束原因：{summary['stop_reason']}",
             f"- 玩家最终状态：{summary['player']}", "", "## 世界功能实际结算",
             *[f"- {name}：{'通过' if passed else '未覆盖'}"
               for name, passed in (payload.get("feature_coverage") or {}).items()],
             "", "## 最终地图",
             "P=玩家位置（若仍在场），R=其他轮回者，M=怪物，$=中立单位；#=墙，:=废墟。", "",
             *("    " + line.rstrip() if line.strip() else ""
               for line in payload["map"]["final_ascii"].splitlines()),
             "", "## 回放说明",
             "JSON timeline 将世界事实账本、行动和引擎实际战斗事件按世界回合合并排序；driver_timeline 保留驱动器提交。",
             "每场战斗的回合数来自实际战斗事件，且与世界回合分开统计；世界事实未由叙事补造。", ""]
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path


def validate_complete_playthrough(payload: dict[str, Any]) -> None:
    """Reject a replay that merely ran for a while instead of reaching a legal world ending."""
    final = payload.get("final_state") or {}
    population = payload.get("initial_population") or {}
    battles = payload.get("battles") or []
    if final.get("game_complete") is not True:
        raise RuntimeError(f"open-world run is incomplete: {final.get('stop_reason', 'unknown stop')}")
    if final.get("stop_reason") != "final duel completed":
        raise RuntimeError(f"unexpected terminal reason: {final.get('stop_reason')}")
    if int(population.get("monsters", 0)) < 1:
        raise RuntimeError("open-world run spawned no monsters")
    if int(population.get("other_reincarnators", 0)) < 2:
        raise RuntimeError("open-world run spawned fewer than two other reincarnators")
    if int(population.get("special_locations", 0)) < 1:
        raise RuntimeError("open-world run spawned no special locations")
    if not battles or int(final.get("battle_count", 0)) != len(battles):
        raise RuntimeError("battle count does not match the exported battle replays")
    if int(final.get("combat_event_count", 0)) != sum(
            int(battle.get("event_count", 0)) for battle in battles):
        raise RuntimeError("combat event count does not match the exported battle replays")
    if int(battles[-1].get("round_count", 0)) < 1 or int(battles[-1].get("event_count", 0)) < 1:
        raise RuntimeError("final duel has no actual combat rounds/events")
    duel_entries = [entry for entry in payload.get("driver_timeline", [])
                    if entry.get("type") == "final_duel"]
    if len(duel_entries) != 1 or duel_entries[0].get("result", {}).get("winner") not in {
            "challenger", "defender"}:
        raise RuntimeError("final duel did not produce exactly one recorded winner")
    if len(final.get("living_reincarnators_at_end", [])) != 1:
        raise RuntimeError("terminal world state does not contain exactly one living reincarnator")
    coverage = payload.get("feature_coverage") or {}
    required_features = ("resource_collected", "resonance_relic_dropped",
                         "resonance_relic_picked_up", "hiring_signal_completed",
                         "map_event_resolved")
    missing = [feature for feature in required_features if coverage.get(feature) is not True]
    if missing:
        raise RuntimeError(f"open-world feature audit incomplete: {', '.join(missing)}")


def main() -> None:
    payload = run_playthrough()
    print(json.dumps({
        "prevalidation_final_state": payload.get("final_state"),
        "feature_coverage": payload.get("feature_coverage"),
        "driver_event_types": sorted({item.get("type", "") for item in payload.get("driver_timeline", [])}),
    }, ensure_ascii=False, indent=2))
    validate_complete_playthrough(payload)
    json_path, markdown_path = write_replay(payload)
    print(json.dumps({
        "final_state": payload["final_state"],
        "initial_population": payload["initial_population"],
        "world_events": len(payload["world_event_log"]),
        "timeline_entries": len(payload["timeline"]),
        "battle_round_counts": [battle["round_count"] for battle in payload["battles"]],
        "battle_event_counts": [battle["event_count"] for battle in payload["battles"]],
        "artifacts": [str(json_path.relative_to(ROOT)), str(markdown_path.relative_to(ROOT))],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
