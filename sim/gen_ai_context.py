#!/usr/bin/env python3
"""生成第四宇宙剧情优先的混合 AI 上下文。

上下文保留世界观、角色名单、角色扮演与外部推演守则，并附机制速查：道纹、遗物、
法术与已实现副本各取名称与一行效果（2026-10-14 用户令：外派 AI 不知道具体道纹与
遗物就会胡说八道）。速查不替代原文——精确数值、边界与结算规则仍按需查询。

AI_CONTEXT.md 是生成文件，不是新的规则事实源。修改来源后运行本脚本；使用 --check
只检查生成物与当前来源是否逐字一致。
"""
from __future__ import annotations

import argparse
import hashlib
import posixpath
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.document_sources import AI_CONTEXT_FILE  # noqa: E402

OUTPUT = ROOT / AI_CONTEXT_FILE

# 未实现 / 草案 / 占位字样：命中即视为未来规划，整条（标题或段落单元）剔除。
FUTURE_WORDS: tuple[str, ...] = ("草案", "未实现", "未接入", "尚未接入", "占位", "待补")

# 所有来源共用的禁用短语：生成链路、一致性测试与数据表字段说明，不属于文字内容。
GLOBAL_DROP_PHRASES: tuple[str, ...] = (
    "AI_CONTEXT",
    "gen_rules",
    "gen_ai_context",
    "gen_daowen_index",
    "data/rules",
    "一致性测试",
    "引擎解析",
    "阈值算法",
    "进度口径",
    "死因文案",
    "重新生成本文件",
    "可读性镜像",
)

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
NUMERAL_RE = re.compile(r"^[一二三四五六七八九十]+、.+$")
UNIT_START_RE = re.compile(
    r"^\s*(?:#{1,6}\s|[-*+]\s|\d+[.、)]|>|\||[一二三四五六七八九十]+、)"
)
# 未加反引号的代码标识符（如 use_daowen、dodge_targets）。单字母数学变量（R_i、T_i）属于规则公式，不算。
CODE_IDENTIFIER_RE = r"[a-z]{2,}_[a-z_]+"
LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\(([^()\s\n]+)\)")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


@dataclass(frozen=True)
class ContextSource:
    """一个需要嵌入 AI_CONTEXT 的规范源（及其剔除规则）。"""

    role: str
    path: Path
    drop_headings: tuple[str, ...] = ()
    keep_headings: tuple[str, ...] | None = None
    drop_preamble: bool = False
    drop_phrases: tuple[str, ...] = ()

    @property
    def relative_path(self) -> str:
        return self.path.relative_to(ROOT).as_posix()


@dataclass(frozen=True)
class DigestSource:
    """机制速查来源：从既有索引摘取名称与一行效果，不打包原文全文。"""

    role: str
    path: str
    extractor: Callable[[Path, str], str]
    drop_phrases: tuple[str, ...] = ()
    count_label: str | None = None


def context_sources() -> list[ContextSource]:
    """剧情优先：嵌入世界观正典与 AI 知识库扮演规范。

    完整规则、副本机制与战报不批量打包；机制速查见 digest_sources()。
    """
    return [
        ContextSource(
            "世界观与角色档案（故事正典）",
            ROOT / "故事文档.md",
            keep_headings=("世界观与底层因果法则", "角色记忆档案"),
            drop_headings=("收录规则", "待核查区", "正文区"),
            drop_phrases=("战报正文区", "待核查区", "独立性声明", "战报.md", "收录规则", "不随引擎数值调整"),
        ),
        ContextSource(
            "AI 角色扮演约束（节选：职责、性格、扮演要求）",
            ROOT / "AI_EXPERIENCE.md",
            keep_headings=("职责划分", "角色性格塑造", "角色扮演要求", "外部推演守则"),
            drop_phrases=("引擎", "行为画像"),
        ),
    ]

def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ==================== 机制速查：结构化摘取（2026-10-14 用户令） ====================

_SECTION_RE = re.compile(r"^##\s+(.*\S)\s*$")
_SUBSECTION_RE = re.compile(r"^###\s+(.*\S)\s*$")
_RELIC_CHAPTERS = ("遗物池", "事件遗物", "怪物自带遗物", "血族遗物", "龙族遗物", "消耗品")


def _read_raw(root: Path, relpath: str) -> str:
    """读取速查来源；目录来源按文件名排序拼接（跳过下划线开头的模板）。"""
    path = root / relpath
    if path.is_dir():
        files = sorted(p for p in path.glob("*.md") if not p.name.startswith("_"))
        return "\n\n".join(
            f"# {p.name}\n{p.read_text(encoding='utf-8')}" for p in files)
    return path.read_text(encoding="utf-8")


def _clean_extracted(text: str, drop_phrases: tuple[str, ...]) -> str:
    out: list[str] = []
    for line in text.splitlines():
        cleaned = _clean_unit(line, drop_phrases)
        if cleaned is not None:
            out.append(cleaned)
    return "\n".join(out)


def _table_rows(raw: str, chapter: str) -> list[list[str]]:
    match = re.search(
        rf"^##\s+{re.escape(chapter)}\s*$(?P<body>.*?)(?=^##\s|\Z)",
        raw, re.MULTILINE | re.DOTALL)
    if not match:
        return []
    rows: list[list[str]] = []
    for line in match.group("body").splitlines():
        text = line.strip()
        if not text.startswith("|"):
            continue
        cells = [cell.strip() for cell in text.strip("|").split("|")]
        if not cells or set(cells[0]) <= {"-", ":", " "}:
            continue
        rows.append(cells)
    return rows


def extract_daowen(root: Path, relpath: str) -> str:
    """道纹速查：每条约「名称：一行效果」；未实现章节整体跳过。"""
    lines = _read_raw(root, relpath).splitlines()
    out: list[str] = []
    chapter = ""
    for index, line in enumerate(lines):
        section = _SECTION_RE.match(line)
        if section:
            chapter = section.group(1)
            continue
        heading = _SUBSECTION_RE.match(line)
        if not heading or any(word in chapter for word in FUTURE_WORDS):
            continue
        j = index + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            continue
        effect = lines[j].strip()
        if effect.startswith("X："):
            out.append(f"{heading.group(1)}：{effect[2:]}")
        elif effect.startswith("X（"):
            out.append(f"{heading.group(1)}：{effect[1:]}")
    return "\n".join(out)


def extract_actions(root: Path, relpath: str) -> str:
    """行动速查：整段照录《规则正文》「战斗行动」节。

    2026-10-24 用户令：AI 上下文必须能回答「这一手怎么打」，不能只列道纹名。
    条文只有一处正文（规则正文·战斗行动），这里整段摘录，不另写一份口径。
    """
    raw = _read_raw(root, relpath)
    match = re.search(
        r"^###\s+战斗行动\s*$(?P<body>.*?)(?=^###\s|^##\s|\Z)",
        raw, re.MULTILINE | re.DOTALL)
    if not match:
        return ""
    return "\n".join(line.strip() for line in match.group("body").splitlines()
                     if line.strip())


def extract_relics(root: Path, relpath: str) -> str:
    """物品速查：遗物与已登记消耗品；占位条目及分类标题跳过。"""
    lines = _read_raw(root, relpath).splitlines()
    out: list[str] = []
    chapter = ""
    for index, line in enumerate(lines):
        section = _SECTION_RE.match(line)
        if section:
            chapter = section.group(1)
            continue
        heading = _SUBSECTION_RE.match(line)
        if not heading or not any(key in chapter for key in _RELIC_CHAPTERS):
            continue
        name = heading.group(1)
        if name == "扭曲都市废墟设施工具库":
            continue
        if "?" in name or "占位" in name:
            continue
        parts: list[str] = []
        j = index + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        while j < len(lines) and len(parts) < 2:
            text = lines[j].strip()
            if not text or text.startswith("#") or text.startswith(">"):
                break
            parts.append(text)
            j += 1
        if parts:
            out.append(f"{name}：" + "".join(parts))
    return "\n".join(out)


def extract_spells(root: Path, relpath: str) -> str:
    """法术速查：法术大全表格 → 每式一行（所需道纹与生效流程）。"""
    rows = _table_rows(_read_raw(root, relpath), "三、法术大全")
    out = [
        f"{cells[0]}：所需道纹 {cells[1]}｜{cells[3]}"
        for cells in rows if len(cells) >= 4 and cells[0] != "法术"
    ]
    return "\n".join(out)


def extract_regions(root: Path, relpath: str) -> str:
    """副本速查：只列已实现副本（草案副本不进入游戏流程）。"""
    rows = _table_rows(_read_raw(root, relpath), "已实现副本")
    out = [
        f"{cells[0]}（{cells[1]}，属性点{cells[2]}，道纹{cells[3]}）"
        for cells in rows if len(cells) >= 4 and cells[0] != "副本"
    ]
    return "\n".join(out)


def extract_roster(root: Path, relpath: str) -> str:
    """角色名单：只取每人记忆档的身份一行；完整记忆是独立文件，不在此汇总。"""
    directory = root / relpath
    if not directory.is_dir():
        return ""
    out: list[str] = []
    for path in sorted(directory.glob("*.md")):
        if path.name.startswith("_"):
            continue
        name = path.stem
        text = path.read_text(encoding="utf-8")
        identity = next(
            (line.split("：", 1)[1].strip()
             for line in text.splitlines()
             if line.startswith("- 身份：") and "：" in line),
            "")
        out.append(f"{name}：{identity}" if identity else f"{name}：身份待录")
    return "\n".join(out)


def digest_sources() -> list[DigestSource]:
    """机制速查与角色名单：只摘名称与一行效果，供外部 AI 避免凭空编造。"""
    return [
        DigestSource("机制速查·行动", "规则正文.md", extract_actions),
        DigestSource("机制速查·道纹", "全道纹索引.md", extract_daowen, count_label="条"),
        DigestSource("机制速查·物品", "物品索引.md", extract_relics, count_label="件"),
        DigestSource("机制速查·法术", "法术索引.md", extract_spells, count_label="式"),
        DigestSource("机制速查·副本", "副本索引.md", extract_regions, count_label="个"),
        DigestSource("角色名单（只列身份，不含记忆）", "角色记忆", extract_roster, count_label="人"),
    ]


def context_blocks() -> list[tuple[str, str, str, str]]:
    """全部来源块：(角色, 相对路径, 过滤后正文, 原始文本哈希)。"""
    sources = context_sources()
    digests = digest_sources()
    included = frozenset([source.relative_path for source in sources] +
                         [digest.path for digest in digests])
    loaded: list[tuple[str, str, str, str]] = []
    for source in sources:
        raw = source.path.read_text(encoding="utf-8").rstrip()
        loaded.append((source.role, source.relative_path,
                       distill_source(source, included), _digest(raw)))
    for digest in digests:
        raw = _read_raw(ROOT, digest.path)
        body = _clean_extracted(digest.extractor(ROOT, digest.path), digest.drop_phrases)
        if not body:
            body = "（暂无）"
        elif digest.count_label:
            body = f"（共 {len(body.splitlines())} {digest.count_label}）\n{body}"
        loaded.append((digest.role, digest.path, body, _digest(raw)))
    return loaded


def _safe_fence(text: str) -> str:
    """返回比正文任何连续反引号都长的 fenced-code 分隔符（保持区块完整）。"""
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _level(line: str) -> int | None:
    """标题层级：Markdown 标题为 # 数；中文序号行（「六、…」）视作二级边界。"""
    match = HEADING_RE.match(line)
    if match:
        return len(match.group(1))
    if NUMERAL_RE.match(line) and len(line) <= 40:
        return 2
    return None


def _title(line: str) -> str:
    match = HEADING_RE.match(line)
    return match.group(2).strip() if match else line.strip()


def _strip_fences(lines: list[str]) -> list[str]:
    out: list[str] = []
    fence: str | None = None
    for line in lines:
        match = FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)
                continue
            out.append(line)
        elif match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
            fence = None
    return out


def _keep_sections(lines: list[str], titles: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        level = _level(lines[i])
        if level is not None and _title(lines[i]) in titles:
            j = i + 1
            while j < n and not (_level(lines[j]) is not None and _level(lines[j]) <= level):
                j += 1
            out.extend(lines[i:j])
            i = j
        else:
            i += 1
    return out


def _drop_sections(lines: list[str], titles: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        level = _level(lines[i])
        if level is not None and _title(lines[i]) in titles:
            j = i + 1
            while j < n and not (_level(lines[j]) is not None and _level(lines[j]) <= level):
                j += 1
            i = j
        else:
            out.append(lines[i])
            i += 1
    return out


def _drop_preamble(lines: list[str]) -> list[str]:
    first = next((i for i, line in enumerate(lines) if line.startswith("## ")), len(lines))
    head = [line for line in lines[:first] if line.startswith("# ")]
    return head + [""] + lines[first:] if head else lines[first:]


def _drop_future_leaf_blocks(lines: list[str]) -> list[str]:
    """三级及以下标题的条目若含未实现/草案/占位字样，整条（含标题）剔除。"""
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        match = HEADING_RE.match(lines[i])
        if match and len(match.group(1)) >= 3:
            j = i + 1
            while j < n and _level(lines[j]) is None:
                j += 1
            block = "\n".join(lines[i:j])
            if any(word in block for word in FUTURE_WORDS):
                i = j
                continue
        out.append(lines[i])
        i += 1
    return out


SENTENCE_END_RE = re.compile(r"[。！？；]\s*$")


def _group_units(lines: list[str]) -> list[list[str] | None]:
    """把行切成段落单元；空行记为 None（段落分隔）。

    单元在空行、列表/标题/表格起始行、以及句末标点（。！？；）之后断开，使一句含代码时
    只剔除该句，而不连带同段的其他规则句。
    """
    units: list[list[str] | None] = []
    current: list[str] | None = None
    for line in lines:
        if not line.strip():
            units.append(None)
            current = None
        elif current is None or UNIT_START_RE.match(line) or SENTENCE_END_RE.search(current[-1]):
            current = [line]
            units.append(current)
        else:
            current.append(line)
    return units


def _is_prose_span(span: str) -> bool:
    """纯中文/符号文字（无 ASCII 字母数字与代码符号）才算可保留的正文。"""
    return bool(span.strip()) and not re.search(r"[A-Za-z0-9_=<>{}\"'/\\|.+*#%@`]", span)


def _clean_unit(text: str, drop_phrases: tuple[str, ...]) -> str | None:
    """处理一个段落单元：含代码则整体剔除；纯文字行内代码去掉反引号；命中禁用词则剔除。"""

    def paren(match: re.Match[str]) -> str:
        return match.group(0) if _is_prose_span(match.group(1)) else ""

    if re.fullmatch(r"\s*>?\s*", text):
        return None
    text = re.sub(r"[（(]\s*`([^`\n]+)`\s*[）)]", paren, text)
    spans = re.findall(r"`([^`\n]+)`", text)
    if any(not _is_prose_span(span) for span in spans):
        return None
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    if re.search(CODE_IDENTIFIER_RE, text):
        return None
    if any(word in text for word in FUTURE_WORDS):
        return None
    if any(phrase in text for phrase in GLOBAL_DROP_PHRASES + drop_phrases):
        return None
    return text


def _resolve_links(text: str, source_rel: str, included: frozenset[str]) -> str:
    """指向已收录文件的链接保留；指向未收录文件（README、报告、被剔除的源）的降为文字。"""

    def repl(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        if re.match(r"^[a-z]+://", target):
            return match.group(0)
        path_part = target.split("#", 1)[0]
        if not path_part:
            return label
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_rel), path_part))
        return match.group(0) if resolved in included else label

    return LINK_RE.sub(repl, text)


def distill_source(source: ContextSource, included: frozenset[str]) -> str:
    """按 ContextSource 的规则生成过滤后的正文（纯函数，供生成与测试共用）。"""
    raw = source.path.read_text(encoding="utf-8").rstrip()
    lines = _strip_fences(raw.splitlines())
    if source.keep_headings is not None:
        lines = _keep_sections(lines, source.keep_headings)
    if source.drop_preamble:
        lines = _drop_preamble(lines)
    lines = _drop_sections(lines, source.drop_headings)
    lines = _drop_future_leaf_blocks(lines)

    out: list[str] = []
    for unit in _group_units(lines):
        if unit is None:
            if out and out[-1] != "":
                out.append("")
            continue
        cleaned = _clean_unit("\n".join(unit), source.drop_phrases)
        if cleaned is not None:
            out.extend(cleaned.split("\n"))

    text = "\n".join(out).strip()
    text = _resolve_links(text, source.relative_path, included)
    return re.sub(r"\n{3,}", "\n\n", text)


def render_context() -> str:
    """渲染全文；纯函数、无时间戳，保证 --check 可以逐字比较。"""
    for source in context_sources():
        if not source.path.is_file():
            raise FileNotFoundError(f"AI_CONTEXT 来源不存在：{source.relative_path}")
    loaded = context_blocks()

    lines = [
        "# 第四宇宙 · AI 上下文",
        "",
        "> **生成文件，禁止手改。** 源文件变更后运行 `python3 sim/gen_ai_context.py`；",
        "> 只检查一致性则运行 `python3 sim/gen_ai_context.py --check`。",
        ">",
        "> 此文件是剧情优先的混合上下文，供只抓取一个 raw URL 的外部 AI 使用。",
        "> 它提供世界观、角色名单、角色扮演与外部推演守则，并附机制速查：战斗行动（怎么出手）、道纹、遗物、法术与已实现副本。",
        "> 速查只保证名称与效果方向；精确数值、边界与结算规则仍以对应原文为准，按需查询。",
        "> 本文件不是新的规则事实源，也不改变角色记忆与规则正本之间的优先级。",
        "",
        "## 使用契约（先读）",
        "",
        "1. **裁定优先级**：用户最新裁定高于一切；涉及精确结算时，必须按需查询《规则正文》及对应机制原文，不得把本文件当作完整规则。",
        "2. **剧情优先**：优先维持世界观因果、人物性格、既有羁绊、当前认知、主线目标与未解悬念；不要用无关机制资料淹没剧情。",
        "3. **角色记忆不等于客观规则**：角色可能误解、遗漏或持有过时认知；记忆中的说法可作为叙事事实，但不能覆盖规则正本。",
        "4. **最小必要规则**：只把理解剧情所需的稳定原则放在这里。世界遵循因果而非宿命，选择会带来后果，力量与解法可能伴随代价；具体数值、触发条件、目标限制和时序按需查原文。",
        "5. **死者之书的遗言不是规则**：遗言可能属于具体角色且可能有真假；不能把角色遗言自动当成客观规则。",
        "6. **副本状态要区分**：故事中提到的副本、能力或线索不自动证明其机制已实现；涉及副本规则时，按需查询对应副本文档。",
        "7. **单角色推演**：外部 AI 只扮演当前角色，按《外部推演守则》输出该角色的行动与台词；其余角色与环境由用户提供。",
        "",
        "## 单文件范围",
        "",
        f"- 已收录 {len(loaded)} 份来源：世界观与角色记忆正典、AI 知识库（职责/性格/扮演要求/外部推演守则节选）、机制速查（行动/道纹/遗物/法术/副本）与角色名单。",
        "- 不整批收录：通用规则正文、完整推演规范、副本机制细节与怪物池、战报、代码和开发资料。",
        "- 来源区块附原始文件 SHA-256 指纹，便于判断内容是否过期；区块正文经过筛选，**不得**反写来源。",
        "",
        "## 来源目录与校验指纹",
        "",
        "| # | 角色 | 原始文件 | SHA-256 |",
        "| --- | --- | --- | --- |",
    ]
    for index, (role, relative_path, _, digest) in enumerate(loaded, 1):
        # 目录来源（角色名单）不生成 Markdown 链接：仓内校验器只校验文件级链接。
        target = (f"{relative_path}/（目录）" if (ROOT / relative_path).is_dir()
                  else f"[{relative_path}]({relative_path})")
        lines.append(f"| {index} | {role} | {target} | `{digest}` |")

    lines.extend([
        "",
        "## 打包正文",
        "",
        "> 每一块为来源文件经过滤后的文字；机制速查为结构化摘录，不是原文全文（战斗行动为整段照录）。",
        "> 正文采用代码块呈现，以防嵌套相对链接在此聚合文件内被错误解析。",
    ])
    for index, (role, relative_path, body, digest) in enumerate(loaded, 1):
        fence = _safe_fence(body)
        lines.extend([
            "", f"### 来源 {index}：{role}", "",
            f"- 路径：`{relative_path}`",
            f"- SHA-256：`{digest}`", "",
            f"{fence}markdown", body, fence,
        ])
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="检查生成物，不写文件")
    args = parser.parse_args(argv)
    rendered = render_context()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != rendered:
            print("AI_CONTEXT.md 与来源不一致，请运行 python3 sim/gen_ai_context.py", file=sys.stderr)
            return 1
        print("AI_CONTEXT.md is up to date")
        return 0
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"written {OUTPUT}: {len(rendered.encode('utf-8')):,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
