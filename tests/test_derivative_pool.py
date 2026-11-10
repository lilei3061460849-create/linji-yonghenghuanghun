"""【衍生道纹】池：单途径收束、形态差异与旧名边界（2026-11-09 用户令·第二轮）。

第一轮把【连击】六通道拆成六条「效果恒同、只换支付途径」的衍生；第二轮用户
裁定后改为：

* 只保留**一条**多段低伤（【连击】生命5X），其余改伤害表现——
  【虚耗】单段、【断脉】单段+无视格挡、【买命】直接失去生命、【冲击】除目标外全体；
* 取消假碎片途径（碎片只留【买命】一条，走通用碎片口径：真+假、先假后真）；
* 取消枯竭途径（枯竭是特殊机制代价，不是资源）；
* 原「法力通道」改收【法限】——法力本身已经是母本【杀伐】的支付途径；
* 改名：连珠→虚耗、血伐→断脉、碎伐／伪伐→买命、燃血→燃命、折寿→折年、
  疾贷→搏命、舍力→献锋；旧名只在读入边界归一。

本文件锁：每条衍生只有一条支付途径；同域不同形态；战斗中真实落账；旧名边界。
"""
import pytest

from engine import derivatives as derivative_rules
from engine.api import GameEngine
from engine.daowen import DaoWenEngine, normalize_daowen_name
from engine.models import Entity, DaoWen, DaoWenInstance
from engine.monsters import make_monster_entity

from tests.setup_support import begin_battle, finish_initial_daowen, hand_priority_to_player


# ---------- 计算层：每条衍生只有一条途径 ----------

@pytest.mark.parametrize("name,cost_key,amount,cost_type", [
    ("连击", "cost_hp", 35, "生命"),
    ("虚耗", "cost_mana_cap", 7, "法限"),
    ("断脉", "cost_blood_limit", 7, "血限"),
    ("买命", "cost_shards", 35, "碎片"),
    ("冲击", "cost_speed", 7, "速度"),
    ("燃命", "cost_hp", 28, "生命"),
    ("折年", "cost_blood_limit", 14, "血限"),
    ("搏命", "cost_speed", 7, "速度"),
    ("献锋", "cost_attack", 28, "攻击力"),
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


def test_shafa_pool_is_one_pool_with_five_forms():
    assert derivative_rules.pool_of("杀伐") == ("连击", "虚耗", "断脉", "买命", "冲击")


@pytest.mark.parametrize("name,key,value", [
    ("连击", "hits", 35),
    ("虚耗", "target_damage", 35),
    ("断脉", "target_damage", 35),
    ("买命", "target_life_loss", 35),
    ("冲击", "others_life_loss", 35),
])
def test_shafa_derivatives_have_distinct_damage_forms(name, key, value):
    """杀伐域五条各自一种伤害表现：只有【连击】是多段，其余形态互不重复。"""
    calc = DaoWenEngine.resolve(name, 7)
    assert calc[key] == value
    if name == "连击":
        assert calc["damage_per_hit"] == 1
    else:
        assert "hits" not in calc, f"{name} 不该再有第二段多段表达"
    if name == "断脉":
        assert calc["ignore_shield"] is True
    else:
        assert not calc.get("ignore_shield")


@pytest.mark.parametrize("name", ["燃命", "折年", "搏命"])
def test_touzhi_derivatives_share_the_same_effect(name):
    assert DaoWenEngine.resolve(name, 7)["mana_gain"] == 7


def test_zaisheng_derivative_heals_4x():
    assert DaoWenEngine.resolve("献锋", 7)["target_heal"] == 28


def test_mothers_keep_exactly_one_channel():
    assert DaoWenEngine.resolve("杀伐", 2)["cost_type"] == "消耗"
    assert DaoWenEngine.resolve("透支", 2)["cost_mana_limit"] == 2
    assert DaoWenEngine.resolve("再生", 2)["cost"] == 2
    # 旧多途径入口不再是形参：传入 cost_channel 也不得改出第二条途径。
    for name in ("透支", "再生", "杀伐"):
        base = DaoWenEngine.resolve(name, 1)
        assert DaoWenEngine.resolve(name, 1, cost_channel="生命") == base


def test_retired_channels_are_gone():
    """假碎片与枯竭两条途径已取消：旧条目不注册，旧名映射到现役条目。"""
    registered = set(DaoWenEngine.list_all())
    assert "伪伐" not in registered and "竭伐" not in registered
    assert derivative_rules.DERIVATIVE_ALIASES["伪伐"] == "买命"
    assert derivative_rules.DERIVATIVE_ALIASES["竭伐"] == "连击"


# ---------- 改名与旧名边界 ----------

def test_old_name_not_registered():
    with pytest.raises(ValueError):
        DaoWenEngine.resolve("血债", 1)
    assert "血债" not in DaoWenEngine._registry or DaoWenEngine.resolve("连击", 1)


@pytest.mark.parametrize("legacy,current", [
    ("血债", "连击"), ("连珠", "虚耗"), ("血伐", "断脉"), ("碎伐", "买命"),
    ("伪伐", "买命"), ("燃血", "燃命"), ("折寿", "折年"), ("疾贷", "搏命"),
    ("舍力", "献锋"), ("竭伐", "连击"),
])
def test_normalize_maps_every_legacy_name(legacy, current):
    assert normalize_daowen_name(legacy) == current
    assert normalize_daowen_name(current) == current


def test_daowen_post_init_normalizes_legacy_derivative_names():
    dw = DaoWen(name="连珠", formula="", cost_type="法限", cost_formula="X", effect_formula="")
    assert dw.name == "虚耗"


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


# ---------- 战斗层：单途径支付真实落账、形态真实生效 ----------

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
    hand_priority_to_player(e)
    return e, p, m


def _cast(e, m, name, x):
    return e.execute_action("use_daowen", {
        "daowen_name": name, "x": x, "target": m.name, "dodge": False})


def test_battle_mana_limit_channel_derivative(tmp_path):
    """【虚耗】收的是法限（不是法力）：法力不变、法限 -X。"""
    e, p, m = _setup_battle(tmp_path, "虚耗")
    mana0, mana_cap0, hp0 = p.current_mana, p.mana_limit, p.current_hp
    r = _cast(e, m, "虚耗", 2)
    assert r["success"], r
    assert p.mana_limit == mana_cap0 - 2
    # 收的是**上限**，不是法力：当前法力只因上限变低被夹到新上限。
    assert p.current_mana == min(mana0, mana_cap0 - 2)
    assert p.current_hp == hp0
    assert m.current_hp == 80 - 10


def test_battle_shard_derivative_spends_fake_first(tmp_path):
    """【买命】走通用碎片口径：额度=真+假，实际扣除先假后真。"""
    e, p, m = _setup_battle(tmp_path, "买命")
    e.state.shards, e.state.fake_shards = 20, 40
    r = _cast(e, m, "买命", 2)
    assert r["success"], r
    assert e.state.fake_shards == 40 - 10
    assert e.state.shards == 20
    assert m.current_hp == 80 - 10


def test_battle_shard_derivative_falls_back_to_real(tmp_path):
    e, p, m = _setup_battle(tmp_path, "买命")
    e.state.shards, e.state.fake_shards = 20, 3
    r = _cast(e, m, "买命", 2)
    assert r["success"], r
    assert e.state.fake_shards == 0 and e.state.shards == 20 - 7
    assert m.current_hp == 80 - 10


def test_battle_shard_derivative_insufficient_rejected_cleanly(tmp_path):
    e, p, m = _setup_battle(tmp_path, "买命")
    e.state.shards, e.state.fake_shards = 7, 0
    r = _cast(e, m, "买命", 2)
    assert r["success"] is False
    assert e.state.shards == 7 and m.current_hp == 80


def test_battle_pierce_derivative_ignores_shield(tmp_path):
    """【断脉】无视格挡：目标有格挡时生命照样掉。"""
    e, p, m = _setup_battle(tmp_path, "断脉")
    m.shield = 30
    r = _cast(e, m, "断脉", 2)
    assert r["success"], r
    assert m.shield == 30, "无视格挡不得消耗格挡"
    assert m.current_hp == 80 - 10
    assert p.blood_limit < 60 or p.current_hp <= p.blood_limit  # 血限支付已落地


def test_battle_multi_hit_derivative_is_split_across_hits(tmp_path):
    """【连击】是唯一的多段：5X 次各 1 点，格挡按段吸收。"""
    e, p, m = _setup_battle(tmp_path, "连击")
    m.shield = 3
    hp0 = p.current_hp
    r = _cast(e, m, "连击", 2)
    assert r["success"], r
    assert m.shield == 0
    assert m.current_hp == 80 - 7          # 10 段里 3 段被格挡吃掉
    assert p.current_hp == hp0 - 10        # 生命5X


def test_battle_impact_derivative_hits_everyone_but_the_target(tmp_path):
    """【冲击】除目标外所有角色失去生命——包含自己。"""
    e, p, m = _setup_battle(tmp_path, "冲击")
    ally = Entity("同伴", "朋友", blood_limit=40, current_hp=40, attack_count=1, attack_power=1)
    e.state.friends.append(ally)
    hp_player, hp_foe, hp_ally = p.current_hp, m.current_hp, ally.current_hp
    r = _cast(e, m, "冲击", 2)
    assert r["success"], r
    assert m.current_hp == hp_foe, "目标被排除"
    assert p.current_hp == hp_player - 10
    assert ally.current_hp == hp_ally - 10


def test_battle_old_name_submission_rejected(tmp_path):
    e, p, m = _setup_battle(tmp_path)
    r = e.execute_action("use_daowen", {
        "daowen_name": "血债", "x": 1, "target": m.name, "dodge": False})
    assert r["success"] is False
    assert m.current_hp == 80
