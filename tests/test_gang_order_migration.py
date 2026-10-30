"""【帮派令】迁移验证：process_relics 战始 if → BATTLE_START 相位 Mechanism。

本阶段验证目标：
  - Relic 可以成为 MechanismRegistry 中的普通声明（Relic = Mechanism）；
  - 通用 Condition relic_active 与通用相位 BATTLE_START 两个抽象缺口补齐后，
    帮派令不需要任何专用 API；
  - 迁移前后行为逐场景一致（参考实现 sweep）。
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
from engine.mechanisms import MECHANISMS, Mechanism, Phase, Trigger
from engine.models import Entity, GameState, Relic, StatusEffect
from engine.validator import check_migrated_mechanism_guards

ROOT = Path(__file__).resolve().parents[1]
COMBAT_SOURCE = (ROOT / "engine" / "combat.py").read_text(encoding="utf-8")


def _arena():
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=0, speed_limit=10, current_speed=5)
    enemy = Entity("M", "怪物", blood_limit=50, current_hp=50)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def _wash_statuses(player):
    return sorted((s.name, s.value, s.remaining_rounds, s.source)
                  for s in player.status_effects)


def _run_old_reference(state, player):
    """迁移前 process_relics 帮派令块的逐行复刻（仅测试对照用，非生产代码）。"""
    relics = {r.name for r in state.relics if state.sealed_relics.get(r.name, 0) <= 0}
    if "帮派令" in relics:
        player.add_status(StatusEffect("洗劫", 3, 3, "帮派令"))
        return "帮派令：获得洗劫3"
    return None


def _run_new_mechanism(combat, player):
    results = combat._dispatch_phase(Phase.BATTLE_START, target=player)
    return results[0] if results else None


# ==================== 1. 注册 ====================

def test_gangpailing_is_registered_mechanism():
    mech = MECHANISMS.get("帮派令")
    assert mech is not None
    assert mech.when.matches_phase(Phase.BATTLE_START)
    assert mech.priority == 10
    assert mech.target is not None and mech.condition is not None
    assert mech.name == "帮派令"


def test_old_gangpailing_if_removed_from_pipeline():
    """旧 if 的生产模式已从核心管线删除（注释中的历史说明允许保留）。"""
    assert '"帮派令" in relics' not in COMBAT_SOURCE, "战始遗物集里的帮派令分支必须删除"
    assert 'StatusEffect("洗劫", 3, 3, "帮派令")' not in COMBAT_SOURCE, \
        "旧帮派令效果体不得残留在核心管线"
    assert check_migrated_mechanism_guards() == []


# ==================== 2. 触发 / 条件 / 目标 ====================

def test_gangpailing_triggers_when_held_and_unsealed():
    state, combat, player, enemy = _arena()
    state.relics = [Relic("帮派令", "")]
    logs = combat.process_relics("battle_start", {"relic_choices": {}})

    assert logs == ["帮派令：获得洗劫3"], "日志结构与迁移前完全一致"
    assert player.has_status("洗劫")
    assert player.get_status_value("洗劫") == 3
    assert player.current_hp == 100 and player.current_mana == 0, "帮派令不碰生命/法力"
    assert not enemy.has_status("洗劫"), "SELF target：只授予玩家自己"


def test_gangpailing_status_shape_exact():
    """洗劫状态最终形态：value=3、remaining_rounds=3、source=帮派令（逐字段与旧代码一致）。"""
    state, combat, player, _ = _arena()
    state.relics = [Relic("帮派令", "")]
    combat.process_relics("battle_start", {"relic_choices": {}})
    assert _wash_statuses(player) == [("洗劫", 3, 3, "帮派令")]


def test_gangpailing_sealed_does_not_trigger():
    state, combat, player, _ = _arena()
    state.relics = [Relic("帮派令", "")]
    state.sealed_relics["帮派令"] = 2   # 抵扣X封印期间不触发
    logs = combat.process_relics("battle_start", {"relic_choices": {}})
    assert logs == []
    assert not player.has_status("洗劫")


def test_gangpailing_not_held_does_not_trigger():
    state, combat, player, _ = _arena()
    logs = combat.process_relics("battle_start", {"relic_choices": {}})
    assert logs == []
    assert not player.has_status("洗劫")


# ==================== 3. 只触发一次 / 顺序 ====================

def test_gangpailing_executes_exactly_once():
    state, combat, player, _ = _arena()
    state.relics = [Relic("帮派令", "")]
    combat.process_relics("battle_start", {"relic_choices": {}})
    assert player.get_status_value("洗劫") == 3
    assert len([s for s in player.status_effects if s.name == "洗劫"]) == 1, \
        "双触发会得到值6或两个条目"


def test_gangpailing_order_between_other_battle_start_relics():
    """战始顺序：龙族利爪(4) → 缄默面具(5) → 帮派令(10) → 负岳索等。"""
    state, combat, player, _ = _arena()
    state.relics = [Relic("缄默面具", ""), Relic("帮派令", "")]
    state.event_modifiers["silent_mask_x"] = 2
    logs = combat.process_relics("battle_start", {"relic_choices": {}})
    assert logs == ["缄默面具：后续法限增加量翻倍", "帮派令：获得洗劫3"]
    assert player.mana_limit == 50, "面具激活本身不加法限"

    assert combat.process_relics("battle_end") == []
    player.mana_limit += 2
    assert player.mana_limit == 54, "战始激活后，后续法限增量翻倍"


def test_gangpailing_priority_among_battle_start_mechanisms():
    """BATTLE_START 多机制 priority：前(5) → 帮派令(10) → 后(20)。"""
    observations = []

    def make_dummy(name, priority, note):
        def effect(ctx, targets):
            observations.append((note, ctx.target.has_status("洗劫")))
            return None
        return Mechanism(name=name, when=Trigger.phase(Phase.BATTLE_START),
                         effect=effect, priority=priority)

    state, combat, player, _ = _arena()
    state.relics = [Relic("帮派令", "")]
    MECHANISMS.register(make_dummy("战始顺序·前", 5, "前"))
    MECHANISMS.register(make_dummy("战始顺序·后", 20, "后"))
    try:
        combat._dispatch_phase(Phase.BATTLE_START, target=player)
        assert observations == [("前", False), ("后", True)], \
            f"priority 顺序错误: {observations}"
        assert player.get_status_value("洗劫") == 3, "dummy 机制不得干扰帮派令"
    finally:
        MECHANISMS.unregister("战始顺序·前")
        MECHANISMS.unregister("战始顺序·后")


# ==================== 4. EffectContext ====================

def test_gangpailing_no_artificial_context():
    """旧帮派令本身没有 ctx；迁移后也不人为制造 ctx——日志/结果结构与旧代码一致。"""
    state, combat, player, _ = _arena()
    state.relics = [Relic("帮派令", "")]
    result = _run_new_mechanism(combat, player)
    assert result == "帮派令：获得洗劫3"
    # 不产生任何事件、不产生 legacy 上下文警告
    assert combat.event_stream == []
    assert player.current_hp == 100 and player.current_mana == 0


# ==================== 5. 迁移前后参考实现 sweep ====================

def test_gangpailing_reference_sweep_zero_mismatch():
    """旧帮派令块 vs 新 Mechanism：持有×封印×预存洗劫 全场景逐结果一致。"""
    pre_wash_options = [None, (1, 2, "x"), (2, 2, "帮派令")]
    mismatches = []
    total = 0
    for held, sealed, pre in itertools.product(
            [False, True], [0, 2], pre_wash_options):
        total += 1
        state_a, combat_a, player_a, _ = _arena()
        state_b, combat_b, player_b, _ = _arena()
        for state, player in ((state_a, player_a), (state_b, player_b)):
            if held:
                state.relics = [Relic("帮派令", "")]
            if sealed:
                state.sealed_relics["帮派令"] = sealed
            if pre is not None:
                player.add_status(StatusEffect("洗劫", pre[0], pre[1], pre[2]))

        old_result = _run_old_reference(state_a, player_a)
        new_result = _run_new_mechanism(combat_b, player_b)
        if old_result != new_result:
            mismatches.append(("result", held, sealed, pre, old_result, new_result))
        if _wash_statuses(player_a) != _wash_statuses(player_b):
            mismatches.append(("status", held, sealed, pre,
                               _wash_statuses(player_a), _wash_statuses(player_b)))
        if (player_a.current_hp, player_a.current_mana) != \
                (player_b.current_hp, player_b.current_mana):
            mismatches.append(("panel", held, sealed, pre))

    assert not mismatches, f"{total} 组场景出现 {len(mismatches)} 组差异: {mismatches[:3]}"


def test_gangpailing_reference_sweep_via_full_process_relics():
    """经完整 process_relics 战始路径（含校验）与旧块对照：持有/封印两类关键场景。"""
    for held, sealed in [(True, 0), (True, 2), (False, 0)]:
        state_a, combat_a, player_a, _ = _arena()
        state_b, combat_b, player_b, _ = _arena()
        for state, player in ((state_a, player_a), (state_b, player_b)):
            if held:
                state.relics = [Relic("帮派令", "")]
            if sealed:
                state.sealed_relics["帮派令"] = sealed

        # 旧参考：直接执行旧块语义
        old_result = _run_old_reference(state_a, player_a)
        # 新路径：完整 process_relics（含校验、其他遗物分支为空的真实路径）
        logs_b = combat_b.process_relics("battle_start", {"relic_choices": {}})
        new_result = logs_b[0] if logs_b else None

        assert old_result == new_result, f"held={held} sealed={sealed}"
        assert _wash_statuses(player_a) == _wash_statuses(player_b)


# ==================== 8. BATTLE_START 全管线（最终行为验证阶段新增） ====================

def test_battle_start_pipeline_silent_mask_then_gangpailing():
    """完整战始管线保持面具(5) → 帮派令(10)，面具只激活后续增量翻倍。"""
    state, combat, player, enemy = _arena()
    state.relics = [Relic("缄默面具", ""), Relic("帮派令", "")]
    state.event_modifiers["silent_mask_x"] = 2  # 旧字段不得改变现行固定机制

    logs = combat.process_relics("battle_start", {"relic_choices": {}})

    assert logs == ["缄默面具：后续法限增加量翻倍", "帮派令：获得洗劫3"], logs
    assert _wash_statuses(player) == [("洗劫", 3, 3, "帮派令")]
    assert player.current_mana == 0, "激活不直接给当前法力"
    assert player.mana_limit == 50, "激活不直接给法限"
    assert combat.process_relics("battle_end") == []

    player.mana_limit += 2
    assert player.mana_limit == 54

    # 激活标记幂等；重复战始本身仍不叠加法限。
    combat.process_relics("battle_start", {"relic_choices": {}})
    assert player.mana_limit == 54
    assert len([s for s in player.status_effects if s.name == "洗劫"]) == 1


def test_battle_start_pipeline_gangpailing_only():
    """只有帮派令时，管线照常只产出帮派令条目（缄默面具无触发）。"""
    state, combat, player, enemy = _arena()
    state.relics = [Relic("帮派令", "")]
    logs = combat.process_relics("battle_start", {"relic_choices": {}})
    assert logs == ["帮派令：获得洗劫3"]
    assert player.current_mana == 0, "无缄默面具时不得加减法力"
    assert _wash_statuses(player) == [("洗劫", 3, 3, "帮派令")]
