"""性能基准：GameEngine 初始化 / ActionPreview / TacticalAI 单次决策。

本脚本只为「优化前后对比」存在，不参与生产流程，也不做任何规则判定。
三组测量都在**固定种子 + 固定初始状态**下重复，结果只反映引擎实现开销：

    .venv/bin/python sim/reports/perf_bench.py
    .venv/bin/python sim/reports/perf_bench.py --json /tmp/bench.json      # 存档供对比
    .venv/bin/python sim/reports/perf_bench.py --count-deepcopy            # 附带 deepcopy 计数

测量口径：
  init      ：新开一局（GameEngine 构造，含规则解析）
  preview   ：ActionPreview.preview 一次道纹动作（引擎副本内真实结算）
  ai        ：TacticalAI 从同一状态出发的一次决策（含其全部候选预演）
               每次决策前用引擎既有事务快照把状态还原，还原耗时不计入。
"""
from __future__ import annotations

# --- 2026-11-04：脚本放进子目录后，不再靠"往上数几层"猜仓库根 ---
# 向上找到同时含 engine/ 与 sim/ 的目录就是仓库根；再附带 sim/ 让老式裸模块名
# import（如 `import balance_sim`）继续可用。搬动本文件不需要再改这几行。
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.abspath(__file__))
while _ROOT != _os.path.dirname(_ROOT) and not _os.path.isdir(_os.path.join(_ROOT, "engine")):
    _ROOT = _os.path.dirname(_ROOT)
for _p in (_ROOT, _os.path.join(_ROOT, "sim"), _os.path.join(_ROOT, "tests")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)


import argparse
import copy
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = _ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------- 场景构造

def build_engine(tmp: str, seed: int = 4):
    """开一局并推进到「战斗回合内、轮回者待出手」，与 tests/test_ai_tactics.py 同构。"""
    from engine.api import GameEngine
    from tests.setup_support import finish_initial_daowen

    engine = GameEngine(db_path=os.path.join(tmp, "bench.db"),
                        save_dir=os.path.join(tmp, "saves"),
                        death_book_path=os.path.join(tmp, "死者之书.md"),
                        rng_seed=seed)
    engine.execute_action("setup_attributes",
                          {"name": "贾凡", "blood_points": 11,
                           "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = engine.execute_action("setup_choose_region", {"region": "龙心谷"})
    engine.execute_action("choose_discovered_relic",
                          {"relic_name": setup["result"]["relic_choices"][0]})
    for dw in ("庇护", "再生", "冲击"):
        engine.execute_action("pre_battle_action",
                              {"sub_action": "学习", "sub": "daowen", "name": dw})
    engine.state.energy = 0
    choices = {}
    engine.execute_action("battle_start", {"relic_choices": choices})
    engine.execute_action("round_start", {})
    return engine


class _Rewind:
    """把引擎还原到基准状态（复用引擎既有事务快照，实体 id 保持稳定）。"""

    def __init__(self, engine):
        self.engine = engine
        self._state = copy.deepcopy(engine.state)
        self._runtime = engine._snapshot_combat_runtime()
        self._dice = copy.deepcopy(engine.dice)
        self._history = len(engine._action_history)
        self._events = (set(engine.event_pool.triggered), engine.event_pool.current)

    def __call__(self):
        engine = self.engine
        engine._restore_state_in_place(self._state)
        engine.combat.state = engine.state
        engine._restore_combat_runtime(self._runtime)
        engine.dice = copy.deepcopy(self._dice)
        engine.combat.dice = engine.dice
        del engine._action_history[self._history:]
        engine._last_result = None
        engine.event_pool.triggered, engine.event_pool.current = (
            set(self._events[0]), self._events[1])


# ---------------------------------------------------------------- 计时工具

def _time_calls(fn, rounds: int, prepare=None) -> dict:
    samples = []
    for _ in range(rounds):
        if prepare is not None:
            prepare()          # 复原基准状态，不计入测量
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return {"total_ms": round(sum(samples), 3),
            "mean_ms": round(statistics.fmean(samples), 4),
            "median_ms": round(statistics.median(samples), 4),
            "p95_ms": round(sorted(samples)[max(0, int(len(samples) * 0.95) - 1)], 4),
            "rounds": rounds}


def _deepcopy_counter():
    """临时统计 copy.deepcopy 调用次数与耗时占比（诊断用）。"""
    real = copy.deepcopy
    stats = {"calls": 0}

    def counting(obj, memo=None, _nil=[]):
        stats["calls"] += 1
        return real(obj, memo) if memo is not None else real(obj)

    return real, counting, stats


# ---------------------------------------------------------------- 三组测量

def bench_init(rounds: int) -> dict:
    from engine.api import GameEngine
    tmp = tempfile.mkdtemp(prefix="lj_bench_init_")

    def one(i=iter(range(rounds))):
        n = next(i)
        GameEngine(db_path=os.path.join(tmp, f"{n}.db"),
                   save_dir=os.path.join(tmp, "saves"),
                   death_book_path=os.path.join(tmp, "死者之书.md"),
                   rng_seed=4)

    return _time_calls(one, rounds)


def inflate_events(engine, count: int) -> int:
    """往真实状态里堆 count 条战斗事件（模拟长局事件流），返回当前条数。"""
    from engine.combat_events import CombatEvent, CombatEventType
    events = engine.state.combat_events
    for i in range(count):
        events.append(CombatEvent(event_type=CombatEventType.DAMAGE_APPLIED,
                                  actor_name="甲", target_name="乙",
                                  data={"actual_damage": i, "note": "长局事件流"}))
    return len(events)


def bench_preview(rounds: int, engine=None, tmp: str = "") -> dict:
    from engine.ai_preview import ActionPreview
    own = engine is None
    if own:
        tmp = tmp or tempfile.mkdtemp(prefix="lj_bench_pv_")
        engine = build_engine(tmp)
    previewer = ActionPreview(engine)
    player = engine.state.player
    target = next((m.name for m in engine.state.enemies if m.is_alive), None)
    action, params = _probe_action(engine)

    def one():
        out = previewer.preview(action, dict(params))
        assert out.get("result") is not None, "preview 未返回结果"

    result = _time_calls(one, rounds)
    result["action"] = action
    result["params"] = {k: v for k, v in params.items()}
    result["enemies"] = len([m for m in engine.state.enemies if m.is_alive])
    result["player_daowen"] = len(player.dao_wen)
    result["target"] = target
    return result


def _probe_action(engine) -> tuple[str, dict]:
    """挑一个能真实结算的候选动作：优先对敌使用「冲击」类直伤道纹。"""
    player = engine.state.player
    alive = [m for m in engine.state.enemies if m.is_alive]
    if "冲击" in player.dao_wen and alive:
        return "use_daowen", {"daowen_name": "冲击", "x": 1, "target": alive[0].name}
    if alive:
        return "prepare_attack", {"target": alive[0].name}
    return "round_end", {}


def bench_ai(rounds: int, engine=None, tmp: str = "") -> dict:
    from engine.ai_tactics import TacticalAI
    if engine is None:
        tmp = tmp or tempfile.mkdtemp(prefix="lj_bench_ai_")
        engine = build_engine(tmp)
    rewind = _Rewind(engine)
    counts = {"actions": 0}

    def one():
        ai = TacticalAI(engine)
        ai._refresh_personality()
        out = ai.take_action()
        counts["actions"] += 1 if out else 0

    result = _time_calls(one, rounds, prepare=rewind)
    result["actions_returned"] = counts["actions"]
    result["enemies"] = len([m for m in engine.state.enemies if m.is_alive])
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds-init", type=int, default=30)
    parser.add_argument("--rounds-preview", type=int, default=100)
    parser.add_argument("--rounds-ai", type=int, default=100)
    parser.add_argument("--events", type=int, default=0,
                        help="预演前先堆多少条战斗事件（模拟长局）")
    parser.add_argument("--json", default="")
    parser.add_argument("--label", default="")
    parser.add_argument("--count-deepcopy", action="store_true")
    args = parser.parse_args()

    tmp = tempfile.mkdtemp(prefix="lj_bench_")
    shared = build_engine(tmp)
    report = {"label": args.label, "python": sys.version.split()[0],
              "events": args.events}
    if args.events:
        report["events"] = inflate_events(shared, args.events)

    print(f"[bench] init x{args.rounds_init} ...")
    report["init"] = bench_init(args.rounds_init)
    print(f"[bench] preview x{args.rounds_preview} ...")
    report["preview"] = bench_preview(args.rounds_preview, engine=shared)
    print(f"[bench] ai x{args.rounds_ai} ...")
    report["ai"] = bench_ai(args.rounds_ai, engine=shared)

    if args.count_deepcopy:
        real, counting, stats = _deepcopy_counter()
        copy.deepcopy = counting
        try:
            bench_preview(10, engine=shared)
            report["preview_deepcopy_per_call"] = round(stats["calls"] / 10, 2)
            stats["calls"] = 0
            bench_ai(10, engine=shared)
            report["ai_deepcopy_per_decision"] = round(stats["calls"] / 10, 2)
        finally:
            copy.deepcopy = real

    for key in ("init", "preview", "ai"):
        row = report[key]
        print(f"[bench] {key:8s} total={row['total_ms']:9.1f}ms  mean={row['mean_ms']:8.3f}ms  "
              f"median={row['median_ms']:8.3f}ms  p95={row['p95_ms']:8.3f}ms  n={row['rounds']}")
    if "preview_deepcopy_per_call" in report:
        print(f"[bench] deepcopy/次: preview={report['preview_deepcopy_per_call']} "
              f"ai_decision={report['ai_deepcopy_per_decision']}")

    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"[bench] 写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())