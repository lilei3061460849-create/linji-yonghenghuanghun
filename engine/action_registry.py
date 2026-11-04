"""行动注册表：`action_type → 处理器方法` 的单一查表入口。

背景：`engine/api.py` 的 `_dispatch_action` 曾是 93 个 `elif` 的字符串路由，
每加一个 action 都要改主入口。本模块把"action 名 → 方法名 + 元信息"搬进表里：

* 处理器仍然是 **`GameEngine` 上的方法**（含 `engine/handlers/*.py` 里的 mixin 方法），
  注册表只存方法名，`_dispatch_action` 用 `getattr(self, spec.handler)` 取；
* 这样可以"只搬位置、不改行为"：把一组方法挪进 mixin，就是在该 mixin 里 `@action(...)`
  声明一次，然后把 `api.py` 里对应的 `elif` 删掉——查表命中即走新路径，
  没注册的 action 继续走旧的 `if/elif`（迁移期两条路径并存，随时可回退）；
* `allowed_phases` 是**声明式元信息**，供文档、清单与测试核对"这个 action 属于哪个阶段"，
  `_dispatch_action` 不据此拦截（真正的阶段门禁仍在 `_phase_error`，动它会改行为）。

新增一个 action 的写法见 `engine/README.md`「行动注册表」一节。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class ActionSpec:
    """一个 action 的注册条目。"""

    name: str
    handler: str
    group: str = "misc"
    allowed_phases: frozenset[str] = frozenset()
    note: str = ""


class ActionRegistry:
    """action 名 → ActionSpec。全进程一份，模块导入时由各 handler 模块填充。"""

    def __init__(self) -> None:
        self._specs: dict[str, ActionSpec] = {}

    def register(self, spec: ActionSpec) -> ActionSpec:
        if spec.name in self._specs:
            existing = self._specs[spec.name]
            raise ValueError(f"action 重复注册: {spec.name}（已由 {existing.handler} 注册）")
        self._specs[spec.name] = spec
        return spec

    def action(self, name: str, *, group: str, handler: str | None = None,
               allowed_phases: tuple[str, ...] = (), note: str = ""):
        """装饰器：在 handler 方法上声明"我是这个 action 的处理器"。

        `handler` 省略时用被装饰函数的 `__name__`。
        """
        def decorator(func):
            self.register(ActionSpec(
                name=name,
                handler=handler or func.__name__,
                group=group,
                allowed_phases=frozenset(allowed_phases),
                note=note,
            ))
            return func

        return decorator

    def get(self, name: str) -> ActionSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[ActionSpec]:
        return [self._specs[name] for name in self.names()]

    def __iter__(self) -> Iterator[ActionSpec]:
        return iter(self.specs())

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, name: object) -> bool:
        return name in self._specs


#: 全进程唯一注册表。各 handler 模块在导入时用 `@ACTION_REGISTRY.action(...)` 声明。
ACTION_REGISTRY = ActionRegistry()
