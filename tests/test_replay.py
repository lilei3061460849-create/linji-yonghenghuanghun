from types import SimpleNamespace

from engine.combat_events import CombatEvent, CombatEventType
from engine.replay import build_replay_payload


def entity(name, entity_type, side, hp, alive=True):
    return SimpleNamespace(
        name=name,
        entity_type=entity_type,
        runtime_id=f"id-{name}",
        current_hp=hp,
        blood_limit=100,
        current_mana=5,
        mana_limit=10,
        shield=0,
        is_alive=alive,
        status_effects=[],
    )


def event(event_type, battle_no, round_no, actor="", target="", **data):
    return CombatEvent(
        event_type=event_type,
        battle_no=battle_no,
        round_no=round_no,
        actor_name=actor,
        target_name=target,
        data=data,
    )


def test_replay_defaults_to_latest_battle_and_keeps_resolved_values():
    state = SimpleNamespace(
        current_battle=2,
        player=entity("玩家", "轮回者", "player", 70),
        friends=[],
        employees=[],
        temp_friends=[],
        enemies=[entity("怪物", "怪物", "enemy", 0, alive=False)],
        combat_events=[
            event(CombatEventType.BATTLE_START, 1, 0),
            event(CombatEventType.DAMAGE_APPLIED, 1, 1, "玩家", "旧怪物", actual_life_loss=9),
            event(CombatEventType.BATTLE_START, 2, 0),
            event(CombatEventType.DAMAGE_APPLIED, 2, 1, "玩家", "怪物",
                  actual_life_loss=12, hp_before=12, hp_after=0),
        ],
    )

    payload = build_replay_payload(state)

    assert payload["battle_no"] == 2
    assert payload["event_count"] == 2
    assert payload["events"][1]["data"]["actual_life_loss"] == 12
    assert {p["name"] for p in payload["participants"]} == {"玩家", "怪物"}
    assert payload["events"][1]["sequence"] == 1


def test_replay_can_export_a_specific_battle_and_json_safe_sets():
    state = SimpleNamespace(
        current_battle=2,
        player=entity("玩家", "轮回者", "player", 70),
        friends=[],
        employees=[],
        temp_friends=[],
        enemies=[],
        combat_events=[
            event(CombatEventType.DAMAGE_APPLIED, 1, 1, "玩家", "怪物",
                  tags={"闪避", "伤害"}, actual_life_loss=4),
            event(CombatEventType.BATTLE_END, 2, 2),
        ],
    )

    payload = build_replay_payload(state, battle_no=1)

    assert payload["event_count"] == 1
    assert sorted(payload["events"][0]["data"]["tags"]) == ["伤害", "闪避"]
    assert payload["participants"][0]["name"] == "玩家"
