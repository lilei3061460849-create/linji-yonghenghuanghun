"""Mechanism：机制声明与注册表。

机制 = 【什么时候】when + 【对谁】target + 【满足什么条件】condition +
       【做什么】effect + priority。机制定义全局唯一。

机制自身状态（state）必须按实体存放：entity → mechanism state。
复用现有 Entity 结构（entity._mechanism_states[机制名]），
不做 JilinMechanism.states[entity_id] 这类全局状态系统。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from .triggers import Phase, Trigger, TriggerContext


@dataclass
class Mechanism:
    name: str
    when: Trigger
    effect: Callable[[TriggerContext, list], Any] = None  # type: ignore[assignment]
    # 相位修正机制返回 {"amount": 新值} 或 int（新值）；事件机制返回值仅作记录。
    target: Any = None                # TargetSelector；None = 不产生目标（纯数值修正）
    condition: Optional[Callable[[TriggerContext], bool]] = None
    priority: int = 100               # 没有持续状态排序锚点时的兼容后备顺序。
    needs_state: bool = False         # 需要按实体的机制自身状态时置 True
    # 机制由某个持续状态发动/维持时，显式写其状态名。空串表示遗物或纯事件机制，
    # 仍按 priority；不再从展示名猜测，避免「畸变·结算」等名字歧义。
    status_name: str = ""
    before_status_rules: bool = False  # 固定遗物减伤先于道纹状态链，不受道纹排序重排。
    status_owner: str = "target"   # target | source；事件机制可由施放者身上的状态驱动
    # 数值规则迁移期适配器：定义是数值/条件/文案的唯一事实源；该字段不改变
    # 非数值 Mechanism 的既有声明与生命周期。
    rule_definition: Any = None

    def ordering_key(self, entity) -> tuple:
        """返回此机制在 entity 的当前自然结算窗口中的通用顺序键。"""
        if self.status_name and entity is not None:
            status = next((s for s in getattr(entity, "status_effects", ())
                           if s.name == self.status_name and not s.is_expired), None)
            # 只有显式道纹序列位置才构成锚点；旧状态/测试状态没有位置时
            # 必须退回 priority，不能无条件压过无状态机制。
            if status is not None and status.daowen_order is not None:
                return (0, *status.ordering_key, self.priority, self.name)
        return (1, self.priority, self.name)

    def __post_init__(self):
        if self.effect is None:
            raise ValueError(f"机制[{self.name}]缺少 effect")
        if (self.rule_definition is not None
                and self.rule_definition.daowen_name != self.name):
            raise ValueError(f"机制[{self.name}]与数值规则定义归属不一致")

    def state_of(self, entity) -> dict:
        """本机制在该实体上的自身状态（惰性创建，按实体存放，不进入全局表）。

        仅 needs_state=True 的机制应使用；状态生命周期与实体一致，
        序列化兼容期内不会写入存档（机制状态需随存档走时另行设计）。
        """
        if entity is None:
            raise ValueError(f"机制[{self.name}]需要实体状态，但实体为 None")
        bucket = getattr(entity, "_mechanism_states", None)
        if bucket is None:
            bucket = {}
            entity._mechanism_states = bucket
        if self.name not in bucket:
            bucket[self.name] = {}
        return bucket[self.name]


class MechanismRegistry:
    """全局机制定义登记处。定义全局唯一；状态走实体（见 Mechanism.state_of）。"""

    def __init__(self):
        self._mechanisms: dict[str, Mechanism] = {}

    def register(self, mechanism: Mechanism) -> Mechanism:
        existing = self._mechanisms.get(mechanism.name)
        if existing is not None and existing is not mechanism:
            raise ValueError(f"机制[{mechanism.name}]重复注册")
        self._mechanisms[mechanism.name] = mechanism
        return mechanism

    def get(self, name: str) -> Optional[Mechanism]:
        return self._mechanisms.get(name)

    def names(self) -> list[str]:
        return sorted(self._mechanisms)

    def all(self) -> list[Mechanism]:
        return [self._mechanisms[n] for n in sorted(self._mechanisms)]

    def event_mechanisms(self) -> list[Mechanism]:
        """全部事件型机制定义，按 priority 升序（供战斗引擎订阅事件总线）。"""
        return sorted(
            (m for m in self._mechanisms.values() if m.when.kind == "event"),
            key=lambda m: m.priority,
        )

    def unregister(self, name: str) -> Optional[Mechanism]:
        """从登记处移除一个机制定义（返回被移除的机制；不存在返回 None）。"""
        return self._mechanisms.pop(name, None)

    def phase_mechanisms(self, phase: str) -> list[Mechanism]:
        """某相位的全部机制，按 priority 升序（同 priority 保持注册顺序）。"""
        return sorted(
            (m for m in self._mechanisms.values() if m.when.matches_phase(phase)),
            key=lambda m: m.priority,
        )


# 全局机制定义登记处（机制定义属于全局词汇；机制状态永远走实体，不进这里）。
MECHANISMS = MechanismRegistry()


class MechanismHookAdapter:
    """【迁移过渡代码，不得新增依赖】

    已迁移 Mechanism 在既有 CombatHookManager 分发路径上的执行壳。
    同一份机制只在此处执行一次：机制定义在声明层，执行仍走 Hook 层
    这一条既有分发路径——不引入第三条路径，也不会与旧 Hook 重复触发。

    数值规则第一阶段通过相位 INCOMING_ADJUST 桥接【加害】【龙鳞】【固执】。
    接线其它相位前，必须先确认对应分发方法在引擎里只有唯一调用点。
    """

    def __init__(self, mechanism: Mechanism):
        if mechanism.when.kind != "phase":
            raise ValueError(
                f"机制[{mechanism.name}]不是相位机制，不能挂到 Hook 分发路径")
        self.mechanism = mechanism
        self.priority = mechanism.priority  # 保留旧 Hook 接口；排序无状态锚点时才用

    def _context(self, target, amount, damage_type, source, state) -> TriggerContext:
        return TriggerContext(
            combat=None, state=state, phase=self.mechanism.when.key,
            target=target, source=source,
            amount=amount, damage_type=damage_type,
        )

    def incoming_status(self, target):
        """返回本伤害机制对应的持续状态，供同窗口的统一 X 排序使用。"""
        statuses = getattr(target, "status_effects", ()) if target is not None else ()
        status_name = self.mechanism.status_name or self.mechanism.name
        return next((status for status in statuses
                     if status.name == status_name and not status.is_expired), None)

    def incoming_order_key(self, target) -> tuple:
        """活跃持续状态按发动 X 降序、同 X 按施加先后；静态 priority 仅为后备。"""
        status = self.incoming_status(target)
        if self.mechanism.before_status_rules:
            return (-1, self.priority, self.mechanism.name)
        rule_definition = self.mechanism.rule_definition
        if rule_definition is not None:
            return rule_definition.ordering_key(status)
        if status is not None:
            return (0, *status.ordering_key, self.priority)
        return (1, self.priority, self.mechanism.name)

    def on_incoming_adjust(self, target, amount, damage_type, source, state,
                           *, rule_context=None):
        mechanism = self.mechanism
        if not mechanism.when.matches_phase(Phase.INCOMING_ADJUST):
            return amount
        rule_definition = mechanism.rule_definition
        if rule_definition is not None:
            from uuid import uuid4
            from ..rule_engine import DamageResolutionContext, RULE_EXECUTOR, validate_damage_amount

            amount = validate_damage_amount(amount, "mechanism incoming amount")
            if amount < 0 and rule_context is None:
                # 兼容旧适配器直接调用：负的初始输入不启动新规则链。
                # HookManager 对正伤害链传入共享 context，负中间值仍会继续串行执行。
                return amount
            context = rule_context or DamageResolutionContext(
                event_id=f"mechanism-adjust:{uuid4().hex}",
                attacker=source,
                recipient=target,
                damage_type=damage_type,
                original_damage=amount,
                incoming_damage=amount,
                current_damage=amount,
                mode="commit",
            )
            if context.recipient is not target or context.damage_type != damage_type:
                raise ValueError("伤害规则上下文与机制调用的受击者/伤害类型不一致")
            context.set_current_damage(amount)
            RULE_EXECUTOR.execute(
                rule_definition, context, status=self.incoming_status(target))
            return context.current_damage

        ctx = self._context(target, amount, damage_type, source, state)
        if mechanism.condition is not None and not mechanism.condition(ctx):
            return amount
        targets = mechanism.target.select(ctx) if mechanism.target is not None else []
        result = mechanism.effect(ctx, targets)
        if isinstance(result, dict):
            return result.get("amount", amount)
        if isinstance(result, (int, float)):
            return int(result)
        return amount


    def on_multiplier_adjust(self, target, amount, damage_type, source, state):
        mechanism = self.mechanism
        if not mechanism.when.matches_phase(Phase.MULTIPLIER_ADJUST):
            return amount
        ctx = self._context(target, amount, damage_type, source, state)
        if mechanism.condition is not None and not mechanism.condition(ctx):
            return amount
        targets = mechanism.target.select(ctx) if mechanism.target is not None else []
        result = mechanism.effect(ctx, targets)
        return result.get("amount", amount) if isinstance(result, dict) else amount
