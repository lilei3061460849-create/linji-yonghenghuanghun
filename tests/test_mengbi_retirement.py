"""蒙蔽道纹退出；强光探照灯继续提供两次伤害无效。"""
import pytest
from pathlib import Path
from engine.daowen import DaoWenEngine, ResonanceEngine, is_reserved_slot
from engine.gamedata import MONSTER_TRANSFORM_DAOWEN
from engine.models import Consumable, StatusEffect, DaoWen, DaoWenInstance
from engine.monsters import make_monster_entity, parse_monster_pool
from tests.test_pozhang_crystal import setup


def test_retired_registration_pool_and_resonance():
    assert "蒙蔽" not in DaoWenEngine.list_all()
    assert "蒙蔽" not in MONSTER_TRANSFORM_DAOWEN
    assert "蒙蔽" not in DaoWenEngine.TRANSFORMED_DAOWEN
    with pytest.raises(ValueError): DaoWenEngine.resolve("蒙蔽", 2)
    assert any(is_reserved_slot(n) and "蒙蔽" in n
               for src, _, dst in ResonanceEngine.CLOSED_LOOPS["怪物原始道纹"] for n in (src, dst))


def test_lamp_uses_durability_and_keeps_status_effect():
    e, p, m, _ = setup()
    e.state.consumables = [Consumable("强光探照灯", "", 2, 2)]
    for remaining in (1, 0):
        r = e.execute_action("consume_item", {"name": "强光探照灯", "target_ref": "enemy:0"})
        assert r["success"], r
        assert m.get_status_value("蒙蔽") >= 2
        assert e.state.consumables[0].current_uses == remaining
    assert not e.execute_action("consume_item", {"name": "强光探照灯", "target_ref": "enemy:0"})["success"]


def test_two_attacks_are_nullified_then_damage_returns():
    e, p, m, _ = setup()
    e.state.consumables = [Consumable("强光探照灯", "", 2, 2)]
    assert e.execute_action("consume_item", {"name": "强光探照灯", "target_ref": "enemy:0"})["success"]
    m.current_mana = 10
    for _ in range(2):
        e.combat.resolve_attack(m, p, dodge=False, parry=False, spell_choices={"before": {}, "after": {}})
        assert p.current_hp == 100
    e.combat.resolve_attack(m, p, dodge=False, parry=False, spell_choices={"before": {}, "after": {}})
    assert p.current_hp < 100


def test_old_panel_and_old_save_do_not_restore_daowen_but_keep_status():
    e, p, m, _ = setup()
    p.dao_wen["蒙蔽"] = DaoWenInstance(DaoWen("蒙蔽", "", "", "", ""))
    p.add_status(StatusEffect("蒙蔽", -1, value=2, source="强光探照灯"))
    restored = e._deserialize_entity_full(e._serialize_entity_full(p))
    assert "蒙蔽" not in restored.dao_wen and restored.has_status("蒙蔽")
    e._migrate_legacy_daowen_status_order()
    assert "蒙蔽" not in p.dao_wen and p.has_status("蒙蔽")
    old = make_monster_entity({"name": "旧怪", "blood_limit": 20, "dao_wen": {"蒙蔽": 3}})
    assert "蒙蔽" not in old.dao_wen
    pool = parse_monster_pool(Path(__file__).resolve().parents[1] / "副本索引.md")
    assert all("蒙蔽" not in d["dao_wen"] for group in pool.values() for d in group)
