"""法术阶级＝重数（2026-10-25 用户令）。

阶数不再按「用了几种道纹」算，而按生效流程写了几重：触发时机一重、每个效果
步骤各一重；循环标记不另计重；瞬发法术没有触发时机，只按步骤计。

用户给的基准例子就是本文件的主用例：血炼周天
「失去生命后→发动再生→发动透支（循环）」＝3 阶＝3 次出手。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from engine.api import GameEngine  # noqa: E402
from engine.daowen import DaoWenEngine  # noqa: E402
from engine.models import DaoWen, DaoWenInstance, Entity  # noqa: E402
from engine.spell_dsl import parse_instant_flow, parse_spell_definition, spell_tier  # noqa: E402
from tests.setup_support import finish_initial_daowen, grant_extra_actions  # noqa: E402


def _tier(trigger, flow):
    known = set(DaoWenEngine.list_all())
    parsed = parse_spell_definition(trigger, flow, known)
    return spell_tier(parsed.trigger, parsed.steps)


# ---------------------------------------------------------------- 正常路径

def test_blood_loop_spell_is_three_tier():
    """血炼周天＝失去生命后（1）＋再生（2）＋透支（3）＝3 阶。"""
    assert _tier("失去生命后", "发动再生X于自身→发动透支X于自身→循环") == 3


def test_single_step_instant_spell_is_one_tier():
    """不带触发时机、直接发动杀伐X＝1 阶。"""
    known = set(DaoWenEngine.list_all())
    parsed = parse_instant_flow("发动杀伐X于目标", known)
    assert spell_tier(parsed.trigger, parsed.steps) == 1


def test_trigger_plus_one_step_is_two_tier():
    """先发制人＝受到伤害前＋杀伐＝2 阶。"""
    assert _tier("受到伤害前", "发动杀伐X于攻击者") == 2


# ---------------------------------------------------------------- 边界条件

def test_loop_marker_adds_no_tier():
    """循环标记不另计重：两段循环体＋触发 = 3 阶，与不带循环时相同。"""
    assert _tier("失去生命后", "发动再生X于自身→发动透支X于自身→循环") == \
        _tier("失去生命后", "发动再生X于自身→发动透支X于自身")


def test_repeated_same_daowen_counts_each_step():
    """同一种道纹出现两次各算一重（不再按种类去重）：触发＋杀伐＋杀伐＝3 阶。"""
    assert _tier("受到伤害前", "发动杀伐X于攻击者→发动杀伐X于攻击者") == 3


def test_both_branches_of_condition_count():
    """条件分支两臂各算重：触发＋（则杀伐）＋（否则透支）＝3 阶。"""
    flow = "若自身 法力 大于等于 2 则 发动杀伐X于攻击者 否则 发动透支X于自身"
    assert _tier("受到伤害前", flow) == 3


# ---------------------------------------------------------------- 错误输入

def test_definition_rejected_when_actions_insufficient(tmp_path):
    """出手不足：拒绝定义、不写入 spells，也不白扣出手。"""
    engine = GameEngine(db_path=str(tmp_path / "tier.db"), rng_seed=3,
                        sealed_candidate_path=str(tmp_path / "tier.json"))
    engine.execute_action("setup_attributes", {"name": "甲", "blood_points": 11,
                                              "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    player = engine.state.player
    for name in ("再生", "透支"):
        player.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="", cost_formula="X", effect_formula=""))
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.enemies.append(Entity("靶怪", "怪物", blood_limit=50, current_hp=50,
                                       attack_count=1, attack_power=1))
    used = player.actions_used_this_round  # 基础出手 2 次，3 阶法术必然放不下

    result = engine.execute_action("define_spell", {"spell": {
        "name": "血炼周天·自定", "required_daowen": ["再生", "透支"],
        "trigger_condition": "失去生命后",
        "effect_flow": "发动再生X于自身→发动透支X于自身→循环"}})

    assert not result["success"], result
    assert "3次出手" in result["error"], result
    assert player.spells == []
    assert player.actions_used_this_round == used, "被拒的定义不得扣出手"


def test_definition_charges_one_action_per_tier(tmp_path):
    """补足出手后：3 阶法术正好扣 3 次出手。"""
    engine = GameEngine(db_path=str(tmp_path / "tier_ok.db"), rng_seed=4,
                        sealed_candidate_path=str(tmp_path / "tier_ok.json"))
    engine.execute_action("setup_attributes", {"name": "乙", "blood_points": 11,
                                              "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    player = engine.state.player
    for name in ("再生", "透支"):
        player.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="", cost_formula="X", effect_formula=""))
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.enemies.append(Entity("靶怪", "怪物", blood_limit=50, current_hp=50,
                                       attack_count=1, attack_power=1))
    grant_extra_actions(player, 5)
    used = player.actions_used_this_round

    result = engine.execute_action("define_spell", {"spell": {
        "name": "血炼周天·自定", "required_daowen": ["再生", "透支"],
        "trigger_condition": "失去生命后",
        "effect_flow": "发动再生X于自身→发动透支X于自身→循环"}})

    assert result["success"], result
    assert result["result"]["tier"] == 3
    assert player.actions_used_this_round == used + 3
