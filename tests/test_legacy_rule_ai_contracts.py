"""当前实验规则 AI 契约与旧招架动作的兼容拒绝。"""
import pytest

from tests.test_ai_tactics import _engine


def test_legacy_declare_parry_action_is_not_offered_or_executable(tmp_path):
    """旧的回合姿态入口不再出现；误提交时由引擎明确拒绝。"""
    e = _engine(tmp_path, learn=())
    offered = {action.get("action_type")
               for action in e.get_available_actions().get("actions", [])}
    assert "declare_parry" not in offered

    result = e.execute_action("declare_parry", {})
    assert not result["success"]
    assert "resolve_attack" in result["error"]


@pytest.mark.xfail(strict=True, reason="2026-09-10 攻次/攻力改制后，1×1 面板下"
                                        "普攻（免费1伤）与杀伐（1法力1伤）的真实取舍已改变")
def test_legacy_one_by_one_daowen_outscores_basic_attack(tmp_path):
    """与招架无关的历史实验 AI 排序断言，继续作为 strict xfail 留档。"""
    from engine.ai_tactics import TacticalAI

    e = _engine(tmp_path, learn=("杀伐",))
    player = e.state.player
    player.current_speed, player.current_mana = 1, 1
    ai = TacticalAI(e, verbose=True)
    ai.take_turn()
    decisions = [line for line in ai.log if "实时决策" in line]
    assert decisions and all("普攻" not in line for line in decisions), decisions


def test_rule_ai_budget_reads_action_count_not_hardcoded_two(tmp_path):
    """实验规则 AI 的出手预算读 action_count（可被 蓄锐·增/疯狂/无力修正）。"""
    from engine.ai_tactics import TacticalAI

    e = _engine(tmp_path, learn=())
    stub = type("_Stub", (), {"action_count": 5, "actions_used_this_round": 1,
                              "current_mana": 0})()
    ai = TacticalAI(e, actor=stub)
    assert ai.remaining_actions() == 4
