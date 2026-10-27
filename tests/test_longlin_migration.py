"""龙鳞转遗物后：固定减伤、持有判定、单路径执行与事件链验证。"""
from __future__ import annotations

import itertools
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import combat_hooks
from engine.combat import CombatEngine
from engine.combat_events import CombatEventType
from engine.combat_hooks import CombatHookManager
from engine.dice import DiceEngine
from engine.mechanisms import MECHANISMS, MechanismHookAdapter, Phase
from engine.models import Entity, GameState, StatusEffect, Relic
from engine.validator import check_migrated_mechanism_guards


def _arena(enemy_hp: int = 100, enemy_bl: int | None = None):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=50, speed_limit=10, current_speed=10)
    enemy = Entity("M", "怪物", blood_limit=enemy_bl if enemy_bl is not None else enemy_hp,
                   current_hp=enemy_hp)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def _events(combat, event_type):
    return [e for e in combat.event_stream if e.event_type == event_type]


def _target(*statuses):
    t = Entity("T", "怪物", blood_limit=50, current_hp=50)
    for name, value in statuses:
        t.add_status(StatusEffect(name=name, remaining_rounds=-1, value=value, source="x"))
    if any(name == "龙鳞" and value > 0 for name, value in statuses):
        t.relics.append(Relic("叠鳞甲", ""))
    return t


class _State:
    def _relic_active(self, entity, name):
        return any(r.name == name and r.enabled for r in entity.relics)

    def side_has(self, entity, name):
        return False


class _OldLonglinReference:
    """固定遗物语义的测试内参考实现（仅用于等价对照，不是生产代码）。"""
    priority = 30

    def on_incoming_adjust(self, target, amount, damage_type, source, state):
        if amount > 0 and damage_type != "代价" and state._relic_active(target, "叠鳞甲"):
            return max(0, amount - 1)
        return amount


def _longlin_adapter(manager):
    return next(h for h in manager.hooks()
                if isinstance(h, MechanismHookAdapter) and h.mechanism.name == "叠鳞甲")


# ==================== 1. 声明层形态 ====================

def test_longlin_is_registered_mechanism():
    mech = MECHANISMS.get("叠鳞甲")
    assert mech is not None
    assert mech.when.matches_phase(Phase.INCOMING_ADJUST)
    assert mech.priority == 30, "priority 必须保持原值 30（顺序即规则）"


def test_old_longlin_hook_fully_removed():
    """旧 LonglinHook 类已删除：全模块不再存在，杜绝第二条分发路径。"""
    assert not hasattr(combat_hooks, "LonglinHook")
    assert "LonglinHook" not in [type(h).__name__ for h in CombatHookManager().hooks()]


# ==================== 2. 同相位多机制 + priority ====================

def test_same_phase_hosts_both_mechanisms_in_priority_order():
    from engine.mechanisms.registry import MECHANISMS as REG
    phase_mechs = REG.phase_mechanisms(Phase.INCOMING_ADJUST)
    assert [m.name for m in phase_mechs] == ["加害", "叠鳞甲", "固执"]
    assert [m.priority for m in phase_mechs] == [20, 30, 40]

    manager = CombatHookManager()
    adapters = [h for h in manager.hooks() if isinstance(h, MechanismHookAdapter)
                and h.mechanism.when.matches_phase(Phase.INCOMING_ADJUST)]
    assert [a.mechanism.name for a in adapters] == ["加害", "叠鳞甲", "固执"]
    assert [a.priority for a in adapters] == [20, 30, 40]


# ==================== 3. 数值语义等价 ====================

def test_longlin_numeric_equivalence():
    manager = CombatHookManager()
    state = _State()

    big = _target(("龙鳞", 15))
    assert manager.apply_incoming_adjust(big, 10, "普通", None, state) == 9, "固定减1，不读取旧状态15"

    mid = _target(("龙鳞", 8))
    assert manager.apply_incoming_adjust(mid, 10, "普通", None, state) == 9, "固定减1，不读取旧状态8"

    assert manager.apply_incoming_adjust(mid, 8, "代价", None, state) == 8, "代价不受减免"
    assert manager.apply_incoming_adjust(mid, 0, "普通", None, state) == 0, "amount<=0 不改"
    assert manager.apply_incoming_adjust(mid, -5, "普通", None, state) == -5

    no_status = _target()
    assert manager.apply_incoming_adjust(no_status, 10, "普通", None, state) == 10

    zero_value = _target(("龙鳞", 0))
    assert manager.apply_incoming_adjust(zero_value, 10, "普通", None, state) == 10, "value缺省按0"

    # 幂等：同一份管理器重复调用不叠加（机制无隐藏全局状态）
    assert manager.apply_incoming_adjust(mid, 10, "普通", None, state) == 9
    assert manager.apply_incoming_adjust(mid, 10, "普通", None, state) == 9


def test_longlin_reference_sweep_zero_mismatch():
    """行为等价：固定遗物减伤参考实现 vs 新 Mechanism 适配器，全场景对照 0 差异。"""
    manager = CombatHookManager()
    adapter = _longlin_adapter(manager)
    old = _OldLonglinReference()
    state = _State()

    status_sets = [[], [("龙鳞", 2)], [("龙鳞", 15)], [("龙鳞", 0)],
                   [("加害", 2), ("龙鳞", 8)], [("龙鳞", 5), ("加害", 5)]]
    mismatches = 0
    for statuses, amount, dtype in itertools.product(
            status_sets, [-5, 0, 1, 8, 10, 100], ["普通", "代价", "无视格挡"]):
        t = Entity("T", "怪物", blood_limit=500, current_hp=500)
        for n, v in statuses:
            t.add_status(StatusEffect(name=n, remaining_rounds=-1, value=v, source="x"))
        if any(n == "龙鳞" and v > 0 for n, v in statuses):
            t.relics.append(Relic("叠鳞甲", ""))
        before = old.on_incoming_adjust(t, amount, dtype, None, state)
        after = adapter.on_incoming_adjust(t, amount, dtype, None, state)
        if before != after:
            mismatches += 1
            print("MISMATCH", statuses, amount, dtype, before, after)
    assert mismatches == 0, f"旧/新实现 {len(status_sets)*6*3} 组场景必须逐结果一致"


# ==================== 4. 加害 + 龙鳞 组合顺序 ====================

def test_jiahai_then_longlin_combo_order_unchanged():
    manager = CombatHookManager()
    target = _target(("加害", 2), ("龙鳞", 8))
    # 现行顺序：max(0, (8 + 2) - 8) = 2 —— 加害先于龙鳞
    assert manager.apply_incoming_adjust(target, 8, "普通", None, _State()) == 9


# ==================== 5. 只触发一次 ====================

def test_longlin_executes_exactly_once_through_engine():
    state, combat, player, enemy = _arena()
    enemy.relics.append(Relic("叠鳞甲", ""))
    detail = combat._apply_hostile_damage(enemy, 10, source=player)
    # raw_damage 是加减区前的原始输入；actual_damage 才是龙鳞结算后的落地值。
    assert detail["raw_damage"] == 10
    assert detail["actual_damage"] == 9
    assert enemy.current_hp == 91

    longlin_adapters = [h for h in combat.hook_manager.hooks()
                        if isinstance(h, MechanismHookAdapter) and h.mechanism.name == "叠鳞甲"]
    assert len(longlin_adapters) == 1, "龙鳞的执行壳在分发列表里只能出现一次"


def test_jiahai_and_longlin_each_execute_once():
    """加害+龙鳞同时存在：每个机制恰好执行一次，组合结果正确。"""
    state, combat, player, enemy = _arena()
    enemy.add_status(StatusEffect(name="加害", remaining_rounds=-1, value=2, source="x"))
    enemy.relics.append(Relic("叠鳞甲", ""))
    detail = combat._apply_hostile_damage(enemy, 10, source=player)
    # (10+2)-3 = 9；任何重复触发都会偏离（如加害双触发=11-3=8、龙鳞双触发=12-6=6）
    assert detail["actual_damage"] == 11
    assert enemy.current_hp == 89
    adapters = [h for h in combat.hook_manager.hooks()
                 if isinstance(h, MechanismHookAdapter)
                 and h.mechanism.when.matches_phase(Phase.INCOMING_ADJUST)]
    assert [h.mechanism.name for h in adapters] == ["加害", "叠鳞甲", "固执"]


# ==================== 6. EffectContext 传递 ====================

def test_longlin_ctx_flow_unchanged():
    """迁移不得扰动伤害链路的 EffectContext / 事件记录。"""
    state, combat, player, enemy = _arena()
    enemy.relics.append(Relic("叠鳞甲", ""))

    detail = combat._apply_hostile_damage(enemy, 8, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 8, "tags": {"daowen"}, "event_id": "LL-1",
    })

    assert "context_warning" not in detail, "正常战斗路径不得退化到 legacy fallback"
    assert detail["ctx"]["source"] == "杀伐"
    assert detail["ctx"]["mechanic"] == "damage"
    assert detail["ctx"]["event_id"] == "LL-1"
    assert detail["actual_damage"] == 7, "固定减1且不重复结算"

    damage_events = _events(combat, CombatEventType.DAMAGE_APPLIED)
    assert len(damage_events) == 1
    assert damage_events[0].ctx["event_id"] == "LL-1"
    # hp_loss 是伤害的子事件：因果链不断
    assert detail["hp_loss_ctx"]["parent_event_id"] == "LL-1"


# ==================== 7. 迁移护栏 ====================

def test_migration_guard_covers_longlin_and_is_clean():
    """龙鳞已迁移：核心管线不得再出现同名硬编码分支；当前仓库必须干净。"""
    assert check_migrated_mechanism_guards() == []


def test_migration_guard_detects_planted_longlin_hardcode(tmp_path):
    planted = tmp_path / "combat.py"
    planted.write_text('if entity.has_status("叠鳞甲"):\n    amount -= 1\n', encoding="utf-8")
    violations = check_migrated_mechanism_guards([str(planted)])
    assert len(violations) == 1
    assert violations[0]["context"]["mechanism"] == "叠鳞甲"
