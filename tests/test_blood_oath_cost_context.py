from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, Relic


def _state_with_oath_and(*relics: str) -> tuple[GameState, CombatEngine, Entity]:
    state = GameState(phase="in_combat", combat_subphase="await_round_start")
    player = Entity(
        "探针", "轮回者",
        blood_limit=90, current_hp=90,
        mana_limit=10, current_mana=0,
        speed_limit=5, current_speed=5,
    )
    state.player = player
    state.relics = [Relic("血誓戒", ""), *(Relic(name, "") for name in relics)]
    state.enemies = [Entity("靶怪", "怪物", blood_limit=100, current_hp=100)]
    return state, CombatEngine(state, DiceEngine()), player



# 2026-10-12 用户令：【共罪】删掉与道纹【透支】重合的「[回始]流血4X换X法力」，
# 只保留平分。原先此处有 2 条测试专测那个回始付费决策，驱动已被删除，
# 按「唯一驱动是已删机制的测试应删除而非改写」处理；平分功能的覆盖仍在
# test_blood_pact_shares_every_supported_numeric_cost /
# test_event_and_daowen_costs_use_blood_pact_bus 等处。

def test_blood_oath_ignores_battle_start_scarlet_fruit_context():
    """战始流血不是回始/行动/反应窗口，不触发血誓戒，也不消耗首次标记。

    2026-10-14：【猩红果实】重做为被动，不再是战始流血载体；改用其旧上下文
    （timing=battle_start 的主动流血）直接注入，意图不变。
    """
    _, combat, player = _state_with_oath_and()

    detail = combat.pay_numeric_cost(
        player, "流血", 10,
        cost_context={"timing": "battle_start", "source": "猩红果实",
                      "source_type": "relic", "tags": {"active_payment"}},
    )

    assert detail["owner"]["paid"] == 10
    assert player.current_hp == 80
    assert player.shield == 0
    assert player.blood_oath_used_this_round is False






def test_uncategorized_direct_bleed_does_not_trigger_blood_oath():
    """未显式传入 cost_context 的底层扣血不触发主动/时点监听。"""
    _, combat, player = _state_with_oath_and()

    detail = combat.pay_numeric_cost(player, "流血", 5)

    assert detail["owner"]["paid"] == 5
    assert player.current_hp == 85
    assert player.shield == 0
    assert player.blood_oath_used_this_round is False
    assert "context_warning" in detail["owner"]["detail"]


def test_player_action_bleed_context_triggers_blood_oath():
    """玩家行动来源的主动流血仍触发血誓戒。"""
    _, combat, player = _state_with_oath_and()

    detail = combat.pay_numeric_cost(
        player, "流血", 5,
        cost_context={"timing": "player_action", "source": "连击", "source_type": "daowen", "tags": {"active_payment"}},
    )

    assert detail["owner"]["detail"]["blood_oath"] == {"type": "shield", "amount": 5}
    assert player.current_hp == 85
    assert player.shield == 5
    assert player.blood_oath_used_this_round is True
