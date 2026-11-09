"""角色卡存档：自定义角色的「保存 / 修改 / 删除」。

设计口径：

* 只存**配方**，不存引擎对象——名字、三维、资源、道纹、遗物、消耗品；
  开局时再按配方现场构造实体，避免把某个版本的运行时对象序列化进存档；
* 单文件 JSON（默认 ``data/player_characters.json``，可用环境变量
  ``LINJI_CHARACTERS_FILE`` 覆盖），写入走「临时文件 + os.replace」，断电不会写出半个文件；
* 只允许本进程写入，读改写全程持锁；同名不合并，按 ``id`` 区分。

存档是玩家本地数据，不是规则事实源：``.gitignore`` 已排除该文件。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "data" / "player_characters.json"

#: 单文件最多保留多少张角色卡（防止前端循环写入把文件撑爆）。
MAX_CHARACTERS = 80

_LOCK = threading.RLock()


def storage_path() -> Path:
    override = os.environ.get("LINJI_CHARACTERS_FILE", "").strip()
    return Path(override) if override else DEFAULT_PATH


def _read() -> list[dict[str, Any]]:
    """读取全部角色卡；文件缺失/损坏时按空列表处理（不静默覆盖坏文件）。"""
    path = storage_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return []
    characters = raw.get("characters") if isinstance(raw, dict) else None
    if not isinstance(characters, list):
        return []
    return [item for item in characters if isinstance(item, dict) and item.get("id")]


def _write(characters: list[dict[str, Any]]) -> None:
    path = storage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "characters": characters}
    handle, temp_name = tempfile.mkstemp(prefix=".characters-", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def list_characters() -> list[dict[str, Any]]:
    """按最近修改时间倒序返回全部角色卡。"""
    with _LOCK:
        characters = _read()
    characters.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return characters


def get_character(character_id: str) -> dict[str, Any] | None:
    with _LOCK:
        for item in _read():
            if item.get("id") == character_id:
                return item
    return None


def save_character(sheet: dict[str, Any]) -> dict[str, Any]:
    """新增或按 ``id`` 更新一张角色卡，返回落盘后的完整记录。"""
    if not isinstance(sheet, dict):
        raise ValueError("角色卡必须是对象")
    name = str(sheet.get("name") or "").strip()
    if not name:
        raise ValueError("角色名不能为空")
    if len(name) > 12:
        raise ValueError("角色名最多 12 个字")

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _LOCK:
        characters = _read()
        character_id = str(sheet.get("id") or "").strip()
        existing = next((item for item in characters if item.get("id") == character_id), None)
        record = dict(sheet)
        if existing is None:
            if len(characters) >= MAX_CHARACTERS:
                raise ValueError(f"角色卡最多 {MAX_CHARACTERS} 张，请先删除一些")
            character_id = uuid.uuid4().hex[:12]
            record["created_at"] = now
        else:
            record["created_at"] = existing.get("created_at") or now
        record["id"] = character_id
        record["name"] = name
        record["updated_at"] = now
        characters = [item for item in characters if item.get("id") != character_id]
        characters.append(record)
        _write(characters)
    return record


def delete_character(character_id: str) -> bool:
    if not character_id:
        raise ValueError("缺少角色 id")
    with _LOCK:
        characters = _read()
        kept = [item for item in characters if item.get("id") != character_id]
        if len(kept) == len(characters):
            return False
        _write(kept)
    return True
