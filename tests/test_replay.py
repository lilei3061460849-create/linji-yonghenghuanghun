import json
from types import SimpleNamespace

from engine.replay import build_replay_payload, export_replay


class FakeEvent:
    def __init__(self, event_type, actor="", target="", data=None):
        self.event_type = event_type
        self.battle_no = 2
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


def test_build_payload_preserves_event_order_and_safe_data():
    state = SimpleNamespace(
        game_id="g-1", current_region="乱葬岗", current_battle=2, current_round=3,
        combat_events=[
            FakeEvent("action_declared", "甲", "乙", {"name": "攻击", "amount": 5}),
            FakeEvent("damage_applied", "甲", "乙", {"actual_damage": 5, "tags": {"a", "b"}}),
        ],
        player=SimpleNamespace(name="甲", runtime_id="p1", entity_type="轮回者", current_hp=10,
                               blood_limit=20, current_mana=2, mana_limit=5, current_speed=1,
                               speed_limit=2, shield=0, is_alive=True, is_departed=False),
        friends=[], employees=[], temp_friends=[], enemies=[],
    )
    payload = build_replay_payload(state)
    assert [e["sequence"] for e in payload["events"]] == [0, 1]
    assert payload["events"][1]["data"]["actual_damage"] == 5
    assert isinstance(payload["events"][1]["data"]["tags"], list)
    assert payload["participants"][0]["name"] == "甲"
    json.dumps(payload, ensure_ascii=False)


def test_export_replay_writes_utf8_json(tmp_path):
    state = SimpleNamespace(game_id="test", current_region="测试副本", current_battle=1,
                            current_round=1, combat_events=[FakeEvent("entity_died", target="怪物")],
                            player=None, friends=[], employees=[], temp_friends=[], enemies=[])
    path = tmp_path / "nested" / "replay.json"
    result = export_replay(state, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert result["success"] is True
    assert result["event_count"] == 1
    assert data["events"][0]["event_type"] == "entity_died"


def test_empty_event_stream_is_exportable(tmp_path):
    state = SimpleNamespace(game_id="empty", current_region="", current_battle=0,
                            current_round=0, combat_events=[], player=None,
                            friends=[], employees=[], temp_friends=[], enemies=[])
    payload = build_replay_payload(state)
    assert payload["events"] == []
    assert payload["metadata"]["event_count"] == 0


def test_export_accepts_engine_wrapper(tmp_path):
    state = SimpleNamespace(game_id="wrapped", current_region="", current_battle=1,
                            current_round=1, combat_events=[FakeEvent("round_start")],
                            player=None, friends=[], employees=[], temp_friends=[], enemies=[])
    engine = SimpleNamespace(state=state)
    result = export_replay(engine, tmp_path / "replay.json")
    assert result["event_count"] == 1
