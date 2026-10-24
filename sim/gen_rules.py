#!/usr/bin/env python3
"""从结构化事实源（data/rules/*.toml）生成《死者之书.md》的「## 规则」节。

章节（2026-10-09 起多章并行；章顺序固定：核心规则在前，特殊事件在后）：
  - 「核心规则」（data/rules/game_rules.toml）：底层逻辑的**压缩讲述**；不改任何
    结算，完整正文以《规则正文》为准。
  - 「特殊事件」（data/rules/lethal_events.toml + special_events.toml，按 order
    合并）：致死类事件（样板批 2026-10-08）结构化数值 + 原文照录；非致死类事件
    （雕塑批 2026-10-09）数值字段随事件自身 + 原文照录。

## 为什么规则署名 ？？？

遗言署真人姓名，可能有真假；规则不会——所有人看到的规则都一样。所以代表规则
的页面一律署名 `？？？`（条目形如 `### ？？？·特殊事件·迷失`），与署人名的个人
遗言区分开。

## 为什么单独一节、不写进「## 死者之页」

`engine/death_book.py` 的遗言有**单句 20 字上限**（DEFAULT_CAPACITY=20），
规则条文塞不进去（光「封印X：代价：异变X，使一个[目标]延后X回合再入场」就 20+ 字）。
规则因此进并列的「## 规则」节，不受 20 字约束。death_book 只读写「## 死者之页」节，
新增本节不会影响运行时。

## 用法

    python3 sim/gen_rules.py          # 写回 死者之书.md
    python3 sim/gen_rules.py --check  # 只比对，不写（供一致性测试用）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.rules_source import game_rules, lethal_events, special_events  # noqa: E402

BOOK = ROOT / "死者之书.md"
SECTION_HEADER = "## 规则"
SIGNATURE = lethal_events.signature
CHAPTER = lethal_events.chapter


def render_section() -> str:
    """把全部规则事实源渲染成「## 规则」节的全文（不含首尾空行）。"""
    lines: list[str] = []
    lines.append(SECTION_HEADER)
    lines.append("")
    lines.append(
        "> 本节由 `data/rules/*.toml` 生成，运行 `python3 sim/gen_rules.py` 重新生成；"
        "**禁止手改**（手改会在生成器下次运行时丢失，且过不了一致性测试）。"
    )
    lines.append(">")
    lines.append(
        f"> 署名统一为 `{SIGNATURE}`：遗言署真人姓名、可能有真假；规则不会，"
        "所有人看到的规则都一样。"
    )
    lines.append(">")
    lines.append(
        "> 「核心规则」章只收从始至终不会改变的底层逻辑的压缩讲述——同一条"
        "逻辑更短的说法，不改任何结算；可调数值与机制细则不进本章，完整正文"
        "以规则文档为准。"
    )
    lines.append("")

    # ---- 章一：核心规则（压缩讲述，2026-10-09 起逐条加入）----
    for entry in game_rules.all():
        lines.append(f"### {game_rules.signature}·{game_rules.chapter}·{entry['name']}")
        lines.append("")
        for text in entry["rule_lines"]:
            lines.append(text)
        lines.append("")

    # ---- 章二：特殊事件（致死类 + 非致死类，按 order 合并排序）----
    all_events = ([("lethal", e) for e in lethal_events.all()]
                  + [("special", e) for e in special_events.all()])
    all_events.sort(key=lambda pair: pair[1].get("order", 0))
    for kind, entry in all_events:
        name = entry["name"]
        # 条目可自带 chapter 覆盖（2026-10-24：【夺书】不再是特殊事件，
        # 以 chapter="同伴规则" 单独成章；其余事件仍渲染进「特殊事件」章）。
        chapter = entry.get("chapter") or CHAPTER
        lines.append(f"### {SIGNATURE}·{chapter}·{name}")
        lines.append("")
        if kind == "lethal":
            # 结构化字段：让人和 AI 都能直接读到数值，不必回散文里数
            bits = []
            if "threshold" in entry:
                bits.append(f"阈值 {entry['threshold']}")
            if "threshold_multiplier" in entry:
                bits.append(f"阈值 ⌈[血限]×{entry['threshold_multiplier']}⌉")
            bits.append(f"进度口径 {entry['counter_label']}")
            lines.append(f"- {'；'.join(bits)}")
            lines.append(f"- 阈值算法：{entry['threshold_expr']}")
            lines.append(f"- 死因文案：{entry['death_cause_text']}")
            lines.append("")
        else:
            # 非致死类：数值字段随事件自身（雕塑：耐久与每耐久伤害/格挡）
            if "durability_ratio" in entry:
                lines.append(
                    f"- 耐久上限 ⌈[血限]×{entry['durability_ratio']}⌉（至少 {entry['durability_min']}）；"
                    f"每点耐久 伤害 {entry['damage_per_durability']} / 格挡 {entry['shield_per_durability']}")
            if entry.get("trigger"):
                lines.append(f"- 触发条件：{entry['trigger']}")
            lines.append("")
        for text in entry["rule_lines"]:
            lines.append(text)
        lines.append("")

    return "\n".join(lines).rstrip()


def extract_section(book_text: str) -> str | None:
    """从《死者之书.md》里取出「## 规则」节的现有内容（没有则返回 None）。"""
    if SECTION_HEADER not in book_text:
        return None
    head, _, rest = book_text.partition(SECTION_HEADER)
    # 切到下一个同级或更高级标题为止
    body = rest
    for marker in ("\n## ", "\n# "):
        idx = body.find(marker, 1)
        if idx != -1:
            body = body[:idx]
    return (SECTION_HEADER + body).rstrip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="只比对现有「## 规则」节与生成结果，不写文件")
    args = parser.parse_args()

    wanted = render_section()
    book_text = BOOK.read_text(encoding="utf-8") if BOOK.exists() else ""

    if args.check:
        current = extract_section(book_text)
        if current is None:
            print("MISSING: 死者之书.md 里没有「## 规则」节")
            return 1
        if current != wanted:
            print("DRIFT: 「## 规则」节与 data/rules/*.toml 不一致，请跑 python3 sim/gen_rules.py")
            return 1
        print("OK: 「## 规则」节与事实源一致")
        return 0

    if not book_text:
        print(f"找不到 {BOOK}", file=sys.stderr)
        return 1

    current = extract_section(book_text)
    if current is None:
        new_text = book_text.rstrip() + "\n\n" + wanted + "\n"
    else:
        new_text = book_text.replace(current, wanted)
    BOOK.write_text(new_text, encoding="utf-8")
    print(f"written {BOOK}: 「## 规则」节 "
          f"核心规则 {len(game_rules.all())} 条 + 特殊事件 "
          f"{len(lethal_events.all()) + len(special_events.all())} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
