"""项目 Markdown 结构、文件链接与标题锚点校验。"""
from __future__ import annotations
from pathlib import Path
import re
from urllib.parse import unquote

LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")

AI_KNOWLEDGE_MAPPING = "用户提到的“AI 知识库”指仓库根目录的 `AI_EXPERIENCE.md`"
AI_POLICY_HEADING = "软件工程实现与验证准则"
AI_POLICY_RULE_MARKERS = (
    "不得编造",
    "我无法验证/无法运行",
    "完成声明必须有证据",
    "有歧义先停止并提问",
    "禁止用简化实现冒充完成",
    "输出使用固定结构",
    "新增功能必须有三类测试",
    "结论必须由验证支撑",
    "可自定义能力必须由数据驱动",
    "这不是自定义系统，只是固定实现",
    "发现规则或架构冲突时停止",
    "及时维护知识库",
)
AI_DELIVERY_SECTIONS = (
    "(1) 需求复述",
    "(2) 不确定点清单 + 必问问题",
    "(3) 设计方案",
    "(4) 实现计划",
    "(5) 交付",
    "(6) 测试",
    "(7) 进度与限制",
)
AI_DEDUCTION_HEADING = "推演铁律"
AI_DEDUCTION_RULE_MARKERS = (
    "只能取自引擎真实返回值",
    "AI 无权书写、改写或预写任何结局",
    "禁止结论先行",
    "禁止掺入叙事与文学描写",
    "后台发生的事件不得遗漏，后台没有的事件不得新增",
    "禁止复制同一段模板叙事",
    "只保留后台数据",
)
AI_WRITING_HEADING = "行文十诫"
AI_WRITING_RULE_MARKERS = (
    "严禁杜撰",
    "先否定后肯定",
    "三词并列",
    "高频词不得置于句首",
    "句尾不得拔高",
    "长短句混用",
    "一篇至多一两处",
    "毫无疑问",
    "堵死反驳",
    "假精确",
)
STALE_KNOWLEDGE_HEADING = re.compile(
    r"^#{2,6}\s+.*(?:过时|作废|已删除|已修复|已解决|补全进度|追记|"
    r"实测数据|测试通过|问题\s*/\s*方法\s*/\s*结果).*$",
    re.MULTILINE,
)

# 类型定义统一由《规则正文·基础定义》维护；此处只检查知识库的资格/转换标准。
# 不要求在知识库复制四类定义，更不能用旧版X措辞覆盖现行“目标数量”。
RESOURCE_DESIGN_HEADING = "【二、资源设计标准】"
RESOURCE_DESIGN_MARKERS = (
    "现有资源无论如何组合都无法实现的效果",
    "消耗品不受此限制",
    "凡是不符合自身类型定义的资源，应转换为其他符合其自身定义的资源类型",
    "效果强度过高时，优先转换为消耗品",
    "效果强度过低时，优先转换为遗物",
    "当场设计",
)


def github_anchor(heading: str) -> str:
    """生成项目当前标题所需的 GitHub 风格锚点。"""
    heading = re.sub(r"<[^>]+>", "", heading)
    heading = re.sub(r"[`*_~]", "", heading).strip().lower()
    heading = "".join(ch for ch in heading if ch.isalnum() or ch in " -_")
    return re.sub(r"\s+", "-", heading)


def _document_anchors(path: Path) -> set[str]:
    anchors = set()
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = HEADING.match(line)
        if not match:
            continue
        base = github_anchor(match.group(2))
        suffix = counts.get(base, 0)
        counts[base] = suffix + 1
        anchors.add(base if suffix == 0 else f"{base}-{suffix}")
    return anchors


def validate_ai_knowledge_base(path: str | Path) -> dict:
    """校验 AI 知识库定位、12 条工程准则、交付顺序、推演铁律、行文十诫和废案标题。"""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"AI知识库校验失败：文件不存在：{path}")

    text = path.read_text(encoding="utf-8")
    errors = []
    first_nonempty = next((line for line in text.splitlines() if line.strip()), "")
    if first_nonempty != "# AI经验库":
        errors.append("一级标题必须是“# AI经验库”")
    if AI_KNOWLEDGE_MAPPING not in text:
        errors.append("缺少 AI 知识库到 AI_EXPERIENCE.md 的明确映射")

    section = re.search(
        rf"^##\s+{re.escape(AI_POLICY_HEADING)}\s*$\n(?P<body>.*?)(?=^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    policy_body = section.group("body") if section else ""
    if section is None:
        errors.append(f"缺少“## {AI_POLICY_HEADING}”章节")
    else:
        rule_numbers = re.findall(r"^(\d+)\.\s", policy_body, re.MULTILINE)
        expected_numbers = [str(number) for number in range(1, 13)]
        if rule_numbers != expected_numbers:
            errors.append(f"工程准则编号必须严格为1~12，实际为：{rule_numbers}")
        for marker in AI_POLICY_RULE_MARKERS:
            if marker not in policy_body:
                errors.append(f"工程准则缺少关键要求：{marker}")

        delivery_sections = [
            f"({number}) {label.strip()}"
            for number, label in re.findall(
                r"^\s+-\s+\((\d)\)\s+(.+?)\s*$", policy_body, re.MULTILINE
            )
        ]
        if tuple(delivery_sections) != AI_DELIVERY_SECTIONS:
            errors.append(
                "固定交付结构必须完整且顺序为：" + " -> ".join(AI_DELIVERY_SECTIONS)
            )

    deduction = re.search(
        rf"^###\s+{re.escape(AI_DEDUCTION_HEADING)}\s*$\n(?P<body>.*?)(?=^#{2,3}\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if deduction is None:
        errors.append(f"缺少“### {AI_DEDUCTION_HEADING}”章节")
    else:
        for marker in AI_DEDUCTION_RULE_MARKERS:
            if marker not in deduction.group("body"):
                errors.append(f"推演铁律缺少关键要求：{marker}")

    writing = re.search(
        rf"^###\s+{re.escape(AI_WRITING_HEADING)}\s*$\n(?P<body>.*?)(?=^#{2,3}\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if writing is None:
        errors.append(f"缺少“### {AI_WRITING_HEADING}”章节")
    else:
        for marker in AI_WRITING_RULE_MARKERS:
            if marker not in writing.group("body"):
                errors.append(f"行文十诫缺少关键要求：{marker}")

    stale_headings = STALE_KNOWLEDGE_HEADING.findall(text)
    if stale_headings:
        errors.append("知识库仍含过期/已解决流水账标题：" + "；".join(stale_headings))

    design = re.search(
        rf"^{re.escape(RESOURCE_DESIGN_HEADING)}\s*$\n(?P<body>.*?)(?=^【|^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if design is None:
        errors.append(f"缺少「{RESOURCE_DESIGN_HEADING}」资源设计标准章节")
    else:
        for marker in RESOURCE_DESIGN_MARKERS:
            if marker not in design.group("body"):
                errors.append(f"资源设计标准缺少关键要求：{marker}")

    if '《规则正文》「基础定义」' not in text:
        errors.append('缺少资源类型定义指向《规则正文》「基础定义」的事实源声明')

    if errors:
        raise ValueError("AI知识库校验失败：\n" + "\n".join(errors))
    return {
        "rules": 12,
        "delivery_sections": len(AI_DELIVERY_SECTIONS),
        "stale_headings": 0,
    }


def validate_markdown_documents(root: str | Path) -> dict:
    """校验全部 Markdown；发现无H1、失效文件链接或失效锚点时抛出 ValueError。"""
    root = Path(root).resolve()
    ignored_parts = {".git", ".venv", ".pytest_cache", "__pycache__", "node_modules"}
    documents = sorted(
        path for path in root.rglob("*.md")
        if not ignored_parts.intersection(path.parts)
    )
    anchors = {path.resolve(): _document_anchors(path) for path in documents}
    errors = []
    link_count = 0

    for path in documents:
        text = path.read_text(encoding="utf-8")
        first_nonempty = next((line for line in text.splitlines() if line.strip()), "")
        if not first_nonempty.startswith("# "):
            errors.append(f"{path.relative_to(root)}: 缺少一级标题")

        in_fence = False
        for line_number, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for match in LINK.finditer(line):
                target = match.group(2).strip()
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                link_count += 1
                file_part, separator, fragment = target.partition("#")
                destination = (path.parent / unquote(file_part)).resolve() if file_part else path.resolve()
                if not destination.is_file():
                    errors.append(
                        f"{path.relative_to(root)}:{line_number}: 链接文件不存在：{target}")
                    continue
                if separator and destination.suffix.lower() == ".md":
                    wanted = github_anchor(unquote(fragment))
                    if wanted not in anchors.get(destination, set()):
                        errors.append(
                            f"{path.relative_to(root)}:{line_number}: 链接锚点不存在：{target}")

    if errors:
        raise ValueError("Markdown文档校验失败：\n" + "\n".join(errors))

    knowledge_path = root / "AI_EXPERIENCE.md"
    if knowledge_path.is_file():
        validate_ai_knowledge_base(knowledge_path)
    return {"documents": len(documents), "links": link_count}
