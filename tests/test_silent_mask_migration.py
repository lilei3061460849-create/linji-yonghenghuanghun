"""【缄默面具】声明机制验证：BATTLE_START 激活后续法限增量翻倍。

现行《物品索引》：你无法再支付任何【代价】；今后每次获得[法限]时，实际增加量翻倍。
本文件只验证法限部分；【禁代价】仍是 api.py 的独立静态校验规则。

验证点：
  - 经 BATTLE_START 相位激活，优先级位于龙族利爪之后、帮派令之前；
  - 激活本身不直接增加法限，后续增加量才翻倍；
  - 不随已取消的 X 字段变化；封印/未持有不激活；战终不重复结算。
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
from engine.models import Entity, GameState, Relic
from engine.validator import check_migrated_mechanism_guards

ROOT = Path(__file__).resolve().parents[1]
COMBAT_SOURCE = (ROOT / "engine" / "combat.py").read_text(encoding="utf-8")
MASK_LOG = "缄默面具：后续法限增加量翻倍"


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
    """现行规则的独立测试参考：战始仅置激活标记，不直接改变法限。"""
    state = combat.state
    relics = {r.name for r in state.relics if state.sealed_relics.get(r.name, 0) <= 0}
    if "缄默面具" in relics:
        object.__setattr__(player, "_silent_mask_active", True)
        return MASK_LOG
    return None


def _run_new(combat, player):
    results = combat._dispatch_phase(Phase.BATTLE_START, target=player)
    return results[0] if results else None


# ==================== 1. 注册 / 旧实现删除 ====================

def test_silent_mask_registered_and_ordered():
    mech = MECHANISMS.get("缄默面具")
    assert mech is not None and mech.when.matches_phase(Phase.BATTLE_START)
    assert mech.priority == 5
    from engine.mechanisms.registry import MECHANISMS as REG
    assert [m.name for m in REG.phase_mechanisms(Phase.BATTLE_START)] == [
        "龙族利爪", "缄默面具", "帮派令",
    ]
    assert REG.phase_mechanisms(Phase.BATTLE_END) == []


def test_old_silent_mask_block_removed():
    assert 'if "缄默面具" in relics' not in COMBAT_SOURCE, "旧缄默面具块必须删除"
    assert check_migrated_mechanism_guards() == []


# ==================== 2. 触发与后续法限增量 ====================

def test_silent_mask_normal():
    """持有：战始激活不直接加法限，随后法限 +3 实际为 +6。"""
    state, combat, player = _arena(mana_limit=50, relics=("缄默面具",), x=2)
    results = combat._dispatch_phase(Phase.BATTLE_START, target=player)
    assert results == [MASK_LOG]
    assert player.mana_limit == 50
    player.mana_limit += 3
    assert player.mana_limit == 56


def test_silent_mask_ignores_legacy_x_field():
    """边界：残留 silent_mask_x 不影响激活，也不把后续增量改回固定 +2。"""
    for legacy in (0, 1, 5):
        state, combat, player = _arena(mana=40, mana_limit=50,
                                       relics=("缄默面具",), x=legacy)
        results = combat._dispatch_phase(Phase.BATTLE_START, target=player)
        assert results == [MASK_LOG], (legacy, results)
        assert player.mana_limit == 50
        player.mana_limit += 2
        assert player.mana_limit == 54
        assert player.current_mana == 40, "提高法限本身不附带当前法力"


def test_silent_mask_immortal_clamp_after_limit_gain():
    """不朽之躯不阻止法限成长；随后获得当前法力仍受新法限钳制。"""
    state, combat, player = _arena(mana=45, mana_limit=50,
                                   relics=("缄默面具", "不朽之躯"))
    combat._dispatch_phase(Phase.BATTLE_START, target=player)
    player.mana_limit += 2
    assert player.mana_limit == 54
    player.gain_mana(99)
    combat.clamp_immortal_body(player)
    assert player.current_mana == 54


def test_silent_mask_not_held_no_entry():
    state, combat, player = _arena(x=2)
    assert _run_new(combat, player) is None
    player.mana_limit += 2
    assert player.mana_limit == 52, "未激活时后续增量不翻倍"


def test_silent_mask_sealed_no_entry():
    state, combat, player = _arena(relics=("缄默面具",), x=2,
                                   sealed={"缄默面具": 2})
    assert _run_new(combat, player) is None
    player.mana_limit += 2
    assert player.mana_limit == 52, "封印未激活时后续增量不翻倍"


# ==================== 3. 顺序 / 只触发一次 ====================

def test_silent_mask_at_battle_start_not_end():
    """完整路径：战始先激活面具再授予帮派令；战终不再加法限。"""
    state, combat, player = _arena(mana_limit=50, relics=("缄默面具", "帮派令"), x=2)
    logs = combat.process_relics("battle_start", {"relic_choices": {}})
    assert logs == [MASK_LOG, "帮派令：获得洗劫3"], logs
    assert player.has_status("洗劫") and player.get_status_value("洗劫") == 3
    assert player.mana_limit == 50, "激活本身不加法限"

    assert combat.process_relics("battle_end") == []
    assert player.mana_limit == 50
    player.mana_limit += 2
    assert player.mana_limit == 54


def test_silent_mask_executes_exactly_once():
    state, combat, player = _arena(mana_limit=50, relics=("缄默面具",), x=2)
    start_logs = combat.process_relics("battle_start", {"relic_choices": {}})
    end_logs = combat.process_relics("battle_end")
    assert [line for line in start_logs + end_logs if "缄默面具" in line] == [MASK_LOG]
    player.mana_limit += 2
    assert player.mana_limit == 54


# ==================== 4. 参考实现 sweep ====================

def test_silent_mask_reference_sweep_zero_mismatch():
    """参考实现 vs 声明机制：持有×封印×不朽全场景的激活与后续增量一致。"""
    mismatches = []
    total = 0
    for held, sealed, immortal in itertools.product(
            [False, True], [False, True], [False, True]):
        total += 1
        relics = tuple((["缄默面具"] if held else []) + (["不朽之躯"] if immortal else []))
        state_a, combat_a, player_a = _arena(
            mana=40, mana_limit=50, relics=relics,
            sealed={"缄默面具": 2} if sealed else None)
        state_b, combat_b, player_b = _arena(
            mana=40, mana_limit=50, relics=relics,
            sealed={"缄默面具": 2} if sealed else None)
        old_result = _ref_silent_mask(combat_a, player_a)
        new_result = _run_new(combat_b, player_b)
        if old_result != new_result:
            mismatches.append(("result", held, sealed, immortal, old_result, new_result))
        active_a = getattr(player_a, "_silent_mask_active", False)
        active_b = getattr(player_b, "_silent_mask_active", False)
        if active_a != active_b:
            mismatches.append(("active", held, sealed, immortal, active_a, active_b))
        player_a.mana_limit += 3
        player_b.mana_limit += 3
        if player_a.mana_limit != player_b.mana_limit:
            mismatches.append(("mana_limit", held, sealed, immortal,
                               player_a.mana_limit, player_b.mana_limit))
        expected = 56 if held and not sealed else 53
        if player_b.mana_limit != expected:
            mismatches.append(("expected_gain", held, sealed, immortal,
                               expected, player_b.mana_limit))

    assert total == 2 * 2 * 2
    assert not mismatches, f"{total} 组场景出现 {len(mismatches)} 组差异: {mismatches[:3]}"


def test_silent_mask_registry_unique_and_no_special_api():
    from engine.mechanisms.registry import MECHANISMS as REG
    assert len([m for m in REG.all() if m.name == "缄默面具"]) == 1
    from engine.mechanisms import verb_names
    assert not any("silent" in v or "mask" in v for v in verb_names())
