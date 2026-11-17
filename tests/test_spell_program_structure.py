"""新流程表示的静态结构测试；不代表运行时已接入。"""
import pytest

from engine.spell_dsl import ActionStep, parse_condition
from engine.spell_program import Action, Condition, Jump, SpellProgram, Stop, WaitEvent, validate_structure


def test_condition_jumps_loop_and_wait_share_stable_node_references():
    condition = parse_condition("自身 生命 大于 10 且 自身 法力 大于 0")
    program = SpellProgram((
        Condition("check", condition, on_false="done"),
        Action("heal", ActionStep("再生", "self")),
        WaitEvent("wait", "失去生命后", resume="check"),
        Stop("done"),
    ), entry="check")
    validate_structure(program)
    # 插入独立节点不会改变已有跳转的目标标识。
    updated = SpellProgram((program.nodes[0], Stop("inserted"), *program.nodes[1:]), "check")
    validate_structure(updated)
    assert updated.nodes[0].on_false == "done" and updated.nodes[3].resume == "check"


@pytest.mark.parametrize("nodes,entry", [
    ((Stop("same"), Stop("same")), "same"),
    ((Jump("go", "missing"),), "go"),
    ((WaitEvent("wait", "回始", "missing"),), "wait"),
    ((Stop("end"),), "missing"),
    ((Stop(""),), ""),
    ((WaitEvent("wait", ""),), "wait"),
])
def test_rejects_invalid_structure(nodes, entry):
    with pytest.raises(ValueError):
        validate_structure(SpellProgram(nodes, entry))


def test_cursor_runs_actions_and_branches_without_effect_side_effects():
    from engine.spell_program import ProgramCursor
    expression = parse_condition("自身 生命 大于 10")
    program = SpellProgram((
        Condition("test", expression, on_true="a", on_false="b"),
        Action("a", ActionStep("再生", "self"), next="end"),
        Action("b", ActionStep("杀伐", "target"), next="end"),
        Stop("end"),
    ), "test")
    for value, expected in [(True, "a"), (False, "b")]:
        cursor = ProgramCursor(program)
        assert cursor.next_action(lambda _: value).id == expected
        assert cursor.next_action(lambda _: value) is None


def test_cursor_rejects_cycles_and_wait_until_their_rules_are_implemented():
    from engine.spell_program import ProgramCursor
    with pytest.raises(ValueError, match="循环"):
        ProgramCursor(SpellProgram((Jump("again", "again"),), "again"))
    with pytest.raises(ValueError, match="事件等待"):
        ProgramCursor(SpellProgram((WaitEvent("wait", "回始"),), "wait"))
