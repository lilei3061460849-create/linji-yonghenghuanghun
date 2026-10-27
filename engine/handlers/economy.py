"""
员工经济系统处理器（Economy Handler）
负责员工派遣、解雇、债务付清与工资支付。

夺书不设专门行动：按「视野内目标」通用规则，想夺书的人直接开战即可。
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
from ..enums import GamePhase, CombatSubphase


def handle_deploy_employee(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """派遣[员工]出战：消耗玩家1出手"""
    employee_ref = params.get("employee_ref", "")
    emp = None
    if isinstance(employee_ref, str) and employee_ref.startswith("employee:"):
        try:
            emp = engine.state.employees[int(employee_ref.split(":", 1)[1])]
        except (ValueError, IndexError):
            emp = None
    if emp is None and params.get("name"):
        matches = [entity for entity in engine.state.employees
                   if entity.name == params["name"] and entity.is_alive]
        emp = matches[0] if len(matches) == 1 else None
    if emp is None or not emp.is_alive:
        return {"success": False, "error": "employee_ref不是存活员工"}
    name = emp.name
    if emp.is_debt_bound:
        return {"success": False, "error": f"{name}属于还债转化员工，已自动参战，无需派遣"}
    if emp.has_retreated:
        return {"success": False, "error": f"{name}本场已【撤退】，无法再次加入本场战斗"}
    if emp.is_deployed:
        return {"success": False, "error": f"{name}已在场，无需重复派遣"}
    if not engine.state.player:
        return {"success": False, "error": "没有玩家"}
    duel_error = engine._check_duel_turn_or_error(engine.state.player)
    if duel_error:
        return duel_error
    budget_error = engine._consume_action_or_error(engine.state.player)
    if budget_error:
        return budget_error
    emp.is_deployed = True
    emp.deployed_at_round = max(1, engine.state.current_round)
    engine._advance_duel_turn()
    return {
        "success": True, "action": "派遣员工",
        "result": {"employee": name, "deployed_at_round": emp.deployed_at_round},
        "note": "本次派遣已消耗玩家1出手",
        "state": engine.combat._get_combat_state(),
    }


def handle_dismiss_employee(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """解雇[员工]：直接移除，计入黑名单，不结算工资"""
    name = params.get("name", "")
    emp = next((e for e in engine.state.employees if e.name == name), None)
    if emp is None:
        return {"success": False, "error": f"找不到员工: {name}"}
    engine.state.employees.remove(emp)
    engine.state.pending_wage_decisions.pop(name, None)
    engine._blacklist_departure("解雇")
    return {
        "success": True, "action": "解雇员工",
        "result": {"employee": name, "blacklist_level": engine.state.blacklist_level,
                   "is_blacklisted": engine.state.is_blacklisted},
    }


def handle_repay_debt_employee(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """独立还债轨道：一次付清该员工负债后离队"""
    name = params.get("name", "")
    employee = next((e for e in engine.state.employees
                     if e.name == name and e.is_debt_bound), None)
    if employee is None:
        return {"success": False, "error": f"找不到还债员工: {name}"}
    debt = max(0, -employee.shards)
    if debt <= 0:
        return {"success": False, "error": f"{name}当前没有未清负债"}
    if engine.state.shards < debt:
        return {"success": False,
                "error": f"必须一次付清{debt}碎片，当前只有{engine.state.shards}"}
    engine.state.shards -= debt
    employee.shards = 0
    engine.state.employees.remove(employee)
    engine.state.pending_wage_decisions.pop(name, None)
    return {
        "success": True,
        "action": "付清还债员工负债",
        "result": {"employee": name, "paid": debt, "departed": True,
                   "blacklist_unchanged": engine.state.blacklist_level,
                   "shards": engine.state.shards},
    }


def handle_pay_employee_wage(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """支付或拒付员工工资决策"""
    name = params.get("name", "")
    decision = params.get("decision", "")
    wage = engine.state.pending_wage_decisions.get(name)
    if wage is None:
        return {"success": False, "error": f"{name}当前没有待决的工资结算（未部署/未存活/已决策过）"}
    if decision == "pay":
        if engine.state.shards < wage:
            return {"success": False, "error": f"碎片不足，需要{wage}，当前{engine.state.shards}，无法支付，请改为提交 refuse"}
        engine.state.shards -= wage
        engine.state.pending_wage_decisions[name] = None
        return {"success": True, "action": "支付工资",
                "result": {"employee": name, "wage_paid": wage, "shards": engine.state.shards}}
    elif decision == "refuse":
        engine.state.pending_wage_decisions[name] = None
        emp = next((e for e in engine.state.employees if e.name == name), None)
        if emp is not None:
            engine.state.employees.remove(emp)
        engine._blacklist_departure("拒付工资")
        return {"success": True, "action": "拒付工资",
                "result": {"employee": name, "wage_refused": wage, "departed": True,
                           "blacklist_level": engine.state.blacklist_level,
                           "is_blacklisted": engine.state.is_blacklisted}}
    else:
        return {"success": False, "error": "decision必须是 pay 或 refuse"}
