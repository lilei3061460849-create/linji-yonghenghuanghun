"""资源设计标准的机械护栏（2026-10-23 用户裁定落地）。

四类资源的资格与结构写在《AI知识库》「【二、资源设计标准】」：

- 新增资格（道纹／遗物／法器）：现有任何资源都无法实现的效果，才有资格新增资源。消耗品不受此限制。
- 道纹：名称仅二字；默认局内发动，局外须条文**明示**；必有 X，X 只能提升强度或持续时间。
- 遗物：无 X 变量；必有触发时机及对应效果。
- 法器：不受限其他资源规则；必有明确且独特的效果。
- 消耗品：主／被动均可；不消耗出手；可当场设计。

本文件只做「能机械判定」的那部分。设计层判断（是否真的独一无二、触发时机是否自洽）
留给 DM 与评审，不在此处伪造通过。

两类债务快照（X 变量、触发时机）用「当前已知集合相等」写法：修好一件必须同步缩小集合，
新增违例会让测试直接失败。对应待办见 GitHub Issue，不在本文件写日期流水账。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine, OUT_OF_COMBAT_DAOWEN  # noqa: E402
from engine.daowen import DaoWenEngine  # noqa: E402
from engine.models import DaoWen, DaoWenInstance  # noqa: E402
from tests.setup_support import finish_initial_daowen  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ITEM_INDEX = ROOT / "物品索引.md"
RULES_TEXT = ROOT / "规则正文.md"
KNOWLEDGE = ROOT / "AI_EXPERIENCE.md"

# 「X 不影响任何效果字段」的已知例外。2026-10-23 用户裁定【真理】改为「X 句话」
# 后，X 同时抬高陈述句数与无法开口回合数，故例外清空：任何道纹的 X 都必须有效。
X_DOES_NOT_SCALE: set[str] = set()

# 遗物条文中出现 X 变量的已知违例。2026-10-23 用户令「遗物彻底重做、不保留原效果」
# 后，血族/龙族/鲛族/事件/草案共 13 件全部改为无 X，违例清空。此集合保持为空：
# 新增任何带 X 的遗物都会让快照测试立刻失败（遗物一律无 X）。
RELIC_X_DEBT: set[tuple[str, str]] = set()

# 遗物条文没有可识别触发时机的已知违例。同日重做后 13 件均补齐触发时机，清空同上。
RELIC_TRIGGER_DEBT: set[tuple[str, str]] = set()

TRIGGER_TOKENS = (
    "战始", "战终", "回始", "回终", "敌回始", "敌回终", "回合结束", "每回合",
    "每次", "每当", "时", "前", "后", "即将", "进入", "获得", "失去", "受到",
    "造成", "选择", "死亡", "命零", "闪避", "出手", "行动",
)


def _relic_entries() -> list[tuple[str, str, str]]:
    """从《物品索引》取 (章节简称, 名称, 首行效果文本)；只取标题含「遗物」的章节。"""
    entries: list[tuple[str, str, str]] = []
    section = ""
    name = ""
    body: list[str] = []

    def flush() -> None:
        nonlocal name, body
        if name and "遗物" in section:
            first = next((ln.strip() for ln in body
                          if ln.strip() and not ln.strip().startswith(">")), "")
            entries.append((section, name, first))
        name, body = "", []

    for line in ITEM_INDEX.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^##\s+(.*)", line)
        if heading:
            flush()
            section = heading.group(1).strip()
            continue
        sub = re.match(r"^###\s+(.*)", line)
        if sub:
            flush()
            name = sub.group(1).strip()
            continue
        if name:
            body.append(line)
    flush()
    return entries


def _grant(engine, names: list[str]) -> None:
    for n in names:
        engine.state.player.dao_wen[n] = DaoWenInstance(
            DaoWen(name=n, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


@pytest.fixture(scope="module")
def engine() -> GameEngine:
    eng = GameEngine(rng_seed=42)
    eng.execute_action("setup_attributes",
                       {"blood_points": 11, "speed_points": 6, "mana_points": 8})
    finish_initial_daowen(eng)
    eng.state.current_region = "罪孽都市"
    eng.state.relics = []
    return eng


# ==================== 道纹：名称二字 ====================

def test_every_registered_daowen_name_is_exactly_two_chars():
    """道纹名单有且仅有二字；注册表与派生索引必须同时满足。"""
    DaoWenEngine.register_all()
    bad = [n for n in DaoWenEngine._registry if len(n) != 2]
    assert bad == [], f"道纹名必须二字：{bad}"

    index = (ROOT / "全道纹索引.md").read_text(encoding="utf-8")
    names = set()
    for line in index.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and re.fullmatch(r"[\u4e00-\u9fff]{1,6}", cells[0]) and cells[0] not in {"道纹", "名称"}:
            names.add(cells[0])
    assert names, "未从 全道纹索引.md 解析出道纹名"
    bad = sorted(n for n in names if len(n) != 2)
    assert bad == [], f"索引里出现非二字道纹：{bad}"


# ==================== 道纹：X 必须改变强度或持续时间 ====================

def test_daowen_x_scales_strength_or_duration():
    """X 必须至少改变一个效果字段；固定 X 等于没有 X，与条文冲突。"""
    DaoWenEngine.register_all()
    ignore = {"x", "y", "summary", "dao_wen", "cost_type"}
    flat = []
    for name in sorted(DaoWenEngine._registry):
        low, high = DaoWenEngine.resolve(name, 1), DaoWenEngine.resolve(name, 3)
        if all(low.get(k) == high.get(k) for k in low if k not in ignore):
            flat.append(name)
    assert set(flat) == X_DOES_NOT_SCALE, (
        f"X 不改变任何效果的道纹集合应与已知例外一致：实际 {sorted(flat)}，"
        f"已知 {sorted(X_DOES_NOT_SCALE)}")


# ==================== 道纹：局外发动须明示 ====================

def test_out_of_combat_daowen_whitelist_is_explicit():
    """默认局内；局外白名单只能由条文明示项构成：消灾、真理（2026-10-23 裁定）。"""
    assert OUT_OF_COMBAT_DAOWEN == frozenset({"消灾", "真理"})


def test_unlisted_daowen_rejected_out_of_combat_and_listed_one_allowed(engine):
    """边界：名单外的道纹在局外被拒且错误文案点明明示口径；名单内的照常发动。"""
    _grant(engine, ["杀伐", "消灾"])
    engine.state.phase = "pre_battle"
    engine.state.fake_shards = 100

    blocked = engine.execute_action("use_daowen", {
        "daowen_name": "杀伐", "x": 1,
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}})
    assert not blocked["success"]
    assert "局外发动须条文明示" in blocked["error"], blocked

    allowed = engine.execute_action("use_daowen", {
        "daowen_name": "消灾", "x": 1,
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}})
    assert allowed["success"], allowed
    assert engine.state.player.actions_used_this_round == 0
    engine.state.phase = "in_combat"


# ==================== 遗物：无 X 变量 / 必有触发时机 ====================

def test_relic_pool_has_no_x_variable():
    """通用遗物池是「无 X」的标杆面，退化成含 X 会直接失败。"""
    for name, effect in GameEngine.RELIC_DEFS:
        assert not re.search(r"X", effect), f"遗物池【{name}】出现 X 变量：{effect}"


def test_relic_x_variable_debt_is_frozen():
    """池外遗物的 X 违例快照：2026-10-23 重做后清零；新增会立刻失败。"""
    hits = {(s, n) for s, n, effect in _relic_entries() if re.search(r"X", effect)}
    assert hits == RELIC_X_DEBT, (
        f"遗物 X 违例集合变化：新增 {sorted(hits - RELIC_X_DEBT)}，"
        f"已修复 {sorted(RELIC_X_DEBT - hits)}")


def test_relic_missing_trigger_debt_is_frozen():
    """遗物必须有触发时机；2026-10-23 重做后清零，新增无时机遗物会立刻失败。"""
    missing = {(s, n) for s, n, effect in _relic_entries()
               if effect and not any(tok in effect for tok in TRIGGER_TOKENS)}
    assert missing == RELIC_TRIGGER_DEBT, (
        f"缺触发时机的遗物集合变化：新增 {sorted(missing - RELIC_TRIGGER_DEBT)}，"
        f"已修复 {sorted(RELIC_TRIGGER_DEBT - missing)}")


# ==================== 法器：独立品类存在且有独占效果 ====================

def test_artifact_section_exists_and_entries_are_unique():
    """法器是独立品类：有专属章节，条目不得与遗物池同名重覆解释。"""
    text = ITEM_INDEX.read_text(encoding="utf-8")
    assert "\n## 法器\n" in text
    section = text.split("\n## 法器\n", 1)[1].split("\n## ", 1)[0]
    names = re.findall(r"^###\s+(.*)$", section, re.MULTILINE)
    assert len(names) >= 20, f"法器条目过少：{names}"
    assert len(set(names)) == len(names)

    relic_names = {n for n, _ in GameEngine.RELIC_DEFS}
    overlap = relic_names & set(names)
    # 【万魂幡】2026-10-25 用户令归法器，已退出 RELIC_DEFS：法器与遗物池不得再有任何重名。
    assert not overlap, f"法器与遗物池重名：{sorted(overlap)}"


# ==================== 消耗品：不耗出手 / 主被动均可 ====================

def test_consumable_rules_text_keeps_no_action_and_passive_capability():
    """消耗品口径：不消耗出手（正文）＋主／被动均可当场设计（知识库）。"""
    rules = RULES_TEXT.read_text(encoding="utf-8")
    assert "使用时不消耗出手" in rules
    assert "消耗品（X/Y）" in rules
    knowledge = KNOWLEDGE.read_text(encoding="utf-8")
    assert "主动与被动均可生效" in knowledge
    assert "不消耗出手" in knowledge
    assert "当场设计" in knowledge


def test_consumable_without_registered_handler_is_rejected(engine):
    """错误输入：无处理器、且文本不含可解析效果的消耗品必须被拒绝，不得只扣耐久。"""
    from engine.models import Consumable

    engine.state.phase = "in_combat"
    item = Consumable(name="临时测试道具", effect="此处写一段没人解析得动的效果",
                      current_uses=2, max_uses=2, kind="normal")
    engine.state.consumables.append(item)
    result = engine.execute_action("consume_item", {"name": item.name})
    assert not result["success"]
    assert "没有已注册的效果处理器" in result["error"]
    assert item.current_uses == 2, "被拒绝的消耗品不得扣耐久"
    engine.state.consumables.remove(item)

# ==================== 真理：X = 陈述句数 + 局外明示 ====================

def test_zhenli_x_equals_sentence_count_and_scales_duration():
    """真理的 X 是句数，同时抬高无法开口的回合数——X 不再是摆设。"""
    DaoWenEngine.register_all()
    one = DaoWenEngine.resolve("真理", 1)
    three = DaoWenEngine.resolve("真理", 3)
    assert one["truth_sentences"] == 1 and three["truth_sentences"] == 3
    assert one["silence_rounds"] == 1 and three["silence_rounds"] == 3


def test_zhenli_enforces_declared_sentence_count(engine):
    """边界：提交的陈述句数必须等于 X，否则整次发动被拒。"""
    from engine.models import DaoWen, DaoWenInstance, Entity

    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.player.dao_wen["真理"] = DaoWenInstance(
        DaoWen(name="真理", formula="真理X", cost_type="冷却",
               cost_formula="2场战斗", effect_formula="X句陈述"),
        cooldown_remaining=0)
    monster = Entity(name="受讯者", entity_type="怪物", blood_limit=50, current_hp=50,
                     attack_count=1, attack_power=1)
    engine.state.enemies.append(monster)

    wrong = engine.execute_action("use_daowen", {
        "daowen_name": "真理", "x": 3, "target_ref": "enemy:0",
        "statement": "只有一句话。"})
    assert not wrong["success"]
    assert "正好3句" in wrong["error"]

    right = engine.execute_action("use_daowen", {
        "daowen_name": "真理", "x": 3, "target_ref": "enemy:0",
        "statement": "第一句。第二句！第三句？"})
    assert right["success"], right
    assert right["result"]["sentences"] == ["第一句", "第二句", "第三句"]
    assert right["result"]["silence_rounds"] == 3
    engine.submit_ruling(interrupt_type="自定义", ruling_text="逐句属实")
    engine.state.enemies.remove(monster)


def test_zhenli_can_be_used_out_of_combat(engine):
    """局外明示：【真理】列入白名单后，局外可用 target_name 指向不在场的对象。"""
    from engine.models import DaoWen, DaoWenInstance

    engine.state.phase = "pre_battle"
    engine.state.player.dao_wen["真理"] = DaoWenInstance(
        DaoWen(name="真理", formula="真理X", cost_type="冷却",
               cost_formula="2场战斗", effect_formula="X句陈述"),
        cooldown_remaining=0)
    result = engine.execute_action("use_daowen", {
        "daowen_name": "真理", "x": 1,
        "target_name": "过路商人", "statement": "你卖过假货"})
    assert result["success"], result
    context = result["result"]["interrupt"]["context"]
    assert context["target"] == "过路商人"
    assert context["sentences"] == ["你卖过假货"]
    engine.submit_ruling(interrupt_type="自定义", ruling_text="属实")
    engine.state.phase = "in_combat"
