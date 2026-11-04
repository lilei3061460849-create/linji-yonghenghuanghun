"""共享员工工资结算助手。

轮回战斗与死斗回放共用同一条正式工资结算路径。此模块仅负责处理
engine 已经提交的 pending_wage_decisions，不承载任何一次性完整跑局策略。
"""
from __future__ import annotations

# --- 2026-11-04：脚本放进子目录后，不再靠"往上数几层"猜仓库根 ---
# 向上找到同时含 engine/ 与 sim/ 的目录就是仓库根；再附带 sim/ 让老式裸模块名
# import（如 `import balance_sim`）继续可用。搬动本文件不需要再改这几行。
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.abspath(__file__))
while _ROOT != _os.path.dirname(_ROOT) and not _os.path.isdir(_os.path.join(_ROOT, "engine")):
    _ROOT = _os.path.dirname(_ROOT)
for _p in (_ROOT, _os.path.join(_ROOT, "sim"), _os.path.join(_ROOT, "tests")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)



def settle_wages(engine, logs: list[str] | None = None) -> None:
    """逐笔结算待决员工工资：碎片足够则支付，不足则拒付。"""
    if logs is None:
        logs = []
    while True:
        pending = {
            name: wage
            for name, wage in engine.state.pending_wage_decisions.items()
            if wage is not None
        }
        if not pending:
            break
        name = next(iter(pending))
        wage = pending[name]
        if engine.state.shards >= wage:
            result = engine.execute_action(
                "pay_employee_wage", {"name": name, "decision": "pay"}
            )
            logs.append(f"支付{name}工资{wage}")
        else:
            result = engine.execute_action(
                "pay_employee_wage", {"name": name, "decision": "refuse"}
            )
            logs.append(f"拒付{name}工资{wage}（碎片不足，{name}离队）")
        if not result.get("success"):
            logs.append(f"工资结算异常：{result.get('error')}")
            break