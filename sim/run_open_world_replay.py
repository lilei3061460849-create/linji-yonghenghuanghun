#!/usr/bin/env python3
"""Deterministic open-world playthrough through the real world and combat APIs."""
import json, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine.api import GameEngine
from engine.models import Entity
from engine.world import WorldState
from engine.replay import build_replay_payload
from sim.produce_real_winners import _resolve_monster_turn_with_spells
from sim.build_learner import _resolve_pending_event

SEED = 20261019
MAX_WORLD_TURNS = 120
OUT = ROOT / "artifacts" / "open_world_replay"


def call(engine, name, params=None):
    result = engine.execute_action(name, params or {})
    if not result.get("success"):
        raise RuntimeError(f"{name} failed: {result}")
    return result


def encounter_for_player(engine):
    world = WorldState.from_dict(engine.state.world_state)
    pid = engine.state.player.runtime_id
    return next((e for e in world.encounters.values()
                 if e.active and pid in e.participants), None)


def decline_spells(target_option):
    options = target_option.get("spell_options") or {}
    return {timing: {s["spell_name"]: {"use": False}
                     for s in (options.get(timing) or [])}
            for timing in ("before", "after")}


def fight(engine, encounter_id, timeline, replays):
    result = call(engine, "battle_start", {"world_encounter_id": encounter_id})
    timeline.append({"type": "battle_start", "result": result})
    call(engine, "round_start")
    guard = 0
    while engine.state.player.is_alive and any(e.is_alive and not e.is_departed
                                                for e in engine.state.enemies):
        guard += 1
        if guard > 30:
            raise RuntimeError("combat exceeded 30 rounds")
        while (engine.state.player.is_alive
               and engine.state.player.actions_used_this_round < engine._action_budget_of(engine.state.player)
               and any(e.is_alive and not e.is_departed for e in engine.state.enemies)):
            prepared = call(engine, "prepare_attack")["result"]
            refs = engine.combat._combat_entity_refs()
            target = next((t for t in prepared["target_options"]
                           if refs.get(t["ref"]) is not None and refs[t["ref"]].is_alive), None)
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
        if not engine.state.player.is_alive or not any(e.is_alive and not e.is_departed
                                                       for e in engine.state.enemies):
            break
        monster_result = _resolve_monster_turn_with_spells(engine)
        if not monster_result.get("success"):
            raise RuntimeError(f"monster phase failed: {monster_result}")
        timeline.append({"type": "monster_phase", "battle": engine.state.current_battle,
                         "round": engine.state.current_round, "result": monster_result})
        if not engine.state.player.is_alive or not any(e.is_alive and not e.is_departed
                                                       for e in engine.state.enemies):
            break
        call(engine, "round_end")
        call(engine, "round_start")
    replay = build_replay_payload(engine.state, battle_no=engine.state.current_battle)
    replay["world_encounter_id"] = encounter_id
    replay["outcome"] = ("player_dead" if not engine.state.player.is_alive else
                         "player_won" if not any(e.is_alive and not e.is_departed
                                                for e in engine.state.enemies) else "stopped")
    replays.append(replay)
    timeline.append({"type": "battle_snapshot", "battle": engine.state.current_battle,
                     "outcome": replay["outcome"], "event_count": replay["event_count"]})
    if replay["outcome"] == "player_won":
        timeline.append({"type": "battle_end", "result": call(engine, "battle_end")})


def map_text(world):
    chars = {"floor": ".", "ruin": ":", "wall": "#", "building": "B", "void": " "}
    occupied = {}
    for a in world.actors.values():
        if a.alive:
            occupied[a.position] = "P" if a.kind == "player" else (
                "M" if a.faction == "monster" else "$")
    return "\n".join("".join(occupied.get((x, y), chars.get(world.map.terrain_at((x, y)), "?"))
                              for x in range(world.map.width))
                      for y in range(world.map.height))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="open-world-replay-") as tmp:
        e = GameEngine(db_path=os.path.join(tmp, "rulings.db"),
                       save_dir=os.path.join(tmp, "saves"),
                       sealed_candidate_path=os.path.join(tmp, "sealed.json"),
                       death_book_path=os.path.join(tmp, "death_book.md"), rng_seed=SEED)
        e.state.current_region = "罪孽都市"
        e.state.phase = "pre_battle"
        e.state.player = Entity(name="测试轮回者", entity_type="轮回者",
                                blood_limit=80, current_hp=80, mana_limit=0, current_mana=0,
                                speed_limit=1, current_speed=1, attack_count=1, attack_power=10)
        for i, (hp, power) in enumerate(((24, 4), (28, 5), (22, 4)), 1):
            e.state.enemies.append(Entity(name=f"地图怪物·{i}", entity_type="怪物",
                                          blood_limit=hp, current_hp=hp, mana_limit=0, current_mana=0,
                                          speed_limit=1, current_speed=1, attack_count=1,
                                          attack_power=power))
        init = call(e, "world_initialize", {"width": 11, "height": 11})
        timeline = [{"type": "world_initialized", "seed": SEED, "result": init,
                     "player": e.state.player.name}]
        replays = []
        stop = "all map enemies defeated"
        while e.state.player.is_alive:
            world = WorldState.from_dict(e.state.world_state)
            if e.event_pool.current is not None:
                event_result = _resolve_pending_event(e)
                timeline.append({"type": "map_event_resolution", "event": e.event_pool.current,
                                 "result": event_result})
                if not event_result.get("success"):
                    raise RuntimeError(f"existing event resolution failed: {event_result}")
                if event_result.get("player_dead"):
                    stop = "player died during map event"
                    break
                continue
            if world.clock.turn >= MAX_WORLD_TURNS:
                stop = f"world-turn limit {MAX_WORLD_TURNS}"
                break
            if not any(x.is_alive and not x.is_departed for x in e.state.enemies):
                break
            encounter = encounter_for_player(e)
            if encounter:
                fight(e, encounter.encounter_id, timeline, replays)
                if not e.state.player.is_alive:
                    stop = "player died in combat"
                    break
                continue
            player_actor = world.actors[e.state.player.runtime_id]
            entity_by_id = {x.runtime_id: x for x in e.state.enemies}
            candidates = []
            for aid, actor in world.actors.items():
                entity = entity_by_id.get(aid)
                if entity and entity.is_alive and not entity.is_departed and actor.alive:
                    path = world.map.find_path(player_actor.position, actor.position)
                    if path:
                        candidates.append((len(path), entity.name, path))
            if not candidates:
                stop = "no reachable living map enemy"
                break
            _, target_name, path = min(candidates, key=lambda x: (x[0], x[1]))
            if len(path) < 2:
                if encounter_for_player(e):
                    continue
                raise RuntimeError(f"{target_name} shares player's cell without an encounter")
            x, y = path[1]
            moved = call(e, "world_move", {"actor_id": e.state.player.runtime_id, "x": x, "y": y})
            timeline.append({"type": "world_move", "world_turn": moved["result"].get("world_turn"),
                             "target": target_name, "destination": [x, y], "result": moved})
        world = WorldState.from_dict(e.state.world_state)
        final = {"player": {"name": e.state.player.name, "alive": e.state.player.is_alive,
                            "hp": e.state.player.current_hp, "blood_limit": e.state.player.blood_limit},
                 "enemies": [{"name": x.name, "alive": x.is_alive, "hp": x.current_hp,
                              "blood_limit": x.blood_limit} for x in e.state.enemies],
                 "world_turn": world.clock.turn, "stop_reason": stop, "battles": len(replays),
                 "combat_event_count": sum(r["event_count"] for r in replays)}
        payload = {"title": "新版开放地图单局完整录像", "seed": SEED,
                   "map": {"width": world.map.width, "height": world.map.height,
                           "final_ascii": map_text(world)},
                   "final_state": final, "timeline": timeline,
                   "world_event_log": world.event_log, "battles": replays}
        (OUT / "open_world_full_replay.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        md = ["# 新版开放地图单局完整录像", "", f"- 固定种子：{SEED}",
              f"- 世界回合：{world.clock.turn}", f"- 战斗场数：{len(replays)}",
              f"- 战斗事件数：{final['combat_event_count']}", f"- 结束原因：{stop}",
              f"- 玩家最终状态：{final['player']}", "", "## 最终地图",
              "P=玩家，M=怪物，$=其他单位；#=墙，:=废墟，.=地面。", "",
              *("    " + line for line in map_text(world).splitlines()),
              "", "## 完整录像数据", "JSON 文件包含逐步行动、所有世界事件与每场战斗事件。"]
        (OUT / "open_world_full_replay.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        print(json.dumps({"final_state": final, "world_events": len(world.event_log),
                          "timeline_entries": len(timeline),
                          "battle_event_counts": [r["event_count"] for r in replays],
                          "artifacts": [str((OUT / "open_world_full_replay.json").relative_to(ROOT)),
                                        str((OUT / "open_world_full_replay.md").relative_to(ROOT))]},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
