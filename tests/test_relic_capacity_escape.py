"""【承载上限】遗物额度撞「强制发现」时的脱困守护（2026-10-12 用户裁定③）。

死结原文：`engine/api.py` 的门禁规定「有待选遗物时不许做任何别的动作」，
而 `_action_choose_discovered_relic` 又规定「遗物额度已满不许选」。两条同时成立时
引擎无法推进，且没有任何动作能清空 `pending_relic_choices`——实测报错
`battle_start: 必须先从发现候选中选择1件遗物`（sim/build_learner 整局因此被判无效）。

用户裁定：「可以替换/放弃」。故这里固化两条脱困出口，缺一即红：
  1. `discard_discovered_relic`：放弃本次发现，清空待选列表，不取任何遗物；
  2. `choose_discovered_relic(replace=…)`：换掉一件已持有的，总种数不变故不受额度限制。

2026-10-22 改版：遗物持有不再受承载拦截（启用归类），超额选择直接成功、
新遗物先停用——死结从源头上不存在了；替换/放弃作为通用能力保留，本文件继续守护。
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

    # 血限30 → 遗物额度恰好 1 种，且已被开局那件占满
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

def test_deadlock_is_real_without_an_escape():
    """前置校验（2026-10-22 改版）：待选门禁仍在，但选择本身不再被承载拒绝——
    超额拿到的遗物持有但停用，死结从源头上解开。"""
    e, held, fresh = _engine_at_capacity()

    blocked = e.execute_action("battle_start", {})
    assert blocked["success"] is False
    assert "必须先从发现候选中选择1件遗物" in blocked["error"]

    pick = e.execute_action("choose_discovered_relic", {"relic_name": fresh[0]})
    assert pick["success"] is True, pick
    assert pick["result"]["enabled"] is False, "生效数已满，新遗物应先停用"
    assert {x.name for x in e.state.relics} == {held, fresh[0]}, "持有不限：两件都在"
    assert e.state.enabled_relic_names() == {held}, "只有原先那件在生效"


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
    e.state.player.blood_limit = 90          # 额度 3，尚有余量
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
    assert "无法用它替换" in r["error"], r
    assert {x.name for x in e.state.relics} == before
    assert e.state.pending_relic_choices, "失败的选择不得清空待选列表"


def test_replace_target_must_be_a_relic_not_a_daowen_name():
    """错误输入：拿道纹名当 replace 目标同样被拒。"""
    e, _, fresh = _engine_at_capacity()
    r = e.execute_action("choose_discovered_relic",
                         {"relic_name": fresh[0], "replace": "杀伐"})
    assert r["success"] is False
    assert "无法用它替换" in r["error"]
