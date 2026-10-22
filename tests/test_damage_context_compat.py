from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, Relic, StatusEffect


def test_legacy_damage_gets_context_warning_without_changing_damage():
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    target = Entity("靶", "怪物", blood_limit=30, current_hp=30, shield=3)
    state.player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    state.enemies = [target]
    combat = CombatEngine(state, DiceEngine())

    detail = combat._apply_hostile_damage(target, 10)

    assert detail["raw_damage"] == 10
    assert detail["shield_absorbed"] == 3
    assert detail["actual_damage"] == 7
    assert target.current_hp == 23
    assert detail["ctx"]["mechanic"] == "damage"
    assert detail["ctx"]["tags"] == ["legacy_context"]
    assert "context_warning" in detail


def test_explicit_damage_context_has_no_warning_and_keeps_event_chain():
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    actor = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    target = Entity("靶", "怪物", blood_limit=30, current_hp=30)
    state.player = actor
    state.enemies = [target]
    combat = CombatEngine(state, DiceEngine())

    detail = combat._apply_hostile_damage(target, 5, source=actor, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": actor, "target": target, "mechanic": "damage", "subtype": "daowen",
        "amount": 5, "tags": {"daowen"}, "parent_event_id": "evt-parent",
    })

    assert detail["actual_damage"] == 5
    assert "context_warning" not in detail
    assert detail["ctx"]["source"] == "杀伐"
    assert detail["ctx"]["source_type"] == "daowen"
    assert detail["ctx"]["parent_event_id"] == "evt-parent"


def test_pierce_still_applies_to_non_attack_damage_with_context():
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    actor = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    actor.add_status(StatusEffect("贯穿", value=1, remaining_rounds=1, source="test"))
    target = Entity("靶", "怪物", blood_limit=30, current_hp=30, shield=20)
    state.player = actor
    state.enemies = [target]
    combat = CombatEngine(state, DiceEngine())

    detail = combat._apply_hostile_damage(target, 8, source=actor, ctx={
        "timing": "player_action", "source": "烙痕钉", "source_type": "relic",
        "actor": actor, "target": target, "mechanic": "damage", "subtype": "relic",
        "amount": 8, "tags": {"relic"},
    })

    assert detail["actual_damage"] == 8
    assert detail["shield_absorbed"] == 0
    assert target.shield == 20
    assert target.current_hp == 22
    assert detail["damage_type"] == "无视格挡"


def test_brand_nail_damage_records_cost_parent_event():
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    target = Entity("靶", "怪物", blood_limit=50, current_hp=50)
    state.player = player
    state.enemies = [target]
    state.relics = [Relic("烙痕钉", "")]
    state.event_modifiers["brand_nail_target_ref"] = "enemy:0"
    combat = CombatEngine(state, DiceEngine())

    payment = combat.pay_numeric_cost(player, "流血", 2, cost_context={
        "timing": "player_action", "source": "连击", "source_type": "daowen",
        "actor": player, "target": player, "mechanic": "cost", "subtype": "bleed",
        "amount": 2, "tags": {"active_payment"}, "event_id": "cost-1",
    })

    nail = payment["owner"]["detail"]["brand_nail"]
    assert target.current_hp == 40
    assert nail["ctx"]["source"] == "烙痕钉"
    assert nail["ctx"]["parent_event_id"] == "cost-1"
