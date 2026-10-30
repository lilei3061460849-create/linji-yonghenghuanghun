"""【嫁祸】删除护栏（2026-10-11 用户令）。

删除理由：**效果与遗物【贪婪契约】重复**（`物品索引.md#贪婪契约`：「[战始]选择一名
[朋友]或[员工]，本场支付的【代价】改由其承担」）。用户裁定直接删掉，不重做。

沿用【活血】删除的先例（`tests/test_huoli_removal.py`）：删干净 + 静态护栏，
防止旧口径被下一次改动复活。

闭环补位沿用既有先例（删【活血】→逆鳞直连裂变、删【尸爆】→分裂直连缄默）：
**裂变 —（转换）→ 背负**，龙心谷闭环仍合拢，边数 7 → 6。
后续加害、龙鳞、裂变、背负等节点已按裁定转换或删除；当前仅以内部「？？」
占位保留这条六边结构，不把旧名字重新列为可获得的副本专属道纹。
"""
from __future__ import annotations

import pathlib

from engine.daowen import DaoWenEngine, ResonanceEngine, RELICIZED_DAOWEN_SLOTS, is_reserved_slot

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 生产代码与 sim 工具都不许再出现【嫁祸】的运行态标识。
# 允许出现在「已删除」说明性注释里，故按标识符而非中文名校验。
FORBIDDEN = ("_jiahuo_left", "_jiahuo_target", "JIAHUO_POLICY", "calculate_jiahuo")
SCAN = [
    "engine/daowen.py", "engine/combat.py", "engine/combat_hooks.py", "engine/api.py",
    "engine/models.py", "engine/battle_report.py", "engine/gamedata.py",
    "engine/combat_parts/daowen_effect.py", "engine/combat_parts/damage_death.py",
    "sim/balance_sim.py", "sim/audit_monsters.py", "sim/monster_targets.py",
]


def test_jiahuo_is_not_registered():
    """正常路径：注册表里没有【嫁祸】，且龙心谷专属名单同步收窄。"""
    DaoWenEngine.register_all()
    assert "嫁祸" not in DaoWenEngine._registry
    from engine.gamedata import REGION_EXCLUSIVE_DAOWEN

    assert "嫁祸" not in REGION_EXCLUSIVE_DAOWEN["龙心谷"]
    assert REGION_EXCLUSIVE_DAOWEN["龙心谷"] == set()


def test_longji_loop_still_closes_with_six_edges():
    """旧道纹退出后，六个独立占位节点仍保持单环，不授予旧道纹。"""
    loop = ResonanceEngine.CLOSED_LOOPS["龙心谷闭环"]
    assert (RELICIZED_DAOWEN_SLOTS["裂变"], "转换",
            RELICIZED_DAOWEN_SLOTS["背负"]) in loop
    assert len(loop) == 6
    outs = {a for a, _, _ in loop}
    ins = {c for _, _, c in loop}
    assert outs == ins, "闭环节点入出必须一一对应"
    assert len(outs) == len(loop), "每个节点只能出现一次，否则不是单环"
    assert all(is_reserved_slot(name) for name in outs)


def test_no_runtime_trace_left_in_production_or_sim():
    """静态：生产代码与 sim 工具不再含嫁祸的运行态标识（注释里的沿革说明不算）。"""
    for rel in SCAN:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for token in FORBIDDEN:
            assert token not in text, f"{rel} 仍含 {token}"


def test_dedicated_jiahuo_sim_scripts_removed():
    """静态：两个嫁祸专属推演脚本已随道纹删除。"""
    assert not (ROOT / "sim" / "jiahuo_full_run.py").exists()
    assert not (ROOT / "sim" / "jiahuo_monster_full_run.py").exists()
