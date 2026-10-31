#!/usr/bin/env python3
"""死斗逐手实录器（2026-09-10）：只记**真实结算**的每个动作，预演世界一律排除。

技术前提：ActionPreview.preview_sequence 临时把 eng.state 换成 deepcopy 副本执行
（engine/ai_preview.py:141-175），因此用「eng.state is 真实state」身份校验即可
100% 区分真实/预演——旧 duel_diff_trace 的深度计数器只包了 preview 没包
preview_sequence，这正是报告在案「假账」混入实录的根因。

用法：
    python3 sim/duel_chronicle.py SEED [--swap] [--no-bridge] [--no-dodge]
输出：人类可读的逐手实录（stdout）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine                     # noqa: E402
from sim.duel_pvp import run_duel_pvp                 # noqa: E402
from tests.setup_support import finish_initial_daowen  # noqa: E402
from sim.handplay_dungeon_with_winner import load_winner  # noqa: E402
from sim.optional_actions import start_battle         # noqa: E402
from sim.wage_settlement import settle_wages          # noqa: E402

WINNER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "breed_winners")
CANON_BOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "死者之书.md")

_INSTANCES: list = []


class RecordingAI:            # 默认由 main 换成 LegacyAwareAI 子类注入
    pass


def chronicle(challenger_path: str, defender_path: str, seed: int,
              bridge: bool = True, duel_dodge: bool = True,
              ai_cls=None) -> dict:
    from sim.legacy_mentor import LegacyAwareAI

    class _Rec(LegacyAwareAI):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            _INSTANCES.append(self)

    if ai_cls is not None:
        base = ai_cls          # 外部注入（如 WinOnlyAI）：无遗言桥
    elif not bridge:
        from engine.ai_tactics import TacticalAI
        base = TacticalAI
    else:
        base = _Rec
    _INSTANCES.clear()

    os.environ["LJ_AI_DUEL_DODGE"] = "1" if duel_dodge else "0"
    tmp_seal = tempfile.mktemp(suffix=".json")
    shutil.copy(defender_path, tmp_seal)
    e = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=seed,
                   sealed_candidate_path=tmp_seal, death_book_path=CANON_BOOK)
    with open(challenger_path, encoding="utf-8") as f:
        snap = json.load(f)
    p0 = snap["player"]
    e.execute_action("setup_attributes", {"name": p0["name"], "blood_points": 10,
                                          "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    load_winner(e, snap)
    e.state.energy = 0
    e.state.current_battle = 6
    bs, _ = start_battle(e)
    if not bs.get("success"):
        return {"error": f"开始第7战失败: {bs.get('error')}"}
    for m in e.state.enemies:
        m.is_alive = False
    e.state.monster_reinforcements = []  # 波次：仪式战脚手架直接清空增援队列
    be = e.execute_action("battle_end", {})
    guard = 0
    while (be.get("success") and be.get("completed") is False
           and be.get("pending_wage_decisions")):
        settle_wages(e, [])
        be = e.execute_action("battle_end", {})
        guard += 1
        if guard > 5:
            break
    # 2026-10-31 规则变更：第7场战终不再自动开启死斗（改为【最终的冠冕】世界事件）。
    # 本 harness 是"死斗/对局压力测试"脚手架，故显式走旧入口进入死斗。
    if "final_crown" not in (be.get("result") or {}):
        e._trigger_final_crown()
    if (be.get("result") or {}).get("final_crown", {}).get("outcome") != "duel_start" \
            and not e.state.in_final_duel:
        return {"error": "未进入死斗"}

    # ---- 逐手捕获（真实世界判定 = state 身份）----
    real_state = e.state
    real_combat = e.combat
    cap: list = []
    orig = e.execute_action

    def panel() -> str:
        p = e.state.player
        rows = []
        if p is not None:
            rows.append(f"挑战{p.name} 血{p.current_hp}/{p.blood_limit} 盾{p.shield} "
                        f"法{p.current_mana}/{p.mana_limit} 速{p.current_speed} "
                        f"出手{p.actions_used_this_round}/{p.action_count}")
        for x in e.state.enemies:
            if x.entity_type == "轮回者":
                rows.append(f"守擂{x.name} 血{x.current_hp}/{x.blood_limit} 盾{x.shield} "
                            f"法{x.current_mana}/{x.mana_limit} 速{x.current_speed} "
                            f"出手{x.actions_used_this_round}/{x.action_count}")
        return " ｜ ".join(rows)

    def traced(action, params=None):
        is_real = e.state is real_state
        before = panel() if is_real else None
        r = orig(action, params)
        if is_real:
            cap.append({"action": action, "params": params,
                        "before": before, "after": panel(), "result": r})
        return r

    e.execute_action = traced
    # 2026-10-13：实录 harness 不是实战——换席局（4血方）在本机需 ~29-31s，
    # 推演骰子每次从 OS 熵重播种，耗时天然抖动 ±2s，30s 默认兜底会随机误杀导致
    # test_win_only_ai 结构性断言（要的是胜负结论，不是“30s 内打完”）抖动。
    # 只放宽 harness 的墙钟预算，引擎 run_duel_pvp 默认 30.0s 不动。
    verdict = run_duel_pvp(e, None, max_rounds=30, max_steps=400, log=[],
                           use_tactical=True, ai_cls=base, max_wall_seconds=300)
    mentors = [round(ai.mentor.adherence, 2) for ai in _INSTANCES]
    return {"seed": seed, "bridge": bridge, "duel_dodge": duel_dodge,
            "challenger": os.path.basename(challenger_path)[:-5],
            "defender": os.path.basename(defender_path)[:-5],
            "actions": cap, "verdict": verdict, "adherence": mentors}


def render(rec: dict) -> str:
    if rec.get("error"):
        return f"seed={rec['seed']} 错误: {rec['error']}"
    v = rec["verdict"]
    dodge_note = "闪避开" if rec["duel_dodge"] else "闪避关"
    lines = [f"### seed={rec['seed']}｜{rec['challenger']}（挑战席）vs "
             f"{rec['defender']}（守擂席）｜遗言桥{'开' if rec['bridge'] else '关'}｜{dodge_note}"
             f"｜信从度{rec['adherence']}",
             f"- 判定：**{v.get('winner')}**，第{v.get('rounds')}回合 —— {v.get('reason')}"]
    rnd = 0
    started = False
    for a in rec["actions"]:
        act = a["action"]
        if act == "round_start":
            started = True
            rnd += 1
            lines.append(f"- **R{rnd}** 开局面板：{a['after']}")
            continue
        if act in ("battle_start", "battle_end", "resolve_ally_phases") or not started:
            continue   # resolve_ally_phases：每步调用的空动作（实录 125 行全是它）
        if act == "prepare_attack":
            continue                      # 真正文在 resolve_attack
        p = a.get("params") or {}
        res = a.get("result") or {}
        if act == "resolve_attack":
            opts = (p.get("hits") or [])
            n_dodge_decl = sum(1 for h in opts if h.get("dodge"))
            per = ((res.get("result") or {}).get("hits")) or []
            n_dodge_ok = sum(1 for h in per if h.get("dodge_success"))
            dmg = sum(h.get("damage_dealt", 0) for h in per)
            sh = sum(h.get("shield_absorbed", 0) for h in per)
            tgt = (per[0].get("target") if per else "?")
            atk = (per[0].get("attacker") if per else "?")
            sides = "挑战→守擂" if "enemy" in str((opts[0].get("target_ref") if opts else "")) else "守擂→挑战"
            lines.append(f"  - {atk} 普攻{len(opts)}击 → {tgt}（{sides}）："
                         f"声明闪避{n_dodge_decl}击/成功{n_dodge_ok}击，"
                         f"总伤{dmg}" + (f"，被盾吸收{sh}" if sh else "")
                         + ("" if dmg or n_dodge_ok else "，0伤（攻力=当前法力=空手）"))
        elif act in ("use_daowen", "use_resonance"):
            name = p.get("daowen_name") or f"残韵·{p.get('resonance_type')}"
            x = p.get("x", "")
            lines.append(f"  - {name}X={x} → {p.get('target', '')}"
                         + ("" if res.get("success") else f"（失败:{str(res.get('error'))[:24]}）"))
        lines.append(f"    - 面板：{a['after']}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("seed", type=int)
    ap.add_argument("--swap", action="store_true", help="守擂快照坐挑战席（换席）")
    ap.add_argument("--no-bridge", action="store_true", help="双方用基础 TacticalAI")
    ap.add_argument("--no-dodge", action="store_true", help="关闪避（复现站桩口径）")
    args = ap.parse_args()
    ch = os.path.join(WINNER_DIR, "普攻武斗_69.json")
    de = os.path.join(WINNER_DIR, "普攻武斗_429.json")
    c, d = (de, ch) if args.swap else (ch, de)
    rec = chronicle(c, d, args.seed, bridge=not args.no_bridge,
                    duel_dodge=not args.no_dodge)
    print(render(rec))


if __name__ == "__main__":
    main()
