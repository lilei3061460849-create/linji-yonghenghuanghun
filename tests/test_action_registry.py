"""行动注册表（`engine/action_registry.py`）与 `_dispatch_action` 查表的契约测试。

迁移策略是"查表命中走新路径，否则回落旧 if/elif"，因此这里锁三件事：

1. 注册表能拦住重复注册（防止两组 handler 抢同一个 action 名）；
2. 查表命中的 action 真的会调用注册的方法，而不是继续走旧长链；
3. 没注册的 action 行为不变——仍然返回同一条"未知行动类型"错误。
"""
from __future__ import annotations

import types

import pytest

from engine.action_registry import ACTION_REGISTRY, ActionRegistry, ActionSpec
from engine.api import GameEngine


@pytest.fixture()
def engine(tmp_path):
    return GameEngine(db_path=str(tmp_path / "rulings.db"),
                      save_dir=str(tmp_path / "saves"),
                      autosave=False, rng_seed=1)


def test_registry_rejects_duplicate_names():
    registry = ActionRegistry()
    registry.register(ActionSpec(name="a", handler="_action_a"))
    with pytest.raises(ValueError, match="重复注册"):
        registry.register(ActionSpec(name="a", handler="_action_b"))


def test_registry_get_and_names_and_contains():
    registry = ActionRegistry()
    registry.register(ActionSpec(name="b", handler="_action_b", group="g2"))
    registry.register(ActionSpec(name="a", handler="_action_a", group="g1"))
    assert registry.names() == ["a", "b"]          # 排序稳定，便于生成清单
    assert "a" in registry and "zzz" not in registry
    assert registry.get("b").group == "g2"
    assert registry.get("zzz") is None
    assert len(registry) == 2


def test_decorator_registers_method_name():
    registry = ActionRegistry()

    class Fake:
        @registry.action("probe", group="test", allowed_phases=("pre_battle",))
        def _handle_probe(self, params):
            return params

    spec = registry.get("probe")
    assert spec.handler == "_handle_probe"
    assert spec.group == "test"
    assert spec.allowed_phases == frozenset({"pre_battle"})


def test_unknown_action_falls_back_to_legacy_chain(engine):
    """未注册的 action 必须仍在旧长链里被判为未知，不能因引入注册表而改变报错。"""
    assert "__definitely_not_an_action__" not in ACTION_REGISTRY
    result = engine.execute_action("__definitely_not_an_action__", {})
    assert result == {"success": False, "error": "未知行动类型: __definitely_not_an_action__"}


def test_registered_action_dispatches_through_table(engine):
    """注册表命中时，调用的是注册的方法（用探针方法证明没有落到旧长链）。"""
    name = "__probe_registered_action__"
    spec = ActionSpec(name=name, handler="_probe_handler", group="test")
    ACTION_REGISTRY.register(spec)
    try:
        calls = []

        def _probe_handler(self, params):
            calls.append(params)
            return {"success": True, "result": {"probe": True}}

        engine._probe_handler = types.MethodType(_probe_handler, engine)
        result = engine.execute_action(name, {"x": 1})
        assert result["success"] is True and result["result"] == {"probe": True}
        assert calls == [{"x": 1}]
    finally:
        ACTION_REGISTRY._specs.pop(name, None)
        del engine._probe_handler


def test_every_registered_handler_exists_on_engine(engine):
    """注册表里每个 handler 名都必须是 GameEngine（含 handlers mixin）上的真实方法。"""
    missing = [spec.name for spec in ACTION_REGISTRY
               if not callable(getattr(engine, spec.handler, None))]
    assert missing == [], f"注册表指向了不存在的方法: {missing}"


def test_registered_actions_are_unique_per_handler_group():
    """同一个方法不应同时被两个 action 名注册（防止误抄 handler 名）。"""
    handlers: dict[str, str] = {}
    clash = []
    for spec in ACTION_REGISTRY:
        if spec.handler in handlers:
            clash.append((handlers[spec.handler], spec.name, spec.handler))
        handlers[spec.handler] = spec.name
    assert clash == [], f"多个 action 复用同一个 handler: {clash}"
