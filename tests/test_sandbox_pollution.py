"""沙盒零污染契约：预演改了什么，都必须原样还原。

这份文件把「预演不留痕迹」变成**自动检测**，而不是靠人记住哪些对象不能碰：

1. 通用快照（``engine/pollution_guard.py``）遍历整个引擎对象图——game state、
   combat runtime、dice/RNG、行动历史、事件池、中断队列、实体运行态全部在内。
   新字段自动纳入，不需要往白名单里加名字。
2. 覆盖成功 / 失败 / 抛异常三条路径：最容易漏的是「沙盒里抛异常后没走恢复」。
3. 负面对照：故意在预演里写脏真实对象，检测器必须报错——否则「检测通过」没有意义。
4. 完整性闸门：引擎/战斗/状态上出现**未分类的可变属性**时直接失败，迫使新增
   状态时同步登记到 ``engine/sandbox.py`` 的隔离清单。

历史背景（两次真实事故，见报告 §2）：
  * 2026-09-19 combat 的 id 键运行态字典被沙盒实体写脏 → AI 实盘轨迹漂移；
  * 2026-09-20 预演【结算事件】写脏 ``event_pool.triggered/current``。
两者都是「新增了一处引擎侧可变状态而沙盒没跟着加」。
"""
from __future__ import annotations

import copy
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import sandbox                                # noqa: E402
from engine.ai_preview import ActionPreview               # noqa: E402
from engine.api import GameEngine                         # noqa: E402
from engine.pollution_guard import (                      # noqa: E402
    PollutionGuard, RuntimePollutionError, assert_runtime_unchanged,
    snapshot_runtime_state,
)
from tests.preview_support import (                       # noqa: E402
    build_battle_engine, build_pre_battle_engine,
)


# ---------------------------------------------------------------- 夹具

@pytest.fixture()
def engine(tmp_path):
    return build_battle_engine(tmp_path)


@pytest.fixture()
def out_of_battle_engine(tmp_path):
    """局外阶段引擎：事件只能在局外结算（战斗内会被阶段门禁拒掉）。"""
    return build_pre_battle_engine(tmp_path)


PREVIEW_CASES = [
    ("use_daowen", {"daowen_name": "庇护", "x": 1, "target": "贾凡"}),
    ("use_daowen", {"daowen_name": "庇护", "x": 2, "target": "贾凡"}),
    ("use_daowen", {"daowen_name": "再生", "x": 1, "target": "贾凡"}),
    ("use_daowen", {"daowen_name": "冲击", "x": 1}),
    ("use_daowen", {"daowen_name": "杀伐", "x": 1}),
    ("use_daowen", {"daowen_name": "连击", "x": 1}),
    ("use_daowen", {"daowen_name": "不存在"}),          # 未知道纹
    ("use_daowen", {"daowen_name": "庇护", "x": 9999}),  # 代价不足
    ("use_daowen", {"daowen_name": "杀伐", "x": 0}),     # X 非法
    ("declare_parry", {"actor_ref": "player:0"}),
    ("prepare_monster_phase", {}),
    ("resolve_monster_phase", {"token": "不存在的token"}),
    ("round_end", {}),
    ("consume_item", {"name": "活性土壤"}),
    ("resolve_event", {"event": "不存在的事件", "option_id": 1}),
    ("这个行动不存在", {}),
]


@pytest.mark.parametrize("action,params", PREVIEW_CASES)
def test_preview_leaves_whole_engine_unchanged(engine, action, params):
    """全图零污染：一次预演前后，引擎对象图必须逐路径一致（含异常路径）。"""
    before = snapshot_runtime_state(engine)
    try:
        ActionPreview(engine).preview(action, dict(params))
    except Exception:
        # 预演自身抛异常也是合法路径——状态同样必须干净（由下面的断言兜住）。
        pass
    assert_runtime_unchanged(before, snapshot_runtime_state(engine),
                             context=f"{action} {params}")


def test_preview_sequence_two_phase_leaves_no_trace(engine):
    """两段动作（prepare_attack → resolve_attack）整串预演同样零污染。"""
    before = snapshot_runtime_state(engine)
    ActionPreview(engine).preview_sequence([
        ("prepare_attack", {}),
        ("resolve_attack", lambda prev: {
            "token": (prev.get("result") or {}).get("token"),
            "target": "赌鬼", "dodge": False,
        }),
    ])
    assert_runtime_unchanged(before, snapshot_runtime_state(engine),
                             context="普攻两段")


def test_preview_event_resolution_does_not_consume_the_event(out_of_battle_engine):
    """回归：预演【结算事件】不得把真实事件标记为已触发。

    2026-09-20 修掉的真实污染——沙盒换 state/dice 但不换 event_pool，
    结算选项会写 ``event_pool.triggered`` 与 ``event_pool.current``。
    """
    engine = out_of_battle_engine
    pool = engine.event_pool
    name = pool.build_pool(engine.state.current_region or "龙心谷")[0]
    pool.current = name
    reject_option = next(o for o in pool.events[name]["options"]
                         if engine._is_reject_option_text(o["text"]))
    option_id = reject_option["id"]
    triggered_before = set(pool.triggered)

    out = ActionPreview(engine).preview("resolve_event",
                                        {"event": name, "option_id": option_id})
    assert (out.get("result") or {}).get("success"), "预演应当能成功结算该事件"

    assert pool.triggered == triggered_before, "预演把真实事件标记为已触发"
    assert pool.current == name, "预演清空了真实待结算事件"


def test_preview_restores_runtime_containers_in_place(engine):
    """运行态容器必须**原地**恢复：身份被别处引用，换成新对象等于状态分叉。"""
    combat = engine.combat
    identities = {k: id(getattr(combat, k, None))
                  for k in sandbox.COMBAT_RUNTIME_ATTRS}
    ActionPreview(engine).preview("use_daowen",
                                  {"daowen_name": "杀伐", "x": 1})
    for key, before_id in identities.items():
        if before_id is None:
            # 惰性创建属性：本来没有，预演后也不该凭空出现
            assert not hasattr(combat, key), f"{key} 在预演后被凭空创建"
            continue
        assert id(getattr(combat, key, None)) == before_id, \
            f"{key} 被换成了新对象（外部引用会看见旧内容）"


def test_guard_detects_deliberate_pollution(engine):
    """负面对照：故意写脏真实运行态，检测器必须报错（否则检测形同虚设）。"""
    combat = engine.combat

    class _PollutingPreview(ActionPreview):
        def preview_sequence(self, steps):
            out = super().preview_sequence(steps)
            combat._monster_activated[id(engine.state.player)] = {"伪造"}
            return out

    before = snapshot_runtime_state(engine)
    _PollutingPreview(engine).preview("use_daowen", {"daowen_name": "庇护", "x": 1,
                                                    "target": "贾凡"})
    with pytest.raises(RuntimePollutionError) as excinfo:
        assert_runtime_unchanged(before, snapshot_runtime_state(engine),
                                 context="负面对照")
    assert "_monster_activated" in str(excinfo.value)


def test_guard_detects_pollution_inside_event_path(engine):
    """负面对照之二：事件池污染也必须被同一个检测器抓到（覆盖已修的那条路径）。"""
    with pytest.raises(RuntimePollutionError):
        with PollutionGuard(engine, "负面对照-事件池"):
            engine.event_pool.triggered.add("伪造事件")
            engine.event_pool.current = "伪造事件"


def test_pollution_guard_context_manager_passes_on_clean_preview(engine):
    """上下文管理器在干净预演上必须静默通过。"""
    with PollutionGuard(engine, "干净预演", check_record_contents=True):
        ActionPreview(engine).preview("use_daowen", {"daowen_name": "再生", "x": 1,
                                                    "target": "贾凡"})


def test_repeated_previews_do_not_accumulate_state(engine):
    """连续预演（含失败与未知动作）不得累积任何痕迹——第 N 次与第 1 次同口径。"""
    before = snapshot_runtime_state(engine)
    preview = ActionPreview(engine)
    for _ in range(3):
        for action, params in PREVIEW_CASES:
            try:
                preview.preview(action, dict(params))
            except Exception:
                pass
    assert_runtime_unchanged(before, snapshot_runtime_state(engine),
                             context="连续 3 轮 × 16 例预演")


def test_random_preview_sequences_leave_no_trace(engine):
    """随机组合预演（含非法参数）——组合多了也不许留下痕迹。"""
    rng = random.Random(20260920)
    names = ["庇护", "再生", "冲击", "杀伐", "连击", "不存在", "点金"]
    before = snapshot_runtime_state(engine)
    preview = ActionPreview(engine)
    for _ in range(60):
        name = rng.choice(names)
        params = {"daowen_name": name, "x": rng.choice([0, 1, 2, 5, 99])}
        if rng.random() < 0.5:
            params["target"] = rng.choice(["贾凡", "赌鬼", "不存在的人"])
        try:
            preview.preview("use_daowen", params)
        except Exception:
            pass
    assert_runtime_unchanged(before, snapshot_runtime_state(engine),
                             context="随机 60 次预演")


def test_preview_matches_execute_after_many_previews(engine, tmp_path):
    """预演零污染的**行为含义**：预演过很多次之后，正式执行仍与没预演过一致。"""
    control = build_battle_engine(tmp_path, name="control")

    preview = ActionPreview(engine)
    for _ in range(30):
        try:
            preview.preview("use_daowen", {"daowen_name": "杀伐", "x": 1})
        except Exception:
            pass
    res_a = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1})
    res_b = control.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1})
    assert res_a.get("success") == res_b.get("success")
    assert engine.state.player.current_hp == control.state.player.current_hp
    assert engine.state.player.current_mana == control.state.player.current_mana
    assert (engine.combat._monster_daowen_round_used.keys()
            == control.combat._monster_daowen_round_used.keys()), \
        "预演在真实运行态里留下了沙盒实体的老键"


# ------------------------------------------------------- 完整性闸门

#: 引擎 / 战斗 / 状态上允许**可变**的属性及其归属。
#:   isolated —— 沙盒作用域负责隔离（见 engine/sandbox.py）
#:   rule_data —— 规则数据，引擎构造后只读
#:   io —— 外部 IO / 配置 / 可选组件，不属于战斗结算
_CLASSIFIED_ENGINE_ATTRS = {
    "state": "isolated", "dice": "isolated", "combat": "isolated",
    "_pending_interrupts": "isolated", "_action_history": "isolated",
    "_last_result": "isolated", "event_pool": "isolated",
    # 【重来本场】战始快照：由 engine/sandbox.py 的 snapshot/restore_engine_side
    # 存引用并原样换回（只整体重新赋值，不就地改写，故无需 deepcopy）。
    "_battle_restart_snapshot": "isolated",
    "monster_pool": "rule_data",
    "death_book": "io", "rulings_db": "io", "_validator": "io", "_rule_sync": "io",
    "save_dir": "io", "death_book_path": "io", "sealed_candidate_path": "io",
}
_CLASSIFIED_COMBAT_ATTRS = {
    **{name: "isolated" for name in sandbox.COMBAT_RUNTIME_ATTRS},
    **{name: "isolated" for name in sandbox.CONTEXT_OBJECTS},
    "state": "isolated", "dice": "isolated", "combat_log": "isolated",
    "hook_manager": "rule_data", "mechanism_bus": "rule_data",
    "_attack_after_window_target": "isolated", "_hp_loss_ctx": "isolated",
}


def _mutable_attrs(obj) -> set:
    """对象上「可能是可变状态」的属性名（排除不可变标量与可调用对象）。"""
    names = set()
    for key, value in vars(obj).items():
        if callable(value):
            continue
        if isinstance(value, (int, float, str, bool, bytes, type(None))):
            continue
        names.add(key)
    return names


def test_engine_side_mutable_state_is_classified(engine):
    """闸门：引擎上出现未登记的可变属性时失败——提醒把它并入沙盒隔离清单。

    这条用例是「不依赖程序员记忆」的落点：新增状态不会静默逃出监管。
    """
    unknown = _mutable_attrs(engine) - set(_CLASSIFIED_ENGINE_ATTRS)
    assert unknown == set(), (
        f"GameEngine 新增了未分类的可变属性 {sorted(unknown)}；"
        f"请判断它是否会被沙盒执行写脏，并登记到 engine/sandbox.py 的隔离清单"
        f"（同时更新本测试的 _CLASSIFIED_ENGINE_ATTRS）")


def test_combat_side_mutable_state_is_classified(engine):
    """闸门：CombatEngine 同理——运行态清单的唯一事实源是 engine/sandbox.py。"""
    unknown = _mutable_attrs(engine.combat) - set(_CLASSIFIED_COMBAT_ATTRS)
    assert unknown == set(), (
        f"CombatEngine 新增了未分类的可变属性 {sorted(unknown)}；"
        f"请登记到 engine/sandbox.py 的 COMBAT_RUNTIME_ATTRS 或本测试的白名单")


def test_sandbox_spec_stays_small_and_explicit():
    """隔离清单是白名单式审计点：条目突然变多通常意味着有人在扩大沙盒面。"""
    assert len(sandbox.COMBAT_RUNTIME_ATTRS) <= 16
    assert set(sandbox.SWAPPED_ROOTS) == {"state", "dice"}


def test_guard_is_off_in_the_production_path(engine, monkeypatch):
    """正式路径不得自动开检测：默认关闭时，预演不许产生任何快照开销。"""
    from engine import ai_preview

    assert ai_preview._GUARD_ON is False, "默认必须关闭（LJ_POLLUTION_GUARD 未设置）"

    calls = {"n": 0}
    real = ai_preview.snapshot_runtime_state

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(ai_preview, "snapshot_runtime_state", counting)
    ActionPreview(engine).preview("use_daowen", {"daowen_name": "庇护", "x": 1,
                                                "target": "贾凡"})
    assert calls["n"] == 0, "关闭状态下仍做了快照＝正式路径被加上开销"


def test_deep_audit_agrees_with_fast_mode(engine):
    """两种扫描口径必须给出同一结论（深模式抓内容，快模式抓结构）。"""
    before_fast = snapshot_runtime_state(engine, check_record_contents=False)
    before_deep = snapshot_runtime_state(engine)
    ActionPreview(engine).preview("use_daowen", {"daowen_name": "冲击", "x": 1})
    after_fast = snapshot_runtime_state(engine, check_record_contents=False)
    after_deep = snapshot_runtime_state(engine)
    assert before_fast.diff(after_fast) == []
    assert before_deep.diff(after_deep) == []


def test_snapshot_is_a_value_not_a_live_reference(engine):
    """快照必须是**值**：否则"前后相等"永远成立，检测形同虚设。"""
    snap = snapshot_runtime_state(engine)
    player = engine.state.player
    engine.combat._monster_activated[id(player)] = {"伪造"}

    diff = snap.diff(snapshot_runtime_state(engine))
    assert any("_monster_activated" in line for line in diff), \
        "引擎已改动，旧快照却没报差异——快照是活引用"

    del engine.combat._monster_activated[id(player)]
    assert snap.diff(snapshot_runtime_state(engine)) == []
    # 快照内容可深拷贝（可打印/可存档/不会把引擎钉在内存里）
    assert copy.deepcopy(snap.entries) == snap.entries


def test_pollution_error_message_names_the_path(engine):
    """报警要能直接指路：错误信息里必须出现具体字段路径。"""
    before = snapshot_runtime_state(engine)
    engine.combat._monster_daowen_round_used[123456] = (1, {"伪造"})
    with pytest.raises(RuntimePollutionError) as excinfo:
        assert_runtime_unchanged(before, snapshot_runtime_state(engine),
                                 context="路径可读性")
    message = str(excinfo.value)
    assert "_monster_daowen_round_used" in message
    assert "路径可读性" in message
