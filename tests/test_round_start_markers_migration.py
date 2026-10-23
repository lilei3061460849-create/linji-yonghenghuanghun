"""【畸变·标记】迁移验证：round_start 纯报告块 → ROUND_START 相位机制。

2026-10-03：【狂暴】道纹删除，【狂暴·标记】机制一并移除（用户令）。
本文件保留【畸变·标记】的全部验证；原本针对狂暴标记的用例已删除，不再铺垫。

验证点：
  - 条目形状逐字一致（deform_pending）；
  - 畸变标记的 blood_loss 是原始乘积（与结算块的 max(0,...) 不同，旧块原文如此）；
  - 与【畸变·结算】（ROUND_END）互不干扰。
"""
from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.mechanisms import MECHANISMS, Phase
from engine.models import Entity, GameState, StatusEffect
from engine.validator import check_migrated_mechanism_guards

ROOT = Path(__file__).resolve().parents[1]
COMBAT_SOURCE = (ROOT / "engine" / "combat.py").read_text(encoding="utf-8")


def _arena(ac=0, ap=0, statuses=()):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=0, speed_limit=10, current_speed=5)
    ent = Entity("E", "怪物", blood_limit=50, current_hp=50,
                 attack_count=ac, attack_power=ap)
    for name, value in statuses:
        ent.add_status(StatusEffect(name=name, remaining_rounds=-1,
                                    value=value, source="x"))
    state.player = player
    state.enemies = [ent]
    return state, CombatEngine(state, DiceEngine()), player, ent


def _phase_results(combat, entity):
    return combat._dispatch_phase(Phase.ROUND_START, target=entity)


def _entry(results, entry_type):
    return next((r for r in results if isinstance(r, dict)
                 and r.get("type") == entry_type), None)


# ==================== 1. 注册 / 旧实现删除 ====================

def test_marker_registered_and_ordered():
    mech_j = MECHANISMS.get("畸变·标记")
    assert mech_j is not None and mech_j.when.matches_phase(Phase.ROUND_START)
    assert mech_j.priority == 60
    assert MECHANISMS.get("狂暴·标记") is None, "【狂暴】删除后标记机制不应再注册"
    from engine.mechanisms.registry import MECHANISMS as REG
    assert [m.name for m in REG.phase_mechanisms(Phase.ROUND_START)] == \
        ["烬翼·回始飞行", "洞察·结算", "畸变·标记"]   # 2026-10-11：【自愈】【衰败】随道纹删除；2026-10-23 新增烬翼(25)


def test_old_marker_blocks_removed():
    assert '"type": "extra_attack_ready"' not in COMBAT_SOURCE
    assert '"type": "deform_pending"' not in COMBAT_SOURCE
    assert check_migrated_mechanism_guards() == []


# ==================== 2. 畸变·标记 ====================

def test_jibian_marker_shapes_and_raw_product():
    """blood_loss 是原始乘积（非结算块的 max(0,...)），逐字复刻旧块。"""
    state, combat, player, ent = _arena(ac=2, ap=3, statuses=(("畸变", 1),))
    results = _phase_results(combat, ent)
    entry = _entry(results, "deform_pending")
    assert entry == {"type": "deform_pending", "entity": "E",
                     "blood_loss": 6, "note": "回终结算"}

    # 面板 0：blood_loss=0（原始乘积，无封底）
    state, combat, player, ent = _arena(ac=0, ap=5, statuses=(("畸变", 2),))
    assert _entry(_phase_results(combat, ent), "deform_pending")["blood_loss"] == 0

    # 无状态 → 无条目
    state, combat, player, ent = _arena(ac=2, ap=3)
    assert _entry(_phase_results(combat, ent), "deform_pending") is None


def test_jibian_marker_does_not_change_blood_limit():
    """标记只是预告；真实扣血限发生在 ROUND_END 的【畸变·结算】。"""
    state, combat, player, ent = _arena(ac=2, ap=3, statuses=(("畸变", 1),))
    results = _phase_results(combat, ent)
    assert _entry(results, "deform_pending") is not None
    assert ent.blood_limit == 50, "标记块不得扣血限"

    # 回终结算仍照常工作（两机制互不干扰）
    res = combat.round_end()
    assert any(e.get("type") == "deform_blood_limit_loss" for e in res["effects"])
    assert ent.blood_limit == 44


# ==================== 3. 只触发一次 / 参考实现 sweep ====================

def test_marker_executes_exactly_once():
    state, combat, player, ent = _arena(ac=2, ap=3, statuses=(("畸变", 1),))
    results = _phase_results(combat, ent)
    assert sum(1 for r in results
               if isinstance(r, dict) and r.get("type") == "deform_pending") == 1


def test_marker_reference_sweep_zero_mismatch():
    """旧标记块 vs 新机制：状态有无×攻击面板 全场景逐结果一致。"""
    def old_marker(entity):
        entries = []
        if entity.has_status("畸变"):
            blood_loss = entity.attack_count * entity.attack_power
            entries.append({"type": "deform_pending", "entity": entity.name,
                            "blood_loss": blood_loss, "note": "回终结算"})
        return entries

    mismatches = []
    total = 0
    for jibian, (ac, ap) in itertools.product([False, True], [(0, 0), (2, 3), (5, 1)]):
        total += 1
        statuses = [("畸变", 3)] if jibian else []
        _, combat_a, _, ent_a = _arena(ac=ac, ap=ap, statuses=statuses)
        _, combat_b, _, ent_b = _arena(ac=ac, ap=ap, statuses=statuses)

        old = old_marker(ent_a)
        new = [r for r in _phase_results(combat_b, ent_b) if isinstance(r, dict)]
        if old != new:
            mismatches.append((jibian, ac, ap, old, new))
        if (ent_a.blood_limit, ent_a.current_hp) != (ent_b.blood_limit, ent_b.current_hp):
            mismatches.append(("state", jibian, ac, ap))

    assert not mismatches, f"{total} 组场景出现 {len(mismatches)} 组差异: {mismatches[:3]}"


def test_marker_registry_unique():
    from engine.mechanisms.registry import MECHANISMS as REG
    assert len([m for m in REG.all() if m.name == "畸变·标记"]) == 1
