"""【衍生】是局内 0 出手行动；【脆弱】为一次性次数型失血重复。

本文件锁四件此前只有临时脚本验过的事：

1. 三个衍生 action 的阶段声明、局外拒绝、战斗内可用，且**不消耗主动出手**；
2. 代价不变（【转换】×1＋血限2＋碎片3；刷新只付血限2）；
3. 发现 pending 的门禁：领取／刷新／放弃都是 0 出手，战斗流程动作不被阻塞；
4. 【脆弱】X 的结算口径与「次数型、不按回合递减」。
"""
from __future__ import annotations

import pytest

from engine import derivatives as derivative_rules
from engine.action_registry import ACTION_REGISTRY
from engine.api import GameEngine
from engine.daowen import DaoWenEngine
from engine.gamedata import learnability_error
from engine.models import StatusEffect

from tests.setup_support import (begin_battle, begin_round, finish_initial_daowen,
                                hand_priority_to_player)


DERIVATIVE_ACTIONS = ("derive_daowen", "refresh_derivative_choices",
                      "choose_derivative_daowen")


@pytest.fixture()
def engine(tmp_path):
    return GameEngine(db_path=str(tmp_path / "rulings.db"),
                      save_dir=str(tmp_path / "saves"),
                      autosave=False, rng_seed=7)


def _enter_combat(e: GameEngine) -> None:
    e.execute_action("setup_attributes", {
        "blood_points": 11, "speed_points": 6, "mana_points": 8})
    finish_initial_daowen(e, prefer="杀伐")
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    assert begin_battle(e)["success"]
    assert begin_round(e)["success"]
    # 先手已是通用规则（速限→法限→血限→当前生命）：本夹具的面板下怪物可能先手。
    # 本文件只验衍生/脆弱的结算口径，故声明本回合轮到轮回者。
    hand_priority_to_player(e)
    assert e.state.phase == "in_combat"
    # 与 test_bingdong / test_lianmin 等既有夹具同口径：显式绑定「失去生命」
    # 写入钩子（含轮回者本人），否则模型层只是纯数据、不触发庇护/脆弱拦截。
    e.combat._bind_existing_hp_hooks()
    # 夹具显式给足衍生代价；正式途径由开局【转换】残韵与碎片余额提供。
    e.state.resonance["转换"] = 1
    e.state.shards = 10


@pytest.mark.parametrize("action_type", DERIVATIVE_ACTIONS)
def test_derivative_actions_are_declared_in_combat(action_type):
    spec = ACTION_REGISTRY.get(action_type)
    assert spec is not None
    assert spec.allowed_phases == frozenset({"in_combat"})


def test_derive_is_rejected_outside_combat(engine):
    engine.execute_action("setup_attributes", {
        "blood_points": 11, "speed_points": 6, "mana_points": 8})
    finish_initial_daowen(engine, prefer="杀伐")
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    engine.execute_action("setup_choose_region", {"region": "扭曲都市"})
    assert engine.state.phase == "pre_battle"
    result = engine.execute_action("derive_daowen", {"source_daowen": "杀伐"})
    assert not result["success"]
    assert "战斗阶段" in result["error"]


def test_in_combat_derive_costs_zero_actions_and_unchanged_price(engine):
    _enter_combat(engine)
    player = engine.state.player
    # 即使本回合出手预算已用尽，0 出手行动仍然可用。
    player.actions_used_this_round = max(1, player.actions_used_this_round)
    before_actions = player.actions_used_this_round
    before_bl = player.blood_limit
    before_hp = player.current_hp
    before_shards = engine.state.shards
    before_res = engine.state.resonance["转换"]

    result = engine.execute_action("derive_daowen", {"source_daowen": "杀伐"})
    assert result["success"], result
    assert result["result"]["cost"] == "【转换】×1、血限2、灵魂碎片3"
    assert result["result"]["parent"] == "杀伐"
    assert 1 <= len(result["result"]["choices"]) <= 3
    assert set(result["result"]["choices"]) <= set(derivative_rules.pool_of("杀伐"))
    assert player.actions_used_this_round == before_actions
    assert player.blood_limit == before_bl - 2
    assert player.current_hp == min(before_hp, player.blood_limit)
    assert engine.state.shards == before_shards - 3
    assert engine.state.resonance["转换"] == before_res - 1
    assert list(engine.state.pending_derivative_choices) == result["result"]["choices"]
    assert engine.state.pending_derivative_parent == "杀伐"


def test_in_combat_action_list_exposes_zero_action_derivative(engine):
    _enter_combat(engine)
    entries = [a for a in engine.get_available_actions()["actions"]
               if a["action_type"] == "derive_daowen"]
    assert len(entries) == 1
    assert entries[0]["available"] is True
    assert "0次主动出手" in entries[0]["note"]


def test_pending_gate_refresh_and_claim_are_all_zero_action(engine):
    _enter_combat(engine)
    player = engine.state.player
    assert engine.execute_action("derive_daowen", {"source_daowen": "杀伐"})["success"]
    actions_before = player.actions_used_this_round
    pending = engine.get_available_actions()
    assert "0出手" in pending["phase"]
    assert {a["action_type"] for a in pending["actions"]} == {
        "choose_derivative_daowen", "refresh_derivative_choices"}

    blocked = engine.execute_action("use_daowen", {
        "daowen_name": "杀伐", "x": 1, "dodge": False,
        "blood_shadow": False, "trigger_spell_choices": {}})
    assert not blocked["success"] and "衍生发现尚未选择" in blocked["error"]
    # 战斗流程动作不被 0 出手发现阻塞；只读门禁，不真的提交攻击决策。
    for flow_action in ("prepare_monster_phase", "resolve_monster_phase",
                        "round_start", "round_end", "skip", "restart_battle"):
        phase_error = engine._phase_error(flow_action, {})
        assert phase_error is None or "衍生发现尚未选择" not in phase_error["error"]

    bl_after_derive = player.blood_limit
    shards_after_derive = engine.state.shards
    res_after_derive = engine.state.resonance["转换"]
    refresh = engine.execute_action("refresh_derivative_choices", {})
    assert refresh["success"], refresh
    assert refresh["result"]["cost"] == "血限2"
    assert player.blood_limit == bl_after_derive - 2
    assert engine.state.shards == shards_after_derive
    assert engine.state.resonance["转换"] == res_after_derive
    assert player.actions_used_this_round == actions_before

    choices = list(engine.state.pending_derivative_choices)
    pages_before = player.pages_used
    claim = engine.execute_action("choose_derivative_daowen",
                                  {"daowen_name": choices[0]})
    assert claim["success"], claim
    assert claim["result"]["granted"] == choices[0]
    assert choices[0] in player.dao_wen
    assert player.pages_used == pages_before + 1
    assert engine.state.pending_derivative_choices == []
    assert engine.state.pending_derivative_parent == ""
    assert player.actions_used_this_round == actions_before
    assert engine._phase_error("use_daowen", {}) is None


def test_abandon_is_zero_action_and_derivative_cannot_be_mother(engine):
    _enter_combat(engine)
    player = engine.state.player
    assert engine.execute_action("derive_daowen", {"source_daowen": "杀伐"})["success"]
    actions_before = player.actions_used_this_round
    owned_before = set(player.dao_wen)
    result = engine.execute_action("choose_derivative_daowen", {"abandon": True})
    assert result["success"] and result["result"]["abandoned"]
    assert engine.state.pending_derivative_choices == []
    assert set(player.dao_wen) == owned_before
    assert player.actions_used_this_round == actions_before

    # 已领取的衍生道纹不能再作母本，也不能走局外【学习】。
    engine.state.resonance["转换"] = 1
    engine.state.shards = 10
    assert engine.execute_action("derive_daowen", {"source_daowen": "杀伐"})["success"]
    pick = engine.state.pending_derivative_choices[0]
    assert engine.execute_action("choose_derivative_daowen",
                                 {"daowen_name": pick})["success"]
    again = engine.execute_action("derive_daowen", {"source_daowen": pick})
    assert not again["success"] and "衍生道纹不能再作为母本" in again["error"]
    assert learnability_error(pick, owned=set(), region=engine.state.current_region,
                              known=set(player.dao_wen)) is not None


def test_fragile_resolve_and_hp_loss_repeats_x_plus_one_times(engine):
    _enter_combat(engine)
    DaoWenEngine.register_all()
    calc = DaoWenEngine.resolve("脆弱", 2)
    assert calc["cost_type"] == "消耗" and calc["cost"] == 4
    assert calc["fragile_repeats"] == 2 and calc["duration"] == -1

    player = engine.state.player
    engine.combat.apply_daowen_effect("脆弱", calc, caster=player, target=player)
    fragile = [s for s in player.status_effects if s.name == "脆弱"]
    assert len(fragile) == 1
    assert fragile[0].value == 2 and fragile[0].remaining_rounds == -1
    # 次数型状态不按回合递减（与【庇护】同构）。
    assert player.tick_status_effects() == []
    assert player.get_status_value("脆弱") == 2

    player.blood_limit = 200
    player.current_hp = 80
    player.current_hp -= 3
    assert player.current_hp == 80 - 3 * (2 + 1) == 71
    assert player.get_status_value("脆弱") == 0
    assert not [s for s in player.status_effects if s.name == "脆弱"]


def test_fragile_is_not_consumed_by_fully_sheltered_loss(engine):
    """被【庇护】整笔免掉的失血不算「失去生命」，脆弱不消费。"""
    _enter_combat(engine)
    player = engine.state.player
    player.blood_limit = 200
    player.current_hp = 80
    player.add_status(StatusEffect(name="庇护", value=1, remaining_rounds=-1,
                                   source="测试"))
    player.add_status(StatusEffect(name="脆弱", value=2, remaining_rounds=-1,
                                   source="测试"))
    player.current_hp -= 3
    assert player.current_hp == 80
    assert player.get_status_value("脆弱") == 2
    player.current_hp -= 3
    assert player.current_hp == 80 - 3 * 3 == 71
    assert player.get_status_value("脆弱") == 0
