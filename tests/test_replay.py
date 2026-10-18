import json
from types import SimpleNamespace

from engine.replay import build_replay_payload


class FakeEvent:
    def __init__(self, event_type, battle_no, actor="", target="", data=None):
        self.event_type = event_type
        self.battle_no = battle_no
        self.round_no = 3
        self.actor_name = actor
        self.target_name = target
        self.data = data or {}
        self.ctx = None
        self.timestamp = 123.0

    def to_dict(self):
        return {
            "event_type": self.event_type,
            "battle_no": self.battle_no,
            "round_no": self.round_no,
            "actor_name": self.actor_name,
            "target_name": self.target_name,
            "data": self.data,
            "ctx": self.ctx,
            "timestamp": self.timestamp,
        }


def test_build_payload_preserves_event_order_and_json_safe_data():
    state = SimpleNamespace(
        game_id="g-1",
        current_region="乱葬岗",
        current_battle=2,
        current_round=3,
        combat_events=[
            FakeEvent("action_declared", 2, "甲", "乙", {"name": "攻击", "amount": 5}),
            FakeEvent("damage_applied", 2, "甲", "乙", {"actual_damage": 5, "tags": {"a", "b"}}),
            FakeEvent("healing_applied", 1, "乙", "甲", {"amount": 2}),
        ],
        player=SimpleNamespace(
            name="甲", runtime_id="p1", entity_type="轮回者", current_hp=10,
            blood_limit=20, current_mana=2, mana_limit=5, current_speed=1,
            speed_limit=2, shield=0, is_alive=True, is_departed=False,
            status_effects=[],
        ),
        friends=[], employees=[], temp_friends=[], enemies=[],
    )

    payload = build_replay_payload(state)

    assert [event["sequence"] for event in payload["events"]] == [0, 1]
    assert [event["event_type"] for event in payload["events"]] == [
        "action_declared", "damage_applied"
    ]
    assert payload["events"][1]["data"]["actual_damage"] == 5
    assert sorted(payload["events"][1]["data"]["tags"]) == ["a", "b"]
    assert payload["participants"][0]["name"] == "甲"
    assert payload["event_count"] == 2
    json.dumps(payload, ensure_ascii=False)


def test_build_payload_can_select_an_older_battle():
    state = SimpleNamespace(
        current_battle=2,
        combat_events=[
            FakeEvent("damage_applied", 1, "甲", "乙", {"actual_damage": 4}),
            FakeEvent("damage_applied", 2, "甲", "丙", {"actual_damage": 8}),
        ],
        player=None, friends=[], employees=[], temp_friends=[], enemies=[],
    )

    payload = build_replay_payload(state, battle_no=1)

    assert payload["battle_no"] == 1
    assert payload["event_count"] == 1
    assert payload["events"][0]["target_name"] == "乙"


def test_build_payload_handles_empty_event_stream():
    state = SimpleNamespace(
        current_battle=0, combat_events=[], player=None,
        friends=[], employees=[], temp_friends=[], enemies=[],
    )

    payload = build_replay_payload(state)

    assert payload["events"] == []
    assert payload["event_count"] == 0
    json.dumps(payload, ensure_ascii=False)
