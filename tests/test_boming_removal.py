"""旧【搏命】（扭曲都市独占）的删除护栏（2026-10-14 用户令）。

删除的是**扭曲都市那条**【搏命】：它留下的待补位由从杀伐闭环迁入的【增殖】承接，
旧效果不得复活。2026-11-09 用户把【透支】衍生的「疾贷」改名为【搏命】，与它同名
但完全不同——新【搏命】是支付速度X换X法力的衍生道纹，不在扭曲都市独占表里，
也没有任何 calculate_boming 运行态标识。本文件锁的仍是「旧效果不复活」。
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
    # 2026-11-09：同名新条目只能是【透支】衍生（速度X→X法力），且不得带旧效果实现。
    from engine import derivatives as derivative_rules
    if "搏命" in DaoWenEngine._registry:
        spec = derivative_rules.spec_of("搏命")
        assert (spec.parent, spec.cost_kind, spec.rate, spec.effect_kind) == \
            ("透支", "速度", 1, "mana_gain")
        assert not hasattr(DaoWenEngine, "calculate_boming")
    from engine.gamedata import REGION_EXCLUSIVE_DAOWEN

    assert "搏命" not in REGION_EXCLUSIVE_DAOWEN["扭曲都市"]
    # 2026-11-02：扭曲都市四面体补齐【饥饿】【扭曲】两个空位。
    assert REGION_EXCLUSIVE_DAOWEN["扭曲都市"] == {
        "变形", "定型", "超频", "增殖", "坏死", "萎缩", "饥饿", "扭曲"}


def test_twisted_tetrahedra_keep_zengzhi_in_the_second_one():
    edges = ResonanceEngine.edges_of_region("扭曲都市")
    assert ("增殖", "反转", "饥饿") in edges
    assert ("增殖", "转换", "扭曲") in edges
    assert ("增殖", "曲解", "坏死") in edges
    # 每个四面体 4 节点 6 边：任两节点恰好一条边、三种残韵各两条。
    for name, tetra in ResonanceEngine.tetrahedra_of_region("扭曲都市").items():
        nodes = {n for src, _r, dst in tetra for n in (src, dst)}
        assert len(nodes) == 4, f"{name} 不是四面体"
        assert len(tetra) == 6, f"{name} 边数不是6"
        # 排序按 Python 字符串序（三/转/曲），只锁"每种恰好两条"。
        counts = {r: [x for _s, x, _d in tetra].count(r) for r in ("反转", "转换", "曲解")}
        assert counts == {"反转": 2, "转换": 2, "曲解": 2}
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
    # 同名新【搏命】是透支衍生，不是旧【搏命】的复活。
    assert not hasattr(DaoWenEngine, "calculate_boming")
    if "搏命" in DaoWenEngine.list_all():
        from engine import derivatives as derivative_rules
        assert derivative_rules.spec_of("搏命").parent == "透支"
