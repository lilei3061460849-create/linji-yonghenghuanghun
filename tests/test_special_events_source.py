"""非致死类特殊事件：data/rules/special_events.toml ↔ 引擎 ↔ 规则正文 ↔ 《死者之书》四方一致。

雕塑批（2026-10-09）。雕塑是 规则正文「特殊事件」节里的**非致死**特殊事件（攻次攻力
双归零 → 永久离场化为消耗品，非[命零]），2026-10-08 样板批只移植了致死类三事件
（迷失/癌变/凡庸），雕塑不符合 lethal schema 漏网至今。本文件守护新管线：

    data/rules/special_events.toml ──► 引擎常量（MonsterLifeMixin.SCULPTURE_*）
                                    ──► 《死者之书.md》「## 规则」节「特殊事件」章
    规则正文.md「特殊事件」节 ──► rule_lines 逐行照录（断言与 规则正文 逐字一致，
                               且与结构化数值互相咬住——改 toml 不改 规则正文 挂测试）
    tests 钉住：order 与致死事件合并后不许撞车、三份事实源署名一致且两份事件
    文件同章、雕塑耐久计算真的走 toml（临时改值验证，不是恰好长得一样）。

新增/修改非致死特殊事件的正确姿势：改 toml → 跑 `python3 sim/gen_rules.py` → 跑本文件。
"""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.combat import CombatEngine  # noqa: E402
from engine.combat_parts.monster_life import MonsterLifeMixin  # noqa: E402
from engine.dice import DiceEngine  # noqa: E402
from engine.models import Entity, GameState  # noqa: E402
from engine.rules_source import game_rules, lethal_events, special_events  # noqa: E402

BOOK = ROOT / "死者之书.md"
RULES_TEXT = ROOT / "规则正文.md"


def _monster(name: str, blood_limit: int) -> Entity:
    return Entity(name=name, entity_type="怪物", blood_limit=blood_limit,
                  current_hp=blood_limit, attack_count=0, attack_power=0)


# ============================================================================
# 一、toml → 引擎常量
# ============================================================================

def test_sculpture_constants_come_from_the_toml():
    """雕塑的伤害/格挡/耐久比必须等于 toml 里的值，不再是各自写死的数字。"""
    assert MonsterLifeMixin.SCULPTURE_DAMAGE == special_events.damage_per_durability("diaosu")
    assert MonsterLifeMixin.SCULPTURE_SHIELD == special_events.shield_per_durability("diaosu")
    # 钉住当前数值，防止有人悄悄改了 toml 却没跑回归
    assert MonsterLifeMixin.SCULPTURE_DAMAGE == 15
    assert MonsterLifeMixin.SCULPTURE_SHIELD == 20
    assert special_events.durability_ratio("diaosu") == 0.05
    assert special_events.durability_min("diaosu") == 1


def test_sculpture_durability_follows_the_toml():
    """耐久 = max(durability_min, ceil([血限]×durability_ratio))，真的读 toml。

    用临时改值验证「确实读的是 toml」而不是「恰好长得一样」（血限 234 → 12）。
    """
    state = GameState()
    state.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                          speed_limit=1, current_speed=1, mana_limit=1, current_mana=1)
    m = _monster("石像鬼", blood_limit=234)
    state.enemies.append(m)
    combat = CombatEngine(state, DiceEngine())
    combat.settle_victory_paths()
    consumable = next(c for c in state.consumables if c.kind == "sculpture")
    assert consumable.max_uses == 12, consumable.max_uses  # ceil(234×0.05)=12

    entry = special_events.by_id("diaosu")
    original = entry["durability_ratio"]
    try:
        entry["durability_ratio"] = 0.10
        state2 = GameState()
        state2.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60,
                               current_hp=60, speed_limit=1, current_speed=1,
                               mana_limit=1, current_mana=1)
        m2 = _monster("石像鬼", blood_limit=234)
        state2.enemies.append(m2)
        CombatEngine(state2, DiceEngine()).settle_victory_paths()
        consumable2 = next(c for c in state2.consumables if c.kind == "sculpture")
        assert consumable2.max_uses == 24, "耐久没有真的走 toml 的 durability_ratio"
    finally:
        entry["durability_ratio"] = original


# ============================================================================
# 二、toml → 《死者之书》规则篇
# ============================================================================

def test_every_special_event_has_a_signed_page_in_the_book():
    """每条非致死特殊事件在书里都有一页，署名 ？？？，数值位与原文都在页里。"""
    text = BOOK.read_text(encoding="utf-8")
    for entry in special_events.all():
        # 条目可自带 chapter 覆盖（【背叛】已改为「同伴规则」，不再是特殊事件）
        chapter = entry.get("chapter") or special_events.chapter
        heading = f"### {special_events.signature}·{chapter}·{entry['name']}"
        assert heading in text, f"死者之书缺少规则页：{heading}"
        page = text.split(heading, 1)[1].split("\n### ", 1)[0]
        if "durability_ratio" in entry:
            assert f"耐久上限 ⌈[血限]×{entry['durability_ratio']}⌉" in page, \
                f"{entry['name']} 页缺少结构化耐久：{page[:120]}"
            assert (f"伤害 {entry['damage_per_durability']} / 格挡 "
                    f"{entry['shield_per_durability']}") in page, \
                f"{entry['name']} 页缺少每耐久伤害/格挡：{page[:120]}"
        for rule_line in entry["rule_lines"]:
            assert rule_line in page, f"{entry['name']} 页缺少规则正文：{rule_line[:60]}"


def test_rule_lines_match_rules_text_verbatim():
    """rule_lines 必须是 规则正文「特殊事件」节原文的逐行照录。"""
    rules_lines = RULES_TEXT.read_text(encoding="utf-8").splitlines()
    for entry in special_events.all():
        assert entry["rule_lines"], f"{entry['name']} 的 rule_lines 为空"
        for line in entry["rule_lines"]:
            assert line in rules_lines, (
                f"{entry['name']} 的 rule_line 不是 规则正文 原文行：{line[:60]}")


def test_structured_numbers_agree_with_rule_lines():
    """结构化数值必须与 rule_lines 里的数字互相咬住——改 toml 不改 规则正文 挂测试。"""
    for entry in special_events.all():
        if "durability_ratio" not in entry:
            continue
        joined = "".join(entry["rule_lines"])
        assert f"造成{entry['damage_per_durability']}点伤害" in joined, \
            f"{entry['name']} 的 rule_lines 与 damage_per_durability 不一致"
        assert f"获得{entry['shield_per_durability']}点格挡" in joined, \
            f"{entry['name']} 的 rule_lines 与 shield_per_durability 不一致"
        pct = round(entry["durability_ratio"] * 100)
        assert f"[血限]{pct}%" in joined, \
            f"{entry['name']} 的 rule_lines 与 durability_ratio 不一致"


# ============================================================================
# 三、事实源自证：toml 本身要完整、与致死事件不撞车
# ============================================================================

def test_remaining_special_events_are_registered_and_debt_threshold_is_source_driven():
    """十项遗留特殊事件全部进入结构化事实源，还债阈值不再硬编码。"""
    expected = {
        "死者之页", "还债", "背叛", "许愿", "逃跑与追击",
        "进化", "撤退", "最终的冠冕", "救赎", "初拥之夜",
    }
    registered = {entry["name"] for entry in special_events.all()}
    assert expected <= registered
    assert special_events.threshold("huanzhai") == 10
    assert MonsterLifeMixin.DEBT_THRESHOLD == special_events.threshold("huanzhai")


def test_toml_entries_are_complete():
    """每条非致死特殊事件必须齐备引擎与生成器要用到的字段。"""
    for entry in special_events.all():
        missing = {"id", "name", "order", "rule_lines"} - set(entry)
        assert not missing, f"非致死特殊事件 {entry.get('id')} 缺字段: {sorted(missing)}"


def test_event_orders_do_not_collide_with_lethal_events():
    """致死类与非致死类特殊事件同章，order 合并后不许撞车（生成器按序合并）。"""
    orders = [e["order"] for e in lethal_events.all()] + [e["order"] for e in special_events.all()]
    assert len(orders) == len(set(orders)), f"特殊事件 order 撞车：{sorted(orders)}"


def test_all_rule_sources_share_signature_and_event_chapter():
    """三份事实源署名必须一致；两份事件文件必须同章（同渲染进「特殊事件」章）。"""
    assert (game_rules.signature == lethal_events.signature
            == special_events.signature == "？？？")
    assert lethal_events.chapter == special_events.chapter == "特殊事件"
