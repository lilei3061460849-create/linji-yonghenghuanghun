"""【衍生道纹】单途径收束与旧名边界（2026-11-09 用户令）。

原【连击】六通道 /【透支】四通道 /【再生】双通道已全部收成「母本一条途径 +
衍生道纹各一条途径」。本文件锁：

* 10 条衍生道纹各自只有**一条**支付途径，价格按唯一事实源汇率；
* 同域效果恒同（杀伐域 5X 次 ×1 点伤害；透支域 X 点法力；再生域 4X 回复）；
* 战斗中每条途径真实落账，且拒绝通道参数 `lianji_cost` 之类的旧多途径入口；
* 旧名【血债】仍只在读入边界归一为【连击】（现为杀伐衍生），引擎拒收旧名提交。
"""
import pytest

from engine import derivatives as derivative_rules
from engine.api import GameEngine
from engine.daowen import DaoWenEngine, normalize_daowen_name
from engine.models import Entity, DaoWen, DaoWenInstance
from engine.monsters import make_monster_entity

from tests.setup_support import begin_battle, finish_initial_daowen


# ---------- 计算层：每条衍生只有一条途径 ----------

@pytest.mark.parametrize("name,cost_key,amount,cost_type", [
    ("连击", "cost_hp", 35, "生命"),
    ("连珠", "cost", 28, "消耗"),
    ("血伐", "cost_blood_limit", 7, "血限"),
    ("竭伐", "cost_mana_limit", 7, "枯竭"),
    ("碎伐", "cost_real_shards", 28, "碎片"),
    ("伪伐", "cost_fake_shards", 280, "碎片"),
    ("燃血", "cost_hp", 28, "生命"),
    ("折寿", "cost_blood_limit", 14, "血限"),
    ("疾贷", "cost_speed", 7, "速度"),
    ("舍力", "cost_attack", 28, "攻击力"),
])
def test_derivative_has_exactly_one_channel(name, cost_key, amount, cost_type):
    DaoWenEngine.register_all()
    calc = DaoWenEngine.resolve(name, 7)
    assert calc["cost_type"] == cost_type
    assert calc[cost_key] == amount
    assert calc["derivative_of"] == derivative_rules.parent_of(name)
    amount_keys = [k for k in calc
                   if k != "cost_type" and (k.startswith("cost_") or k == "cost")]
    assert amount_keys == [cost_key], f"{name} 出现了第二条支付途径: {amount_keys}"
    assert f"支付：{derivative_rules.spec_of(name).cost_kind}{amount}" in calc["summary"]


@pytest.mark.parametrize("name", ["连击", "连珠", "血伐", "竭伐", "碎伐", "伪伐"])
def test_shafa_derivatives_share_the_same_effect(name):
    calc = DaoWenEngine.resolve(name, 7)
    assert calc["hits"] == 35 and calc["damage_per_hit"] == 1
    assert calc["total_damage"] == 35


@pytest.mark.parametrize("name", ["燃血", "折寿", "疾贷"])
def test_touzhi_derivatives_share_the_same_effect(name):
    assert DaoWenEngine.resolve(name, 7)["mana_gain"] == 7


def test_zaisheng_derivative_heals_4x():
    assert DaoWenEngine.resolve("舍力", 7)["target_heal"] == 28


def test_mothers_keep_exactly_one_channel():
    assert DaoWenEngine.resolve("杀伐", 2)["cost_type"] == "消耗"
    assert DaoWenEngine.resolve("透支", 2)["cost_mana_limit"] == 2
    assert DaoWenEngine.resolve("再生", 2)["cost"] == 2
    # 旧多途径入口不再是形参：传入 cost_channel 也不得改出第二条途径。
    for name in ("透支", "再生", "杀伐"):
        base = DaoWenEngine.resolve(name, 1)
        assert DaoWenEngine.resolve(name, 1, cost_channel="生命") == base


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
    assert list(m.dao_wen) == ["透支", "连击"]
    assert m.dao_wen["连击"].dao_wen.name == "连击"


def test_load_migration_renames_entity_daowen(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "t.db"), rng_seed=1)
    p = Entity("旧档人", "轮回者", blood_limit=60, current_hp=60, attack_count=1, attack_power=1)
    e.state.player = p
    p.dao_wen["血债"] = DaoWenInstance(
        DaoWen(name="连击", formula="", cost_type="生命", cost_formula="X", effect_formula=""),
        x_value=3)
    p.dao_wen["血债"].dao_wen.name = "血债"
    assert e._migrate_legacy_xuezhai_names() is True
    assert list(p.dao_wen) == ["透支", "连击"]
    assert p.dao_wen["连击"].dao_wen.name == "连击"
    assert p.dao_wen["连击"].x_value == 3
    assert e._migrate_legacy_xuezhai_names() is False


# ---------- 战斗层：单途径支付真实落账 ----------

def _setup_battle(tmp_path, daowen_name="连击"):
    e = GameEngine(db_path=str(tmp_path / "test.db"), rng_seed=4)
    e.execute_action("setup_attributes", {
        "name": "衍生手", "blood_points": 5, "speed_points": 8, "mana_points": 12})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    s = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("choose_discovered_relic", {"relic_name": s["result"]["relic_choices"][0]})
    while e.state.energy > 0:
        e.execute_action("pre_battle_action", {"sub_action": "修行", "tier": 1})
    started = begin_battle(e)
    assert started["success"], started
    p = e.state.player
    p.dao_wen[daowen_name] = DaoWenInstance(
        DaoWen(name=daowen_name, formula="", cost_type="消耗",
               cost_formula="X", effect_formula=""), x_value=0, x_free=True)
    m = Entity("靶怪", "怪物", blood_limit=80, current_hp=80,
               attack_count=1, attack_power=1)
    e.state.enemies[:] = [m]
    e.execute_action("round_start", {"relic_choices": {}})
    return e, p, m


def _cast(e, m, name, x):
    return e.execute_action("use_daowen", {
        "daowen_name": name, "x": x, "target": m.name, "dodge": False})


def test_battle_mana_channel_derivative(tmp_path):
    e, p, m = _setup_battle(tmp_path, "连珠")
    mana0, hp0 = p.current_mana, p.current_hp
    assert mana0 >= 4
    r = _cast(e, m, "连珠", 1)
    assert r["success"], r
    assert p.current_mana == mana0 - 4
    assert p.current_hp == hp0
    assert m.current_hp == 80 - 5


def test_battle_fake_shard_derivative_only_touches_fake(tmp_path):
    e, p, m = _setup_battle(tmp_path, "伪伐")
    e.state.shards, e.state.fake_shards = 10, 200
    r = _cast(e, m, "伪伐", 2)
    assert r["success"], r
    assert e.state.fake_shards == 200 - 80
    assert e.state.shards == 10
    assert m.current_hp == 80 - 10


def test_battle_real_shard_derivative_spends_fake_first(tmp_path):
    """【碎片】代价按通用口径：额度=真+假，实际扣除先假后真。"""
    e, p, m = _setup_battle(tmp_path, "碎伐")
    e.state.shards, e.state.fake_shards = 20, 40
    r = _cast(e, m, "碎伐", 2)
    assert r["success"], r
    assert e.state.fake_shards == 40 - 8
    assert e.state.shards == 20
    assert m.current_hp == 80 - 10


def test_battle_real_shard_derivative_falls_back_to_real(tmp_path):
    e, p, m = _setup_battle(tmp_path, "碎伐")
    e.state.shards, e.state.fake_shards = 20, 3
    r = _cast(e, m, "碎伐", 2)
    assert r["success"], r
    assert e.state.fake_shards == 0 and e.state.shards == 20 - 5
    assert m.current_hp == 80 - 10


def test_battle_shard_derivative_insufficient_rejected_cleanly(tmp_path):
    e, p, m = _setup_battle(tmp_path, "碎伐")
    e.state.shards, e.state.fake_shards = 7, 0
    r = _cast(e, m, "碎伐", 2)
    assert r["success"] is False
    assert e.state.shards == 7 and m.current_hp == 80


def test_battle_old_name_submission_rejected(tmp_path):
    e, p, m = _setup_battle(tmp_path)
    r = e.execute_action("use_daowen", {
        "daowen_name": "血债", "x": 1, "target": m.name, "dodge": False})
    assert r["success"] is False
    assert m.current_hp == 80
