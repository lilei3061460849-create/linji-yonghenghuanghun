"""【苍白之花】重做后的护栏（2026-10-11 用户令）。

新口径（事实源 `物品索引.md#苍白之花`）：**你的当前法力可以超出[法限]。**
旧效果「每场[战始]可选择是否疲惫5；若选择，则[战终]精力+1」已废止。

这里只钉**新效果**的边界：
1. 持有者当前法力可以溢出，且因为[攻击力]=当前法力，溢出直接抬高普攻输出；
2. 只豁免法力——当前生命/当前速度仍受上限约束（不得顺手放开另外两项）；
3. 不持有者照旧被钳制（这条全局钳制是 2026-09-13 的用户裁定，不能被本遗物削弱）；
4. 遗物被封存时豁免同时失效（`CombatEngine.mana_overflow_exempt` 读 `sealed_relics`）；
5. 【枯竭】只登记下一次[战始]的当前法力损失，不压低法限，也不在支付时削掉溢出法力。
"""
from __future__ import annotations

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, Relic

RELIC = "苍白之花"


def _arena(*, relic: bool, sealed: int = 0):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=50, current_hp=50,
                    mana_limit=10, current_mana=10, speed_limit=5, current_speed=5)
    state.player = player
    state.enemies = []
    if relic:
        state.relics.append(Relic(name=RELIC, effect="你的当前法力可以超出[法限]。"))
        if sealed:
            state.sealed_relics[RELIC] = sealed
    return state, CombatEngine(state, DiceEngine()), player


def test_holder_mana_can_exceed_mana_limit_and_lifts_attack_power():
    """正常路径：法力溢出保留，且[攻击力]=当前法力跟着涨。"""
    _, combat, player = _arena(relic=True)
    player.current_mana = 25
    combat.clamp_immortal_body(player)
    assert player.current_mana == 25, f"法力应保留溢出值，实际 {player.current_mana}"
    assert player.effective_attack_power() == 25, "攻击力=当前法力，溢出应抬高普攻输出"


def test_without_relic_mana_is_still_clamped():
    """对照：不持有者仍被 2026-09-13 的全局钳制压回[法限]。"""
    _, combat, player = _arena(relic=False)
    player.current_mana = 25
    combat.clamp_immortal_body(player)
    assert player.current_mana == 10, f"应被钳回法限10，实际 {player.current_mana}"


def test_only_mana_is_exempt_hp_and_speed_still_capped():
    """边界：只豁免法力，当前生命/当前速度不得跟着放开。"""
    _, combat, player = _arena(relic=True)
    player.current_hp = 99
    player.current_speed = 99
    player.current_mana = 40
    combat.clamp_immortal_body(player)
    assert player.current_hp == 50, f"当前生命仍须≤血限，实际 {player.current_hp}"
    assert player.current_speed == 5, f"当前速度仍须≤速限，实际 {player.current_speed}"
    assert player.current_mana == 40, "法力仍应溢出保留"


def test_sealed_relic_loses_the_exemption():
    """边界：遗物被封存 → 豁免失效，法力重新被钳制。"""
    _, combat, player = _arena(relic=True, sealed=1)
    player.current_mana = 25
    combat.clamp_immortal_body(player)
    assert player.current_mana == 10, f"封存后应被钳回法限10，实际 {player.current_mana}"


def test_mana_overflow_exempt_is_a_single_shared_predicate():
    """豁免判定只有一处实现：持有者 True、非持有者 False、封存 False。"""
    _, combat, player = _arena(relic=True)
    assert combat.mana_overflow_exempt(player) is True
    _, combat_sealed, player_sealed = _arena(relic=True, sealed=1)
    assert combat_sealed.mana_overflow_exempt(player_sealed) is False
    _, combat_bare, player_bare = _arena(relic=False)
    assert combat_bare.mana_overflow_exempt(player_bare) is False


def test_kujie_cost_defers_mana_loss_without_changing_mana_limit():
    """支付只登记待扣额度；下一次战始扣当前法力，持有者的溢出同样只减4。"""
    _, combat, player = _arena(relic=True)
    player.current_mana = 25          # 溢出 15
    combat.pay_numeric_cost(player, "枯竭", 4)
    assert player.mana_limit == 10
    assert player.current_mana == 25
    assert player.pending_exhaust_mana == 4
    combat.settle_battle_start_exhaust()
    assert (player.current_mana, player.mana_limit) == (21, 10)

    _, combat_bare, player_bare = _arena(relic=False)
    player_bare.current_mana = 10
    combat_bare.pay_numeric_cost(player_bare, "枯竭", 4)
    assert (player_bare.current_mana, player_bare.mana_limit) == (10, 10)
    combat_bare.settle_battle_start_exhaust()
    assert (player_bare.current_mana, player_bare.mana_limit) == (6, 10)
