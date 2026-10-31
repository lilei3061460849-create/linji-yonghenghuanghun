"""
pytest 风格测试 - 里程碑2：员工经济系统（出战支援 / 战终工资 pay-refuse / 解雇 / 失信黑名单）

范围声明：本文件覆盖的内容——
1. 雇佣(罪孽都市专属)真正创建员工Entity(此前为纯占位，未创建任何对象)，默认待命(is_deployed=False)
2. deploy_employee：派遣出战，员工必须先部署才计入战场(get_all_player_side)与工资结算
3. 战终工资结算门槛：pending_wage_decisions 非空时返回completed=False中间态，需逐个 pay_employee_wage 决策
4. dismiss_employee：自由解雇
5. 失信黑名单：累计3次(拒付/解雇/死亡离队) -> is_blacklisted，之后雇佣被拒绝
6. "还债"转化员工独立于本套经济(自动部署、不产生工资决策)——设计取舍見随消息附带的进度报告

不在本文件覆盖范围内：朋友/员工自动出手实际战斗行为、撤退机制、夺书战斗、
第8场最终死斗、龙心谷"炼心"具体效果、雇佣后的"发现并选择转化道纹"子步骤。

运行方式：
    python -m pytest tests/test_employee_economy.py -v
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from engine.models import Entity, GameState, StatusEffect
from engine.combat import CombatEngine
from engine.dice import DiceEngine


def _finish_round_without_monster_actions(engine):
    engine.state.combat_subphase = "await_round_end"
    return engine.execute_action("round_end", {})


def _new_engine(db_suffix: str) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_emp_{db_suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.energy = 3
    return engine


def _hire(engine, params):
    result = engine.execute_action("pre_battle_action", params)
    if result.get("success"):
        name = params["name"]
        choices = engine.state.pending_daowen_choices[name]
        chosen = engine.execute_action("choose_hired_daowen", {"name": name, "daowen": choices[0]})
        assert chosen["success"]
    return result


def _start_battle(engine):
    engine.state.energy = 0
    choices = {}
    engine.state.relics.clear()  # 工资/派遣夹具不依赖随机遗物被动效果。
    # 2026-10-13：【三相残韵盘】删除，战始显式决策预填不再需要。
    result = engine.execute_action("battle_start", {"relic_choices": choices})
    engine.state.enemies.clear()  # 本文件只测员工经济，不保留随机怪物。
    # 2026-10-12 出怪下界 1→2 之后，第1场可能抽出 N=2 → 除首发外还有增援排队。
    # 只清 enemies 会把增援留在队列里，战终门禁（"仍有N只怪物增援未进场"）就此卡死。
    # 本文件的意图是"不保留任何随机怪物"，所以增援队列要一并清掉。
    engine.state.monster_reinforcements = []
    return result


# ========================================================================
# 正常路径
# ========================================================================

def test_hire_creates_real_employee_not_deployed():
    """正常路径：雇佣必须真正创建Entity并加入state.employees，默认待命(is_deployed=False)"""
    engine = _new_engine("hire_ok")
    r = _hire(engine, {
        "sub_action": "雇佣", "name": "打手甲", "blood_alloc": 5, "atk_bundles": 5,
    })
    assert r["success"] is True, r
    emp = next((e for e in engine.state.employees if e.name == "打手甲"), None)
    assert emp is not None, "雇佣后必须在 state.employees 里真正存在该员工实体"
    assert emp.blood_limit == 60 and emp.current_hp == 60, "5点分配值*12=60血限"
    assert emp.attack_count == 5 and emp.attack_power == 10, "5个捆绑=5攻击次数+10攻击力"
    assert emp.is_deployed is False, "新雇佣员工必须默认待命，不占场"


def test_deploy_employee_then_counts_as_battlefield_entity():
    """正常路径：deploy_employee后，该员工才计入 get_all_player_side()（可被选中/参战）"""
    engine = _new_engine("deploy_ok")
    _hire(engine, {
        "sub_action": "雇佣", "name": "打手乙", "blood_alloc": 8, "atk_bundles": 4,
    })
    emp = next(e for e in engine.state.employees if e.name == "打手乙")
    assert emp not in engine.state.get_all_player_side(), "未部署前不应计入战场"

    _start_battle(engine)
    engine.execute_action("round_start", {})
    r = engine.execute_action("deploy_employee", {"name": "打手乙"})
    assert r["success"] is True, r
    assert emp.is_deployed is True
    assert emp in engine.state.get_all_player_side(), "部署后必须计入战场"


def test_wage_settlement_pay_then_battle_end_succeeds():
    """正常路径：3个完整回合参战 -> 工资=min(12,2*(1+3))=8 -> pay -> battle_end 成功"""
    engine = _new_engine("wage_pay")
    engine.state.shards = 100
    _hire(engine, {
        "sub_action": "雇佣", "name": "小张", "blood_alloc": 2, "atk_bundles": 6,
    })
    _start_battle(engine)
    engine.execute_action("round_start", {})  # 第1回合开始，current_round=1
    d = engine.execute_action("deploy_employee", {"name": "小张"})
    assert d["success"] is True
    emp = next(e for e in engine.state.employees if e.name == "小张")
    assert emp.deployed_at_round == 1

    # 走完3个完整回合(round1已在上面start过，这里补上round1的end，再走round2/round3)
    _finish_round_without_monster_actions(engine)
    engine.execute_action("round_start", {})
    _finish_round_without_monster_actions(engine)
    engine.execute_action("round_start", {})
    _finish_round_without_monster_actions(engine)
    assert engine.state.current_round == 3

    blocked = engine.execute_action("battle_end", {})
    assert blocked["success"] is True and blocked["completed"] is False, \
        "工资待决是成功生成的中间结算，不应伪装成失败行动"
    assert blocked["pending_wage_decisions"] == {"小张": 8}, \
        f"3回合参战工资应为min(12,2*(1+3))=8，实际{blocked['pending_wage_decisions']}"

    before_shards = engine.state.shards
    paid = engine.execute_action("pay_employee_wage", {"name": "小张", "decision": "pay"})
    assert paid["success"] is True
    assert engine.state.shards == before_shards - 8

    finished = engine.execute_action("battle_end", {})
    assert finished["success"] is True, finished
    assert "小张" not in engine.state.pending_wage_decisions
    assert emp.is_alive is True, "支付工资后员工应留存"
    assert emp.is_deployed is False, "战终后应回到待命状态，下一场需重新部署"


def test_refuse_wage_forces_departure_and_blacklist_increment():
    """正常路径：拒付工资 -> 强制离队 + 黑名单计数+1"""
    engine = _new_engine("wage_refuse")
    _hire(engine, {
        "sub_action": "雇佣", "name": "老王", "blood_alloc": 8, "atk_bundles": 4,
    })
    _start_battle(engine)
    engine.execute_action("round_start", {})
    engine.execute_action("deploy_employee", {"name": "老王"})

    engine.execute_action("battle_end", {})  # 触发工资计算，因待决而阻塞
    assert engine.state.blacklist_level == 0
    refused = engine.execute_action("pay_employee_wage", {"name": "老王", "decision": "refuse"})
    assert refused["success"] is True
    assert not any(e.name == "老王" for e in engine.state.employees), "拒付后必须强制离队"
    assert engine.state.blacklist_level == 1

    finished = engine.execute_action("battle_end", {})
    assert finished["success"] is True


def test_dismiss_employee_free_and_immediate():
    """正常路径：解雇是自由行动，无代价，立即移除，不结算工资"""
    engine = _new_engine("dismiss_ok")
    _hire(engine, {
        "sub_action": "雇佣", "name": "阿强", "blood_alloc": 17, "atk_bundles": 1,
    })
    before_shards = engine.state.shards
    r = engine.execute_action("dismiss_employee", {"name": "阿强"})
    assert r["success"] is True
    assert not any(e.name == "阿强" for e in engine.state.employees)
    assert engine.state.shards == before_shards, "解雇不应产生任何碎片支出"
    assert engine.state.blacklist_level == 1


def test_debt_bound_employee_exempt_from_deployment_and_wage():
    """正常路径：还债转化员工自动 is_deployed=True，且不出现在战终待决工资列表里"""
    state = GameState()
    state.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60)
    monster = Entity(name="毒枭", entity_type="怪物", blood_limit=100, current_hp=100,
                      attack_count=2, attack_power=10, shards=-15)
    state.enemies.append(monster)
    combat = CombatEngine(state, DiceEngine())
    result = combat._debt_bind_monster(monster)
    assert result["type"] == "debt_bind"
    assert monster.is_deployed is True, "还债转化员工应'视为其参战'，立即部署"
    assert monster in state.get_all_player_side()
    assert monster.is_debt_bound is True


# ========================================================================
# 边界条件
# ========================================================================

def test_deploy_already_deployed_is_rejected():
    """边界：重复部署同一员工应报错，而不是静默成功或叠加状态"""
    engine = _new_engine("deploy_dup")
    _hire(engine, {
        "sub_action": "雇佣", "name": "重复哥", "blood_alloc": 17, "atk_bundles": 1,
    })
    _start_battle(engine)
    engine.execute_action("round_start", {})
    first = engine.execute_action("deploy_employee", {"name": "重复哥"})
    assert first["success"] is True
    second = engine.execute_action("deploy_employee", {"name": "重复哥"})
    assert second["success"] is False


def test_blacklist_triggers_exactly_at_three_departures():
    """边界：累计正好3次离队(解雇)才触发is_blacklisted，第2次时仍未触发"""
    engine = _new_engine("blacklist_edge")
    for i in range(2):
        _hire(engine, {
            "sub_action": "雇佣", "name": f"路人{i}", "blood_alloc": 17, "atk_bundles": 1,
        })
        engine.execute_action("dismiss_employee", {"name": f"路人{i}"})
    assert engine.state.blacklist_level == 2
    assert engine.state.is_blacklisted is False, "累计2次不应触发黑名单"

    _hire(engine, {
        "sub_action": "雇佣", "name": "路人2", "blood_alloc": 17, "atk_bundles": 1,
    })
    engine.execute_action("dismiss_employee", {"name": "路人2"})
    assert engine.state.blacklist_level == 3
    assert engine.state.is_blacklisted is True, "累计满3次必须触发黑名单"


def test_wage_single_round_gives_floor_wage():
    """边界：部署后仅参战当前这1个回合就结束战斗，工资=min(12,2*(1+1))=4，而不是0或报错"""
    engine = _new_engine("wage_zero_round")
    engine.state.shards = 100
    _hire(engine, {
        "sub_action": "雇佣", "name": "秒退", "blood_alloc": 17, "atk_bundles": 1,
    })
    _start_battle(engine)
    engine.execute_action("round_start", {})
    engine.execute_action("deploy_employee", {"name": "秒退"})
    blocked = engine.execute_action("battle_end", {})
    assert blocked["pending_wage_decisions"] == {"秒退": 4}


# ========================================================================
# 错误输入 / 非法配置：校验器应当拒绝
# ========================================================================

def test_hire_rejects_illegal_budget_allocation():
    """错误输入：blood_alloc + 3*atk_bundles 必须恰好=20，非法分配必须被拒绝且不创建员工"""
    engine = _new_engine("hire_bad_budget")
    before_count = len(engine.state.employees)
    r = _hire(engine, {
        "sub_action": "雇佣", "name": "超支哥", "blood_alloc": 5, "atk_bundles": 6,  # 5+18=23≠20
    })
    assert r["success"] is False
    assert len(engine.state.employees) == before_count, "非法预算不应创建任何员工实体"


def test_hire_allows_zero_attack_count_boundary():
    """R05边界：允许把20点全投血限；该员工合法存在，但出手预算为0。"""
    engine = _new_engine("hire_zero_atk")
    before_count = len(engine.state.employees)
    r = _hire(engine, {
        "sub_action": "雇佣", "name": "纯坦克", "blood_alloc": 20, "atk_bundles": 0,
    })
    assert r["success"] is True
    assert len(engine.state.employees) == before_count + 1
    emp = next(e for e in engine.state.employees if e.name == "纯坦克")
    # 出手预算与攻击次数解耦：基础 1 次，不由攻击次数推导。0 攻次员工仍有 1 次出手，
    # 只是[攻次]=[当前速度]=0 → 普攻 0 击；这 1 次出手可以拿去发动道纹。
    assert emp.attack_count == 0 and emp.action_count == 1

    r2 = _hire(engine, {
        "sub_action": "雇佣", "name": "合法员工", "blood_alloc": 17, "atk_bundles": 1,
    })
    assert r2["success"] is True


def test_hire_rejects_when_blacklisted():
    """错误输入：is_blacklisted=True 时雇佣动作必须被拒绝"""
    engine = _new_engine("hire_blacklisted")
    engine.state.is_blacklisted = True
    r = _hire(engine, {
        "sub_action": "雇佣", "name": "黑名单后来客", "blood_alloc": 17, "atk_bundles": 1,
    })
    assert r["success"] is False
    assert not any(e.name == "黑名单后来客" for e in engine.state.employees)


def test_pay_wage_with_insufficient_shards_is_rejected_not_auto_refused():
    """错误输入：碎片不足时选择pay必须被拒绝(报错)，不能静默转成拒付，也不能强行倒扣负数碎片"""
    engine = _new_engine("wage_insufficient")
    engine.state.shards = 3  # 明显不够付工资(3回合参战工资=8)
    _hire(engine, {
        "sub_action": "雇佣", "name": "穷雇主专属员工", "blood_alloc": 17, "atk_bundles": 1,
    })
    _start_battle(engine)
    engine.execute_action("round_start", {})
    engine.execute_action("deploy_employee", {"name": "穷雇主专属员工"})
    _finish_round_without_monster_actions(engine)
    engine.execute_action("round_start", {})
    _finish_round_without_monster_actions(engine)
    engine.execute_action("round_start", {})
    _finish_round_without_monster_actions(engine)  # 3回合 -> 工资=8 > 3碎片
    engine.execute_action("battle_end", {})  # 触发待决计算

    r = engine.execute_action("pay_employee_wage", {"name": "穷雇主专属员工", "decision": "pay"})
    assert r["success"] is False, "碎片不足时pay必须被拒绝"
    assert engine.state.shards == 3, "被拒绝的支付不能扣款"
    assert engine.state.pending_wage_decisions.get("穷雇主专属员工") == 8, \
        "被拒绝后该员工应仍处于待决状态且工资金额不变"


def test_battle_end_blocked_until_all_pending_wages_resolved():
    """错误输入/非法状态：只要还有任意一名员工未决策，battle_end必须持续失败，不能被绕过"""
    engine = _new_engine("wage_block_multi")
    engine.state.shards = 100
    for name, alloc in [("甲", 8), ("乙", 17)]:
        _hire(engine, {
            "sub_action": "雇佣", "name": name, "blood_alloc": alloc, "atk_bundles": (20 - alloc) // 3,
        })
    _start_battle(engine)
    engine.execute_action("round_start", {})
    # 单行动契约：全员基础 1 次出手，同回合派两人要先补一次出手（通用修正，不是特例）。
    engine.state.player.add_status(
        StatusEffect(name="疯狂", value=1, remaining_rounds=-1, source="test"))
    assert engine.execute_action("deploy_employee", {"name": "甲"})["success"]
    assert engine.execute_action("deploy_employee", {"name": "乙"})["success"]
    _finish_round_without_monster_actions(engine)

    r1 = engine.execute_action("battle_end", {})
    assert r1["success"] is True and r1["completed"] is False
    assert len(r1["pending_wage_decisions"]) == 2

    engine.execute_action("pay_employee_wage", {"name": "甲", "decision": "pay"})
    r2 = engine.execute_action("battle_end", {})
    assert r2["success"] is True and r2["completed"] is False, \
        "还有'乙'未决策，应继续返回工资待决中间态"
    assert list(r2["pending_wage_decisions"].keys()) == ["乙"]

    engine.execute_action("pay_employee_wage", {"name": "乙", "decision": "pay"})
    r3 = engine.execute_action("battle_end", {})
    assert r3["success"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_deployed_employee_grows_but_benched_does_not():
    """回归：战终参战员工攻次+1，待命员工不成长（r8B5实测：重置顺序bug致参战者永不成长）。"""
    engine = _new_engine("growth_fix")
    engine.state.shards = 100
    _hire(engine, {
        "sub_action": "雇佣", "name": "参战者", "blood_alloc": 2, "atk_bundles": 6,
    })
    _hire(engine, {
        "sub_action": "雇佣", "name": "板凳", "blood_alloc": 2, "atk_bundles": 6,
    })
    _start_battle(engine)
    engine.execute_action("round_start", {})
    d = engine.execute_action("deploy_employee", {"name": "参战者"})
    assert d["success"] is True
    battler = next(e for e in engine.state.employees if e.name == "参战者")
    bencher = next(e for e in engine.state.employees if e.name == "板凳")
    assert (battler.attack_count, bencher.attack_count) == (6, 6)
    _finish_round_without_monster_actions(engine)
    blocked = engine.execute_action("battle_end", {})
    assert blocked["success"] is True and blocked["completed"] is False
    paid = engine.execute_action("pay_employee_wage", {"name": "参战者", "decision": "pay"})
    assert paid["success"] is True
    finished = engine.execute_action("battle_end", {})
    assert finished["success"] is True, finished
    assert battler.attack_count == 7, f"参战者应+1攻次，实{battler.attack_count}"
    assert bencher.attack_count == 6, f"板凳不应成长，实{bencher.attack_count}"
    assert any("参战者" in g for g in finished["result"]["ally_growth"])
