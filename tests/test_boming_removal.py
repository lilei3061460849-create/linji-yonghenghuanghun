"""【搏命】删除护栏（2026-10-14 用户令）。

【搏命】仍不得恢复。它删除时在扭曲闭环留下的待补位，现由从杀伐闭环迁入的
【增殖】承接；这不改变搏命删除本身，也不恢复旧效果。
"""
from __future__ import annotations

import pathlib

from engine.daowen import DaoWenEngine, ResonanceEngine

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 生产代码与 sim 工具都不许再出现【搏命】的运行态标识。
# 允许出现在「已删除」说明性注释里，故按标识符而非中文名校验。
FORBIDDEN = ("calculate_boming",)
SCAN = [
    "engine/daowen.py", "engine/combat.py", "engine/combat_hooks.py", "engine/api.py",
    "engine/models.py", "engine/battle_report.py", "engine/gamedata.py",
    "engine/combat_parts/daowen_effect.py", "engine/combat_parts/damage_death.py",
    "sim/balance_sim.py", "sim/audit_monsters.py", "sim/monster_targets.py",
]


def test_boming_is_not_registered():
    DaoWenEngine.register_all()
    assert "搏命" not in DaoWenEngine._registry
    from engine.gamedata import REGION_EXCLUSIVE_DAOWEN

    assert "搏命" not in REGION_EXCLUSIVE_DAOWEN["扭曲都市"]
    assert REGION_EXCLUSIVE_DAOWEN["扭曲都市"] == {
        "变形", "定型", "超频", "增殖", "坏死", "萎缩"}


def test_twisted_loop_keeps_seven_edges_with_zengzhi_in_former_slot():
    loop = ResonanceEngine.CLOSED_LOOPS["扭曲都市闭环"]
    assert ("超频", "反转", "增殖") in loop
    assert ("增殖", "转换", "坏死") in loop
    assert len(loop) == 7
    outs = {a for a, _, _ in loop}
    ins = {c for _, _, c in loop}
    assert outs == ins, "闭环节点入出必须一一对应"
    assert len(outs) == len(loop), "每个节点只能出现一次，否则不是单环"
    DaoWenEngine.register_all()
    assert "增殖" in DaoWenEngine._registry


def test_no_runtime_trace_left_in_production_or_sim():
    """静态：生产代码与 sim 工具不再含搏命的运行态标识（注释里的沿革说明不算）。"""
    for rel in SCAN:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for token in FORBIDDEN:
            assert token not in text, f"{rel} 仍含 {token}"


def test_jianghua_is_still_gone():
    """【僵化】此前已被【搏命】取代，删除搏命不得把它复活。"""
    DaoWenEngine.register_all()
    assert "僵化" not in DaoWenEngine.list_all()
    assert "搏命" not in DaoWenEngine.list_all()
