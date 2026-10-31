"""罪孽都市 2026-10-31 重画：烁金／赦免／贿赂／交易（抢劫、勒索已删）。

规格（用户文案逐字）：
  烁金X：支付：法力X。使[目标]碎片减少10X（不足减到0，不记负债）。
  赦免X：支付：碎片2X。选择X件遗物，使其变为启用。
  贿赂X：支付：碎片10X。使[目标]X件已启用遗物变为未启用。
  交易X：支付：碎片15X。发现X件遗物，仅本场战斗有效。
闭环八边（保留抢劫/勒索即等于留着已删纹）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.gamedata import REGION_EXCLUSIVE_DAOWEN
from engine.models import DaoWen, DaoWenInstance, Entity, Relic
from engine.monsters import make_monster_entity, parse_monster_pool
from tests.setup_support import finish_initial_daowen

FOUR = ("烁金", "赦免", "贿赂", "交易")
RETIRED = ("抢劫", "勒索")


def _setup(mana=100, shards=0):
    engine = GameEngine(rng_seed=42)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 6, "mana_points": 8})
    finish_initial_daowen(engine)
    engine.state.current_region = "罪孽都市"
    engine.state.phase = "in_combat"
    engine.state.player.current_mana = mana
    engine.state.shards = shards
    engine.state.fake_shards = 0
    engine.state.relics = []  # 钉死：本文件断言与开局随机遗物无关
    return engine


def _grant(engine, names):
    for n in names:
        engine.state.player.dao_wen[n] = DaoWenInstance(
            DaoWen(name=n, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


def _add_monster(engine, name="靶怪", hp=100, shards=0, fake=0):
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
               attack_count=1, attack_power=5)
    m.shards = shards
    m.fake_shards = fake
    engine.state.enemies.append(m)
    return m


def _cast(engine, name, x, **extra):
    params = {"daowen_name": name, "x": x, "dodge": False,
              "blood_shadow": False, "trigger_spell_choices": {}}
    params.update(extra)
    return engine.execute_action("use_daowen", params)


# ==================== 注册表与闭环 ====================

def test_registry_prices_match_the_spec():
    """四纹价格与量：法力X / 碎片2X / 碎片10X / 碎片15X。"""
    DaoWenEngine.register_all()
    shuo = DaoWenEngine.resolve("烁金", 2, caster=None)
    assert shuo["cost_type"] == "消耗" and shuo["cost"] == 2
    assert shuo["shard_burn"] == 20
    she = DaoWenEngine.resolve("赦免", 2)
    assert she["cost_type"] == "碎片" and she["cost_real_shards"] == 4
    assert she["relic_enable"] == 2
    hui = DaoWenEngine.resolve("贿赂", 2)
    assert hui["cost_type"] == "碎片" and hui["cost_real_shards"] == 20
    assert hui["relic_disable"] == 2
    jiao = DaoWenEngine.resolve("交易", 2)
    assert jiao["cost_type"] == "碎片" and jiao["cost_real_shards"] == 30
    assert jiao["relic_discovery"] == 2
    assert "duration" not in jiao, "交易单次结算，不挂持续"


def test_retired_daowen_are_gone_from_registry_and_loops():
    DaoWenEngine.register_all()
    for name in RETIRED:
        assert name not in DaoWenEngine._registry
        with pytest.raises(ValueError):
            DaoWenEngine.resolve(name, 1)
        assert name not in REGION_EXCLUSIVE_DAOWEN["罪孽都市"]
        assert not any(name in (src, dst)
                       for loop in ResonanceEngine.CLOSED_LOOPS.values()
                       for src, _, dst in loop), name


def test_loop_is_eight_edges_in_the_given_order():
    loop = ResonanceEngine.CLOSED_LOOPS["罪孽都市闭环"]
    assert loop == [
        ("点金", "反转", "烁金"),
        ("烁金", "转换", "假钞"),
        ("假钞", "曲解", "交易"),
        ("交易", "转换", "赦免"),
        ("赦免", "反转", "贿赂"),
        ("贿赂", "曲解", "豪夺"),
        ("豪夺", "转换", "典当"),
        ("典当", "曲解", "点金"),
    ]


def test_reversal_edges_are_bidirectional_and_no_fork_left():
    """烁金是点金的翻转、贿赂与赦免互为翻转；两条都双向可查。"""
    fw = ResonanceEngine.find_transformations("点金", "反转")
    assert [(p["target_daowen"], p["direction"]) for p in fw] == [("烁金", "正向")]
    back = ResonanceEngine.find_transformations("烁金", "反转")
    assert [(p["target_daowen"], p["direction"]) for p in back] == [("点金", "反向")]
    assert [(p["target_daowen"], p["direction"])
            for p in ResonanceEngine.find_transformations("赦免", "反转")] == [("贿赂", "正向")]
    assert [(p["target_daowen"], p["direction"])
            for p in ResonanceEngine.find_transformations("贿赂", "反转")] == [("赦免", "反向")]
    # 点金的反转不再分叉到环外
    assert "罪孽都市分叉" not in ResonanceEngine.CLOSED_LOOPS


def test_exclusive_set_is_the_eight_loop_nodes():
    from engine.combat import CombatEngine
    current = set(REGION_EXCLUSIVE_DAOWEN["罪孽都市"])
    assert current == {"点金", "烁金", "假钞", "交易", "赦免", "贿赂", "豪夺", "典当"}
    assert current == set(CombatEngine.REGION_EXCLUSIVE_DAOWEN["罪孽都市"])
    assert "消灾" not in current and "清算" not in current


# ==================== 烁金 ====================

def test_shuojin_burns_ten_x_and_nobody_gains():
    engine = _setup()
    m = _add_monster(engine, shards=50)
    calc = DaoWenEngine.resolve("烁金", 2, target=m, caster=engine.state.player)
    r = engine.combat.apply_daowen_effect("烁金", calc, engine.state.player, m)
    assert m.shards == 30
    assert engine.state.shards == 0, "烁金销毁碎片，施法者不获得"
    assert r["effects"][-1] == {"type": "shard_burn", "target": m.name, "lost": 20}


def test_shuojin_shortfall_stops_at_zero_and_books_no_debt():
    """不足减到0：假碎片先烧，真碎片不扣负（勒索的记债已删）。"""
    engine = _setup()
    m = _add_monster(engine, shards=5, fake=8)
    calc = DaoWenEngine.resolve("烁金", 2, target=m, caster=engine.state.player)
    r = engine.combat.apply_daowen_effect("烁金", calc, engine.state.player, m)
    assert m.fake_shards == 0 and m.shards == 0
    assert r["effects"][-1]["lost"] == 13
    assert not hasattr(m, "_daowen_debt") or not getattr(m, "_daowen_debt")


def test_shuojin_player_cast_pays_mana_only():
    engine = _setup(mana=100)
    _grant(engine, ["烁金"])
    m = _add_monster(engine, shards=40)
    r = _cast(engine, "烁金", 1, target_ref="enemy:0")
    assert r["success"], r
    assert engine.state.player.current_mana == 99
    assert m.shards == 30


# ==================== 赦免 ====================

def test_shemian_enables_exactly_the_named_relics():
    engine = _setup()
    engine.state.relics = [Relic("甲", "", enabled=False), Relic("乙", "", enabled=False),
                           Relic("丙", "", enabled=True)]
    calc = DaoWenEngine.resolve("赦免", 2, relic_names=["甲", "乙"])
    r = engine.combat.apply_daowen_effect("赦免", calc, engine.state.player, engine.state.player)
    assert [x.enabled for x in engine.state.relics] == [True, True, True]
    assert r["effects"][-1] == {"type": "relic_enable",
                                "target": engine.state.player.name, "enabled": ["甲", "乙"]}


def test_shemian_pays_two_shards_per_x_and_needs_own_relics():
    engine = _setup(shards=100)
    _grant(engine, ["赦免"])
    engine.state.relics = [Relic("甲", "", enabled=False)]
    # 未持有的遗物不能赦免
    bad = _cast(engine, "赦免", 1, relic_names=["不存在"])
    assert not bad["success"] and "赦免" in bad["error"]
    # 数量必须等于X
    bad2 = _cast(engine, "赦免", 2, relic_names=["甲"])
    assert not bad2["success"] and "2" in bad2["error"]
    # 失败不消耗出手，仍可正常发动
    r = _cast(engine, "赦免", 1, relic_names=["甲"])
    assert r["success"], r
    assert engine.state.shards == 98
    assert engine.state.relics[0].enabled is True


# ==================== 贿赂 ====================

def test_huilu_disables_named_enabled_relics_of_the_target():
    engine = _setup(shards=100)
    _grant(engine, ["贿赂"])
    m = _add_monster(engine)
    m.relics = [Relic("甲", "", enabled=True), Relic("乙", "", enabled=True),
                Relic("丙", "", enabled=False)]
    # 必须显式提交目标
    no_target = _cast(engine, "贿赂", 1, relic_names=["乙"])
    assert not no_target["success"] and "target_ref" in no_target["error"]
    # 目标已停用的遗物不能再被停用
    bad = _cast(engine, "贿赂", 1, target_ref="enemy:0", relic_names=["丙"])
    assert not bad["success"]
    r = _cast(engine, "贿赂", 1, target_ref="enemy:0", relic_names=["甲"])
    assert r["success"], r
    assert engine.state.shards == 90, "贿赂X=1 支付10碎片"
    assert [x.enabled for x in m.relics] == [False, True, False]


# ==================== 交易 ====================

def test_jiaoyi_offers_x_rounds_and_tags_relics_battle_only():
    engine = _setup(shards=200)
    _grant(engine, ["交易"])
    r = _cast(engine, "交易", 2)
    assert r["success"], r
    assert engine.state.shards == 170, "交易X=2 支付30碎片"
    first = r["execution"]["trade_discovery"]
    assert len(first["choices"]) == 3 and first["remaining"] == 1
    assert engine.state.to_dict()["pending_relic_discovery_rounds"] == 1, "排队轮数必须进状态投影"
    assert engine.state.pending_relic_source == "交易"

    picked = engine.execute_action("choose_discovered_relic", {"relic_name": first["choices"][0]})
    assert picked["success"], picked
    gained = next(x for x in engine.state.relics if x.name == first["choices"][0])
    assert "本场" in gained.tags
    second = picked["result"]
    assert len(second["choices"]) == 3 and second["remaining"] == 0

    picked2 = engine.execute_action("choose_discovered_relic", {"relic_name": second["choices"][0]})
    assert picked2["success"], picked2
    assert "choices" not in picked2["result"], "X轮发完不再续发"
    assert engine.state.pending_relic_choices == []


def test_jiaoyi_relics_are_removed_at_battle_end():
    engine = _setup(shards=200)
    _grant(engine, ["交易"])
    r = _cast(engine, "交易", 1)
    assert r["success"], r
    choice = r["execution"]["trade_discovery"]["choices"][0]
    assert engine.execute_action("choose_discovered_relic", {"relic_name": choice})["success"]
    assert any(x.name == choice for x in engine.state.relics)
    removed = engine._clear_battle_only_relics()
    assert removed == [choice]
    assert not any(x.name == choice for x in engine.state.relics)


def test_jiaoyi_requires_full_shard_payment():
    engine = _setup(shards=10)
    _grant(engine, ["交易"])
    r = _cast(engine, "交易", 1)
    assert not r["success"], "碎片不足15不能透支发动"
    assert not engine.state.pending_relic_choices


# ==================== 怪物面板 ====================

def test_panels_drop_retired_daowen_and_legacy_saves_cannot_regrant():
    pool = parse_monster_pool("副本索引.md")["罪孽都市"]
    for entry in pool:
        for name in RETIRED:
            assert name not in entry["dao_wen"], (entry["name"], name)
    legacy = make_monster_entity({"name": "旧打手", "blood_limit": 54,
                                  "dao_wen": {"勒索": 2, "烁金": 1}})
    assert "勒索" not in legacy.dao_wen and "烁金" in legacy.dao_wen
