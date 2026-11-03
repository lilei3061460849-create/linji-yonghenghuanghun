"""同伴与雇佣行动：部署/遣散/发薪、雇佣道纹选择、替同伴还债。

2026-11-03 从 `engine/api.py` **原样搬出**（只搬位置，不改逻辑），作为
`GameEngine` 的 mixin 挂回；`@ACTION_REGISTRY.action(...)` 声明归属，
`_dispatch_action` 查表即命中。
"""
from __future__ import annotations



from ..action_registry import ACTION_REGISTRY
from ..models import DaoWen, DaoWenInstance
from ..yaoguai import BANNER_NAME
from .economy import (handle_deploy_employee, handle_dismiss_employee,
                      handle_pay_employee_wage, handle_repay_debt_employee)


class CompanionHandlersMixin:
    """宿主类：`GameEngine`。"""

    @ACTION_REGISTRY.action("deploy_employee", group="companions")
    def _action_deploy_employee(self, params: dict) -> dict:
        return handle_deploy_employee(self, params)

    @ACTION_REGISTRY.action("dismiss_employee", group="companions")
    def _action_dismiss_employee(self, params: dict) -> dict:
        return handle_dismiss_employee(self, params)

    @ACTION_REGISTRY.action("pay_employee_wage", group="companions")
    def _action_pay_employee_wage(self, params: dict) -> dict:
        return handle_pay_employee_wage(self, params)

    # ==================== 战斗行动 ====================

    # ---------- "发动道纹"的共用环节（use_daowen 与瞬发法术每一步共用） ----------

    @ACTION_REGISTRY.action("choose_hired_daowen", group="companions")
    def _action_choose_hired_daowen(self, params: dict) -> dict:
        """雇佣的diy后置步骤：从发现的3个转化道纹候选中选择1种，赋予该员工"""
        name = params.get("name", "")
        daowen_name = params.get("daowen", "")
        choices = self.state.pending_daowen_choices.get(name)
        if choices is None:
            return {"success": False, "error": f"{name}没有待选择的转化道纹（未雇佣该员工/已选择过）"}
        if daowen_name not in choices:
            return {"success": False, "error": f"{daowen_name}不在候选范围内: {choices}"}
        emp = next((e for e in self.state.employees if e.name == name), None)
        if emp is None:
            return {"success": False, "error": f"找不到员工: {name}"}
        emp.dao_wen[daowen_name] = DaoWenInstance(
            DaoWen(name=daowen_name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
        del self.state.pending_daowen_choices[name]
        return {"success": True, "action": "选择转化道纹",
                "result": {"employee": name, "chosen": daowen_name,
                           "employee_daowen": list(emp.dao_wen.keys())}}

    @ACTION_REGISTRY.action("repay_debt_employee", group="companions")
    def _action_repay_debt_employee(self, params: dict) -> dict:
        return handle_repay_debt_employee(self, params)
