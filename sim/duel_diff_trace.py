#!/usr/bin/env python3
"""死斗逐次 diff 追踪器（诊断工具）。

报告.md 硬伤1 的核验方式就是「逐次 diff」：每执行一个引擎动作就打印双方
（血/盾/法/速）的前后快照，凡是「没有真实掉血/回血却变了」的项都是假账。

用法：
    python3 sim/duel_diff_trace.py                       # 默认封印控制_21 vs 杀伐法攻_45, seed=1
    python3 sim/duel_diff_trace.py 杀伐法攻_45.json 封印控制_21.json 7

输出：
    [DIFF] <action> <params>   —— 只有面板发生变化的动作才打印
       - 执行前面板
       + 执行后面板
    末尾打印动作日志与最终判定。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine  # noqa: E402
from sim.duel_pvp import run_duel_pvp  # noqa: E402

WINNER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "breed_winners")


def snap(e) -> str:
    """双方轮回者的面板快照字符串。"""
    rows = []
    p = e.state.player
    if p is not None:
        rows.append(f"挑战{p.name} hp={p.current_hp}/{p.blood_limit} 盾={p.shield} "
                    f"法={p.current_mana}/{p.mana_limit} 速={p.current_speed}/{p.speed_limit}"
                    f" 出手={p.actions_used_this_round}/{p.action_count}")
    for x in e.state.enemies:
        if x.entity_type == "轮回者":
            rows.append(f"守擂{x.name} hp={x.current_hp}/{x.blood_limit} 盾={x.shield} "
                        f"法={x.current_mana}/{x.mana_limit} 速={x.current_speed}/{x.speed_limit}"
                        f" 出手={x.actions_used_this_round}/{x.action_count}")
    return " | ".join(rows)


def _check_invariants(e, action: str) -> list[str]:
    """每步真实结算后的面板不变量（报告.md 硬伤1 的验收口径）。

    只查"物理上不可能"的项。以下两条**刻意不查**（DM 裁定 2026-08-30，
    两条都是合法面板，曾误判为假账）：
      - 格挡 > 血限：格挡无上限，【庇护】叠到 68/36 合法。
      - 法力 > 法限：回始是加法，守夜灯等额外获得可叠加；只有【不朽之躯】才 clamp。
    """
    bad = []
    for label, ent in _duelists(e):
        if ent.shield < 0:
            bad.append(f"{action}: {label}{ent.name} 格挡 {ent.shield} 为负")
        if ent.current_hp < 0 or ent.current_hp > ent.blood_limit:
            bad.append(f"{action}: {label}{ent.name} 生命 {ent.current_hp} 超出 0~血限{ent.blood_limit}")
    return bad


def _duelists(e):
    out = []
    if e.state.player is not None:
        out.append(("挑战", e.state.player))
    for x in e.state.enemies:
        if x.entity_type == "轮回者":
            out.append(("守擂", x))
    return out


def traced_duel(challenger_path: str, defender_path: str, seed: int,
                max_rounds: int = 30, verbose_all: bool = False,
                ai_cls=None) -> dict:
    from tests.setup_support import finish_initial_daowen
    from sim.handplay_dungeon_with_winner import load_winner
    from sim.optional_actions import start_battle
    from sim.wage_settlement import settle_wages

    tmp = tempfile.mktemp(suffix=".json")
    shutil.copy(defender_path, tmp)
    db = tempfile.mktemp(suffix=".db")
    e = GameEngine(db_path=db, rng_seed=seed, sealed_candidate_path=tmp)
    with open(challenger_path, encoding="utf-8") as f:
        snap_json = json.load(f)
    p0 = snap_json["player"]
    e.execute_action("setup_attributes", {"name": p0["name"], "blood_points": 10,
                                          "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    load_winner(e, snap_json)
    e.state.energy = 0
    e.state.current_battle = 6
    bs, _ = start_battle(e)
    if not bs.get("success"):
        print("start_battle 失败:", str(bs.get("error"))[:200])
        return {}
    for m in e.state.enemies:
        m.is_alive = False
    e.state.monster_reinforcements = []  # 波次：仪式战脚手架直接清空增援队列
    be = e.execute_action("battle_end", {})
    guard = 0
    while be.get("success") and be.get("completed") is False and be.get("pending_wage_decisions"):
        settle_wages(e, [])
        be = e.execute_action("battle_end", {})
        guard += 1
        if guard > 5:
            break
    # 2026-10-31：第7场战终不再自动开启死斗（改由【最终的冠冕】世界事件承载），
    # 本追溯脚手架显式走旧入口进入死斗。
    if "final_crown" not in (be.get("result") or {}):
        e._trigger_final_crown()
    crown = (be.get("result") or {}).get("final_crown", {})
    if crown.get("outcome") != "duel_start" and not e.state.in_final_duel:
        print("未进入死斗:", crown.get("outcome"), str(be.get("error"))[:200])
        return {}

    orig = e.execute_action
    violations: list[str] = []
    print(f"⚔ 死斗 {os.path.basename(challenger_path)}(挑战者) vs "
          f"{os.path.basename(defender_path)}(守擂) seed={seed}")
    print("  开局:", snap(e))

    # 预演（ActionPreview）会临时把 eng.state 换成 deepcopy 副本再调 execute_action。
    # 若不区分，追踪器会把「预演世界的数值」误当成真实结算——这正是 报告.md
    # 里「68 盾/法力 51」等假账证据的来源。这里用一个深度计数器把预演排除。
    from engine import ai_preview
    depth = {"n": 0}
    # ②修复（2026-09-10 用户裁定）：旧计数器只补 `preview`，而预演主入口与
    # WinOnlyAI 全走 `preview_sequence`（preview 只是它的委托壳）——推演世界的
    # 出手全部漏记成 [真实]，正是「68 盾/法力 51」假账证据的来源。改补
    # preview_sequence（两个口径都经它），并叠加 WinOnlyAI._swap_world 的
    # 整场推演深度（那条路径不经 ActionPreview，直接 execute_action）。
    _orig_preview_seq = ai_preview.ActionPreview.preview_sequence

    def _tracked_preview(self, steps):
        depth["n"] += 1
        try:
            return _orig_preview_seq(self, steps)
        finally:
            depth["n"] -= 1

    ai_preview.ActionPreview.preview_sequence = _tracked_preview

    try:
        from sim.win_only_ai import playout_depth as _playout_depth
    except Exception:
        _playout_depth = None

    def _in_preview_world():
        return depth["n"] or (_playout_depth() if _playout_depth else 0)

    def traced(action, params=None):
        if _in_preview_world():   # 预演/推演世界：不打印、不算真实结算
            return orig(action, params)
        before = snap(e)
        r = orig(action, params)
        after = snap(e)
        violations.extend(_check_invariants(e, action))
        if before != after or verbose_all:
            print(f"[真实] {action} {str(params)[:160]}")
            print(f"   - {before}")
            print(f"   + {after}")
            if isinstance(r, dict) and (verbose_all or action in ("round_start", "round_end")):
                res = r.get("result") or {}
                for ef in (res.get("effects") or [])[:60]:
                    print("     效果:", ef)
        return r

    e.execute_action = traced
    log_buf: list[str] = []
    对话 = types.SimpleNamespace(buf=[], events=0, next_line_round=1)
    res = run_duel_pvp(e, None, max_rounds=max_rounds, max_steps=400, log=log_buf,
                       use_tactical=True, 对话=对话, ai_cls=ai_cls)
    print("\n=== 面板不变量 ===")
    if violations:
        for v in violations[:40]:
            print("  ✗", v)
        print(f"  共 {len(violations)} 处违反")
    else:
        print("  ✓ 全程 格挡 ≥ 0、0 ≤ 生命 ≤ 血限（格挡/法力超上限为合法面板，不查）")
    print("\n=== 动作日志 ===")
    for line in log_buf[:80]:
        print(" ", line)
    if 对话.buf:
        print("=== 对白 ===")
        for line in 对话.buf:
            print("  💬", line)
    # 战场公开频道（硬伤3）：双方共享、不带真伪标记的台词实录
    try:
        from engine.dialogue import format_channel
        ch = format_channel(e.state)
    except Exception:
        ch = []
    if ch:
        print("\n=== 战场公开频道（双方可见·真伪不标）===")
        for line in ch:
            print("  🗣", line)
    print("\n=== 判定 ===", res)
    return res


def main() -> None:
    a = sys.argv[1] if len(sys.argv) > 1 else "封印控制_21.json"
    b = sys.argv[2] if len(sys.argv) > 2 else "杀伐法攻_45.json"
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    traced_duel(os.path.join(WINNER_DIR, a), os.path.join(WINNER_DIR, b), seed)


if __name__ == "__main__":
    main()
