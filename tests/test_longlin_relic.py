"""龙鳞遗物：固定逐笔减伤，克制任意数量的1伤多段衍生（【连击】）。

2026-11-09 第二轮改造后，杀伐衍生只剩【连击】是多段低伤；【虚耗】【断脉】改单段、
【买命】直接失去生命、【冲击】是群体裸扣，都不再被「逐笔减伤」吃掉。
"""
import pytest
from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine, ResonanceEngine, is_reserved_slot
from engine.dice import DiceEngine
from engine.gamedata import REGION_EXCLUSIVE_DAOWEN
from engine.models import Entity, GameState, Relic, StatusEffect
from engine.monsters import make_monster_entity, parse_monster_pool
from tests.test_daowen_wiring import _engine, _give, _monster
from pathlib import Path


def arena():
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    p = Entity("受测者", "轮回者", blood_limit=100, current_hp=100)
    m = Entity("敌人", "怪物", blood_limit=100, current_hp=100)
    state.player = p
    state.enemies = [m]
    state.relics = [Relic("叠鳞甲", "")]
    return state, CombatEngine(state, DiceEngine()), p, m


@pytest.mark.parametrize("kind", ["普通", "无视格挡"])
@pytest.mark.parametrize("amount,loss", [(0, 0), (1, 0), (2, 1), (10, 9)])
def test_fixed_reduction_per_damage(kind, amount, loss):
    state, combat, p, m = arena()
    combat._apply_hostile_damage(p, amount, damage_type=kind, source=m)
    assert p.current_hp == 100 - loss


def test_repeated_one_damage_never_accumulates_into_hp_loss():
    state, combat, p, m = arena()
    for _ in range(200):
        combat._apply_hostile_damage(p, 1, source=m)
    assert p.current_hp == 100


def test_real_lianji_is_blocked_per_hit():
    """【连击】生命2X 换 X 段各 1 点：逐笔减伤把 25 段全吃掉，目标毫发无损。"""
    e = _engine("longlin_lianji")
    p = e.state.player
    _give(p, "连击")
    target = _monster(e, "鳞甲靶", hp=100)
    target.relics.append(Relic("叠鳞甲", ""))
    hp0 = p.current_hp
    r = e.execute_action("use_daowen", {"daowen_name": "连击", "x": 25,
        "target": target.name, "dodge": False, "blood_shadow": False})
    assert r["success"], r
    assert target.current_hp == 100
    assert p.current_hp == hp0 - 50  # 【连击】生命2X＝50


def test_single_hit_derivative_is_not_blocked_into_zero():
    """同一条遗物挡不住单段大额：【虚耗】照常打出，只被减 1 点。"""
    e = _engine("longlin_big_hit")
    p = e.state.player
    _give(p, "虚耗")
    target = _monster(e, "鳞甲靶", hp=100)
    target.relics.append(Relic("叠鳞甲", ""))
    mana_cap0 = p.mana_limit
    r = e.execute_action("use_daowen", {"daowen_name": "虚耗", "x": 1,
        "target": target.name, "dodge": False, "blood_shadow": False})
    assert r["success"], r
    assert target.current_hp == 100 - 11   # 12 点整笔，龙鳞减 1
    assert p.mana_limit == mana_cap0 - 1   # 法限X


def test_enabled_and_sealed_relic_rules_are_respected():
    state, combat, p, m = arena()
    state.relics[0].enabled = False
    combat._apply_hostile_damage(p, 2, source=m)
    assert p.current_hp == 98
    state.relics[0].enabled = True
    state.sealed_relics["叠鳞甲"] = 1
    combat._apply_hostile_damage(p, 2, source=m)
    assert p.current_hp == 96


def test_old_status_does_not_grant_extra_or_stackable_reduction():
    state, combat, p, m = arena()
    p.add_status(StatusEffect("龙鳞", -1, value=99))
    combat._apply_hostile_damage(p, 10, source=m)
    assert p.current_hp == 91


def test_does_not_discount_bleeding_cost_or_make_guzhi_total_immunity():
    state, combat, p, m = arena()
    combat._apply_numeric_cost_part(p, "流血", 2)
    assert p.current_hp == 98
    p.add_status(StatusEffect("固执", 2, value=1, daowen_order=0))
    combat._apply_hostile_damage(p, 100, source=m)
    assert p.current_hp == 97


def test_retired_daowen_and_resonance_slot():
    assert "龙鳞" not in DaoWenEngine.list_all()
    assert "龙鳞" not in REGION_EXCLUSIVE_DAOWEN["龙心谷"]
    with pytest.raises(ValueError, match="未知道纹"):
        DaoWenEngine.resolve("龙鳞", 1)
    loop = ResonanceEngine.edges_of_region("龙心谷")
    assert not any("龙鳞" == n for src, _, dst in loop for n in (src, dst))
    assert all("龙鳞" not in n for src, _, dst in loop for n in (src, dst))


def test_panel_retypes_longlin_as_relic_and_retains_other_daowen():
    pool = parse_monster_pool(Path(__file__).resolve().parents[1] / "副本索引.md")
    bear = next(d for d in pool["龙心谷"] if d["name"] == "石背熊")
    entity = make_monster_entity(bear)
    assert "龙鳞" not in entity.dao_wen
    assert "庇护" in entity.dao_wen
    assert any(r.name == "叠鳞甲" for r in entity.relics)


def test_discovery_pool_contains_relic_not_daowen():
    e = _engine("longlin_discovery")
    assert any(r.name == "叠鳞甲" for r in e.state.relics_pool)
    assert "龙鳞" not in DaoWenEngine.list_all()
