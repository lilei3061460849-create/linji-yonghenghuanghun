"""
员工与背叛经济系统处理器（Economy & Rebellion Handler）
负责员工派遣、解雇、债务付清、工资支付与背叛处置。
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


def handle_declare_betrayal(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """[朋友]／[员工]声明背叛（2026-10-24 用户令：自选项，不是特殊事件）。

    没有攻击总值门槛，不自动触发：谁要夺书，谁自己声明。
    """
    if engine.state.rebellion_in_progress:
        return {"success": False, "error": "镇压战斗尚未结算，先调用 resolve_rebellion_battle"}
    if engine.state.rebellion_active:
        return {"success": False, "error": "已有待处理的背叛，先选镇压／让利／谈判"}
    if engine.state.enemies:
        return {"success": False,
                "error": "场上仍有敌方角色：背叛只能在战斗之外声明（[战终]后或局外）"}
    raw = params.get("names")
    if raw is None:
        raw = [params["name"]] if params.get("name") else []
    if isinstance(raw, str):
        raw = [raw]
    names = [str(x).strip() for x in raw if str(x).strip()]
    if not names:
        return {"success": False, "error": "必须指名背叛者（names）"}
    pool: Dict[str, str] = {}
    for e in engine.state.employees:
        if e.is_alive:
            pool[e.name] = "员工"
    for f in engine.state.friends:
        if f.is_alive:
            pool[f.name] = "朋友"
    unknown = [n for n in names if n not in pool]
    if unknown:
        return {"success": False, "error": f"不是存活的[朋友]／[员工]：{unknown}"}
    engine.state.rebellion_rebels = names
    engine.state.rebellion_active = True
    return {
        "success": True, "action": "背叛",
        "result": {"rebels": names, "kinds": {n: pool[n] for n in names}},
        "instruction": "背叛者已摊牌；轮回者须从镇压／让利／谈判中选一项应对"
                       "（suppress_rebellion / appease_rebellion / negotiate_rebellion）",
    }


def handle_suppress_rebellion(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """镇压背叛：把声明背叛的[朋友]／[员工]搬进state.enemies"""
    force = params.get("force", False)
    err = engine._pending_rebellion_error(force)
    if err:
        return err
    declared = list(getattr(engine.state, "rebellion_rebels", []) or [])
    if declared:
        nameset = set(declared)
        rebels = [e for e in engine.state.employees + engine.state.friends
                  if e.name in nameset and e.is_alive]
    else:
        rebels = [e for e in engine.state.employees if e.is_alive]
    if not rebels:
        return {"success": False, "error": "没有可镇压的背叛者"}
    for e in rebels:
        e.is_deployed = True
        e.has_retreated = False
    rebel_ids = {id(e) for e in rebels}
    engine.state.employees = [e for e in engine.state.employees if id(e) not in rebel_ids]
    engine.state.friends = [f for f in engine.state.friends if id(f) not in rebel_ids]
    engine.state.rebellion_rebels = []
    engine.state.enemies = rebels
    engine.state.current_round = 0
    engine.combat.reset_monster_activation()
    engine.state.rebellion_in_progress = True
    engine.state.rebellion_active = False
    engine.state.phase = GamePhase.IN_COMBAT.value
    engine.state.combat_subphase = CombatSubphase.AWAIT_ROUND_START.value
    return {
        "success": True, "action": "镇压背叛",
        "result": {
            "rebels": [e.name for e in rebels],
            "panels": [{"name": e.name, "attack_count": e.attack_count, "attack_power": e.attack_power,
                        "blood_limit": e.blood_limit, "current_hp": e.current_hp,
                        "dao_wen": {k: v.x_value for k, v in e.dao_wen.items()}} for e in rebels],
        },
        "instruction": "背叛员工已作为本场敌方(state.enemies)，按普通战斗流程推进；"
                       "战斗分出胜负后调用 resolve_rebellion_battle(outcome=victory/defeat) 结算",
    }


def handle_resolve_rebellion_battle(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """镇压战斗结算"""
    if not engine.state.rebellion_in_progress:
        return {"success": False, "error": "当前没有进行中的背叛·镇压战斗"}
    outcome = params.get("outcome", "")
    if outcome not in ("victory", "defeat"):
        return {"success": False, "error": "outcome必须是 victory 或 defeat（战斗失败与主动撤退统一按defeat结算）"}
    escaped = [e.name for e in engine.state.enemies if e.is_alive]
    if outcome == "defeat":
        engine.state.shards = 0
    engine.state.enemies = []
    engine.state.rebellion_in_progress = False
    engine.state.rebellion_rebels = []
    engine.state.phase = "pre_battle"
    return {
        "success": True, "action": "镇压结算",
        "result": {"outcome": outcome, "shards": engine.state.shards,
                   "escaped_with_loot": escaped if outcome == "defeat" else []},
    }


def handle_appease_rebellion(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """让利：平息叛乱（只对[员工]有效——[朋友]不吃工资）"""
    force = params.get("force", False)
    err = engine._pending_rebellion_error(force)
    if err:
        return err
    declared = list(getattr(engine.state, "rebellion_rebels", []) or [])
    if declared:
        employee_names = {e.name for e in engine.state.employees}
        if not any(n in employee_names for n in declared):
            return {"success": False,
                    "error": "背叛者里没有[员工]：让利是加工资，对[朋友]无效——只能镇压或谈判"}
    engine.state.wage_bonus += 5
    engine.state.rebellion_active = False
    engine.state.rebellion_rebels = []
    return {"success": True, "action": "让利", "result": {"wage_bonus": engine.state.wage_bonus}}


def handle_negotiate_rebellion(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """谈判"""
    force = params.get("force", False)
    err = engine._pending_rebellion_error(force)
    if err:
        return err
    proposal = params.get("proposal", "")
    if not proposal:
        return {"success": False, "error": "必须给出谈判方案(proposal)，禁止空谈判"}
    interrupt = engine.combat.initiate_negotiation(proposal)
    engine._pending_interrupts.append(interrupt)
    return {
        "success": True, "action": "背叛·谈判",
        "interrupt": interrupt.to_dict(),
        "instruction": "需要DM裁定谈判方案是否合理；裁定后请调用 appease_rebellion(force=True) 平息叛乱"
                       "或改用 suppress_rebellion(force=True) 镇压",
    }
