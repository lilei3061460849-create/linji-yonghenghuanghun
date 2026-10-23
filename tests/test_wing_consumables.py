"""翼类消耗品：血翼 / 烬翼（2026-10-23 用户令定稿）。

设计（用户原话）：
- 血翼：本回合你造成的伤害给予你等量回复／获得飞行1／立刻逃跑
- 烬翼：本回合你不会触发任何特殊事件（凡庸、雕塑、癌变等等）／获得飞行1／立刻逃跑
- 再加上原本的耐久回复方式（血翼：每累计失去100%[血限]生命+1；烬翼：怪物命零+1）

覆盖：三模式各自的正常路径 + 边界 + 错误输入（缺 mode／非法 mode／耐久归零）。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine  # noqa: E402
from engine.consumables import make_wing_item  # noqa: E402
from engine.models import Entity  # noqa: E402
from tests.setup_support import finish_initial_daowen  # noqa: E402


def _engine(tmp_path, *, energy=0):
    e = GameEngine(db_path=str(tmp_path / "wing.db"), rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.relics = []
    e.state.energy = energy
    return e


def _in_battle(tmp_path):
    e = _engine(tmp_path)
    e.state.consumables.extend([make_wing_item("血翼"), make_wing_item("烬翼")])
    assert e.execute_action("battle_start", {})["success"]
    assert e.execute_action("round_start", {})["success"]
    return e


def _wing(engine, name):
    return next(c for c in engine.state.consumables if c.name == name)


def _enemy(engine, name="靶子", hp=200):
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
               attack_power=1, attack_count=1)
    engine.state.enemies.append(m)
    return m


# ==================== 血翼 · 噬血 ====================

def test_blood_wing_lifesteal_heals_equal_to_damage_dealt(tmp_path):
    """正常路径：本回合你造成的伤害，按等量回复自身。"""
    e = _in_battle(tmp_path)
    player = e.state.player
    player.current_hp = 30
    assert e.execute_action("consume_item", {"name": "血翼", "mode": "lifesteal"})["success"]
    assert player.has_status("噬血")
    target = _enemy(e)
    before = player.current_hp
    result = e.combat.resolve_attack(player, target, is_must_hit=True)
    assert result["damage_dealt"] > 0
    assert player.current_hp == before + result["damage_dealt"]


def test_blood_wing_lifesteal_boundary_inactive_without_mode(tmp_path):
    """边界：没有开启噬血时，造成伤害不回血。"""
    e = _in_battle(tmp_path)
    player = e.state.player
    player.current_hp = 30
    target = _enemy(e)
    before = player.current_hp
    e.combat.resolve_attack(player, target, is_must_hit=True)
    assert player.current_hp == before
    assert not player.has_status("噬血")


def test_blood_wing_lifesteal_expires_at_round_end(tmp_path):
    """边界：「本回合」= 状态 remaining_rounds 1 且回终到期。"""
    e = _in_battle(tmp_path)
    player = e.state.player
    e.execute_action("consume_item", {"name": "血翼", "mode": "lifesteal"})
    assert player.get_status_value("噬血") == 1
    e.state.combat_subphase = "await_round_end"
    e.execute_action("round_end", {})
    assert not player.has_status("噬血")


# ==================== 烬翼 · 熄事 ====================

def test_ember_wing_suppress_blocks_special_events(tmp_path):
    """正常路径：本回合不触发凡庸／癌变／迷失。"""
    e = _in_battle(tmp_path)
    player = e.state.player
    assert e.execute_action("consume_item", {"name": "烬翼", "mode": "suppress"})["success"]
    assert e.state.suppresses_special_events(player)

    # 癌变：即使累计回复远超阈值也不触发
    player.total_healed = e.combat.cancer_threshold_of(player) * 3
    assert e.combat.check_cancer(player) is None
    # 凡庸：结算被拦下，角色仍存活
    assert e.combat._apply_mediocrity(player, "连续五回合未出手")[0].get("suppressed") is True
    # 迷失：不结算（异变层数保留）
    lost = e.combat._resolve_mutation_lost(player)
    assert lost.get("lost") is False and lost.get("suppressed") is True
    assert player.is_alive


def test_ember_wing_suppress_boundary_events_fire_without_it(tmp_path):
    """边界：未开启熄事时，癌变照常触发。"""
    e = _in_battle(tmp_path)
    player = e.state.player
    player.total_healed = e.combat.cancer_threshold_of(player) * 3
    assert e.combat.check_cancer(player) is not None


def test_ember_wing_suppress_expires_at_round_end(tmp_path):
    """边界：熄事只持续本回合，回终到期后特殊事件恢复触发。"""
    e = _in_battle(tmp_path)
    player = e.state.player
    e.execute_action("consume_item", {"name": "烬翼", "mode": "suppress"})
    e.state.combat_subphase = "await_round_end"
    e.execute_action("round_end", {})
    assert not player.has_status("熄事")
    player.total_healed = e.combat.cancer_threshold_of(player) * 3
    assert e.combat.check_cancer(player) is not None


# ==================== 两件共用：飞行 / 立刻逃跑 ====================

@pytest.mark.parametrize("name", ["血翼", "烬翼"])
def test_wing_fly_mode_grants_flight(tmp_path, name):
    """正常路径：mode=fly 获得【飞行】1回合并扣1耐久。"""
    e = _in_battle(tmp_path)
    player = e.state.player
    r = e.execute_action("consume_item", {"name": name, "mode": "fly"})
    assert r["success"], r
    assert player.get_status_value("飞行") == 1
    assert _wing(e, name).current_uses == _wing(e, name).max_uses - 1


@pytest.mark.parametrize("name", ["血翼", "烬翼"])
def test_wing_flee_mode_leaves_battle_without_action_cost(tmp_path, name):
    """正常路径：mode=flee 立刻脱战，只耗1耐久、不耗出手。"""
    e = _in_battle(tmp_path)
    player = e.state.player
    actions_before = player.actions_used_this_round
    r = e.execute_action("consume_item", {"name": name, "mode": "flee"})
    assert r["success"], r
    assert e.state.phase == "pre_battle"
    assert e.state.enemies == []
    assert _wing(e, name).current_uses == _wing(e, name).max_uses - 1
    assert player.actions_used_this_round == actions_before, "消耗品不占出手"


@pytest.mark.parametrize("name", ["血翼", "烬翼"])
def test_wing_flee_blocked_in_final_duel_and_keeps_durability(tmp_path, name):
    """边界：最终死斗不能逃，且失败不扣耐久。"""
    e = _in_battle(tmp_path)
    e.state.in_final_duel = True
    before = _wing(e, name).current_uses
    r = e.execute_action("consume_item", {"name": name, "mode": "flee"})
    assert r["success"] is False
    assert _wing(e, name).current_uses == before


# ==================== 错误输入 ====================

@pytest.mark.parametrize("name", ["血翼", "烬翼"])
def test_wing_requires_valid_mode(tmp_path, name):
    """错误输入：缺 mode 或 mode 非法时拒绝，且不扣耐久。"""
    e = _in_battle(tmp_path)
    before = _wing(e, name).current_uses
    assert e.execute_action("consume_item", {"name": name})["success"] is False
    assert e.execute_action("consume_item", {"name": name, "mode": "不存在"})["success"] is False
    assert _wing(e, name).current_uses == before


def test_wing_mode_is_item_specific(tmp_path):
    """错误输入：血翼不能用烬翼的专属模式，反之亦然。"""
    e = _in_battle(tmp_path)
    before = _wing(e, "血翼").current_uses
    assert e.execute_action("consume_item", {"name": "血翼", "mode": "suppress"})["success"] is False
    assert e.execute_action("consume_item", {"name": "烬翼", "mode": "lifesteal"})["success"] is False
    assert _wing(e, "血翼").current_uses == before


@pytest.mark.parametrize("name", ["血翼", "烬翼"])
def test_depleted_wing_is_unusable_but_not_destroyed(tmp_path, name):
    """边界：耐久归零后不可用，但条目保留（不自动销毁）。"""
    e = _in_battle(tmp_path)
    _wing(e, name).current_uses = 0
    r = e.execute_action("consume_item", {"name": name, "mode": "fly"})
    assert r["success"] is False
    assert any(c.name == name and c.is_depleted for c in e.state.consumables)


# ==================== 耐久回复（原有口径回归） ====================

def test_blood_wing_recharges_per_full_blood_limit_lost(tmp_path):
    """正常路径：每累计失去100%[血限]生命，耐久+1，余数滚存。"""
    e = _in_battle(tmp_path)
    player = e.state.player
    wing = _wing(e, "血翼")
    wing.current_uses = 1
    limit = player.blood_limit
    e.combat.pay_numeric_cost(player, "流血", limit // 2)
    assert wing.current_uses == 1
    e.combat.pay_numeric_cost(player, "流血", limit - limit // 2)
    assert wing.current_uses == 2


def test_ember_wing_recharges_on_monster_death(tmp_path):
    """正常路径：怪物命零 +1 耐久，满耐久不溢出。"""
    e = _in_battle(tmp_path)
    wing = _wing(e, "烬翼")
    wing.current_uses = 2
    monster = _enemy(e, name="死亡靶", hp=20)
    monster.current_hp = 0
    e.combat._check_hp_zero_death(monster, None)
    assert wing.current_uses == 3
    wing.current_uses = wing.max_uses
    monster2 = _enemy(e, name="死亡靶二", hp=20)
    monster2.current_hp = 0
    e.combat._check_hp_zero_death(monster2, None)
    assert wing.current_uses == wing.max_uses


# ==================== 两件之间的相互作用 ====================

def test_lifesteal_heal_counts_toward_cancer_threshold(tmp_path):
    """边界：噬血回复走统一回复入口，因此照常累计癌变进度。"""
    e = _in_battle(tmp_path)
    player = e.state.player
    player.current_hp = 10
    e.execute_action("consume_item", {"name": "血翼", "mode": "lifesteal"})
    before = player.total_healed
    target = _enemy(e)
    dealt = e.combat.resolve_attack(player, target, is_must_hit=True)["damage_dealt"]
    assert player.total_healed == before + dealt


def test_suppress_prevents_cancer_death_that_lifesteal_would_cause(tmp_path):
    """边界：噬血攒出的癌变，在【熄事】当回合不结算（角色存活）。"""
    e = _in_battle(tmp_path)
    player = e.state.player
    e.execute_action("consume_item", {"name": "烬翼", "mode": "suppress"})
    player.total_healed = e.combat.cancer_threshold_of(player)
    e.combat.settle_victory_paths()
    assert player.is_alive
    assert e.combat.check_cancer(player) is None


def test_cancer_death_fires_normally_without_suppress(tmp_path):
    """错误/对照：没有熄事时，同样的癌变进度照常命零。"""
    e = _in_battle(tmp_path)
    player = e.state.player
    player.total_healed = e.combat.cancer_threshold_of(player)
    assert e.combat.check_cancer(player) is not None
    assert not player.is_alive
