"""引擎事务快照：**哪些状态需要保存/还原**的唯一清单。

背景：一次 `execute_action` 是原子操作——失败或抛异常都不能留下半截状态。
快照原本散落在 `GameEngine._execute_action_core` 与
`_action_timed_world_pre_battle` 两处，各自手写 5 行 deepcopy + 7 行还原；
少还原一项就会留下脏状态，而且很难被测出来（异常路径的覆盖率天然低）。

本模块把这些字段集中成一个类：

| 字段 | 含义 | 不还原会怎样 |
| --- | --- | --- |
| `state` | `GameState` 深拷贝（含玩家/敌人/遗物/世界快照/资源……） | 一切游戏状态泄漏 |
| `combat_runtime` | `_snapshot_combat_runtime()`：战斗期临时运行态（法术栈、两阶段 pending、连击计数…） | 战斗卡在半提交状态 |
| `event_pool` | `(triggered 集合, current 事件)` | 事件池被"用过一次"污染，或事件卡死 |
| `dice` | `DiceEngine` 深拷贝 | 失败的行动消耗了随机源，同种子不再可复现 |
| `pending_interrupts` | 待 DM 裁定的中断队列 | 失败提交产生幽灵中断 |

刻意**不在**清单里的东西：

* `_last_result` —— 失败时按设计会被改写成错误结果，不是"脏状态"；
* `_action_history` —— 行动账簿只在成功后追加，异常路径不写；
* `combat.resolution` —— 生命周期计数器由 `begin_action/end_action` 自己配对收尾。

已知偏离（不是遗漏，别顺手"修"）：

* `Entity/GameState.shards_peak` 是"本局历史最高余额"的单调记录，
  `__setattr__` 把任何下调夹回旧值，所以失败行动中途抬高的峰值回滚后仍会保留。
  改为严格事务语义会改变玩家可见的历史峰值，是否影响门槛判定需另行裁定；
  `tests/test_engine_rollback.py::test_rollback_never_lowers_monotonic_shards_peak`
  把现状固定成测试。

新增引擎级可变状态时，请先判断它属于"行动中途会变、且失败必须回退"，
是的话加进 `EngineSnapshot.capture`；并在 `tests/test_engine_rollback.py`
的对照表里补一条，否则那条状态不会被回滚测试覆盖。
"""
from __future__ import annotations

import copy
from typing import Any


class EngineSnapshot:
    """一次行动前的完整可变状态快照。"""

    __slots__ = ("state", "combat_runtime", "event_pool", "dice", "pending_interrupts")

    def __init__(self, state: Any, combat_runtime: dict, event_pool: tuple,
                 dice: Any, pending_interrupts: Any) -> None:
        self.state = state
        self.combat_runtime = combat_runtime
        self.event_pool = event_pool
        self.dice = dice
        self.pending_interrupts = pending_interrupts

    @classmethod
    def capture(cls, engine: Any) -> "EngineSnapshot":
        """按清单抓取一遍。字段顺序与 `restore` 对称，便于对照检查。"""
        return cls(
            state=copy.deepcopy(engine.state),
            combat_runtime=engine._snapshot_combat_runtime(),
            event_pool=(set(engine.event_pool.triggered), engine.event_pool.current),
            dice=copy.deepcopy(engine.dice),
            pending_interrupts=copy.deepcopy(engine._pending_interrupts),
        )

    def restore(self, engine: Any) -> None:
        """原地还原。必须在 `engine.state` 尚未被换成新对象之前调用（见实现）。"""
        engine._restore_state_in_place(self.state)
        engine.combat.state = engine.state
        engine._restore_combat_runtime(self.combat_runtime)
        engine.dice = self.dice
        engine.combat.dice = engine.dice
        engine.event_pool.triggered, engine.event_pool.current = self.event_pool
        engine._pending_interrupts = self.pending_interrupts
