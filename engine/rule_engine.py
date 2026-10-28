"""可组合、可追踪的道纹数值规则执行器（第一阶段：受击伤害窗口）。

规则事实源同时驱动运行时执行和道纹摘要；未登记的道纹继续留在既有
DaoWenEngine/旧执行路径中。本模块不导入 Entity/CombatEngine，以避免
改变模型层依赖方向，也让运算符与上下文可独立测试。
"""
from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
import sys
from typing import Any, Callable, Iterable, Optional, TypeAlias


RuleNumber: TypeAlias = int | Fraction


class RuleError(ValueError):
    """规则定义、输入或执行过程无法安全解释。"""


class MissingRuleValueError(RuleError):
    """必需的规则输入缺失。"""


class DuplicateRuleExecutionError(RuleError):
    """同一结算上下文中同一规则被重复执行。"""


class RuleEvent(str, Enum):
    DAMAGE_RECEIVED = "damage_received"


class RuleStage(str, Enum):
    # 格挡已经吸收伤害；仍有数值时，持续状态在生命损失倍率/扣血之前执行。
    POST_BLOCK_PRE_LIFE_LOSS_MULTIPLIER = "post_block_pre_life_loss_multiplier"


class TargetRole(str, Enum):
    ATTACKER = "attacker"
    RECIPIENT = "recipient"
    OWNER = "owner"
    EVENT = "event"


class ValueRef(str, Enum):
    ORIGINAL_DAMAGE = "original_damage"
    INCOMING_DAMAGE = "incoming_damage"
    CURRENT_DAMAGE = "current_damage"
    SHIELD_ABSORBED = "shield_absorbed"
    POST_RULE_DAMAGE = "post_rule_damage"
    DAMAGE_AFTER_LIFE_LOSS_MULTIPLIER = "damage_after_life_loss_multiplier"
    ACTUAL_LIFE_LOSS = "actual_life_loss"
    HP_BEFORE = "hp_before"
    HP_AFTER = "hp_after"


class ReadSemantics(str, Enum):
    LATEST_VALUE = "latest_value"
    EVENT_SNAPSHOT = "event_snapshot"


class RuleOperator(str, Enum):
    ADD = "add"
    SUBTRACT = "subtract"
    MULTIPLY = "multiply"
    DIVIDE = "divide"
    MIN = "min"
    MAX = "max"
    CLAMP = "clamp"
    SCALE_PERCENT = "scale_percent"
    CONVERT = "convert"
    DEFER = "defer"
    CUSTOM = "custom"


class OperandSource(str, Enum):
    CONSTANT = "constant"
    STATUS_VALUE = "status_value"
    ACTIVATION_X = "activation_x"
    ORIGINAL_DAMAGE = "original_damage"
    CURRENT_DAMAGE = "current_damage"


class RoundingMode(str, Enum):
    CEIL = "ceil"
    FLOOR = "floor"
    HALF_UP = "half_up"
    TRUNCATE = "truncate"


class ConditionGroupMode(str, Enum):
    ALL = "all"
    ANY = "any"
    NOT = "not"


class ConditionKind(str, Enum):
    TARGET_HAS_STATUS = "target_has_status"
    ACTOR_HAS_STATUS = "actor_has_status"
    DAMAGE_TYPE_IS = "damage_type_is"
    DAMAGE_TYPE_NOT = "damage_type_not"
    VALUE_COMPARE = "value_compare"
    EVENT_TAG_PRESENT = "event_tag_present"


class CompareOperator(str, Enum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LE = "le"
    GT = "gt"
    GE = "ge"


class DurationKind(str, Enum):
    FINITE_X_ROUNDS = "finite_x_rounds"
    PERMANENT = "permanent"


@dataclass(frozen=True)
class ValueComparison:
    field: ValueRef
    operator: CompareOperator
    value: RuleNumber

    def __post_init__(self) -> None:
        if not isinstance(self.field, ValueRef) or not isinstance(self.operator, CompareOperator):
            raise RuleError("数值条件必须声明有效的 ValueRef 与 CompareOperator")
        _coerce_rule_number(self.value, field="condition comparison value")


@dataclass(frozen=True)
class RuleCondition:
    kind: ConditionKind
    value: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ConditionKind):
            raise RuleError("condition kind 必须是 ConditionKind")
        if self.kind in (ConditionKind.TARGET_HAS_STATUS, ConditionKind.ACTOR_HAS_STATUS,
                         ConditionKind.DAMAGE_TYPE_IS, ConditionKind.DAMAGE_TYPE_NOT,
                         ConditionKind.EVENT_TAG_PRESENT):
            if not isinstance(self.value, str) or not self.value:
                raise RuleError(f"{self.kind.value} 条件必须有非空字符串")
        elif self.kind == ConditionKind.VALUE_COMPARE and not isinstance(self.value, ValueComparison):
            raise RuleError("VALUE_COMPARE 必须包含 ValueComparison")


@dataclass(frozen=True)
class ConditionGroup:
    mode: ConditionGroupMode
    conditions: tuple["RuleCondition | ConditionGroup", ...]

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ConditionGroupMode):
            raise RuleError("condition group mode 必须是 ConditionGroupMode")
        if not isinstance(self.conditions, tuple) or not self.conditions:
            raise RuleError("condition group 必须包含至少一个子条件")
        if any(not isinstance(item, (RuleCondition, ConditionGroup))
               for item in self.conditions):
            raise RuleError("condition group 子条件类型无效")
        if self.mode == ConditionGroupMode.NOT and len(self.conditions) != 1:
            raise RuleError("NOT 条件组必须恰有一个子条件")


Condition: TypeAlias = RuleCondition | ConditionGroup


@dataclass(frozen=True)
class OperatorSpec:
    kind: RuleOperator
    operand: Optional[RuleNumber] = None
    lower_bound: Optional[RuleNumber] = None
    upper_bound: Optional[RuleNumber] = None
    rounding: RoundingMode = RoundingMode.CEIL
    # CONVERT：按稳定字符串键查表（"1", "1/2", "default"）；未命中保留原值，
    # 除非显式配置 default_value。
    conversion_map: tuple[tuple[str, RuleNumber], ...] = ()
    default_value: Optional[RuleNumber] = None
    # DEFER：只排入待处理队列；本阶段绝不递归触发事件。
    deferred_event: str = ""
    executor_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RuleOperator):
            raise RuleError("operator kind 必须是 RuleOperator")
        for label, value in (("operand", self.operand),
                             ("lower_bound", self.lower_bound),
                             ("upper_bound", self.upper_bound),
                             ("default_value", self.default_value)):
            if value is not None:
                _coerce_rule_number(value, field=f"operator {label}")
        for key, value in self.conversion_map:
            if not isinstance(key, str):
                raise RuleError("CONVERT 键必须是字符串")
            _coerce_rule_number(value, field="conversion_map value")
        if not isinstance(self.rounding, RoundingMode):
            raise RuleError("rounding 必须是 RoundingMode")
        if self.lower_bound is not None and self.upper_bound is not None:
            if self.lower_bound > self.upper_bound:
                raise RuleError("运算符下限不能大于上限")
        if self.kind == RuleOperator.CLAMP and (
            self.lower_bound is None and self.upper_bound is None
        ):
            raise RuleError("CLAMP 至少需要一个边界")
        if self.kind == RuleOperator.DEFER and not self.deferred_event:
            raise RuleError("DEFER 必须声明 deferred_event")
        if self.kind == RuleOperator.CUSTOM and not self.executor_id:
            raise RuleError("CUSTOM 必须声明 executor_id")


@dataclass(frozen=True)
class DamageRuleDefinition:
    rule_id: str
    daowen_id: str
    daowen_name: str
    status_name: str
    event: RuleEvent
    stage: RuleStage
    target_role: TargetRole
    reads: ValueRef
    read_semantics: ReadSemantics
    writes: ValueRef
    operator: OperatorSpec
    operand_source: OperandSource
    conditions: Condition
    priority: int
    missing_status_value: Optional[RuleNumber] = None

    def __post_init__(self) -> None:
        for label, value in (("rule_id", self.rule_id), ("daowen_id", self.daowen_id),
                             ("daowen_name", self.daowen_name), ("status_name", self.status_name)):
            if not isinstance(value, str) or not value.strip():
                raise RuleError(f"{label} 必须是非空字符串")
        if not isinstance(self.event, RuleEvent) or not isinstance(self.stage, RuleStage):
            raise RuleError("伤害规则 event/stage 类型无效")
        if not isinstance(self.target_role, TargetRole) or not isinstance(self.operand_source, OperandSource):
            raise RuleError("伤害规则 target_role/operand_source 类型无效")
        if not isinstance(self.read_semantics, ReadSemantics):
            raise RuleError("伤害规则 read_semantics 类型无效")
        if not isinstance(self.operator, OperatorSpec):
            raise RuleError("伤害规则 operator 必须是 OperatorSpec")
        if not isinstance(self.conditions, (RuleCondition, ConditionGroup)):
            raise RuleError("伤害规则 conditions 类型无效")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise RuleError("伤害规则 priority 必须是整数")
        if self.missing_status_value is not None:
            _coerce_rule_number(self.missing_status_value, field="missing_status_value")
        if self.reads != ValueRef.CURRENT_DAMAGE or self.writes != ValueRef.CURRENT_DAMAGE:
            # 第一阶段的唯一执行相位是受击伤害值链；其它值/阶段应通过新执行器扩展，
            # 不能悄悄把攻击者、生命值或实际失血混入当前伤害链。
            raise RuleError("第一阶段伤害规则必须读写 CURRENT_DAMAGE")
        if self.read_semantics != ReadSemantics.LATEST_VALUE:
            raise RuleError("伤害规则必须读取同一结算链最新当前值")

    def ordering_key(self, status: Any = None) -> tuple:
        """状态按道纹序列位置排序；缺少序列锚点时以 priority/rule_id 稳定后备。"""
        if status is not None:
            return (0, *status.ordering_key, self.priority, self.rule_id)
        return (1, self.priority, self.rule_id)

    def parameter(self, context: "DamageResolutionContext", status: Any) -> Optional[RuleNumber]:
        if self.operand_source == OperandSource.CONSTANT:
            return self.operator.operand
        if self.operand_source == OperandSource.STATUS_VALUE:
            recipient = context.recipient
            statuses = getattr(recipient, "status_effects", None)
            if statuses is not None:
                active = [item for item in statuses
                          if getattr(item, "name", None) == self.status_name
                          and not getattr(item, "is_expired", False)]
                if active:
                    # 与旧 Entity.get_status_value 契约一致：兼容存档里并存的
                    # 同名状态数值聚合；但先逐项校验，避免 sum(bool) 把 bool
                    # 悄悄变成 int，或让 None/float 混入结算。
                    values = [
                        _coerce_rule_number(getattr(item, "value", None),
                                            field=f"{self.status_name}.value")
                        for item in active
                    ]
                    raw = sum(values, 0)
                else:
                    raw = None
            else:
                getter = getattr(recipient, "get_status_value", None)
                if callable(getter):
                    raw = getter(self.status_name)
                else:
                    raw = getattr(status, "value", None) if status is not None else None
            if raw is None:
                if self.missing_status_value is None:
                    raise MissingRuleValueError(
                        f"规则 {self.rule_id} 缺少状态数值，且未定义缺省值")
                return _coerce_rule_number(
                    self.missing_status_value, field=f"{self.status_name}.default_value")
            return _coerce_rule_number(raw, field=f"{self.status_name}.value")
        if self.operand_source == OperandSource.ACTIVATION_X:
            raw = getattr(status, "ordering_x", None) if status is not None else None
            if raw is None:
                if self.missing_status_value is None:
                    raise MissingRuleValueError(
                        f"规则 {self.rule_id} 缺少发动 X，且未定义缺省值")
                return self.missing_status_value
            return _coerce_rule_number(raw, field=f"{self.status_name}.activation_x")
        if self.operand_source == OperandSource.ORIGINAL_DAMAGE:
            return context.original_damage
        if self.operand_source == OperandSource.CURRENT_DAMAGE:
            return context.current_damage
        raise RuleError(f"不支持的 operand source: {self.operand_source}")


@dataclass(frozen=True)
class DurationSpec:
    kind: DurationKind

    def value(self, x: int) -> int:
        return -1 if self.kind == DurationKind.PERMANENT else x

    def describe(self, x: str | int) -> str:
        if self.kind == DurationKind.PERMANENT:
            return "永久"
        return f"持续{x}回合"


@dataclass(frozen=True)
class DaoWenRuleDefinition:
    daowen_id: str
    name: str
    cost_type: str
    cost_multiplier: Fraction
    duration: DurationSpec
    rules: tuple[DamageRuleDefinition, ...]
    default_subject: str
    cost_unit: str = "法力"

    def __post_init__(self) -> None:
        if not self.daowen_id.strip() or not self.name.strip():
            raise RuleError("道纹定义必须有稳定 ID 和名称")
        if not self.rules:
            raise RuleError(f"{self.name} 至少需要一条规则")
        if any(rule.daowen_id != self.daowen_id or rule.daowen_name != self.name
               for rule in self.rules):
            raise RuleError(f"{self.name} 的子规则归属不一致")

    def cost(self, x: int) -> int:
        result = Fraction(x) * self.cost_multiplier
        if result.denominator != 1:
            raise RuleError(f"{self.name} 的代价不是整数：{result}")
        return int(result)

    def cost_text(self, x: str | int, *, include_unit: bool = False) -> str:
        amount = _format_scaled_x(self.cost_multiplier, x)
        if self.cost_type == "冷却":
            return f"冷却{amount}场"
        if self.cost_type == "消耗":
            unit = self.cost_unit if include_unit else ""
            return f"消耗{amount}{unit}"
        return f"{self.cost_type}{amount}"

    def effect_text(self, x: str | int = "X", target_name: Optional[str] = None) -> str:
        subject = self.default_subject if self.default_subject == "自身" else (target_name or "[目标]")
        pieces = [self._rule_text(rule, x, subject) for rule in self.rules]
        return "；".join(pieces)

    def _rule_text(self, rule: DamageRuleDefinition, x: str | int, subject: str) -> str:
        value_label = _value_label(rule.reads)
        operator = rule.operator
        # 2026-10-11 用户令：固执改为「你每次失去的生命不会超过1」，龙鳞描述简化。
        # 三条都不再复述「格挡后仍有伤害待结算的非代价受击」这一长串触发口径。
        # 主语按用户口径写「你」（default_subject 仍是「自身」，只在文案层改写）。
        subject = "你" if self.default_subject == "自身" else subject
        if rule.daowen_name == "固执":
            cap = _format_number(operator.operand if operator.operand is not None else 0)
            return f"{subject}每次失去的生命不会超过{cap}，{self.duration.describe(x)}"
        if rule.daowen_name == "龙鳞":
            bound = _format_number(operator.lower_bound) if operator.lower_bound is not None else "0"
            return f"{subject}每次受到伤害-{x}（最低为{bound}），{self.duration.describe(x)}"
        trigger = "每次格挡后仍有伤害待结算的非代价受击时"
        if operator.kind == RuleOperator.ADD:
            action = f"使{value_label}增加{x}"
        elif operator.kind == RuleOperator.SUBTRACT:
            if operator.lower_bound is not None:
                action = f"将{value_label}减少{x}（最低为{_format_number(operator.lower_bound)}）"
            else:
                action = f"将{value_label}减少{x}"
        elif operator.kind == RuleOperator.MIN:
            operand = _format_number(operator.operand if operator.operand is not None else 0)
            action = f"将{value_label}限制为不超过{operand}"
        elif operator.kind == RuleOperator.MAX:
            operand = _format_number(operator.operand if operator.operand is not None else 0)
            action = f"将{value_label}至少设为{operand}"
        else:
            action = f"按{operator.kind.value}规则变换{value_label}"
        return (f"{subject}{trigger}，{action}；该阶段后续规则读取更新后的当前值，"
                f"{self.duration.describe(x)}")

    def summary(self, x: int, target_name: Optional[str] = None) -> str:
        return f"{self.cost_text(x, include_unit=True)}，{self.effect_text(x=x, target_name=target_name)}"

    def migration_record(self) -> dict[str, str]:
        return {
            "status": "migrated",
            "reason": "由统一数值规则执行器驱动；结算值与描述共用此定义。",
        }


@dataclass
class DamageResolutionContext:
    """一笔伤害的规则结算上下文。

    字段严格区分攻击者、受击者、事件、输入/当前/最终伤害、格挡吸收、
    失血倍率后伤害、生命值变化与实际生命损失。preview 和 commit 各用一个
    context：预演不写实体、不消费状态、trace 明确分开。
    """

    event_id: str
    attacker: Any
    recipient: Any
    damage_type: str
    original_damage: int
    incoming_damage: int
    current_damage: int
    mode: str = "commit"
    stage: RuleStage = RuleStage.POST_BLOCK_PRE_LIFE_LOSS_MULTIPLIER
    shield_absorbed: int = 0
    post_rule_damage: Optional[int] = None
    damage_after_life_loss_multiplier: Optional[int] = None
    actual_life_loss: Optional[int] = None
    hp_before: Optional[int] = None
    hp_after: Optional[int] = None
    tags: frozenset[str] = frozenset()
    trace: list[dict[str, Any]] = field(default_factory=list)
    deferred_effects: list[dict[str, Any]] = field(default_factory=list)
    _executed_rule_ids: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise RuleError("DamageResolutionContext.event_id 必须是非空字符串")
        if not isinstance(self.damage_type, str) or not self.damage_type:
            raise RuleError("DamageResolutionContext.damage_type 必须是非空字符串")
        for field_name in ("original_damage", "incoming_damage", "current_damage", "shield_absorbed"):
            setattr(self, field_name, validate_damage_amount(getattr(self, field_name), field_name))
        for field_name in ("post_rule_damage", "damage_after_life_loss_multiplier",
                           "actual_life_loss", "hp_before", "hp_after"):
            value = getattr(self, field_name)
            if value is not None:
                setattr(self, field_name, validate_damage_amount(
                    value, field_name, allow_negative=False))
        if self.shield_absorbed < 0:
            raise RuleError("shield_absorbed 不得为负数")
        if not isinstance(self.stage, RuleStage):
            raise RuleError("DamageResolutionContext.stage 必须是 RuleStage")
        if self.mode not in {"preview", "commit"}:
            raise RuleError("DamageResolutionContext.mode 必须为 preview 或 commit")
        if any(not isinstance(tag, str) for tag in self.tags):
            raise RuleError("DamageResolutionContext.tags 只能包含字符串")

    def get_value(self, ref: ValueRef) -> Optional[int]:
        return {
            ValueRef.ORIGINAL_DAMAGE: self.original_damage,
            ValueRef.INCOMING_DAMAGE: self.incoming_damage,
            ValueRef.CURRENT_DAMAGE: self.current_damage,
            ValueRef.SHIELD_ABSORBED: self.shield_absorbed,
            ValueRef.POST_RULE_DAMAGE: self.post_rule_damage,
            ValueRef.DAMAGE_AFTER_LIFE_LOSS_MULTIPLIER: self.damage_after_life_loss_multiplier,
            ValueRef.ACTUAL_LIFE_LOSS: self.actual_life_loss,
            ValueRef.HP_BEFORE: self.hp_before,
            ValueRef.HP_AFTER: self.hp_after,
        }[ref]

    def set_current_damage(self, value: Any) -> None:
        self.current_damage = validate_damage_amount(value, "current_damage", allow_negative=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "mode": self.mode,
            "stage": self.stage.value,
            "attacker": getattr(self.attacker, "name", None),
            "recipient": getattr(self.recipient, "name", None),
            "damage_type": self.damage_type,
            "original_damage": _trace_number(self.original_damage),
            "incoming_damage": _trace_number(self.incoming_damage),
            "current_damage": _trace_number(self.current_damage),
            "shield_absorbed": _trace_number(self.shield_absorbed),
            "post_rule_damage": (_trace_number(self.post_rule_damage)
                                 if self.post_rule_damage is not None else None),
            "damage_after_life_loss_multiplier": (
                _trace_number(self.damage_after_life_loss_multiplier)
                if self.damage_after_life_loss_multiplier is not None else None),
            "actual_life_loss": (_trace_number(self.actual_life_loss)
                                  if self.actual_life_loss is not None else None),
            "hp_before": _trace_number(self.hp_before) if self.hp_before is not None else None,
            "hp_after": _trace_number(self.hp_after) if self.hp_after is not None else None,
            "rules": [dict(step) for step in self.trace],
            "deferred_effects": [dict(effect) for effect in self.deferred_effects],
        }


@dataclass(frozen=True)
class CustomOperatorResult:
    value: RuleNumber
    explanation: str

    def __post_init__(self) -> None:
        _coerce_rule_number(self.value, field="custom result")
        if not isinstance(self.explanation, str):
            raise RuleError("自定义运算解释必须是字符串")


@dataclass(frozen=True)
class RuleEntityView:
    """传给 CUSTOM 扩展的只读实体快照，不暴露可写 Entity 引用。"""

    name: Optional[str]
    entity_type: Optional[str]
    current_hp: Optional[int]
    blood_limit: Optional[int]
    shield: Optional[int]
    statuses: tuple[tuple[str, Any], ...] = ()

    def has_status(self, name: str) -> bool:
        return any(status_name == name for status_name, _ in self.statuses)

    def get_status_value(self, name: str) -> int:
        return sum(value for status_name, value in self.statuses
                   if status_name == name and isinstance(value, int) and not isinstance(value, bool))


def _rule_entity_view(entity: Any) -> Optional[RuleEntityView]:
    if entity is None:
        return None
    statuses = []
    for status in getattr(entity, "status_effects", ()):
        if getattr(status, "is_expired", False):
            continue
        value = getattr(status, "value", None)
        if value is None or type(value) in (str, int, float, bool):
            statuses.append((str(getattr(status, "name", "")), value))
    return RuleEntityView(
        name=getattr(entity, "name", None),
        entity_type=getattr(entity, "entity_type", None),
        current_hp=getattr(entity, "current_hp", None),
        blood_limit=getattr(entity, "blood_limit", None),
        shield=getattr(entity, "shield", None),
        statuses=tuple(statuses),
    )


CustomOperator = Callable[[RuleNumber, Optional[RuleNumber], OperatorSpec,
                           DamageResolutionContext, DamageRuleDefinition], CustomOperatorResult]
_CUSTOM_OPERATORS: dict[str, CustomOperator] = {}


def _custom_context_copy(context: DamageResolutionContext) -> DamageResolutionContext:
    """保留扩展回调的 context API，但隔离所有可变容器与实体引用。"""
    safe = copy(context)
    safe.attacker = _rule_entity_view(context.attacker)
    safe.recipient = _rule_entity_view(context.recipient)
    safe.trace = deepcopy(context.trace)
    safe.deferred_effects = deepcopy(context.deferred_effects)
    safe._executed_rule_ids = set(context._executed_rule_ids)
    return safe


def register_custom_operator(executor_id: str, executor: CustomOperator) -> None:
    """注册复杂规则运算扩展；注册键全局唯一，执行函数只能返回数值和解释。"""
    if not isinstance(executor_id, str) or not executor_id.strip():
        raise RuleError("自定义运算器 ID 不得为空，且必须是字符串")
    if not callable(executor):
        raise RuleError("自定义运算器必须是可调用对象")
    if executor_id in _CUSTOM_OPERATORS:
        raise RuleError(f"自定义运算器重复注册：{executor_id}")
    _CUSTOM_OPERATORS[executor_id] = executor


def validate_damage_amount(value: Any, field_name: str = "damage",
                           *, allow_negative: bool = True) -> int:
    """伤害入口契约：Python 有限精度整数有效；负数作为无伤害 no-op。

    None、bool、float（包括 NaN/±Infinity）和非整数一律拒绝，不做静默转型。
    不新增整数位数上限以免改变旧调用方的任意精度整数语义；规则 trace 的
    序列化字段会把超过运行时 JSON 转换阈值的整数安全表示成十进制字符串。
    状态持续时间的 -1「永久」不属于伤害数值，不经此函数。
    """
    if value is None:
        raise MissingRuleValueError(f"{field_name} 缺失；伤害数值不得为 None")
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise RuleError(f"{field_name} 不接受 NaN 或 ±Infinity；请使用有限整数")
        raise RuleError(f"{field_name} 必须为整数，收到 {type(value).__name__}")
    if not allow_negative and value < 0:
        raise RuleError(f"{field_name} 不得为负数")
    return value


def apply_final_life_loss_cap(amount: int, recipient: Any, damage_type: str) -> int:
    """应用位于所有失血倍率之后的最终生命损失边界。

    当前【固执】是消耗品【定命丸】施加的1回合状态：「你每次失去的生命不会超过1」，
    因此**代价路径与直接失血一并封顶**。旧口径（`damage_type != "代价"` 的排除，
    以及「格挡后仍有伤害待结算的非代价受击」这一触发前提）已废止。
    `damage_type` 参数保留是为了兼容既有调用点，本函数不再据它分流。
    本函数不修改实体状态。
    """
    amount = validate_damage_amount(
        amount, "final_life_loss_cap.amount", allow_negative=False)
    cap = int(GUZHI_RULE.operator.operand)
    if (amount > cap and recipient is not None
            and callable(getattr(recipient, "has_status", None))
            and recipient.has_status("固执")):
        return cap
    return amount


def _coerce_rule_number(value: Any, *, field: str) -> RuleNumber:
    if isinstance(value, bool) or not isinstance(value, (int, Fraction)):
        raise RuleError(f"{field} 必须是有限整数或有理数")
    return value


def _clamp(value: RuleNumber, spec: OperatorSpec) -> RuleNumber:
    if spec.lower_bound is not None:
        value = max(value, spec.lower_bound)
    if spec.upper_bound is not None:
        value = min(value, spec.upper_bound)
    return value


def _fraction_round(value: Fraction, mode: RoundingMode) -> int:
    if mode == RoundingMode.CEIL:
        return -(-value.numerator // value.denominator)
    if mode == RoundingMode.FLOOR:
        return value.numerator // value.denominator
    if mode == RoundingMode.TRUNCATE:
        return (abs(value.numerator) // value.denominator) * (1 if value >= 0 else -1)
    # HALF_UP: ties away from zero.
    sign = 1 if value >= 0 else -1
    magnitude = abs(value)
    whole, remainder = divmod(magnitude.numerator, magnitude.denominator)
    return sign * (whole + (1 if remainder * 2 >= magnitude.denominator else 0))


def _stable_number_key(value: RuleNumber) -> str:
    value = _coerce_rule_number(value, field="conversion value")
    if isinstance(value, Fraction) and value.denominator != 1:
        return f"{_int_to_decimal(value.numerator)}/{_int_to_decimal(value.denominator)}"
    return _int_to_decimal(int(value))


def apply_operator(value: RuleNumber, operand: Optional[RuleNumber], spec: OperatorSpec,
                   *, context: Optional[DamageResolutionContext] = None,
                   definition: Optional[DamageRuleDefinition] = None) -> CustomOperatorResult:
    """纯数值运算器；支持的运算均在此集中定义，规则组合由调用顺序决定。"""
    value = _coerce_rule_number(value, field="operator value")
    if operand is not None:
        operand = _coerce_rule_number(operand, field="operator operand")
    kind = spec.kind
    if kind == RuleOperator.ADD:
        if operand is None:
            raise MissingRuleValueError("ADD 缺少 operand")
        result = value + operand
        explanation = f"{_format_number(value)} + {_format_number(operand)} = {_format_number(result)}"
    elif kind == RuleOperator.SUBTRACT:
        if operand is None:
            raise MissingRuleValueError("SUBTRACT 缺少 operand")
        raw = value - operand
        result = _clamp(raw, spec)
        explanation = f"{_format_number(value)} - {_format_number(operand)} = {_format_number(result)}"
        if result != raw:
            explanation += f"（边界钳制：{_format_number(raw)} → {_format_number(result)}）"
    elif kind == RuleOperator.MULTIPLY:
        if operand is None:
            raise MissingRuleValueError("MULTIPLY 缺少 operand")
        result = _clamp(value * operand, spec)
        explanation = f"{_format_number(value)} × {_format_number(operand)} = {_format_number(result)}"
    elif kind == RuleOperator.DIVIDE:
        if operand is None:
            raise MissingRuleValueError("DIVIDE 缺少 operand")
        if operand == 0:
            raise RuleError("DIVIDE 的 operand 不得为0")
        result = _clamp(Fraction(value) / operand, spec)
        explanation = f"{_format_number(value)} ÷ {_format_number(operand)} = {_format_number(result)}"
    elif kind == RuleOperator.MIN:
        if operand is None:
            raise MissingRuleValueError("MIN 缺少 operand")
        result = min(value, operand)
        result = _clamp(result, spec)
        explanation = f"min({_format_number(value)}, {_format_number(operand)}) = {_format_number(result)}"
    elif kind == RuleOperator.MAX:
        if operand is None:
            raise MissingRuleValueError("MAX 缺少 operand")
        result = max(value, operand)
        result = _clamp(result, spec)
        explanation = f"max({_format_number(value)}, {_format_number(operand)}) = {_format_number(result)}"
    elif kind == RuleOperator.CLAMP:
        result = _clamp(value, spec)
        explanation = (
            f"clamp({_format_number(value)}, lower={_format_optional(spec.lower_bound)}, "
            f"upper={_format_optional(spec.upper_bound)}) = {_format_number(result)}"
        )
    elif kind == RuleOperator.SCALE_PERCENT:
        if operand is None:
            raise MissingRuleValueError("SCALE_PERCENT 缺少百分比 operand")
        exact = Fraction(value) * Fraction(operand) / 100
        rounded = _fraction_round(exact, spec.rounding)
        result = _clamp(rounded, spec)
        explanation = (
            f"{_format_number(value)} × {_format_number(operand)}% = "
            f"{_format_number(exact)} → {_format_number(result)}（{spec.rounding.value}）"
        )
    elif kind == RuleOperator.CONVERT:
        table = dict(spec.conversion_map)
        key = _stable_number_key(value)
        if key in table:
            result = table[key]
            explanation = f"convert({key}) = {_format_number(result)}"
        elif spec.default_value is not None:
            result = spec.default_value
            explanation = f"convert({key}) 未命中，使用默认值 {_format_number(result)}"
        else:
            result = value
            explanation = f"convert({key}) 未命中，保持原值 {_format_number(value)}"
    elif kind == RuleOperator.DEFER:
        if context is None or definition is None:
            raise RuleError("DEFER 需要规则上下文和规则定义")
        context.deferred_effects.append({
            "event": spec.deferred_event,
            "rule_id": definition.rule_id,
            "recipient": getattr(context.recipient, "name", None),
            "operand": _format_optional(operand),
            "cause_event_id": context.event_id,
        })
        result = value
        explanation = f"排入延后事件 {spec.deferred_event}；本次不递归触发"
    elif kind == RuleOperator.CUSTOM:
        if context is None or definition is None:
            raise RuleError("CUSTOM 需要规则上下文和规则定义")
        executor = _CUSTOM_OPERATORS.get(spec.executor_id)
        if executor is None:
            raise RuleError(f"未注册自定义运算器：{spec.executor_id}")
        custom = executor(value, operand, spec, _custom_context_copy(context), definition)
        if not isinstance(custom, CustomOperatorResult):
            raise RuleError("自定义运算器必须返回 CustomOperatorResult")
        result = _coerce_rule_number(custom.value, field="custom result")
        explanation = custom.explanation
    else:  # pragma: no cover - Enum 兜底
        raise RuleError(f"不支持的运算符：{kind}")
    return CustomOperatorResult(result, explanation)


_SAFE_INT_CHUNK_DIGITS = 500
_SAFE_INT_CHUNK_BASE = 10 ** _SAFE_INT_CHUNK_DIGITS


def _int_to_decimal(value: int) -> str:
    """不依赖 CPython 全局位数开关，将任意精度整数转换为十进制。"""
    if abs(value) < _SAFE_INT_CHUNK_BASE:
        return str(value)
    sign = "-" if value < 0 else ""
    remaining = abs(value)
    chunks = []
    while remaining:
        remaining, chunk = divmod(remaining, _SAFE_INT_CHUNK_BASE)
        chunks.append(chunk)
    head = str(chunks.pop())
    return sign + head + "".join(f"{chunk:0{_SAFE_INT_CHUNK_DIGITS}d}"
                                for chunk in reversed(chunks))


def _trace_integer(value: int) -> int | str:
    limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
    if limit and value.bit_length() * 30_103 // 100_000 + 1 >= limit - 32:
        return _int_to_decimal(value)
    return value


def _trace_number(value: RuleNumber) -> int | str:
    value = _coerce_rule_number(value, field="trace number")
    if isinstance(value, Fraction):
        if value.denominator != 1:
            return _format_number(value)
        return _trace_integer(value.numerator)
    return _trace_integer(value)


def _condition_evaluation(condition: Condition, context: DamageResolutionContext,
                          status: Any) -> tuple[bool, dict[str, Any]]:
    if isinstance(condition, ConditionGroup):
        if condition.mode == ConditionGroupMode.NOT and len(condition.conditions) != 1:
            raise RuleError("NOT 条件组必须恰有一个子条件")
        children = [_condition_evaluation(child, context, status)
                    for child in condition.conditions]
        values = [matched for matched, _ in children]
        if condition.mode == ConditionGroupMode.ALL:
            matched = all(values)
        elif condition.mode == ConditionGroupMode.ANY:
            matched = any(values)
        elif condition.mode == ConditionGroupMode.NOT:
            matched = not values[0]
        else:
            raise RuleError(f"未知条件组：{condition.mode}")
        return matched, {
            "kind": "group",
            "mode": condition.mode.value,
            "matched": matched,
            "children": [detail for _, detail in children],
        }
    if condition.kind in (ConditionKind.TARGET_HAS_STATUS, ConditionKind.ACTOR_HAS_STATUS):
        name = str(condition.value)
        entity = (context.recipient if condition.kind == ConditionKind.TARGET_HAS_STATUS
                  else context.attacker)
        role = "recipient" if condition.kind == ConditionKind.TARGET_HAS_STATUS else "attacker"
        if entity is None:
            observed = False
        else:
            has_status = getattr(entity, "has_status", None)
            if callable(has_status):
                observed = bool(has_status(name))
            else:
                observed = any(
                    getattr(item, "name", None) == name
                    and not getattr(item, "is_expired", False)
                    for item in getattr(entity, "status_effects", ())
                )
        return observed, {
            "kind": condition.kind.value,
            "role": role,
            "status": name,
            "observed": observed,
            "matched": observed,
        }
    if condition.kind == ConditionKind.DAMAGE_TYPE_IS:
        observed = context.damage_type == condition.value
        return observed, {
            "kind": condition.kind.value,
            "expected": str(condition.value),
            "observed": context.damage_type,
            "matched": observed,
        }
    if condition.kind == ConditionKind.DAMAGE_TYPE_NOT:
        observed = context.damage_type != condition.value
        return observed, {
            "kind": condition.kind.value,
            "expected_not": str(condition.value),
            "observed": context.damage_type,
            "matched": observed,
        }
    if condition.kind == ConditionKind.EVENT_TAG_PRESENT:
        tag = str(condition.value)
        observed = tag in context.tags
        return observed, {
            "kind": condition.kind.value,
            "tag": tag,
            "observed": observed,
            "matched": observed,
        }
    if condition.kind == ConditionKind.VALUE_COMPARE:
        if not isinstance(condition.value, ValueComparison):
            raise RuleError("VALUE_COMPARE 必须包含 ValueComparison")
        comparison = condition.value
        compared = context.get_value(comparison.field)
        if compared is None:
            raise MissingRuleValueError(
                f"条件读取的结算值 {comparison.field.value} 尚未产生")
        expected = comparison.value
        matched = {
            CompareOperator.EQ: lambda: compared == expected,
            CompareOperator.NE: lambda: compared != expected,
            CompareOperator.LT: lambda: compared < expected,
            CompareOperator.LE: lambda: compared <= expected,
            CompareOperator.GT: lambda: compared > expected,
            CompareOperator.GE: lambda: compared >= expected,
        }[comparison.operator]()
        return matched, {
            "kind": condition.kind.value,
            "field": comparison.field.value,
            "operator": comparison.operator.value,
            "expected": _trace_number(expected),
            "observed": _trace_number(compared),
            "matched": matched,
        }
    raise RuleError(f"不支持的条件：{condition.kind}")


def _condition_result(condition: Condition, context: DamageResolutionContext,
                      status: Any) -> bool:
    return _condition_evaluation(condition, context, status)[0]


def _condition_explanations(condition: Condition) -> list[str]:
    if isinstance(condition, ConditionGroup):
        separator = " 且 " if condition.mode == ConditionGroupMode.ALL else " 或 "
        parts = [part for child in condition.conditions for part in _condition_explanations(child)]
        if condition.mode == ConditionGroupMode.NOT:
            return ["非（" + (parts[0] if parts else "无条件") + "）"]
        return [separator.join(parts)] if parts else ["无条件"]
    labels = {
        ConditionKind.TARGET_HAS_STATUS: lambda: f"受击者具有【{condition.value}】",
        ConditionKind.ACTOR_HAS_STATUS: lambda: f"攻击者具有【{condition.value}】",
        ConditionKind.DAMAGE_TYPE_IS: lambda: f"伤害类型为【{condition.value}】",
        ConditionKind.DAMAGE_TYPE_NOT: lambda: f"伤害类型不是【{condition.value}】",
        ConditionKind.VALUE_COMPARE: lambda: f"满足数值条件 {condition.value}",
        ConditionKind.EVENT_TAG_PRESENT: lambda: f"事件带有标签 {condition.value}",
    }
    return [labels[condition.kind]()]


def _rule_skip_reason(definition: DamageRuleDefinition,
                      context: DamageResolutionContext) -> Optional[str]:
    if not isinstance(context.event_id, str) or not context.event_id.strip():
        raise RuleError("伤害规则需要稳定 event_id")
    if definition.event != RuleEvent.DAMAGE_RECEIVED:
        return "unsupported_event"
    if context.stage != definition.stage:
        return "stage_mismatch"
    if context.recipient is None:
        return "missing_recipient"
    return None


def rule_matches(definition: DamageRuleDefinition, context: DamageResolutionContext,
                 status: Any = None) -> bool:
    """执行与适配器共用的规则阶段/触发/条件判定。"""
    if _rule_skip_reason(definition, context) is not None:
        return False
    return _condition_result(definition.conditions, context, status)


def _status_trace_info(status: Any) -> Optional[dict[str, Any]]:
    if status is None:
        return None
    ordering_x = getattr(status, "ordering_x", 0)
    if isinstance(ordering_x, bool) or not isinstance(ordering_x, int):
        ordering_x = 0
    return {
        "status_name": str(getattr(status, "name", "")),
        "ordering_x": _trace_number(ordering_x),
        # 绝对施加序号是进程全局计数器，不能放进需要可复现的战报；
        # trace 列表顺序本身记录了同 X 的实际施加先后。
        "same_x_tiebreak": "application_order",
    }


class DamageRuleExecutor:
    """确定性规则执行器：一次 context 内每条 rule_id 最多执行一次。"""

    def execute(self, definition: DamageRuleDefinition, context: DamageResolutionContext,
                status: Any = None) -> int:
        if definition.rule_id in context._executed_rule_ids:
            raise DuplicateRuleExecutionError(
                f"事件 {context.event_id} 的 {context.mode} 上下文中规则 "
                f"{definition.rule_id} 已执行；预演和正式结算必须用独立上下文")

        reason = _rule_skip_reason(definition, context)
        before = context.current_damage
        condition_trace: Optional[dict[str, Any]] = None
        if reason is None:
            matched, condition_trace = _condition_evaluation(
                definition.conditions, context, status)
            if not matched:
                reason = "condition_not_met"

        if reason is not None:
            context.trace.append({
                "mode": context.mode,
                "status": "skipped",
                "reason": reason,
                "rule_id": definition.rule_id,
                "daowen_id": definition.daowen_id,
                "daowen": definition.daowen_name,
                "stage": definition.stage.value,
                "before": _trace_number(before),
                "after": _trace_number(before),
                "condition_trace": condition_trace,
                "explanation": (
                    f"{definition.daowen_name}：未执行（{reason}）；"
                    f"当前值保持 {_format_number(before)}"
                ),
            })
            context._executed_rule_ids.add(definition.rule_id)
            return before

        read_value = context.get_value(definition.reads)
        if read_value is None:
            raise MissingRuleValueError(f"规则 {definition.rule_id} 读取的数值缺失")
        before = read_value
        operand = definition.parameter(context, status)

        # 运算和副作用先在隔离副本中准备；参数/运算/trace 构造任一步失败都
        # 不留下“已执行”标记、不改当前值，也不提交 DEFER/trace 半成品。
        operator_context = copy(context)
        operator_context.trace = []
        operator_context.deferred_effects = []
        operator_context._executed_rule_ids = set(context._executed_rule_ids)
        result = apply_operator(
            read_value, operand, definition.operator,
            context=operator_context, definition=definition,
        )
        number = _coerce_rule_number(result.value, field="rule result")
        if isinstance(number, Fraction):
            after = _fraction_round(number, definition.operator.rounding)
        else:
            after = number
        after = validate_damage_amount(after, f"rule {definition.rule_id} result")
        conditions = " 且 ".join(_condition_explanations(definition.conditions))
        result_explanation = result.explanation
        if Fraction(after) != Fraction(number):
            result_explanation += (
                f"；伤害写回需为整数，按 {definition.operator.rounding.value} "
                f"舍入为 {after}"
            )
        entry = {
            "mode": context.mode,
            "status": "applied",
            "rule_id": definition.rule_id,
            "daowen_id": definition.daowen_id,
            "daowen": definition.daowen_name,
            "stage": definition.stage.value,
            "target_role": definition.target_role.value,
            "source_status": _status_trace_info(status),
            "read": definition.reads.value,
            "read_semantics": definition.read_semantics.value,
            "write": definition.writes.value,
            "operator": definition.operator.kind.value,
            "rounding": definition.operator.rounding.value,
            "operand_source": definition.operand_source.value,
            "operand": _format_optional(operand),
            "before": _trace_number(before),
            "after": _trace_number(after),
            "conditions": conditions,
            "condition_trace": condition_trace,
            "explanation": (
                f"{definition.daowen_name}：条件成立（{conditions}）；"
                f"读取当前值 {_format_number(before)}；{result_explanation}；"
                f"写回当前值 {_format_number(after)}"
            ),
        }

        # 以上已完成所有可能失败的规则工作；以下为唯一提交点。
        context.set_current_damage(after)
        context.deferred_effects.extend(operator_context.deferred_effects)
        context.trace.append(entry)
        context._executed_rule_ids.add(definition.rule_id)
        return after


RULE_EXECUTOR = DamageRuleExecutor()


def _damage_rule(rule_id: str, daowen_id: str, name: str, status_name: str,
                 operator: OperatorSpec, operand_source: OperandSource,
                 priority: int, *, missing_status_value: Optional[int] = 0) -> DamageRuleDefinition:
    return DamageRuleDefinition(
        rule_id=rule_id,
        daowen_id=daowen_id,
        daowen_name=name,
        status_name=status_name,
        event=RuleEvent.DAMAGE_RECEIVED,
        stage=RuleStage.POST_BLOCK_PRE_LIFE_LOSS_MULTIPLIER,
        target_role=TargetRole.RECIPIENT,
        reads=ValueRef.CURRENT_DAMAGE,
        read_semantics=ReadSemantics.LATEST_VALUE,
        writes=ValueRef.CURRENT_DAMAGE,
        operator=operator,
        operand_source=operand_source,
        conditions=ConditionGroup(ConditionGroupMode.ALL, (
            RuleCondition(ConditionKind.DAMAGE_TYPE_NOT, "代价"),
            RuleCondition(ConditionKind.TARGET_HAS_STATUS, status_name),
        )),
        priority=priority,
        missing_status_value=missing_status_value,
    )


# 【加害】已转为消耗品【灼罪钉】。此规则仅结算消耗品施加的「加害」状态，不再代表可发动道纹。
JIAHAI_RULE = _damage_rule(
    "item.jiazuoding.incoming_damage.add", "item.jiazuoding", "加害", "加害",
    OperatorSpec(RuleOperator.ADD), OperandSource.STATUS_VALUE, 20,
)
STATUS_DAMAGE_RULES = (JIAHAI_RULE,)
# 旧减法规则仅保留给独立规则执行器的兼容/代数测试；不注册、不授予、不参与龙鳞遗物结算。
LONG_LIN_RULE = _damage_rule(
    "daowen.longlin.incoming_damage.subtract", "daowen.longlin", "龙鳞", "龙鳞",
    OperatorSpec(RuleOperator.SUBTRACT, lower_bound=0), OperandSource.STATUS_VALUE, 30,
)
GUZHI_RULE = _damage_rule(
    "daowen.guzhi.incoming_damage.cap", "daowen.guzhi", "固执", "固执",
    OperatorSpec(RuleOperator.MIN, operand=1), OperandSource.CONSTANT, 40,
    missing_status_value=None,
)

# 固执只作为消耗品【定命丸】施加的状态，不再是可发动道纹。
STATUS_DAMAGE_RULES = (JIAHAI_RULE, GUZHI_RULE)
DAOWEN_RULES: dict[str, DaoWenRuleDefinition] = {}

RULES_BY_ID: dict[str, DamageRuleDefinition] = {
    rule.rule_id: rule
    for rule in (
        [rule for daowen in DAOWEN_RULES.values() for rule in daowen.rules]
        + list(STATUS_DAMAGE_RULES)
    )
}
if len(RULES_BY_ID) != (
        sum(len(defn.rules) for defn in DAOWEN_RULES.values()) + len(STATUS_DAMAGE_RULES)):
    raise RuleError("规则 ID 重复")

UNMIGRATED_REASON = (
    "本阶段未迁移；继续使用 DaoWenEngine 与原执行路径，行为保持不变。"
    "不得在旧路径与统一规则路径中同时改写同一结算值。"
)


def get_daowen_rule(name: str) -> Optional[DaoWenRuleDefinition]:
    return DAOWEN_RULES.get(name)


def daowen_migration_status(name: str) -> dict[str, str]:
    definition = get_daowen_rule(name)
    if definition is not None:
        return definition.migration_record()
    return {"status": "legacy", "reason": UNMIGRATED_REASON}


def validate_migration_coverage(daowen_names: Iterable[str]) -> dict[str, dict[str, str]]:
    """按 DaoWenEngine 名册逐项返回显式迁移状态；不自动迁移未登记项。"""
    return {name: daowen_migration_status(name) for name in sorted(set(daowen_names))}


def validate_rule_context_input(amount: Any, field_name: str = "damage") -> int:
    """仅开放给伤害入口；负整数保留为 no-op，非有限/缺失值明确拒绝。"""
    return validate_damage_amount(amount, field_name, allow_negative=True)


def render_rule_description(name: str, x: str | int = "X",
                            target_name: Optional[str] = None) -> str:
    definition = get_daowen_rule(name)
    if definition is None:
        raise KeyError(f"道纹【{name}】尚未迁移到统一数值规则系统")
    return definition.effect_text(x=x, target_name=target_name)


def _value_label(ref: ValueRef) -> str:
    return {
        ValueRef.ORIGINAL_DAMAGE: "原始伤害",
        ValueRef.INCOMING_DAMAGE: "上游调整后的伤害",
        ValueRef.CURRENT_DAMAGE: "当前待结算伤害",
        ValueRef.SHIELD_ABSORBED: "格挡吸收量",
        ValueRef.POST_RULE_DAMAGE: "规则处理后伤害",
        ValueRef.DAMAGE_AFTER_LIFE_LOSS_MULTIPLIER: "失血倍率后伤害",
        ValueRef.ACTUAL_LIFE_LOSS: "实际失去生命",
        ValueRef.HP_BEFORE: "受击前生命值",
        ValueRef.HP_AFTER: "受击后生命值",
    }[ref]


def _format_scaled_x(multiplier: Fraction, x: str | int) -> str:
    if isinstance(x, str):
        coefficient = _format_number(multiplier)
        return x if multiplier == 1 else f"{coefficient}{x}"
    value = Fraction(x) * multiplier
    return _format_number(value)


def _format_number(value: Optional[RuleNumber]) -> str:
    if value is None:
        return "缺失"
    value = _coerce_rule_number(value, field="format value")
    if isinstance(value, Fraction) and value.denominator != 1:
        return f"{_int_to_decimal(value.numerator)}/{_int_to_decimal(value.denominator)}"
    return _int_to_decimal(int(value))


def _format_optional(value: Optional[RuleNumber]) -> str:
    return _format_number(value)
