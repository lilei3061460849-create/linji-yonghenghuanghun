"""《死者之书》的死页：文件是唯一事实源。

只读写 `死者之书.md` 的「## 死者之页」节，不得改动「## 法术大全」。
新增一页只需往该节追加，不必改引擎代码。

**2026-10-24 用户令：原【死之传承】改名【死者之页】**——每页两项：
  * 〖生平〗由引擎自动总结（`summarize_life`，只用事实字段，不编故事）；
  * 〖遗言〗由本人自定义一句话，上限 20 字。
旧书的「## 遗言」节仍按原样读入（存量页不至于读不出来），写入一律走新节的格式。
**DM裁定 2026-08-31：遗言不再三段式**——旧「触发点／岔路／代价预算」字段只读不写。
"""
from __future__ import annotations
from .rules_source import lethal_events

import re
from pathlib import Path
from typing import Any, Optional


LEGACY_FIELD = "text"
LIFE_FIELD = "life"
LEGACY_FIELDS = (LEGACY_FIELD,)
FIELD_LABELS = {LEGACY_FIELD: "遗言", LIFE_FIELD: "生平"}
LEGACY_LABEL = "遗言"
LIFE_LABEL = "生平"
# 旧三段式字段：只用于读取旧书页／兼容旧调用方，不再写出。
DEPRECATED_FIELDS = ("trigger_point", "fork", "cost_budget")
DEPRECATED_LABELS = {"trigger_point": "触发点", "fork": "岔路", "cost_budget": "代价预算"}
DEFAULT_CAPACITY = 20
# 生平由引擎自动总结，不受遗言那 20 字约束；这里只防一句话长到无法阅读。
DEFAULT_LIFE_CAPACITY = 120
SECTION_HEADER = "## 死者之页"
# 旧书用「## 遗言」作节名，读取时一并认；写入一律用新节名。
SECTION_HEADERS = (SECTION_HEADER, "## 遗言")
EMPTY_MARK = "当前没有死者之页。"

# 可扩展草稿表：新增死因只需加一条，流程代码不用改。
# 可扩展草稿表：新增死因只需加一条，流程代码不用改。
# 2026-10-08：致死类特殊事件（迷失/癌变/凡庸）的文案不再是本地常量，事实源移到
# data/rules/lethal_events.toml（各自条目的 death_cause_text），与引擎阈值、
# 进度格式串同源——改一处即可，三处（引擎行为/面板进度/死因文案）跟着变。
CAUSE_DRAFTS: dict[str, dict[str, str]] = {
    "attack": {"text": "受到致死攻击命零"},
    "collapse": {"text": lethal_events.death_cause_text("mishi")},
    "mediocrity": {"text": lethal_events.death_cause_text("fanyong")},
    "duel": {"text": "最终死斗落败"},
    "bleed": {"text": "代价流血导致命零"},
    "cancer": {"text": lethal_events.death_cause_text("aibian")},
    "echo_error": {"text": "回音长廊安魂曲", "title": "错误遗言"},
}


def clip_text(value: str, limit: int = DEFAULT_CAPACITY) -> str:
    text = (value or "").strip()
    if not text:
        return "未记录"
    return text[:limit]


def _collapse_legacy(legacy: Any) -> str:
    """从旧三段式对象里取出可当单句遗言的那一句（优先触发点）。"""
    if isinstance(legacy, str):
        return legacy
    if not isinstance(legacy, dict):
        return ""
    if isinstance(legacy.get(LEGACY_FIELD), str):
        return legacy[LEGACY_FIELD]
    for field_name in DEPRECATED_FIELDS:
        value = legacy.get(field_name)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def validate_legacy(legacy: Any, capacity: int = DEFAULT_CAPACITY) -> dict[str, str]:
    """单句校验：一句话，非空，不超过字数上限。

    入参可以是纯字符串，也可以是 `{"text": ...}`（+可选 title）。旧三段式字段一律拒绝——
    格式只有一种；存量旧书页的兼容放在**读取端**（`parse_legacies` 按第一句折叠）。
    """
    allowed_extra = {"title", "action", "option", LIFE_FIELD}
    if isinstance(legacy, dict):
        if set(legacy) & set(DEPRECATED_FIELDS):
            raise ValueError(
                "遗言已改为单句：只提交 text（≤"
                f"{capacity}字），旧三段式 trigger_point/fork/cost_budget 已废止")
        extra = set(legacy) - {LEGACY_FIELD} - allowed_extra
        if extra:
            raise ValueError(
                f"死者之页只能包含 {LEGACY_FIELD}/{LIFE_FIELD}(+title)，收到多余字段: {sorted(extra)}")
    elif not isinstance(legacy, str):
        raise ValueError("遗言必须是一句话（字符串）或包含 text 的对象")

    text = _collapse_legacy(legacy)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("遗言必须是非空字符串")
    text = text.strip()
    if len(text) > capacity:
        raise ValueError(f"遗言超过{capacity}字上限")

    normalized: dict[str, str] = {LEGACY_FIELD: text}
    title = legacy.get("title") if isinstance(legacy, dict) else None
    if isinstance(title, str) and title.strip():
        normalized["title"] = title.strip()
    life = legacy.get(LIFE_FIELD) if isinstance(legacy, dict) else None
    if isinstance(life, str) and life.strip():
        # 生平是自动总结的叙述，不限 20 字；只做一次长度钳制防止整段书页被撑爆。
        normalized[LIFE_FIELD] = life.strip()[:DEFAULT_LIFE_CAPACITY]
    return normalized


def summarize_life(state: Any, capacity: int = DEFAULT_LIFE_CAPACITY) -> str:
    """自动总结生平（2026-10-24 用户令：生平由引擎总结，遗言才由本人自定义）。

    只读引擎已有的**事实字段**：打到哪一场、在哪个副本、持几种道纹、带几件遗物、
    余多少碎片。不写形容词，不编经历——叙事由玩家在外部补，这一行只保证「这一页
    确实属于这个人的这一趟轮回」。
    """
    player = getattr(state, "player", None)
    if player is None:
        return ""
    bits: list[str] = []
    region = (getattr(state, "current_region", "") or "").strip()
    battle = int(getattr(state, "current_battle", 0) or 0)
    if region:
        bits.append(f"在{region}")
    if battle > 0:
        bits.append(f"打到第{battle}场")
    dao_wen = list(getattr(player, "dao_wen", {}) or {})
    if dao_wen:
        bits.append(f"持道纹{len(dao_wen)}种")
    relics = list(getattr(state, "relics", []) or [])
    if relics:
        bits.append(f"携遗物{len(relics)}件")
    shards = int(getattr(state, "shards", 0) or 0)
    bits.append(f"余{shards}碎片")
    return "，".join(bits)[:capacity]


def draft_legacy(
    state: Any,
    cause: str,
    last_action: Optional[dict] = None,
    capacity: int = DEFAULT_CAPACITY,
) -> dict[str, str]:
    """按死因模板生成一句不超过字数上限的遗言草稿。

    DM裁定 2026-08-31 后每页只有一句话，所以草稿只保留「第N场 + 死因短句」；
    旧三段式里的岔路/代价预算（含按资源状态改写的那套）随格式一起废止。
    `last_action` 仅用于在字数允许时补一句最后动作，不再单独成段。
    """
    template = dict(CAUSE_DRAFTS.get(cause) or CAUSE_DRAFTS["attack"])
    battle = int(getattr(state, "current_battle", 0) or 0)
    text = template[LEGACY_FIELD]
    if battle > 0:
        prefix = f"第{battle}场"
        if len(prefix + text) <= capacity:
            text = prefix + text
    if last_action:
        params = last_action.get("params") or {}
        hint = ""
        if last_action.get("action") == "use_daowen":
            hint = f"末手{params.get('daowen_name', '道纹')}"
        elif last_action.get("action") == "declare_escape":
            hint = "试图逃跑"
        if hint and len(text) + len(hint) <= capacity:
            text = f"{text}{hint}"
    template[LEGACY_FIELD] = clip_text(text, capacity)

    life = summarize_life(state, DEFAULT_LIFE_CAPACITY)
    if life:
        template[LIFE_FIELD] = life

    player = getattr(state, "player", None)
    player_name = getattr(player, "name", "") if player is not None else ""
    region = getattr(state, "current_region", "") or ""
    title_bits = [bit for bit in (player_name, region, f"第{battle}场" if battle else "") if bit]
    if template.get("title"):
        pass
    elif title_bits:
        template["title"] = "·".join(title_bits)
    return validate_legacy(template, capacity)


def render_legacy_section(entries: list[dict[str, str]]) -> str:
    if not entries:
        return f"{SECTION_HEADER}\n\n{EMPTY_MARK}\n"
    lines = [SECTION_HEADER, ""]
    for index, entry in enumerate(entries, 1):
        title = entry.get("title") or f"遗言{index}"
        lines.append(f"### {title}")
        lines.append("")
        if entry.get(LIFE_FIELD):
            lines.append(f"- {LIFE_LABEL}：{entry[LIFE_FIELD]}")
        lines.append(f"- {LEGACY_LABEL}：{entry[LEGACY_FIELD]}")
        lines.append("")
    return "\n".join(lines)


def _split_legacy_section(text: str) -> tuple[str, str, str]:
    match = None
    for header in SECTION_HEADERS:
        match = re.search(rf"^{re.escape(header)}\s*$", text, re.MULTILINE)
        if match:
            break
    if not match:
        return text.rstrip() + "\n\n", "", ""
    prefix = text[: match.start()]
    rest = text[match.start() :]
    next_heading = re.search(r"\n## ", rest[1:])
    if next_heading:
        cut = 1 + next_heading.start()
        return prefix, rest[:cut].rstrip() + "\n", rest[cut:]
    return prefix, rest, ""


def parse_legacies(text: str) -> list[dict[str, str]]:
    """读回全部遗言页。新格式读「- 遗言：」；旧三段式书页按「- 触发点：」折叠读入。"""
    _, section, _ = _split_legacy_section(text)
    if not section.strip() or (EMPTY_MARK in section and "### " not in section):
        return []
    labels = [(LEGACY_LABEL, LEGACY_FIELD),
              *[(label, field) for field, label in DEPRECATED_LABELS.items()]]
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if current.get(LEGACY_FIELD):
            entries.append(dict(current))

    for raw in section.splitlines():
        line = raw.strip()
        if line.startswith("### "):
            flush()
            current = {"title": line[4:].strip()}
            continue
        if line.startswith(f"- {LIFE_LABEL}：") or line.startswith(f"- {LIFE_LABEL}:"):
            current[LIFE_FIELD] = line.split("：", 1)[-1].split(":", 1)[-1].strip()
            continue
        for label, field in labels:
            if line.startswith(f"- {label}：") or line.startswith(f"- {label}:"):
                value = line.split("：", 1)[-1].split(":", 1)[-1].strip()
                # 旧书页的三段按顺序读到，只保留第一句（触发点优先）
                if field == LEGACY_FIELD or LEGACY_FIELD not in current:
                    current[LEGACY_FIELD] = value
                break
    flush()
    return entries


class DeathBookStore:
    """《死者之书.md》遗言节的读写器。path 可换成测试用临时文件。"""

    def __init__(self, path: str | Path = "死者之书.md"):
        self.path = Path(path)

    def load(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        return parse_legacies(self.path.read_text(encoding="utf-8"))

    def write_all(self, entries: list[dict[str, str]]) -> list[dict[str, str]]:
        normalized = [validate_legacy(entry) for entry in entries]
        if self.path.exists():
            text = self.path.read_text(encoding="utf-8")
        else:
            text = "# 死者之书\n\n"
        prefix, _, suffix = _split_legacy_section(text)
        rendered = render_legacy_section(normalized)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(prefix + rendered + (("\n" + suffix.lstrip("\n")) if suffix else ""),
                             encoding="utf-8")
        return self.load()

    def append(self, legacy: dict[str, str] | str) -> dict[str, str]:
        validated = validate_legacy(legacy)
        entries = self.load()
        entries.append(validated)
        self.write_all(entries)
        return validated

    def remove_at(self, index: int) -> Optional[dict[str, str]]:
        """按 0-based 下标删除一页。越界返回 None，文件不变。"""
        entries = self.load()
        if index < 0 or index >= len(entries):
            return None
        removed = entries.pop(index)
        self.write_all(entries)
        return removed

    def remove_by_title(self, title: str) -> Optional[dict[str, str]]:
        """按标题删除第一页同名遗言。找不到返回 None，文件不变。"""
        want = (title or "").strip()
        if not want:
            return None
        entries = self.load()
        for i, entry in enumerate(entries):
            if (entry.get("title") or "").strip() == want:
                removed = entries.pop(i)
                self.write_all(entries)
                return removed
        return None
