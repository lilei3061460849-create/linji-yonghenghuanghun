"""【勒索】（2026-10-14 用户令：补点金反转分叉，还债新生产者）。

规格：勒索X：消耗3X。目标失去10X碎片，不足部分记负债（真碎片扣负）。单次结算。
与【赎金】同价同量形状不同：赎金偷取（施法者获得，不足不记负债），勒索销毁+记负债。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.gamedata import REGION_EXCLUSIVE_DAOWEN
from engine.models import DaoWen, DaoWenInstance, Entity
from engine.monsters import parse_monster_pool
from tests.setup_support import finish_initial_daowen


def _setup(mana=100):
    engine = GameEngine(rng_seed=42)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 6, "mana_points": 8})
    finish_initial_daowen(engine)
    engine.state.current_region = "罪孽都市"
    engine.state.phase = "in_combat"
    engine.state.player.current_mana = mana
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


def test_registry_numbers_mirror_shujin_price_and_scale():
    """注册：消耗3X法力，shard_debt=10X（与赎金同价同量）。"""
    DaoWenEngine.register_all()
    calc = DaoWenEngine.resolve("勒索", 2, caster=None)
    assert calc["cost_type"] == "消耗" and calc["cost"] == 6
    assert calc["shard_debt"] == 20


def test_dianjin_fanzhuan_edge_and_back():
    """点金反转分叉仍双向；勒索另在假钞—点金主环中拥有两条曲解边。"""
    fw = ResonanceEngine.find_transformations("点金", "反转")
    assert [(p["target_daowen"], p["direction"]) for p in fw] == [("勒索", "正向")]
    back = ResonanceEngine.find_transformations("勒索", "反转")
    assert [(p["target_daowen"], p["direction"]) for p in back] == [("点金", "反向")]
    main_ring = ResonanceEngine.find_transformations("勒索", "曲解")
    assert [(p["target_daowen"], p["direction"]) for p in main_ring] == [
        ("假钞", "反向"), ("点金", "正向")]
    assert len(ResonanceEngine.CLOSED_LOOPS["罪孽都市闭环"]) == 7, "分叉不进环表，现行环仍7边合拢"


def test_effect_full_pay_destroys_shards_nobody_gains():
    """足额：目标失去10X，碎片销毁谁都不获得（与赎金偷取相反）。"""
    engine = _setup()
    engine.state.shards = 0
    m = _add_monster(engine, shards=50)
    calc = DaoWenEngine.resolve("勒索", 2, target=m, caster=engine.state.player)
    r = engine.combat.apply_daowen_effect("勒索", calc, engine.state.player, m)
    assert m.shards == 30
    assert engine.state.shards == 0, "勒索销毁碎片，施法者不获得"
    assert r["effects"][-1]["type"] == "lesuo"


def test_effect_shortfall_books_debt_fake_first():
    """不足：假碎片优先，真碎片扣负记负债。"""
    engine = _setup()
    m = _add_monster(engine, shards=5, fake=8)
    calc = DaoWenEngine.resolve("勒索", 2, target=m, caster=engine.state.player)
    r = engine.combat.apply_daowen_effect("勒索", calc, engine.state.player, m)
    assert m.fake_shards == 0 and m.shards == -7
    last = r["effects"][-1]
    assert last["type"] == "lesuo_debt" and last["debt_now"] == 7 and last["obligation"] == 20


def test_player_cast_full_plumbing():
    """全链路：use_daowen 扣费3X，目标碎片扣负。"""
    engine = _setup(mana=100)
    _grant(engine, ["勒索"])
    m = _add_monster(engine, shards=0)
    r = engine.execute_action("use_daowen", {"daowen_name": "勒索", "x": 1,
                                             "target_ref": "enemy:0",
                                             "dodge": False, "blood_shadow": False,
                                             "trigger_spell_choices": {}})
    assert r["success"], r
    assert engine.state.player.current_mana == 97
    assert m.shards == -10


def test_debt_reaches_threshold_triggers_haizhai():
    """X=2 一次打满20负债 → 回终还债转员工。"""
    engine = _setup()
    _grant(engine, ["勒索"])
    m = _add_monster(engine, shards=0)
    r = engine.execute_action("use_daowen", {"daowen_name": "勒索", "x": 2,
                                             "target_ref": "enemy:0",
                                             "dodge": False, "blood_shadow": False,
                                             "trigger_spell_choices": {}})
    assert r["success"], r
    assert m.shards == -20
    engine.combat.settle_victory_paths()
    assert m.is_debt_bound and m.entity_type == "员工"
    assert m in engine.state.employees and m not in engine.state.enemies


def test_carriers_dashou_and_kanmenquan():
    """承载：打手/看门犬面板带勒索（用户 2026-10-14 定）。"""
    pool = parse_monster_pool("副本索引.md")["罪孽都市"]
    by_name = {d["name"]: d for d in pool}
    for carrier in ("打手", "看门犬"):
        assert "勒索" in by_name[carrier]["dao_wen"], carrier


def test_monster_casts_lesuo_debts_the_player():
    """怪物发动：扣玩家碎片可致负债（玩家无还债转化，只记账）。"""
    engine = _setup()
    engine.state.shards = 5
    engine.state.fake_shards = 0
    caster = _add_monster(engine, name="打手", hp=54)
    calc = DaoWenEngine.resolve("勒索", 1, target=engine.state.player, caster=caster)
    r = engine.combat.apply_daowen_effect("勒索", calc, caster, engine.state.player)
    assert engine.state.shards == -5
    assert r["effects"][-1]["type"] == "lesuo_debt"


def test_sin_city_exclusive_set_matches_current_registry():
    """两处名单同源；消灾、清算等退役项不得继续影响战始碎片计算。"""
    from engine.combat import CombatEngine
    current = set(REGION_EXCLUSIVE_DAOWEN["罪孽都市"])
    assert current == set(CombatEngine.REGION_EXCLUSIVE_DAOWEN["罪孽都市"])
    assert len(current) == 6
    assert "勒索" in current
    assert "消灾" not in current and "清算" not in current
