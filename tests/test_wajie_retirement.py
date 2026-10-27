"""瓦解删除，不再运行旧百分比削血限效果或等待重做。"""
import pytest
from pathlib import Path
from engine.daowen import DaoWenEngine, ResonanceEngine, is_reserved_slot
from engine.gamedata import REGION_EXCLUSIVE_DAOWEN
from engine.models import DaoWen, DaoWenInstance
from engine.monsters import parse_monster_pool, make_monster_entity
from tests.test_pozhang_crystal import setup


def test_registration_learning_and_loop_retired():
    assert "瓦解" not in DaoWenEngine.list_all()
    assert "瓦解" not in REGION_EXCLUSIVE_DAOWEN["乱葬岗"]
    with pytest.raises(ValueError): DaoWenEngine.resolve("瓦解", 3)
    assert any(is_reserved_slot(n) and "瓦解" in n
               for src, _, dst in ResonanceEngine.CLOSED_LOOPS["乱葬岗闭环"] for n in (src, dst))


def test_panels_and_legacy_constructor_do_not_grant():
    pool = parse_monster_pool(Path(__file__).resolve().parents[1] / "副本索引.md")
    for name in ("腐疫鼠", "勾魂使者"):
        m = make_monster_entity(next(d for d in pool["乱葬岗"] if d["name"] == name))
        assert "瓦解" not in m.dao_wen
    old = make_monster_entity({"name": "旧鼠", "blood_limit": 40, "dao_wen": {"瓦解": 3}})
    assert "瓦解" not in old.dao_wen


def test_legacy_use_rejected_without_damage_or_cost():
    e, p, m, _ = setup()
    p.dao_wen["瓦解"] = DaoWenInstance(DaoWen("瓦解", "", "", "", ""))
    before = (m.blood_limit, m.current_hp, p.current_mana, p.actions_used_this_round)
    r = e.execute_action("use_daowen", {"daowen_name": "瓦解", "x": 3, "target_ref": "enemy:0"})
    assert not r["success"]
    assert (m.blood_limit, m.current_hp, p.current_mana, p.actions_used_this_round) == before


def test_legacy_save_and_snapshot_do_not_restore():
    e, p, m, _ = setup()
    p.dao_wen["瓦解"] = DaoWenInstance(DaoWen("瓦解", "", "", "", ""))
    restored = e._deserialize_entity_full(e._serialize_entity_full(p))
    assert "瓦解" not in restored.dao_wen
    e._migrate_legacy_daowen_status_order()
    assert "瓦解" not in p.dao_wen
