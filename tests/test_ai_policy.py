"""AI知识库与战斗推演格式完整性契约测试。"""
from pathlib import Path
import re
import pytest

from engine.document_validation import validate_markdown_documents

ROOT = Path(__file__).resolve().parents[1]
AI_EXPERIENCE_FILE = ROOT / "AI_EXPERIENCE.md"
SPELLS_INDEX_FILE = ROOT / "法术索引.md"
README_FILE = ROOT / "README.md"
PLAYBOOK_FILE = ROOT / "推演规范.md"
RULES_TEXT_FILE = ROOT / "规则正文.md"


# ---------- 正常路径 ----------

def test_ai_knowledge_base_contains_all_core_sections_and_scenarios():
    """正常路径：AI_EXPERIENCE.md 包含职责划分、协作规范、推演铁律、流水线与六大场景示例。"""
    assert AI_EXPERIENCE_FILE.is_file(), "AI_EXPERIENCE.md 文件必须存在"
    text = AI_EXPERIENCE_FILE.read_text(encoding="utf-8")

    assert text.startswith("# AI经验库")
    assert "## 软件工程实现与验证准则" in text
    assert "## AI角色扮演、协作规范与推演铁律" in text
    assert "### 职责划分" in text
    assert "### AI协作规范" in text
    assert "### 推演铁律" in text
    assert "## 战斗推演原子流水线与典型实战示例" in text

    # 协作规范九条必须齐全
    for sub in [
        "1. **越权禁令与工具授权**",
        "2. **事实源与同步**",
        "3. **设计合规**",
        "4. **代价与收益**",
        "5. **效果撰写**",
        "6. **命名**",
        "7. **叙事**",
        "8. **工作次序**",
        "9. **测试默认**",
    ]:
        assert sub in text, f"AI协作规范缺少小节：{sub}"

    # 六大典型场景（2026-10-12 用户令去重后：1-3 正本是本文件，4-6 正本是《法术索引》
    # 「四、反应型法术战斗流程示例」；本文件只留指针，不得再抄一份回来）
    assert "场景 1：基础攻防" in text
    assert "场景 2：闪避交互" in text
    assert "场景 3：残韵插队逆转" in text
    spells = SPELLS_INDEX_FILE.read_text(encoding="utf-8")
    assert "## 四、反应型法术战斗流程示例" in spells
    assert "### 场景：防御型反应法术" in spells
    assert "### 场景：交错反击型法术" in spells
    assert "### 场景：循环自伤法术" in spells
    # 指针必须真实存在，否则正本被搬走后没人找得到
    assert "法术索引.md#四反应型法术战斗流程示例" in text


def test_readme_is_a_clean_entry_and_playbook_keeps_execution_format():
    """README 是入口，不再承载长规则；完整推演格式迁到推演规范。"""
    readme_text = README_FILE.read_text(encoding="utf-8")
    assert "[AI 知识库](AI_EXPERIENCE.md)" in readme_text
    assert "[规则正文](规则正文.md)" in readme_text
    assert "[推演规范](推演规范.md)" in readme_text
    assert "## 第四宇宙规则正文" not in readme_text
    assert "【十三、AI协作规范】" not in readme_text
    assert "零、职责划分" not in readme_text
    assert "六、战斗推演格式" not in readme_text
    assert "七步原子流水线" not in readme_text
    assert "六、战斗推演格式" in PLAYBOOK_FILE.read_text(encoding="utf-8")
    assert "七步原子流水线" in PLAYBOOK_FILE.read_text(encoding="utf-8")
    assert "### 道纹体系" in RULES_TEXT_FILE.read_text(encoding="utf-8")


# ---------- 边界条件 ----------

def test_six_scenarios_contain_exact_mechanic_details():
    """边界条件：验证场景3中残韵作用于轮回者永久转化道纹、场景6千刀万剐完整三轮自驱动循环直到法力耗尽中断。"""
    text = AI_EXPERIENCE_FILE.read_text(encoding="utf-8")

    # 场景3边界：A的杀伐永久转化为再生，B同时永久获得再生
    assert "A 拥有的道纹【杀伐】永久转化为【再生】" in text
    assert "B 同时永久获得【再生】" in text

    # 场景6边界：千刀万剐 3 轮循环展开，法力从 6 扣减至 0，中断检查法力不足
    # （2026-10-12 去重后正本在《法术索引》，本文件不再持有副本）
    spells = SPELLS_INDEX_FILE.read_text(encoding="utf-8")
    assert "【失去生命后】B 触发循环法术【千刀万剐】（第 1 轮循环，X=2）" in spells
    assert "【失去生命后】B 触发循环法术【千刀万剐】（第 2 轮循环，X=2）" in spells
    assert "【失去生命后】B 触发循环法术【千刀万剐】（第 3 轮循环，X=2）" in spells
    assert "【中断检查】：B 当前法力为 0，无法支付下一轮【再生2】的 2 点法力" in spells


# ---------- 错误输入 / 非法配置 ----------

def test_missing_h1_or_broken_links_in_documents_are_rejected(tmp_path):
    """错误输入：缺少一级标题或包含失效内部锚点时必须被校验器拒绝。"""
    bad_doc = tmp_path / "bad.md"
    bad_doc.write_text("缺少一级标题\n\n[坏链接](README.md#不存在的锚点)\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# 灵记\n\n[目标](bad.md)\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        validate_markdown_documents(tmp_path)
    message = str(exc.value)
    assert "缺少一级标题" in message or "链接锚点不存在" in message
