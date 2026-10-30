"""清算删除后，仍保留灾厄轮盘原有注册、目标与伤害行为的守护。"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.mechanisms import MECHANISMS, Phase, Trigger
from engine.api import GameEngine
from engine.daowen import DaoWenEngine, ResonanceEngine, RELICIZED_SLOT
from engine.gamedata import REGION_EXCLUSIVE_DAOWEN
from engine.models import DaoWen, DaoWenInstance, Entity, GameState, Relic, StatusEffect
from engine.validator import check_migrated_mechanism_guards


def _arena(player_hp: int = 100, enemy_hp: int = 100):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=player_hp,
                    mana_limit=50, current_mana=50, speed_limit=10, current_speed=10)
    enemy = Entity("M", "怪物", blood_limit=enemy_hp, current_hp=enemy_hp,
                   speed_limit=2, current_speed=2)
    state.player = player
    state.enemies = [enemy]
    state.shards = 0
    return state, CombatEngine(state, DiceEngine(seed=7)), player, enemy


def _effects_of(effects, kind):
    return [e for e in effects if e.get("type") == kind]


def test_duming_is_no_longer_a_daowen_and_is_in_the_relic_pool():
    DaoWenEngine.register_all()
    assert "赌命" not in DaoWenEngine._registry
    assert "赌命" not in REGION_EXCLUSIVE_DAOWEN["罪孽都市"]
    relic_names = {name for name, _ in GameEngine.RELIC_DEFS}
    assert "灾厄轮盘" in relic_names
    assert "赌命" not in relic_names
    with pytest.raises(ValueError):
        DaoWenEngine.resolve("赌命", 1)

    # 假钞—点金之间的原通用空位已补入勒索；不得再把它当成可保留的旧占位边。
    assert RELICIZED_SLOT == "？？"
    assert RELICIZED_SLOT not in DaoWenEngine._registry
    loop = ResonanceEngine.CLOSED_LOOPS["罪孽都市闭环"]
    assert ("假钞", "曲解", "勒索") in loop
    assert ("勒索", "曲解", "点金") in loop
    assert len(loop) == 7 and not any("赌命" in (src, dst) for src, _, dst in loop)
    options = ResonanceEngine.get_available_resonance("假钞")
    assert not any(option["target_daowen"] == "赌命" for option in options)
    assert any(option["target_daowen"] == "勒索" and not option.get("reserved")
               for option in options)
    stock = {"曲解": 1}
    converted = ResonanceEngine.apply_resonance(
        "假钞", "曲解", True, True, stock, target_daowen="勒索")
    assert converted["success"] is True and converted["target"] == "勒索"


# ==================== 声明映射 ====================


def test_pipeline_has_no_hardcoded_branches_for_migrated_mechanisms():
    """护栏：核心管线不得再出现三机制的硬编码 has_status 分支。"""
    assert check_migrated_mechanism_guards() == []


# 2026-10-13：【逼债】删除，原数值口径四组用例已删。


# ==================== 灾厄轮盘遗物 ====================

def test_disaster_wheel_hits_one_target_for_thirty_percent_current_hp():
    state, combat, player, enemy = _arena(player_hp=100, enemy_hp=200)
    state.relics.append(Relic("灾厄轮盘", "[回始]随机选一名存活角色，使其失去30%当前生命"))
    effects = combat.round_start()["effects"]
    got = _effects_of(effects, "duming")
    assert len(got) == 1
    entry = got[0]
    assert entry["caster"] == player.name
    assert entry["of"] == 2 and 1 <= entry["roll"] <= 2
    # 30% 按**出手前**的当前生命算（旧口径 ceil(当前生命×30/100)），报告里带 hp_before/hp_after
    assert entry["lost"] == math.ceil(entry["hp_before"] * 30 / 100)
    assert entry["damage"] == entry["lost"]
    assert entry["hp_after"] == entry["hp_before"] - entry["lost"]
    victim = player if entry["target"] == player.name else enemy
    assert victim.current_hp == entry["hp_after"]


def test_disaster_wheel_report_matches_the_rolled_target():
    """roll/of 与目标必须一对：第 roll 个存活角色就是被打的那个（号码口径）。"""
    state, combat, player, enemy = _arena()
    state.relics.append(Relic("灾厄轮盘", "[回始]随机选一名存活角色，使其失去30%当前生命"))
    entry = _effects_of(combat.round_start()["effects"], "duming")[0]
    alive = [e for e in (state.get_all_player_side() + state.get_all_enemy_side()) if e.is_alive]
    assert alive[entry["roll"] - 1].name == entry["target"]


def test_disaster_wheel_single_alive_always_targeted_and_consumes_nothing_extra():
    state, combat, player, enemy = _arena()
    enemy.is_alive = False
    state.relics.append(Relic("灾厄轮盘", "[回始]随机选一名存活角色，使其失去30%当前生命"))
    before = player.current_hp
    entry = _effects_of(combat.round_start()["effects"], "duming")[0]
    assert entry["target"] == player.name and entry["of"] == 1 and entry["roll"] == 1
    assert player.current_hp < before


def test_disaster_wheel_requires_an_active_unsealed_relic():
    state, combat, player, _ = _arena()
    assert not _effects_of(combat.round_start()["effects"], "duming")

    state.relics.append(Relic("灾厄轮盘", "[回始]随机选一名存活角色，使其失去30%当前生命"))
    state.sealed_relics["灾厄轮盘"] = 1
    assert not _effects_of(combat.round_start()["effects"], "duming")

    state.sealed_relics.clear()
    assert len(_effects_of(combat.round_start()["effects"], "duming")) == 1


# ==================== 顺序即规则 ====================


# ==================== 对账：状态消失即清账 ====================
