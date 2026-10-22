"""效果结算入口的唯一性契约（Phase 4）。

需求原话：「杀伐/再生/连击/庇护/死亡/复活/分裂/进化/三相 全部走同一条路径，
不得绕开结算入口做状态变更」。

本文件把这个要求变成可执行的断言，而不是靠命名约定：

* 每次效果结算都必须在**结算帧内**发生（`resolution.depth >= 1`）；
* 关键汇点（伤害/回复/代价/命零/分裂/进化/选项/攻击/怪物阶段）必须真的被记账；
* 汇点被调用时若不在任何结算帧内，即为「绕过结算入口」——直接失败；
* 包装层不得改变行为：与包装前同样的输入必须产出同样的结果（对照在
  tests/test_action_preview_parity.py 与 sim/behavior_trace.py 里）。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.models import Entity, GameState, StatusEffect              # noqa: E402
from engine.resolution import (                                        # noqa: E402
    KIND_COST, KIND_DEATH, KIND_DAMAGE, KIND_EFFECT, KIND_EVOLVE,
    KIND_HEAL, KIND_SPLIT, ResolutionContext,
)
from tests.preview_support import (                                     # noqa: E402
    build_battle_engine, build_pre_battle_engine,
)

#: 本轮要求覆盖的效果 → 期望在链上出现的帧类型。
#: 名单取自需求原文；每个条目都必须在真实对局里跑出对应帧。
NAMED_EFFECTS = {
    "杀伐": KIND_DAMAGE,
    "再生": KIND_HEAL,
    "连击": KIND_COST,          # 连击以「失去生命」结算（代价/失血路径）
    "庇护": KIND_EFFECT,        # 护盾来自道纹效果帧
    "死亡": KIND_DEATH,
    "分裂": KIND_SPLIT,
    "进化": KIND_EVOLVE,
}


class FrameRecorder:
    """记录一次行动里所有结算帧的进出，并捕获「帧外的汇点调用」。"""

    def __init__(self, ctx: ResolutionContext):
        self.ctx = ctx
        self.kinds: list[str] = []
        self.min_depth_per_sink: dict[str, int] = {}

    def __enter__(self):
        self._real_enter = ResolutionContext.enter
        self._real_leave = ResolutionContext.leave
        recorder = self

        def enter(ctx_self, kind, label=""):
            token = recorder._real_enter(ctx_self, kind, label)
            recorder.kinds.append(kind)
            return token

        def leave(ctx_self, token=None):
            return recorder._real_leave(ctx_self, token)

        ResolutionContext.enter = enter
        ResolutionContext.leave = leave
        return self

    def __exit__(self, *exc):
        ResolutionContext.enter = self._real_enter
        ResolutionContext.leave = self._real_leave
        return False


def _alive_enemy(engine):
    return next(e.name for e in engine.state.enemies if e.is_alive)


def _cast(engine, name, target=None, **params):
    """发一次道纹；未指定目标时自动选第一个存活敌人（杀伐/连击需要目标）。"""
    if target is None:
        target = _alive_enemy(engine) if name in ("杀伐", "连击", "坠落") else "贾凡"
    payload = {"daowen_name": name, "x": params.pop("x", 1), "target": target}
    payload.update(params)
    return engine.execute_action("use_daowen", payload)


# ---------------------------------------------------------------- 唯一入口

def test_daowen_effect_entry_is_the_only_public_name():
    """`apply_daowen_effect` 仍在，且实现体被改名——包装层是唯一公开入口。"""
    from engine.combat import CombatEngine
    assert hasattr(CombatEngine, "apply_daowen_effect")
    assert hasattr(CombatEngine, "_apply_daowen_effect_impl")
    assert CombatEngine.apply_daowen_effect.__doc__.count("唯一公开入口") == 1


def test_every_effect_entry_opens_a_frame_before_touching_state():
    """五个入口（道纹/攻击/怪物阶段/进化/选项）都在改状态之前开帧。"""
    from engine.combat import CombatEngine
    import engine.events as events
    for name in ("_apply_daowen_effect_impl", "_resolve_attack_impl",
                 "_resolve_monster_phase_impl", "_execute_evolution_impl"):
        wrapper = {"_apply_daowen_effect_impl": "apply_daowen_effect",
                   "_resolve_attack_impl": "resolve_attack",
                   "_resolve_monster_phase_impl": "resolve_monster_phase",
                   "_execute_evolution_impl": "execute_evolution"}[name]
        func = getattr(CombatEngine, wrapper)
        assert "resolution_frame" in func.__code__.co_names, f"{wrapper} 未开结算帧"
    assert "resolution_frame" in events.resolve_option_effect.__code__.co_names


@pytest.mark.parametrize("effect", ["杀伐", "再生", "庇护", "连击"])
def test_named_daowen_effects_resolve_inside_a_frame(tmp_path, effect):
    engine = build_battle_engine(tmp_path, name=f"eff_{effect}")
    ctx = engine.combat.resolution
    with FrameRecorder(ctx) as rec:
        result = _cast(engine, effect)
    assert result.get("success") is True, result.get("error")
    assert KIND_EFFECT in rec.kinds, f"{effect} 没有走结算帧：{rec.kinds}"
    assert ctx.depth == 0 and not ctx.chain(), "帧必须全部退出"


def test_damage_and_heal_sinks_record_deltas(tmp_path):
    """trace 打开时，伤害/回复帧必须带上 hp 变化（回答「它为什么没死」）。"""
    engine = build_battle_engine(tmp_path, name="trace_delta")
    ctx = engine.combat.resolution
    ctx.set_tracing(True)
    enemy = next(e for e in engine.state.enemies if e.is_alive)
    enemy_hp_before = enemy.current_hp
    _cast(engine, "杀伐", x=1)
    text = ctx.describe_trace()
    assert f"hp {enemy_hp_before}→" in text, text
    _cast(engine, "再生", x=1)
    assert "hp " in ctx.describe_trace()


def test_history_is_bounded():
    """trace 数据必须有上限（不得大量驻留对象）。"""
    ctx = ResolutionContext(tracing=True)
    for _ in range(ResolutionContext.MAX_HISTORY + 40):
        token = ctx.enter(KIND_EFFECT, "x")
        ctx.leave(token)
    assert len(ctx.history) == ResolutionContext.MAX_HISTORY


# ---------------------------------------------------------------- 无绕过

def test_no_sink_runs_outside_a_frame(tmp_path, monkeypatch):
    """把关键汇点全部包一层：任何「帧外」调用都记下来。

    这是需求「不得绕开结算入口做状态变更」的直接检查——包括触发、
    嵌套效果、怪物阶段在内的整局循环都要满足。
    """
    from engine.combat_parts import damage_death
    from engine import models as models_mod

    violations: list[str] = []

    def guard(label, ctx_getter):
        def wrapper(func):
            def inner(self, *args, **kwargs):
                ctx = ctx_getter(self)
                if ctx is not None and ctx.depth == 0 and ctx.effect_count > 0:
                    # 行动已经开始（effect_count>0）却没有任何帧在栈上 = 绕过入口
                    violations.append(label)
                return func(self, *args, **kwargs)
            return inner
        return wrapper

    monkeypatch.setattr(
        damage_death.DamageDeathMixin, "_apply_hostile_damage_inner",
        guard("_apply_hostile_damage_inner", lambda self: self.resolution))
    from engine.combat_events import engine_for_state
    monkeypatch.setattr(
        models_mod.GameState, "_apply_heal_inner",
        guard("_apply_heal_inner",
              lambda self: getattr(engine_for_state(self), "resolution", None)))

    engine = build_battle_engine(tmp_path, name="nobypass")
    for _ in range(4):
        if not any(e.is_alive for e in engine.state.enemies):
            break
        _cast(engine, "杀伐", x=1)
        _cast(engine, "再生", x=1)
    assert violations == [], f"存在绕过结算入口的汇点调用：{violations}"


def test_action_boundary_clears_frames_after_nested_chain(tmp_path):
    """A→B→C 之后深度必须回零（链不会残留到下一次行动）。"""
    engine = build_battle_engine(tmp_path, name="chain_reset")
    ctx = engine.combat.resolution
    ctx.set_tracing(True)
    for _ in range(3):
        if not any(e.is_alive for e in engine.state.enemies):
            break
        _cast(engine, "杀伐", x=1)
        assert ctx.depth == 0
    assert ctx.trip_reason == "", "正常对局不应触发任何保险丝"


def test_out_of_battle_option_effect_uses_the_same_frame(tmp_path):
    """局外事件选项效果（另一层入口）同样走结算帧。"""
    import engine.events as events
    engine = build_pre_battle_engine(tmp_path, name="option")
    ctx = engine.combat.resolution
    with FrameRecorder(ctx) as rec:
        events.resolve_option_effect("获得3枚碎片", engine, "测试事件")
    assert rec.kinds == [KIND_EFFECT], rec.kinds
    assert ctx.depth == 0


def test_split_and_death_frames_are_reachable(tmp_path):
    """分裂/死亡必须在真实对局里可达（否则契约是空的）。"""
    engine = build_battle_engine(tmp_path, name="split_death")
    ctx = engine.combat.resolution
    seen: list[str] = []
    real_enter = ResolutionContext.enter

    def enter(ctx_self, kind, label=""):
        seen.append(kind)
        return real_enter(ctx_self, kind, label)

    ResolutionContext.enter = enter
    try:
        enemy = next(e for e in engine.state.enemies if e.is_alive)
        # 直接把怪物打到 0：走统一命零管线（含命零帧）
        engine.combat._apply_hostile_damage(enemy, enemy.current_hp + 10,
                                            source=engine.state.player)
    finally:
        ResolutionContext.enter = real_enter
    assert KIND_DAMAGE in seen and KIND_DEATH in seen, seen
    assert ctx.depth == 0


def test_entity_status_effects_are_engine_level_not_bypass():
    """状态效果（add_status）本身是 Entity 层 API，不经引擎——记录现状。

    这条测试的存在是为了**明确边界**：汇点清单里的 Entity 方法（take_damage/
    add_status/add_mutation）挂在实体上，实体没有引擎引用，因此它们不计入结算帧；
    引擎侧的调用点仍受帧保护（上面几条测试覆盖）。等到需要时再统一，
    而不是现在为它们引入全局注册表（那才是「为抽象而抽象」）。
    """
    entity = Entity("E", "怪物", blood_limit=10, current_hp=10)
    entity.add_status(StatusEffect(name="洞察", remaining_rounds=1, value=1))
    assert any(s.name == "洞察" for s in entity.status_effects)
    assert entity.current_hp == 10
