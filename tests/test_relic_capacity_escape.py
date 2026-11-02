"""遗物发现/替换/放弃的脱困出口。

2026-11-01（重构1）：遗物已脱离死者之页，"满页"不再是遗物获得的障碍；
现在的额度是**启用槽**＝ceil(血限/30)，满了就以 enabled=False 入库，
不会出现"必须放弃奖励才能继续"的死局——本文件改为验证这一新口径。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine                                    # noqa: E402
from engine.models import Entity, Relic                              # noqa: E402
from tests.setup_support import finish_initial_daowen                # noqa: E402


def _engine_at_capacity():
    """构造「遗物额度已满 + 有强制遗物发现待选」的死局现场。"""
    e = GameEngine(db_path="/tmp/linji_tests/relic_escape.db", rng_seed=7)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8,
                      "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    first = setup["result"]["relic_choices"][0]
    e.execute_action("choose_discovered_relic", {"relic_name": first})

    # 九种道纹+一种遗物占满十张页，与血限无关。
    from tests.test_load_capacity import _grant
    e.state.player.dao_wen.clear()
    for i in range(9):
        _grant(e.state.player, f"占页{i}")
    e.state.player.blood_limit = 30
    assert e.state.player.relic_capacity == 1
    assert len({r.name for r in e.state.relics}) == 1

    # 造一次「必须选1件」的强制发现（局外阶段，故不豁免）
    e.state.phase = "pre_battle"
    pool = e.state.relics_pool
    fresh = [r.name for r in pool
             if r.name not in {x.name for x in e.state.relics}][:2]
    assert len(fresh) == 2, "测试前置：遗物池里至少要有 2 件未持有的"
    e.state.pending_relic_choices = list(fresh)
    e.state.pending_relic_source = "测试强制发现"
    return e, first, fresh


# ---------- 正常路径 ----------

def test_relic_discovery_never_hits_a_page_deadlock():
    """待选门禁仍在（必须先选），但选择本身永不被页数拒绝。

    血限30 → 1 个启用槽且已被 held 占用：新遗物照样入库，只是 enabled=False。
    """
    e, held, fresh = _engine_at_capacity()

    blocked = e.execute_action("battle_start", {})
    assert blocked["success"] is False
    assert "必须先从发现候选中选择1件遗物" in blocked["error"]

    pick = e.execute_action("choose_discovered_relic", {"relic_name": fresh[0]})
    assert pick["success"] is True, pick
    owned = {x.name for x in e.state.relics}
    assert owned == {held, fresh[0]}, "奖励必须真的到手，不再被页数吞掉"
    assert {r.name for r in e.state.relics if r.enabled} == {held}, "启用槽已满 → 新件停用入库"
    assert e.state.pending_relic_choices == []


def test_discard_clears_pending_and_lets_the_game_proceed():
    """脱困出口一：放弃本次发现——不取遗物，待选列表清空。"""
    e, _, fresh = _engine_at_capacity()
    owned_before = {r.name for r in e.state.relics}

    r = e.execute_action("discard_discovered_relic", {})

    assert r["success"] is True, r
    assert sorted(r["result"]["discarded_choices"]) == sorted(fresh)
    assert e.state.pending_relic_choices == [], "待选列表必须被清空"
    assert {x.name for x in e.state.relics} == owned_before, "放弃不得改动持有"

    # 门禁不再拦：battle_start 的报错换成别的（不再是那条强制选择）
    after = e.execute_action("battle_start", {})
    assert "必须先从发现候选中选择1件遗物" not in after.get("error", "")


def test_replace_swaps_one_for_one_without_raising_the_count():
    """脱困出口二：替换——总数不变，所以不受额度限制。"""
    e, held, fresh = _engine_at_capacity()

    r = e.execute_action("choose_discovered_relic",
                         {"relic_name": fresh[0], "replace": held})

    assert r["success"] is True, r
    assert r["result"]["replaced"] == held
    owned = {x.name for x in e.state.relics}
    assert fresh[0] in owned and held not in owned, owned
    assert len(owned) == 1, "替换后总种数仍应等于额度"
    assert e.state.pending_relic_choices == [], "选择完成后待选列表应清空"


def test_replace_does_not_consume_the_quota_check():
    """替换走的是 1 换 1，不应被额度拒绝——哪怕额度是 1。"""
    e, held, fresh = _engine_at_capacity()
    assert e.state.player.relic_capacity == 1
    assert e.execute_action("choose_discovered_relic",
                            {"relic_name": fresh[0],
                             "replace": held})["success"] is True


# ---------- 边界条件 ----------

def test_replace_still_works_when_there_is_spare_quota():
    """额度还有余量时也允许替换（它是通用能力，不只是脱困手段）。"""
    e, held, fresh = _engine_at_capacity()
    e.state.player.dao_wen.pop("占页0")  # 释放一页，不通过加血限扩容。
    r = e.execute_action("choose_discovered_relic",
                         {"relic_name": fresh[0], "replace": held})
    assert r["success"] is True
    assert {x.name for x in e.state.relics} == {fresh[0]}


def test_discard_without_pending_is_rejected():
    """没有待选发现时，放弃是无效动作。"""
    e, _, _ = _engine_at_capacity()
    e.state.pending_relic_choices = []
    r = e.execute_action("discard_discovered_relic", {})
    assert r["success"] is False
    assert "没有待选择的遗物发现" in r["error"]


# ---------- 错误输入 ----------

def test_replace_with_unowned_relic_is_rejected_and_changes_nothing():
    """replace 指向未持有的遗物：拒绝，且不得动任何持有物。"""
    e, held, fresh = _engine_at_capacity()
    before = {x.name for x in e.state.relics}

    r = e.execute_action("choose_discovered_relic",
                         {"relic_name": fresh[0], "replace": "根本不存在的遗物"})

    assert r["success"] is False
    assert "替换" in r["error"], r
    assert {x.name for x in e.state.relics} == before
    assert e.state.pending_relic_choices, "失败的选择不得清空待选列表"


def test_replace_target_must_be_a_relic_not_a_daowen_name():
    """错误输入：拿道纹名当 replace 目标同样被拒。"""
    e, _, fresh = _engine_at_capacity()
    r = e.execute_action("choose_discovered_relic",
                         {"relic_name": fresh[0], "replace": "杀伐"})
    assert r["success"] is False
    assert "替换" in r["error"]
