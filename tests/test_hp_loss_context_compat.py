from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, Relic


def _combat_with_player(*relics: str) -> tuple[GameState, CombatEngine, Entity, Entity]:
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100, mana_limit=10, current_mana=0)
    enemy = Entity("M", "怪物", blood_limit=100, current_hp=100, attack_power=7)
    state.player = player
    state.enemies = [enemy]
    state.relics = [Relic(name, "") for name in relics]
    state.current_round = 1
    return state, CombatEngine(state, DiceEngine()), player, enemy


def test_hp_loss_event_recorded_for_damage_and_cost_without_double_counting():
    _, combat, player, enemy = _combat_with_player("皮衣")

    combat.pay_numeric_cost(player, "流血", 5, cost_context={
        "timing": "player_action", "source": "连击", "source_type": "daowen",
        "actor": player, "target": player, "mechanic": "cost", "subtype": "bleed",
        "amount": 5, "tags": {"active_payment"},
    })
    combat._apply_hostile_damage(player, 7, source=enemy, ctx={
        "timing": "monster_action", "source": "普通攻击", "source_type": "attack",
        "actor": enemy, "target": player, "mechanic": "damage", "subtype": "attack",
        "amount": 7, "tags": {"attack"},
    })

    events = getattr(player, "_hp_loss_events", [])
    assert [e["mechanic"] for e in events] == ["hp_loss", "hp_loss"]
    assert [e["amount"] for e in events] == [5, 7]
    assert player.hp_lost_this_round == 12

    combat.round_end()
    assert combat.state.event_modifiers["leather_shield_next"] == 12
    assert player.hp_lost_this_round == 0
    assert getattr(player, "_hp_loss_events", []) == []

    combat.round_start({})
    assert player.shield == 12


def test_raw_hp_loss_records_legacy_context_warning():
    _, combat, player, _ = _combat_with_player()

    result = combat._raw_hp_loss(player, 6)

    assert result["lost"] == 6
    assert result["hp_loss_ctx"]["mechanic"] == "hp_loss"
    assert result["hp_loss_ctx"]["tags"] == ["legacy_context"]
    assert "context_warning" in result
    assert player.hp_lost_this_round == 6
