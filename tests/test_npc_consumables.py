"""NPC持有者/阵营/真实效果及三个自动战斗入口。"""
import copy
import pytest
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, Consumable, StatusEffect
from engine.npc_consumables import auto_use_consumables, consume_tool, tick_world_item_statuses
from tests.test_ally_command_phases import _engine, _start_battle


def item(name, effect="", kind=""):
    return Consumable(name, effect, 2, 2, kind=kind)


def arena():
    state = GameState()
    state.phase = "in_combat"
    state.player = Entity("P", "轮回者", blood_limit=100, current_hp=100)
    monster = Entity("M", "怪物", blood_limit=100, current_hp=30, mana_limit=50, current_mana=10)
    state.enemies = [monster]
    return CombatEngine(state, DiceEngine(seed=1)), monster


def test_npc_heals_self_and_spends_only_own_durability():
    combat, npc = arena()
    npc.consumables = [item("备用血泵")]
    combat.state.consumables = [item("备用血泵")]
    logs = auto_use_consumables(combat, npc)
    assert npc.current_hp == 50 and combat.state.player.current_hp == 100
    assert npc.consumables[0].current_uses == 1
    assert combat.state.consumables[0].current_uses == 2
    assert logs[0]["actor_id"] == npc.runtime_id and npc.actions_used_this_round == 0


@pytest.mark.parametrize("name", ["反怪物电击枪", "高爆手雷", "强光探照灯"])
def test_tools_target_opposite_side_not_another_monster(name):
    combat, npc = arena()
    friend = Entity("F", "朋友", blood_limit=100, current_hp=100)
    combat.state.friends = [friend]
    npc.consumables = [item(name)]
    other = Entity("M2", "怪物", blood_limit=100, current_hp=1)
    combat.state.enemies.append(other)
    logs = auto_use_consumables(combat, npc)
    assert logs and other.current_hp == 1 and not other.status_effects
    assert combat.state.player.current_hp < 100 or friend.current_hp < 100 or any(
        e.has_status("蒙蔽") for e in (combat.state.player, friend))


def test_heal_block_and_mana_block_do_not_waste_items():
    combat, npc = arena()
    npc.add_status(StatusEffect("坏死", 2, value=1))
    npc.add_status(StatusEffect("勾魂", 2, value=1))
    npc.consumables = [item("备用血泵"), item("储能电池")]
    assert auto_use_consumables(combat, npc) == []
    assert [c.current_uses for c in npc.consumables] == [2, 2]


def test_first_aid_makes_explicit_debuff_choice():
    combat, npc = arena()
    npc.add_status(StatusEffect("坏死", 2, value=1))
    npc.consumables = [item("急救箱")]
    logs = auto_use_consumables(combat, npc)
    assert logs and not npc.has_status("坏死")
    assert npc.current_hp == 30  # heal occurs before debuff removal, as for player


def test_exhausted_dead_and_unknown_items_not_spent():
    combat, npc = arena()
    npc.consumables = [item("未知物品"), item("备用血泵"), item("龙血瓶", "回复20")]
    npc.consumables[1].current_uses = 0
    assert auto_use_consumables(combat, npc) == []
    npc.is_alive = False
    npc.consumables[1].current_uses = 2
    assert auto_use_consumables(combat, npc) == []
    assert npc.consumables[1].current_uses == 2


def test_invalid_tool_target_no_consumption():
    combat, npc = arena()
    tool = item("反怪物电击枪")
    before = copy.deepcopy(combat.state.to_dict())
    result = consume_tool(combat, tool, {"target_ref": "enemy:0"}, npc, [combat.state.player])
    assert not result["success"] and tool.current_uses == 2
    assert combat.state.to_dict() == before


def test_named_wing_sculpture_and_healing_use_real_holder():
    combat, npc = arena()
    npc.consumables = [item("定命丸"), item("血翼", kind="wing"),
                       item("雕塑", kind="sculpture"), item("绷带", "恢复10生命")]
    logs = auto_use_consumables(combat, npc)
    assert len(logs) == 4
    assert npc.has_status("固执") and npc.has_status("飞行")
    assert npc.current_hp == 40 and combat.state.player.current_hp == 85
    assert not combat.state.player.status_effects


def test_wanjunyin_owned_by_npc_and_never_borrowed_from_player():
    combat, npc = arena()
    npc.consumables = [item("万钧印")]
    assert combat._wanjunyin_holder(npc)
    combat.resolve_attack(combat.state.player, npc, dodge=False, parry=False,
                          spell_choices={"before": {}, "after": {}})
    assert npc.consumables[0].current_uses == 1
    ally = Entity("F", "朋友")
    combat.state.consumables = [item("万钧印")]
    assert not combat._wanjunyin_holder(ally)


@pytest.mark.parametrize("kind", ["friend", "employee"])
def test_ally_autonomous_entry_uses_own_items(kind):
    e = _engine("npc-items-" + kind)
    ally = _start_battle(e)
    if kind == "employee":
        e.state.friends = []
        ally.entity_type = "员工"
        ally.is_deployed = True
        e.state.employees = [ally]
    ally.current_hp = 10
    ally.consumables = [item("备用血泵")]
    result = e.execute_action("resolve_ally_phases", {})
    assert result["success"] and ally.current_hp == 30
    assert result["result"]["allies"][0]["consumables"]


def test_world_consumable_status_clock_ticks_once_and_survives_copy():
    combat, npc = arena()
    npc.consumables = [item("定命丸")]
    tick_world_item_statuses(combat.state, [npc], 1)
    auto_use_consumables(combat, npc)
    tick_world_item_statuses(combat.state, [npc], 1)
    assert npc.has_status("固执")
    state = copy.deepcopy(combat.state)
    tick_world_item_statuses(state, state.enemies, 2)
    assert not state.enemies[0].has_status("固执")
