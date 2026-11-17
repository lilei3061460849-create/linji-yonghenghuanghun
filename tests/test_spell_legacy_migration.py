"""旧流程编译与历史格式暂停存档兼容边界。"""
import base64
import json
import pickle

import pytest

from engine.api import GameEngine
from engine.combat_parts.spells import SpellReactionMixin
from engine.spell_dsl import ActionStep, LoopStep, parse_effect_flow
from engine.spell_program import ProgramCursor, compile_legacy
from tests.test_spell_priority_windows import _cast, _engine, _skip


def _strip_new_fields(path, *fields):
    # 只反序列化本测试自己生成的存档；模拟重构前快照字段集合。
    data = json.loads(path.read_text())
    snapshot = pickle.loads(base64.b64decode(data["payload"]))
    for frame in snapshot["combat_runtime"]["spell_stack"]:
        for key in fields:
            frame.pop(key, None)
    data["payload"] = base64.b64encode(pickle.dumps(snapshot)).decode("ascii")
    path.write_text(json.dumps(data))


NEW_FIELDS = ("branch_trace", "frozen_at_start", "loop_start_fingerprint", "control_loop_frames")


def test_every_current_builtin_flow_compiles_to_cursor():
    engine, _, _ = _engine()
    loop_names = set()
    for name, flow in SpellReactionMixin.SPELL_FLOWS.items():
        definition = engine.combat._spell_definition_from_flow(name, flow, flow["trigger"])
        program = compile_legacy(definition.body)
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
def test_valid_custom_legacy_examples_compile(flow):
    body, loop = parse_effect_flow(flow, {"再生", "燃命", "庇护"})
    assert loop
    program = compile_legacy(body)
    assert program.loops
    ProgramCursor(program)


def test_unknown_legacy_loop_node_reports_path_instead_of_silent_fallback():
    with pytest.raises(ValueError, match="节点0L0.*object.*无法编译"):
        compile_legacy([LoopStep((object(),))])


def test_pre_refactor_nonloop_paused_format_still_resumes(tmp_path):
    engine, player, monster = _engine(player_extra=2)
    engine.save_dir = str(tmp_path)
    result = _cast(engine, "player:0", "发动杀伐X于目标→发动再生X于自身",
                   "enemy:0", [1, 1])
    assert result["success"]
    assert engine.save_game("old_nonloop")["success"]
    path = tmp_path / "save_old_nonloop.json"
    _strip_new_fields(path, *NEW_FIELDS)
    loaded = GameEngine(autosave=False)
    loaded.save_dir = str(tmp_path)
    assert loaded.load_game("old_nonloop")["success"]
    assert loaded.state.enemies[0].current_hp == 49
    assert loaded.state.player.actions_used_this_round == 1
    response = _skip(loaded, "enemy:0")
    assert response["success"]
    assert loaded.state.enemies[0].current_hp == 49
    assert loaded.state.player.actions_used_this_round == 2


def test_pre_refactor_loop_without_baseline_is_rejected_before_state_replacement(tmp_path):
    engine, _, _ = _engine(player_extra=3)
    engine.save_dir = str(tmp_path)
    result = _cast(engine, "player:0", "发动庇护X于自身→循环2次", "enemy:0", [1])
    assert result["success"]
    assert engine.save_game("old_loop")["success"]
    path = tmp_path / "save_old_loop.json"
    _strip_new_fields(path, *NEW_FIELDS)
    loaded = GameEngine(autosave=False)
    loaded.save_dir = str(tmp_path)
    initial_state = loaded.state
    result = loaded.load_game("old_loop")
    assert not result["success"] and "缺少循环轮初快照" in result["error"]
    assert loaded.state is initial_state


def test_intermediate_single_loop_snapshot_with_baseline_resumes(tmp_path):
    engine, player, _ = _engine(player_extra=3)
    engine.save_dir = str(tmp_path)
    assert _cast(engine, "player:0", "发动庇护X于自身→循环2次", "enemy:0", [1])["success"]
    assert engine.save_game("old_baseline")["success"]
    path = tmp_path / "save_old_baseline.json"
    _strip_new_fields(path, "branch_trace", "frozen_at_start", "control_loop_frames")
    loaded = GameEngine(autosave=False)
    loaded.save_dir = str(tmp_path)
    assert loaded.load_game("old_baseline")["success"]
    assert loaded.state.player.current_mana == 99
    assert loaded.state.player.actions_used_this_round == 1
    execution = loaded.combat._spell_stack[-1]["execution"]
    assert _skip(loaded, "enemy:0")["success"]
    assert len(execution.results) == 2
    assert loaded.state.player.current_mana == 98
    assert loaded.state.player.actions_used_this_round == 2


@pytest.mark.parametrize("name", ["不死不休", "千刀万剐", "血炼周天", "徒劳无功"])
def test_builtin_loop_new_cursor_matches_legacy_steps(name):
    from engine.models import DaoWen, DaoWenInstance
    from engine.spell_execution import SpellCastRequest, SpellExecution, StepRequest

    def run(legacy):
        engine, player, enemy = _engine()
        player.current_hp = 40
        player.current_mana = 20
        for daowen in SpellReactionMixin.BUILTIN_SPELL_DAOWEN[name]:
            player.dao_wen[daowen] = DaoWenInstance(
                DaoWen(name=daowen, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
        flow = engine.combat.SPELL_FLOWS[name]
        definition = engine.combat._spell_definition_from_flow(name, flow, flow["trigger"])
        refs = engine.combat._combat_entity_refs()
        steps = [StepRequest(x=1, target_ref="enemy:0" if role == "any" else None)
                 for _, role in flow["steps"]]
        execution = SpellExecution(engine.combat, definition, player, enemy, refs,
                                   SpellCastRequest(steps=steps, max_iterations=2))
        assert execution._control_program and execution._control_program.loops
        if legacy:
            execution._control_program = None
        execution.run_all()
        return ([(r.daowen, r.status.value, r.iteration, r.cost_paid, r.reason.value)
                 for r in execution.results], execution.status.value,
                execution.interrupt_reason.value, execution.loop_stop_reason,
                player.current_hp, player.current_mana, player.total_healed,
                enemy.current_hp)

    assert run(False) == run(True)


def test_intermediate_dynamic_loop_without_branch_trace_infers_distinct_steps(tmp_path):
    from engine.models import DaoWen, DaoWenInstance
    engine, player, monster = _engine(player_extra=5)
    player.current_hp, player.current_mana = 40, 0
    player.dao_wen["燃命"] = DaoWenInstance(
        DaoWen(name="燃命", formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=0, x_free=True)
    flow = "若自身 生命 大于 30 则 发动燃命X于自身 否则 发动再生X于自身→循环"
    execution = engine.combat.build_instant_execution(player, flow, monster,
        [{"x": 3}, {"x": 3}], engine.combat._combat_entity_refs(), max_iterations=4)
    engine._bind_spell_step_payment(execution)
    execution.run_to_window()
    execution.run_to_window()
    assert [r.daowen for r in execution.results] == ["燃命", "再生"]
    before = (player.current_hp, player.current_mana, player.total_healed, player.actions_used_this_round)
    engine._push_spell_frame(execution)
    engine.save_dir = str(tmp_path)
    assert engine.save_game("old_dynamic")["success"]
    _strip_new_fields(tmp_path / "save_old_dynamic.json", "branch_trace", "frozen_at_start", "control_loop_frames")
    loaded = GameEngine(autosave=False)
    loaded.save_dir = str(tmp_path)
    assert loaded.load_game("old_dynamic")["success"]
    restored = loaded.combat._spell_stack[-1]["execution"]
    lp = loaded.state.player
    assert (lp.current_hp, lp.current_mana, lp.total_healed, lp.actions_used_this_round) == before
    assert [taken for _, taken in restored._branch_trace] == [0, 1]
    assert _skip(loaded, "enemy:0")["success"]
    assert [r.daowen for r in restored.results] == ["燃命", "再生", "燃命", "再生"]
    assert lp.actions_used_this_round == 4


def test_nested_snapshot_with_only_one_loop_baseline_fails_atomically(tmp_path):
    from engine.spell_execution import (SpellDefinition, SpellExecution, SpellCastRequest,
                                        StepRequest, TriggerType, Lifecycle)
    engine, player, enemy = _engine(player_extra=8)
    body = [LoopStep((LoopStep((ActionStep("庇护", "self"),), 2),
                      ActionStep("庇护", "self")), 2)]
    definition = SpellDefinition("旧嵌套", ["庇护"], TriggerType.IMMEDIATE, Lifecycle.INSTANT, body)
    execution = SpellExecution(engine.combat, definition, player, enemy,
                               engine.combat._combat_entity_refs(),
                               SpellCastRequest(steps=[StepRequest(x=1), StepRequest(x=1)]))
    engine._bind_spell_step_payment(execution)
    execution.run_to_window()
    engine._push_spell_frame(execution)
    engine.save_dir = str(tmp_path)
    assert engine.save_game("old_nested")["success"]
    _strip_new_fields(tmp_path / "save_old_nested.json", "control_loop_frames")
    loaded = GameEngine(autosave=False)
    loaded.save_dir = str(tmp_path)
    before = loaded.state
    result = loaded.load_game("old_nested")
    assert not result["success"] and "缺少嵌套循环轮初快照" in result["error"]
    assert loaded.state is before


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
                                execution._loop_start_fingerprint, execution._control_loop_frames)
    finally:
        combat._execute_single_daowen_step = original
    assert (player.current_hp, player.current_mana, player.total_healed) == before
    assert len(restored.results) == 1
