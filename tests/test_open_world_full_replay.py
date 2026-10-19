"""Deterministic end-to-end proof for the canonical open-world loop."""

from sim.run_open_world_replay import SEED, run_playthrough


def test_seeded_open_world_run_reaches_final_duel_and_exports_engine_events():
    replay = run_playthrough(SEED)
    final = replay["final_state"]

    assert final["game_complete"] is True
    assert final["stop_reason"] == "final duel completed"
    assert final["winner_id"] == final["player"]["runtime_id"]
    assert final["winner_name"] == final["player"]["name"]
    assert final["player"]["alive"] is True
    assert final["world_turn"] > 0

    # The run must use the ordinary tier-1 population and a rules-valid setup,
    # not a handful of hand-placed enemies or an over-budget test character.
    assert replay["run_config"]["region"] == "罪孽都市"
    assert replay["initial_population"]["monsters"] >= 1
    assert replay["initial_population"]["other_reincarnators"] >= 2
    assert replay["initial_population"]["special_locations"] >= 1
    setup = replay["driver_timeline"][0]["setup"]
    assert setup["attributes"]["total"] == 25

    assert final["battle_count"] == len(replay["battles"]) >= 1
    assert final["combat_event_count"] == sum(
        battle["event_count"] for battle in replay["battles"])
    final_battle = replay["battles"][-1]
    assert final_battle["outcome"] == "player_won"
    final_duel_entries = [entry for entry in replay["driver_timeline"]
                          if entry["type"] == "final_duel"]
    assert len(final_duel_entries) == 1
    assert final_duel_entries[0]["result"]["winner"] == "challenger"
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

    repeated = run_playthrough(SEED)
    assert repeated["final_state"]["world_turn"] == final["world_turn"]
    assert repeated["final_state"]["player"]["hp"] == final["player"]["hp"]
    assert repeated["final_state"]["winner_name"] == final["winner_name"]
    assert [(battle["round_count"], battle["event_count"], battle["outcome"])
            for battle in repeated["battles"]] == [
        (battle["round_count"], battle["event_count"], battle["outcome"])
        for battle in replay["battles"]]


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
