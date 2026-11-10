"""[员工]出场并存活满 3 场战斗后转为[朋友]。

口径（用户裁定）：**出场** = 本场被派遣(is_deployed)参战；**存活** = [战终]时仍
存活。两条件同时满足才计 1 场，累计满 Entity.EMPLOYEE_PROMOTION_BATTLES(3)
即转为[朋友]：从 state.employees 移入 state.friends，entity_type 改为"朋友"。

不计入的情形：
- 待命未上场（is_deployed=False）—— 没出场
- 本场阵亡 —— 没存活（且已被战终清理移出 employees）
- 还债员工(is_debt_bound) —— 身份是债务约束而非雇佣关系，不晋升

运行方式：
    python -m pytest tests/test_employee_promotion.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity

from tests.setup_support import finish_initial_daowen


def _new_engine(db_suffix: str) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_promo_{db_suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.shards = 500          # 工资照付，避免欠薪离队干扰
    engine.state.energy = 3
    return engine


def _hire(engine, name: str):
    result = engine.execute_action("pre_battle_action", {
        "sub_action": "雇佣", "name": name, "blood_alloc": 5, "atk_bundles": 5})
    if result.get("success"):
        choices = engine.state.pending_daowen_choices[name]
        assert engine.execute_action("choose_hired_daowen", {"name": name, "daowen": choices[0]})["success"]
    return result


def _run_battle(engine, deploy_names=(), *, deploy: bool = True):
    """跑完一整场战斗（无怪物），返回最终 battle_end 结果。"""
    engine.state.energy = 0
    choices = {}
    relic = engine.state.relics[0].name
    # 2026-10-13：【三相残韵盘】删除，战始显式决策预填不再需要。
    engine.execute_action("battle_start", {"relic_choices": choices})
    engine.state.enemies.clear()
    # 2026-10-12 出怪下界 1→2：第1场可能带增援排队，只清 enemies 会让战终门禁卡死。
    engine.state.monster_reinforcements = []
    engine.execute_action("round_start", {})
    if deploy:
        for n in deploy_names:
            engine.execute_action("deploy_employee", {"name": n})
    # 走满 3 个回合，保证"出场"成立
    for _ in range(3):
        engine.execute_action("round_end", {})
        engine.execute_action("round_start", {})
    blocked = engine.execute_action("battle_end", {})
    while blocked.get("completed") is False:
        for name in list(blocked.get("pending_wage_decisions") or {}):
            engine.execute_action("pay_employee_wage", {"name": name, "decision": "pay"})
        blocked = engine.execute_action("battle_end", {})
    return blocked


def test_promotes_after_three_survived_battles():
    """正常路径：连续 3 场参战并存活 → 第 3 场战终转为[朋友]。"""
    engine = _new_engine("three")
    _hire(engine, "老张")
    emp = next(e for e in engine.state.employees if e.name == "老张")

    for i in range(2):
        _run_battle(engine, ["老张"])
        assert emp in engine.state.employees, f"第{i+1}场后仍应是[员工]"
        assert emp.survived_battles_as_employee == i + 1
        assert emp.entity_type == "员工"

    res = _run_battle(engine, ["老张"])
    assert emp not in engine.state.employees, "满3场后必须移出 employees"
    assert emp in engine.state.friends, "满3场后必须加入 friends"
    assert emp.entity_type == "朋友"
    assert emp.is_alive is True
    assert res["result"]["employee_promotions"] == [{"name": "老张", "battles": 3}]


def test_not_promoted_before_three():
    """边界：只存活 2 场不晋升。"""
    engine = _new_engine("two")
    _hire(engine, "小李")
    emp = next(e for e in engine.state.employees if e.name == "小李")
    for i in range(2):
        res = _run_battle(engine, ["小李"])
        assert emp in engine.state.employees
        assert res["result"]["employee_promotions"] == [], "未满3场不应晋升"
    assert emp.survived_battles_as_employee == 2


def test_idle_employee_never_promoted():
    """待命未上场不计场次：始终不晋升。"""
    engine = _new_engine("idle")
    _hire(engine, "待命王")
    emp = next(e for e in engine.state.employees if e.name == "待命王")
    for _ in range(4):
        _run_battle(engine, deploy_names=[], deploy=False)
    assert emp.survived_battles_as_employee == 0, "没出场不应累计"
    assert emp in engine.state.employees, "从未出场不应晋升"


def test_death_resets_progress_by_removal():
    """阵亡员工被移出名单，不再参与后续晋升。"""
    engine = _new_engine("dead")
    _hire(engine, "短命")
    emp = next(e for e in engine.state.employees if e.name == "短命")
    _run_battle(engine, ["短命"])
    assert emp.survived_battles_as_employee == 1
    # 直接击杀后再跑一场，战终清理应将其移出
    emp.current_hp = 0
    emp.is_alive = False
    _run_battle(engine, ["短命"])
    assert emp not in engine.state.employees, "阵亡员工应被战终清理移出"


def test_promotion_threshold_is_single_source():
    """阈值唯一事实源：Entity.EMPLOYEE_PROMOTION_BATTLES == 3。"""
    assert Entity.EMPLOYEE_PROMOTION_BATTLES == 3
