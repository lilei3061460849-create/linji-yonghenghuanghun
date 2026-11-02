"""死者之页承载：道纹／法器／消耗品共十页；遗物自 2026-11-01 起脱离页数体系。

保留真实学习/发现/残韵路由守护。遗物现在的承载口径是"已启用件数
≤ ceil(血限/30)"，专测见 tests/test_relic_enabled_limit.py。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Consumable, Entity, Relic
from tests.setup_support import finish_initial_daowen


def _grant(entity, name):
    from engine.models import DaoWen, DaoWenInstance
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


def _fill_to(player, n):
    """把玩家道纹补到 n 种（多的删掉、少的用杀伐闭环里的通用道纹补上）。"""
    for name in list(player.dao_wen)[n:]:
        player.dao_wen.pop(name, None)
    for name in ("杀伐", "再生", "庇护", "束缚", "连击"):
        if len(player.dao_wen) >= n:
            break
        if name not in player.dao_wen:
            _grant(player, name)


def _engine():
    e = GameEngine(db_path=os.path.join(tempfile.mkdtemp(prefix="cap"), "g.db"),
                   rng_seed=7, save_dir=tempfile.mkdtemp(prefix="cap2"))
    e.execute_action("setup_attributes", {"name": "承载者", "blood_points": 7,
                                          "speed_points": 8, "mana_points": 10})
    finish_initial_daowen(e)
    # 局外行动（学习/共鸣）必须走完开局流程才可用
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "罪孽都市"})
    if setup.get("result", {}).get("relic_choices"):
        e.execute_action("choose_discovered_relic",
                         {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.energy = 5
    return e



def test_low_blood_does_not_limit_pages_but_does_limit_enabled_relics():
    p = Entity("T", "轮回者", blood_limit=1)
    p.dao_wen.clear()  # 2026-11-01：出生自带的【透支】另占一页，本用例测空页口径。
    assert p.load_capacity == p.daowen_capacity == 10
    # 重构1：遗物承载改指"可同时启用的件数"＝ceil(血限/30)
    assert p.relic_capacity == p.enabled_relic_limit == 1
    p.blood_limit = 900
    assert p.load_capacity == 10 and p.enabled_relic_limit == 30
    p.blood_limit = 31
    assert p.enabled_relic_limit == 2, "31~60 血限可启用 2 件"


def test_learn_daowen_over_capacity_is_rejected_without_spending():
    e = _engine(); p = e.state.player
    _fill_to(p, 1)
    # 页数由 法器 占满（遗物自 2026-11-01 起不再占页）
    e.state.artifacts_owned = [f"占页法器{i}" for i in range(9)]
    e.state.shards = 100
    before = (e.state.shards, e.state.energy, list(p.dao_wen))
    r = e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "弱化"})
    assert not r["success"] and "死者之页" in r["error"]
    assert (e.state.shards, e.state.energy, list(p.dao_wen)) == before


def full_page_engine():
    """页数占满（10页：1道纹 + 9法器），但一件遗物都没有。"""
    e = _engine()
    _fill_to(e.state.player, 1)
    e.state.artifacts_owned = [f"占页法器{i}" for i in range(9)]
    e.state.relics = []
    from engine.death_pages import bind_resources
    bind_resources(e.state)     # artifacts 的页账是快照，改完要重新绑定
    assert e.state.player.pages_used == e.state.player.load_capacity == 10
    e._init_relic_pool()
    return e


def test_discovered_relic_is_not_blocked_by_full_pages():
    """重构1：遗物不占页 → 满页也照常获得，奖励不再被吞。"""
    e = full_page_engine()
    name = e.state.relics_pool[0].name
    e.state.pending_relic_choices = [name]
    e.state.pending_relic_source = "测试"
    r = e.execute_action("choose_discovered_relic", {"relic_name": name})
    assert r["success"], r
    assert any(item.name == name for item in e.state.relics)
    assert not any(item.name == name for item in e.state.relics_pool)
    assert e.state.player.pages_used == 10, "遗物入库不涨页"


def test_paid_relic_acquisition_with_full_pages_only_pays_once():
    e = full_page_engine()
    e.state.shards = 100
    e.state.energy = 2
    r = e.execute_action("pre_battle_action", {"sub_action": "共鸣", "tier": 2,
                                              "name": e.state.relics_pool[0].name})
    assert r["success"], r
    assert len(e.state.relics) == 1 and e.state.player.pages_used == 10


def test_resonance_transform_keeps_holder_count_and_stays_within_capacity():
    """正常路径：残韵转化对**持有者**是一换一（种类数不变）；施法者另获转化后道纹。

    实测口径（`engine/api.py::_action_use_resonance`）：`_permanently_convert_daowen`
    把持有者的 source 换成 dest（1→1，不触容量），`_grant_transformed_daowen` 再给
    **施法者**补一份 dest（已持有则不重复）。所以"一换一"只对持有者成立；
    施法者那一份是真正的获得，会不会受承载上限约束属于口径问题（见下条用例）。
    """
    e = _engine()
    p = e.state.player
    p.blood_limit = 30                      # 额度 1 种
    p.dao_wen.clear()
    monster = Entity("M", "怪物", blood_limit=50, current_hp=50)
    monster.dao_wen.clear()  # 出生自带的【透支】不参与本用例的换纹计数
    _grant(monster, "全力")                 # 残韵转化的是**目标怪物**的道纹
    e.state.enemies = [monster]
    e.state.resonance["反转"] = 1
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"

    r = e.execute_action("use_resonance", {"source_daowen": "全力",
                                           "resonance_type": "反转",
                                           "target_ref": "enemy:0"})
    assert r["success"], r
    assert len(monster.dao_wen) == 1, "持有者一换一，种类数不变"
    # 2026-11-02 四面体三：全力 --反转--> 全速（弱化是强化／全速的邻居）。
    assert "全速" in monster.dao_wen and "全力" not in monster.dao_wen
    # 施法者另获一份【蒙蔽】；血限30 → 额度1，此时刚好等于额度，未超。
    assert list(p.dao_wen) == ["全速"], "施法者获得转化后道纹"
    assert len(p.dao_wen) <= p.daowen_capacity


def test_resonance_grant_beyond_capacity_is_refused():
    """裁定②（2026-10-12）：施法者已达额度时，残韵转化**不**把转化后道纹补给他。

    持有者的一换一照常发生（那是 1→1，不触容量）；只有"给施法者补一份"这个真正的
    获得受【承载上限】约束，超额就不授予（`_grant_transformed_daowen` 返回 False）。
    """
    e = _engine()
    p = e.state.player
    p.blood_limit = 30                      # 额度 1 种
    _fill_to(p, 1)
    e.state.artifacts_owned = [f"占页法器{i}" for i in range(9)]
    held = list(p.dao_wen)[0]
    monster = Entity("M", "怪物", blood_limit=50, current_hp=50)
    _grant(monster, "全力")
    e.state.enemies = [monster]
    e.state.resonance["反转"] = 1
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"

    r = e.execute_action("use_resonance", {"source_daowen": "全力",
                                           "resonance_type": "反转",
                                           "target_ref": "enemy:0"})
    assert r["success"], r
    assert not r.get("granted_daowen"), f"超额时不应授予：{r.get('granted_daowen')}"
    assert list(p.dao_wen) == [held], f"施法者道纹不得增加：{list(p.dao_wen)}"
    assert "全速" in monster.dao_wen and "全力" not in monster.dao_wen, "持有者的一换一照常"
