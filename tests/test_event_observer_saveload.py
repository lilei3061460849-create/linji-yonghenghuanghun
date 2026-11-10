"""跨 Engine 实例存档生命周期回归（2026-08-19 修复）。

背景：事件观察者（combat_events._StateEventObserver）持引擎 id。跨进程/跨实例
读档时，pickle 恢复的观察者仍指旧引擎，事件机制会静默失效。
修复：load_game 成功替换 state 后重新绑定当前 CombatEngine 观察者。

本文件是真实跨实例场景：新 GameEngine 实例加载旧实例写出的存档。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.combat_events import CombatEventType, get_combat_event_observer
from engine.models import Entity, Relic, StatusEffect


def _setup_engine(db_path, save_dir, seed, *, with_battle=True):
    engine = GameEngine(db_path=db_path, save_dir=save_dir, rng_seed=seed)
    if not with_battle:
        return engine
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=20, current_mana=20, speed_limit=20, current_speed=5)
    enemy = Entity("M", "怪物", blood_limit=50, current_hp=50, shards=10)
    engine.state.player = player
    engine.state.enemies = [enemy]
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.current_round = 1
    return engine


def test_cross_engine_load_rebinds_observer_and_event_mechanisms_fire(tmp_path):
    save_dir = str(tmp_path)
    # 旧实例：写档
    e1 = _setup_engine(os.path.join(save_dir, "r1.db"), save_dir, seed=1)
    e1.state.relics = [Relic("焦黑发丝", "")]
    e1.state.player.add_status(StatusEffect("洗劫", -1, 2, "e"))
    assert e1.save_game("x")["success"]

    # 新实例（模拟下次启动）：读同一存档
    e2 = _setup_engine(os.path.join(save_dir, "r2.db"), save_dir, seed=2)
    res = e2.load_game("x")
    assert res["success"]

    # 观察者必须重新绑定到当前引擎（修复点）
    obs = get_combat_event_observer(e2.state)
    assert obs is not None
    assert obs._engine_id == id(e2.combat), "观察者必须指向当前 CombatEngine"

    m2 = e2.state.enemies[0]
    p2 = e2.state.player
    speed_before = p2.current_speed

    # 命零 → 焦黑发丝（ENTITY_DIED 事件机制）必须触发
    e2.combat._apply_hostile_damage(m2, 60, source=p2)
    assert m2.is_alive is False
    assert p2.current_speed == speed_before + 2, "跨进程读档后焦黑发丝必须生效"

    # 洗劫·夺碎片（DAMAGE_APPLIED 事件机制）在加载战斗中仍生效
    state2 = e2.state
    state2.combat_events.clear()
    m3 = Entity("M2", "怪物", blood_limit=40, current_hp=40, shards=8)
    state2.enemies = [m3]
    shards_before_second = state2.shards
    e2.combat._apply_hostile_damage(m3, 5, source=p2)
    assert m3.shards == 3, "洗劫·夺碎片在加载后战斗必须生效"
    assert state2.shards == shards_before_second + 5

    # HEAL_APPLIED 总线在加载后仍可分发（事件基础设施同链路）
    hits = []
    from engine.mechanisms import MECHANISMS, Mechanism, Trigger
    dummy = Mechanism(name="读档治疗观察", when=Trigger.event(CombatEventType.HEAL_APPLIED),
                      effect=lambda ctx, targets: hits.append(1))
    MECHANISMS.register(dummy)
    try:
        e2.combat.mechanism_bus.register(dummy)
        e2.state.apply_heal(p2, 1)
        assert hits == [1]
    finally:
        e2.combat.mechanism_bus.unregister(dummy)
        MECHANISMS.unregister("读档治疗观察")


def test_cross_engine_load_without_battle_no_observer_no_error(tmp_path):
    """非战斗态存档跨实例加载：无观察者绑定需求，零行为变化。"""
    save_dir = str(tmp_path)
    e1 = _setup_engine(os.path.join(save_dir, "r1.db"), save_dir, seed=3,
                       with_battle=False)
    assert e1.save_game("y")["success"]
    e2 = _setup_engine(os.path.join(save_dir, "r2.db"), save_dir, seed=4,
                       with_battle=False)
    res = e2.load_game("y")
    assert res["success"]
    # 加载后重新绑定观察者（修复无条件执行，绑定到当前引擎——无害且一致）
    obs = get_combat_event_observer(e2.state)
    assert obs is not None and obs._engine_id == id(e2.combat)


def test_same_engine_load_still_works(tmp_path):
    """同实例读档（既有路径）不受修复影响。"""
    save_dir = str(tmp_path)
    e = _setup_engine(os.path.join(save_dir, "r.db"), save_dir, seed=5)
    e.state.relics = [Relic("焦黑发丝", "")]
    assert e.save_game("z")["success"]
    assert e.load_game("z")["success"]
    obs = get_combat_event_observer(e.state)
    assert obs is not None and obs._engine_id == id(e.combat)


# ==================== 跨进程存档后继续战斗（最终行为验证阶段新增） ====================

def test_cross_engine_load_continue_battle_full_round(tmp_path):
    """跨进程读档后继续完整战斗循环：回始机制、伤害、死亡、回终机制全部照常。

    场景：回合 1 回始阶段存档 -> 新引擎加载 -> 继续 round_start/round_end -> 回合 2 回始。
    """
    save_dir = str(tmp_path)
    e1 = _setup_engine(os.path.join(save_dir, "r1.db"), save_dir, seed=11)
    p1 = e1.state.player
    m1 = e1.state.enemies[0]
    m1.current_hp = 20
    m1.blood_limit = 20
    m1.shards = 8
    p1.add_status(StatusEffect("洗劫", -1, 2, "e"))
    p1.add_status(StatusEffect("自愈", -1, 1, "e"))
    # 【衰败】删除（它是当时唯一的回始掉血源）。改用【畸变】
    # ——它的回始机制「畸变·标记」仍在（`engine/mechanisms/builtins.py`），
    # 但只发报告条目不改生命，所以断言从「掉血」改成「状态与机制条目都活过读档」。
    m1.add_status(StatusEffect("畸变", -1, 2, "e"))
    e1.state.relics = [Relic("焦黑发丝", "")]
    e1.state.current_round = 1
    assert e1.save_game("mid")["success"]

    # 新实例加载同一存档（模拟下次启动/跨进程）
    e2 = _setup_engine(os.path.join(save_dir, "r2.db"), save_dir, seed=12)
    assert e2.load_game("mid")["success"]
    p2 = e2.state.player
    m2 = e2.state.enemies[0]

    hp_before = m2.current_hp
    sp_before = p2.current_speed
    shards_before = e2.state.shards

    # 回合 1 回始：洞察·结算/畸变·标记 等 ROUND_START 机制在加载后的引擎中照常结算
    res = e2.combat.round_start()
    assert m2.has_status("畸变"), "局内状态必须活过 存档→读档"
    pending = [e for e in res["effects"]
               if e.get("type") == "deform_pending" and e.get("entity") == m2.name]
    assert pending, f"畸变·标记必须在读档后照常结算: {res['effects']}"
    assert pending[0]["blood_loss"] == m2.attack_count * m2.attack_power
    assert m2.current_hp == hp_before, "畸变·标记是纯报告条目，回始不改生命"

    # 玩家行动造成伤害 -> 洗劫夺碎片（DAMAGE_APPLIED 事件机制）
    m2.shards = 8
    e2.combat._apply_hostile_damage(m2, 5, source=p2)
    assert m2.shards == 3, "读档后洗劫·夺碎片必须继续生效"
    assert e2.state.shards == shards_before + 5

    # 击杀 -> 焦黑发丝（ENTITY_DIED 事件机制）：按当前 hp 补足致死伤害
    kill_dmg = m2.current_hp + 10
    e2.combat._apply_hostile_damage(m2, kill_dmg, source=p2)
    assert m2.is_alive is False
    assert p2.current_speed == sp_before + 2, "读档后焦黑发丝必须继续生效"

    # 回终：加载后事件机制 + 回终相位照常
    e2.combat.round_end()

    # 回合 2 回始仍正常
    e2.state.current_round = 2
    e2.combat.round_start()
    assert e2.combat is not None


def test_cross_engine_load_rng_continuity(tmp_path):
    """跨进程读档后 RNG 序列连续：存档后下一次随机数 = 不存档时同一位置的随机数。"""
    save_dir = str(tmp_path)

    # 路径 A：不存档，直接消耗两次随机数
    eA = _setup_engine(os.path.join(save_dir, "a.db"), save_dir, seed=42)
    rollA1 = eA.dice.auto_roll("rA1", ["a", "b"])
    rollA2 = eA.dice.auto_roll("rA2", ["a", "b"])

    # 路径 B：第一次随机后存档 -> 新引擎加载 -> 再取第二次
    eB = _setup_engine(os.path.join(save_dir, "b1.db"), save_dir, seed=42)
    rollB1 = eB.dice.auto_roll("rB1", ["a", "b"])
    assert rollB1["player_number"] == rollA1["player_number"], "同 seed 同标签必须先对齐"
    assert eB.save_game("rng")["success"]

    eC = _setup_engine(os.path.join(save_dir, "b2.db"), save_dir, seed=43)
    assert eC.load_game("rng")["success"]
    rollB2 = eC.dice.auto_roll("rB2", ["a", "b"])

    # 存档把 dice 状态一并恢复：rB2 的结果必须等于不存档路径的 rollA2
    assert rollB2["player_number"] == rollA2["player_number"], \
        f"读档后 RNG 序列必须连续: {rollA2} vs {rollB2}"


def test_cross_engine_load_pending_dongcha_round_start(tmp_path):
    """读档时带洞察 pending：新引擎回始必须把 pending 结算为法力（mana 动词路径）。"""
    save_dir = str(tmp_path)
    e1 = _setup_engine(os.path.join(save_dir, "r1.db"), save_dir, seed=21)
    p1 = e1.state.player
    p1._dongcha_pending = 7
    p1.current_mana = 10
    assert e1.save_game("pending")["success"]

    e2 = _setup_engine(os.path.join(save_dir, "r2.db"), save_dir, seed=22)
    assert e2.load_game("pending")["success"]
    p2 = e2.state.player
    assert getattr(p2, "_dongcha_pending", 0) == 7, "pending 必须随存档恢复"

    e2.combat.round_start()
    assert getattr(p2, "_dongcha_pending", 0) == 0, "读档后回始必须结算 pending"
    assert p2.current_mana == 10 + 7, \
        f"法力=回填+洞察结算 {p2.current_mana}"
