"""自由结晶契约测试（2026-10-05 用户令）。

规则口径（物品索引·自由结晶；规则正文·凡庸/救赎）：
- 凡庸怪物炸裂掉落【自由结晶】（1/1）；
- 对[目标]使用后，使其随机付出一种代价，共计10（随机池＝生命/血限/枯竭/
  萎缩/速度/失忆/异变/冷却八种数值代价；唯一不入池）；
- 若目标是生命≤20%[血限]的怪物，代价结算后仍存活的，强制触发救赎事件
  （无视「没有原始怪物道纹」条件）；
- 消耗品不占主动出手，1/1 用完即耗尽。
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine import api as engine_api
from engine.api import GameEngine
from engine.models import Consumable, DaoWen, DaoWenInstance, Entity


def _engine(tmp_path) -> GameEngine:
    return GameEngine(db_path=str(tmp_path / "crystal.db"), rng_seed=7)


def _battle_state(engine: GameEngine, player_hp: int = 60) -> Entity:
    player = Entity("测试者", "轮回者", blood_limit=player_hp, current_hp=player_hp,
                    speed_limit=10, current_speed=10, mana_limit=12, current_mana=12)
    engine.state.player = player
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    return player


def _grant_crystal(engine: GameEngine) -> Consumable:
    crystal = Consumable(
        name="自由结晶",
        effect=("对[目标]使用后使其随机付出一种代价，共计10；"
                "若目标是生命≤20%血限的怪物，强制触发救赎事件"),
        current_uses=1, max_uses=1)
    engine.state.consumables.append(crystal)
    return crystal


@pytest.fixture
def single_cost(monkeypatch):
    """把随机池收窄到单一代价，保证测试确定性。"""
    def _set(cost_type: str):
        monkeypatch.setattr(engine_api, "FREE_CRYSTAL_COST_POOL", (cost_type,))
    return _set


def test_mediocrity_drops_free_crystal_with_new_effect(tmp_path):
    """正常路径：凡庸怪物炸裂掉落新口径【自由结晶】（1/1）。"""
    engine = _engine(tmp_path)
    _battle_state(engine)
    monster = Entity("血肉巨囊", "怪物", blood_limit=100, current_hp=100,
                     attack_count=1, attack_power=1)
    engine.state.enemies.append(monster)

    effects = engine.combat._apply_mediocrity(monster, "连续五回合未出手")

    assert not monster.is_alive
    loots = [e for e in effects if e.get("type") == "mediocrity_loot"]
    assert loots and "自由结晶" in loots[0]["note"]
    crystals = [c for c in engine.state.consumables if c.name == "自由结晶"]
    assert len(crystals) == 1
    assert crystals[0].current_uses == 1 and crystals[0].max_uses == 1
    assert "随机付出一种代价，共计10" in crystals[0].effect
    assert "强制触发救赎事件" in crystals[0].effect


def test_free_crystal_aging_cost_on_healthy_monster(tmp_path, single_cost):
    """正常路径：对健康怪物使用→付出血限10（血限-10），不触发救赎。"""
    single_cost("血限")
    engine = _engine(tmp_path)
    _battle_state(engine)
    monster = Entity("靶怪", "怪物", blood_limit=200, current_hp=200,
                     attack_count=1, attack_power=1)
    engine.state.enemies.append(monster)
    _grant_crystal(engine)

    r = engine.execute_action("consume_item", {
        "name": "自由结晶", "target_ref": "enemy:0"})

    assert r["success"], r
    assert r["result"]["cost_type"] == "血限"
    assert r["result"]["cost_amount"] == 10
    assert monster.blood_limit == 190
    assert r["result"]["redemption"] is None, "生命>20%血限不得触发救赎"
    assert r["result"]["uses_remaining"] == 0
    assert engine.state.consumables[0].is_depleted


def test_free_crystal_forces_redemption_on_low_hp_monster(tmp_path, single_cost):
    """正常路径：生命≤20%血限的怪物被强制救赎——即使持有原始道纹。"""
    single_cost("速度")
    engine = _engine(tmp_path)
    _battle_state(engine)
    monster = Entity("垂死怪", "怪物", blood_limit=100, current_hp=20,
                     attack_count=1, attack_power=1,
                     speed_limit=10, current_speed=10)
    # 持有原始道纹【全力】：常规救赎会被拦，自由结晶必须强制绕过
    monster.dao_wen["全力"] = DaoWenInstance(
        DaoWen("全力", "", "异变", "5X", "全力X"), x_value=1)
    engine.state.enemies.append(monster)
    _grant_crystal(engine)

    r = engine.execute_action("consume_item", {
        "name": "自由结晶", "target_ref": "enemy:0"})

    assert r["success"], r
    assert monster.current_speed == 0, "速度10应扣光当前速度"
    assert r["result"]["redemption"] is not None, "≤20%血限必须强制救赎"
    assert engine.state.pending_redemption is not None
    assert engine.state.pending_redemption["cause"] == "free_crystal"
    assert engine.state.pending_redemption["name"] == "垂死怪"


def test_free_crystal_no_redemption_above_twenty_percent(tmp_path, single_cost):
    """边界：21%血限不满足强制救赎条件。"""
    single_cost("枯竭")
    engine = _engine(tmp_path)
    _battle_state(engine)
    monster = Entity("半血怪", "怪物", blood_limit=100, current_hp=21,
                     attack_count=1, attack_power=1, mana_limit=12, current_mana=12)
    engine.state.enemies.append(monster)
    _grant_crystal(engine)

    r = engine.execute_action("consume_item", {
        "name": "自由结晶", "target_ref": "enemy:0"})

    assert r["success"], r
    assert (monster.current_mana, monster.mana_limit) == (12, 12)
    assert monster.pending_exhaust_mana == 10, "枯竭10应登记下一次战始扣蓝"
    assert r["result"]["redemption"] is None
    assert not engine.state.pending_redemption


def test_free_crystal_bleed_can_kill_before_redemption(tmp_path, single_cost):
    """边界：代价先结算——生命致死时怪物按命零死亡，不再进入救赎。"""
    single_cost("生命")
    engine = _engine(tmp_path)
    _battle_state(engine)
    monster = Entity("残血怪", "怪物", blood_limit=100, current_hp=5,
                     attack_count=1, attack_power=1)
    engine.state.enemies.append(monster)
    _grant_crystal(engine)

    r = engine.execute_action("consume_item", {
        "name": "自由结晶", "target_ref": "enemy:0"})

    assert r["success"], r
    assert not monster.is_alive, "生命10应直接命零"
    assert r["result"]["redemption"] is None
    assert not engine.state.pending_redemption


def test_free_crystal_usable_on_reincarnator(tmp_path, single_cost):
    """正常路径：自由结晶可对轮回者使用（只付代价，不涉及救赎）。"""
    single_cost("法限")
    engine = _engine(tmp_path)
    player = _battle_state(engine)
    _grant_crystal(engine)

    r = engine.execute_action("consume_item", {
        "name": "自由结晶", "target_ref": "player:0"})

    assert r["success"], r
    assert player.mana_limit == 2 and player.current_mana == 2
    assert player.speed_limit == 10, "旧萎缩支付已迁为法限，不再削减速限"
    assert r["result"]["redemption"] is None


def test_free_crystal_requires_living_combat_target(tmp_path, single_cost):
    """错误输入：无合法目标时拒绝使用，不扣耐久。"""
    single_cost("血限")
    engine = _engine(tmp_path)
    _battle_state(engine)
    crystal = _grant_crystal(engine)

    r = engine.execute_action("consume_item", {
        "name": "自由结晶", "target_ref": "enemy:99"})

    assert not r["success"]
    assert crystal.current_uses == 1


def test_settle_free_crystal_cost_covers_all_eight_types(tmp_path):
    """正常路径：八种数值代价逐一结算（失忆/冷却为自由结晶专属结算体）。"""
    engine = _engine(tmp_path)
    _battle_state(engine)

    # 失忆：最多失去当前持有的全部道纹（3种<10，全失）
    amnesiac = Entity("失忆靶", "怪物", blood_limit=100, current_hp=100)
    for name in ("杀伐", "再生", "庇护"):
        amnesiac.dao_wen[name] = DaoWenInstance(
            DaoWen(name, "", "消耗", "X", f"{name}X"), x_value=1)
    detail = engine._settle_free_crystal_cost(amnesiac, "失忆", 10)
    assert detail["paid"] == 3 and amnesiac.dao_wen == {}

    # 冷却：目标持有的全部道纹进入冷却10场
    cooled = Entity("冷却靶", "怪物", blood_limit=100, current_hp=100)
    for name in ("固执", "束缚"):
        cooled.dao_wen[name] = DaoWenInstance(
            DaoWen(name, "", "冷却", "X", f"{name}X"), x_value=1)
    detail = engine._settle_free_crystal_cost(cooled, "冷却", 10)
    assert detail["cooled_daowen"] == ["固执", "束缚"]
    assert all(inst.cooldown_remaining == 10 for inst in cooled.dao_wen.values())
    assert all(not inst.can_use() for inst in cooled.dao_wen.values())

    # 异变：+10层
    mutated = Entity("异变靶", "怪物", blood_limit=100, current_hp=100)
    engine._settle_free_crystal_cost(mutated, "异变", 10)
    assert mutated.mutation_count == 10

    # 生命：失去10生命
    bled = Entity("生命靶", "怪物", blood_limit=100, current_hp=100)
    engine._settle_free_crystal_cost(bled, "生命", 10)
    assert bled.current_hp == 90

    # 血限/法限/速度结算对应面板；枯竭只登记下一次战始扣蓝。
    aged = Entity("血限靶", "怪物", blood_limit=100, current_hp=100,
                  mana_limit=20, current_mana=20, speed_limit=8, current_speed=8)
    engine._settle_free_crystal_cost(aged, "血限", 10)
    engine._settle_free_crystal_cost(aged, "枯竭", 10)
    engine._settle_free_crystal_cost(aged, "萎缩", 10)  # 旧存档输入迁移为法限
    engine._settle_free_crystal_cost(aged, "速度", 10)
    assert aged.blood_limit == 90
    assert aged.mana_limit == 10 and aged.current_mana == 10
    assert aged.pending_exhaust_mana == 10
    assert aged.speed_limit == 8 and aged.current_speed == 0
