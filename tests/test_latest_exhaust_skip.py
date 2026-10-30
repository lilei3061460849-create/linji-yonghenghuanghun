"""最新裁定：枯竭在下一次战始扣蓝；跳过结束回合并保留出手计数。"""
import pytest

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState
from tests.test_spell_priority_windows import _cast, _daowen, _engine, _skip


def _combat():
    player = Entity("P", "轮回者", blood_limit=30, current_hp=30,
                    mana_limit=10, current_mana=7,
                    speed_limit=5, current_speed=5)
    enemy = Entity("M", "怪物", blood_limit=30, current_hp=30,
                   mana_limit=8, current_mana=5,
                   speed_limit=5, current_speed=5)
    state = GameState(player=player, enemies=[enemy], phase="in_combat",
                      combat_subphase="player_actions")
    return CombatEngine(state, DiceEngine()), player, enemy


def test_exhaust_registers_without_changing_current_mana_or_limit():
    combat, player, _ = _combat()
    paid = combat.pay_numeric_cost(player, "枯竭", 3)
    assert paid["remaining"] == 3
    assert (player.current_mana, player.mana_limit) == (7, 10)
    assert player.pending_exhaust_mana == 3
    assert player.reversible_cost_layers["枯竭"] == 3

    # 同一角色可累计登记，但总额不能超过法限；失败不部分登记。
    with pytest.raises(ValueError, match="无法完整承担枯竭8"):
        combat.pay_numeric_cost(player, "枯竭", 8)
    assert player.pending_exhaust_mana == 3
    assert (player.current_mana, player.mana_limit) == (7, 10)


def test_battle_start_settles_exhaust_once_after_mana_refill():
    combat, player, enemy = _combat()
    combat.pay_numeric_cost(player, "枯竭", 4)
    combat.pay_numeric_cost(enemy, "枯竭", 2)
    player.current_mana = player.mana_limit  # 与公共战始相同的满蓝顺序
    logs = combat.settle_battle_start_exhaust()
    assert (player.current_mana, player.mana_limit) == (6, 10)
    assert (enemy.current_mana, enemy.mana_limit) == (3, 8)
    assert player.pending_exhaust_mana == enemy.pending_exhaust_mana == 0
    assert player.reversible_cost_layers["枯竭"] == 0
    assert {log["entity"] for log in logs} == {"P", "M"}
    assert combat.settle_battle_start_exhaust() == []
    assert (player.current_mana, enemy.current_mana) == (6, 3)


def test_public_battle_start_consumes_pending_exhaust_only_once():
    engine = GameEngine(autosave=False, rng_seed=1)
    player = Entity("P", "轮回者", blood_limit=50, current_hp=50,
                    mana_limit=20, current_mana=3,
                    speed_limit=5, current_speed=5)
    engine.state.player = player
    engine.state.phase = "pre_battle"
    engine.state.energy = 0
    engine.state.dungeon = "扭曲都市"
    player.pending_exhaust_mana = 4
    player.reversible_cost_layers["枯竭"] = 4

    first = engine.execute_action("battle_start", {})
    assert first["success"], first
    assert (player.current_mana, player.mana_limit) == (16, 20)
    assert player.pending_exhaust_mana == 0
    assert first["exhaust_logs"][0]["paid"] == 4

    engine.state.phase = "pre_battle"
    player.current_mana = 1
    second = engine.execute_action("battle_start", {})
    assert second["success"], second
    assert player.current_mana == 20
    assert second["exhaust_logs"] == []


def test_purification_cancels_only_unsettled_exhaust():
    combat, player, _ = _combat()
    combat.pay_numeric_cost(player, "枯竭", 5)
    calc = DaoWenEngine.resolve("净化", 2, cost_choices=[{"type": "枯竭"}])
    combat.apply_daowen_effect("净化", calc, player, player)
    assert player.pending_exhaust_mana == 3
    assert player.reversible_cost_layers["枯竭"] == 3
    assert (player.current_mana, player.mana_limit) == (7, 10)

    player.current_mana = player.mana_limit
    combat.settle_battle_start_exhaust()
    assert player.current_mana == 7
    # 已经结算的法力损失不是可逆转的待扣枯竭，净化不会追溯恢复。
    with pytest.raises(ValueError, match="未拥有可净化的【枯竭】层数"):
        DaoWenEngine.resolve("净化", 1, cost_choices=[{"type": "枯竭"}],
                             target=player)
    assert player.current_mana == 7


def test_tidecall_and_mirage_both_cost_exhaust_x():
    for name in ("唤潮", "蜃景"):
        calc = DaoWenEngine.resolve(name, 3)
        assert calc["cost_type"] == "枯竭"
        assert calc["cost_mana_limit"] == 3
        assert "cost" not in calc or calc.get("cost") in (None, 0)
        assert f"枯竭3" in calc["summary"]


def test_skip_ends_side_turn_without_spending_or_resetting_actions():
    engine, player, monster = _engine(player_extra=1)
    first = _daowen(engine, "player:0", "enemy:0")
    assert first["success"], first
    assert player.actions_used_this_round == 1

    assert _skip(engine, "enemy:0")["success"]
    skipped = _skip(engine, "player:0")
    assert skipped["success"], skipped
    assert player.actions_used_this_round == 1
    assert monster.actions_used_this_round == 0
    assert engine.combat._ended_turn_sides == {"player_side", "opponent_side"}
    assert engine.state.combat_subphase == "await_round_end"

    # 新回合才按通常规则重置计数并解除双方结束标记。
    assert engine.execute_action("round_end", {})["success"]
    assert engine.execute_action("round_start", {})["success"]
    assert engine.combat._ended_turn_sides == set()
    assert player.actions_used_this_round == 0


def test_ended_side_cannot_start_action_and_spell_windows_auto_pass():
    engine, player, monster = _engine(player_extra=2)
    assert _daowen(engine, "player:0", "enemy:0")["success"]
    assert _skip(engine, "enemy:0")["success"]
    assert "opponent_side" in engine.combat._ended_turn_sides

    rejected = _daowen(engine, "enemy:0", "player:0")
    assert not rejected["success"]
    assert "已结束本回合" in rejected["error"]

    response = _cast(engine, "player:0", "发动庇护X于自身→发动再生X于自身",
                     "player:0", [1, 1])
    assert response["success"], response
    assert response["result"]["execution_status"] == "completed"
    assert response["result"].get("spell_window") is None
    assert player.actions_used_this_round == 3
    assert engine.combat._spell_stack == []
