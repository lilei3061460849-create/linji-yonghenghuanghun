#!/usr/bin/env python3
"""Export the latest (or selected) battle from an existing engine save to replay JSON.

Usage:
    python tools/export_replay.py --slot current --output replay.json
    python tools/export_replay.py --slot current --battle 3 --output replay-3.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.api import GameEngine
from engine.replay import build_replay_payload


def main() -> int:
    parser = argparse.ArgumentParser(description="导出已保存战斗的回放 JSON")
    parser.add_argument("--slot", default="current", help="存档槽名称，默认 current")
    parser.add_argument("--save-dir", default="data/saves", help="存档目录")
    parser.add_argument("--battle", type=int, default=None, help="要导出的场次；默认最新一场")
    parser.add_argument("--output", default="replay.json", help="输出 JSON 文件路径")
    args = parser.parse_args()

    engine = GameEngine(save_dir=args.save_dir, autosave=False)
    loaded = engine.load_game(args.slot)
    if not loaded.get("success"):
        print(f"无法读取存档：{loaded.get('error', '未知错误')}", file=sys.stderr)
        return 1

    payload = build_replay_payload(engine.state, battle_no=args.battle)
    if not payload["events"]:
        print(
            "该场战斗没有可导出的战斗事件。请确认存档已记录战斗事件，"
            "并检查 --battle 场次编号。",
            file=sys.stderr,
        )
        return 2

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(f"已导出 {payload['event_count']} 个事件：{output.resolve()}")
    print(f"场次：{payload['battle_no']}；角色：{len(payload['participants'])}；回合：{payload['round_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
