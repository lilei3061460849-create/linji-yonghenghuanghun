"""spell_dsl 单元测试：触发时机词汇表 / 条件表达式 / 效果流程解析 / 循环标记。

这些测试独立于战斗引擎，只验证"文本 → 结构化 AST"这一层的正确性，
覆盖用户提出的四点诉求：
1. 触发时机不再限定三选一，兼容更多"XX时"写法；
2. 句式错误必须报错（SpellDslError），不能静默解析失败；
3. 条件分支（若...则...否则...，且/或/非/嵌套）能被正确解析与求值；
4. 效果流程可显式声明目标（自身/攻击者/目标/施法者/任意目标）与循环标记。
"""
import pytest

from engine.spell_dsl import (
    SpellDslError, parse_trigger, parse_condition, evaluate_condition,
    parse_effect_flow, parse_spell_definition, ActionStep, IfStep, LoopStep,
    BoolOp, Not, Cmp, collect_step_daowen, iter_action_steps, count_action_steps,
    TRIGGER_BEFORE_DAMAGE, TRIGGER_AFTER_DAMAGE, TRIGGER_BEFORE_LIFE_LOST,
    TRIGGER_AFTER_LIFE_LOST, TRIGGER_TARGET_BEFORE_DAOWEN,
    TRIGGER_BATTLE_START, TRIGGER_BATTLE_END, TRIGGER_ROUND_START,
    TRIGGER_ROUND_END, TRIGGER_ENEMY_ROUND_START, TRIGGER_ENEMY_ROUND_END,
)


# ========================================================================
# 一、触发时机：兼容更多写法
# ========================================================================

@pytest.mark.parametrize("text,expected", [
    ("受到伤害前", TRIGGER_BEFORE_DAMAGE),
    ("我方受到伤害前", TRIGGER_BEFORE_DAMAGE),
    ("受到攻击前", TRIGGER_BEFORE_DAMAGE),
    ("受到伤害后", TRIGGER_AFTER_DAMAGE),
    ("失去生命后", TRIGGER_AFTER_LIFE_LOST),
    ("失去生命后（循环）", TRIGGER_AFTER_LIFE_LOST),
    ("掉血后", TRIGGER_AFTER_LIFE_LOST),
    ("失去生命前", TRIGGER_BEFORE_LIFE_LOST),
    ("生命减少前", TRIGGER_BEFORE_LIFE_LOST),
    ("目标发动道纹前", TRIGGER_TARGET_BEFORE_DAOWEN),
    ("对方发动道纹前", TRIGGER_TARGET_BEFORE_DAOWEN),
    ("战始", TRIGGER_BATTLE_START),
    ("战斗开始时", TRIGGER_BATTLE_START),
    ("开局时", TRIGGER_BATTLE_START),
    ("战终", TRIGGER_BATTLE_END),
    ("战斗结束时", TRIGGER_BATTLE_END),
    ("回始", TRIGGER_ROUND_START),
    ("每回合开始时", TRIGGER_ROUND_START),
    ("回合开始时", TRIGGER_ROUND_START),
    ("回终", TRIGGER_ROUND_END),
    ("回合结束时", TRIGGER_ROUND_END),
    ("敌回始", TRIGGER_ENEMY_ROUND_START),
    ("敌方回合开始时", TRIGGER_ENEMY_ROUND_START),
    ("敌回终", TRIGGER_ENEMY_ROUND_END),
    ("敌方回合结束时", TRIGGER_ENEMY_ROUND_END),
])
def test_parse_trigger_accepts_wide_vocabulary(text, expected):
    assert parse_trigger(text) == expected


def test_parse_trigger_rejects_garbage_with_helpful_message():
    with pytest.raises(SpellDslError) as exc:
        parse_trigger("我心情好的时候")
    assert "无法识别触发条件" in str(exc.value)


def test_parse_trigger_rejects_empty():
    with pytest.raises(SpellDslError):
        parse_trigger("")
    with pytest.raises(SpellDslError):
        parse_trigger("   ")


def test_round_start_and_enemy_round_start_are_distinguished():
    """"回合开始"和"敌回合开始"不能被互相误判——这是本次修复的关键边界。"""
    assert parse_trigger("回合开始时") == TRIGGER_ROUND_START
    assert parse_trigger("敌方回合开始时") == TRIGGER_ENEMY_ROUND_START
    assert parse_trigger("回合结束时") == TRIGGER_ROUND_END
    assert parse_trigger("敌方回合结束时") == TRIGGER_ENEMY_ROUND_END


# ========================================================================
# 二、条件表达式：且/或/非/嵌套/比较
# ========================================================================

def _resolver(values: dict):
    def _r(subject, field):
        return values[(subject, field)]
    return _r


def test_condition_simple_comparison():
    node = parse_condition("自身 生命 小于 50")
    assert evaluate_condition(node, _resolver({("self", "hp"): 30})) is True
    assert evaluate_condition(node, _resolver({("self", "hp"): 80})) is False


def test_condition_and_or_not_precedence():
    # (a 且 b) 或 非 c
    node = parse_condition("自身 生命 小于 50 且 自身 法力 大于等于 10 或 非 目标 护盾 大于 0")
    assert isinstance(node, BoolOp) and node.op == "or"

    values_true_via_and = {("self", "hp"): 30, ("self", "mana"): 20, ("target", "shield"): 5}
    assert evaluate_condition(node, _resolver(values_true_via_and)) is True

    values_true_via_not = {("self", "hp"): 80, ("self", "mana"): 20, ("target", "shield"): 0}
    assert evaluate_condition(node, _resolver(values_true_via_not)) is True

    values_false = {("self", "hp"): 80, ("self", "mana"): 1, ("target", "shield"): 5}
    assert evaluate_condition(node, _resolver(values_false)) is False


def test_condition_parentheses_nesting():
    node = parse_condition("(自身 生命 小于 50 或 自身 法力 小于 5) 且 目标 护盾 等于 0")
    assert evaluate_condition(node, _resolver(
        {("self", "hp"): 30, ("self", "mana"): 99, ("target", "shield"): 0})) is True
    assert evaluate_condition(node, _resolver(
        {("self", "hp"): 90, ("self", "mana"): 99, ("target", "shield"): 0})) is False


def test_condition_status_has_lacks():
    node_has = parse_condition("目标 拥有 飞行")
    node_lacks = parse_condition("目标 没有 飞行")
    assert evaluate_condition(node_has, _resolver({("target", ("status", "飞行")): True})) is True
    assert evaluate_condition(node_lacks, _resolver({("target", ("status", "飞行")): False})) is True


def test_condition_daowen_stacks():
    node = parse_condition("自身 杀伐层数 大于 2")
    assert evaluate_condition(node, _resolver({("self", ("daowen_stacks", "杀伐")): 3})) is True
    assert evaluate_condition(node, _resolver({("self", ("daowen_stacks", "杀伐")): 1})) is False


def test_condition_rejects_illegal_subject():
    with pytest.raises(SpellDslError):
        parse_condition("路人甲 生命 小于 50")


def test_condition_rejects_illegal_field():
    with pytest.raises(SpellDslError):
        parse_condition("自身 智商 小于 50")


def test_condition_rejects_non_integer_value():
    with pytest.raises(SpellDslError):
        parse_condition("自身 生命 小于 很多")


def test_condition_rejects_unclosed_parenthesis():
    with pytest.raises(SpellDslError):
        parse_condition("(自身 生命 小于 50 且 自身 法力 大于 5")


# ========================================================================
# 三、效果流程：目标声明 / 条件分支 / 循环
# ========================================================================

KNOWN = {"杀伐", "再生", "透支", "庇护", "连击", "坠落"}


def test_effect_flow_requires_explicit_target():
    with pytest.raises(SpellDslError) as exc:
        parse_effect_flow("发动杀伐X", KNOWN)
    assert "缺少显式目标声明" in str(exc.value)


def test_effect_flow_parses_explicit_targets():
    steps, loop = parse_effect_flow("发动杀伐X于攻击者→发动再生X于自身", KNOWN)
    assert loop is False
    assert steps == [ActionStep("杀伐", "attacker"), ActionStep("再生", "self")]


def test_effect_flow_any_target_allowed():
    steps, loop = parse_effect_flow("发动杀伐X于任意目标", KNOWN)
    assert steps == [ActionStep("杀伐", "any")]


def test_effect_flow_rejects_unknown_daowen():
    with pytest.raises(SpellDslError) as exc:
        parse_effect_flow("发动回复X于自身", KNOWN)
    assert "不存在的道纹" in str(exc.value)


def test_effect_flow_rejects_unknown_target_word():
    with pytest.raises(SpellDslError) as exc:
        parse_effect_flow("发动杀伐X于路人", KNOWN)
    assert "非法" in str(exc.value)


def test_effect_flow_loop_marker():
    """循环标记产出 LoopStep：控制流归执行器，不再预展开。"""
    steps, loop = parse_effect_flow("发动连击X于攻击者→循环直到法力耗尽", KNOWN)
    assert loop is True
    assert steps == [LoopStep(body=(ActionStep("连击", "attacker"),), max_iterations=None)]

    steps2, loop2 = parse_effect_flow("发动再生X于自身→发动连击X于攻击者→循环", KNOWN)
    assert loop2 is True
    assert steps2 == [LoopStep(body=(ActionStep("再生", "self"),
                                     ActionStep("连击", "attacker")), max_iterations=None)]


def test_effect_flow_fixed_count_loop():
    """支持"循环N次"的定次循环（N≥1）。"""
    steps, loop = parse_effect_flow("发动再生X于自身→循环3次", KNOWN)
    assert loop is True
    assert steps == [LoopStep(body=(ActionStep("再生", "self"),), max_iterations=3)]


def test_effect_flow_nested_conditional_branch():
    """条件分支可嵌套，且执行顺序按深度优先展开成决策槽位。"""
    steps, loop = parse_effect_flow(
        "若自身 生命 小于 50 则（若自身 法力 大于 2 则 发动杀伐X于攻击者 "
        "否则 发动再生X于自身）否则 发动连击X于攻击者", KNOWN)
    branch = steps[0]
    assert isinstance(branch, IfStep)
    inner = branch.then_steps[0]
    assert isinstance(inner, IfStep)
    assert inner.then_steps == (ActionStep("杀伐", "attacker"),)
    assert inner.else_steps == (ActionStep("再生", "self"),)
    assert branch.else_steps == (ActionStep("连击", "attacker"),)
    indices = [(i, s.daowen) for i, s in iter_action_steps(steps)]
    assert indices == [(0, "杀伐"), (1, "再生"), (2, "连击")]
    assert count_action_steps(steps) == 3


def test_effect_flow_conditional_branch():
    steps, loop = parse_effect_flow(
        "若自身 生命 小于 50 则 发动再生X于自身 否则 发动杀伐X于攻击者", KNOWN)
    assert loop is False
    assert len(steps) == 1
    branch = steps[0]
    assert isinstance(branch, IfStep)
    assert branch.then_steps == (ActionStep("再生", "self"),)
    assert branch.else_steps == (ActionStep("杀伐", "attacker"),)


def test_effect_flow_conditional_branch_without_else():
    steps, _ = parse_effect_flow("若目标 拥有 飞行 则 发动坠落X于目标", KNOWN)
    branch = steps[0]
    assert branch.else_steps == ()


def test_effect_flow_conditional_then_multiple_actions():
    steps, _ = parse_effect_flow(
        "若自身 法力 大于等于 10 则 发动杀伐X于攻击者；发动再生X于自身", KNOWN)
    branch = steps[0]
    assert branch.then_steps == (ActionStep("杀伐", "attacker"), ActionStep("再生", "self"))


def test_blood_splash_uses_mana_branch_before_loop():
    """【血溅五步】：法力够时先杀伐再透支，不够时跳过杀伐保留再生/透支闭环。"""
    flow = (
        "若自身 法力 大于等于 2 则 "
        "发动再生X于自身；发动杀伐X于攻击者；发动透支X于自身 "
        "否则 发动再生X于自身；发动透支X于自身→循环"
    )
    parsed = parse_spell_definition("失去生命后", flow, KNOWN)
    assert parsed.loop is True
    assert len(parsed.steps) == 1 and isinstance(parsed.steps[0], LoopStep)
    branch = parsed.steps[0].body[0]
    assert isinstance(branch, IfStep)
    assert [step.daowen for step in branch.then_steps] == ["再生", "杀伐", "透支"]
    assert [step.daowen for step in branch.else_steps] == ["再生", "透支"]


def test_effect_flow_mixed_branch_and_plain_steps():
    steps, loop = parse_effect_flow(
        "发动庇护X于自身→若目标 拥有 飞行 则 发动坠落X于目标→发动杀伐X于攻击者→循环", KNOWN)
    assert loop is True
    assert len(steps) == 1 and isinstance(steps[0], LoopStep)
    body = steps[0].body
    assert len(body) == 3
    assert isinstance(body[0], ActionStep)
    assert isinstance(body[1], IfStep)
    assert isinstance(body[2], ActionStep)


def test_effect_flow_rejects_empty():
    with pytest.raises(SpellDslError):
        parse_effect_flow("", KNOWN)
    with pytest.raises(SpellDslError):
        parse_effect_flow("循环", KNOWN)  # 去掉循环标记后为空


def test_collect_step_daowen_includes_branches():
    steps, _ = parse_effect_flow(
        "发动庇护X于自身→若目标 拥有 飞行 则 发动坠落X于目标 否则 发动杀伐X于攻击者", KNOWN)
    assert collect_step_daowen(steps) == {"庇护", "坠落", "杀伐"}


# ========================================================================
# 四、统一入口 parse_spell_definition
# ========================================================================

def test_parse_spell_definition_end_to_end():
    parsed = parse_spell_definition(
        "我方受到伤害前",
        "若自身 生命 小于 50 则 发动再生X于自身 否则 发动杀伐X于攻击者→循环",
        KNOWN,
    )
    assert parsed.trigger == TRIGGER_BEFORE_DAMAGE
    assert parsed.loop is True
    assert len(parsed.steps) == 1


def test_parse_spell_definition_propagates_trigger_error():
    with pytest.raises(SpellDslError):
        parse_spell_definition("莫名其妙的时机", "发动杀伐X于攻击者", KNOWN)


def test_parse_spell_definition_propagates_flow_error():
    with pytest.raises(SpellDslError):
        parse_spell_definition("受到伤害前", "发动杀伐X", KNOWN)  # 缺目标
