"""龙鳞遗物：固定逐笔减伤，克制任意数量的1伤连击。"""
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
    e = _engine("longlin_lianji")
    p = e.state.player
    _give(p, "连击")
    target = _monster(e, "鳞甲靶", hp=100)
    target.relics.append(Relic("叠鳞甲", ""))
    r = e.execute_action("use_daowen", {"daowen_name": "连击", "x": 10,
        "lianji_cost": "法力", "target": target.name, "dodge": False, "blood_shadow": False})
    assert r["success"], r
    assert target.current_hp == 100
    assert p.current_mana == 30


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
    loop = ResonanceEngine.CLOSED_LOOPS["龙心谷闭环"]
    assert not any("龙鳞" == n for src, _, dst in loop for n in (src, dst))
    assert any(is_reserved_slot(n) and "龙鳞" in n for src, _, dst in loop for n in (src, dst))


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
