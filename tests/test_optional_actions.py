"""
pytest - 可选遗物/法器策略（sim/optional_actions.py）

背景（用户：可以不用 但是不能不让用，修）：
此前所有脚本把可选战始遗物（猩红果实/苍白之花）一律
use:False 拒绝，回始遗物（血契/余火印，余火印已于2026-10-14删除）从不使用，终音法器（黑金名片/罪业金库/
教父左轮/共心环）没有任何发动策略——这些机制"存在但不可用"。

本测试锁定：
1. 战始遗物按情形主动发动（速度高→苍白之花；2026-10-13【三相残韵盘】删除）
2. 回始遗物按情形主动发动（回锋刀；余火印已于2026-10-14删除，血契不再是回始主动遗物）
3. 终音法器可被实际发动（黑金名片减敌血限/罪业金库换格挡/教父左轮必中伤害/
   共心环共享龙心）
4. 不满足条件时显式拒绝（策略返回 use=False，而不是让引擎代猜）
"""
import os
import sys

import pytest

from tests.setup_support import finish_initial_daowen
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.api import GameEngine
from engine.models import Consumable, Relic
from sim.optional_actions import (
    battle_start_relic_choices, round_start_relic_choices,
    try_select_shared_dragon_heart, try_use_black_card,
    try_use_crime_vault,
    try_fire_godfather_revolver, start_battle, start_round,
)


@pytest.fixture()
def engine(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "optional.db"), rng_seed=7)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    # 2026-09-28：二阶副本配方式出怪首发 S 随机、增援会提前进场，使「目标在进场后索引稳定」
    # 的单怪前提失效；本文件验的是可选遗物/法器本身，与副本阶级正交，改用一阶副本即可保留
    # "单怪首发 + 第1场无增援"的可预测场景。
    setup = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    return e


# ---------- 战始遗物 ----------

# 【折速法印】的两条用例（速度高则发动、速度低则拒绝）随该遗物于
# 2026-09-13 一并删除——其"疲惫换法力"效果已改为道纹【搏命】（2026-10-14 起并入【透支·疲惫通道】，搏命删除），
# 回归见 tests/test_boming_daowen.py。



# 2026-10-12 用户令：【血契】删掉与道纹【透支】重合的「[回始]流血4X换X法力」，
# 只保留平分。原先此处有 2 条测试专测那个回始付费决策，驱动已被删除，
# 按「唯一驱动是已删机制的测试应删除而非改写」处理；平分功能的覆盖仍在
# test_blood_pact_shares_every_supported_numeric_cost /
# test_event_and_daowen_costs_use_blood_pact_bus 等处。

# 2026-10-13：【三相残韵盘】删除，原 test_san_xiang_disc 用例（唯一驱动已删）一并删除。


# ---------- 回始遗物 ----------





def test_black_card_halves_enemy_blood_limit(engine):
    """黑金名片：碎片充足时战始发动，敌方血限真实减半。"""
    engine.state.artifacts_owned.append("黑金名片")
    engine.state.shards = 200
    engine.state.energy = 0
    bs, artifact_logs = start_battle(engine)
    assert bs.get("success"), bs
    assert any(l.get("action") == "黑金名片" and l.get("success") for l in artifact_logs)
    for m in engine.state.enemies:
        assert m.blood_limit <= m.battle_start_blood_limit


def test_crime_vault_gives_shield(engine):
    """罪业金库：受威胁且碎片充足时回始发动获得格挡。"""
    engine.state.artifacts_owned.append("罪业金库")
    engine.state.shards = 500
    engine.state.energy = 0
    bs, _ = start_battle(engine)
    assert bs.get("success"), bs
    for m in engine.state.enemies:
        m.attack_count = 3
        m.attack_power = 5
    shield_before = engine.state.player.shield
    r = try_use_crime_vault(engine)
    assert r and r.get("success"), r
    assert engine.state.player.shield > shield_before


def test_godfather_revolver_deals_must_hit_damage(engine):
    """教父左轮：战斗内可发动，造成30%血限×次数必中伤害。"""
    engine.state.artifacts_owned.append("教父左轮")  # 纯法器，不再借消耗品记耐久
    engine.state.energy = 0
    bs, _ = start_battle(engine)
    assert bs.get("success"), bs
    rs, _ = start_round(engine)
    assert rs.get("success"), rs
    target = next(x for x in engine.state.enemies if x.is_alive)
    hp_before = target.current_hp
    r = try_fire_godfather_revolver(engine)
    assert r and r.get("success"), r
    assert r["result"]["damage"] >= 1
    assert target.current_hp < hp_before


def test_shared_dragon_heart_selects_type(engine):
    """共心环：持有龙心时战始自动选择共享类型（start_battle 内发动）。"""
    engine.state.artifacts_owned.append("共心环")
    engine.state.consumables.append(Consumable(
        name="流血龙心", effect="", current_uses=5, max_uses=5,
        kind="dragon_heart", dragon_heart_type="流血"))
    engine.state.energy = 0
    bs, artifact_logs = start_battle(engine)
    assert bs.get("success"), bs
    assert any(l.get("action") == "共心环" and l.get("success") for l in artifact_logs)
    assert engine.state.shared_dragon_heart_type == "流血"


def test_start_battle_and_start_round_keep_working(engine):
    """组合入口：start_battle/start_round 正常推进，无可选遗物时不影响战斗。"""
    engine.state.energy = 0
    bs, _ = start_battle(engine)
    assert bs.get("success"), bs
    rs, _ = start_round(engine)
    assert rs.get("success"), rs
