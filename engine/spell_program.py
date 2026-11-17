"""法术流程的结构化表示（设计阶段，尚未接入战斗执行）。

此模块只提供控制流图及结构校验；不会结算道纹、判断可用性或注册事件。
既有 SpellExecution 仍是唯一战斗执行器。节点 ID 由描述者显式提供，跳转
不依赖列表下标；自然顺序则由 nodes 的顺序决定。
"""
from dataclasses import dataclass
from typing import Union

from .spell_dsl import ActionStep, BoolOp, Cmp, Not


@dataclass(frozen=True)
class Action:
    id: str
    effect: ActionStep
    next: str | None = None


@dataclass(frozen=True)
class Condition:
    id: str
    expression: Union[BoolOp, Cmp, Not]
    # None 表示继续执行列表中的下一节点。
    on_true: str | None = None
    on_false: str | None = None


@dataclass(frozen=True)
class Jump:
    id: str
    target: str


@dataclass(frozen=True)
class WaitEvent:
    id: str
    event: str
    # 恢复后从该节点继续；None 表示继续执行列表中的下一节点。
    resume: str | None = None


@dataclass(frozen=True)
class Stop:
    id: str


Node = Union[Action, Condition, Jump, WaitEvent, Stop]


@dataclass(frozen=True)
class SpellProgram:
    nodes: tuple[Node, ...]
    entry: str


def validate_structure(program: SpellProgram) -> None:
    """校验节点标识和静态引用，不代替战斗上下文/触发时机校验。"""
    if not isinstance(program, SpellProgram) or not isinstance(program.nodes, tuple):
        raise ValueError("法术流程必须包含节点元组")
    ids: set[str] = set()
    for node in program.nodes:
        if not isinstance(node, (Action, Condition, Jump, WaitEvent, Stop)):
            raise ValueError("未知流程节点")
        if not isinstance(node.id, str) or not node.id.strip():
            raise ValueError("节点标识必须是非空字符串")
        if node.id in ids:
            raise ValueError(f"重复节点标识: {node.id}")
        ids.add(node.id)
        if isinstance(node, Action) and not isinstance(node.effect, ActionStep):
            raise ValueError(f"节点{node.id}必须包含现有道纹动作")
        if isinstance(node, Condition) and not isinstance(node.expression, (BoolOp, Cmp, Not)):
            raise ValueError(f"节点{node.id}必须包含合法条件表达式")
        if isinstance(node, WaitEvent) and (not isinstance(node.event, str) or not node.event.strip()):
            raise ValueError(f"节点{node.id}必须指定事件")
    if not isinstance(program.entry, str) or program.entry not in ids:
        raise ValueError(f"入口节点不存在: {program.entry}")
    for node in program.nodes:
        refs = ((node.next,) if isinstance(node, Action)
                else (node.on_true, node.on_false) if isinstance(node, Condition)
                else (node.target,) if isinstance(node, Jump)
                else (node.resume,) if isinstance(node, WaitEvent) else ())
        if isinstance(node, Jump) and node.target is None:
            raise ValueError(f"节点{node.id}必须指定跳转目标")
        for ref in refs:
            if ref is not None and (not isinstance(ref, str) or ref not in ids):
                raise ValueError(f"节点{node.id}引用不存在的节点: {ref}")


class ProgramCursor:
    """推进控制流，动作由调用方结算；不绕过原有战斗引擎。

    此阶段只支持无环图；事件等待与循环检查尚未接入，不可用于真实循环。
    """
    def __init__(self, program: SpellProgram):
        validate_structure(program)
        self.program = program
        self.nodes = {node.id: node for node in program.nodes}
        self.fallthrough = {node.id: (program.nodes[i + 1].id
                            if i + 1 < len(program.nodes) else None)
                            for i, node in enumerate(program.nodes)}
        self.position = program.entry
        self._assert_acyclic()

    def _successors(self, node):
        following = self.fallthrough[node.id]
        if isinstance(node, Action):
            return (node.next if node.next is not None else following,)
        if isinstance(node, Condition):
            return (node.on_true if node.on_true is not None else following,
                    node.on_false if node.on_false is not None else following)
        if isinstance(node, Jump):
            return (node.target,)
        if isinstance(node, WaitEvent):
            raise ValueError("事件等待尚未接入战斗事件调度")
        return ()

    def _assert_acyclic(self):
        # 暂不开放无净变化检测尚未实现的循环，避免错误或无限结算。
        visited, active = set(), set()
        def walk(key):
            if key is None or key in visited:
                return
            if key in active:
                raise ValueError("流程循环尚未接入循环中断规则")
            active.add(key)
            for successor in self._successors(self.nodes[key]):
                walk(successor)
            active.remove(key)
            visited.add(key)
        walk(self.position)

    def next_action(self, decide):
        """执行控制节点直到动作或结束；decide(condition) 由战斗执行器提供。"""
        while self.position is not None:
            node = self.nodes[self.position]
            following = self.fallthrough[node.id]
            if isinstance(node, Stop):
                self.position = None
                return None
            if isinstance(node, Action):
                self.position = node.next if node.next is not None else following
                return node
            if isinstance(node, Condition):
                target = node.on_true if decide(node) else node.on_false
                self.position = target if target is not None else following
            elif isinstance(node, Jump):
                self.position = node.target
        return None


def compile_legacy_acyclic(steps):
    """将旧无循环 AST 转换为跳转图；含循环或未知节点返回 None。"""
    from .spell_dsl import IfStep, LoopStep

    def supported(items):
        return all(isinstance(item, ActionStep) or
                   (isinstance(item, IfStep) and supported(item.then_steps)
                    and supported(item.else_steps)) for item in items)

    if not supported(steps):
        return None
    nodes = [Stop("$end")]

    def compile_block(items, continuation, prefix):
        dest = continuation
        for index in range(len(items) - 1, -1, -1):
            item = items[index]
            path = f"{prefix}{index}"
            if isinstance(item, ActionStep):
                key = f"action:{path}"
                nodes.append(Action(key, item, next=dest))
            elif isinstance(item, IfStep):
                then = compile_block(item.then_steps, dest, f"{path}T")
                otherwise = compile_block(item.else_steps, dest, f"{path}E")
                key = f"if:{path}"
                nodes.append(Condition(key, item.condition, on_true=then, on_false=otherwise))
            else:
                raise TypeError(f"不支持的旧法术步骤: {type(item).__name__}")
            dest = key
        return dest

    entry = compile_block(steps, "$end", "")
    return SpellProgram(tuple(nodes), entry)
