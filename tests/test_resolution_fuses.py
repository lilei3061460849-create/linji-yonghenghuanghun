"""保险丝契约（Phase 5）：拦失控，不拦「合法的重复」。

需求里两条硬性约束在这里落地：

* 不得一刀切禁止重复效果（`if effect in visited: return` 是禁止写法）——
  失去生命→再生→连击→失去生命 是**合法**的，必须照常结算；
* 又必须能拦住 A→B→A→B… 与 A→B→C→A 这种无限套娃。
本文件用「重复触发但不嵌套」vs「自触发导致嵌套」两种形态把这两条区分开：
前者必须畅通，后者必须被保险丝截断，并且截断要**可诊断**（报错里带链）。

阈值本身的有效性由 `sim/threshold_evidence.py` 实测支撑（深度峰值 5 / 单行动
效果峰值 83），本文件只负责锁住「余量 ≥10 倍」这条要求。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine                                  # noqa: E402
from engine.combat_events import CombatEvent, CombatEventType           # noqa: E402
from engine.dice import DiceEngine                                      # noqa: E402
from engine.mechanisms import Mechanism, Trigger                       # noqa: E402
from engine.models import Entity, GameState                             # noqa: E402
from engine.resolution import (                                         # noqa: E402
    KIND_DAMAGE, KIND_HEAL, ResolutionBudgetError, ResolutionContext,
    ResolutionCycleError, ResolutionDepthError,
)
from tests.preview_support import build_battle_engine                   # noqa: E402

#: `sim/threshold_evidence.py` 实测峰值（全路径记账后）。
MEASURED_PEAK_DEPTH = 5
MEASURED_PEAK_EFFECTS = 83


def _probe_battle(*, player_hp: int = 60, enemy_hp: int = 60):
    state = GameState(phase="in_combat", combat_subphase="await_round_start")
    player = Entity("探针", "轮回者", blood_limit=player_hp, current_hp=player_hp,
                    mana_limit=10, current_mana=10, speed_limit=5, current_speed=5)
    enemy = Entity("靶怪", "怪物", blood_limit=enemy_hp, current_hp=enemy_hp)
    state.player, state.enemies = player, [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


class _TempMechanism:
    """临时订阅一个事件机制，测试结束必定注销（不留全局残留）。

    订阅要落在**总线上**：`TriggerBus` 在战斗实例构造时从注册表取一次订阅，
    之后再往 `MECHANISMS` 里塞定义不会被已建好的战斗看到。
    """

    def __init__(self, combat, name: str, event_type: CombatEventType, effect,
                 priority: int = 100):
        self.combat = combat
        self.mechanism = Mechanism(name=name, when=Trigger.event(event_type),
                                   effect=effect, priority=priority)

    def __enter__(self):
        self.combat.mechanism_bus.register(self.mechanism)
        return self.mechanism

    def __exit__(self, *exc):
        self.combat.mechanism_bus.unregister(self.mechanism)
        return False


# ---------------------------------------------------------------- 阈值依据

def test_thresholds_keep_at_least_ten_times_headroom():
    """阈值必须留 ≥10 倍实测余量——这是「保险丝永不改变游戏结果」的依据。

    余量不足时**不许**顺手调大：先查是不是出现了真的失控（见 sim/threshold_evidence.py）。
    """
    assert ResolutionContext.MAX_DEPTH >= MEASURED_PEAK_DEPTH * 10
    assert ResolutionContext.MAX_EFFECTS >= MEASURED_PEAK_EFFECTS * 10


def test_legal_repeats_are_never_blocked_by_identity(tmp_path):
    """同一个效果反复结算（不嵌套）必须畅通——禁止任何「按名字去重」的写法。

    形态：连续 N 次失去生命，每次都正常触发、正常记账，深度始终为 1。
    """
    state, combat, player, _enemy = _probe_battle(player_hp=200)
    ctx = combat.resolution
    depths = []

    def on_damage(trigger_ctx, targets):
        depths.append(combat.resolution.depth)
        return {"repeats": True}

    with _TempMechanism(combat, "测试·伤害计数", CombatEventType.DAMAGE_APPLIED, on_damage):
        for _ in range(40):
            before = player.current_hp
            combat._apply_hostile_damage(player, 1, source=None)
            assert player.current_hp == before - 1
    assert len(depths) >= 1, "机制根本没被分发到（测试前提不成立）"
    assert max(depths) <= 2, f"顺序触发的重复不应嵌套：{depths}"
    assert ctx.depth == 0 and ctx.effect_count > 0


def test_legal_chain_after_life_lost_may_repeat_the_same_effect(tmp_path):
    """失去生命→再生→连击→失去生命 这种「回到同一状态」的链是合法的。

    这里用一个显式推进的模型复现该形态：每次失去生命都触发 1 点回复，
    共 30 轮。判定它「合法」的依据不是效果名字，而是**每次都在消耗状态**
    （这里消耗的是 HP 余额）：链会自然收敛，因此不许被拦。
    """
    state, combat, player, _enemy = _probe_battle(player_hp=100)
    ctx = combat.resolution
    burned = []

    def on_hp_lost(trigger_ctx, targets):
        # 合法的自我修正：失去生命之后用再生把血补回来（净消耗 1 点）
        burned.append(player.current_hp)
        combat.state.apply_heal(player, 1)
        return {"healed": 1}

    with _TempMechanism(combat, "测试·连击再生回环", CombatEventType.DAMAGE_APPLIED, on_hp_lost):
        for _ in range(30):
            combat._apply_hostile_damage(player, 2, source=None)

    assert len(burned) == 30, f"合法链被截断了：只结算 {len(burned)} 次"
    assert ctx.depth == 0
    assert ctx.trip_reason == ""
    assert player.current_hp == 100 - 30


# ---------------------------------------------------------------- 失控拦截

def test_self_triggering_mechanism_is_caught_with_a_diagnostic_chain():
    """A→B→A 的自触发必须被保险丝截断，且错误信息能说明链长什么样。"""
    state, combat, player, _enemy = _probe_battle(player_hp=100)
    real_max = ResolutionContext.MAX_DEPTH
    ResolutionContext.MAX_DEPTH = 8          # 只为把自触发放大到可观测的层数
    try:
        def self_trigger(trigger_ctx, targets):
            # 每次失去生命都再造成一次失去生命：真实对局里的「无限套娃」形态
            combat._apply_hostile_damage(player, 1, source=None)
            return {"recursing": True}

        with _TempMechanism(combat, "测试·自触发", CombatEventType.DAMAGE_APPLIED, self_trigger):
            with pytest.raises(ResolutionDepthError) as excinfo:
                combat._apply_hostile_damage(player, 1, source=None)
        message = str(excinfo.value)
        assert "8 层" in message
        assert "trigger" in message or "damage" in message, message
    finally:
        ResolutionContext.MAX_DEPTH = real_max
    # 截断之后引擎必须干净：深度归零、下次结算正常
    assert combat.resolution.depth == 0
    assert combat.resolution.trip_reason, "终止原因必须可追踪"
    combat.resolution.begin_action("复位", {})
    combat._apply_hostile_damage(player, 1, source=None)
    assert combat.resolution.depth == 0


def test_depth_error_is_a_recursion_error_and_catchable():
    """保持旧契约：越界是 RecursionError（旧 `_effect_chain_depth` 语义）。"""
    assert issubclass(ResolutionDepthError, RecursionError)


def test_budget_fuse_catches_breadth_explosion_at_depth_one():
    """深度不涨、数量爆炸也要拦（旧的深度计数永远拦不住这一形态）。"""
    state, combat, _player, enemy = _probe_battle(enemy_hp=10 ** 6)
    real_max = ResolutionContext.MAX_EFFECTS
    ResolutionContext.MAX_EFFECTS = 12
    try:
        combat.resolution.begin_action("测试：广度爆炸")
        with pytest.raises(ResolutionBudgetError) as excinfo:
            for _ in range(50):
                combat._apply_hostile_damage(enemy, 1, source=None)
        assert "12" in str(excinfo.value)
    finally:
        ResolutionContext.MAX_EFFECTS = real_max
        combat.resolution.end_action()
    assert combat.resolution.depth == 0
    assert combat.resolution.trip_reason.startswith("效果数")


def test_action_rolls_back_and_reports_when_fuse_trips(tmp_path, monkeypatch):
    """API 边界：保险丝触发时行动返回失败、状态可继续使用、错误可读。"""
    engine = build_battle_engine(tmp_path, name="fuse_api")
    monkeypatch.setattr(ResolutionContext, "MAX_DEPTH", 2)
    enemy = next(e for e in engine.state.enemies if e.is_alive)
    hp_before = enemy.current_hp
    result = engine.execute_action("use_daowen",
                                   {"daowen_name": "杀伐", "x": 1,
                                    "target": enemy.name})
    assert result.get("success") is False
    assert "层" in result.get("error", ""), result
    assert engine.combat.resolution.depth == 0
    assert engine.combat.resolution.trip_reason
    # 阈值恢复正常后，同一引擎还能正常打架（不会因为触发过一次就永久卡死）
    monkeypatch.setattr(ResolutionContext, "MAX_DEPTH", 64)
    ok = engine.execute_action("use_daowen",
                               {"daowen_name": "杀伐", "x": 1,
                                "target": enemy.name})
    assert ok.get("success") is True, ok.get("error")
    assert enemy.current_hp <= hp_before if enemy.is_alive else True


def test_trip_reason_and_chain_survive_for_diagnosis():
    """触发后必须留下「为什么被截断」的证据（终止原因 + 链）。"""
    ctx = ResolutionContext(tracing=True)
    real_max = ResolutionContext.MAX_DEPTH
    ResolutionContext.MAX_DEPTH = 4
    try:
        with pytest.raises(ResolutionDepthError):
            for i in range(10):
                token = ctx.enter("damage", f"第{i}层")
                assert token is None or token.depth == i + 1
    finally:
        ResolutionContext.MAX_DEPTH = real_max
    assert ctx.trip_reason == "深度 5 超过 4"
    assert "damage:第0层" in ctx.describe_chain()


# ---------------------------------------------------------------- 循环诊断

def test_a_b_a_b_cycle_is_named_before_reaching_the_depth_bound():
    """A→B→A→B…：不必等到 64 层，同类帧堆到上限就点名报错。"""
    ctx = ResolutionContext()
    assert max(ResolutionContext.MAX_REPEAT * 2,
               ResolutionContext.MAX_DEPTH) >= ResolutionContext.MAX_REPEAT * 2
    tokens = []
    with pytest.raises(ResolutionCycleError) as excinfo:
        for _ in range(ResolutionContext.MAX_REPEAT + 5):
            tokens.append(ctx.enter(KIND_DAMAGE, "伤害"))
            tokens.append(ctx.enter(KIND_HEAL, "再生"))     # A→B→A→B 形态
    assert KIND_DAMAGE in str(excinfo.value)
    assert "同一链" in str(excinfo.value)
    assert ctx.trip_reason
    for token in reversed(tokens):
        ctx.leave(token)
    assert ctx.depth == 0
    assert ctx._kind_counts == {}, "循环诊断的账本必须随退出清干净"


def test_a_b_c_a_cycle_is_also_caught():
    """A→B→C→A：换了一类又绕回来的形态同样要被抓到。"""
    ctx = ResolutionContext()
    tokens = []
    with pytest.raises(ResolutionCycleError):
        for _ in range(ResolutionContext.MAX_REPEAT + 2):
            for kind in (KIND_DAMAGE, KIND_HEAL, "trigger"):
                tokens.append(ctx.enter(kind))
    for token in reversed(tokens):
        ctx.leave(token)
    assert ctx.depth == 0 and ctx._kind_counts == {}


def test_repeats_that_are_sequential_never_accumulate():
    """顺序重复（合法的）与嵌套重复（循环）必须被区分开——这是判定标准本身。"""
    ctx = ResolutionContext()
    for _ in range(ResolutionContext.MAX_REPEAT * 3):
        token = ctx.enter(KIND_DAMAGE)
        ctx.leave(token)
    assert ctx.depth == 0 and ctx._kind_counts == {}
    assert ctx.trip_reason == ""


def test_engine_self_trigger_now_trips_the_cycle_fuse():
    """引擎级：自触发机制在 16 层内被点名（而不是等到 64 层）。"""
    state, combat, player, _enemy = _probe_battle(player_hp=100)

    def self_trigger(trigger_ctx, targets):
        combat._apply_hostile_damage(player, 1, source=None)
        return {"recursing": True}

    with _TempMechanism(combat, "测试·自触发2", CombatEventType.DAMAGE_APPLIED,
                        self_trigger):
        with pytest.raises(ResolutionCycleError):
            combat._apply_hostile_damage(player, 1, source=None)
    assert combat.resolution.depth == 0
    assert combat.resolution.trip_reason.startswith("同类结算")


def test_explain_answers_why_a_target_did_not_die(tmp_path):
    """trace 必须能回答「这个怪为什么没死」——给出事实，不重新推理。"""
    engine = build_battle_engine(tmp_path, name="explain")
    ctx = engine.combat.resolution
    ctx.set_tracing(True)
    enemy = next(e for e in engine.state.enemies if e.is_alive)
    engine.execute_action("use_daowen",
                          {"daowen_name": "杀伐", "x": 1, "target": enemy.name})
    text = ctx.explain(enemy.name)
    assert "damage" in text and enemy.name in text
    assert "hp " in text, "必须能看到血量变化，否则回答不了「为什么没死」"
    assert "没有涉及" in ctx.explain("不存在的实体")
