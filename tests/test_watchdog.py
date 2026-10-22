"""
测试：防死循环与自动中断诊断守门员系统 (CombatWatchdog & RunFailureWatchdog)
"""

import pytest
from engine.watchdog import CombatWatchdog, RunFailureWatchdog
from engine.models import GameState, Entity, StatusEffect


def test_combat_watchdog_detects_stagnant_loop_and_provides_diagnosis():
    state = GameState()
    p = Entity(name="测试者", entity_type="轮回者", blood_limit=42, current_hp=42, mana_limit=20, current_mana=20)
    m = Entity(name="狂徒", entity_type="怪物", blood_limit=100, current_hp=100)
    m.add_status(StatusEffect(name="固执", value=3, remaining_rounds=3, source="狂徒"))
    state.player = p
    state.enemies = [m]

    wd = CombatWatchdog(max_stagnant_actions=3)
    wd.reset_battle()

    # Action 1: hit absorbed / blocked -> monster HP stays 100
    r1 = {"success": True, "action": "发动道纹【杀伐X=5】"}
    res = wd.record_action("use_daowen", {"daowen": "杀伐"}, r1, state)
    assert res is None

    # Action 2: stagnant
    res = wd.record_action("use_daowen", {"daowen": "杀伐"}, r1, state)
    assert res is None

    # Action 3: stagnant -> triggers watchdog interrupt!
    res = wd.record_action("use_daowen", {"daowen": "杀伐"}, r1, state)
    assert res is not None
    assert res["interrupted"] is True
    assert "固执" in str(res["diagnosis"]["findings"])
    assert "连击" in str(res["diagnosis"]["recommendations"])


def test_run_failure_watchdog_triggers_after_max_failures():
    rf = RunFailureWatchdog(max_consecutive_failures=3)

    assert rf.record_run(1, False, "attack") is None
    assert rf.record_run(1, False, "attack") is None
    alert = rf.record_run(1, False, "attack")
    assert alert is not None
    assert alert["interrupted"] is True
    assert alert["analysis"]["primary_bottleneck_battle"] == "第 2 场"


def test_combat_watchdog_detects_5min_timeout():
    state = GameState()
    p = Entity(name="测试者", entity_type="轮回者", blood_limit=42, current_hp=42, mana_limit=20, current_mana=20)
    m = Entity(name="骨天使", entity_type="怪物", blood_limit=100, current_hp=100)
    m.add_status(StatusEffect(name="飞行", value=3, remaining_rounds=3, source="骨天使"))
    state.player = p
    state.enemies = [m]

    wd = CombatWatchdog(max_stagnant_actions=10, timeout_seconds=300.0)
    wd.reset_battle()

    r1 = {"success": True, "action": "发动道纹【杀伐X=5】"}
    res = wd.record_action("use_daowen", {"daowen": "杀伐"}, r1, state, current_time=wd.battle_start_time + 10.0)
    assert res is None

    # After 5 minutes (305 seconds) with no progress -> triggers 5-min timeout interrupt!
    res = wd.record_action("use_daowen", {"daowen": "杀伐"}, r1, state, current_time=wd.battle_start_time + 305.0)
    assert res is not None
    assert res["interrupted"] is True
    assert res["reason"] == "timeout_5min"
    assert "超过 300.0 秒/5分钟" in res["message"]
    assert "飞行" in str(res["diagnosis"]["findings"])
    # 2026-10-03：【坠落】道纹删除，推荐语只保留“残韵篡改飞行”
    assert "飞行" in str(res["diagnosis"]["recommendations"])

