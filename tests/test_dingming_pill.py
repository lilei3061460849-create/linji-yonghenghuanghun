"""定命丸：固执原效果，固定1回合，耐久2/2。"""
import pytest
from engine.daowen import DaoWenEngine, ResonanceEngine, is_reserved_slot
from engine.gamedata import SHAFA_LOOP_DAOWEN
from engine.models import Consumable, Relic
from tests.test_daowen_wiring import _engine, _monster


def ready():
    e = _engine("dingming_pill")
    p = e.state.player
    p.current_hp = p.blood_limit = 100
    item = Consumable("定命丸", "你每次失去的生命不会超过1，持续1回合", 2, 2)
    e.state.consumables = [item]
    m = _monster(e, "靶", hp=100)
    return e, p, m, item


def test_use_twice_and_reject_exhausted_item():
    e, p, m, item = ready()
    mana = p.current_mana
    for remaining in (1, 0):
        r = e.execute_action("consume_item", {"name": "定命丸"})
        assert r["success"], r
        assert item.current_uses == remaining
        assert p.has_status("固执")
        assert next(s for s in p.status_effects if s.name == "固执").remaining_rounds == 1
    assert p.current_mana == mana
    assert p.actions_used_this_round == 0
    assert not e.execute_action("consume_item", {"name": "定命丸"})["success"]
    assert item.current_uses == 0


def test_original_cap_includes_damage_bleeding_and_expires():
    e, p, m, item = ready()
    assert e.execute_action("consume_item", {"name": "定命丸"})["success"]
    e.combat._apply_hostile_damage(p, 50, source=m)
    assert p.current_hp == 99
    e.combat._apply_numeric_cost_part(p, "流血", 20)
    assert p.current_hp == 98
    e.combat.round_end()
    assert not p.has_status("固执")
    e.combat._apply_hostile_damage(p, 10, source=m)
    assert p.current_hp == 88


@pytest.mark.parametrize("phase,target", [("pre_battle", None), ("in_combat", "enemy:0")])
def test_illegal_use_does_not_spend_durability(phase, target):
    e, p, m, item = ready()
    e.state.phase = phase
    params = {"name": "定命丸"}
    if target:
        params["target_ref"] = target
    assert not e.execute_action("consume_item", params)["success"]
    assert item.current_uses == 2
    assert not p.has_status("固执")


def test_old_daowen_is_retired_and_slot_cannot_be_obtained():
    assert "固执" not in DaoWenEngine.list_all()
    assert "固执" not in SHAFA_LOOP_DAOWEN
    with pytest.raises(ValueError, match="未知道纹"):
        DaoWenEngine.resolve("固执", 1)
    assert any(is_reserved_slot(n) and "固执" in n
               for src, _, dst in ResonanceEngine.CLOSED_LOOPS["杀伐闭环"] for n in (src, dst))


def test_legacy_relic_name_migrates_without_changing_effect():
    e, p, m, item = ready()
    e.state.relics = [Relic("龙鳞", "旧名")]
    e._migrate_legacy_daowen_status_order()
    assert e.state.relics[0].name == "叠鳞甲"
    e.combat._apply_hostile_damage(p, 1, source=m)
    assert p.current_hp == 100
