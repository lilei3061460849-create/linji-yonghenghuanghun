"""ResolutionContext（结算生命周期上下文）的契约。

它只做两件事：**记账**（深度 / 预算 / 链）与**保险丝**（超限抛错）。
本文件锁死三件事：

1. 记账正确：进出成对、深度归零、异常路径也归零、链能描述因果。
2. 保险丝只在**正常规则不可能到达**的范围外触发——阈值必须有实测余量，
   否则它会变成「改动游戏结果」的规则。
3. 不参与规则判定：任何开关都不改变战斗结果（同种子对照）。
"""
from __future__ import annotations

import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine                            # noqa: E402
from engine.combat import CombatEngine                       # noqa: E402
from engine.dice import DiceEngine                           # noqa: E402
from engine.models import Entity, GameState                  # noqa: E402
from engine.resolution import (                              # noqa: E402
    KIND_DAMAGE, KIND_HEAL, KIND_OTHER, ResolutionBudgetError,
    ResolutionContext, ResolutionDepthError,
)
from tests.preview_support import build_battle_engine       # noqa: E402


# ---------------------------------------------------------------- 纯记账

def test_enter_leave_are_balanced():
    ctx = ResolutionContext()
    assert ctx.depth == 0 and ctx.effect_count == 0
    token = ctx.enter(KIND_DAMAGE, "杀伐→赌鬼")
    assert ctx.depth == 1 and ctx.effect_count == 1
    ctx.leave(token)
    assert ctx.depth == 0
    assert ctx.effect_count == 1, "效果计数是本次行动的累计量，不随退出回退"


def test_nested_depth_tracks_actual_nesting():
    ctx = ResolutionContext()
    a = ctx.enter(KIND_DAMAGE, "外")
    b = ctx.enter(KIND_HEAL, "内")
    assert ctx.depth == 2
    ctx.leave(b)
    assert ctx.depth == 1
    ctx.leave(a)
    assert ctx.depth == 0


def test_leave_on_unbalanced_call_never_goes_negative():
    """防御：多调一次 leave 不许把深度搞成负数（否则保险丝会被永久绕过）。"""
    ctx = ResolutionContext()
    ctx.leave(None)
    assert ctx.depth == 0


def test_depth_fuse_raises_at_threshold():
    ctx = ResolutionContext()
    # 每层换一种 kind，避免先撞上同类循环诊断（那条另有测试）
    kinds = [f"k{i}" for i in range(ctx.MAX_DEPTH)]
    tokens = [ctx.enter(kind) for kind in kinds]
    assert ctx.depth == ctx.MAX_DEPTH
    with pytest.raises(ResolutionDepthError) as excinfo:
        ctx.enter("越界")
    assert str(ctx.MAX_DEPTH) in str(excinfo.value)
    assert ctx.trip_reason, "终止原因必须可追踪"
    for token in tokens:
        ctx.leave(token)
    assert ctx.depth == 0, "抛错后仍必须能正常退出（try/finally 语义）"


def test_budget_fuse_raises_on_breadth_explosion():
    """深度不涨、数量爆炸的形态：靠预算保险丝拦（这是原来完全没有的保护）。

    预算的计量单位是**一次顶层行动**（begin_action ~ end_action），
    所以这里必须先开一次行动——不开口径就没有意义。
    """
    ctx = ResolutionContext()
    ctx.begin_action("测试")
    for _ in range(ctx.MAX_EFFECTS):
        assert ctx.depth == 0, "本例刻意不嵌套"
        token = ctx.enter(KIND_OTHER)
        ctx.leave(token)
        assert ctx.depth == 0, "本例刻意不嵌套，只堆数量"
    with pytest.raises(ResolutionBudgetError):
        token = ctx.enter(KIND_OTHER)
        ctx.leave(token)
    ctx.end_action()


def test_top_level_calls_outside_an_action_start_a_fresh_budget():
    """直调内部入口（无 API 行动包裹）时，每次顶层结算重新起算预算。

    否则工具/测试直驱引擎会把预算算成「一次无限长的行动」并误报失控。
    """
    ctx = ResolutionContext()
    for _ in range(ctx.MAX_EFFECTS + 100):
        token = ctx.enter(KIND_OTHER)
        ctx.leave(token)
    assert ctx.trip_reason == "", "直调内部入口不应触发预算保险丝"
    assert ctx.effect_count == 1, "每次顶层结算自成一次（计数为 1）"


def test_begin_action_resets_counters_and_chain():
    ctx = ResolutionContext(tracing=True)
    ctx.enter(KIND_DAMAGE, "旧行动")
    ctx.begin_action("use_daowen", {})
    assert ctx.depth == 0 and ctx.effect_count == 0 and ctx.chain() == []
    assert ctx.action == "use_daowen"


def test_thresholds_have_measured_headroom():
    """阈值必须显著高于实测峰值（全路径记账后实测：深度 5 / 单行动效果 83）。

    留 10 倍以上余量是本条硬性要求——否则保险丝会开始改游戏结果。
    实测脚本：`sim/threshold_evidence.py`。
    """
    assert ResolutionContext.MAX_DEPTH >= 50
    assert CombatEngine.MAX_EFFECT_CHAIN_DEPTH == ResolutionContext.MAX_DEPTH
    assert ResolutionContext.MAX_EFFECTS >= 830


# ---------------------------------------------------------------- trace

def test_tracing_records_the_causal_chain():
    ctx = ResolutionContext(tracing=True)
    a = ctx.enter(KIND_DAMAGE, "杀伐→赌鬼")
    b = ctx.enter(KIND_HEAL, "再生→赌鬼")
    chain = ctx.chain()
    assert [f.kind for f in chain] == [KIND_DAMAGE, KIND_HEAL]
    assert chain[1].parent == chain[0].seq, "子帧必须挂到父帧上"
    assert chain[0].depth == 1 and chain[1].depth == 2
    assert "杀伐→赌鬼" in ctx.describe_chain()
    ctx.leave(b)
    ctx.leave(a)
    assert ctx.chain() == [], "退出后链必须清空"


def test_tracing_off_costs_no_allocation():
    """默认关闭：enter 不建对象、不往链里追加（正式路径零分配）。"""
    ctx = ResolutionContext()
    assert ctx.tracing is False
    token = ctx.enter(KIND_DAMAGE, "x")
    assert token is None, "未开 trace 时不应创建 ResolutionFrame"
    assert ctx.chain() == []
    ctx.leave(token)


def test_annotate_and_note_trigger_only_in_trace_mode():
    ctx = ResolutionContext()
    ctx.annotate("k", "v")
    ctx.note_trigger("失去生命")
    assert ctx.scratch == {} and ctx.seen_triggers == [], "关闭 trace 时不留数据"
    ctx.set_tracing(True)
    ctx.annotate("k", "v")
    ctx.note_trigger("失去生命")
    assert ctx.scratch == {"k": "v"} and ctx.seen_triggers == ["失去生命"]


def test_set_tracing_false_clears_chain():
    ctx = ResolutionContext(tracing=True)
    ctx.enter(KIND_OTHER, "x")
    ctx.set_tracing(False)
    assert ctx.chain() == []


# ---------------------------------------------------------------- 沙盒

def test_snapshot_restore_is_in_place():
    ctx = ResolutionContext(tracing=True)
    ctx.enter(KIND_DAMAGE, "真实")
    token = ctx.snapshot()
    ctx.enter(KIND_HEAL, "沙盒内")
    ctx.enter(KIND_OTHER, "沙盒内更深")
    ctx.restore(token)
    assert ctx.depth == 1, "沙盒内的深度必须被换回"
    assert [f.label for f in ctx.chain()] == ["真实"]


def test_preview_does_not_consume_real_resolution_budget(tmp_path, monkeypatch):
    """预演不是真实行动：不得吃掉真实 action 的深度/预算。"""
    engine = build_battle_engine(tmp_path)
    combat = engine.combat

    markers = {}
    real_begin = combat.resolution.begin_action

    def recording_begin(action, params=None):
        real_begin(action, params)
        markers[action] = (combat.resolution.depth, combat.resolution.effect_count)

    monkeypatch.setattr(combat.resolution, "begin_action", recording_begin)

    from engine.ai_preview import ActionPreview
    for _ in range(5):
        ActionPreview(engine).preview("use_daowen",
                                      {"daowen_name": "杀伐", "x": 1,
                                       "target": "赌鬼"})

    assert combat.resolution.depth == 0, "预演结束后深度必须归零"
    assert combat.resolution.effect_count == 0, "预演不得把预算算到真实行动头上"


# ---------------------------------------------------------------- 接线

def test_action_boundary_resets_counters_on_success_and_failure(tmp_path):
    engine = build_battle_engine(tmp_path)
    engine.execute_action("use_daowen",
                          {"daowen_name": "杀伐", "x": 1, "target": "赌鬼"})
    assert engine.combat.resolution.depth == 0
    assert engine.combat.resolution.action == ""
    engine.execute_action("use_daowen", {"daowen_name": "不存在"})
    assert engine.combat.resolution.depth == 0


def test_action_boundary_resets_counters_after_exception(tmp_path, monkeypatch):
    """异常也必须在边界处收尾，否则下一次行动会带着上一次的深度。"""
    engine = build_battle_engine(tmp_path)

    def boom(self, action_type, params):
        raise RuntimeError("故意炸")

    monkeypatch.setattr(GameEngine, "_dispatch_action", boom)
    target = next(e.name for e in engine.state.enemies if e.is_alive)
    result = engine.execute_action("use_daowen",
                                   {"daowen_name": "杀伐", "x": 1, "target": target})
    assert result.get("success") is False
    assert engine.combat.resolution.depth == 0, "异常路径未收尾"


def test_effect_count_is_nonzero_inside_a_damaging_action(tmp_path, monkeypatch):
    """记账要真的在工作：一次造成伤害的行动，过程中效果次数必须 > 0。"""
    engine = build_battle_engine(tmp_path)
    ctx = engine.combat.resolution
    seen = {"max_effects": 0, "max_depth": 0}

    real_leave = ResolutionContext.leave

    def spy_leave(self, token=None):
        seen["max_effects"] = max(seen["max_effects"], self.effect_count)
        seen["max_depth"] = max(seen["max_depth"], self.depth)
        return real_leave(self, token)

    monkeypatch.setattr(ResolutionContext, "leave", spy_leave)
    target = next(e.name for e in engine.state.enemies if e.is_alive)
    result = engine.execute_action("use_daowen",
                                   {"daowen_name": "杀伐", "x": 1, "target": target})
    assert result.get("success"), f"动作应当成功：{result.get('error')}"
    assert seen["max_effects"] >= 1, "一次造成伤害的行动必须记录到效果次数"
    assert seen["max_depth"] >= 1, "必须记录到嵌套深度"
    assert ctx.depth == 0, "行动结束后深度必须归零"
    assert ctx.effect_count >= 1, "本次行动的效果数应保留到下一次 begin_action"
    engine.execute_action("use_daowen", {"daowen_name": "再生", "x": 1,
                                         "target": "贾凡"})
    assert ctx.effect_count < 100, "新行动必须重置预算，不能累加"


# ---------------------------------------------------------------- 兼容别名

def test_effect_chain_depth_alias_still_works():
    """`_effect_chain_depth` 是历史契约（sim/win_only_ai.py 与旧测试直接读写）。"""
    state = GameState()
    combat = CombatEngine(state, DiceEngine())
    assert combat._effect_chain_depth == 0
    combat._effect_chain_depth = 7
    assert combat.resolution.depth == 7
    combat._effect_chain_depth = 0
    assert combat.resolution.depth == 0


def test_damage_fuse_still_raises_recursion_error():
    """旧契约：越界抛 RecursionError（ResolutionDepthError 是其子类）。"""
    state = GameState()
    combat = CombatEngine(state, DiceEngine())
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100)
    enemy = Entity("E", "怪物", blood_limit=100, current_hp=100)
    state.player, state.enemies = player, [enemy]

    combat._effect_chain_depth = combat.MAX_EFFECT_CHAIN_DEPTH
    with pytest.raises(RecursionError):
        combat._apply_hostile_damage(enemy, 1, source=player)
    combat._effect_chain_depth = 0
    assert combat.resolution.depth == 0


def test_context_does_not_change_battle_outcome(tmp_path):
    """不参与规则判定：开不开 trace 的同种子对局必须逐字段一致。"""
    results = []
    for tracing in (False, True):
        engine = build_battle_engine(tmp_path, name=f"trace{int(tracing)}")
        engine.combat.resolution.set_tracing(tracing)
        from sim import build_learner as bl
        # 2026-10-13：_play 默认共享生产封存路径，第一局的封存会被第二局拿去死斗
        # （跨局泄漏；两局都不开 trace 也照样分叉）。每局独立 lab_paths 隔离，
        # 两局看到的都是“无封存”同一初始环境，比较的才是 trace 开关本身。
        tag = f"trace{int(tracing)}"
        lab = {"db_path": str(tmp_path / f"{tag}.db"),
               "sealed_path": str(tmp_path / f"{tag}_seal.json"),
               "death_book_path": str(tmp_path / f"{tag}_book.md")}
        results.append(bl.play("坠落", ["杀伐", "连击", "再生", "庇护", "透支"],
                               "罪孽都市", seed=3, rng=random.Random(3),
                               lab_paths=lab))
    assert results[0] == results[1], "trace 打开改变了战斗结果"
