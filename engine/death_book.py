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
    """单页校验：〖遗言〗一句话，不超过字数上限（可留空——他最后什么也没说）；〖生平〗引擎生成。

    入参可以是纯字符串（只写遗言），也可以是 `{"text"/"life"/"title"}`。旧三段式字段一律拒绝——
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
    text = text.strip() if isinstance(text, str) else ""
    if text and len(text) > capacity:
        raise ValueError(f"遗言超过{capacity}字上限")

    normalized: dict[str, str] = {}
    if text:
        normalized[LEGACY_FIELD] = text
    title = legacy.get("title") if isinstance(legacy, dict) else None
    if isinstance(title, str) and title.strip():
        normalized["title"] = title.strip()
    life = legacy.get(LIFE_FIELD) if isinstance(legacy, dict) else None
    if isinstance(life, str) and life.strip():
        # 生平是自动总结的叙述，不限 20 字；只做一次长度钳制防止整段书页被撑爆。
        normalized[LIFE_FIELD] = life.strip()[:DEFAULT_LIFE_CAPACITY]
    if not normalized.get(LEGACY_FIELD) and not normalized.get(LIFE_FIELD):
        raise ValueError("一页至少要有一项：〖生平〗或〖遗言〗")
    return normalized


def summarize_life(state: Any, cause: str = "",
                   capacity: int = DEFAULT_LIFE_CAPACITY) -> str:
    """自动总结生平（2026-10-24 用户令：生平由引擎总结，遗言才由本人自定义）。

    只读引擎已有的**事实字段**：打到哪一场、在哪个副本、持几种道纹、带几件遗物、
    余多少碎片、死于什么。不写形容词，不编经历——叙事由玩家在外部补，这一行只保证
    「这一页确实属于这个人的这一趟轮回」。

    2026-10-25 用户令：**死因不是遗言**（「第4场连续五回合触发凡庸」只能算死因），
    所以死因进生平、遗言留给本人自己写；没有本人的话就不写遗言那一行。
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
    life = "，".join(bits)
    death_text = (CAUSE_DRAFTS.get(cause) or {}).get(LEGACY_FIELD, "")
    if death_text:
        life = f"{life}；{death_text}"
    return life[:capacity]


def draft_legacy(
    state: Any,
    cause: str = "",
    last_action: Optional[dict] = None,
    capacity: int = DEFAULT_CAPACITY,
) -> dict[str, str]:
    """起草一页：〖生平〗由引擎写，〖遗言〗留空等本人自定义。

    2026-10-25 用户令：**遗言不能由死因代填**——「第4场连续五回合触发凡庸」那只是死因，
    已经被 `summarize_life` 写进生平；遗言栏只放本人真的说过的话，没说过就留空
    （页面照写，只是没有遗言那一行）。旧版按死因模板拼「第N场+死因」的写法整段删除。
    `last_action` 只为兼容旧调用方保留，不再参与文案。
    """
    template: dict[str, str] = {}
    life = summarize_life(state, cause, DEFAULT_LIFE_CAPACITY)
    if life:
        template[LIFE_FIELD] = life

    player = getattr(state, "player", None)
    player_name = getattr(player, "name", "") if player is not None else ""
    region = getattr(state, "current_region", "") or ""
    battle = int(getattr(state, "current_battle", 0) or 0)
    title_bits = [bit for bit in (player_name, region, f"第{battle}场" if battle else "") if bit]
    if title_bits:
        template["title"] = "·".join(title_bits)
    return validate_legacy(template, capacity)


def render_legacy_section(entries: list[dict[str, str]]) -> str:
    if not entries:
        return f"{SECTION_HEADER}\n\n{EMPTY_MARK}\n"
    lines = [SECTION_HEADER, ""]
    for index, entry in enumerate(entries, 1):
        title = entry.get("title") or f"死者之页{index}"
        lines.append(f"### {title}")
        lines.append("")
        if entry.get(LIFE_FIELD):
            lines.append(f"- {LIFE_LABEL}：{entry[LIFE_FIELD]}")
        if entry.get(LEGACY_FIELD):
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
    """读回全部死者之页。新格式读「- 生平：」「- 遗言：」；旧三段式书页按「- 触发点：」折叠读入。

    2026-10-25 起遗言可以没有（他最后什么也没说）——只要页里有生平或遗言就成立。
    """
    _, section, _ = _split_legacy_section(text)
    if not section.strip() or (EMPTY_MARK in section and "### " not in section):
        return []
    labels = [(LEGACY_LABEL, LEGACY_FIELD),
              *[(label, field) for field, label in DEPRECATED_LABELS.items()]]
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if current.get(LEGACY_FIELD) or current.get(LIFE_FIELD):
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
