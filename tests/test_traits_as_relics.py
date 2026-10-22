"""
血族/龙族遗物视图与初拥专属道纹的分类边界。

龙族项目与除【真理】外的初拥项目以遗物形式存放；dragon_traits /
first_embrace_traits 是对 relics 的只读视图（按 tags 过滤）。【真理】作为
初拥专属道纹存入角色道纹表，不混入遗物视图。

覆盖：正常路径 / 边界条件 / 错误输入
"""
import os
import sys

import pytest

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import GameState


def _engine(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "t.db"), rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    return e


# ---------- 正常路径 ----------

def test_dragon_item_is_stored_as_relic(tmp_path):
    """正常路径：解锁龙族项目后，它必须真的出现在 relics 里"""
    e = _engine(tmp_path)
    e.state.artifacts_owned.append("真龙之心")
    e.state.dragon_nature = 12
    r = e.execute_action("unlock_dragon_trait", {"trait": "龙息"})
    assert r["success"], r.get("error")
    names = [x.name for x in e.state.relics]
    assert "龙息" in names, f"龙息未作为遗物存入 relics：{names}"


def test_blood_item_is_stored_as_relic(tmp_path):
    """正常路径：初拥遗物项目必须真的出现在 relics 里"""
    e = _engine(tmp_path)
    e.state.pending_first_embrace = True
    r = e.execute_action("choose_first_embrace", {"choice": 7})  # 血影
    assert r["success"], r.get("error")
    assert "血影" in [x.name for x in e.state.relics]


def test_truth_is_stored_as_bloodline_daowen_not_relic(tmp_path):
    """选项5只授予专属道纹【真理】，不增加血族遗物名单。"""
    e = _engine(tmp_path)
    e.state.pending_first_embrace = True
    result = e.execute_action("choose_first_embrace", {"choice": 5})
    assert result["success"], result.get("error")
    assert "真理" in e.state.player.dao_wen
    assert "真理" not in [relic.name for relic in e.state.relics]
    assert "真理眼" not in [relic.name for relic in e.state.relics]
    assert "真理" not in e.state.first_embrace_traits


def test_views_reflect_relics(tmp_path):
    """正常路径：两个视图必须与 relics 保持一致（不是各存一份）"""
    g = GameState()
    g.grant_relic("龙威", "效果", tag="龙族")
    g.grant_relic("血食", "效果", tag="血族")
    g.grant_relic("守夜灯", "普通遗物")
    assert g.dragon_traits == ["龙威"]
    assert g.first_embrace_traits == ["血食"]
    assert len(g.relics) == 3


# ---------- 边界条件 ----------

def test_relic_rules_apply_destroy(tmp_path):
    """边界：作为遗物，必须可被销毁（遗物通用规则）"""
    g = GameState()
    g.grant_relic("龙息", "e", tag="龙族")
    assert g.remove_relic("龙息") is True
    assert g.dragon_traits == []
    assert g.relics == []


def test_tail_sacrifice_destroys_the_relic(tmp_path):
    """边界：断尾求生应销毁一件龙族遗物，relics 与视图同步减少"""
    e = _engine(tmp_path)
    e.state.artifacts_owned.append("真龙之心")
    e.state.dragon_nature = 24
    e.execute_action("unlock_dragon_trait", {"trait": "断尾求生"})
    e.execute_action("unlock_dragon_trait", {"trait": "龙息"})
    assert set(e.state.dragon_traits) == {"断尾求生", "龙息"}
    e.state.remove_relic("龙息")
    assert e.state.dragon_traits == ["断尾求生"]
    assert "龙息" not in [x.name for x in e.state.relics]


def test_removing_absent_relic_returns_false():
    """边界：移除不存在的遗物返回 False，不抛异常"""
    g = GameState()
    assert g.remove_relic("不存在") is False


# ---------- 错误输入 / 非法配置 ----------

def test_no_duplicate_relic_grant(tmp_path):
    """错误输入：同一龙族遗物不可重复获得"""
    e = _engine(tmp_path)
    e.state.artifacts_owned.append("真龙之心")
    e.state.dragon_nature = 24
    assert e.execute_action("unlock_dragon_trait", {"trait": "龙息"})["success"]
    r2 = e.execute_action("unlock_dragon_trait", {"trait": "龙息"})
    assert not r2["success"], "重复获得同一遗物必须被拒绝"


def test_unlock_requires_artifact(tmp_path):
    """错误输入：没有真龙之心时不得解锁龙族遗物"""
    e = _engine(tmp_path)
    e.state.dragon_nature = 99
    r = e.execute_action("unlock_dragon_trait", {"trait": "龙息"})
    assert not r["success"]


def test_no_parallel_trait_lists_remain():
    """
    非法配置检出：GameState 不得再有可写的独立名单字段。
    dragon_traits / first_embrace_traits 必须是 property（只读视图）。
    """
    assert isinstance(type(GameState()).dragon_traits, property)
    assert isinstance(type(GameState()).first_embrace_traits, property)
