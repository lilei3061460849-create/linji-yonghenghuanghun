"""自定义施法的前置道纹与入口回归测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from tests.setup_support import finish_initial_daowen, grant_extra_actions


def _engine(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "t.db"), rng_seed=7,
                   sealed_candidate_path=str(tmp_path / "s.json"))
    e.execute_action("setup_attributes", {
        "name": "施法者", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    grant_extra_actions(e.state.player, 7)  # 阶数＝重数后，2 阶法术要 2 次出手
    return e


def test_predefined_spell_rejects_missing_daowen(tmp_path):
    e = _engine(tmp_path)
    before = ([s.name for s in e.state.player.spells], e.state.player.actions_used_this_round)
    result = e.execute_action("define_spell", {"spell_name": "借力打力"})
    assert not result["success"]
    assert "庇护" in result["error"]
    assert ([s.name for s in e.state.player.spells], e.state.player.actions_used_this_round) == before


def test_predefined_spell_requires_all_daowen_and_is_written_to_spells(tmp_path):
    e = _engine(tmp_path)
    p = e.state.player
    from engine.models import DaoWen, DaoWenInstance
    p.dao_wen["庇护"] = DaoWenInstance(DaoWen("庇护", "", "消耗", "X", ""), x_value=1)
    result = e.execute_action("define_spell", {"spell_name": "借力打力"})
    assert result["success"], result
    assert [s.name for s in p.spells] == ["借力打力"]
    assert p.armed_spells == []


def test_undefine_removes_definition_without_action_cost(tmp_path):
    e = _engine(tmp_path)
    p = e.state.player
    used = p.actions_used_this_round
    assert e.execute_action("define_spell", {"spell_name": "先发制人"})["success"]
    assert e.execute_action("undefine_spell", {"spell_name": "先发制人"})["success"]
    assert p.spells == []
    assert p.actions_used_this_round == used + 2, "先发制人（受到伤害前＋杀伐）是 2 阶"
    assert not e.execute_action("undefine_spell", {"spell_name": "先发制人"})["success"]


def test_learning_spell_sub_is_not_an_action(tmp_path):
    e = _engine(tmp_path)
    e.state.phase = "pre_battle"
    result = e.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "spell", "tier": 1, "names": ["借力打力"]})
    assert not result["success"]
    assert "学习sub必须是daowen" in result["error"]
