"""AI_EXPERIENCE.md 当前工程准则与协作规范契约。"""
from pathlib import Path
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.document_validation import validate_ai_knowledge_base

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "AI_EXPERIENCE.md"


def test_ai_knowledge_base_contains_current_engineering_and_collaboration_policy():
    result = validate_ai_knowledge_base(KNOWLEDGE)
    assert result == {"rules": 12, "delivery_sections": 7, "stale_headings": 0}
    text = KNOWLEDGE.read_text(encoding="utf-8")
    assert "## AI开发协作规范" in text
    assert "### 1. 游戏规则优先于架构统一" in text
    for marker in (
        "## AI角色扮演、协作规范与推演铁律",
        "### 职责划分",
        "### 角色性格塑造",
        "### 外部推演守则",
        "### 行文十诫",
        "### 推演铁律",
        "## 战斗推演原子流水线与典型实战示例",
        "### 白刃战双操推演模式",
        "## 现行手操经验",
        "手操铁律",
        "死锁判卫冕",
        "当前固定2次",
        "Arena 固定分支",
        "默认只保留 `main`",
        "最终死斗触发条件裁定的沿革",
        "## 当前执行约束与已确认结论（2026-10-19）",
        "## 实验与变更档案",
        "## 实验灾备协议",
        "seed=901",
        "四项并行任务",
    ):
        assert marker not in text, marker


def test_missing_collaboration_rule_is_rejected(tmp_path):
    text = KNOWLEDGE.read_text(encoding="utf-8")
    invalid = text.replace("**尊重授权与事实源**", "**已删除的协作原则**", 1)
    assert invalid != text
    path = tmp_path / "AI_EXPERIENCE.md"
    path.write_text(invalid, encoding="utf-8")
    with pytest.raises(ValueError, match="开发协作规范缺少关键要求：尊重授权与事实源"):
        validate_ai_knowledge_base(path)


def test_delivery_sections_with_all_items_but_wrong_order_are_rejected(tmp_path):
    text = KNOWLEDGE.read_text(encoding="utf-8")
    original = "   - (5) 交付\n   - (6) 测试"
    assert original in text
    invalid = text.replace(original, "   - (6) 测试\n   - (5) 交付", 1)
    path = tmp_path / "AI_EXPERIENCE.md"
    path.write_text(invalid, encoding="utf-8")
    with pytest.raises(ValueError, match="固定交付结构必须完整且顺序"):
        validate_ai_knowledge_base(path)


def test_missing_mapping_and_resolved_history_heading_are_rejected(tmp_path):
    text = KNOWLEDGE.read_text(encoding="utf-8")
    text = text.replace(
        "> 用户提到的“AI 知识库”指仓库根目录的 `AI_EXPERIENCE.md`。\n",
        "",
        1,
    )
    text += "\n## 已解决修复追记\n\n这里不应重新归档。\n"
    path = tmp_path / "AI_EXPERIENCE.md"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        validate_ai_knowledge_base(path)
    message = str(exc.value)
    assert "缺少 AI 知识库到 AI_EXPERIENCE.md 的明确映射" in message
    assert "知识库仍含过期/已解决流水账标题" in message


def test_type_definitions_must_reference_canonical_rules_not_old_duplicates(tmp_path):
    text = KNOWLEDGE.read_text(encoding="utf-8")
    path = tmp_path / "AI_EXPERIENCE.md"
    path.write_text(text.replace('《规则正文》「基础定义」', '其他材料'), encoding="utf-8")
    with pytest.raises(ValueError, match="缺少资源类型定义指向"):
        validate_ai_knowledge_base(path)
