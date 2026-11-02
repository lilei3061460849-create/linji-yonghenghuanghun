from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, Relic


def _state() -> tuple[GameState, CombatEngine, Entity, Entity]:
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=60, current_hp=60, speed_limit=10, current_speed=10)
    enemy = Entity("M", "怪物", blood_limit=100, current_hp=100)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def test_fatigue_cost_records_speed_change_with_cost_parent():
    _, combat, player, _ = _state()

    payment = combat.pay_numeric_cost(player, "疲惫", 3, cost_context={
        "timing": "battle_start", "source": "苍白之花", "source_type": "relic",
        "actor": player, "target": player, "mechanic": "cost", "subtype": "fatigue",
        "amount": 3, "tags": {"active_payment"}, "event_id": "cost-speed-1",
    })

    assert payment["actual_paid"] == 3
    assert player.current_speed == 7
    events = getattr(player, "_speed_change_events", [])
    assert len(events) == 1
    assert events[0]["mechanic"] == "speed_change"
    assert events[0]["amount"] == -3
    assert events[0]["parent_event_id"] == "cost-speed-1"
    assert events[0]["source"] == "苍白之花"


def test_huifeng_damage_records_speed_loss_parent_event():
    state, combat, player, enemy = _state()
    state.relics = [Relic("回锋刀", "")]

    lost = combat._lose_current_speed(player, 2, "enemy:0", require_huifeng=True, ctx={
        "timing": "battle_start", "source": "苍白之花", "source_type": "relic",
        "actor": player, "target": player, "mechanic": "speed_change", "subtype": "current_speed",
        "amount": -2, "tags": {"active_payment"}, "event_id": "speed-1",
    })

    assert lost == 2
    assert player.current_speed == 8
    # 2026-11-02：每次速度实际变化固定 5 点伤害（旧版为 3×失去点数）。
    assert enemy.current_hp == 95
    speed_event = getattr(player, "_speed_change_events", [])[0]
    assert speed_event["amount"] == -2
    # 回锋刀实际造成伤害已经落地，damage_dealt_this_round 仍保持旧逻辑累加。
    assert player.damage_dealt_this_round == 5

    enemy.current_hp = 100
    detail = combat._trigger_huifeng_on_speed_loss(player, 1, "enemy:0", speed_ctx={
        "timing": "battle_start", "event_id": "speed-parent-1",
    })
    assert detail["ctx"]["source"] == "回锋刀"
    assert detail["ctx"]["parent_event_id"] == "speed-parent-1"


def test_daowen_speed_boost_records_speed_change_context():
    _, combat, player, _ = _state()
    player.current_speed = 4

    result = combat.apply_daowen_effect("超频", {"x": 3, "speed_boost": 3}, player, player)

    assert result["effects"][0]["type"] == "speed_boost"
    assert player.current_speed == 7
    events = getattr(player, "_speed_change_events", [])
    assert len(events) == 1
    assert events[0]["source"] == "超频"
    assert events[0]["source_type"] == "daowen"
    assert events[0]["amount"] == 3


def test_speed_events_clear_on_round_boundaries():
    _, combat, player, _ = _state()
    player.current_speed -= 1   # 2026-09-13 全局上限：满速时 +1 会被吃掉，记不到事件
    combat._gain_speed(player, 1, ctx={
        "timing": "player_action", "source": "超频", "source_type": "daowen",
        "actor": player, "target": player, "mechanic": "speed_change", "subtype": "current_speed",
        "amount": 1, "tags": {"daowen"},
    })
    assert getattr(player, "_speed_change_events", [])

    combat.round_end()
    assert getattr(player, "_speed_change_events", []) == []
