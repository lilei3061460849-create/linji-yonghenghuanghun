"""【连击】（2026-10-22 由【血债】改名）多支付通道测试。

用户令口径：与【透支】同例的多代价道纹——支付六选一（生命5X/法力5X/
血限X/枯竭X/碎片5X/假碎片50X，价格与效果全表同乘五），各通道只有
倍率不同，效果完全相同：选择[目标]5X次、每次1点伤害，不带任何通道专属机制。
旧名【血债】仅在读入边界自动映射，引擎不接受旧名提交。
"""
import pytest

from engine.api import GameEngine
from engine.daowen import DaoWenEngine, normalize_daowen_name
from engine.models import Entity, DaoWen, DaoWenInstance
from engine.monsters import make_monster_entity

from tests.setup_support import begin_battle, finish_initial_daowen


# ---------- 计算层：六通道同效、异价 ----------

def test_lianji_default_channel_is_bleed_legacy_price():
    calc = DaoWenEngine.resolve("连击", 4)
    assert calc["cost_type"] == "生命"
    assert calc["cost_hp"] == 20
    assert calc["hits"] == 20 and calc["damage_per_hit"] == 1
    assert calc["total_damage"] == 20


@pytest.mark.parametrize("channel,cost_key,amount,cost_type", [
    ("生命", "cost_hp", 35, "生命"),
    ("法力", "cost", 35, "消耗"),
    ("血限", "cost_blood_limit", 7, "血限"),
    ("枯竭", "cost_mana_limit", 7, "枯竭"),
    ("碎片", "cost_real_shards", 35, "碎片"),
    ("假碎片", "cost_fake_shards", 350, "碎片"),
])
def test_lianji_channels_same_effect_different_price(channel, cost_key, amount, cost_type):
    calc = DaoWenEngine.resolve("连击", 7, cost_channel=channel)
    assert calc[cost_key] == amount
    assert calc["cost_type"] == cost_type
    # 效果恒同：与缺省生命通道逐字段一致（除代价）
    assert calc["hits"] == 35 and calc["damage_per_hit"] == 1
    assert calc["total_damage"] == 35


def test_lianji_limit_channels_priced_in_whole_x():
    # 全乘五后上限类回到整数X计价，无⌈X/5⌉零头
    assert DaoWenEngine.resolve("连击", 5, cost_channel="血限")["cost_blood_limit"] == 5
    assert DaoWenEngine.resolve("连击", 6, cost_channel="枯竭")["cost_mana_limit"] == 6
    assert DaoWenEngine.resolve("连击", 1, cost_channel="血限")["cost_blood_limit"] == 1


def test_lianji_invalid_channel_rejected():
    with pytest.raises(ValueError):
        DaoWenEngine.resolve("连击", 1, cost_channel="寿命")


# ---------- 改名与旧名边界 ----------

def test_old_name_not_registered():
    with pytest.raises(ValueError):
        DaoWenEngine.resolve("血债", 1)
    assert "血债" not in DaoWenEngine._registry or DaoWenEngine.resolve("连击", 1)


def test_normalize_and_daowen_post_init():
    assert normalize_daowen_name("血债") == "连击"
    assert normalize_daowen_name("连击") == "连击"
    dw = DaoWen(name="血债", formula="", cost_type="生命", cost_formula="X", effect_formula="")
    assert dw.name == "连击"


def test_monster_panel_old_name_normalized():
    m = make_monster_entity({
        "name": "旧面板怪", "blood_limit": 30, "attack_count": 1, "attack_power": 1,
        "dao_wen": {"血债": 2},
    })
    assert list(m.dao_wen) == ["连击"]
    assert m.dao_wen["连击"].dao_wen.name == "连击"


def test_load_migration_renames_entity_daowen(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "t.db"), rng_seed=1)
    p = Entity("旧档人", "轮回者", blood_limit=60, current_hp=60, attack_count=1, attack_power=1)
    e.state.player = p
    p.dao_wen["血债"] = DaoWenInstance(
        DaoWen(name="连击", formula="", cost_type="生命", cost_formula="X", effect_formula=""),
        x_value=3)
    # DaoWen.__post_init__ 已把实例名归一；此处模拟 pickle 旧档的键与实例名双旧
    p.dao_wen["血债"].dao_wen.name = "血债"
    assert e._migrate_legacy_xuezhai_names() is True
    assert list(p.dao_wen) == ["连击"]
    assert p.dao_wen["连击"].dao_wen.name == "连击"
    assert p.dao_wen["连击"].x_value == 3
    assert e._migrate_legacy_xuezhai_names() is False  # 幂等


# ---------- 战斗层：通道支付真实落账 ----------

def _setup_battle(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "test.db"), rng_seed=4)
    e.execute_action("setup_attributes", {
        "name": "连击手", "blood_points": 5, "speed_points": 8, "mana_points": 12})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    s = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("choose_discovered_relic", {"relic_name": s["result"]["relic_choices"][0]})
    while e.state.energy > 0:
        e.execute_action("pre_battle_action", {"sub_action": "修行", "tier": 1})
    started = begin_battle(e)
    assert started["success"], started
    p = e.state.player
    p.dao_wen["连击"] = DaoWenInstance(
        DaoWen(name="连击", formula="", cost_type="生命", cost_formula="X", effect_formula=""),
        x_value=0, x_free=True)
    m = Entity("靶怪", "怪物", blood_limit=80, current_hp=80, attack_count=1, attack_power=1)
    e.state.enemies[:] = [m]
    e.execute_action("round_start", {"relic_choices": {}})
    return e, p, m


def _cast(e, m, x, channel):
    return e.execute_action("use_daowen", {
        "daowen_name": "连击", "x": x, "target": m.name,
        "lianji_cost": channel, "dodge": False})


def test_battle_mana_channel_laser(tmp_path):
    e, p, m = _setup_battle(tmp_path)
    mana0, hp0 = p.current_mana, p.current_hp
    assert mana0 >= 5
    r = _cast(e, m, 1, "法力")
    assert r["success"], r
    assert p.current_mana == mana0 - 5       # 法力通道：5/击扣法力
    assert p.current_hp == hp0               # 不生命
    assert m.current_hp == 80 - 5            # 效果恒同：5击×1点


def test_battle_real_shard_channel_only_touches_real(tmp_path):
    e, p, m = _setup_battle(tmp_path)
    e.state.shards, e.state.fake_shards = 20, 40
    r = _cast(e, m, 2, "碎片")
    assert r["success"], r
    assert e.state.shards == 10              # 5/击
    assert e.state.fake_shards == 40         # 独立通道：不走假碎片优先
    assert m.current_hp == 80 - 10           # X=2 → 10击


def test_battle_fake_shard_channel_only_touches_fake(tmp_path):
    e, p, m = _setup_battle(tmp_path)
    e.state.shards, e.state.fake_shards = 10, 200
    r = _cast(e, m, 2, "假碎片")
    assert r["success"], r
    assert e.state.fake_shards == 200 - 100  # 50/击
    assert e.state.shards == 10
    assert m.current_hp == 80 - 10           # X=2 → 10击


def test_battle_shard_channel_insufficient_rejected_cleanly(tmp_path):
    e, p, m = _setup_battle(tmp_path)
    e.state.shards, e.state.fake_shards = 9, 0
    r = _cast(e, m, 2, "碎片")
    assert r["success"] is False
    assert "无法完整承担碎片10" in r["error"]
    assert e.state.shards == 9 and m.current_hp == 80  # 未扣未伤


def test_battle_old_name_submission_rejected(tmp_path):
    e, p, m = _setup_battle(tmp_path)
    r = e.execute_action("use_daowen", {
        "daowen_name": "血债", "x": 1, "target": m.name, "dodge": False})
    assert r["success"] is False
    assert m.current_hp == 80
