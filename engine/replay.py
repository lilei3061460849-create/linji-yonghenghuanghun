"""战局回放数据导出。只读取已经发生的事件，不重新执行游戏规则。"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

REPLAY_SCHEMA_VERSION = 1


def _json_safe(value: Any, depth: int = 0) -> Any:
    """将事件内容规整为可安全写入 JSON 的基础类型。"""
    if depth > 10:
        return "<nested value omitted>"
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Enum):
        return _json_safe(value.value, depth + 1)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            return _json_safe(value.to_dict(), depth + 1)
        except Exception:
            return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        try:
            return _json_safe(asdict(value), depth + 1)
        except Exception:
            return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item, depth + 1) for item in value]
    return str(value)


def _entity_snapshot(entity: Any) -> dict[str, Any] | None:
    if entity is None:
        return None
    fields = (
        "name", "runtime_id", "entity_type", "current_hp", "blood_limit",
        "current_mana", "mana_limit", "current_speed", "speed_limit",
        "shield", "is_alive", "is_departed",
    )
    snapshot = {}
    for field in fields:
        if hasattr(entity, field):
            snapshot[field] = _json_safe(getattr(entity, field))
    return snapshot or None


def _game_state(source: Any) -> Any:
    """同时接受 GameEngine 与 GameState，方便接入现有运行流程。"""
    return getattr(source, "state", source)


def build_replay_payload(state: Any, title: str | None = None) -> dict[str, Any]:
    """从 GameEngine 或 GameState 构建回放 JSON；不推进游戏时间。"""
    state = _game_state(state)
    raw_events = list(getattr(state, "combat_events", []) or [])
    events = []
    for sequence, event in enumerate(raw_events):
        if hasattr(event, "to_dict"):
            record = event.to_dict()
        elif isinstance(event, dict):
            record = dict(event)
        else:
            record = {"event_type": "unknown", "data": {"detail": str(event)}}
        safe_record = _json_safe(record)
        safe_record["sequence"] = sequence
        safe_record.setdefault("event_type", "unknown")
        safe_record.setdefault("battle_no", 0)
        safe_record.setdefault("round_no", 0)
        safe_record.setdefault("actor_name", "")
        safe_record.setdefault("target_name", "")
        safe_record.setdefault("data", {})
        events.append(safe_record)

    entities = []
    seen = set()
    for group_name in ("player", "friends", "employees", "temp_friends", "enemies"):
        group = getattr(state, group_name, None)
        group = group if isinstance(group, (list, tuple)) else [group]
        for entity in group:
            snapshot = _entity_snapshot(entity)
            if not snapshot:
                continue
            identity = snapshot.get("runtime_id") or snapshot.get("name")
            if identity in seen:
                continue
            seen.add(identity)
            snapshot["side"] = "enemy" if group_name == "enemies" else "ally"
            entities.append(snapshot)

    return {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "title": title or f"战局回放 · {getattr(state, 'game_id', '') or '未命名副本'}",
        "metadata": {
            "game_id": _json_safe(getattr(state, "game_id", "")),
            "region": _json_safe(getattr(state, "current_region", "")),
            "current_battle": _json_safe(getattr(state, "current_battle", 0)),
            "current_round": _json_safe(getattr(state, "current_round", 0)),
            "event_count": len(events),
        },
        "participants": entities,
        "events": events,
        "map": {
            "status": "not_connected",
            "note": "首版先回放真实战斗事件；地图坐标接入后可在同一时间轴展示位置变化。",
        },
    }


def export_replay(state: Any, path: str | Path, title: str | None = None) -> dict[str, Any]:
    """将现有战斗事件导出为可由手机浏览器导入的 UTF-8 JSON 文件。"""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = build_replay_payload(state, title=title)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "success": True,
        "filepath": str(destination),
        "event_count": len(payload["events"]),
        "schema_version": REPLAY_SCHEMA_VERSION,
    }
