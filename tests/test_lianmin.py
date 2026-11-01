"""【怜悯X】守卫：封锁一种支付途径，覆盖道纹能用的一切支付途径。

2026-10-31 用户裁定：凡是道纹中可以支付的资源，它都可以禁止支付，
包括冷却、异变等特殊机制代价，不只是六条资源通道。
"""
from __future__ import annotations

import pytest

from engine.api import GameEngine
from engine.daowen import DaoWenEngine
from engine.models import DaoWen, DaoWenInstance, Entity


def _give(entity, name):
    entity.dao_wen[name] = DaoWenInstance(DaoWen(
        name=name, formula="", cost_type="", cost_formula="", effect_formula=""))


def _engine():
    engine = GameEngine(autosave=False)
    player = Entity("轮回者", "轮回者", blood_limit=80, current_hp=80,
                    mana_limit=100, current_mana=100, speed_limit=20, current_speed=20)
    enemy = Entity("甲", "怪物", blood_limit=60, current_hp=60,
                   mana_limit=30, current_mana=30, speed_limit=3, current_speed=3)
    engine.state.player = player
    engine.state.enemies = [enemy]
    engine.state.friends = []
    engine.state.employees = []
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.combat._bind_existing_hp_hooks()
    return engine, player, enemy


def _cast_lianmin(engine, blocked):
    _give(engine.state.player, "怜悯")
    return engine.execute_action("use_daowen", {
        "daowen_name": "怜悯", "x": 2, "target_ref": "enemy:0",
        "lianmin_cost": blocked, "dodge": False, "blood_shadow": False,
    })


def test_lianmin_blocks_a_resource_channel():
    engine, player, enemy = _engine()
    result = _cast_lianmin(engine, "法力")
    assert result["success"] is True, result
    assert player.current_mana == 90, "怜悯支付法力5X（X=2 → 10）"
    assert enemy.get_status_effects("怜悯")[0].remaining_rounds == 2
    assert engine.combat.blocked_payment_types(enemy) == {"法力"}
    with pytest.raises(ValueError, match="【怜悯】"):
        engine.combat.pay_numeric_cost(enemy, "法力", 1)
    # 未被封锁的途径照常
    engine.combat.pay_numeric_cost(enemy, "生命", 1)
    assert enemy.current_hp == 59


@pytest.mark.parametrize("blocked", ["冷却", "异变", "失忆", "枯竭", "攻击力"])
def test_lianmin_covers_special_cost_types(blocked):
    engine, _, enemy = _engine()
    assert _cast_lianmin(engine, blocked)["success"] is True
    assert blocked in engine.combat.blocked_payment_types(enemy)


def test_lianmin_blocks_cooldown_payment_in_practice():
    engine, _, enemy = _engine()
    assert _cast_lianmin(engine, "冷却")["success"] is True
    _give(enemy, "恐惧")
    with pytest.raises(ValueError, match="【怜悯】"):
        engine.combat.pay_daowen_cooldown("恐惧", {"cost_type": "冷却", "cost": 2}, enemy)
    assert enemy.dao_wen["恐惧"].cooldown_remaining == 0


def test_lianmin_blocks_amnesia_and_mutation_payment_in_practice():
    engine, _, enemy = _engine()
    assert _cast_lianmin(engine, "失忆")["success"] is True
    _give(enemy, "点金")
    calc = DaoWenEngine.resolve("点金", 1, caster=enemy)
    with pytest.raises(ValueError, match="【怜悯】"):
        engine.combat.apply_daowen_effect("点金", calc, enemy, enemy)
    assert "点金" in enemy.dao_wen


def test_lianmin_expires_with_its_duration():
    engine, _, enemy = _engine()
    assert _cast_lianmin(engine, "法力")["success"] is True
    status = enemy.get_status_effects("怜悯")[0]
    status.remaining_rounds = 0
    assert engine.combat.blocked_payment_types(enemy) == set()
    engine.combat.pay_numeric_cost(enemy, "法力", 1)
    assert enemy.current_mana == 29


def test_lianmin_rejects_unknown_payment_type():
    with pytest.raises(ValueError, match="支付途径非法"):
        DaoWenEngine.resolve("怜悯", 1, blocked_type="运气")
