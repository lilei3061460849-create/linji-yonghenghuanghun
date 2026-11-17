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
        refs = ((node.on_true, node.on_false) if isinstance(node, Condition)
                else (node.target,) if isinstance(node, Jump)
                else (node.resume,) if isinstance(node, WaitEvent) else ())
        if isinstance(node, Jump) and node.target is None:
            raise ValueError(f"节点{node.id}必须指定跳转目标")
        for ref in refs:
            if ref is not None and (not isinstance(ref, str) or ref not in ids):
                raise ValueError(f"节点{node.id}引用不存在的节点: {ref}")
