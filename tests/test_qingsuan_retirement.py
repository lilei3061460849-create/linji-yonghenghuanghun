"""清算及扣盾账本彻底退出，不替换成消耗品；灾厄轮盘保留。"""
import pytest
from pathlib import Path
from engine.daowen import DaoWenEngine, ResonanceEngine, is_reserved_slot
from engine.gamedata import REGION_EXCLUSIVE_DAOWEN
from engine.mechanisms import MECHANISMS
from engine.mechanisms.ledger import ledger_names, ledger_of
from engine.models import DaoWen, DaoWenInstance, StatusEffect
from engine.monsters import make_monster_entity, parse_monster_pool
from tests.test_world_consumables import engine
from tests.test_zui_ledger_migration import _arena


def test_registry_mechanisms_ledger_and_slot_retired():
    assert "清算" not in DaoWenEngine.list_all()
    assert "清算" not in REGION_EXCLUSIVE_DAOWEN["罪孽都市"]
    assert MECHANISMS.get("清算·结算") is None
    assert MECHANISMS.get("清算·对账") is None
    assert ledger_names() == []
    assert MECHANISMS.get("灾厄轮盘·结算") is not None
    with pytest.raises(ValueError): DaoWenEngine.resolve("清算", 2)
    loop = ResonanceEngine.CLOSED_LOOPS["罪孽都市闭环"]
    assert any(is_reserved_slot(n) and "清算" in n for src, _, dst in loop for n in (src, dst))


def test_even_unmigrated_legacy_status_and_ledger_cannot_drain_shield():
    state, c, p, m = _arena()
    state.shards = 999
    m.shield = 50
    m._qingsuan = [{"x": 3, "caster": p}]
    m.add_status(StatusEffect("清算", 3, value=3, source=p.name))
    for _ in range(2):
        result = c.round_start()
        assert m.shield == 50
        assert not any(e.get("type") == "qingsuan" for e in result["effects"])
    with pytest.raises(ValueError): ledger_of(m, "清算")


def test_save_migration_removes_daowen_status_and_residual_account(tmp_path):
    e = engine(tmp_path); p = e.state.player
    p.dao_wen["清算"] = DaoWenInstance(DaoWen("清算", "", "", "", ""))
    p.add_status(StatusEffect("清算", 3, value=3))
    p._qingsuan = [{"x": 3, "caster": p}]
    restored = e._deserialize_entity_full(e._serialize_entity_full(p))
    assert "清算" not in restored.dao_wen and not restored.has_status("清算")
    assert not hasattr(restored, "_qingsuan")
    e._migrate_legacy_daowen_status_order()
    assert "清算" not in p.dao_wen and not p.has_status("清算")
    assert not hasattr(p, "_qingsuan")


def test_monster_panels_and_legacy_constructor_cannot_regrant():
    pool = parse_monster_pool(Path(__file__).resolve().parents[1] / "副本索引.md")
    for name in ("军火商", "狙击手"):
        m = make_monster_entity(next(d for d in pool["罪孽都市"] if d["name"] == name))
        assert "清算" not in m.dao_wen
    m = make_monster_entity({"name": "旧狙击手", "blood_limit": 18, "dao_wen": {"清算": 2}})
    assert "清算" not in m.dao_wen


def test_no_replacement_item_created(tmp_path):
    from engine.api import TWISTED_TOOL_LIBRARY
    from engine.events import EVENT_CONSUMABLES
    assert "蚀盾符" not in TWISTED_TOOL_LIBRARY and "蚀盾符" not in EVENT_CONSUMABLES
