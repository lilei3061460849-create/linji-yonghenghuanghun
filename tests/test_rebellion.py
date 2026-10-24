"""
pytest 风格测试 - 背叛三选一处理分支（镇压/让利/谈判）

2026-10-24 用户令：背叛不再是按数值判定的特殊事件，改为[朋友]／[员工]的自选项——
没有攻击总值门槛，引擎不自动触发，由扮演他们的 AI 自己声明（declare_betrayal）。
原文（规则正文·同伴规则）：
"背叛（[朋友]或[员工]的自选项；不是特殊事件，没有触发阈值）
[朋友]或[员工]可以自己决定要不要背叛、夺取《死者之书》。没有攻击总值门槛，不在[战终]自动判定，
也不由DM代为发动——想夺书的人自己开口，之后的代价由他自己承担。
-镇压：与所有背叛者开启战斗！…若战斗胜利，肃清叛徒并保留财产。
-让利：本次轮回所有[员工]每场工资+5，背叛平息（仅对[员工]背叛有效）。
-谈判：给出合理的谈判方案破解叛乱。
背叛者推翻并取代持有者后，夺得《死者之书》，成为新的轮回者。"

设计要点(用户已确认思路：直接复用现有战斗体系，把背叛员工的面板"当出怪"塞进state.enemies)：
1. suppress_rebellion：把state.employees整体搬进state.enemies(保留其完整面板与道纹)，
   之后战斗完全走已有的 round_start/attack/use_daowen/monster_phase/round_end 流程，
   没有引入任何新的战斗计算逻辑。
2. resolve_rebellion_battle(outcome=victory/defeat)：victory=保留碎片、叛徒清空(不产生击杀奖励)；
   defeat=清空碎片、存活叛徒永久离开(不回员工名单)。"战斗失败"与"主动撤退"统一按defeat结算。
3. appease_rebellion：全局+5工资加成(wage_bonus)，叠加在12碎片封顶之后。
4. negotiate_rebellion：走标准Interrupt流程交DM裁定，不在引擎里编造判定逻辑。
5. 三个分支都要求 rebellion_active(即[战终]检查命中)，除非显式传 force=True。

运行方式：
    python -m pytest tests/test_rebellion.py -v
"""
import os
from tests.setup_support import finish_initial_daowen
os.makedirs("/tmp/linji_tests", exist_ok=True)
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance


def _setup_with_rebellion(db_suffix: str) -> GameEngine:
    """构造一个[员工]已声明背叛、等待处理（rebellion_active=True）的局面"""
    engine = GameEngine(db_path=f"data/test_rebellion_{db_suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.phase = "in_combat"
    engine.state.player.current_hp = 5  # 很低，容易被员工攻击总值超过
    engine.state.player.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    engine.state.shards = 100
    emp = Entity(name="彪悍打手", entity_type="员工", blood_limit=50, current_hp=50,
                 attack_count=5, attack_power=5, is_deployed=True)  # 攻击总值25 ≥ 玩家HP5
    engine.state.employees.append(emp)
    engine.state.enemies.clear()
    r = engine.execute_action("declare_betrayal", {"names": ["彪悍打手"]})
    assert r["success"] is True, r
    assert engine.state.rebellion_active is True, "测试前置条件：员工声明背叛后应待处理"
    return engine


# ========================================================================
# 正常路径
# ========================================================================

def test_suppress_moves_employees_into_enemies_with_full_panel():
    """正常路径：镇压把所有员工原样搬进state.enemies，面板与道纹完整保留"""
    engine = _setup_with_rebellion("suppress_ok")
    emp = engine.state.employees[0]
    emp.dao_wen["弱化"] = DaoWenInstance(DaoWen(name="弱化", formula="", cost_type="消耗", cost_formula="X", effect_formula=""), x_value=3)

    r = engine.execute_action("suppress_rebellion", {})
    assert r["success"] is True
    assert engine.state.employees == []
    assert len(engine.state.enemies) == 1
    rebel = engine.state.enemies[0]
    assert rebel.name == "彪悍打手"
    # 镇压不再夹带[战终]攻次成长：面板原样搬进敌方（2026-10-24 起背叛是自选，不经战终判定）
    assert rebel.attack_count == 5 and rebel.attack_power == 5 and rebel.current_hp == 50
    assert "弱化" in rebel.dao_wen
    assert engine.state.rebellion_in_progress is True
    assert engine.state.rebellion_active is False


def test_suppress_battle_uses_existing_combat_flow_unmodified():
    """正常路径：镇压后的战斗完全复用现有 round_start/attack/use_daowen/round_end 流程，
    不需要任何专门为背叛新增的战斗计算代码"""
    engine = _setup_with_rebellion("reuse_flow")
    engine.execute_action("suppress_rebellion", {})
    rebel = engine.state.enemies[0]

    engine.execute_action("round_start", {})
    # 法点6 → 法限3：杀伐 x 最多3；3×5=15 伤害（DM裁定 2026-09-10：5X）
    r_atk = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 3, "target": "彪悍打手"})
    assert r_atk["success"] is True, r_atk
    assert rebel.current_hp == 50 - 9   # 杀伐3→X²=9
    prepared = engine.execute_action("prepare_monster_phase", {})
    actor = prepared["result"]["actors"][0]
    attacks = [{"hits": [{"target_ref": "player:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}
                          for _ in range(actor["base_hits_per_attack"])]}
               for _ in range(actor["base_attack_actions"])]
    r_phase = engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"],
        "choices": [{"actor_ref": actor["actor_ref"], "daowen": None,
                     "attack_actions": attacks}],
    })
    assert r_phase["success"] is True
    engine.execute_action("round_end", {})


def test_victory_keeps_shards_and_clears_rebels():
    """正常路径：镇压胜利=保留碎片(不额外扣、不额外奖励)，叛徒清空"""
    engine = _setup_with_rebellion("victory_ok")
    engine.execute_action("suppress_rebellion", {})
    shards_before = engine.state.shards
    r = engine.execute_action("resolve_rebellion_battle", {"outcome": "victory"})
    assert r["success"] is True
    assert engine.state.shards == shards_before, "胜利不应扣碎片，也不应有额外击杀奖励"
    assert engine.state.enemies == []
    assert engine.state.employees == [], "叛徒被肃清，不回到员工名单"
    assert engine.state.rebellion_in_progress is False


def test_defeat_loses_all_shards_and_survivors_escape_with_loot():
    """正常路径：镇压失败(或撤退)=失去全部碎片，存活叛徒永久离开(不回员工名单)"""
    engine = _setup_with_rebellion("defeat_ok")
    engine.execute_action("suppress_rebellion", {})
    r = engine.execute_action("resolve_rebellion_battle", {"outcome": "defeat"})
    assert r["success"] is True
    assert engine.state.shards == 0
    assert r["result"]["escaped_with_loot"] == ["彪悍打手"]
    assert engine.state.employees == [], "逃跑的叛徒不应回到员工名单"
    assert engine.state.enemies == []


def test_appease_adds_wage_bonus_and_resolves_without_combat():
    """正常路径：让利不开战，全局工资+5，立即平息叛乱"""
    engine = _setup_with_rebellion("appease_ok")
    r = engine.execute_action("appease_rebellion", {})
    assert r["success"] is True
    assert engine.state.wage_bonus == 5
    assert engine.state.rebellion_active is False
    assert engine.state.employees != [], "让利不应触发战斗，员工原样留任"


def test_appease_wage_bonus_applies_on_top_of_cap_in_next_wage_calc():
    """正常路径：让利后的+5工资加成，在下一次工资结算里生效(叠加在12碎片封顶之后)"""
    engine = _setup_with_rebellion("appease_wage_effect")
    engine.execute_action("appease_rebellion", {})
    emp = engine.state.employees[0]
    # 上一次battle_end已把该员工重置为待命，模拟"下一场重新部署参战"
    emp.is_deployed = True
    emp.deployed_at_round = engine.state.current_round
    engine.state.pending_wage_decisions = {}
    engine.state.phase = "in_combat"  # 构造下一场已结束、等待工资结算的合法战终前状态
    engine.execute_action("battle_end", {})
    wage = engine.state.pending_wage_decisions.get("彪悍打手")
    assert wage is not None
    assert wage == min(12, 2 * (1 + 1)) + 5, f"应在原公式基础上+5，实际{wage}"


def test_negotiate_raises_interrupt_for_dm_not_auto_resolved():
    """正常路径：谈判必须走Interrupt交DM裁定，引擎不得自行编造判定结果"""
    engine = _setup_with_rebellion("negotiate_ok")
    r = engine.execute_action("negotiate_rebellion", {"proposal": "承诺提高工资并优先偿还历史欠薪"})
    assert r["success"] is True
    assert "interrupt" in r
    assert engine._pending_interrupts, "应产生待DM裁定的中断，不能自动判定成功或失败"
    assert engine.state.rebellion_active is True, "谈判尚未经DM裁定前，背叛仍应视为未解决"


# ========================================================================
# 边界条件
# ========================================================================

def test_force_bypasses_threshold_check():
    """边界：force=True时跳过"须先有人声明背叛"的前置，直接处理（供DM/剧本强制场景使用）"""
    engine = GameEngine(db_path="/tmp/linji_tests/test_rebellion_force.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    emp = Entity(name="小虾米", entity_type="员工", blood_limit=10, current_hp=10,
                 attack_count=1, attack_power=1, is_deployed=True)
    engine.state.employees.append(emp)
    assert engine.state.rebellion_active is False

    r = engine.execute_action("suppress_rebellion", {"force": True})
    assert r["success"] is True
    assert engine.state.enemies[0].name == "小虾米"


def test_suppress_with_no_employees_rejected():
    """边界：没有任何员工时不能"镇压"，应报错而不是空手开战"""
    engine = GameEngine(db_path="/tmp/linji_tests/test_rebellion_empty.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    r = engine.execute_action("suppress_rebellion", {"force": True})
    assert r["success"] is False


def test_multiple_employees_all_rebel_together():
    """边界：force镇压仍把全部[员工]一起搬入state.enemies，不是只挑一个"""
    engine = GameEngine(db_path="/tmp/linji_tests/test_rebellion_multi.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    for i in range(3):
        engine.state.employees.append(Entity(name=f"员工{i}", entity_type="员工", blood_limit=20,
                                              current_hp=20, attack_count=1, attack_power=1, is_deployed=True))
    r = engine.execute_action("suppress_rebellion", {"force": True})
    assert r["success"] is True
    assert len(engine.state.enemies) == 3
    assert engine.state.employees == []


# ========================================================================
# 错误输入 / 非法配置
# ========================================================================

def test_resolve_rejected_without_active_rebellion_battle():
    """错误输入：没有进行中的镇压战斗时调用resolve_rebellion_battle必须报错"""
    engine = GameEngine(db_path="/tmp/linji_tests/test_rebellion_no_battle.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    r = engine.execute_action("resolve_rebellion_battle", {"outcome": "victory"})
    assert r["success"] is False


def test_resolve_rejects_invalid_outcome_value():
    """错误输入：outcome必须是victory/defeat二选一，非法值必须被拒绝"""
    engine = _setup_with_rebellion("bad_outcome")
    engine.execute_action("suppress_rebellion", {})
    r = engine.execute_action("resolve_rebellion_battle", {"outcome": "maybe_win"})
    assert r["success"] is False
    assert engine.state.rebellion_in_progress is True, "非法输入不应改变进行中状态"


def test_branches_rejected_without_active_rebellion_and_without_force():
    """错误输入：没有待处理背叛且未传force时，三个分支都必须拒绝，不能平白无故触发"""
    engine = GameEngine(db_path="/tmp/linji_tests/test_rebellion_noactive.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.state.employees.append(Entity(name="老实人", entity_type="员工", blood_limit=10,
                                          current_hp=10, attack_count=1, attack_power=1, is_deployed=True))
    assert engine.execute_action("suppress_rebellion", {})["success"] is False
    assert engine.execute_action("appease_rebellion", {})["success"] is False
    assert engine.execute_action("negotiate_rebellion", {"proposal": "随便说说"})["success"] is False


def test_negotiate_rejects_empty_proposal():
    """错误输入：不能提交空谈判方案就要求平息叛乱"""
    engine = _setup_with_rebellion("empty_proposal")
    r = engine.execute_action("negotiate_rebellion", {"proposal": ""})
    assert r["success"] is False
    assert engine.state.rebellion_active is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
