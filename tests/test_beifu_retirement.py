"""背负道纹删除，不影响正文已载明的通用护卫指令。"""
import pytest
from pathlib import Path
from engine.daowen import DaoWenEngine, ResonanceEngine, is_reserved_slot
from engine.gamedata import REGION_EXCLUSIVE_DAOWEN
from engine.monsters import parse_monster_pool, make_monster_entity
from engine.models import DaoWen, DaoWenInstance
from tests.test_world_consumables import engine


def test_retirement_registration_learning_and_loop():
    assert "背负" not in DaoWenEngine.list_all()
    assert "背负" not in REGION_EXCLUSIVE_DAOWEN["龙心谷"]
    with pytest.raises(ValueError):
        DaoWenEngine.resolve("背负", 1)
    assert any(is_reserved_slot(n) and "背负" in n
               for src, _, dst in ResonanceEngine.CLOSED_LOOPS["龙心谷闭环"] for n in (src, dst))


def test_panel_and_legacy_configuration_do_not_grant_beifu():
    pool = parse_monster_pool(Path(__file__).resolve().parents[1] / "副本索引.md")
    for name in ("背碑人", "断翼巨像"):
        monster = make_monster_entity(next(d for d in pool["龙心谷"] if d["name"] == name))
        assert "背负" not in monster.dao_wen
    monster = make_monster_entity({"name": "旧背碑人", "blood_limit": 54,
                                  "dao_wen": {"背负": 3}})
    assert "背负" not in monster.dao_wen


def test_event_friend_can_guard_without_beifu_or_spending_a_page(tmp_path):
    e = engine(tmp_path)
    e.state.current_region = "龙心谷"
    e.event_pool.current = "断桥余烬"
    option = next(o for o in e.event_pool.events["断桥余烬"]["options"] if o["text"].startswith("接过伤者"))
    r = e.execute_action("resolve_event", {"event": "断桥余烬", "option_id": option["id"]})
    assert r["success"], r
    friend = next(f for f in e.state.friends if f.name == "岩行者")
    # 2026-11-01：出生自带【透支】占一页，其余道纹仍为空。
    assert set(friend.dao_wen) == {"透支"} and friend.pages_used == 1
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    r = e.execute_action("command_ally", {"ally_ref": "friend:0", "instruction": "护卫 3"})
    assert r["success"], r
    assert friend._beifu_left == 3 and friend.actions_used_this_round == 0
    # 护卫只是指令，不改页数：透支那一页照记，页数仍为 1。
    assert e.state.player.actions_used_this_round == 0 and friend.pages_used == 1


def test_old_daowen_cannot_be_used_even_if_manually_loaded(tmp_path):
    e = engine(tmp_path)
    p = e.state.player
    p.dao_wen["背负"] = DaoWenInstance(DaoWen("背负", "", "", "", ""))
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    r = e.execute_action("use_daowen", {"daowen_name": "背负", "x": 1, "target_ref": "player:0"})
    assert not r["success"]
    assert getattr(p, "_beifu_left", 0) == 0


def test_old_save_removes_beifu_daowen_but_preserves_guard_state(tmp_path):
    e = engine(tmp_path)
    p = e.state.player
    p.dao_wen["背负"] = DaoWenInstance(DaoWen("背负", "", "", "", ""))
    p._beifu_left = 2
    p._beifu_target = "another_actor"
    saved = e._serialize_entity_full(p)
    assert "背负" not in e._deserialize_entity_full(saved).dao_wen
    e._migrate_legacy_daowen_status_order()
    assert "背负" not in p.dao_wen
    assert p._beifu_left == 2 and p._beifu_target == "another_actor"
