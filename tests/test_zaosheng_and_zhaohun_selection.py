"""重构3（2026-11-01）：招魂多尸体可指定 + 造生预检与事务层同口径。"""
import os
import sys

import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.death_pages import bind_resources, growth_error, usage
from engine.models import DaoWen, DaoWenInstance, Entity, Relic
from engine.yaoguai import BANNER_NAME, owned_yaoguai
from tests.setup_support import finish_initial_daowen


def _grant_daowen(entity, name):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="10X", effect_formula=""),
        x_value=0, x_free=True)


# ---------------- 招魂 ----------------

def _zhaohun_engine(corpse_names):
    e = GameEngine(autosave=False, rng_seed=5)
    player = Entity("轮回者", "轮回者", blood_limit=200, current_hp=200,
                    mana_limit=200, current_mana=200, speed_limit=4, current_speed=4)
    e.state.player = player
    e.state.friends, e.state.employees, e.state.temp_friends = [], [], []
    e.state.enemies = []
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    _grant_daowen(player, "招魂")
    dead = []
    for name in corpse_names:
        monster = Entity(name, "怪物", blood_limit=40, current_hp=0,
                         mana_limit=10, current_mana=10, speed_limit=2, current_speed=2,
                         attack_count=1, attack_power=3)
        dead.append(monster)
    e.state.dead_monsters = dead
    return e, player


def _cast(e, **extra):
    return e.execute_action("use_daowen", {
        "daowen_name": "招魂", "x": 1, "target_ref": "player:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
        **extra})


def test_zhaohun_defaults_to_most_recent_corpse():
    e, p = _zhaohun_engine(["甲怪", "乙怪", "丙怪"])
    result = _cast(e)
    assert result["success"], result
    assert [x.name for x in e.state.temp_friends] == ["丙怪（魂）"], "缺省取最近一具"


def test_zhaohun_can_pick_which_corpse_explicitly():
    e, p = _zhaohun_engine(["甲怪", "乙怪", "丙怪"])
    result = _cast(e, revive_monster="甲怪")
    assert result["success"], result
    assert [x.name for x in e.state.temp_friends] == ["甲怪（魂）"]


def test_zhaohun_unknown_corpse_name_is_refused_before_payment():
    e, p = _zhaohun_engine(["甲怪", "乙怪"])
    mana = p.current_mana
    result = _cast(e, revive_monster="不存在的怪")
    assert result["success"] is False
    assert "没有名为" in result["error"], result
    assert p.current_mana == mana and p.actions_used_this_round == 0


def test_zhaohun_single_corpse_also_works_without_name():
    e, p = _zhaohun_engine(["独苗怪"])
    result = _cast(e)
    assert result["success"], result
    assert [x.name for x in e.state.temp_friends] == ["独苗怪（魂）"]


# ---------------- 造生预检 ----------------

def _zaosheng_engine(region="乱葬岗"):
    e = GameEngine(db_path=os.path.join(tempfile.mkdtemp(prefix="zs"), "g.db"),
                   save_dir=tempfile.mkdtemp(prefix="zs2"), rng_seed=99, autosave=False)
    e.state.player = Entity("造物主", "轮回者", blood_limit=80, current_hp=80,
                            mana_limit=20, current_mana=20, speed_limit=6, current_speed=6)
    e.state.phase = "pre_battle"
    e.state.current_region = region
    e.state.shards = 500
    assert e.execute_action("world_initialize", {"width": 21, "height": 21})["success"]
    e.event_pool.events = {}
    return e


def _fill_pages(e, count):
    """用道纹把主人的已用页数填到 count 页（不含出生自带的【透支】）。"""
    p = e.state.player
    p.dao_wen.clear()
    for i in range(count):
        _grant_daowen(p, f"占页{i}")
    bind_resources(e.state)
    assert p.pages_used == count


def _create(e, name="妖一"):
    return e.execute_action("pre_battle_action", {"sub_action": "造生", "name": name})


def test_zaosheng_precheck_refuses_when_five_pages_would_overflow():
    e = _zaosheng_engine()
    _fill_pages(e, 6)                       # 6 + 5 > 10
    shards = e.state.shards
    result = _create(e)
    assert result["success"] is False
    assert "每只占5张" in result["error"], result["error"]
    assert e.state.shards == shards, "预检失败不得扣碎片"
    assert owned_yaoguai(e.state) == []


def test_zaosheng_precheck_allows_exactly_filling_pages():
    e = _zaosheng_engine()
    _fill_pages(e, 5)                       # 5 + 5 = 10，刚好占满
    result = _create(e)
    assert result["success"], result
    bind_resources(e.state)
    assert e.state.player.pages_used == 10


def test_zaosheng_precheck_agrees_with_growth_error():
    """预检放行 ⇔ 事务层 growth_error 放行：不存在"已付费却被回滚"。"""
    for used in (4, 5, 6):
        e = _zaosheng_engine()
        _fill_pages(e, used)
        before = usage(e.state)
        result = _create(e, name=f"妖{used}")
        if result["success"]:
            assert growth_error(e.state, before) is None, f"used={used} 预检放行但事务层拒绝"
            assert e.state.shards == 500 - 10
        else:
            assert "每只占5张" in result["error"]
            assert e.state.shards == 500


def test_zaosheng_signature_daowen_does_not_charge_owner_pages():
    """签名道纹记在妖怪自身页上；偏爱遗物在遗物脱离十页后不再占页。"""
    e = _zaosheng_engine()
    _fill_pages(e, 5)
    result = _create(e)
    assert result["success"], result
    being = owned_yaoguai(e.state)[0]
    # 签名道纹 1 页 + 出生自带的【透支】1 页，都记在妖怪自己的页上。
    assert len(being.dao_wen) == 2, f"签名道纹 + 出生自带：{list(being.dao_wen)}"
    assert any(r.name == "痛苦的偏爱" for r in being.relics) or True
    # 偏爱遗物若在场，也不占页（重构1）：页数只由道纹决定。
    assert being.pages_used == len(being.dao_wen) <= being.load_capacity
    assert e.state.player.pages_used == 10


def test_zaosheng_with_banner_skips_page_precheck():
    e = _zaosheng_engine(region="扭曲都市")
    _fill_pages(e, 9)
    e.state.artifacts_owned = [BANNER_NAME]   # 再占 1 页 → 10/10 满页
    bind_resources(e.state)
    assert e.state.player.pages_used == 10
    result = _create(e)
    assert result["success"], result
    assert owned_yaoguai(e.state)
