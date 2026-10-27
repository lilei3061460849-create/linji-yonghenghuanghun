"""贯穿退出与破障晶双模式的交易/伤害边界。"""
import pytest
from engine.models import Consumable, StatusEffect, Relic
from engine.daowen import DaoWenEngine, ResonanceEngine, is_reserved_slot
from engine.gamedata import SHAFA_LOOP_DAOWEN
from tests.test_dingming_pill import ready


def setup():
    e, p, m, _ = ready()
    item = Consumable("破障晶", "", 2, 2)
    e.state.consumables = [item]
    return e, p, m, item


def test_shot_preserves_shield_and_can_use_both_modes():
    e, p, m, item = setup()
    m.shield = 100
    assert e.execute_action("consume_item", {"name": item.name, "mode": "发射", "target_ref": "enemy:0"})["success"]
    assert m.current_hp == 85 and m.shield == 100
    assert e.execute_action("consume_item", {"name": item.name, "mode": "破障"})["success"]
    e.combat._apply_hostile_damage(m, 9, source=p)
    assert m.current_hp == 76 and m.shield == 100
    assert item.current_uses == 0
    assert not e.execute_action("consume_item", {"name": item.name, "mode": "破障"})["success"]


def test_buff_refreshes_expires_and_does_not_ignore_reduction_or_cap():
    e, p, m, item = setup()
    for _ in range(2):
        assert e.execute_action("consume_item", {"name": item.name, "mode": "破障"})["success"]
    assert sum(s.remaining_rounds for s in p.status_effects if s.name == "贯穿") == 1
    m.shield = 100
    m.relics.append(Relic("叠鳞甲", ""))
    m.add_status(StatusEffect("固执", 1, value=1, source="定命丸"))
    e.combat._apply_hostile_damage(m, 10, source=p)
    assert m.current_hp == 99 and m.shield == 100
    e.combat.round_end()
    assert not p.has_status("贯穿")
    # round_end clears shield normally; reestablish it to test expiration.
    m.shield = 100
    e.combat._apply_hostile_damage(m, 10, source=p)
    assert m.current_hp == 99 and m.shield < 100


@pytest.mark.parametrize("params", [{}, {"mode": "x"}, {"mode": "发射"},
    {"mode": "发射", "target_ref": "player:0"}, {"mode": "破障", "target_ref": "enemy:0"}])
def test_invalid_requests_do_not_spend(params):
    e, p, m, item = setup()
    assert not e.execute_action("consume_item", {"name": item.name, **params})["success"]
    assert item.current_uses == 2 and not p.has_status("贯穿")


def test_outside_combat_or_dead_target_rejected():
    e, p, m, item = setup()
    m.is_alive = False
    assert not e.execute_action("consume_item", {"name": item.name, "mode": "发射", "target_ref": "enemy:0"})["success"]
    e.state.phase = "pre_battle"
    assert not e.execute_action("consume_item", {"name": item.name, "mode": "破障"})["success"]
    assert item.current_uses == 2


def test_retirement_and_legacy_name():
    e, p, m, item = setup()
    assert "贯穿" not in DaoWenEngine.list_all() and "贯穿" not in SHAFA_LOOP_DAOWEN
    with pytest.raises(ValueError):
        DaoWenEngine.resolve("贯穿", 1)
    assert any(is_reserved_slot(n) and "贯穿" in n
               for src, _, dst in ResonanceEngine.CLOSED_LOOPS["杀伐闭环"] for n in (src, dst))
    item.name = "穿甲弹"
    item.current_uses = 1
    e._migrate_legacy_daowen_status_order()
    assert item.name == "破障晶" and item.current_uses == 1
