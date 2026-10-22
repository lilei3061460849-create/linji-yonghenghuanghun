"""执行器语义（Part 3/4/5/7）：IF 执行期求值、LOOP 执行器拥有迭代、单步契约统一。

这些测试直接驱动 CombatEngine + SpellExecution，不经过 LLM/决策层，
目的是把"引擎实际怎么执行一个法术程序"这件事锁死：

  IF    —— 条件在执行到该步的那一刻按**当前** GameState 求值（含嵌套），
           循环的每一轮重新求值，不会被预展开成固定分支；
  LOOP  —— 迭代由执行器拥有：定次（循环N次）/规则循环（付不起、命零、
           本轮无进展即停）/调用方上限 max_iterations/工程安全阀；
  STEP  —— "一步 = 一次发动道纹"：飞行、缄默面具、施法者死亡、碎片代价、
           「目标发动道纹前」反应窗口在各类法术里同一口径；
           唯一显式差异是反应链层数（事件型法术不开新窗口）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import DaoWen, DaoWenInstance, Entity, GameState, Relic, Spell
from engine.spell_execution import ExecutionStatus, InterruptReason, StepStatus


def _state(player_hp=100, mana=20, enemy_hp=100):
    state = GameState(phase="in_combat", combat_subphase="player_actions", current_round=1)
    player = Entity("轮回者", "轮回者", blood_limit=100, current_hp=player_hp,
                    mana_limit=100, current_mana=mana, speed_limit=5, current_speed=5)
    enemy = Entity("靶怪", "怪物", blood_limit=enemy_hp, current_hp=enemy_hp,
                   attack_count=1, attack_power=3)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def _give(entity, *names):
    for name in names:
        entity.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X",
                   effect_formula=""), x_value=0)


def _cast(combat, player, enemy, flow, steps, max_iterations=None, target=None):
    refs = combat._combat_entity_refs()
    execution = combat.build_instant_execution(
        player, flow, target if target is not None else enemy, steps, refs,
        max_iterations=max_iterations)
    execution.run_all()
    return execution


def _daowen_sequence(execution):
    return [r.daowen for r in execution.results]


# ---------------------------------------------------------------------------
# IF：执行期求值
# ---------------------------------------------------------------------------

def test_if_is_evaluated_when_reached_not_precomputed():
    """前一步改变了状态 → 后一步的条件按新状态选分支（预展开模型必然选错）。"""
    state, combat, player, enemy = _state(player_hp=50)
    _give(player, "再生", "杀伐", "庇护")
    flow = ("发动再生X于自身→"
            "若自身 生命 大于 60 则 发动杀伐X于目标 否则 发动庇护X于自身")
    # 生命 50 + 再生3(12) = 62 > 60 → 立刻走"则"分支（杀伐），
    # 而不是按施法前的 50 走"否则"（庇护）。
    execution = _cast(combat, player, enemy, flow,
                      [{"x": 3}, {"x": 1, "dodge": False}, {"x": 1}])
    assert execution.status == ExecutionStatus.COMPLETED
    assert _daowen_sequence(execution) == ["再生", "杀伐"]
    assert player.shield == 0 and enemy.current_hp < 100

    # 同样的流程、只把再生调小：50 + 再生1(4) = 54 ≤ 60 → 走"否则"（庇护）
    state2, combat2, player2, enemy2 = _state(player_hp=50)
    _give(player2, "再生", "杀伐", "庇护")
    execution2 = _cast(combat2, player2, enemy2, flow,
                       [{"x": 1}, {"x": 1, "dodge": False}, {"x": 2}])
    assert _daowen_sequence(execution2) == ["再生", "庇护"]
    assert player2.shield > 0 and enemy2.current_hp == 100


def test_nested_if_is_evaluated_at_runtime():
    """嵌套条件分支：内外层都在执行到该步时求值。"""
    state, combat, player, enemy = _state(player_hp=50, mana=5)
    _give(player, "再生", "杀伐", "庇护")
    flow = ("发动再生X于自身→"
            "若自身 生命 大于 60 则（若自身 法力 大于 2 则 发动杀伐X于目标 "
            "否则 发动庇护X于自身）否则 发动庇护X于自身")
    execution = _cast(combat, player, enemy, flow,
                      [{"x": 3}, {"x": 1, "dodge": False}, {"x": 2}, {"x": 2}])
    # 生命 62>60 → 进则分支；再生3 把法力 5 花到 2 → 内层"法力大于2"为假 → 庇护
    assert _daowen_sequence(execution) == ["再生", "庇护"]
    assert player.shield > 0 and enemy.current_hp == 100


def test_loop_re_evaluates_condition_every_round():
    """循环体每轮按真实状态重新选分支（否则会锁死在第一轮的选择上）。"""
    state, combat, player, enemy = _state(player_hp=40, mana=0)
    _give(player, "透支", "再生")
    flow = "若自身 生命 大于 30 则 发动透支X于自身 否则 发动再生X于自身→循环"
    execution = _cast(combat, player, enemy, flow,
                      [{"x": 3}, {"x": 3}], max_iterations=4)
    # 40 →透支3(流血12) 28 → 28≤30 走再生3 → 40 → 透支3 → 28 → 再生3 …
    assert _daowen_sequence(execution) == ["透支", "再生", "透支", "再生"]
    assert [r.iteration for r in execution.results] == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# LOOP：执行器拥有迭代
# ---------------------------------------------------------------------------

def test_fixed_count_loop_runs_exactly_n_rounds():
    state, combat, player, enemy = _state(mana=20)
    _give(player, "庇护", "再生")
    execution = _cast(combat, player, enemy, "发动庇护X于自身→循环2次", [{"x": 1}])
    assert _daowen_sequence(execution) == ["庇护", "庇护"]
    assert execution.loop_stop_reason == "max_iterations"
    assert execution.status == ExecutionStatus.COMPLETED


def test_loop_with_no_reachable_step_stops_instead_of_spinning():
    """循环体里的分支都不成立 → 本轮无进展即停（不是无限空转，也不是中断）。"""
    state, combat, player, enemy = _state(player_hp=100, mana=20)
    _give(player, "杀伐")
    execution = _cast(combat, player, enemy,
                      "若自身 生命 小于 0 则 发动杀伐X于目标→循环",
                      [{"x": 1, "dodge": False}])
    assert execution.status == ExecutionStatus.COMPLETED
    assert execution.loop_stop_reason == "no_progress"
    assert execution.results == []


def test_loop_hits_engineering_fuse_with_explicit_reason(monkeypatch):
    """工程安全阀：正常规则循环不会触达；触达时给出 loop_guard 中断原因。"""
    import engine.spell_execution as spell_execution
    monkeypatch.setattr(spell_execution, "MAX_SPELL_LOOP_ITERATIONS", 3)
    state, combat, player, enemy = _state(mana=1000)
    _give(player, "庇护")
    execution = _cast(combat, player, enemy, "发动庇护X于自身→循环", [{"x": 1}])
    assert execution.status == ExecutionStatus.INTERRUPTED
    assert execution.interrupt_reason == InterruptReason.LOOP_GUARD
    assert len(execution.results) == 3
    assert execution.logs()[-1]["interrupted"] == "loop_guard"


def test_loop_stops_when_caster_has_no_resource_left():
    """规则终止：法力耗尽 = mana_insufficient 中断，已结算步骤保留。"""
    state, combat, player, enemy = _state(mana=2)
    _give(player, "庇护")
    execution = _cast(combat, player, enemy, "发动庇护X于自身→循环", [{"x": 1}])
    assert [r.status for r in execution.results] == [
        StepStatus.COMPLETED, StepStatus.COMPLETED, StepStatus.INTERRUPTED]
    assert execution.results[-1].reason == InterruptReason.MANA_INSUFFICIENT
    assert player.shield == 4  # 两次庇护的成果保留


# ---------------------------------------------------------------------------
# STEP：一步 = 一次发动道纹（各法术类型同一口径）
# ---------------------------------------------------------------------------

def test_step_policy_flags_are_explicit():
    """唯一显式差异：事件型法术的步骤不开新的「目标发动道纹前」窗口。"""
    from engine.combat_parts.spells import SpellReactionMixin
    assert SpellReactionMixin.CAST_STEP_POLICY.allow_trigger_reactions is True
    assert SpellReactionMixin.EVENT_STEP_POLICY.allow_trigger_reactions is False
    # 两类策略都声明"这一步是发动道纹"（飞行/缄默面具/施法者死亡/碎片代价同样适用）
    assert SpellReactionMixin.CAST_STEP_POLICY.declares_daowen is True
    assert SpellReactionMixin.EVENT_STEP_POLICY.declares_daowen is True


def test_instant_step_opens_target_before_daowen_window():
    """主动施法的一步会开启敌方「目标发动道纹前」窗口（与 use_daowen 同口径）。"""
    state, combat, player, enemy = _state(mana=20)
    _give(player, "杀伐")
    _give(enemy, "坠落", "杀伐", "连击")
    enemy.mana_limit = enemy.current_mana = 20
    enemy.spells.append(Spell(name="咎由自取", required_daowen=["坠落", "杀伐", "连击"],
                              trigger_condition="目标发动道纹前",
                              effect_flow="目标发动道纹前→发动坠落X于目标"))
    prepared = combat.prepare_daowen_trigger_spells(player)
    ref = next(iter(prepared))
    steps = [{"x": 1, "target_ref": s["target_ref"], "dodge": False}
             for s in prepared[ref][0]["steps"]]
    choices = {ref: {"咎由自取": {"use": True, "steps": steps}}}
    execution = _cast(combat, player, enemy, "发动杀伐X于目标", [{"x": 1, "dodge": False}],
                      target=enemy)
    # 未提交 trigger_spell_choices → 该步以明确原因中断（而不是静默跳过窗口）
    assert execution.results[0].reason == InterruptReason.TRIGGER_CHOICES_INVALID
    # 提交后窗口打开，反应法术前置生效
    execution2 = _cast(combat, player, enemy, "发动杀伐X于目标",
                       [{"x": 1, "dodge": False, "trigger_spell_choices": choices}],
                       target=enemy)
    assert execution2.status in (ExecutionStatus.COMPLETED, ExecutionStatus.INTERRUPTED)
    assert execution2.results[0].trigger_spell_logs


def test_event_path_does_not_open_trigger_window():
    """事件型法术的步骤不开窗口：A→B 允许，B 不再为 A 开窗（防 A→B→A）。"""
    state, combat, player, enemy = _state(player_hp=60, mana=20)
    _give(player, "再生")
    _give(enemy, "坠落", "杀伐", "连击")
    enemy.mana_limit = enemy.current_mana = 20
    enemy.spells.append(Spell(name="咎由自取", required_daowen=["坠落", "杀伐", "连击"],
                              trigger_condition="目标发动道纹前",
                              effect_flow="目标发动道纹前→发动坠落X于目标"))
    player.spells.append(Spell(name="自愈", required_daowen=["再生"],
                               trigger_condition="失去生命后", effect_flow="发动再生X于自身"))
    refs = combat._combat_entity_refs()
    logs = combat._resolve_spell_reactions(
        "失去生命后", player, enemy,
        {"自愈": {"use": True, "steps": [{"x": 1, "target_ref": "player:0"}]}}, refs)
    fired = [lg for lg in logs if lg.get("spell") == "自愈" and lg.get("execution")]
    assert fired, logs
    # 事件型步骤没有触发窗口 → 敌方咎由自取没有任何日志
    assert not [lg for lg in logs if lg.get("spell") == "咎由自取"]
    assert enemy.current_hp == 100 and not enemy.has_status("坠落")


def test_event_path_applies_silence_mask_to_step():
    """缄默面具对事件型法术的步骤同样生效（R-1：一步就是一次发动道纹）。"""
    state, combat, player, enemy = _state(player_hp=60, mana=20)
    state.relics.append(Relic("缄默面具", ""))
    _give(player, "透支")
    player.spells.append(Spell(name="卖血", required_daowen=["透支"],
                               trigger_condition="失去生命后", effect_flow="发动透支X于自身"))
    refs = combat._combat_entity_refs()
    logs = combat._resolve_spell_reactions(
        "失去生命后", player, enemy,
        {"卖血": {"use": True, "steps": [{"x": 1, "target_ref": "player:0"}]}}, refs)
    entry = next(lg for lg in logs if lg.get("spell") == "卖血")
    assert entry["interrupted"] == InterruptReason.DAOWEN_UNUSABLE.value
    assert "缄默面具" in entry["detail"]
    assert player.current_hp == 60  # 没有流血代价


def test_event_path_pays_xiaozai_shard_cost_and_interrupts_when_short():
    """消灾X 的碎片代价在事件型法术步骤里同样支付/拦截。"""
    state, combat, player, enemy = _state(player_hp=60, mana=20)
    state.fake_shards = 0
    state.shards = 0
    _give(player, "消灾")
    player.spells.append(Spell(name="消灾", required_daowen=["消灾"],
                               trigger_condition="失去生命后", effect_flow="发动消灾X于自身"))
    refs = combat._combat_entity_refs()
    logs = combat._resolve_spell_reactions(
        "失去生命后", player, enemy,
        {"消灾": {"use": True, "steps": [{"x": 1, "target_ref": "player:0"}]}}, refs)
    entry = next(lg for lg in logs if lg.get("spell") == "消灾")
    assert entry["interrupted"] == InterruptReason.SHARDS_INSUFFICIENT.value
    assert combat.dice.rerolls_pending == 0

    state.fake_shards = 60
    logs2 = combat._resolve_spell_reactions(
        "失去生命后", player, enemy,
        {"消灾": {"use": True, "steps": [{"x": 1, "target_ref": "player:0"}]}}, refs)
    assert any(lg.get("spell") == "消灾" and lg.get("execution") for lg in logs2)
    assert state.fake_shards == 10
    assert combat.dice.rerolls_pending == 1


def test_event_path_flight_blocks_step_with_explicit_reason():
    """飞行目标：事件型法术步骤同样按 target_untargetable 中断。"""
    state, combat, player, enemy = _state(player_hp=60, mana=20)
    enemy.is_flying = True
    _give(player, "杀伐")
    player.spells.append(Spell(name="反击", required_daowen=["杀伐"],
                               trigger_condition="失去生命后", effect_flow="发动杀伐X于攻击者"))
    refs = combat._combat_entity_refs()
    logs = combat._resolve_spell_reactions(
        "失去生命后", player, enemy,
        {"反击": {"use": True, "steps": [{"x": 1, "target_ref": "enemy:0", "dodge": False}]}},
        refs)
    entry = next(lg for lg in logs if lg.get("spell") == "反击")
    assert entry["interrupted"] == InterruptReason.TARGET_UNTARGETABLE.value
    assert enemy.current_hp == 100


def test_multiple_independent_reactions_on_one_event():
    """同一事件可以触发多个互相独立的反应法术（各用自己那份提交）。"""
    state, combat, player, enemy = _state(player_hp=60, mana=20)
    _give(player, "再生", "庇护")
    player.spells.append(Spell(name="自愈", required_daowen=["再生"],
                               trigger_condition="失去生命后", effect_flow="发动再生X于自身"))
    player.spells.append(Spell(name="加固", required_daowen=["庇护"],
                               trigger_condition="失去生命后", effect_flow="发动庇护X于自身"))
    refs = combat._combat_entity_refs()
    logs = combat._resolve_spell_reactions("失去生命后", player, enemy, {
        "自愈": {"use": True, "steps": [{"x": 1, "target_ref": "player:0"}]},
        "加固": {"use": True, "steps": [{"x": 1, "target_ref": "player:0"}]},
    }, refs)
    assert any(lg.get("spell") == "自愈" and lg.get("execution") for lg in logs)
    assert any(lg.get("spell") == "加固" and lg.get("execution") for lg in logs)
    assert player.current_hp == 64 and player.shield == 2


def test_reaction_resolution_does_not_chain_into_itself():
    """反应结算期间不再对新的失血开窗（A→B 允许，B→A 阻断）。"""
    state, combat, player, enemy = _state(player_hp=60, mana=20)
    _give(player, "再生")
    player.spells.append(Spell(name="自愈", required_daowen=["再生"],
                               trigger_condition="失去生命后", effect_flow="发动再生X于自身"))
    combat._resolving_life_lost_reactions += 1
    try:
        assert combat._fire_after_life_lost(player, None) == []
    finally:
        combat._resolving_life_lost_reactions -= 1


def test_any_target_slot_picks_submitted_entity():
    """role=any 的槽位由提交方选目标；schema 给出候选清单。"""
    state, combat, player, enemy = _state()
    second = Entity("二号", "怪物", blood_limit=80, current_hp=80)
    state.enemies.append(second)
    _give(player, "杀伐")
    refs = combat._combat_entity_refs()
    schema = combat._slot_schema(
        {"steps": [("杀伐", "any")]}, player,
        combat._reaction_subject_of(player, enemy))
    assert set(schema[0]["target_options"]) >= {"enemy:0", "enemy:1"}
    execution = _cast(combat, player, enemy, "发动杀伐X于任意目标",
                      [{"x": 2, "target_ref": "enemy:1", "dodge": False}],
                      target=None)
    assert execution.results[0].target_name == "二号"
    assert second.current_hp < 80 and enemy.current_hp == 100


def test_same_interruption_reason_and_detail_as_use_daowen():
    """R-1：法术的一步与 use_daowen 使用同一批前置环节，连报错文案都一致。"""
    from tests.test_dragon_heart import _new_engine, _start_with_enemy
    engine = _new_engine("exec_same_reason")
    _start_with_enemy(engine)
    player = engine.state.player
    _give(player, "透支")
    engine.state.relics.append(Relic("缄默面具", ""))
    refs = engine.combat._combat_entity_refs()

    direct = engine.execute_action("use_daowen", {
        "daowen_name": "透支", "x": 1, "target_ref": "player:0"})
    assert not direct["success"] and "缄默面具" in direct["error"]

    execution = _cast(engine.combat, player, engine.state.enemies[0],
                      "发动透支X于自身", [{"x": 1}], target=None)
    assert execution.results[0].reason == InterruptReason.DAOWEN_UNUSABLE
    assert execution.results[0].detail in direct["error"] or \
        direct["error"] in execution.results[0].detail

    engine.state.relics.clear()
    engine.state.fake_shards = 0
    engine.state.shards = 0
    _give(player, "消灾")
    direct2 = engine.execute_action("use_daowen", {
        "daowen_name": "消灾", "x": 1, "target_ref": "player:0"})
    assert not direct2["success"] and "碎片不足" in direct2["error"]
    execution2 = _cast(engine.combat, player, engine.state.enemies[0],
                       "发动消灾X于自身", [{"x": 1}], target=None)
    assert execution2.results[0].reason == InterruptReason.SHARDS_INSUFFICIENT
    assert "碎片不足" in execution2.results[0].detail
