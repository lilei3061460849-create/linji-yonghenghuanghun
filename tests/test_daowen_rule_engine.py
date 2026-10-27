"""统一道纹数值规则：定义、运算、顺序、兼容边界与双阶段 trace。"""
from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
import copy
from dataclasses import replace

import pytest

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.combat_hooks import CombatHookManager
from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, StatusEffect
from engine.rule_engine import (
    DAOWEN_RULES,
    CompareOperator,
    ConditionGroup,
    ConditionGroupMode,
    ConditionKind,
    CustomOperatorResult,
    DuplicateRuleExecutionError,
    DamageResolutionContext,
    JIAHAI_RULE,
    LONG_LIN_RULE,
    OperatorSpec,
    OperandSource,
    RuleCondition,
    RULE_EXECUTOR,
    RuleError,
    RuleOperator,
    RoundingMode,
    UNMIGRATED_REASON,
    ValueComparison,
    ValueRef,
    apply_operator,
    daowen_migration_status,
    render_rule_description,
    register_custom_operator,
    rule_matches,
)

ROOT = Path(__file__).resolve().parents[1]


class _State:
    def side_has(self, *_):
        return False


def _target(*statuses: tuple[str, int]) -> Entity:
    entity = Entity("受击者", "轮回者", blood_limit=100, current_hp=100)
    for name, value in statuses:
        entity.add_status(StatusEffect(
            name=name, value=value, remaining_rounds=-1, source="test"))
    return entity


def _combat_target(*statuses: tuple[str, int]):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    target = Entity("受击者", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=10, current_mana=10, speed_limit=1, current_speed=1)
    attacker = Entity("攻击者", "怪物", blood_limit=100, current_hp=100)
    for name, value in statuses:
        target.add_status(StatusEffect(name=name, value=value,
                                       remaining_rounds=-1, source="test"))
    state.player = target
    state.enemies = [attacker]
    return state, target, attacker, CombatEngine(state, DiceEngine(seed=17))


def test_three_rules_have_stable_ids_and_complete_metadata():
    rule_ids = []
    for daowen in DAOWEN_RULES.values():
        assert daowen.daowen_id.startswith("daowen.")
        assert daowen.rules
        for rule in daowen.rules:
            rule_ids.append(rule.rule_id)
            assert rule.event.value == "damage_received"
            assert rule.stage.value == "post_block_pre_life_loss_multiplier"
            assert rule.target_role.value == "recipient"
            assert rule.reads == rule.writes == ValueRef.CURRENT_DAMAGE
            assert rule.read_semantics.value == "latest_value"
            assert rule.conditions is not None
    assert len(rule_ids) == len(set(rule_ids)) == 1


def test_calculation_summaries_are_rendered_from_the_runtime_rule_definitions():
    DaoWenEngine.register_all()
    for name, x, kwargs in (
        ("固执", 2, {}),
    ):
        result = DaoWenEngine._registry[name](x, **kwargs)
        definition = DAOWEN_RULES[name]
        expected_target = kwargs["target"].name if "target" in kwargs else None
        assert result["summary"] == definition.summary(x, target_name=expected_target)
        assert DaoWenEngine.migration_status(name)["status"] == "migrated"

    guzhi = DaoWenEngine.calculate_guzhi(2)
    assert guzhi["max_current_damage_per_hit"] == 1
    # 兼容键代表最终实际失血上限；新键代表倍率前的当前受击伤害上限。
    assert guzhi["max_life_loss_per_hit"] == 1
    assert guzhi["max_current_damage_per_hit"] == 1
    assert "龙鳞" not in DaoWenEngine.list_all()
    assert "加害" not in DaoWenEngine.list_all()


def test_zaisheng_merges_self_feeding_as_an_alternate_attack_cost():
    DaoWenEngine.register_all()
    target = Entity("目标", "轮回者", blood_limit=100, current_hp=20,
                    mana_limit=10, current_mana=10)
    mana = DaoWenEngine.resolve("再生", 2, target=target, cost_channel="法力")
    attack = DaoWenEngine.resolve("再生", 2, target=target, cost_channel="攻击力")

    assert mana["cost_type"] == "消耗"
    assert mana["cost"] == 2
    assert mana["target_heal"] == 8
    assert attack["cost_type"] == "攻击力"
    assert attack["cost_attack"] == 8
    assert attack["target_heal"] == 8
    assert "自食" not in DaoWenEngine.list_all()
    assert ("全力", "曲解", "再生") in ResonanceEngine.CLOSED_LOOPS["怪物原始道纹"]


def test_migration_status_keeps_unmigrated_daowen_on_the_legacy_path():
    DaoWenEngine.register_all()
    assert set(DAOWEN_RULES) == {"固执"}
    assert daowen_migration_status("杀伐") == {
        "status": "legacy", "reason": UNMIGRATED_REASON,
    }
    assert DaoWenEngine.migration_status("杀伐")["status"] == "legacy"


def test_generated_rule_descriptions_match_the_authoritative_documents():
    common_rules = (ROOT / "规则正文.md").read_text(encoding="utf-8")
    dungeon = (ROOT / "副本" / "龙心谷.md").read_text(encoding="utf-8")
    index = (ROOT / "全道纹索引.md").read_text(encoding="utf-8")

    guzhi = render_rule_description("固执", "X")
    assert guzhi in common_rules
    assert guzhi in index
    assert "### 龙鳞" in (ROOT / "物品索引.md").read_text(encoding="utf-8")
    assert "龙鳞X：支付：法力2X" not in dungeon
    assert "| 龙鳞 |" not in index


def test_generic_numeric_operators_are_composable_and_explainable():
    assert apply_operator(5, 2, OperatorSpec(RuleOperator.ADD)).value == 7
    assert apply_operator(5, 2, OperatorSpec(RuleOperator.SUBTRACT)).value == 3
    assert apply_operator(5, 8, OperatorSpec(
        RuleOperator.SUBTRACT, lower_bound=0)).value == 0
    assert apply_operator(5, 2, OperatorSpec(RuleOperator.MULTIPLY)).value == 10
    assert apply_operator(10, 4, OperatorSpec(RuleOperator.DIVIDE)).value == Fraction(5, 2)
    assert apply_operator(5, 2, OperatorSpec(RuleOperator.MIN)).value == 2
    assert apply_operator(5, 8, OperatorSpec(RuleOperator.MAX)).value == 8
    assert apply_operator(12, None, OperatorSpec(
        RuleOperator.CLAMP, lower_bound=1, upper_bound=9)).value == 9
    assert apply_operator(11, 25, OperatorSpec(
        RuleOperator.SCALE_PERCENT, rounding=RoundingMode.CEIL)).value == 3
    converted = apply_operator(2, None, OperatorSpec(
        RuleOperator.CONVERT, conversion_map=(("2", 20),)))
    assert converted.value == 20
    assert "20" in converted.explanation


def test_fractional_damage_operator_rounds_at_the_rule_write_boundary_and_traces_it():
    target = _target(("龙鳞", 1))
    status = target.status_effects[0]
    definition = replace(
        LONG_LIN_RULE,
        operator=OperatorSpec(
            RuleOperator.DIVIDE, operand=2, rounding=RoundingMode.CEIL),
        operand_source=OperandSource.CONSTANT,
    )
    context = DamageResolutionContext(
        event_id="fractional-writeback", attacker=None, recipient=target,
        damage_type="普通", original_damage=5, incoming_damage=5, current_damage=5,
    )

    assert RULE_EXECUTOR.execute(definition, context, status) == 3
    assert context.current_damage == 3
    assert context.trace[0]["rounding"] == "ceil"
    assert "舍入为 3" in context.trace[0]["explanation"]
    json.dumps(context.to_dict(), ensure_ascii=False, allow_nan=False)


def test_failed_rule_execution_is_atomic_and_can_be_retried():
    target = _target(("龙鳞", 1))
    status = target.status_effects[0]
    context = DamageResolutionContext(
        event_id="atomic-rule", attacker=None, recipient=target,
        damage_type="普通", original_damage=8, incoming_damage=8, current_damage=8,
    )
    failing = replace(
        LONG_LIN_RULE,
        operator=OperatorSpec(RuleOperator.DIVIDE, operand=0),
        operand_source=OperandSource.CONSTANT,
    )

    with pytest.raises(RuleError, match="不得为0"):
        RULE_EXECUTOR.execute(failing, context, status)
    assert context.current_damage == 8
    assert context.trace == []
    assert context.deferred_effects == []
    assert failing.rule_id not in context._executed_rule_ids

    retry = replace(
        LONG_LIN_RULE,
        operator=OperatorSpec(RuleOperator.SUBTRACT, operand=2),
        operand_source=OperandSource.CONSTANT,
    )
    assert RULE_EXECUTOR.execute(retry, context, status) == 6
    assert context.trace[0]["after"] == 6


def test_incoming_rule_window_commits_all_context_changes_atomically(monkeypatch):
    import engine.rule_engine as rule_engine

    target = _target(("固执", 1), ("加害", 1), ("龙鳞", 1))
    context = DamageResolutionContext(
        event_id="atomic-window", attacker=None, recipient=target,
        damage_type="普通", original_damage=8, incoming_damage=8, current_damage=8,
    )
    manager = CombatHookManager()
    original_execute = RULE_EXECUTOR.execute
    calls = {"count": 0}

    def fail_after_one_applied(definition, working_context, status=None):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuleError("second rule failed")
        return original_execute(definition, working_context, status)

    monkeypatch.setattr(rule_engine.RULE_EXECUTOR, "execute", fail_after_one_applied)
    with pytest.raises(RuleError, match="second rule failed"):
        manager.apply_incoming_adjust(
            target, 8, "普通", None, _State(), resolution_context=context)

    assert calls["count"] == 2
    assert context.current_damage == 8
    assert context.trace == []
    assert context.deferred_effects == []
    assert context._executed_rule_ids == set()


def test_custom_operator_receives_isolated_context_copy_and_frozen_entity_snapshots(monkeypatch):
    import dataclasses
    import engine.rule_engine as rule_engine

    monkeypatch.setattr(rule_engine, "_CUSTOM_OPERATORS", {})
    target = _target(("龙鳞", 2))
    context = DamageResolutionContext(
        event_id="readonly-custom", attacker=None, recipient=target,
        damage_type="普通", original_damage=4, incoming_damage=4, current_damage=4,
        trace=[{"existing": {"value": 7}}],
    )

    def inspect_snapshot(value, operand, spec, operator_context, definition):
        assert operator_context.current_damage == 4
        assert operator_context.recipient.current_hp == 100
        # context API 仍可写，但只写到执行器提供的隔离副本；实体视图是冻结快照。
        operator_context.current_damage = 999
        operator_context.trace[0]["existing"]["value"] = 99
        operator_context.trace.append({"fake": True})
        with pytest.raises(dataclasses.FrozenInstanceError):
            operator_context.recipient.current_hp = 0
        return CustomOperatorResult(value + 1, "隔离副本运算")

    register_custom_operator("tests.readonly", inspect_snapshot)
    result = apply_operator(
        4, None,
        OperatorSpec(RuleOperator.CUSTOM, executor_id="tests.readonly"),
        context=context, definition=LONG_LIN_RULE,
    )
    assert result.value == 5
    assert context.current_damage == 4
    assert context.trace == [{"existing": {"value": 7}}]
    assert target.current_hp == 100


def test_condition_groups_support_value_comparisons():
    target = _target(("加害", 2))
    context = DamageResolutionContext(
        event_id="condition-1", attacker=None, recipient=target,
        damage_type="普通", original_damage=8, incoming_damage=8, current_damage=5,
    )
    definition = replace(
        JIAHAI_RULE,
        conditions=ConditionGroup(ConditionGroupMode.ALL, (
            RuleCondition(ConditionKind.DAMAGE_TYPE_NOT, "代价"),
            RuleCondition(ConditionKind.TARGET_HAS_STATUS, "加害"),
            RuleCondition(ConditionKind.VALUE_COMPARE, ValueComparison(
                ValueRef.CURRENT_DAMAGE, CompareOperator.GE, 5)),
        )),
    )
    assert rule_matches(definition, context, target.status_effects[0]) is True
    context.set_current_damage(4)
    assert rule_matches(definition, context, target.status_effects[0]) is False


def test_skipped_rules_emit_truthful_condition_trace_without_mutating_damage():
    target = _target(("加害", 1))
    context = DamageResolutionContext(
        event_id="condition-skip", attacker=None, recipient=target,
        damage_type="普通", original_damage=5, incoming_damage=5, current_damage=5,
    )

    assert RULE_EXECUTOR.execute(LONG_LIN_RULE, context) == 5
    assert context.current_damage == 5
    assert len(context.trace) == 1
    step = context.trace[0]
    assert step["status"] == "skipped"
    assert step["reason"] == "condition_not_met"
    assert step["condition_trace"]["matched"] is False
    status_check = step["condition_trace"]["children"][1]
    assert status_check["status"] == "龙鳞"
    assert status_check["observed"] is False
    json.dumps(step, ensure_ascii=False, allow_nan=False)


def test_custom_and_deferred_operators_are_bounded_and_traceable(monkeypatch):
    import engine.rule_engine as rule_engine

    monkeypatch.setattr(rule_engine, "_CUSTOM_OPERATORS", {})

    def double_with_reason(value, operand, spec, context, definition):
        return CustomOperatorResult(value * 2, f"扩展运算器：{value}×2")

    register_custom_operator("tests.double_with_reason", double_with_reason)
    context = DamageResolutionContext(
        event_id="extension-1", attacker=None, recipient=Entity("T", "怪物"),
        damage_type="普通", original_damage=4, incoming_damage=4, current_damage=4,
    )
    custom = apply_operator(
        4, None, OperatorSpec(RuleOperator.CUSTOM, executor_id="tests.double_with_reason"),
        context=context, definition=LONG_LIN_RULE,
    )
    assert custom.value == 8
    assert "×2" in custom.explanation
    with pytest.raises(RuleError, match="重复注册"):
        register_custom_operator("tests.double_with_reason", double_with_reason)

    deferred = apply_operator(
        4, 2, OperatorSpec(RuleOperator.DEFER, deferred_event="next_phase.damage_followup"),
        context=context, definition=LONG_LIN_RULE,
    )
    assert deferred.value == 4, "延后事件不在当前值链递归执行"
    assert context.deferred_effects == [{
        "event": "next_phase.damage_followup",
        "rule_id": LONG_LIN_RULE.rule_id,
        "recipient": "T",
        "operand": "2",
        "cause_event_id": "extension-1",
    }]


def test_status_parameter_preserves_legacy_aggregation_for_duplicate_saved_statuses():
    target = Entity("T", "怪物", blood_limit=100, current_hp=100)
    # 正常 add_status 会合并同名项；旧/直接构造档案仍可能保留多项，旧 get_status_value
    # 的契约是求和，统一定义也应保持这一数值语义。
    target.status_effects.extend((
        StatusEffect("加害", value=2, remaining_rounds=-1, source="a"),
        StatusEffect("加害", value=3, remaining_rounds=-1, source="b"),
    ))
    assert CombatHookManager().apply_incoming_adjust(
        target, 4, "普通", None, _State()) == 9


def test_zero_is_a_valid_intermediate_value_and_add_reads_latest_value():
    target = _target(("加害", 2), ("龙鳞", 8))
    context = DamageResolutionContext(
        event_id="zero-then-add", attacker=None, recipient=target,
        damage_type="普通", original_damage=8, incoming_damage=8, current_damage=8)
    # 旧减法定义只作为规则执行器的独立代数测试，不再注册成龙鳞道纹。
    RULE_EXECUTOR.execute(LONG_LIN_RULE, context, target.status_effects[1])
    assert context.current_damage == 0
    RULE_EXECUTOR.execute(JIAHAI_RULE, context, target.status_effects[0])
    assert context.current_damage == 2


def test_reusing_rule_id_in_one_context_is_rejected_but_preview_commit_are_distinct():
    target = _target(("龙鳞", 2))
    status = target.status_effects[0]
    preview = DamageResolutionContext(
        event_id="duplicate-guard", attacker=None, recipient=target,
        damage_type="普通", original_damage=5, incoming_damage=5, current_damage=5,
        mode="preview",
    )
    commit = DamageResolutionContext(
        event_id="duplicate-guard", attacker=None, recipient=target,
        damage_type="普通", original_damage=5, incoming_damage=5, current_damage=5,
        mode="commit",
    )
    assert RULE_EXECUTOR.execute(LONG_LIN_RULE, preview, status) == 3
    assert RULE_EXECUTOR.execute(LONG_LIN_RULE, commit, status) == 3
    with pytest.raises(DuplicateRuleExecutionError):
        RULE_EXECUTOR.execute(LONG_LIN_RULE, commit, status)


def test_rule_trace_separates_attacker_target_shield_damage_and_actual_life_loss():
    state, target, attacker, combat = _combat_target(("固执", 1))
    target.shield = 2
    detail = combat._apply_hostile_damage(target, 5, source=attacker)

    assert detail["damage_resolution"]["attacker"] == "攻击者"
    assert detail["damage_resolution"]["recipient"] == "受击者"
    assert detail["damage_resolution"]["original_damage"] == 5
    assert detail["damage_resolution"]["preview"]["rules"][0]["before"] == 3
    assert detail["damage_resolution"]["preview"]["post_rule_damage"] == 1
    assert detail["damage_resolution"]["commit"]["shield_absorbed"] == 2
    assert detail["damage_resolution"]["commit"]["post_rule_damage"] == 1
    assert detail["actual_damage"] == 1
    assert detail["actual_life_loss"] == 1
    # 规则 trace 和本次伤害 context 可被严格 JSON 编码，不包含 NaN/Infinity/实体对象。
    json.dumps(detail["damage_resolution"], ensure_ascii=False, allow_nan=False)
    json.dumps(detail["rule_trace"], ensure_ascii=False, allow_nan=False)
    event = next(e for e in combat.event_stream if e.event_type.value == "damage_applied")
    assert event.data["actual_life_loss"] == 1
    assert event.data["rule_trace"] == detail["rule_trace"]


def test_damage_inputs_define_negative_missing_and_nonfinite_semantics():
    target = Entity("T", "怪物", blood_limit=100, current_hp=100)
    assert target.take_damage(-3)["actual_damage"] == 0
    assert target.current_hp == 100
    assert target.take_damage(10**12)["actual_damage"] == 10**12

    for invalid in (None, True, 1.0, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(RuleError):
            target.take_damage(invalid)

    with pytest.raises(RuleError):
        CombatHookManager().apply_incoming_adjust(target, float("inf"), "普通", None, _State())


def test_negative_intermediate_values_compose_then_damage_window_floors_without_healing():
    target = _target(("龙鳞", 10), ("加害", 2))
    context = DamageResolutionContext(
        event_id="negative-intermediate-rules", attacker=None, recipient=target,
        damage_type="普通", original_damage=5, incoming_damage=5, current_damage=5,
    )
    subtract = replace(
        LONG_LIN_RULE,
        operator=OperatorSpec(RuleOperator.SUBTRACT, operand=10),
        operand_source=OperandSource.CONSTANT,
    )
    add = replace(
        JIAHAI_RULE,
        operator=OperatorSpec(RuleOperator.ADD, operand=2),
        operand_source=OperandSource.CONSTANT,
    )
    assert RULE_EXECUTOR.execute(subtract, context, target.status_effects[0]) == -5
    assert RULE_EXECUTOR.execute(add, context, target.status_effects[1]) == -3
    applied = [step for step in context.trace if step["status"] == "applied"]
    assert [(step["before"], step["after"]) for step in applied] == [(5, -5), (-5, -3)]

    class NegativeHook:
        def on_incoming_adjust(self, _target, _amount, _damage_type, _source, _state):
            return -3

    manager = CombatHookManager()
    manager._hooks.append(NegativeHook())
    floor_context = DamageResolutionContext(
        event_id="negative-intermediate-floor", attacker=None, recipient=_target(),
        damage_type="普通", original_damage=5, incoming_damage=5, current_damage=5,
    )
    assert manager.apply_incoming_adjust(
        floor_context.recipient, 5, "普通", None, _State(),
        resolution_context=floor_context) == 0
    assert floor_context.trace[-1]["reason"] == "negative_damage_floor"
    assert floor_context.trace[-1]["before"] == -3
    assert floor_context.trace[-1]["after"] == 0


def test_entity_damage_adjuster_failure_rolls_back_shield_and_life_multiplier_is_validated():
    target = Entity("T", "怪物", blood_limit=100, current_hp=100, shield=5)
    lost_before = target.hp_lost_this_round

    def fail(_amount):
        raise RuntimeError("rule failed")

    with pytest.raises(RuntimeError, match="rule failed"):
        target.take_damage(8, status_adjuster=fail)
    assert target.shield == 5
    assert target.current_hp == 100
    assert target.hp_lost_this_round == lost_before

    for invalid in (None, True, 0, -1, 1.0, float("inf")):
        with pytest.raises((RuleError, ValueError)):
            target.take_damage(1, life_loss_multiplier=invalid)


def test_entity_damage_distinguishes_pre_multiplier_damage_from_actual_hp_loss():
    target = Entity("T", "怪物", blood_limit=3, current_hp=3)
    detail = target.take_damage(10)
    assert detail["actual_damage"] == 10
    assert detail["actual_life_loss"] == 3
    assert detail["hp_after"] == 0


def test_arbitrary_precision_damage_remains_accepted_and_trace_json_is_safe():
    huge = 10 ** 5_000
    target = Entity("T", "怪物", blood_limit=100, current_hp=100)
    detail = target.take_damage(huge)
    assert detail["actual_damage"] == huge
    assert detail["actual_life_loss"] == 100

    rule_target = _target(("加害", 1))
    context = DamageResolutionContext(
        event_id="huge-trace", attacker=None, recipient=rule_target,
        damage_type="普通", original_damage=huge, incoming_damage=huge,
        current_damage=huge,
    )
    manager = CombatHookManager()
    assert manager.apply_incoming_adjust(
        rule_target, huge, "普通", None, _State(), resolution_context=context) == huge + 1
    encoded = json.dumps(context.to_dict(), ensure_ascii=False, allow_nan=False)
    trace_value = context.to_dict()["original_damage"]
    assert isinstance(trace_value, str) and len(trace_value) == 5_001
    assert '"status": "applied"' in encoded


def test_negative_hostile_damage_is_an_explicit_noop_without_spending_shield():
    state, target, attacker, combat = _combat_target()
    target.shield = 7
    events_before = list(combat.event_stream)

    detail = combat._apply_hostile_damage(target, -4, source=attacker)

    assert detail["no_op"] is True
    assert detail["no_op_reason"] == "negative_damage_input"
    assert detail["actual_damage"] == detail["actual_life_loss"] == 0
    assert target.current_hp == 100
    assert target.shield == 7
    assert list(combat.event_stream) == events_before
    json.dumps(detail, ensure_ascii=False, allow_nan=False)


def test_status_ordering_rejects_noninteger_x_and_preserves_mainline_sequence_order():
    # activation_x is legacy metadata only; mainline ordering uses daowen_order.
    unanchored = StatusEffect("invalid-x", value=5, activation_x=float("inf"),
                              remaining_rounds=-1)
    anchored = StatusEffect("anchored", value=5, activation_x=float("inf"),
                            remaining_rounds=-1, daowen_order=3)
    assert unanchored.ordering_key == (1, 0)
    assert anchored.ordering_key == (0, 3)


def test_full_sealed_entity_roundtrip_preserves_mainline_sequence_metadata():
    entity = Entity("存档角色", "轮回者", blood_limit=100, current_hp=100)
    entity.add_status(StatusEffect("固执", value=1, remaining_rounds=2,
                                   source="固执", daowen_order=1))
    entity.add_status(StatusEffect("龙鳞", value=2, remaining_rounds=-1,
                                   source="龙鳞", daowen_order=0))
    api = GameEngine.__new__(GameEngine)

    snapshot = api._serialize_entity_full(entity)
    restored = api._deserialize_entity_full(snapshot)
    assert [(s.name, s.daowen_order) for s in restored.ordered_statuses()] == [
        (s.name, s.daowen_order) for s in entity.ordered_statuses()
    ]

    # 旧封存状态缺少道纹序列锚点时仍可加载，并回退到普通状态顺序。
    legacy = copy.deepcopy(snapshot)
    for status in legacy["status_effects"]:
        status.pop("daowen_order", None)
    legacy_restored = api._deserialize_entity_full(legacy)
    assert all(status.daowen_order is None for status in legacy_restored.status_effects)


def test_direct_entity_compatibility_path_still_applies_guzhi_once():
    target = Entity("T", "怪物", blood_limit=100, current_hp=100)
    target.add_status(StatusEffect("固执", value=1, remaining_rounds=1, source="test"))
    detail = target.take_damage(5)
    assert detail["actual_damage"] == 1
    assert detail["capped_by"] == "固执"
    assert target.current_hp == 99
