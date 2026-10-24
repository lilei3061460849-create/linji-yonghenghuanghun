"""【洞察】迁移验证：round_start 内嵌块 → ROUND_START 相位 Mechanism（priority 30）。

验证点：
  - mana 动词（统一法力入口）逐字覆盖旧 洞察 语义（+= pending + 不朽之躯钳制）；
  - 条件：pending>0 且 轮回者 且 存活（与旧块短路顺序同义——不满足则不触发也不清零）；
  - 顺序：洞察(20) → 畸变·标记(60) → 勾魂（尚未迁移，硬编码块在其后）；
    自愈(10) 与 衰败 已随道纹删除（2026-10-11 用户令）。
"""
from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.mechanisms import MECHANISMS, Phase
from engine.models import Entity, GameState, Relic
from engine.validator import check_migrated_mechanism_guards

ROOT = Path(__file__).resolve().parents[1]
COMBAT_SOURCE = (ROOT / "engine" / "combat.py").read_text(encoding="utf-8")


def _arena(pending=0, entity_type="轮回者", mana=20, mana_limit=50,
           immortal=False, alive=True):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=0, speed_limit=10, current_speed=5)
    ent = Entity("E", entity_type, blood_limit=50, current_hp=50,
                 mana_limit=mana_limit, current_mana=mana)
    if pending:
        ent._dongcha_pending = pending
    ent.is_alive = alive
    state.player = player
    state.enemies = [ent]
    if immortal:
        state.relics = [Relic("不朽之躯", "")]
    return state, CombatEngine(state, DiceEngine()), player, ent


def _old_dongcha(combat, entity):
    """round_start 洞察旧块的逐行复刻（仅测试对照用，非生产代码）。"""
    pending = getattr(entity, "_dongcha_pending", 0)
    if pending and entity.entity_type == "轮回者" and entity.is_alive:
        entity.current_mana += pending
        combat.clamp_immortal_body(entity)
        entry = {"type": "dongcha_mana", "entity": entity.name, "gained": pending}
        entity._dongcha_pending = 0
        return entry
    return None


def _run_new(combat, entity):
    results = combat._dispatch_phase(Phase.ROUND_START, target=entity)
    return next((r for r in results if isinstance(r, dict)
                 and r.get("type") == "dongcha_mana"), None)


# ==================== 1. 注册 / 旧实现删除 ====================

def test_dongcha_is_registered():
    mech = MECHANISMS.get("洞察·结算")
    assert mech is not None and mech.when.matches_phase(Phase.ROUND_START)
    assert mech.priority == 30
    from engine.mechanisms.registry import MECHANISMS as REG
    assert [m.name for m in REG.phase_mechanisms(Phase.ROUND_START)] == \
        ["洞察·结算", "畸变·标记"]
        # 2026-10-11：【衰败】随道纹删除
        # 2026-10-03：【狂暴·标记】随道纹删除；2026-10-11：【自愈】随道纹删除
        # 2026-10-23：烬翼当日改为消耗品，回始机制已撤，不再占位


def test_old_dongcha_block_removed():
    assert '"type": "dongcha_mana"' not in COMBAT_SOURCE, "核心管线不得残留旧洞察条目代码"
    assert check_migrated_mechanism_guards() == []


# ==================== 2. 触发语义 ====================

def test_dongcha_normal():
    state, combat, player, ent = _arena(pending=15, mana=20)
    # 完整 round_start 会先给轮回者补法限法力（+50 既有规则），再结算洞察
    res = combat.round_start()
    entries = [e for e in res["effects"] if e.get("type") == "dongcha_mana"]
    assert entries == [{"type": "dongcha_mana", "entity": "E", "gained": 15}]
    assert ent.current_mana == 35, "一池制：20 + 15(洞察)，回始不再补法限50"
    assert getattr(ent, "_dongcha_pending", None) is None or ent._dongcha_pending == 0


def test_dongcha_immortal_clamp():
    """不朽之躯钳制：持有者（玩家自身）获得后被封顶到法限。"""
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=40, speed_limit=10, current_speed=5)
    player._dongcha_pending = 15
    state.player = player
    state.enemies = [Entity("M", "怪物", blood_limit=50, current_hp=50)]
    state.relics = [Relic("不朽之躯", "")]
    combat = CombatEngine(state, DiceEngine())
    results = combat._dispatch_phase(Phase.ROUND_START, target=player)
    assert any(isinstance(r, dict) and r.get("type") == "dongcha_mana"
               for r in results)
    assert player.current_mana == 50, "不朽之躯钳制到法限"


def test_dongcha_skips_without_pending():
    state, combat, player, ent = _arena(pending=0, mana=20)
    results = combat._dispatch_phase(Phase.ROUND_START, target=ent)
    assert not any(isinstance(r, dict) and r.get("type") == "dongcha_mana"
                   for r in results)
    assert ent.current_mana == 20


def test_dongcha_skips_non_reincarnator_and_pending_survives():
    """非轮回者有 pending：与旧块一致——不触发、不清零。"""
    state, combat, player, ent = _arena(pending=10, entity_type="怪物", mana=20)
    res = combat.round_start()
    assert not any(e.get("type") == "dongcha_mana" for e in res["effects"])
    assert ent._dongcha_pending == 10, "旧语义：不满足条件则 pending 保留"


def test_dongcha_skips_dead():
    state, combat, player, ent = _arena(pending=10, mana=20, alive=False)
    assert _run_new(combat, ent) is None
    assert ent.current_mana == 20 and ent._dongcha_pending == 10


# ==================== 3. 顺序 ====================



def test_dongcha_executes_exactly_once():
    state, combat, player, ent = _arena(pending=10, mana=20)
    results = combat._dispatch_phase(Phase.ROUND_START, target=ent)
    assert sum(1 for r in results
               if isinstance(r, dict) and r.get("type") == "dongcha_mana") == 1
    assert ent.current_mana == 30, "若双触发会得到 40（30+10）"


# ==================== 4. 参考实现 sweep ====================

def test_dongcha_reference_sweep_zero_mismatch():
    """旧洞察块 vs 新机制：pending×实体类型×存活×不朽×当前法力 全场景逐结果一致。"""
    mismatches = []
    total = 0
    for pending, etype, alive, immortal, mana in itertools.product(
            [0, 5, 15], ["轮回者", "怪物"], [False, True],
            [False, True], [20, 70]):
        total += 1
        state_a, combat_a, _, ent_a = _arena(pending=pending, entity_type=etype,
                                             mana=mana, immortal=immortal, alive=alive)
        state_b, combat_b, _, ent_b = _arena(pending=pending, entity_type=etype,
                                             mana=mana, immortal=immortal, alive=alive)
        entry_a = _old_dongcha(combat_a, ent_a)
        entry_b = _run_new(combat_b, ent_b)
        if entry_a != entry_b:
            mismatches.append(("entry", pending, etype, alive, immortal, mana,
                               entry_a, entry_b))
        if ent_a.current_mana != ent_b.current_mana:
            mismatches.append(("mana", pending, etype, alive, immortal, mana,
                               ent_a.current_mana, ent_b.current_mana))
        pa = getattr(ent_a, "_dongcha_pending", None)
        pb = getattr(ent_b, "_dongcha_pending", None)
        if pa != pb:
            mismatches.append(("pending_after", pending, etype, alive, immortal, mana, pa, pb))

    assert not mismatches, f"{total} 组场景出现 {len(mismatches)} 组差异: {mismatches[:3]}"


def test_dongcha_registry_unique_and_no_special_api():
    from engine.mechanisms.registry import MECHANISMS as REG
    assert len([m for m in REG.all() if m.name == "洞察·结算"]) == 1
    from engine.mechanisms import verb_names
    assert not any("dongcha" in v for v in verb_names())
