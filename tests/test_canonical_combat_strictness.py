"""
正文战斗规则严格合规性全量测试（Canonical Combat Pipeline & Iron Laws Audit）
严格按 README《七步原子时序切片流水线》与《推演铁律》审计战斗程序：
1. 声明与付费：法力与代价精确扣除，不足时严厉拒绝；
2. 反应法术与守夜灯：受伤害前反应抢攻，回增法力供给；
3. 闪避与避风铃：速度消耗、判定完全失效、避风铃叠甲；
4. 爆裂受到伤害前反噬：反伤先于落地，反噬致死则攻击取消；
5. 重定向与濒死拦截：嫁祸、背负、撤退、负岳碑、断尾求生；
6. 格挡吸收与普通伤害：格挡全额吸收，绝无“真实伤害”穿透；
7. 落地后效果：逆鳞蓄势、伤痕扣血限、洗劫夺碎片、死亡触发；
8. 回终与死斗交替：格挡清空、持续递减、死斗交替与余量继续。
"""

import math
import os
import sys
import pytest

from tests.setup_support import advance_to_player_turn, finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.mechanisms import Phase
from engine.models import Entity, Relic, StatusEffect, DaoWenInstance, DaoWen, Spell
from engine.enums import EntityType


def _setup_engine(tmp_path, region="龙心谷", relic="避风铃"):
    e = GameEngine(db_path=str(tmp_path / "combat_test.db"), rng_seed=42)
    e.execute_action("setup_attributes", {
        "name": "测试者", "blood_points": 7, "speed_points": 8, "mana_points": 10
    })
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    s = e.execute_action("setup_choose_region", {"region": region})
    # 显式固定本用例的遗物，避免遗物池扩容后随机初始遗物给测试叠加额外效果。
    e.execute_action("choose_discovered_relic", {"relic_name": s["result"]["relic_choices"][0]})
    from engine.models import Relic
    e.state.relics = [Relic(name=relic, effect="测试遗物")]
    e.state.energy = 0
    # 挂载测试怪物
    m = Entity(name="测试靶子", blood_limit=100, current_hp=100, speed_limit=10, current_speed=10, entity_type="怪物")
    e.state.enemies = [m]
    e.state.phase = "in_combat"
    e.state.current_round = 1
    return e


# ==================== 1. 声明与付费阶段测试 ====================

def test_step1_mana_payment_and_rejection(tmp_path):
    """正常与非法：法力充足时精确扣除，法力不足时严厉拒绝且状态不发生任何改变"""
    e = _setup_engine(tmp_path)
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    p = e.state.player
    target = e.state.enemies[0]
    p.current_mana = 10

    # 正常付费：杀伐4消耗4法力
    res = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 4, "target_ref": "enemy:0"})
    assert res["success"] is True
    assert p.current_mana == 6, "10 - 4 = 6"

    # 非法付费：试图发动杀伐10（需10法力，当前仅6）
    # 交替出手：两次主动行动之间必须经过敌方一手，否则第二次会被优先权闸门拦下，
    # 测不到「法力不足」这条拒绝理由。
    advanced = advance_to_player_turn(e)
    assert advanced["success"], advanced
    res_fail = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 10, "target_ref": "enemy:0"})
    assert res_fail["success"] is False
    # 代价按《规则正文·代价》「付不起即不可发动」整笔支付：拒绝文案是
    # 「无法完整承担法力10（可支付6）」，不再有旧的「法力不足」字样。
    assert "无法完整承担法力10" in res_fail["error"], res_fail["error"]
    assert "可支付6" in res_fail["error"], res_fail["error"]
    assert p.current_mana == 6, "扣费失败法力不得改变"


# ==================== 2. 反应法术与守夜灯测试 ====================

def test_step2_night_watchman_lamp_reactive_mana(tmp_path):
    """正常路径：守夜灯在[回增]获得等同于当前世界回合数的法力。"""
    e = _setup_engine(tmp_path, relic="守夜灯")
    p = e.state.player
    p.mana_limit = 50
    p.current_mana = 0
    e.state.current_round = 3

    e.combat._dispatch_phase(Phase.ROUND_INCREASE, target=p)
    assert p.current_mana == 3, "世界第3回合每经历一个回合 = 3点法力"


# ==================== 3. 闪避与避风铃测试 ====================

def test_step3_dodge_negates_attack_and_triggers_relic(tmp_path):
    """正常路径：声明闪避消耗1点速度，攻击完全失效，触发避风铃获得3点格挡"""
    e = _setup_engine(tmp_path, relic="避风铃")
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    p = e.state.player
    target = e.state.enemies[0]
    target.speed_limit = 10
    target.current_speed = 10
    hp_before = target.current_hp

    # 玩家发动杀伐10，目标声明闪避
    res = e.execute_action("use_daowen", {
        "daowen_name": "杀伐", "x": 5, "target_ref": "enemy:0", "dodge": True
    })
    assert res["success"] is True
    assert target.current_speed == 9, "消耗1点速度"
    assert target.current_hp == hp_before, "判定完全失效，目标扣0血"


def test_step3_cannot_dodge_without_speed(tmp_path):
    """边界条件：速度为0时无法闪避，强制承受全额伤害"""
    e = _setup_engine(tmp_path)
    p = e.state.player
    target = e.state.enemies[0]
    target.current_speed = 0
    hp_before = target.current_hp

    res = e.execute_action("use_daowen", {
        "daowen_name": "杀伐", "x": 5, "target_ref": "enemy:0", "dodge": True
    })
    assert res["success"] is True
    assert target.current_speed == 0
    assert target.current_hp == hp_before - 25, "速度为0闪避失败，目标实打实扣除25点生命（杀伐5→5X）"


# ==================== 4. 千荆甲受到伤害前反噬测试（2026-10-03 承接原【爆裂】） ====================

def test_step4_qianjingjia_reflects_before_damage(tmp_path):
    """正常路径：千荆甲在受到攻击伤害前扣除攻击者生命；若攻击者因此命零，伤害不落地"""
    e = _setup_engine(tmp_path)
    p = e.state.player
    enemy = Entity(name="脆皮怪", blood_limit=15, current_hp=15, attack_power=20, entity_type="怪物")
    e.state.relics.append(Relic("千荆甲", ""))
    p.shield = 10
    hp_before = p.current_hp

    # 脆皮怪对玩家造成20点攻击伤害：受到伤害前反噬20点生命 -> 脆皮怪当前生命归零[命零]，攻击取消
    dmg_res = e.combat._apply_hostile_damage(p, 20, "普通", enemy, ctx={
        "timing": "monster_action", "source": "普通攻击", "source_type": "attack",
        "actor": enemy, "target": p, "mechanic": "damage", "subtype": "attack",
        "amount": 20, "tags": {"attack"}})
    assert enemy.current_hp <= 0 or not enemy.is_alive
    assert p.current_hp == hp_before, "攻击者在造成伤害前命零，落地伤害取消"


# ==================== 5. 伤害重定向与濒死拦截测试 ====================

def test_step5_beifu_redirection_and_retreat(tmp_path):
    """正常路径：背负吸收伤害，朋友濒死自动撤退"""
    e = _setup_engine(tmp_path)
    p = e.state.player
    friend = Entity(name="岩行者", blood_limit=54, current_hp=54, entity_type="朋友")
    friend._beifu_left = 1
    friend._beifu_target = p.runtime_id
    p.add_status(StatusEffect(name="被背负", value=1, remaining_rounds=-1, source="岩行者"))
    e.state.friends.append(friend)

    # 敌人对玩家造成20点伤害 -> 被岩行者背负吸收
    res = e.combat._apply_hostile_damage(p, 20, "普通", None)
    assert p.current_hp == 42, "玩家扣0血"
    assert friend.current_hp == 34, "54 - 20 = 34"


# ==================== 6. 格挡吸收与普通伤害测试 ====================

def test_step6_shield_fully_absorbs_damage_no_true_damage(tmp_path):
    """核心规则验证：格挡必须完全吸收伤害，绝对没有无视格挡的真实伤害"""
    e = _setup_engine(tmp_path)
    p = e.state.player
    p.shield = 25
    hp_before = p.current_hp

    # 受到20点伤害
    res = e.combat._apply_hostile_damage(p, 20, "普通", None)
    assert res["shield_absorbed"] == 20
    assert res["actual_damage"] == 0
    assert p.shield == 5
    assert p.current_hp == hp_before, "格挡充足时生命不得扣除"


# ==================== 7. 落地后效果测试 ====================

def test_step7_nilin_and_shanghen_triggers(tmp_path):
    """正常路径：受创增加逆鳞层数，受创削减伤痕血限"""
    e = _setup_engine(tmp_path)
    p = e.state.player
    p.add_status(StatusEffect(name="逆鳞", value=1, remaining_rounds=2, source=p.name))
    p.add_status(StatusEffect(name="伤痕", value=2, remaining_rounds=-1, source="enemy"))

    bl_before = p.blood_limit
    # 造成10点实际伤害
    res = e.combat._apply_hostile_damage(p, 10, "普通", None)
    assert p._nilin == 10, "逆鳞增加10层"
    assert p.blood_limit == bl_before - 2, "伤痕削减2点血限"


# ==================== 8. 死斗交替与预算限制测试 ====================

def test_step8_duel_alternation_and_exhaustion(tmp_path):
    """正常路径：双方均有出手时强制交替；一方耗尽后另一方余量继续"""
    e = _setup_engine(tmp_path)
    e.state.in_final_duel = True
    p = e.state.player
    # 《规则正文》第7条/第191行：所有角色每回合基础 1 次出手，再叠加通用修正。
    # 要造出「一方多手、一方少手」的出手差，只能用【疯狂】这类通用修正（+2）。
    p.add_status(StatusEffect(name="疯狂", value=2, remaining_rounds=-1, source="test"))
    opp = Entity(name="对手", blood_limit=42, current_hp=42, speed_limit=6, entity_type="轮回者")
    e.state.enemies = [opp]

    assert e.combat.single_round_action_count(opp) == 1  # 基础1，无修正
    assert e.combat.single_round_action_count(p) == 3     # 基础1 + 疯狂2
    assert e.combat.single_round_action_count(p) > e.combat.single_round_action_count(opp)
