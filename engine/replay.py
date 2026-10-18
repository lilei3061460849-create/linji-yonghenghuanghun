"""Combat replay export helpers.

Replay data is a read-only projection of the engine's existing combat event stream.
It never invokes combat rules, AI, or random-number generation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any


REPLAY_SCHEMA_VERSION = 1


def _json_safe(value: Any, depth: int = 0) -> Any:
    """Convert event payload values to JSON-safe primitives without serializing engines."""
    if depth > 8:
        return str(value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        # Strict JSON has no NaN or infinity.
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, Enum):
        return _json_safe(value.value, depth + 1)
    if isinstance(value, dict):
        return {str(key): _json_safe(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_safe(item, depth + 1) for item in value), key=str)
    # Avoid dataclass/asdict recursion through Entity -> GameState / engine references.
    if hasattr(value, "entity_type") and hasattr(value, "name"):
        return {
            "name": str(getattr(value, "name", "")),
            "entity_type": str(getattr(value, "entity_type", "")),
            "runtime_id": str(getattr(value, "runtime_id", "")),
        }
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _json_safe(to_dict(), depth + 1)
        except Exception:
            return str(value)
    return str(value)


def _entity_snapshot(entity: Any, side: str) -> dict[str, Any]:
    statuses = []
    for status in getattr(entity, "status_effects", []) or []:
        statuses.append({
            "name": str(getattr(status, "name", "状态")),
            "remaining_rounds": getattr(status, "remaining_rounds", None),
            "value": getattr(status, "value", None),
        })
    return {
        "name": str(getattr(entity, "name", "未知角色")),
        "entity_type": str(getattr(entity, "entity_type", "")),
        "runtime_id": str(getattr(entity, "runtime_id", "")),
        "side": side,
        # These are export-time values, not historical per-event values.
        "final_state": {
            "hp": getattr(entity, "current_hp", None),
            "blood_limit": getattr(entity, "blood_limit", None),
            "mana": getattr(entity, "current_mana", None),
            "mana_limit": getattr(entity, "mana_limit", None),
            "shield": getattr(entity, "shield", None),
            "alive": bool(getattr(entity, "is_alive", False)),
            "statuses": statuses,
        },
    }


def build_replay_payload(state: Any, battle_no: int | None = None) -> dict[str, Any]:
    """Build a portable replay JSON payload from a GameState-like object.

    Defaults to the latest battle represented in the event stream. The payload is
    descriptive only: event order and values are copied from already-resolved facts.
    """
    raw_events = list(getattr(state, "combat_events", []) or [])
    event_dicts = []
    for event in raw_events:
        if hasattr(event, "to_dict") and callable(event.to_dict):
            item = event.to_dict()
        elif isinstance(event, dict):
            item = dict(event)
        else:
            continue
        event_dicts.append(item)

    available_battles = [
        int(item.get("battle_no") or 0)
        for item in event_dicts
        if item.get("battle_no") is not None
    ]
    selected_battle = battle_no
    if selected_battle is None:
        selected_battle = max(available_battles, default=int(getattr(state, "current_battle", 0) or 0))

    selected = [
        item for item in event_dicts
        if int(item.get("battle_no") or 0) == int(selected_battle)
    ]
    events = []
    seen_names: list[str] = []
    for index, item in enumerate(selected):
        actor = str(item.get("actor_name") or item.get("actor") or "")
        target = str(item.get("target_name") or item.get("target") or "")
        for name in (actor, target):
            if name and name not in seen_names:
                seen_names.append(name)
        events.append({
            "sequence": index,
            "event_type": str(item.get("event_type") or "unknown"),
            "battle_no": int(item.get("battle_no") or selected_battle or 0),
            "round_no": int(item.get("round_no") or 0),
            "actor_name": actor,
            "target_name": target,
            "data": _json_safe(item.get("data") or {}),
            "ctx": _json_safe(item.get("ctx")),
            "timestamp": _json_safe(item.get("timestamp")),
        })

    entity_snapshots = []
    player = getattr(state, "player", None)
    if player is not None:
        entity_snapshots.append(_entity_snapshot(player, "player"))
    for attr, side in (
        ("friends", "player"),
        ("employees", "player"),
        ("temp_friends", "player"),
        ("enemies", "enemy"),
    ):
        for entity in getattr(state, attr, []) or []:
            entity_snapshots.append(_entity_snapshot(entity, side))

    # Keep participants relevant to this battle. If an old event stream lacks names,
    # retain the available entity roster so the viewer can still render a useful panel.
    participant_names = set(seen_names)
    participants = [
        entity for entity in entity_snapshots
        if not participant_names or entity["name"] in participant_names
    ]
    known = {entity["name"] for entity in participants}
    for name in seen_names:
        if name not in known:
            participants.append({
                "name": name,
                "entity_type": "",
                "runtime_id": "",
                "side": "unknown",
                "final_state": {},
            })

    rounds = sorted({event["round_no"] for event in events if event["round_no"] > 0})
    return {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "title": f"第 {selected_battle} 场战斗回放",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "battle_no": int(selected_battle or 0),
        "round_count": max(rounds, default=0),
        "event_count": len(events),
        "participants": participants,
        "events": events,
        "notes": [
            "事件顺序与结算数值来自引擎已发生的战斗事件。",
            "角色 final_state 是导出时的状态，不代表每个历史回合的状态。",
            "第一版只呈现战斗双方与事件过程，不生成尚未接入引擎的世界地图位置。",
        ],
    }
