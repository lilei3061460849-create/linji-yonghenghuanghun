"""2026-10-27：十张死者之页，四类共用且与血限脱钩。"""
import pickle
import pytest
from engine.models import Entity, Relic, Consumable, DaoWen, DaoWenInstance
from engine.death_pages import bind_resources, can_accept, usage, growth_error
from tests.test_world_consumables import engine, pickup_world


def grant_dw(p, name):
    p.dao_wen[name] = DaoWenInstance(DaoWen(name, "", "", "", ""))


@pytest.mark.parametrize("blood", [0, 1, 29, 30, 300, 3000])
def test_everyone_defaults_to_ten_independent_of_blood(blood):
    p = Entity("P", "怪物", blood_limit=blood)
    assert p.load_capacity == 10
    p.relics.clear()  # 怪物默认自带痛苦的偏爱，清除后再测试空页。
    assert p.daowen_capacity == 10 and p.relic_capacity == 10
    grant_dw(p, "杀伐")
    p.relics.append(Relic("叠鳞甲", ""))
    p.consumables.append(Consumable("破障晶", "", 2, 2))
    p.artifacts.append("法器甲")
    assert p.pages_used == 4 and p.daowen_capacity == 7
    assert p.relic_capacity == 7
    p.blood_limit = 1
    assert p.pages_used == 4 and p.load_capacity == 10


def test_four_categories_share_pages_and_duplicates_do_not_charge_twice(tmp_path):
    e = engine(tmp_path); p = e.state.player
    for i in range(7): grant_dw(p, f"纹{i}")
    e.state.relics = [Relic("甲", "", enabled=False)]
    e.state.consumables = [Consumable("乙", "", 2, 2), Consumable("乙", "", 0, 2)]
    e.state.artifacts_owned = ["丙"]
    bind_resources(e.state)
    assert p.pages_used == 10
    assert can_accept(p, "消耗品", "乙") and not can_accept(p, "消耗品", "丁")
    assert not can_accept(p, "遗物", "乙")
    assert p.daowen_capacity == 7
    del p.dao_wen["纹0"]
    assert p.pages_used == 9 and can_accept(p, "消耗品", "丁")


@pytest.mark.parametrize("kind", ["道纹", "遗物", "法器", "消耗品"])
def test_transaction_rejects_eleventh_kind_without_charging_or_advancing_rng(tmp_path, monkeypatch, kind):
    e = engine(tmp_path); p = e.state.player
    for i in range(10): grant_dw(p, str(i))
    e.state.shards = 20
    rng = e.dice._rng.getstate()
    def fake_action(action, params):
        e.state.shards -= 10
        e.dice.auto_roll("page_failure", [1, 2, 3])
        if kind == "道纹": grant_dw(p, "新纹")
        if kind == "遗物": e.state.relics.append(Relic("新遗物", ""))
        if kind == "消耗品": e.state.consumables.append(Consumable("新物", "", 1, 1))
        if kind == "法器": e.state.artifacts_owned.append("新法器")
        return {"success": True}
    monkeypatch.setattr(e, "_dispatch_action", fake_action)
    result = e.execute_action("world_status", {})
    assert not result["success"] and "死者之页" in result["error"]
    assert p.pages_used == 10 and len(p.dao_wen) == 10
    assert e.state.shards == 20 and e.dice._rng.getstate() == rng
    assert not e.state.artifacts_owned and not e.state.relics and not e.state.consumables


def test_legacy_defaults_and_snapshot_preserve_pages(tmp_path):
    e = engine(tmp_path); p = e.state.player
    del p.death_page_count
    assert p.load_capacity == 10
    p.death_page_count = 12
    assert pickle.loads(pickle.dumps(p)).load_capacity == 12
    assert e._deserialize_entity_full(e._serialize_entity_full(p)).load_capacity == 12


def test_full_pages_leave_map_item_and_allow_same_type_pickup(tmp_path):
    from engine.world_consumables import collect_consumables
    e = engine(tmp_path)
    world, npc = pickup_world(e)
    for i in range(10): grant_dw(npc, str(i))
    world.actors[npc.runtime_id].x = 3
    assert collect_consumables(world, e.state) == []
    assert world.map.locations["consumable_1"]["active"]
    del npc.dao_wen["0"]
    assert len(collect_consumables(world, e.state)) == 1
    assert npc.pages_used == 10


def test_relics_do_not_have_separate_enabled_quota(tmp_path):
    e = engine(tmp_path); p = e.state.player
    p.blood_limit = 1
    e.state.relics = [Relic(str(i), "") for i in range(10)]
    bind_resources(e.state)
    assert e.state.set_active_relics([str(i) for i in range(10)])[0]
    assert all(r.enabled for r in e.state.relics)
    assert e.state.toggle_relic("0") == (True, False)
    assert p.pages_used == 10  # 停用不释放页。
    assert e.state.toggle_relic("0") == (True, True)


def test_legacy_overload_is_not_confiscated_but_cannot_grow(tmp_path):
    e = engine(tmp_path); p = e.state.player
    for i in range(12): grant_dw(p, str(i))
    before = usage(e.state)
    assert growth_error(e.state, before) is None
    grant_dw(p, "13")
    assert growth_error(e.state, before)
