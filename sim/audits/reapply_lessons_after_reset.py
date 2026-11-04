#!/usr/bin/env python3
"""在知识库重置后恢复方法论提示与有证据的经验，不回填旧版成绩。

data/build_knowledge.json 的 methodology 仅用于实验治理，不是当前游戏策略结论；
lessons 必须带证据字段。旧版本 history、胜率、遥测和单次最优构筑不得恢复。
"""
from __future__ import annotations

# --- 2026-11-04：脚本放进子目录后，不再靠"往上数几层"猜仓库根 ---
# 向上找到同时含 engine/ 与 sim/ 的目录就是仓库根；再附带 sim/ 让老式裸模块名
# import（如 `import balance_sim`）继续可用。搬动本文件不需要再改这几行。
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.abspath(__file__))
while _ROOT != _os.path.dirname(_ROOT) and not _os.path.isdir(_os.path.join(_ROOT, "engine")):
    _ROOT = _os.path.dirname(_ROOT)
for _p in (_ROOT, _os.path.join(_ROOT, "sim"), _os.path.join(_ROOT, "tests")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)


import json
import subprocess
from pathlib import Path

ROOT = _ROOT
KNOWLEDGE = ROOT / "data" / "build_knowledge.json"


def committed_knowledge() -> dict[str, list[dict]]:
    try:
        raw = subprocess.check_output(
            ["git", "show", "HEAD:data/build_knowledge.json"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        data = json.loads(raw)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return {"lessons": [], "methodology": []}

    lessons = [
        item for item in data.get("lessons", [])
        if item.get("kind") in {"心得", "陷阱"}
        and item.get("text") and item.get("evidence")
    ]
    methodology = [
        item for item in data.get("methodology", [])
        if item.get("topic") and item.get("guidance")
    ]
    return {"lessons": lessons, "methodology": methodology}


def _deduplicate(items: list[dict], key: str) -> list[dict]:
    out = []
    seen = set()
    for item in items:
        value = item.get(key, "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(item)
    return out


def main() -> None:
    data = json.loads(KNOWLEDGE.read_text(encoding="utf-8"))
    committed = committed_knowledge()

    lessons = _deduplicate(
        [
            item for item in data.get("lessons", [])
            if item.get("kind") in {"心得", "陷阱"} and item.get("text") and item.get("evidence")
        ] + committed["lessons"],
        "text",
    )
    methodology = _deduplicate(
        data.get("methodology", []) + committed["methodology"],
        "guidance",
    )
    data["lessons"] = lessons
    data["methodology"] = methodology
    KNOWLEDGE.write_text(
        json.dumps(data, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    print(
        f"已恢复 {len(lessons)} 条有证据经验与 {len(methodology)} 条方法论提示；"
        "未恢复任何旧版成绩、遥测或最优构筑。"
    )


if __name__ == "__main__":
    main()