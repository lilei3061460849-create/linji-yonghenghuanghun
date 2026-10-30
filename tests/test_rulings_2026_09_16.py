"""2026-09-16 裁定批次的回归测试。

覆盖 报告.md 清单里本次已处理项：
- A5 真拒绝判定收紧为「开头/分词」匹配，并显式排除带代价/收益的「拒绝改造」
- B2 副本专属道纹补进正文
- B4 命零碎片公式禁止改写，事件收益改为独立列项奖金
- B5 死斗守擂侧（怪物）出手预算封顶，不得无限出手
- D1 血炼周天补内置流程；血溅五步删除
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from engine.events import parse_events
from engine.enums import CostType
from engine.models import DaoWen, DaoWenInstance, Entity
from tests.setup_support import finish_initial_daowen


# ========================================================================
# A5 真拒绝判定
# ========================================================================

@pytest.mark.parametrize("text,expected", [
    ("拒绝：无事发生", True),
    ("3.拒绝：无事发生", True),
    ("拒绝下注", True),                       # 正文点名：地下角斗场·拒绝下注
    ("捂住耳朵：无事发生", True),
    ("绕桥而行", True),
    ("离开", True),
    ("目送", True),
    ("无视他", True),
    ("转身就走", True),
    ("避开陷阱", True),
    # 带实质得失的「拒绝改造」不算真拒绝
    ("拒绝改造：流血5，获得10碎片", False),
    ("拒绝：失去10碎片", False),
    ("献祭血肉：衰老8，获得1点[速限]", False),
    # 句中出现「无事发生」但整体是带代价的赌局 → 不是真拒绝
    ("下注生命：流血X。50%获得2X[碎片]，50%无事发生。", False),
])
def test_true_rejection_matching(text, expected):
    assert GameEngine._is_reject_option_text(text) is expected


def test_true_rejection_counts_match_documented_numbers():
    """事件池统计：36 个事件；当前有33个事件含真拒绝，共33个真拒绝选项。"""
    events = parse_events("副本索引.md")
    with_reject = 0
    total = 0
    for name, data in events.items():
        hits = sum(1 for opt in data.get("options") or []
                   if GameEngine._is_reject_option_text(opt["text"]))
        if hits:
            with_reject += 1
            total += hits
    assert len(events) == 36
    assert with_reject == 33
    assert total == 33


# ========================================================================
# B2 副本专属道纹已进正文
# ========================================================================

def test_dungeon_daowen_are_documented_in_authoritative_source():
    """已实现副本道纹进通用正文，草案道纹留在各自副本文档。

    期望名单取自引擎，文档范围取自副本清单；不把沉沦海草案误塞进已实现正文。
    """
    from engine.dungeons import load_dungeon_manifest
    from engine.gamedata import REGION_EXCLUSIVE_DAOWEN

    lines = open("规则正文.md", encoding="utf-8").read().split("\n")
    start = next(i for i, line in enumerate(lines)
                 if line.startswith("### 副本专属道纹"))
    end = next(i for i, line in enumerate(lines)
               if i > start and line.startswith("### "))
    section = "\n".join(lines[start:end])
    statuses = {entry.name: entry.status
                for entry in load_dungeon_manifest(Path("副本索引.md"))}

    expected = sorted({name for names in REGION_EXCLUSIVE_DAOWEN.values()
                       for name in names})
    assert expected, "引擎没有登记任何副本专属道纹，名单来源可能已改名"
    for region, names in REGION_EXCLUSIVE_DAOWEN.items():
        if statuses[region] == "已实现":
            for name in names:
                assert f"【{name}】" in section, f"正文缺少已实现副本道纹【{name}】"
        else:
            draft = (Path("副本") / f"{region}.md").read_text(encoding="utf-8")
            for name in names:
                assert re.search(rf"^\d+\.{re.escape(name)}X", draft, re.M), \
                    f"{region}草案缺少道纹【{name}】定义"

    # 已删除的道纹不得以**生效条文**的形式残留：删除注记（- ~~【X】~~）允许保留，
    # 但活条文会让人以为它还能用。
    for name in ("镇尸", "尸爆", "爆裂", "活血"):
        live = [ln for ln in section.split("\n")
                if f"【{name}】" in ln and not ln.lstrip().startswith("- ~~")]
        assert not live, f"已删除的【{name}】在正文中仍是生效条文: {live}"


# ========================================================================
# B4 命零碎片公式禁止改写
# ========================================================================

def _engine_with_dead_monster(tmp_path, hp=200, daowen=("杀伐",), **mods):
    e = GameEngine(db_path=str(tmp_path / "b4.db"), rng_seed=5)
    e.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8,
                                          "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    e.execute_action("setup_choose_region", {"region": "罪孽都市"})
    foe = Entity(name="靶怪", entity_type="怪物", blood_limit=hp, current_hp=hp,
                 attack_count=1, attack_power=1)
    foe.battle_start_blood_limit = hp
    for name in daowen:
        foe.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="消耗",
                   cost_formula="X", effect_formula=""), x_value=1)
    foe.is_alive = False
    e.state.enemies.append(foe)
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.event_modifiers.update(mods)
    return e


def test_shard_formula_never_scaled_by_event_modifiers(tmp_path):
    """arena_double_loot 不得乘进命零公式，只能作为独立奖金另发。"""
    import math
    base = _engine_with_dead_monster(tmp_path)
    r = base.execute_action("battle_end", {})
    assert r["success"], r
    formula = math.ceil(200 * 0.02) + 1 * 5      # ⌈200×2%⌉ + 1道纹×5 = 4+5 = 9
    assert r["result"]["shard_reward"] == formula
    assert r["result"]["event_bonus_shards"] == 0

    doubled = _engine_with_dead_monster(tmp_path, arena_double_loot=True)
    r2 = doubled.execute_action("battle_end", {})
    assert r2["success"], r2
    # 公式本身不变，翻倍的是另发的「角斗场奖金」
    assert r2["result"]["shard_reward"] == formula, "命零公式被改写了"
    assert r2["result"]["event_bonus_shards"] == formula
    assert any(b["name"] == "角斗场奖金" for b in r2["result"]["event_bonuses"])


def test_bounty_paid_as_separate_line_item(tmp_path):
    """通缉悬赏金的 30 碎片是独立奖金，不并入命零公式。"""
    import math
    e = _engine_with_dead_monster(tmp_path, bounty_reward=30)
    r = e.execute_action("battle_end", {})
    assert r["success"], r
    formula = math.ceil(200 * 0.02) + 5
    assert r["result"]["shard_reward"] == formula
    assert r["result"]["event_bonus_shards"] == 30
    assert any(b["name"] == "悬赏金" for b in r["result"]["event_bonuses"])


# ========================================================================
# B5 死斗守擂侧（怪物）出手预算封顶
# ========================================================================

def test_duel_monster_defender_is_capped_not_unlimited(tmp_path):
    """所有角色基础1出手；守擂怪物同样封顶，不得无限出手。"""
    from tests.test_final_duel import _new_candidate, _finish_battle_7, _cleanup
    from tests.attack_support import resolve_attack

    path = str(tmp_path / "duel.json")
    sealed = _new_candidate("b5_sealed", path, speed_points=4, name="对手")
    _finish_battle_7(sealed)
    ch = _new_candidate("b5_ch", path, speed_points=12, name="挑战者")
    _finish_battle_7(ch)

    mob = Entity(name="守擂怪物", entity_type="怪物", blood_limit=9999,
                 current_hp=9999, attack_count=1, attack_power=0,
                 speed_limit=0, current_speed=0)
    ch.state.enemies.append(mob)
    player = ch.state.player
    budget = ch._action_budget_of(mob)
    assert budget == 1, "怪物与其他角色一样，基础出手预算为1"

    acted = 0
    for _ in range(12):
        if (ch.state.duel_turn == "player_side"
                and player.actions_used_this_round < ch._action_budget_of(player)):
            resolve_attack(ch, player.name, [])
            continue
        if ch.state.duel_turn != "opponent_side":
            break
        if resolve_attack(ch, mob.name, [])["success"]:
            acted += 1
        if ch.state.combat_subphase == "await_round_end":
            break
    assert acted <= budget, f"守擂怪物出手 {acted} 次，超出预算 {budget}"
    _cleanup(path)


# ========================================================================
# D1 血炼周天内置流程 / 血溅五步已删除
# ========================================================================

def test_blood_cycle_spell_has_builtin_flow(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "d1.db"), rng_seed=1)
    flow = e.combat.SPELL_FLOWS.get("血炼周天")
    assert flow is not None, "血炼周天缺内置流程"
    assert flow["loop"] is True
    daowen = [e.combat._step_daowen(s) for s in flow["steps"]]
    assert daowen == ["再生", "透支"], daowen
    assert e.SPELL_REGISTRY.get("血炼周天") == ["再生", "透支"]


def test_blood_splash_removed_from_death_book():
    book = open("死者之书.md", encoding="utf-8").read()
    index = open("法术索引.md", encoding="utf-8").read()
    assert "血溅五步" not in book, "血溅五步应已从《死者之书》删除"
    assert "血溅五步" not in index, "血溅五步应已从《法术索引》删除"


# ========================================================================
# 旧「唯一」支付义项已删除；现行唯一是使用历史禁令，由 resource_daowen 测试覆盖
# ========================================================================

def test_legacy_unique_cost_type_is_retired():
    from engine.enums import SPECIAL_COST_TYPES
    assert "UNIQUE" not in CostType.__members__
    assert SPECIAL_COST_TYPES == {"枯竭", "失忆", "异变", "冷却"}
