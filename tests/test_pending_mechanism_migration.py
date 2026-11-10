"""六项挂起迁移：旧实现参考扫掠、原位顺序、封存、唯一执行与真实存档。"""
from __future__ import annotations

from pathlib import Path

import pytest

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.effect_context import make_context
from engine.mechanisms import MECHANISMS, Phase
from engine.models import Entity, GameState, Relic, StatusEffect


def arena(relics=(), hp=60):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=hp,
                    speed_limit=10, current_speed=5, mana_limit=20, current_mana=10)
    state.player = player
    state.enemies = [Entity("E", "怪物", blood_limit=100, current_hp=100,
                            attack_count=2, attack_power=3)]
    state.friends = [Entity("F", "朋友", blood_limit=100, current_hp=100)]
    state.relics = [Relic(n, "") for n in relics]
    return state, CombatEngine(state, DiceEngine(seed=7)), player


def canonical(value):
    """只归一化运行时编号/墙钟；保留事件数量、顺序与父子引用关系。"""
    ids = {}
    def walk(item):
        if isinstance(item, list):
            return [walk(value) for value in item]
        if isinstance(item, dict):
            output = {}
            for key, value in item.items():
                if key == "timestamp":
                    continue
                if key in ("event_id", "parent_event_id") and value is not None:
                    output[key] = ids.setdefault(value, f"event-{len(ids) + 1}")
                else:
                    output[key] = walk(value)
            return output
        return item
    return walk(value)


def old_blood_lineage(state, combat, entity):
    if entity.damage_dealt_this_round > 0:
        detail = state.apply_heal(entity, entity.damage_dealt_this_round, ctx={
            "timing": "round_end", "source": "血族血脉", "source_type": "relic",
            "actor": entity, "target": entity, "owner": entity,
            "mechanic": "heal", "subtype": "blood_lineage", "amount": entity.damage_dealt_this_round,
            "tags": {"relic", "round_end"}})
        return {"type": "blood_lineage_heal", "entity": entity.name,
                "amount": detail["actual_heal"], "heal_ctx": detail.get("heal_ctx")}
    payment = combat.pay_numeric_cost(entity, "流血", 20, forced=True, cost_context={
        "timing": "round_end", "source": "血族血脉", "source_type": "relic", "tags": {"automatic"}})
    return {"type": "blood_lineage_bleed", "entity": entity.name,
            "cost": payment, "amount": payment["actual_paid"]}


def old_red_curse(state, combat, entity):
    context = make_context(timing="round_end", source="赤族诅咒", source_type="bloodline",
                           actor=entity, target=entity, owner=entity,
                           mechanic="cost", subtype="bleed", amount=20,
                           tags={"bloodline", "round_end", "automatic"})
    detail = entity.take_damage(20, "代价", life_loss_multiplier=state.life_loss_multiplier(entity))
    loss = combat._record_hp_loss_event(entity, detail.get("actual_damage", 0), context, subtype="cost")
    if detail.get("died"):
        combat._check_hp_zero_death(entity, ctx=loss or context)
    return {"type": "chizu_curse_bleed", "entity": entity.name,
            "amount": detail["actual_damage"], "died": detail["died"]}


@pytest.mark.parametrize("hp", [10, 60, 100])
@pytest.mark.parametrize("dealt", [0, 1, 30, 150])
def test_blood_lineage_reference_sweep(hp, dealt):
    state, combat, player = arena(["血族血脉"], hp)
    old_state, old_combat, old_player = arena(["血族血脉"], hp)
    player.damage_dealt_this_round = old_player.damage_dealt_this_round = dealt
    expected = old_blood_lineage(old_state, old_combat, old_player)
    result = combat._dispatch_phase(Phase.ROUND_END_POST_MEDIOCRITY, target=player)
    assert canonical({"effects": result, "events": [e.to_dict() for e in state.combat_events]}) == canonical({
        "effects": [expected], "events": [e.to_dict() for e in old_state.combat_events]})
    if dealt == 0 and hp < 20:
        # 自动流血付不起照扣，正常命零，不报错。
        assert player.current_hp == 0 and not player.is_alive
        assert result[0]["cost"]["owner"]["detail"]["died"] is True
    assert (player.current_hp, player.is_alive, player.total_healed) == (
        old_player.current_hp, old_player.is_alive, old_player.total_healed)
    assert canonical([e.to_dict() for e in state.combat_events]) == canonical([e.to_dict() for e in old_state.combat_events])


@pytest.mark.parametrize("hp", [1, 20, 50])
@pytest.mark.parametrize("first_cup,stubborn", [(False, False), (True, False), (False, True)])
def test_red_curse_reference_sweep(hp, first_cup, stubborn):
    state, combat, player = arena(["第一杯"] if first_cup else [])
    old_state, old_combat, old_player = arena(["第一杯"] if first_cup else [])
    for st, pl in ((state, player), (old_state, old_player)):
        pl.entity_type = "赤族"
        pl.current_hp = hp
        if stubborn:
            pl.add_status(StatusEffect("固执", 2, 1, "test"))
    expected = old_red_curse(old_state, old_combat, old_player)
    assert combat._dispatch_phase(Phase.ROUND_END_POST_MEDIOCRITY, target=player) == [expected]
    assert (player.current_hp, player.is_alive, player.hp_lost_this_round) == (
        old_player.current_hp, old_player.is_alive, old_player.hp_lost_this_round)
    assert canonical([e.to_dict() for e in state.combat_events]) == canonical([e.to_dict() for e in old_state.combat_events])


@pytest.mark.parametrize("held,sealed", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("event_loss,fallback", [(0, 8), (4, 8), (0, 0)])
def test_leather_record_then_clear_and_next_round(held, sealed, event_loss, fallback):
    state, combat, player = arena(["皮衣"] if held else [])
    if sealed:
        state.sealed_relics["皮衣"] = 5  # 原 side_has 不检查封存，迁移不得擅自改变。
    player._hp_loss_events = [{"amount": event_loss}]
    player.hp_lost_this_round = fallback
    combat._dispatch_phase_all(Phase.ROUND_END_TRACKING, [player, *state.enemies])
    expected = (event_loss if event_loss > 0 else fallback) if held else 0
    assert state.event_modifiers.get("leather_shield_next", 0) == expected
    assert player.hp_lost_this_round == 0 and player._hp_loss_events == []
    result = combat._dispatch_phase(Phase.ROUND_START_PREPARE, target=player)
    assert player.shield == expected
    assert "leather_shield_next" not in state.event_modifiers
    assert result == ([{"type": "leather_shield", "amount": expected}] if expected else [])


def test_no_skin_still_consumes_stale_pending_reward():
    state, combat, player = arena()
    state.event_modifiers["leather_shield_next"] = 8
    assert combat._dispatch_phase(Phase.ROUND_START_PREPARE, target=player) == []
    assert player.shield == 0 and "leather_shield_next" not in state.event_modifiers


@pytest.mark.parametrize("target_type", ["轮回者", "朋友", "员工", "怪物"])
@pytest.mark.parametrize("amount", [0, 1, 17])
@pytest.mark.parametrize("damage_type", ["普通", "代价"])
def test_dragon_multiplier_reference_and_no_incoming_double(target_type, amount, damage_type):
    state, combat, player = arena(["龙族血脉"])
    target = state.enemies[0]
    target.entity_type = target_type
    expected = amount * 2 if target_type != "怪物" and damage_type != "代价" else amount
    manager = combat.hook_manager
    result = manager.apply_multiplier_adjust(target, amount, damage_type, player, state)
    assert result == expected
    assert manager.apply_incoming_adjust(target, result, damage_type, player, state) == expected


def test_late_battle_start_armor_ownership_alive_and_log_order():
    state, combat, player = arena()
    friend = state.friends[0]
    friend.relics = [Relic("防弹插板", "")]
    employee = Entity("W", "员工", blood_limit=50, current_hp=50, is_deployed=False,
                      relics=[Relic("防弹插板", "")])
    state.employees = [employee]
    state.relics = [Relic("防弹插板", "")]  # 玩家持有不等于全体朋友持有。
    logs = combat.process_relics("battle_start", {"relic_choices": {}})
    assert logs == ["防弹插板：F+15格挡", "防弹插板：W+15格挡"]
    assert friend.shield == employee.shield == 15 and player.shield == 0
    friend.is_alive = False
    assert combat._dispatch_phase(Phase.BATTLE_START_AFTER_CHOICES, target=friend) == []


# 2026-10-13：【三相残韵盘】遗物删除，原 test_sanxiang_end_reference_and_sealing
# 与 test_sanxiang_survives_real_save_load 两组用例（战终残韵+存档 roundtrip）一并删除。


def test_migrated_paths_are_unique():
    root = Path(__file__).resolve().parents[1]
    source = (root / "engine/combat.py").read_text()
    for marker in ('"type": "chizu_curse_bleed"', '"type": "blood_lineage_heal"',
                   '"type": "leather_shield"', '防弹插板：{ally.name}', 'if trigger == "battle_end" and "三相残韵盘"'):
        assert marker not in source
    assert "class DragonBloodlineMultiplierHook" not in (root / "engine/combat_hooks.py").read_text()
    names = ["赤族诅咒", "血族血脉·回终", "皮衣·获得格挡", "皮衣·记录",
             "防弹插板", "龙族血脉·乘区"]
    assert all(MECHANISMS.get(name) is not None for name in names)
    # 2026-10-13：【三相残韵盘·战终】随遗物删除，必须已注销。
    assert MECHANISMS.get("三相残韵盘·战终") is None
