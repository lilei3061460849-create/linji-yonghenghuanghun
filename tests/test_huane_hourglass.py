"""裂变改遗物：伤害延期，不分段，不无限延期或重复减伤。"""
import pickle
import pytest
from pathlib import Path
from engine.models import Entity, Relic, StatusEffect
from engine.daowen import DaoWenEngine, ResonanceEngine, is_reserved_slot
from engine.gamedata import REGION_EXCLUSIVE_DAOWEN
from engine.monsters import parse_monster_pool, make_monster_entity
from tests.test_longlin_relic import arena


def ready():
    state, combat, p, m = arena()
    state.relics = [Relic("缓厄沙漏", "")]
    return state, combat, p, m


@pytest.mark.parametrize("amount,immediate,deferred", [(0, 0, 0), (1, 0, 1), (2, 1, 1), (7, 3, 4), (20, 10, 10)])
def test_half_moves_to_next_round_once(amount, immediate, deferred):
    state, c, p, m = ready()
    r = c._apply_hostile_damage(p, amount, source=m)
    assert p.current_hp == 100 - immediate
    assert sum(e["amount"] for e in p.deferred_damage) == deferred
    c.round_start()
    assert p.current_hp == 100 - amount
    assert not p.deferred_damage
    c.round_start()
    assert p.current_hp == 100 - amount


def test_shield_and_reduction_only_apply_once():
    state, c, p, m = ready()
    state.relics.append(Relic("叠鳞甲", ""))
    p.shield = 3
    c._apply_hostile_damage(p, 12, source=m)
    # 12 - 3盾 - 1减伤 = 8，当前4，下回合4。
    assert p.current_hp == 96 and p.shield == 0
    assert p.deferred_damage[0]["amount"] == 4
    p.shield = 50
    c.round_start()
    assert p.current_hp == 92 and p.shield == 50
    assert not p.deferred_damage


def test_first_cup_multiplier_is_not_reapplied():
    state, c, p, m = ready()
    state.relics.append(Relic("第一杯", ""))
    c._apply_hostile_damage(p, 10, source=m)
    assert p.current_hp == 90 and p.deferred_damage[0]["amount"] == 10
    c.round_start()
    assert p.current_hp == 80


def test_sealed_or_disabled_does_not_defer_new_damage_but_debt_persists():
    state, c, p, m = ready()
    c._apply_hostile_damage(p, 10, source=m)
    state.relics[0].enabled = False
    c._apply_hostile_damage(p, 10, source=m)
    assert p.current_hp == 85
    c.round_start()
    assert p.current_hp == 80 and not p.deferred_damage
    state.relics[0].enabled = True
    state.sealed_relics["缓厄沙漏"] = 1
    c._apply_hostile_damage(p, 10, source=m)
    assert p.current_hp == 70 and not p.deferred_damage


def test_each_hit_is_owed_separately_and_relic_removal_does_not_erase_it():
    state, c, p, m = ready()
    for amount in (3, 5, 7):
        c._apply_hostile_damage(p, amount, source=m)
    assert [e["amount"] for e in p.deferred_damage] == [2, 3, 4]
    state.relics.clear()
    c.round_start()
    assert p.current_hp == 85


def test_cost_is_not_damage_and_final_cap_is_kept():
    state, c, p, m = ready()
    c._apply_numeric_cost_part(p, "流血", 8)
    assert p.current_hp == 92 and not p.deferred_damage
    c._apply_hostile_damage(p, 10, source=m)
    p.add_status(StatusEffect("固执", 2, value=1))
    c.round_start()
    assert p.current_hp == 86  # 当下5 + 延期5由最终封顶压为1。


def test_pending_survives_pickle_and_character_snapshot_and_round_number_reset(tmp_path):
    from engine.api import GameEngine
    state, c, p, m = ready()
    state.current_round = 6
    c._apply_hostile_damage(p, 10, source=m)
    saved = pickle.loads(pickle.dumps(p))
    assert saved.deferred_damage == p.deferred_damage
    e = GameEngine(db_path=str(tmp_path / "hourglass.db"), autosave=False)
    restored = e._deserialize_entity_full(e._serialize_entity_full(p))
    assert restored.deferred_damage == p.deferred_damage
    state.current_round = 0  # 模拟战终后下一场战斗，不能靠重置回合数洗掉欠账。
    c.round_start()
    assert p.current_hp == 90 and not p.deferred_damage


def test_deferred_damage_can_kill_and_old_status_cannot_split_attacks():
    state, c, p, m = ready()
    c._apply_hostile_damage(p, 160, source=m)
    assert p.current_hp == 20
    c.round_start()
    assert p.current_hp == 0 and not p.is_alive
    assert not p.deferred_damage


def test_preview_does_not_treat_the_delayed_half_as_immediate_lethal_damage():
    state, c, p, m = ready()
    p.current_hp = 6
    r = c._apply_hostile_damage(p, 10, source=m)
    assert p.current_hp == 1 and p.is_alive
    assert p.deferred_damage[0]["amount"] == 5


def test_retires_daowen_and_retypes_monster_panels():
    assert "裂变" not in DaoWenEngine.list_all()
    assert "裂变" not in REGION_EXCLUSIVE_DAOWEN["龙心谷"]
    with pytest.raises(ValueError): DaoWenEngine.resolve("裂变", 2)
    loop = ResonanceEngine.CLOSED_LOOPS["龙心谷闭环"]
    assert any(is_reserved_slot(n) and "裂变" in n for src, _, dst in loop for n in (src, dst))
    pool = parse_monster_pool(Path(__file__).resolve().parents[1] / "副本索引.md")
    for name in ("碎岩鸮", "熔洞蛛"):
        monster = make_monster_entity(next(d for d in pool["龙心谷"] if d["name"] == name))
        assert "裂变" not in monster.dao_wen
        assert any(r.name == "缓厄沙漏" for r in monster.relics)
    legacy = make_monster_entity({"name": "旧鸮", "attack_count": 1, "attack_power": 2,
        "blood_limit": 30, "dao_wen": {"裂变": 2}})
    assert "裂变" not in legacy.dao_wen and any(r.name == "缓厄沙漏" for r in legacy.relics)
