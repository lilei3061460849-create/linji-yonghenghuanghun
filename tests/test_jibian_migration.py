"""【畸变·结算】迁移验证：round_end 内嵌块 → ROUND_END 相位 Mechanism。

锚定语义（审计确认）：
  - 分发点位于 round_end 第一逐实体循环顶部、凡庸 tick 之前（原畸变·结算位置）；
  - blood_loss=0 仍产生战报条目；血限公式/lethal=True/EffectContext/事件链保持；
  - 畸变致死后该实体不再执行凡庸 tick；凡庸硬块与中断裁定完全不受影响。
"""
from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.combat_events import CombatEventType
from engine.dice import DiceEngine
from engine.enums import EffectPolarity
from engine.mechanisms import MECHANISMS, Phase
from engine.models import Entity, GameState, StatusEffect
from engine.validator import check_migrated_mechanism_guards

ROOT = Path(__file__).resolve().parents[1]
COMBAT_SOURCE = (ROOT / "engine" / "combat.py").read_text(encoding="utf-8")


def _arena(enemy_hp=100, enemy_bl=None, enemy_ac=0, enemy_ap=0):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=0, speed_limit=10, current_speed=5)
    enemy = Entity("M", "怪物", blood_limit=enemy_bl if enemy_bl is not None else enemy_hp,
                   current_hp=enemy_hp, attack_count=enemy_ac, attack_power=enemy_ap)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def _events(combat, event_type):
    return [e for e in combat.event_stream if e.event_type == event_type]


def _give_jibian(entity, value=1, source="x"):
    entity.add_status(StatusEffect(name="畸变", remaining_rounds=1, value=value, source=source))


def _deform_entries(entries, entity_name):
    return [e for e in entries if e.get("type") == "deform_blood_limit_loss"
            and e.get("entity") == entity_name]


class _OldJibianReference:
    """round_end 畸变·结算旧块的逐行复刻（仅测试对照用，非生产代码）。"""

    def run(self, combat, entity):
        if entity.has_status("畸变") and entity.is_alive:
            blood_loss = max(0, entity.attack_count * entity.attack_power)
            before_limit = entity.blood_limit
            delta = max(0, entity.blood_limit - blood_loss) - entity.blood_limit
            combat._apply_blood_limit_change(
                entity, delta, "畸变", EffectPolarity.DEBUFF.value,
                source_type="daowen", subtype="deform",
                ctx={"timing": "round_end", "source": "畸变", "source_type": "daowen",
                     "actor": entity, "target": entity,
                     "mechanic": "blood_limit_change", "subtype": "deform",
                     "amount": delta, "tags": {"daowen", "round_end"}},
                tags={"daowen", "round_end", "blood_limit_loss"})
            return {"type": "deform_blood_limit_loss",
                    "entity": entity.name,
                    "blood_loss": before_limit - entity.blood_limit,
                    "blood_limit_after": entity.blood_limit,
                    "hp_after": entity.current_hp,
                    "died": not entity.is_alive}
        return None


def _run_new_mechanism(combat, entity):
    results = combat._dispatch_phase(Phase.ROUND_END, target=entity)
    return next((r for r in results if isinstance(r, dict)
                 and r.get("type") == "deform_blood_limit_loss"), None)


def _stable(entry):
    if entry is None:
        return None
    return (entry.get("type"), entry.get("entity"), entry.get("blood_loss"),
            entry.get("blood_limit_after"), entry.get("hp_after"), entry.get("died"))


# ==================== 1. 注册 / 旧实现删除 ====================

def test_jibian_settle_is_registered_mechanism():
    mech = MECHANISMS.get("畸变·结算")
    assert mech is not None
    assert mech.when.matches_phase(Phase.ROUND_END)
    assert mech.priority == 10, "旧位置=回终第一循环顶部（凡庸 tick 之前）"
    from engine.mechanisms.registry import MECHANISMS as REG
    assert [m.name for m in REG.phase_mechanisms(Phase.ROUND_END)] == ["畸变·结算"]


def test_old_jibian_settle_if_removed():
    assert "deform_blood_limit_loss" not in COMBAT_SOURCE, \
        "核心管线不得残留旧畸变·结算条目代码"
    assert check_migrated_mechanism_guards() == []


# ==================== 2. 触发 / 数值 / 边界 ====================

def test_jibian_no_status_no_entry():
    state, combat, player, enemy = _arena(enemy_hp=50, enemy_bl=50)
    res = combat.round_end()
    assert _deform_entries(res["effects"], "M") == []
    assert enemy.blood_limit == 50 and enemy.current_hp == 50


def test_jibian_normal_settlement():
    state, combat, player, enemy = _arena(enemy_hp=50, enemy_bl=50, enemy_ac=2, enemy_ap=3)
    _give_jibian(enemy)
    res = combat.round_end()
    entries = _deform_entries(res["effects"], "M")
    assert entries == [{"type": "deform_blood_limit_loss", "entity": "M",
                        "blood_loss": 6, "blood_limit_after": 44,
                        "hp_after": 44, "died": False}]
    assert enemy.blood_limit == 44 and enemy.current_hp == 44
    assert player.blood_limit == 100, "无畸变者不受影响"


def test_jibian_panel_zero_still_produces_entry():
    """blood_loss=0（攻击面板为 0）仍产生战报条目——旧语义必须保持。"""
    state, combat, player, enemy = _arena(enemy_hp=50, enemy_bl=50)  # ac=ap=0
    _give_jibian(enemy)
    res = combat.round_end()
    entries = _deform_entries(res["effects"], "M")
    assert entries == [{"type": "deform_blood_limit_loss", "entity": "M",
                        "blood_loss": 0, "blood_limit_after": 50,
                        "hp_after": 50, "died": False}]
    assert enemy.blood_limit == 50


def test_jibian_formula_and_clamp():
    """失去量 = max(0, 血限−面板损失) 封底；血限压 0 连带生命封顶与命零判定（lethal=True）。"""
    cases = [
        (50, 50, 2, 3, 6, 44, 44, False),    # 常规：−6
        (50, 10, 2, 3, 6, 44, 10, False),    # 生命<血限：封顶后 hp 不变
        (6, 6, 2, 3, 6, 0, 0, True),         # 面板损失=血限 → 命零
        (5, 5, 5, 5, 5, 0, 0, True),         # 面板损失>血限 → 实际扣除封底到血限值(5)
        (30, 30, 2, 3, 6, 24, 24, False),
    ]
    for bl, hp, ac, ap, loss, bl_after, hp_after, died in cases:
        state, combat, player, enemy = _arena(enemy_hp=hp, enemy_bl=bl,
                                              enemy_ac=ac, enemy_ap=ap)
        _give_jibian(enemy)
        res = combat.round_end()
        entry = _deform_entries(res["effects"], "M")[0]
        assert entry["blood_loss"] == loss, f"bl={bl} hp={hp} ac={ac} ap={ap}"
        assert entry["blood_limit_after"] == bl_after
        assert entry["hp_after"] == hp_after
        assert entry["died"] is died
        assert enemy.blood_limit == bl_after and enemy.current_hp == hp_after
        assert enemy.is_alive is (not died)


def test_jibian_layers_do_not_affect_settlement():
    """结算只认状态存在性（公式用攻击面板，不用层数）——与旧实现一致。"""
    for layers in (1, 2, 5):
        state, combat, player, enemy = _arena(enemy_hp=30, enemy_bl=30,
                                              enemy_ac=2, enemy_ap=3)
        _give_jibian(enemy, layers)
        combat.round_end()
        assert enemy.blood_limit == 24, f"layers={layers}"


def test_jibian_dead_entity_skipped():
    state, combat, player, enemy = _arena(enemy_hp=30, enemy_bl=30, enemy_ac=2, enemy_ap=3)
    _give_jibian(enemy)
    enemy.is_alive = False
    assert _run_new_mechanism(combat, enemy) is None
    assert enemy.blood_limit == 30


# ==================== 3. ctx / 事件链 ====================

def test_jibian_ctx_and_events():
    state, combat, player, enemy = _arena(enemy_hp=50, enemy_bl=50, enemy_ac=2, enemy_ap=3)
    _give_jibian(enemy)
    combat.round_end()

    bl_events = _events(combat, CombatEventType.BLOOD_LIMIT_CHANGED)
    assert len(bl_events) == 1
    ctx = bl_events[0].ctx
    assert ctx["source"] == "畸变" and ctx["source_type"] == "daowen"
    assert ctx["mechanic"] == "blood_limit_change" and ctx["subtype"] == "deform"
    assert ctx["timing"] == "round_end"
    assert set(ctx["tags"]) == {"daowen", "round_end", "blood_limit_loss"}
    assert bl_events[0].data["blood_limit_after"] == 44
    # 注：_blood_limit_events 会被 round_end 自身的重置循环清空（既有行为），
    # 事实源以 state.combat_events 为准。


def test_jibian_death_events_chain():
    state, combat, player, enemy = _arena(enemy_hp=6, enemy_bl=6, enemy_ac=2, enemy_ap=3)
    _give_jibian(enemy)
    combat.round_end()

    died = _events(combat, CombatEventType.ENTITY_DIED)
    assert len(died) == 1
    assert died[0].ctx["source"] == "畸变"
    assert died[0].ctx["parent_event_id"] is not None, "血限变化→命零 因果链必须连续"
    bl_events = _events(combat, CombatEventType.BLOOD_LIMIT_CHANGED)
    assert bl_events[0].ctx["event_id"] == died[0].ctx["parent_event_id"]


# ==================== 4. 顺序：畸变 → 凡庸 ====================

def test_jibian_death_skips_mediocrity_tick():
    """畸变致死 → 该实体跳过凡庸 tick（计数停在 4）；凡庸对其它实体照常触发。"""
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=0, speed_limit=10, current_speed=5)
    doomed = Entity("畸变者", "怪物", blood_limit=6, current_hp=6,
                    attack_count=2, attack_power=3)
    doomed.no_net_change_rounds = 4
    _give_jibian(doomed)
    ticking = Entity("平庸者", "怪物", blood_limit=50, current_hp=50)
    ticking.no_net_change_rounds = 4
    state.player = player
    state.enemies = [doomed, ticking]
    combat = CombatEngine(state, DiceEngine())

    res = combat.round_end()

    # 畸变致死者：tick 被跳过（is_alive 前置），计数停在 4
    assert doomed.is_alive is False
    assert doomed.no_net_change_rounds == 4
    # 另一只正常 tick → 达阈值 → 凡庸触发（凡庸硬块与中断裁定不受影响）
    assert ticking.is_alive is False
    assert ticking.no_net_change_rounds == 0
    assert any(e.get("type") == "mediocrity" and e.get("entity") == "平庸者"
               for e in res["effects"])

    # 顺序：畸变条目（第一循环）必须先于凡庸条目（循环后的批量结算）
    types = [e.get("type") for e in res["effects"]]
    assert types.index("deform_blood_limit_loss") < types.index("mediocrity"), \
        f"顺序异常: {types}"


def test_jibian_effects_order_within_round_end():
    """畸形结算条目位于凡庸/血族等后续回终块之前。"""
    state, combat, player, enemy = _arena(enemy_hp=50, enemy_bl=50, enemy_ac=2, enemy_ap=3)
    _give_jibian(enemy)
    enemy.shield = 5
    res = combat.round_end()
    types = [e.get("type") for e in res["effects"] if e.get("entity") == "M"]
    assert types.index("deform_blood_limit_loss") < types.index("status_expired")
    # DM裁定 2026-09-10：格挡不再[敌回终]清除，只被打掉或[战终]统一清除。
    assert enemy.shield == 5, "格挡应跨回合保留"
    assert "shield_clear" not in types


# ==================== 5. 只触发一次 ====================

def test_jibian_executes_exactly_once():
    state, combat, player, enemy = _arena(enemy_hp=50, enemy_bl=50, enemy_ac=2, enemy_ap=3)
    _give_jibian(enemy)
    res = combat.round_end()
    assert len(_deform_entries(res["effects"], "M")) == 1
    assert enemy.blood_limit == 44, "若重复触发会得到 38（44-6）"


# ==================== 6. 参考实现 sweep ====================

def test_jibian_reference_sweep_zero_mismatch():
    """旧畸变·结算块 vs 新 Mechanism：层数×攻击面板×血限×生命 全场景逐结果一致。"""
    old = _OldJibianReference()
    mismatches = []
    total = 0
    for layers, (ac, ap), bl, hp in itertools.product(
            [None, 1, 3], [(0, 0), (2, 3), (5, 5)], [6, 30, 100], [1, 25, 100]):
        if hp > bl:
            continue  # 非法初始状态不入 sweep（结算会封顶生命，行为由引擎定义）
        total += 1
        state_a, combat_a, _, enemy_a = _arena(enemy_hp=hp, enemy_bl=bl,
                                               enemy_ac=ac, enemy_ap=ap)
        state_b, combat_b, _, enemy_b = _arena(enemy_hp=hp, enemy_bl=bl,
                                               enemy_ac=ac, enemy_ap=ap)
        for ent in (enemy_a, enemy_b):
            if layers is not None:
                _give_jibian(ent, layers)

        entry_a = old.run(combat_a, enemy_a)
        entry_b = _run_new_mechanism(combat_b, enemy_b)

        if _stable(entry_a) != _stable(entry_b):
            mismatches.append(("entry", layers, ac, ap, bl, hp,
                               _stable(entry_a), _stable(entry_b)))
        if (enemy_a.blood_limit, enemy_a.current_hp, enemy_a.is_alive) != \
                (enemy_b.blood_limit, enemy_b.current_hp, enemy_b.is_alive):
            mismatches.append(("state", layers, ac, ap, bl, hp))
        if len(_events(combat_a, CombatEventType.BLOOD_LIMIT_CHANGED)) != \
                len(_events(combat_b, CombatEventType.BLOOD_LIMIT_CHANGED)):
            mismatches.append(("bl_events", layers, ac, ap, bl, hp))
        if len(_events(combat_a, CombatEventType.ENTITY_DIED)) != \
                len(_events(combat_b, CombatEventType.ENTITY_DIED)):
            mismatches.append(("death_events", layers, ac, ap, bl, hp))

    assert total > 40, "sweep 规模异常"
    assert not mismatches, f"{total} 组场景出现 {len(mismatches)} 组差异: {mismatches[:3]}"


# ==================== 7. 生产扫描 ====================

def test_jibian_single_registration_and_no_special_api():
    from engine.mechanisms.registry import MECHANISMS as REG
    mechs = [m for m in REG.all() if m.name == "畸变·结算"]
    assert len(mechs) == 1

    from engine.mechanisms import verb_names
    assert not any("jibian" in v or "deform" in v for v in verb_names())
    assert "def _jibian" not in COMBAT_SOURCE
    # 回始的【畸变·标记】块已随本批迁移到声明层（"畸变·标记"机制），
    # 核心管线不再有 deform_pending 报告代码。
    from engine.mechanisms import MECHANISMS as REG
    assert REG.get("畸变·标记") is not None
