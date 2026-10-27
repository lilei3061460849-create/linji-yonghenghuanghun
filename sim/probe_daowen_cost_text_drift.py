#!/usr/bin/env python3
"""对照探针：道纹「代价文本」与「引擎实扣」的一致性扫描。

检查同一个道纹的四处代价口径（都以 X 为单位）：
  1. 引擎实扣：`engine/daowen.py::calculate_*` 返回的 cost 字段（真实扣费口径，
     支付路径读的就是它；实跑验证见 `报告.md`《七》）。
  2. 引擎摘要：同一个 dict 里的 summary 文本（形如「消耗5X法力…」）。
  3. 规则正文：`AI_EXPERIENCE.md` 里该道纹的单行条目（形如「【坏死】X：支付：法力5X。」）。
  4. 全道纹索引：`全道纹索引.md` 总览表的代价列。

任一处与「引擎实扣」不一致即登记。只读，不改任何文件。

用法（仓库根目录）：
    python sim/probe_daowen_cost_text_drift.py
    python sim/probe_daowen_cost_text_drift.py --json out.json
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.daowen import DaoWenEngine  # noqa: E402


def rule_per_x(name: str, text: str):
    for line in text.splitlines():
        for pat in (rf"【{re.escape(name)}】X[：:]消耗(\d+)X",
                    rf"{re.escape(name)}X[：:]消耗(\d+)X",
                    rf"{re.escape(name)}X（消耗(\d+)X"):
            m = re.search(pat, line)
            if m:
                return int(m.group(1))
    return None


def index_per_x(name: str, text: str):
    m = re.search(rf"^\| {re.escape(name)} \|(.*)$", text, re.M)
    if not m:
        return None
    mm = re.search(r"消耗(\d+)X", m.group(1))
    return int(mm.group(1)) if mm else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, default=None, help="把结果另存为 JSON")
    args = ap.parse_args()

    rules = (ROOT / "AI_EXPERIENCE.md").read_text(encoding="utf-8")
    index = (ROOT / "全道纹索引.md").read_text(encoding="utf-8")

    rows, unparsed = [], []
    for name in sorted(DaoWenEngine.list_all()):
        try:
            calc = DaoWenEngine.resolve(name, 3)
        except Exception as exc:                       # X=3 不合法（X 上限等）
            unparsed.append({"name": name, "note": f"resolve(3) 抛错: {exc}"})
            continue
        if calc.get("cost_type") != "消耗" or calc.get("cost") is None:
            continue                                   # 非「消耗」代价类不在此扫描
        m = re.search(r"消耗(\d+)(假碎片|碎片|法力)", calc.get("summary", ""))
        if not m:
            continue
        engine_per_x = calc["cost"] / 3
        summary_per_x = int(m.group(1)) / 3
        rule = rule_per_x(name, rules)
        idx = index_per_x(name, index)
        if engine_per_x == summary_per_x and rule in (None, engine_per_x) \
                and idx in (None, engine_per_x):
            continue
        classes = []
        if summary_per_x != engine_per_x:
            classes.append("摘要")
        if rule is not None and rule != engine_per_x:
            classes.append("正文")
        if idx is not None and idx != engine_per_x:
            classes.append("索引")
        rows.append({"name": name, "engine_per_x": engine_per_x,
                     "summary_per_x": summary_per_x, "rule_per_x": rule,
                     "index_per_x": idx, "texts": "/".join(classes)})

    print(f"扫描 {len(DaoWenEngine.list_all())} 个道纹｜代价文本与引擎实扣不一致 {len(rows)} 个")
    print("| 道纹 | 引擎实扣/X | 摘要 | 规则正文 | 索引 | 不一致处 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for r in rows:
        f = lambda v: "—" if v is None else f"{v:g}X"          # noqa: E731
        print(f"| {r['name']} | {r['engine_per_x']:g}X | {f(r['summary_per_x'])} | "
              f"{f(r['rule_per_x'])} | {f(r['index_per_x'])} | {r['texts']} |")
    only_rule = [r for r in rows if "正文" in r["texts"]]
    print(f"\n其中「规则正文也写贵了」的 {len(only_rule)} 个（需裁定改文本还是改引擎）："
          + "、".join(r["name"] for r in only_rule))
    if unparsed:
        print("未能扫描（X=3 非法）：" + "、".join(u["name"] for u in unparsed))
    if args.json:
        args.json.write_text(json.dumps({"mismatches": rows, "unparsed": unparsed},
                                        ensure_ascii=False, indent=1), encoding="utf-8")
        print("已写:", args.json)


if __name__ == "__main__":
    main()
