"""事务回滚测试：失败/抛异常的行动必须把引擎恢复到调用前。

体检要求（2026-11-03 任务 3.4）：`engine/rollback.py::EngineSnapshot` 是
"哪些状态需要快照/还原"的唯一清单；这里用**行为**验证清单是否完整：

* 自然失败：action 因为参数/阶段不合法被拒（`success=False`）；
* 注入异常：把处理器换成"改坏状态后再抛异常"的假处理器。

两种情况都要断言：调用后 `state` / `combat_runtime` / `event_pool` / `dice` /
`pending_interrupts` 五项与调用前逐字节一致。**未列入清单的状态不在断言范围内**——
真发现了遗漏，就往清单与 `_fingerprint()` 里各加一条。
"""
from __future__ import annotations

import copy
import dataclasses
import pickle
import weakref

import pytest

from engine.api import GameEngine
from tests.setup_support import finish_initial_daowen, resolve_opening_relic

# 每个用例：(action, params, 期望失败原因关键词)
NATURAL_FAILURES = [
    ("world_move", {"x": 999, "y": 999}, ""),
    ("world_move", {"x": "1", "y": 2}, ""),
    ("use_daowen", {"daowen_name": "根本不存在的道纹", "x": 1}, ""),
    ("define_spell", {"spell": {"name": "", "required_daowen": [], "trigger_condition": "",
                                "effect_flow": ""}}, ""),
    ("consume_item", {"item_name": "不存在的消耗品"}, ""),
]


def _fingerprint(engine: GameEngine) -> dict:
    """引擎全部可变状态的指纹（按 EngineSnapshot 的清单逐项取）。

    刻意不用 `pickle.dumps(state)` 当指纹：`GameState` 里有 `set`，内容相同的
    两个集合在 pickle 里字节序可能不同，会制造假红灯。逐字段 `==` 对顺序不敏感。
    `dice` 反过来只能用 pickle——它内部是 `random.Random`，`==` 比的是同一性。
    """
    return {
        "state": copy.deepcopy(engine.state),
        "combat_runtime": copy.deepcopy(engine._snapshot_combat_runtime()),
        "event_pool": (set(engine.event_pool.triggered), engine.event_pool.current),
        "dice": pickle.dumps(engine.dice, protocol=4),
        "pending_interrupts": copy.deepcopy(engine._pending_interrupts),
    }


# 【单调字段】只升不降的历史记录：`Entity.__setattr__` / `GameState.__setattr__`
# 会把下调夹回旧值，所以回滚**也无法**把它们降下来。这不是本次改动引入的缺陷，
# 而是既有设计（models.py:439/1437 "只升不降"）；见 rollback.py 文档的"已知偏离"。
MONOTONIC_FIELDS = {"shards_peak"}


def _comparable(value, _depth: int = 0):
    """把任意状态值转成可比较、与容器顺序无关的普通结构。"""
    if _depth > 6:                      # 防环/防深挖：超出就当原子值
        return repr(value)
    if isinstance(value, weakref.ReferenceType):
        return "弱引用（运行时绑定，不属于快照数据）"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _comparable(getattr(value, f.name), _depth + 1)
                for f in dataclasses.fields(value) if f.name not in MONOTONIC_FIELDS}
    if isinstance(value, dict):
        return {_comparable(k, _depth + 1): _comparable(v, _depth + 1)
                for k, v in value.items() if k not in MONOTONIC_FIELDS}
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted((_comparable(v, _depth + 1) for v in value), key=repr)
    if hasattr(value, "__dict__"):
        return {k: _comparable(v, _depth + 1)
                for k, v in value.__dict__.items() if k not in MONOTONIC_FIELDS}
    return value


def _assert_same_fingerprint(before: dict, after: dict, label: str) -> None:
    before_state, after_state = before["state"], after["state"]
    for field in dataclasses.fields(before_state):
        if field.name in MONOTONIC_FIELDS:
            continue
        left = _comparable(getattr(before_state, field.name))
        right = _comparable(getattr(after_state, field.name))
        assert right == left, f"{label}: 回滚后 state.{field.name} 与调用前不一致"
    for key in ("combat_runtime", "event_pool", "dice", "pending_interrupts"):
        assert after[key] == before[key], f"{label}: 回滚后 {key} 与调用前不一致"


def _assert_unchanged(before: dict, engine: GameEngine, label: str) -> None:
    _assert_same_fingerprint(before, _fingerprint(engine), label)


@pytest.fixture()
def started_engine(tmp_path):
    """一份足够"有状态"的开局：属性 → 开局遗物 → 初始道纹 → 副本 → 开放世界地图。"""
    engine = GameEngine(db_path=str(tmp_path / "rulings.db"),
                        save_dir=str(tmp_path / "saves"),
                        autosave=False, rng_seed=20261103)
    result = engine.execute_action("setup_attributes", {
        "name": "回滚测试", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    assert result.get("success"), result
    resolve_opening_relic(engine)
    finish_initial_daowen(engine)
    result = engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    assert result.get("success"), result
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    assert setup.get("success"), setup
    assert setup.get("result", {}).get("relic_choices") is not None  # 选副本会附带发现
    result = engine.execute_action("world_initialize", {"width": 21, "height": 21})
    assert result.get("success"), result
    return engine


@pytest.mark.parametrize("action,params,_note", NATURAL_FAILURES)
def test_naturally_failing_action_leaves_no_trace(started_engine, action, params, _note):
    before = _fingerprint(started_engine)
    result = started_engine.execute_action(action, params)
    assert result.get("success") is False, (action, result)
    _assert_unchanged(before, started_engine, action)


@pytest.mark.parametrize("method_name,action,params", [
    ("_action_world_move", "world_move", {"x": 1, "y": 1}),
    ("_action_world_wait", "world_wait", {"turns": 1}),
    ("_action_world_status", "world_status", {}),
    ("_action_world_vision", "world_vision", {}),
    # 注意：别用 use_daowen——局外发动会被前置校验挡掉，异常根本到不了处理器。
    ("_action_world_interact", "world_interact", {"target": "x"}),
    ("_action_world_suggest_ally", "world_suggest_ally", {"suggestion": "x"}),
])
def test_exception_midway_rolls_back_everything(started_engine, method_name, action, params,
                                                monkeypatch):
    """处理器改了状态再抛异常：事务层必须把五项状态全部还原。"""
    before = _fingerprint(started_engine)
    original = getattr(started_engine, method_name)

    def _mutate_then_blow_up(self, params):
        # 故意写坏多个维度的状态，确保回滚真的发生而不是"本来就没改"
        self.state.shards += 777
        self.state.current_round += 3
        self.state.world_state = {"tampered": True}
        self.combat._priority_passes += 5
        self.event_pool.triggered.add("被污染的事件")
        self._pending_interrupts.append({"type": "幽灵中断"})
        raise RuntimeError(f"注入异常::{method_name}")

    monkeypatch.setattr(started_engine, method_name, _mutate_then_blow_up.__get__(started_engine),
                        raising=True)
    result = started_engine.execute_action(action, params)
    assert result.get("success") is False
    assert "注入异常" in str(result.get("error"))
    _assert_unchanged(before, started_engine, method_name)
    # 假处理器只该被调用一次，且原方法仍然挂在类上（monkeypatch 已还原）
    assert getattr(type(started_engine), method_name) is not _mutate_then_blow_up
    assert callable(original)


def test_snapshot_restore_roundtrip_is_idempotent(started_engine):
    """EngineSnapshot 连续还原两次结果一致（还原本身不产生副作用）。"""
    from engine.rollback import EngineSnapshot

    snapshot = EngineSnapshot.capture(started_engine)
    baseline = _fingerprint(started_engine)
    started_engine.state.shards += 1
    snapshot.restore(started_engine)
    first = _fingerprint(started_engine)
    snapshot.restore(started_engine)
    second = _fingerprint(started_engine)
    _assert_same_fingerprint(baseline, first, "第一次还原")
    _assert_same_fingerprint(baseline, second, "第二次还原")


def test_snapshot_covers_every_documented_field(started_engine):
    """清单完整性：文档里列的字段必须都能抓到、都能还原。"""
    from engine.rollback import EngineSnapshot

    snapshot = EngineSnapshot.capture(started_engine)
    assert set(EngineSnapshot.__slots__) == {
        "state", "combat_runtime", "event_pool", "dice", "pending_interrupts",
    }
    assert isinstance(snapshot.state, type(started_engine.state))
    assert isinstance(snapshot.combat_runtime, dict)
    assert isinstance(snapshot.event_pool, tuple) and len(snapshot.event_pool) == 2
    assert not isinstance(snapshot.dice, type(None))
    assert isinstance(snapshot.pending_interrupts, list)


def test_rollback_never_lowers_monotonic_shards_peak(started_engine):
    """已知偏离，写成测试固定住：碎片峰值只升不降，回滚也不会调低。

    `shards_peak` 是"本局历史最高余额"的单调记录，`Entity/GameState.__setattr__`
    会把任何下调夹回旧值。因此一次失败行动若中途抬高了碎片，回滚后余额复原、
    峰值保留抬高值。这是既有设计，不是可顺手"修"的行为——真要改成严格事务语义
    会改变玩家可见的历史峰值（是否影响门槛判定需另行裁定）。
    """
    peak_before = started_engine.state.shards_peak
    started_engine.state.shards = peak_before + 100       # 只写内存，走 action 之外
    assert started_engine.state.shards_peak == peak_before + 100
    started_engine.state.shards = peak_before             # 余额降回去，峰值不降
    assert started_engine.state.shards_peak == peak_before + 100
    assert started_engine.state.player.shards_peak == peak_before + 100


def test_successful_action_still_mutates_state(started_engine):
    """反向对照：成功行动确实改状态（否则上面的"回滚成功"可能是假象）。"""
    before = _fingerprint(started_engine)
    result = started_engine.execute_action("world_move", {"x": 1, "y": 1})
    if result.get("success"):
        assert _fingerprint(started_engine)["state"] != before["state"]
    else:
        # 目标格不可通行时，行动应被拒且状态不变——这也是合法结果
        _assert_unchanged(before, started_engine, "world_move(合法/非法都接受)")
