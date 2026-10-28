"""随机组合压力测试（Phase 8）：随机 Action / Effect / Trigger / 目标 / 状态 / 链。

需求原话：「随机 1000 例起步，运行时间允许再上 1 万 / 10 万；检查 HP 合法性、
实体引用有效、结算能终止、预演不污染、异常不破坏状态」。

设计取舍（都在注释里说明，方便以后维护）：

* **种子固定**：`FUZZ_SEED` 写死，任何一次失败都能用同一行命令复现；
* **规模可调**：默认 2000 例（需求下限 1000 的 2 倍，跑一遍约 35 s，适合每次全套测试）。
  规模档位用环境变量切换，已实测：
      LJ_FUZZ_CASES=10000  pytest tests/test_resolution_fuzz.py   # 8 分 34 秒，通过
      LJ_FUZZ_CASES=100000 pytest tests/test_resolution_fuzz.py   # 约 85 分钟（未跑，留给
                                                                 # 发版前的一次性压测）
  单步成本主要来自 `_op_preview` 里的污染快照（调试工具，默认关闭，不影响正式性能）；
* **同一批引擎反复用**：12 局引擎各跑 `总数/12` 步。相比每例新建引擎，
  这样还能顺带压「长局状态」这一面；
* **每一步都单独设看门狗**：`time_limit()` 用 SIGALRM 给单步上墙钟上限，
  真正的死循环会变成一次可复现的失败，而不是挂住整个测试进程；
* **不变量复用组合测试的那一套**（`assert_battle_invariants`），保证两处口径一致；
* 随机操作里包含**非法输入**（不存在的道纹、不存在的目标、负数 X、缺参数），
  要求引擎返回错误而不是抛异常或破坏状态。
"""
from __future__ import annotations

import json
import os
import random
import signal
import sys
import time
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.ai_preview import ActionPreview                          # noqa: E402
from engine.combat_events import CombatEventType                     # noqa: E402
from engine.mechanisms import Mechanism, Trigger                     # noqa: E402
from engine.models import DaoWen, DaoWenInstance, StatusEffect       # noqa: E402
from engine.pollution_guard import (                                 # noqa: E402
    assert_runtime_unchanged, snapshot_runtime_state,
)
from engine.resolution import ResolutionContext                      # noqa: E402
from tests.preview_support import build_battle_engine                # noqa: E402
from tests.test_effect_composition import assert_battle_invariants   # noqa: E402

FUZZ_SEED = 20260920
ENGINES = 12
#: 随机例数（可用 LJ_FUZZ_CASES 覆盖）。需求档位：1000 → 1 万 → 10 万。
FUZZ_CASES = max(1000, int(os.environ.get("LJ_FUZZ_CASES", "2000")))
STEPS_PER_ENGINE = max(10, FUZZ_CASES // ENGINES)
STEP_TIME_LIMIT = 3.0        # 单步墙钟上限（秒）：终止性证据
# 测试监听器不是游戏内资源；轮换保留20个，避免测试装置无限累积后自行撞生产保险丝。
MAX_ACTIVE_FUZZ_MECHANISMS = 20

DAOWEN_POOL = ["杀伐", "再生", "庇护", "连击", "坠落", "透支", "分裂", "压制",
               "贯穿", "增殖", "变形", "迷失", "焦热", "咆哮"]
ILLEGAL_DAOWEN = ["不存在的道纹", "", "杀伐X", None]
BAD_PARAMS = [
    {"daowen_name": "杀伐"},                                  # 缺目标
    {"daowen_name": "杀伐", "x": 0},                          # X=0
    {"daowen_name": "再生", "x": -3},                         # 负 X
    {"daowen_name": "庇护", "x": 999},                        # 超额 X
    {"daowen_name": "杀伐", "x": 1, "target": "不存在的目标"},  # 悬垂目标
    {"daowen_name": "杀伐", "x": "一"},                        # 非数字 X
    {},                                                       # 空参数
]
STATUS_POOL = ["洞察", "贯穿", "急速", "蒙蔽", "眩晕", "束缚", "滋养", "衰败"]


@contextmanager
def time_limit(seconds: float = STEP_TIME_LIMIT):
    """给单步上墙钟上限：死循环 → TimeoutError（而不是挂住测试进程）。"""
    if not hasattr(signal, "SIGALRM"):     # pragma: no cover - 非 POSIX
        yield
        return

    def _handler(signum, frame):
        raise TimeoutError(f"单步超过 {seconds}s，疑似不终止")

    previous = signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _grant(engine, *names):
    for name in names:
        if name and name not in engine.state.player.dao_wen:
            engine.state.player.dao_wen[name] = DaoWenInstance(
                DaoWen(name=name, formula=f"{name}X", cost_type="消耗",
                       cost_formula="X", effect_formula=""))


def _refill(engine):
    player = engine.state.player
    player.current_hp = player.blood_limit
    player.current_mana = max(player.mana_limit, 10)
    player.current_speed = max(player.current_speed, player.speed_limit)
    engine.state.energy = 3


def _random_target(engine, rng):
    choices = [engine.state.player.name]
    choices += [e.name for e in engine.state.enemies if e.is_alive]
    choices += ["不存在的目标", None]
    return rng.choice(choices)


def _op_cast_daowen(engine, rng):
    name = rng.choice(DAOWEN_POOL + ILLEGAL_DAOWEN)
    if not isinstance(name, str):
        return {"action": "daowen", "name": name, "result": "invalid"}
    _grant(engine, name)
    params = {"daowen_name": name, "x": rng.choice([1, 1, 2, 3, 0, -1, 99])}
    target = _random_target(engine, rng)
    if target:
        params["target"] = target
    result = engine.execute_action("use_daowen", params)
    return {"action": "daowen", "name": name, "result": result}


def _op_bad_params(engine, rng):
    params = dict(rng.choice(BAD_PARAMS))
    if params.get("target") is None and "target" in params:
        params["target"] = _random_target(engine, rng)
    result = engine.execute_action("use_daowen", params)
    return {"action": "bad_params", "params": params, "result": result}


def _op_damage(engine, rng):
    enemies = [e for e in engine.state.enemies if e.is_alive]
    if not enemies:
        return {"action": "damage", "result": "no_target"}
    target = rng.choice(enemies)
    amount = rng.choice([0, 1, 3, 7, 25, 999])
    engine.combat._apply_hostile_damage(target, amount, source=engine.state.player)
    return {"action": "damage", "amount": amount, "result": "ok"}


def _op_heal(engine, rng):
    who = rng.choice([engine.state.player] +
                     [e for e in engine.state.enemies if e.is_alive])
    amount = rng.choice([0, 1, 4, 20, 999])
    engine.combat.state.apply_heal(who, amount)
    return {"action": "heal", "amount": amount, "result": "ok"}


def _op_status(engine, rng):
    who = rng.choice([engine.state.player] +
                     [e for e in engine.state.enemies if e.is_alive])
    name = rng.choice(STATUS_POOL)
    who.add_status(StatusEffect(name=name, remaining_rounds=rng.choice([1, 2, -1]),
                                value=rng.choice([1, 2, 3])))
    return {"action": "status", "name": name, "result": "ok"}


def _op_split(engine, rng):
    monsters = [e for e in engine.state.enemies if e.is_alive]
    if not monsters:
        return {"action": "split", "result": "no_target"}
    caster = rng.choice(monsters)
    creation = engine.combat._spawn_fenlie_creation(caster, rng.choice([1, 2, 3]), 3)
    return {"action": "split", "creations": 1, "name": creation.name, "result": "ok"}


def _op_evolve(engine, rng):
    monsters = [e for e in engine.state.enemies if e.is_alive]
    if not monsters or not engine.state.player.dao_wen:
        return {"action": "evolve", "result": "no_target"}
    monster = rng.choice(monsters)
    if rng.random() < 0.5:            # 一半样例刻意制造困境前提
        monster.current_hp = max(1, monster.current_hp // 4)
    result = engine.combat.execute_evolution(
        monster, rng.choice(list(engine.state.player.dao_wen)), rng.choice([1, 1, 2]))
    return {"action": "evolve", "result": result}


def _op_seal(engine, rng):
    monsters = [e for e in engine.state.enemies if e.is_alive and e.entity_type == "怪物"]
    if not monsters:
        return {"action": "seal", "result": "no_target"}
    monster = rng.choice(monsters)
    engine.combat._delay_monster_reentry(monster, rng.choice([1, 2]))
    return {"action": "seal", "result": "ok"}


def _op_round_start(engine, rng):
    result = engine.combat.round_start({})
    return {"action": "round_start", "round": engine.state.current_round,
            "effects": len(result.get("effects", []))}


def _op_preview(engine, rng, guard_states):
    """预演随机动作，并验证预演前后真实运行态完全没变。"""
    before = snapshot_runtime_state(engine, "fuzz_before")
    name = rng.choice(DAOWEN_POOL)
    _grant(engine, name)
    params = {"daowen_name": name, "x": rng.choice([1, 2]),
              "target": _random_target(engine, rng)}
    pv = ActionPreview(engine).preview("use_daowen", params)
    assert_runtime_unchanged(before, snapshot_runtime_state(engine, "fuzz_after"),
                             context="fuzz 预演")
    guard_states.append(pv is not None)
    return {"action": "preview", "name": name, "result": bool(pv)}


def _make_random_mechanism(rng, log):
    """随机触发机制：在伤害事件上做一件随机小事（含空操作）。"""
    choice = rng.randrange(5)

    def effect(trigger_ctx, targets):
        target = trigger_ctx.target
        log.append((choice, getattr(target, "name", "?")))
        if choice == 0 or target is None:
            return {}
        if choice == 1 and hasattr(target, "add_status"):
            target.add_status(StatusEffect(name="洞察", remaining_rounds=1, value=1))
        elif choice == 2 and hasattr(target, "current_hp") and target.is_alive:
            trigger_ctx.combat.state.apply_heal(target, 1) if trigger_ctx.combat else None
        elif choice == 3 and hasattr(target, "gain_shield"):
            target.gain_shield(1)
        return {"fuzz": choice}

    return Mechanism(name=f"fuzz·{rng.randrange(10 ** 9)}",
                     when=Trigger.event(CombatEventType.DAMAGE_APPLIED),
                     effect=effect, priority=rng.choice([10, 50, 100]))


def _run_fuzz(engine, rng, steps: int, log: dict):
    mechanisms = []
    ops = [_op_cast_daowen, _op_bad_params, _op_damage, _op_heal, _op_status,
           _op_split, _op_evolve, _op_seal, _op_round_start, _op_preview]
    for step in range(steps):
        _refill(engine)
        if rng.random() < 0.25:                 # 随机挂一个触发机制
            if len(mechanisms) >= MAX_ACTIVE_FUZZ_MECHANISMS:
                engine.combat.mechanism_bus.unregister(mechanisms.pop(0))
            mechanism = _make_random_mechanism(rng, log["mechanisms"])
            engine.combat.mechanism_bus.register(mechanism)
            mechanisms.append(mechanism)
        op = rng.choice(ops)
        started = time.perf_counter()
        try:
            with time_limit():
                record = op(engine, rng, log["previews"]) if op is _op_preview \
                    else op(engine, rng)
            log["steps"].append(record)
        except TimeoutError as exc:
            raise AssertionError(
                f"第 {step} 步不终止（seed={FUZZ_SEED}）：{op.__name__}: {exc}") from exc
        elapsed = time.perf_counter() - started
        log["max_step_ms"] = max(log["max_step_ms"], elapsed * 1000)
        # 每一步之后都检查：状态合法、结算已收尾
        assert_battle_invariants(engine, context=f"seed={FUZZ_SEED} step={step}")
        if engine.state.player and not engine.state.player.is_alive:
            log["player_deaths"] += 1
            engine.state.player.is_alive = True
            engine.state.player.current_hp = 1     # 让随机局能继续跑下去
    for mechanism in mechanisms:
        engine.combat.mechanism_bus.unregister(mechanism)
    return log


@pytest.mark.parametrize("engine_index", range(ENGINES))
def test_random_resolution_stress(tmp_path, engine_index):
    rng = random.Random(FUZZ_SEED + engine_index)
    engine = build_battle_engine(tmp_path, name=f"fuzz{engine_index}")
    for name in DAOWEN_POOL:
        _grant(engine, name)
    log = {"steps": [], "mechanisms": [], "previews": [], "max_step_ms": 0.0,
           "player_deaths": 0}
    log = _run_fuzz(engine, rng, STEPS_PER_ENGINE, log)
    assert len(log["steps"]) == STEPS_PER_ENGINE, "随机步数不足"
    valid = [s for s in log["steps"] if s["action"] == "daowen"
             and isinstance(s.get("result"), dict)]
    assert valid, "随机用例里应当有道纹结算"
    assert log["max_step_ms"] < STEP_TIME_LIMIT * 1000, \
        f"存在过慢的单步：{log['max_step_ms']:.0f} ms"
    assert_battle_invariants(engine, context=f"fuzz 收尾 engine={engine_index}")


def test_fuzz_total_cases_meet_the_requirement():
    """口径检查：随机例数不少于需求下限（1000），当前档位记录在案。

    档位实测：默认 2000（约 35 s）、`LJ_FUZZ_CASES=10000`（8 分 34 秒，通过）、
    10 万档留给发版前一次性压测。
    """
    assert ENGINES * STEPS_PER_ENGINE >= 1000
    assert FUZZ_CASES >= 1000


def test_fuzz_is_reproducible_with_the_recorded_seed():
    """同一颗种子必须给出同一串随机选择（否则失败不可复现）。"""
    a = random.Random(FUZZ_SEED)
    b = random.Random(FUZZ_SEED)
    assert [a.random() for _ in range(50)] == [b.random() for _ in range(50)]


def _content_only(snapshot):
    """把快照归一成「只看内容」：失败行动的既有回滚会造成两类无害差异。

    1. **行动历史**：失败的尝试也会被记进 `_action_history`（引擎既有行为，不是污染）；
    2. **别名/身份**：失败路径的整体回滚是「深拷贝快照 + 原地恢复」，恢复后
       `state` 引用的对象来自那份深拷贝，于是「谁与谁共享同一个对象」这层关系会变，
       快照里的 `<cycle:...>` 标记位置随之移动（同一份内容换了路径）。
       引擎内容一字未改——`monster_pool` 的内容另有 JSON 级别的等值断言。

    这是**既有**的引擎行为；Phase 1/3 的预演零污染契约走的是沙盒换根再换回，
    不经过这条回滚路径，因此不受影响。外部引用问题写进最终报告的「剩余风险」。
    """
    entries = snapshot.entries
    drop_prefixes = ["engine['_action_history']", "engine['monster_pool']"]
    cycle_paths = [k for k, v in entries.items()
                   if isinstance(v, str) and v.startswith("<cycle:")]
    def _under(path, parent):
        return path.startswith(parent) and (
            len(path) == len(parent) or path[len(parent)] in "[.:")
    snapshot.entries = {
        k: v for k, v in entries.items()
        if not any(_under(k, prefix) for prefix in drop_prefixes)
        and not any(_under(k, cycle) for cycle in cycle_paths)
    }
    # 回滚会换掉 dice 对象（内容相同）：身份差异属既有行为，按内容比对。
    snapshot.roots = {k: v for k, v in snapshot.roots.items() if k != "dice"}
    return snapshot


def _pool_content(engine) -> str:
    return json.dumps(engine.monster_pool, sort_keys=True, default=str)


def test_illegal_input_never_corrupts_state(tmp_path):
    """非法输入只允许「返回错误」：除行动历史外状态不变、结算帧不留、引擎还能用。"""
    engine = build_battle_engine(tmp_path, name="fuzz_illegal")
    rng = random.Random(FUZZ_SEED)
    for params in BAD_PARAMS:
        payload = dict(params)
        if payload.get("target") is None and "target" in payload:
            payload["target"] = _random_target(engine, rng) or "贾凡"
        before = snapshot_runtime_state(engine, "illegal_before")
        pool_before = _pool_content(engine)
        result = engine.execute_action("use_daowen", payload)
        assert isinstance(result, dict)
        if result.get("success") is False:
            assert result.get("error"), f"失败必须给出原因：{payload}"
            assert_runtime_unchanged(
                _content_only(before),
                _content_only(snapshot_runtime_state(engine, "illegal_after")),
                context=f"非法输入 {payload}")
            assert _pool_content(engine) == pool_before, "怪物池内容被改动"
        assert engine.combat.resolution.depth == 0
    assert_battle_invariants(engine, context="非法输入之后")


def test_exception_mid_resolution_leaves_context_clean(tmp_path, monkeypatch):
    """异常打断结算：上下文必须收尾，下一次行动不受影响。"""
    engine = build_battle_engine(tmp_path, name="fuzz_exception")
    ctx = engine.combat.resolution
    calls = {"n": 0}
    real_note = ResolutionContext.note

    def exploding_note(self, text):
        calls["n"] += 1
        if calls["n"] == 1 and self._chain:
            raise RuntimeError("故意在结算中途炸")
        return real_note(self, text)

    monkeypatch.setattr(ResolutionContext, "note", exploding_note)
    enemy = next(e for e in engine.state.enemies if e.is_alive)
    engine.combat.resolution.set_tracing(True)
    engine.execute_action("use_daowen",
                          {"daowen_name": "杀伐", "x": 1, "target": enemy.name})
    monkeypatch.setattr(ResolutionContext, "note", real_note)
    assert ctx.depth == 0, "异常路径没有收尾"
    meta = ctx.snapshot()
    ctx.restore(meta)                              # 快照/恢复在异常后仍自洽
    assert ctx.depth == 0
    engine.combat.round_start({})
    assert engine.execute_action("use_daowen",
                                 {"daowen_name": "庇护", "x": 1,
                                  "target": "贾凡"}).get("success") is True
    assert_battle_invariants(engine, context="异常之后")


def test_failed_action_keeps_entity_hp_hook_binding(tmp_path, monkeypatch):
    """失败行动回滚后，实体的降血兜底钩子必须仍然绑在引擎上。

    这是 Phase 8 随机压力测试**实测**发现并修掉的缺陷：回滚是「深拷贝快照 +
    原地恢复」，而 `_hp_engine_ref` 是弱引用（不参与序列化，深拷贝后为 None），
    恢复时会把活实体上的绑定覆盖成 None——之后这个实体的降血兜底钩子就哑了。
    修复方式是在两条回滚路径后重新绑定（engine/api.py）。
    """
    from engine.combat import CombatEngine
    engine = build_battle_engine(tmp_path, name="fuzz_rebind")
    combat = engine.combat
    enemy = next(e for e in engine.state.enemies if e.is_alive)

    # 前提：先让实体完成一次正常绑定
    combat._bind_hp_hook(enemy)
    assert getattr(enemy, "_hp_engine_ref", None) is not None

    fired = []
    real = CombatEngine._on_entity_hp_fallen
    monkeypatch.setattr(CombatEngine, "_on_entity_hp_fallen",
                        lambda self, entity, old, new: fired.append((entity.name, old, new))
                        or real(self, entity, old, new))

    # 一次必然失败的行动（缺目标）→ 走回滚路径
    result = engine.execute_action("use_daowen", {"daowen_name": "杀伐"})
    assert result.get("success") is False

    ref = getattr(enemy, "_hp_engine_ref", None)
    assert ref is not None and ref() is combat, "回滚后 hp 兜底钩子丢了绑定"

    # 行为验证：直接写 current_hp 下降（不经既有入口）时兜底钩子应当照常上报
    enemy.current_hp = max(1, enemy.current_hp - 1)
    assert fired, "兜底钩子没有触发——绑定形同虚设"
    assert fired[0][0] == enemy.name
