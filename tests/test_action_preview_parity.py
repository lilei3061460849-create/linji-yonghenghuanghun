"""预演（ActionPreview）与正式执行（execute_action）的等价性契约。

背景（2026-09-19 性能优化）：`execute_action` 拆成
`execute_action`（事务层：快照 + 回滚）与 `_execute_action_core`（唯一执行实现）。
预演在自带 sandbox 里直接调用 core，不再重复建立 transaction snapshot。

本文件锁死两条契约：
  1. **规则等价**：同一初始状态 / 同一随机种子下，
     「先预演再正式执行」与「只正式执行」得到**逐字段相同**的返回与最终状态；
     预演本身对真实引擎零副作用（不消耗随机源、不改状态、不留历史）。
  2. **零重复快照**：一次预演不得再建 transaction snapshot（deepcopy 次数护栏）——
     否则双份整状态复制会被悄悄改回来。

对照组构造：两个同种子、同流程的引擎，只有「是否先跑过预演」不同。
"""
from __future__ import annotations

import copy
import json
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.ai_preview import ActionPreview          # noqa: E402
from tests.setup_support import finish_initial_daowen  # noqa: E402

# runtime_id / game_id / token / 事件 id 每次构造都不同，属于身份标识而非规则事实。
_VOLATILE = {"runtime_id", "game_id", "token", "timestamp", "event_id", "parent_event_id"}


def _norm(obj):
    if isinstance(obj, dict):
        return {k: ("<volatile>" if k in _VOLATILE else _norm(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_norm(v) for v in obj]
    return obj


def _fingerprint(state) -> str:
    return json.dumps(_norm(state.to_dict()), ensure_ascii=False, sort_keys=True)


def _new_engine(tmp_path, name: str, seed: int = 4):
    from engine.api import GameEngine
    e = GameEngine(db_path=str(tmp_path / f"{name}.db"),
                   save_dir=str(tmp_path / name),
                   death_book_path=str(tmp_path / f"{name}_book.md"),
                   rng_seed=seed)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11,
                      "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    for dw in ("庇护", "再生", "冲击"):
        e.execute_action("pre_battle_action",
                         {"sub_action": "学习", "sub": "daowen", "name": dw})
    e.state.energy = 0
    choices = {}
    relic = e.state.relics[0].name if e.state.relics else ""
    # 2026-10-13：【三相残韵盘】删除，战始显式决策预填不再需要。
    e.execute_action("battle_start", {"relic_choices": choices})
    e.execute_action("round_start", {})
    return e


CASES = [
    ("use_daowen", {"daowen_name": "庇护", "x": 1, "target": "贾凡"}),
    ("use_daowen", {"daowen_name": "庇护", "x": 2, "target": "贾凡"}),
    ("use_daowen", {"daowen_name": "冲击", "x": 1}),
    ("use_daowen", {"daowen_name": "再生", "x": 1, "target": "贾凡"}),
    ("use_daowen", {"daowen_name": "不存在"}),
    ("round_end", {}),
    ("prepare_attack", {}),
    # 结构审查补充：这三条路径与 use_daowen 走不同内部实现
    # （旧招架动作的兼容拒绝 / 残韵转换 / 瞬发法术执行器），预演须与正式执行同源。
    ("declare_parry", {}),
    ("use_resonance", {"source_daowen": "再生", "resonance_type": "反转"}),
    ("cast", {"flow": "发动杀伐X于目标", "steps": [{"x": 1}], "target_ref": "enemy:0"}),
]


@pytest.fixture()
def engines(tmp_path):
    """两个同种子引擎：A 会先跑预演，B 只正式执行（对照组）。"""
    return _new_engine(tmp_path, "a"), _new_engine(tmp_path, "b")


def test_same_seed_engines_start_identical(engines):
    """对照前提：两局流程相同 ⇒ 初始状态逐字段一致。"""
    a, b = engines
    assert _fingerprint(a.state) == _fingerprint(b.state)


@pytest.mark.parametrize("action,params", CASES)
def test_preview_equals_formal_execution(engines, action, params):
    """规则等价：预演不改真实状态，且预演后正式执行的结果与未预演时逐字段相同。"""
    a, b = engines
    before = _fingerprint(a.state)
    history_before = len(a._action_history)

    pv = ActionPreview(a).preview(action, dict(params))

    assert _fingerprint(a.state) == before, "预演泄漏：真实状态被改动"
    assert len(a._action_history) == history_before, "预演泄漏：真实行动历史被写入"

    res_a = a.execute_action(action, dict(params))
    res_b = b.execute_action(action, dict(params))

    assert _norm(res_a) == _norm(res_b), "预演后正式执行的返回与未预演时不同"
    assert _fingerprint(a.state) == _fingerprint(b.state), "最终状态漂移"
    # 预演结果与正式执行同源：成功/失败口径必须一致
    assert bool((pv.get("result") or {}).get("success")) == bool(res_a.get("success"))


def test_preview_sequence_attack_two_phase(engines):
    """两阶段动作（prepare_attack → resolve_attack）整串预演的等价性。"""
    a, b = engines
    before = _fingerprint(a.state)
    pv = ActionPreview(a).preview_sequence([
        ("prepare_attack", {}),
        ("resolve_attack", lambda prev: {
            "token": (prev.get("result") or {}).get("token"),
            "target": "怪物",
            "dodge": False,
        }),
    ])
    assert _fingerprint(a.state) == before, "预演泄漏：真实状态被改动"
    assert pv.get("results") is not None


def test_preview_leaves_combat_runtime_untouched(engines):
    """预演零副作用（运行态口径）：combat 自己的字典也不得被沙盒写脏。

    这些字典按 `id(entity)` 建索引，而沙盒实体是深拷贝副本：预演一旦把沙盒
    实体的键写进真实引擎，就会留下一批「老键」；下次沙盒副本极可能复用同一
    地址，于是下一次预演会「看见」上一次预演用过的道纹（每回合每道纹至多
    一次），候选集在预演里悄悄缩水——adfd9d8 的实盘轨迹漂移即源于此
    （2026-09-19：修 engine/ai_preview.py 的 _RUNTIME_ATTRS 保存/恢复前，
    强制 transaction=True 的对照轨迹与优化树一致，说明差额全部出自这里）。
    """
    a, _ = engines
    combat = a.combat

    def rt():
        return {k: copy.deepcopy(getattr(combat, k, None))
                for k in ActionPreview._RUNTIME_ATTRS}

    before = rt()
    # 逐条预演若干会被引擎写运行态的路径（命中/失败都要覆盖）
    for action, params in (("use_daowen", {"daowen_name": "冲击", "x": 1}),
                           ("use_daowen", {"daowen_name": "庇护", "x": 1,
                                           "target": "贾凡"}),
                           ("use_daowen", {"daowen_name": "不存在"}),
                           ("prepare_monster_phase", {})):
        ActionPreview(a).preview(action, dict(params))
    assert rt() == before, "预演泄漏：真实引擎的战斗运行态被写入"

    # 真实实体之外不得出现任何「老键」（沙盒实体 id 留下的残渣）
    real_ids = {id(e) for e in ([a.state.player] if a.state.player else [])
                + a.state.friends + a.state.employees + a.state.temp_friends
                + a.state.enemies}
    for attr in ("_monster_activated", "_monster_daowen_round_used",
                 "_resonance_rewrites"):
        stale = [k for k in getattr(combat, attr) if k not in real_ids]
        assert stale == [], f"预演在 {attr} 留下沙盒实体老键：{stale}"


def test_preview_does_not_build_transaction_snapshot(engines):
    """性能护栏：预演走 core，不得再建 transaction snapshot（deepcopy 次数）。"""
    a, _ = engines
    calls = {"n": 0}
    real = copy.deepcopy

    def counting(obj, memo=None):
        calls["n"] += 1
        return real(obj) if memo is None else real(obj, memo)

    copy.deepcopy = counting
    try:
        ActionPreview(a).preview("use_daowen", {"daowen_name": "庇护", "x": 1,
                                               "target": "贾凡"})
    finally:
        copy.deepcopy = real
    # sandbox 自身：state + dice + pending_interrupts = 3 份；
    # 若事务层被再次触发会翻倍（≥6），本断言即失败。
    assert calls["n"] <= 4, f"预演仍存在重复快照：deepcopy {calls['n']} 次"
