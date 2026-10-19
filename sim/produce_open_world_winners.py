#!/usr/bin/env python3
"""Generate auditable first-tier winners from full open-world runs.

Unlike the legacy seven-battle TacticalAI loop, this driver runs the canonical map,
real encounters, existing combat engine, and the last-two-survivors final duel.
Only the actual player-side final-duel winner is written as a sample.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sim.run_open_world_replay import run_playthrough, validate_complete_playthrough

REGIONS = ["罪孽都市", "扭曲都市", "龙心谷"]
WINNER_DIR = Path(os.environ.get("REAL_WINNER_DIR", ROOT / "data" / "real_winners"))


def validate_open_world_winner_snapshot(snapshot: dict[str, Any]) -> None:
    """Reject candidate wrappers, defeated challengers, and untraceable snapshots."""
    if not isinstance(snapshot, dict) or "candidates" in snapshot:
        raise ValueError("winner sample must be a top-level character snapshot, not a candidates wrapper")
    player = snapshot.get("player")
    origin = snapshot.get("origin") or {}
    if not isinstance(player, dict) or player.get("entity_type") != "轮回者":
        raise ValueError("winner sample is missing its top-level reincarnator")
    if player.get("is_alive") is not True:
        raise ValueError("winner sample does not contain a living winner")
    if len(player.get("dao_wen") or {}) < 2:
        raise ValueError("winner sample lacks learned DaoWen progression")
    if len(player.get("spells") or []) < 1:
        raise ValueError("winner sample lacks a learned spell")
    if not isinstance(origin.get("seed"), int) or not origin.get("region"):
        raise ValueError("winner sample is missing seed/region provenance")
    if origin.get("generator") != "sim/produce_open_world_winners.py":
        raise ValueError("winner sample has no recognized open-world generator provenance")
    if origin.get("final_duel_won") is not True:
        raise ValueError("winner sample was not won by the player in the final duel")
    if origin.get("stop_reason") != "final duel completed":
        raise ValueError("winner sample did not reach a legal final-duel terminal")
    if int(origin.get("battle_count", 0)) < 2 or int(origin.get("ordinary_battle_count", 0)) < 1:
        raise ValueError("winner sample needs a real map battle plus the final duel")
    if int(origin.get("world_turn", 0)) < 1 or int(origin.get("combat_event_count", 0)) < 1:
        raise ValueError("winner sample has no auditable world/combat progression")
    attributes = origin.get("initial_attributes")
    if (not isinstance(attributes, dict)
            or sum(int(value) for value in attributes.values()) != 25
            or origin.get("initial_attribute_points") != 25):
        raise ValueError("winner sample does not record a valid 25-point initial build")


def _read_existing_files(directory: Path) -> tuple[list[int], list[int], list[str]]:
    indices: list[int] = []
    seeds: list[int] = []
    legacy_files: list[str] = []
    for path in directory.glob("winner_*.json"):
        match = re.fullmatch(r"winner_(\d+)\.json", path.name)
        if not match:
            continue
        indices.append(int(match.group(1)))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            validate_open_world_winner_snapshot(data)
            seeds.append(int(data["origin"]["seed"]))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            # Legacy wrappers stay untouched but are never counted as valid samples.
            legacy_files.append(path.name)
    return indices, seeds, sorted(legacy_files)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=3, help="number of real winners required")
    parser.add_argument("--max-games", type=int, default=500, help="maximum full open-world runs")
    parser.add_argument("--start-seed", type=int, default=1, help="first candidate random seed")
    parser.add_argument("--max-world-turns", type=int, default=120, help="safety ceiling, not a win condition")
    args = parser.parse_args()

    WINNER_DIR.mkdir(parents=True, exist_ok=True)
    existing_indices, existing_seeds, legacy_files = _read_existing_files(WINNER_DIR)
    next_index = max(existing_indices, default=0) + 1
    seed = max(args.start_seed, max(existing_seeds, default=args.start_seed - 1) + 1)
    games_played = 0
    winners = 0
    outcome_counts: dict[str, int] = {}
    region_counts: dict[str, int] = {}
    failure_reasons: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []

    while winners < args.count and games_played < args.max_games:
        region = REGIONS[(seed - 1) % len(REGIONS)]
        player_name = "测试轮回者"
        payload = None
        attempt: dict[str, Any] = {"seed": seed, "region": region, "player_name": player_name}
        games_played += 1
        region_counts[region] = region_counts.get(region, 0) + 1

        try:
            payload = run_playthrough(
                seed=seed,
                max_world_turns=args.max_world_turns,
                region=region,
                player_name=player_name,
                include_winner_snapshot=True,
                blood_points=10,
                speed_points=8,
                mana_points=7,
            )
            validate_complete_playthrough(payload)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
            final = (payload or {}).get("final_state") or {}
            status = "incomplete_or_invalid"
            attempt.update({
                "status": status,
                "error": reason,
                "world_turn": final.get("world_turn"),
                "stop_reason": final.get("stop_reason"),
                "battle_count": final.get("battle_count"),
            })
            outcome_counts[status] = outcome_counts.get(status, 0) + 1
            attempts.append(attempt)
            print(f"#{games_played:>3} seed={seed} {region} 无效/未通关：{reason}")
            seed += 1
            continue

        final = payload["final_state"]
        ordinary_battle_count = sum(
            1 for entry in payload.get("driver_timeline", [])
            if entry.get("type") == "battle_start" and not entry.get("final_duel", False)
        )
        player_won = (
            final.get("winner_id") == (final.get("player") or {}).get("runtime_id")
            and bool((payload.get("winner_snapshot") or {}).get("player", {}).get("is_alive"))
        )
        attempt.update({
            "world_turn": final.get("world_turn"),
            "stop_reason": final.get("stop_reason"),
            "winner_name": final.get("winner_name"),
            "battle_count": final.get("battle_count"),
            "ordinary_battle_count": ordinary_battle_count,
            "combat_event_count": final.get("combat_event_count"),
            "status": "player_won" if player_won else "opponent_won",
        })
        outcome_counts[attempt["status"]] = outcome_counts.get(attempt["status"], 0) + 1

        if player_won:
            snapshot = payload["winner_snapshot"]
            setup = (payload.get("driver_timeline") or [{}])[0].get("setup") or {}
            attributes = setup.get("attributes") or {}
            snapshot["origin"] = {
                "seed": seed,
                "region": region,
                "rank": next_index,
                "generator": "sim/produce_open_world_winners.py",
                "generator_version": 1,
                "initial_attributes": {
                    "blood_points": attributes.get("blood_points"),
                    "speed_points": attributes.get("speed_points"),
                    "mana_points": attributes.get("mana_points"),
                },
                "initial_attribute_points": attributes.get("total"),
                "world_turn": final.get("world_turn"),
                "battle_count": final.get("battle_count"),
                "ordinary_battle_count": ordinary_battle_count,
                "combat_event_count": final.get("combat_event_count"),
                "stop_reason": final.get("stop_reason"),
                "final_duel_won": True,
                "winner_name": final.get("winner_name"),
                "winner_runtime_id": final.get("winner_id"),
                "growth_actions": (payload.get("driver_timeline") or [{}])[0].get("setup", {}).get("growth_actions", []),
                "dao_wen_names": sorted((snapshot.get("player", {}).get("dao_wen") or {}).keys()),
                "spell_names": list(snapshot.get("player", {}).get("spells") or []),
                "friend_count": len(snapshot.get("friends") or []),
                "employee_count": len(snapshot.get("employees") or []),
                "relic_count": len(snapshot.get("relics") or []),
                "shards_peak": snapshot.get("shards_peak", 0),
            }
            try:
                validate_open_world_winner_snapshot(snapshot)
            except ValueError as exc:
                reason = f"winner snapshot rejected: {exc}"
                failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
                attempt["status"] = "winner_snapshot_rejected"
                attempt["error"] = reason
                attempts.append(attempt)
                outcome_counts["winner_snapshot_rejected"] = outcome_counts.get("winner_snapshot_rejected", 0) + 1
                print(f"#{games_played:>3} seed={seed} {region} 胜者快照未达数据契约：{reason}")
                seed += 1
                continue
            out = WINNER_DIR / f"winner_{next_index:02d}.json"
            out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            digest = hashlib.sha256(out.read_bytes()).hexdigest()
            replay_path = WINNER_DIR / f"winner_{next_index:02d}_replay.json"
            replay_payload = {key: value for key, value in payload.items()
                              if key != "winner_snapshot"}
            replay_path.write_text(
                json.dumps(replay_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            replay_digest = hashlib.sha256(replay_path.read_bytes()).hexdigest()
            samples.append({
                "file": out.name,
                "sha256": digest,
                "replay_file": replay_path.name,
                "replay_sha256": replay_digest,
                "seed": seed,
                "region": region,
                "world_turn": final.get("world_turn"),
                "battle_count": final.get("battle_count"),
                "combat_event_count": final.get("combat_event_count"),
            })
            next_index += 1
            winners += 1
            print(f"#{games_played:>3} seed={seed} {region} 真实开放世界胜者：{out.name}")
        else:
            print(f"#{games_played:>3} seed={seed} {region} 最终死斗胜者为对手：{final.get('winner_name')}")
        attempts.append(attempt)
        seed += 1

    report = {
        "schema_version": 1,
        "generator": "sim/produce_open_world_winners.py",
        "requested_winners": args.count,
        "max_games": args.max_games,
        "games_played": games_played,
        "winners_generated_this_run": winners,
        "region_counts": region_counts,
        "outcome_counts": outcome_counts,
        "failure_reasons": failure_reasons,
        "legacy_files_ignored": legacy_files,
        "samples": samples,
        "attempts": attempts,
    }
    (WINNER_DIR / "sampling_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.count > 0 and winners < args.count:
        print(f"错误：要求 {args.count} 位真实开放世界胜者，实际仅生成 {winners} 位。")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
