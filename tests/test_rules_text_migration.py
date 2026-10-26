"""README 精确中文规则迁移：入口层 ↔ 规则正文 ↔ 推演规范 ↔ 运行时解析器。

2026-10-09 用户授权：README 不再堆放会变动的中文规则；精确规则迁入 `规则正文.md`，
开局/战斗/死斗/战报流程迁入 `推演规范.md`。本文件守住迁移不可回退的边界：

    README.md          入口、AI/维护速览、导航（非规范）
    规则正文.md         通用规则/通用道纹/通用事件的唯一精确 Markdown 事实源
    推演规范.md         操作与记录规范
    engine/events.py   从规则正文解析通用事件
    engine/rule_sync.py 从规则正文提取通用道纹

若将条文偷偷抄回 README，会重新制造双事实源；若解析器仍读 README，则 README 瘦身后
通用事件或道纹同步会静默失效。两种退化均必须在这里直接失败。
"""
from pathlib import Path

from engine.document_sources import COMMON_RULES_FILE, PLAYBOOK_FILE, README_FILE
from engine.events import RULES_TEXT_FILE, parse_events
from engine.rule_sync import RuleSync

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / README_FILE
RULES_TEXT = ROOT / COMMON_RULES_FILE
PLAYBOOK = ROOT / PLAYBOOK_FILE


def test_document_source_constants_and_runtime_readers_use_the_migrated_source():
    """运行时事件解析与规则同步必须指向规则正文，README 不得再充当规则输入。"""
    assert COMMON_RULES_FILE == "规则正文.md"
    assert PLAYBOOK_FILE == "推演规范.md"
    assert README_FILE == "README.md"
    assert RULES_TEXT_FILE == COMMON_RULES_FILE
    assert RuleSync.DEFAULT_RULE_FILES[0] == COMMON_RULES_FILE
    assert README_FILE not in RuleSync.DEFAULT_RULE_FILES

    events = parse_events(ROOT / "副本索引.md")
    assert set(("无名冢", "遗忘书屋", "祭坛", "过路商人", "无魂泥潭")) <= set(events)


def test_exact_rule_and_playbook_sections_moved_without_loss():
    """迁出的三段关键正文/流程仍在新文件，不是删规则。"""
    rules = RULES_TEXT.read_text(encoding="utf-8")
    playbook = PLAYBOOK.read_text(encoding="utf-8")

    for heading in ("### 基础定义", "### 核心规则", "### 道纹体系",
                    "### 特殊事件", "### 通用事件池"):
        assert heading in rules, f"规则正文缺少迁出的章节：{heading}"
    for heading in ("## 五分钟上手", "## 游戏流程", "### 战斗整体结构", "### 报告书写口径"):
        assert heading in playbook, f"推演规范缺少迁出的章节：{heading}"


def test_readme_is_only_entry_not_a_second_chinese_rule_source():
    """README 只留导航/速览，不能回长出精确正文的章节或通用事件原文。"""
    readme = README.read_text(encoding="utf-8")
    assert "入口与速览，不是规则正本" in readme
    assert "[规则正文](规则正文.md)" in readme
    assert "[推演规范](推演规范.md)" in readme
    for forbidden in ("## 第四宇宙规则正文", "### 基础定义", "### 道纹体系",
                      "### 特殊事件", "### 通用事件池", "## 游戏流程", "六、战斗推演格式"):
        assert forbidden not in readme, f"README 重新承载精确规则：{forbidden}"
    # 典型通用事件原文只应留在规则正文，不能重造第二份。
    assert "无名冢：一片插满残破兵器的荒地" not in readme


def test_rule_sync_extracts_same_common_daowen_after_migration():
    """瘦身 README 后通用道纹同步仍完整，避免错误迁移成 0 条的静默退化。"""
    sync = RuleSync(db_path="data/test_rule_sources.db")
    names = {item["name"] for item in sync.extract_daowen_from_file(COMMON_RULES_FILE)}
    # 2026-10-11 校正：33 为旧口径遗留；本测试的要点是"不为 0、且关键道纹在列"，
    # 2026-10-15：独立【飞行】及已遗物化的【寄生】删除后，通用道纹定义数相应减少。
    # 2026-10-25：【疯狂】【减速】遗物化 → 20 → 18。
    assert len(names) == 18
    assert {"杀伐", "波及", "封印"} <= names
    assert {"飞行", "寄生"}.isdisjoint(names)
    assert sync.extract_daowen_from_file(README_FILE) == []
