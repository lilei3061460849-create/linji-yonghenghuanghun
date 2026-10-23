"""【缄默面具】迁移验证：process_relics 战始 if → BATTLE_END 相位 Mechanism。

2026-09-17 用户令改版：效果由「[战始]获得 20X 法力」改为「[战终][法限]+X」。
属性模型统一后法力=攻击力，[战始]一次性给 20X 法力等于同时白送攻击力，强度跳变；
且法力每场都会回满，[战始]给蓝的边际价值很低。改为战终永久成长。

2026-10-23 遗物彻底重做：取消 X，固定为每场[战终][法限]+2（不再读
event_modifiers.silent_mask_x，残留字段不影响结果）。

验证点：
  - 经 BATTLE_END 相位触发，固定[法限]+2；
  - 不随旧 X 字段变化；封印不触发；未持有不触发；
  - 【禁代价】是 api.py 的静态校验规则，不在本机制范围；
  - 战始相位不再出现缄默面具（已移至战终）。
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


def _arena(mana=0, mana_limit=50, relics=(), sealed=None, x=None):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=mana_limit, current_mana=mana,
                    speed_limit=10, current_speed=5)
    state.player = player
    state.enemies = [Entity("M", "怪物", blood_limit=50, current_hp=50)]
    state.relics = [Relic(name, "") for name in relics]
    if sealed:
        for name, rounds in sealed.items():
            state.sealed_relics[name] = rounds
    if x is not None:
        state.event_modifiers["silent_mask_x"] = x
    return state, CombatEngine(state, DiceEngine()), player


def _ref_silent_mask(combat, player):
    """新规则的参考实现（仅测试对照用，非生产代码）：[战终]固定[法限]+2。"""
    state = combat.state
    relics = {r.name for r in state.relics if state.sealed_relics.get(r.name, 0) <= 0}
    if "缄默面具" in relics:
        player.mana_limit += 2
        combat.clamp_immortal_body(player)
        return f"缄默面具：[法限]+2（现为{player.mana_limit}）"
    return None


def _run_new(combat, player):
    results = combat._dispatch_phase(Phase.BATTLE_END, target=player)
    return results[0] if results else None


# ==================== 1. 注册 / 旧实现删除 ====================

def test_silent_mask_registered_and_ordered():
    mech = MECHANISMS.get("缄默面具")
    assert mech is not None and mech.when.matches_phase(Phase.BATTLE_END)
    assert mech.priority == 10
    from engine.mechanisms.registry import MECHANISMS as REG
    # 战始相位不再有缄默面具（已移至战终）；战始现有龙族利爪(4)与帮派令(10)
    assert "缄默面具" not in [m.name for m in REG.phase_mechanisms(Phase.BATTLE_START)]
    assert [m.name for m in REG.phase_mechanisms(Phase.BATTLE_START)] == ["龙族利爪", "帮派令"]
    # 2026-10-13：【三相残韵盘·战终】随遗物删除，战终相位只剩缄默面具。
    assert [m.name for m in REG.phase_mechanisms(Phase.BATTLE_END)] == ["缄默面具"]


def test_old_silent_mask_block_removed():
    assert 'if "缄默面具" in relics' not in COMBAT_SOURCE, "旧缄默面具块必须删除"
    assert check_migrated_mechanism_guards() == []


# ==================== 2. 触发语义 ====================

def test_silent_mask_normal():
    """持有 + X=2 → [战终][法限]+2。"""
    state, combat, player = _arena(mana_limit=50, relics=("缄默面具",), x=2)
    results = combat._dispatch_phase(Phase.BATTLE_END, target=player)
    assert results == ["缄默面具：[法限]+2（现为52）"]
    assert player.mana_limit == 52


def test_silent_mask_fixed_two_ignores_legacy_x_field():
    """边界：X 已取消，残留的 silent_mask_x 字段（旧存档/旧事件）不得改变固定 +2。"""
    for legacy in (0, 1, 5):
        state, combat, player = _arena(mana=70, mana_limit=50,
                                       relics=("缄默面具", "不朽之躯"), x=legacy)
        results = combat._dispatch_phase(Phase.BATTLE_END, target=player)
        assert results == ["缄默面具：[法限]+2（现为52）"], (legacy, results)
        assert player.mana_limit == 52
        assert player.current_mana == 52, "加成后仍执行钳制"


def test_silent_mask_immortal_clamp_after_limit_gain():
    """法限提高后当前法力不得超过新上限（全局钳制规则）。"""
    state, combat, player = _arena(mana=45, mana_limit=50,
                                   relics=("缄默面具", "不朽之躯"))
    combat._dispatch_phase(Phase.BATTLE_END, target=player)
    assert player.mana_limit == 52
    assert player.current_mana <= player.mana_limit


def test_silent_mask_not_held_no_entry():
    state, combat, player = _arena(x=2)
    assert _run_new(combat, player) is None
    assert player.mana_limit == 50


def test_silent_mask_sealed_no_entry():
    state, combat, player = _arena(relics=("缄默面具",), x=2,
                                   sealed={"缄默面具": 2})
    assert _run_new(combat, player) is None
    assert player.mana_limit == 50


# ==================== 3. 顺序 / 只触发一次 ====================

def test_silent_mask_at_battle_end_not_start():
    """完整路径：战始只有帮派令；缄默面具改在战终结算固定[法限]+2。"""
    state, combat, player = _arena(mana_limit=50, relics=("缄默面具", "帮派令"), x=2)
    logs = combat.process_relics("battle_start", {"relic_choices": {}})
    assert logs == ["帮派令：获得洗劫3"], logs
    assert player.has_status("洗劫") and player.get_status_value("洗劫") == 3
    assert player.mana_limit == 50, "战始不得加法限"

    end_logs = combat.process_relics("battle_end")
    assert any("缄默面具" in line for line in end_logs), end_logs
    assert player.mana_limit == 52


def test_silent_mask_executes_exactly_once():
    state, combat, player = _arena(mana_limit=50, relics=("缄默面具",), x=2)
    combat.process_relics("battle_start", {"relic_choices": {}})
    combat.process_relics("battle_end")
    assert player.mana_limit == 52, "若双触发会得到 54"


# ==================== 4. 参考实现 sweep ====================

def test_silent_mask_reference_sweep_zero_mismatch():
    """参考实现 vs 新机制：持有×封印×不朽 全场景逐结果一致。"""
    mismatches = []
    total = 0
    for held, sealed, immortal in itertools.product(
            [False, True], [False, True], [False, True]):
        total += 1
        relics_a = ([r for r in (["缄默面具"] if held else [])
                     + (["不朽之躯"] if immortal else [])])
        state_a, combat_a, player_a = _arena(mana=40, mana_limit=50,
                                             relics=tuple(relics_a),
                                             sealed={"缄默面具": 2} if sealed else None)
        state_b, combat_b, player_b = _arena(mana=40, mana_limit=50,
                                             relics=tuple(relics_a),
                                             sealed={"缄默面具": 2} if sealed else None)
        old_result = _ref_silent_mask(combat_a, player_a)
        new_result = _run_new(combat_b, player_b)
        if old_result != new_result:
            mismatches.append(("result", held, sealed, immortal, old_result, new_result))
        if player_a.mana_limit != player_b.mana_limit:
            mismatches.append(("mana_limit", held, sealed, immortal,
                               player_a.mana_limit, player_b.mana_limit))

    assert total == 2 * 2 * 2
    assert not mismatches, f"{total} 组场景出现 {len(mismatches)} 组差异: {mismatches[:3]}"


def test_silent_mask_registry_unique_and_no_special_api():
    from engine.mechanisms.registry import MECHANISMS as REG
    assert len([m for m in REG.all() if m.name == "缄默面具"]) == 1
    from engine.mechanisms import verb_names
    assert not any("silent" in v or "mask" in v for v in verb_names())
