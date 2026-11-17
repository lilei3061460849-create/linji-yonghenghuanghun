"""致死类特殊事件：结构化事实源 → 引擎 → 《死者之书》规则篇 的三方一致。

这是「结构化事实源」的**样板批**（2026-10-08）。它要证明的闭环是：

    data/rules/lethal_events.toml  ──► 引擎常量（Entity.*）
                                   ──► 进度渲染（Entity.lethal_progress）
                                   ──► 死者之书死因文案（CAUSE_DRAFTS）
                                   ──► 《死者之书.md》「## 规则」节（？？？ 署名）

旧机制 rule_sync.py 是「正则抓 markdown → 与引擎比对数量」，真冲突时谁对并无断言。
本套测试反过来断言**每一端的每个值都等于 toml 里的那个值**，缺一项、多一项、
改一项都会挂。

新增/修改致死事件的正确姿势：改 toml → 跑 `python3 sim/gen_rules.py` → 跑本文件。
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.death_book import CAUSE_DRAFTS  # noqa: E402
from engine.models import Entity  # noqa: E402
from engine.rules_source import lethal_events  # noqa: E402

BOOK = ROOT / "死者之书.md"
GEN = ROOT / "sim" / "gen_rules.py"


# ============================================================================
# 一、toml → 引擎常量
# ============================================================================

def test_entity_thresholds_come_from_the_toml():
    """引擎三个阈值必须等于 toml 里的值，不再是各自写死的数字。"""
    assert Entity.MUTATION_COLLAPSE_THRESHOLD == lethal_events.threshold("mishi")
    assert Entity.CANCER_HEAL_MULTIPLIER == lethal_events.threshold_multiplier("aibian")
    assert Entity.MEDIOCRITY_ROUNDS == lethal_events.threshold("fanyong")
    # 钉住当前数值，防止有人悄悄改了 toml 却没跑回归
    assert Entity.MUTATION_COLLAPSE_THRESHOLD == 50
    assert Entity.CANCER_HEAL_MULTIPLIER == 2.0
    assert Entity.MEDIOCRITY_ROUNDS == 5


def test_death_cause_texts_come_from_the_toml():
    """死者之书三条死因文案必须与 toml 同源。"""
    for event_id in ("mishi", "aibian", "fanyong"):
        key = lethal_events.death_cause_key(event_id)
        assert CAUSE_DRAFTS[key]["text"] == lethal_events.death_cause_text(event_id)


def test_progress_format_is_taken_from_the_toml():
    """进度串的括号样式由 toml 决定，不在 lethal_progress 里写死。

    改 toml 的 progress_format，渲染结果必须跟着变——这条用例用临时改值验证
    的是「确实读的是 toml」而不是「恰好长得一样」。
    """
    entity = Entity(name="测", entity_type="轮回者", blood_limit=60, current_hp=60)
    entity.mutation_count = 10
    # blood_limit>0 时癌变也会出现在进度里，这里只看迷失那一条
    assert entity.lethal_progress()[0] == "迷失（10/50）"

    entry = lethal_events.by_id("mishi")
    original = entry["progress_format"]
    try:
        entry["progress_format"] = "{name}[{current}/{limit}]"
        assert entity.lethal_progress()[0] == "迷失[10/50]", "进度格式串没有真的走 toml"
    finally:
        entry["progress_format"] = original


def test_fanyong_display_keys_come_from_the_toml():
    """凡庸两项独立计数的显示键由 TOML 提供。"""
    keys = lethal_events.progress_keys("fanyong")
    assert keys == ["凡庸·未出手", "凡庸·未使其他角色生命减少"]
    entity = Entity(name="测", entity_type="轮回者", blood_limit=60, current_hp=60)
    entity.no_action_rounds = 3
    assert keys[0] in entity.lethal_counters()
    entity.no_action_rounds = 0
    entity.no_damage_rounds = 3
    assert keys[1] in entity.lethal_counters()
    assert any(keys[1] in item for item in entity.lethal_progress())


# ============================================================================
# 二、toml → 《死者之书》规则篇
# ============================================================================

def test_rule_section_is_up_to_date_with_the_toml():
    """「## 规则」节必须与生成器输出逐字一致（等价于跑 gen_rules.py --check）。"""
    if not BOOK.exists():
        pytest.skip("死者之书.md 不存在")
    proc = subprocess.run(
        [sys.executable, str(GEN), "--check"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        "《死者之书.md》的「## 规则」节与 data/rules/*.toml 不一致，"
        f"请跑 `python3 sim/gen_rules.py`。\n{proc.stdout}{proc.stderr}"
    )


def test_every_lethal_event_has_a_signed_page_in_the_book():
    """每条致死事件在书里都有一页，署名必须是 ？？？（规则不署人名）。"""
    text = BOOK.read_text(encoding="utf-8")
    for entry in lethal_events.all():
        heading = f"### {lethal_events.signature}·{lethal_events.chapter}·{entry['name']}"
        assert heading in text, f"死者之书缺少规则页：{heading}"
        # 规则页必须带上结构化数值，光有散文等于又回到旧机制
        page = text.split(heading, 1)[1].split("\n### ", 1)[0]
        label = ("阈值 50" if entry["name"] == "迷失"
                 else "阈值 5" if entry["name"] == "凡庸"
                 else "阈值 ⌈[血限]×2.0⌉")
        assert label in page, f"{entry['name']} 页缺少结构化阈值：{page[:120]}"
        for rule_line in entry["rule_lines"]:
            assert rule_line in page, f"{entry['name']} 页缺少规则正文：{rule_line[:60]}"


def test_rule_section_is_not_polluted_by_personal_legacies():
    """规则节里不许出现署人名的页面——规则署 ？？？，遗言才署人名。"""
    text = BOOK.read_text(encoding="utf-8")
    section = text.split("## 规则", 1)[1]
    for line in section.splitlines():
        if line.startswith("### "):
            assert line.startswith(f"### {lethal_events.signature}·"), (
                f"规则节出现非 ？？？ 署名的页面：{line}"
            )


# ============================================================================
# 三、事实源自证：toml 本身要完整
# ============================================================================

def test_toml_entries_are_complete():
    """每条致死事件必须齐备引擎与生成器要用到的字段。"""
    required = {"id", "name", "order", "counter_field", "counter_label",
                "threshold_expr", "death_cause_key", "death_cause_text",
                "progress_format", "rule_lines"}
    for entry in lethal_events.all():
        missing = required - set(entry)
        assert not missing, f"致死事件 {entry.get('id')} 缺字段: {sorted(missing)}"
        # 阈值二选一，不能两个都没有
        assert "threshold" in entry or "threshold_multiplier" in entry, \
            f"致死事件 {entry['id']} 既没有 threshold 也没有 threshold_multiplier"
        assert entry["rule_lines"], f"致死事件 {entry['id']} 的 rule_lines 为空"
