"""法术流程编译及新存档格式。"""
import base64
import json
import pickle

import pytest

from engine.api import GameEngine
from engine.combat_parts.spells import SpellReactionMixin
from engine.spell_dsl import LoopStep, parse_effect_flow
from engine.spell_program import ProgramCursor, compile_program
from tests.test_spell_priority_windows import _cast, _engine


def _strip_new_fields(path, *fields):
    # 只反序列化本测试自己生成的存档。
    data = json.loads(path.read_text())
    snapshot = pickle.loads(base64.b64decode(data["payload"]))
    for frame in snapshot["combat_runtime"]["spell_stack"]:
        for key in fields:
            frame.pop(key, None)
    data["payload"] = base64.b64encode(pickle.dumps(snapshot)).decode("ascii")
    path.write_text(json.dumps(data))



def test_every_current_builtin_flow_compiles_to_cursor():
    engine, _, _ = _engine()
    loop_names = set()
    for name, flow in SpellReactionMixin.SPELL_FLOWS.items():
        definition = engine.combat._spell_definition_from_flow(name, flow, flow["trigger"])
        program = definition.program
        assert program is not None, name
        ProgramCursor(program)
        if flow.get("loop"):
            loop_names.add(name)
            assert program.loops, name
    assert loop_names == {"不死不休", "千刀万剐", "血炼周天", "徒劳无功"}


@pytest.mark.parametrize("flow", [
    "发动再生X于自身→发动燃命X于自身→循环",
    "发动庇护X于自身→循环2次",
    "若自身 生命 大于 30 则 发动燃命X于自身 否则 发动再生X于自身→循环",
    "若自身 生命 大于 30 则（若自身 法力 大于 2 则 发动再生X于自身 "
    "否则 发动燃命X于自身）否则 发动庇护X于自身→循环",
])
def test_valid_custom_examples_compile(flow):
    body, loop = parse_effect_flow(flow, {"再生", "燃命", "庇护"})
    assert loop
    program = compile_program(body)
    assert program.loops
    ProgramCursor(program)


def test_unknown_loop_node_reports_path():
    with pytest.raises(ValueError, match="节点0L0.*object.*无法编译"):
        compile_program([LoopStep((object(),))])


def test_replay_does_not_repeat_payment_effect_or_callback():
    from tests.test_spell_executor_semantics import _state, _give
    from engine.spell_execution import SpellExecution
    _, combat, player, enemy = _state(player_hp=50, mana=20)
    _give(player, "再生")
    execution = combat.build_instant_execution(player, "发动再生X于自身→循环2次", enemy,
                                              [{"x": 1}], combat._combat_entity_refs())
    execution.run_to_window()
    before = (player.current_hp, player.current_mana, player.total_healed)
    def unexpected(*args, **kwargs):
        raise AssertionError("重放不得支付、结算或调用回调")
    restored = SpellExecution(combat, execution.definition, player, enemy,
                              combat._combat_entity_refs(), execution.request,
                              before_step=unexpected, on_step=unexpected)
    original = combat._execute_single_daowen_step
    combat._execute_single_daowen_step = unexpected
    try:
        restored.restore_paused(execution.results, execution._run_decisions,
                                execution._branch_trace, execution._frozen_at_start,
                                execution._control_loop_frames)
    finally:
        combat._execute_single_daowen_step = original
    assert (player.current_hp, player.current_mana, player.total_healed) == before
    assert len(restored.results) == 1


@pytest.mark.parametrize("field", ["branch_trace", "frozen_at_start", "control_loop_frames"])
def test_missing_required_pause_field_rejected_before_replacing_state(tmp_path, field):
    engine, _, _ = _engine(player_extra=3)
    engine.save_dir = str(tmp_path)
    assert _cast(engine, "player:0", "发动庇护X于自身→循环2次", "enemy:0", [1])["success"]
    assert engine.save_game("incomplete")["success"]
    _strip_new_fields(tmp_path / "save_incomplete.json", field)
    loaded = GameEngine(autosave=False)
    loaded.save_dir = str(tmp_path)
    original = loaded.state
    result = loaded.load_game("incomplete")
    assert not result["success"] and "缺少新格式数据" in result["error"]
    assert loaded.state is original


def test_old_save_version_is_rejected(tmp_path):
    engine, _, _ = _engine()
    engine.save_dir = str(tmp_path)
    assert engine.save_game("old")["success"]
    path = tmp_path / "save_old.json"
    data = json.loads(path.read_text())
    data["version"] = engine.SAVE_FORMAT_VERSION - 1
    path.write_text(json.dumps(data))
    original = engine.state
    assert not engine.load_game("old")["success"]
    assert engine.state is original


@pytest.mark.parametrize("name", sorted(SpellReactionMixin.SPELL_FLOWS))
def test_each_builtin_runs_through_program_cursor(name, monkeypatch):
    from engine.models import DaoWen, DaoWenInstance
    from engine.spell_execution import SpellCastRequest, SpellExecution, StepRequest
    from engine.spell_dsl import iter_action_steps
    engine, player, enemy = _engine()
    flow = engine.combat.SPELL_FLOWS[name]
    definition = engine.combat._spell_definition_from_flow(name, flow, flow["trigger"])
    for daowen in definition.required_daowen:
        player.dao_wen[daowen] = DaoWenInstance(
            DaoWen(name=daowen, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    steps = [StepRequest(x=1, target_ref="enemy:0" if step.target == "any" else None)
             for _, step in iter_action_steps(definition.body)]
    calls = []
    original = ProgramCursor.next_action
    def track(cursor, *args, **kwargs):
        calls.append(cursor.program)
        return original(cursor, *args, **kwargs)
    monkeypatch.setattr(ProgramCursor, "next_action", track)
    execution = SpellExecution(engine.combat, definition, player, enemy,
                               engine.combat._combat_entity_refs(),
                               SpellCastRequest(steps=steps, max_iterations=1),
                               target_resolver=engine.combat._instant_target_resolver(enemy))
    execution.run_all()
    assert calls and all(program is definition.program for program in calls)
    assert execution.results, name
    assert execution.status.value != "failed", (name, execution.interrupt_detail)
