"""AI知识库、规则导航与正式推演文档的完整性契约测试。"""
from pathlib import Path
import pytest

from engine.document_validation import validate_markdown_documents

ROOT = Path(__file__).resolve().parents[1]
AI_EXPERIENCE_FILE = ROOT / "AI_EXPERIENCE.md"
SPELLS_INDEX_FILE = ROOT / "法术索引.md"
README_FILE = ROOT / "README.md"
PLAYBOOK_FILE = ROOT / "推演规范.md"
RULES_TEXT_FILE = ROOT / "规则正文.md"


def test_ai_knowledge_base_keeps_engineering_policy_and_drops_retired_playbooks():
    assert AI_EXPERIENCE_FILE.is_file()
    text = AI_EXPERIENCE_FILE.read_text(encoding="utf-8")
    assert text.startswith("# AI经验库")
    for heading in (
        "## 软件工程实现与验证准则",
        "## 第四宇宙设计原则",
        "## AI开发协作规范",
        "## 知识库维护",
        "## 文档分工与事实源",
        "## 文档管理规则",
        "## 当前执行与验证口径",
        "## 规则正文的引擎接线口径",
        "## 当前有效的工程约束",
        "## 当前限制与待裁定入口",
    ):
        assert heading in text
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
        "剧情优先",
        "死锁判卫冕",
        "当前固定2次",
    ):
        assert marker not in text, marker
    collab = text.split("## AI开发协作规范", 1)[1].split("\n## ", 1)[0]
    for marker in (
        "尊重授权与事实源",
        "保持规则与实现边界",
        "处理创造性机制",
        "设计与实现",
        "修改顺序",
        "验证与交付",
    ):
        assert marker in collab


def test_readme_is_a_clean_entry_and_playbook_keeps_formal_sections():
    readme_text = README_FILE.read_text(encoding="utf-8")
    assert "[AI 知识库](AI_EXPERIENCE.md)" in readme_text
    assert "[规则正文](规则正文.md)" in readme_text
    assert "[推演规范](推演规范.md)" in readme_text
    assert "## 第四宇宙规则正文" not in readme_text
    playbook = PLAYBOOK_FILE.read_text(encoding="utf-8")
    assert "### 战斗整体结构" in playbook
    assert "### 报告书写口径" in playbook
    assert "### 道纹体系" in RULES_TEXT_FILE.read_text(encoding="utf-8")


def test_spell_examples_remain_in_their_canonical_index():
    spells = SPELLS_INDEX_FILE.read_text(encoding="utf-8")
    assert "## 五、反应型法术战斗流程示例" in spells
    assert "### 场景：防御型反应法术" in spells
    assert "### 场景：交错反击型法术" in spells
    assert "### 场景：循环自伤法术" in spells
    assert "【失去生命后】B 触发循环法术【千刀万剐】（第 1 轮循环，X=2）" in spells
    assert "【失去生命后】B 触发循环法术【千刀万剐】（第 3 轮循环，X=2）" in spells
    assert "AI_EXPERIENCE.md#典型实战场景示例" not in spells


def test_missing_h1_or_broken_links_in_documents_are_rejected(tmp_path):
    bad_doc = tmp_path / "bad.md"
    bad_doc.write_text("缺少一级标题\n\n[坏链接](README.md#不存在的锚点)\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# 灵记\n\n[目标](bad.md)\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        validate_markdown_documents(tmp_path)
    message = str(exc.value)
    assert "缺少一级标题" in message or "链接锚点不存在" in message
