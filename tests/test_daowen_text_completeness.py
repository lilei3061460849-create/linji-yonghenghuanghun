"""护栏：道纹效果正文不得只剩支付句。

`全道纹索引.md` 里出现了只有「X：支付：法力NX。」而无效果句的
条目，且 `规则正文.md` 副本专属道纹节一度用「见《全道纹索引》」代替正文。两条都
算效果描述丢失，本文件把这类退化钉死：

1. 索引条目必须以道纹名开头，且代价段之后必须有非空效果句；
2. 规则正文副本专属道纹节不得出现指向索引的指针，每条 bullet 必须有正文；
3. 副本文件里出现的专属道纹名，必须能在索引或规则正文副本节里读到完整条文。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "全道纹索引.md"
RULES = ROOT / "规则正文.md"
AI_CONTEXT = ROOT / "AI_CONTEXT.md"

# 代价段结束标记：「。」或「；」之后才是效果句。
_SENTENCE_SPLIT = re.compile(r"[。；]")
_COMMA_SPLIT = re.compile(r"，")


def _index_entries() -> dict[str, str]:
    text = INDEX.read_text(encoding="utf-8")
    entries: dict[str, str] = {}
    for chunk in re.split(r"^### ", text, flags=re.M)[1:]:
        name, _, body = chunk.partition("\n")
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        entries[name.strip()] = lines[0] if lines else ""
    return entries


def _dungeon_daowen_names() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "副本").glob("*.md"):
        names.update(re.findall(r"^\d+\.([\u4e00-\u9fff]{2})X", path.read_text(encoding="utf-8"), re.M))
    return names


def _rule_dungeon_section() -> str:
    text = RULES.read_text(encoding="utf-8")
    return text.split("### 副本专属道纹", 1)[1].split("### 原始怪物道纹", 1)[0]


def test_every_index_entry_starts_with_its_name():
    bad = [f"{name}：{line}" for name, line in _index_entries().items()
           if not line.startswith(f"{name}X")]
    assert bad == [], "索引条目必须以道纹名开头（否则速查里只剩「X：支付…」）：" + repr(bad[:5])


def test_every_index_entry_has_an_effect_sentence():
    """代价段之后必须还有效果正文，不能只有「支付：…。」。

    效果句可能用「。」「；」「，」与代价相连（例：`支付：异变X，使一个[目标]延后X回合再入场。`），
    所以取最后一个分句判断，仅剩支付枚举即视为缺效果。
    """
    bad = []
    for name, line in _index_entries().items():
        body = line[len(name):] if line.startswith(name) else line
        effect = _SENTENCE_SPLIT.split(body, maxsplit=1)
        tail = effect[1].strip() if len(effect) > 1 else ""
        if len(tail) < 4:
            comma = _COMMA_SPLIT.split(body, maxsplit=1)
            tail = comma[1].strip() if len(comma) > 1 else ""
        if len(tail) < 4:
            bad.append(f"{name}：{line}")
    assert bad == [], "索引条目缺少效果句：" + repr(bad)


def test_rule_dungeon_section_has_no_pointer_to_the_index():
    section = _rule_dungeon_section()
    for phrase in ("见《全道纹索引》", "以《全道纹索引》为准", "效果正文同样见"):
        assert phrase not in section, f"规则正文副本道纹节不得用指针「{phrase}」代替正文"


def test_rule_dungeon_section_bullets_carry_full_text():
    section = _rule_dungeon_section()
    bullets = re.findall(r"(?m)^- 【([\u4e00-\u9fff]{2})】(.*)$", section)
    assert bullets, "规则正文副本道纹节没有任何道纹条文"
    thin = [name for name, body in bullets if len(body.strip()) < 18]
    assert thin == [], f"规则正文副本道纹条文过短，疑似只剩支付：{thin}"


def test_dungeon_daowen_text_is_reachable_from_index_or_rules():
    """副本文件里的专属道纹，其条文必须能在索引或规则正文副本节读到。"""
    index_names = set(_index_entries())
    section = _rule_dungeon_section()
    rule_names = set(re.findall(r"(?m)^- 【([\u4e00-\u9fff]{2})】", section))
    missing = sorted(_dungeon_daowen_names() - index_names - rule_names)
    assert missing == [], f"这些道纹在任何正文里都读不到效果：{missing}"


def test_ai_context_daowen_digest_is_not_empty():
    text = AI_CONTEXT.read_text(encoding="utf-8")
    block = text.split("### 来源 4：机制速查·道纹", 1)[1].split("### 来源 5", 1)[0]
    assert "（暂无）" not in block, "AI 上下文的道纹速查整块丢失"
    lines = [line for line in block.splitlines() if "：支付" in line or "：代价" in line]
    assert len(lines) >= 40, f"道纹速查条目过少（{len(lines)}）"
