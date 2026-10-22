#!/usr/bin/env python3
"""
流派胜率对比：测试不同道纹组合（杀伐系 / 波及系 / 各副本专属）的实战表现。

用法：
    python3 sim/build_winrate.py                    # 全部流派 × 三副本，各50局
    python3 sim/build_winrate.py --runs 200         # 每组200局
    python3 sim/build_winrate.py --region 龙心谷     # 只测一个副本
    python3 sim/build_winrate.py --build 波及系      # 只测一个流派
    python3 sim/build_winrate.py --list             # 列出可用流派

新增流派：直接在 BUILDS 里加一行即可，无需改动 AI 代码
（AI 由 engine/ai_tactics.TACTICAL_ROLES 数据驱动）。
"""
import argparse
import math
import os
import random
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.setup_support import finish_initial_daowen

from engine.api import GameEngine
from engine.ai_tactics import TacticalAI
from sim.build_learner import _resolve_monster_turn

# 流派 = 局外要学习的道纹清单；所有构筑开局都自动持有【杀伐】。
# 学习受精力限制，最多3个/场，列表可跨场累积。
BUILDS: dict[str, dict] = {
    "杀伐系":   {"learn": ["庇护", "再生", "波及", "连击", "束缚"]},
    "波及系":   {"learn": ["波及", "贯穿", "透支", "束缚", "封印", "增殖"]},
    # 2026-08-21：冲击改名波及；缓慢/慈悲/切割已删除。
    "波及纯控": {"learn": ["波及", "束缚", "封印", "贯穿"]},
    "龙心谷系": {"learn": ["加害", "裂变", "伤痕", "龙鳞", "活血"]},
    "扭曲都市系": {"learn": ["僵化", "坏死", "退化", "定型", "爆裂"]},
    "罪孽都市系": {"learn": ["典当", "点金", "清算", "假钞"]},   # 2026-10-13：删【逼债】→【典当】顶替
    "纯杀伐对照": {"learn": []},
}

REGIONS = ["罪孽都市", "扭曲都市", "龙心谷"]


def _practice_tier1_mana(engine: GameEngine) -> dict:
    """用合法的轮回者属性点口径执行一次低档修行。

    1档只给1属性点，而法限按2点一档；若直接提交 mana_points=1，
    引擎会正确拒绝并回滚精力。旧批测器在失败后重复提交同一动作，形成死循环。
    有足够池内余点时兑2法，其余点先存池，保证测试器每次都推进。
    """
    allocations = {"mana_points": 2} if engine.state.attribute_points + 1 >= 2 else {}
    return engine.execute_action("pre_battle_action", {
        "sub_action": "修行", "tier": 1, "allocations": allocations,
    })


def run_one(build: str, region: str, seed: int, battles: int = 7) -> dict:
    """跑一局到通关或阵亡。返回结果统计。"""
    cfg = BUILDS[build]
    e = GameEngine(db_path="/tmp/winrate.db", rng_seed=seed)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": region})
    optional_relics = set()   # 2026-10-13：【三相残韵盘】删除，无需规避
    starter_relic = next((n for n in setup["result"]["relic_choices"] if n not in optional_relics),
                         setup["result"]["relic_choices"][0])
    e.execute_action("choose_discovered_relic", {"relic_name": starter_relic})

    ai = TacticalAI(e)
    to_learn = list(cfg["learn"])
    cleared = 0

    for battle_no in range(1, battles + 1):
        # 局外：优先学完流派道纹，其余精力用于修行
        while e.state.energy > 0:
            if to_learn:
                name = to_learn.pop(0)
                r = e.execute_action("pre_battle_action",
                                     {"sub_action": "学习", "sub": "daowen", "name": name})
                if not r.get("success"):
                    to_learn.insert(0, name)
                    _practice_tier1_mana(e)
            else:
                _practice_tier1_mana(e)

        from sim.optional_actions import battle_start_relic_choices
        started = e.execute_action("battle_start",
                                   {"relic_choices": battle_start_relic_choices(e)})
        if not started.get("success"):
            return {"cleared": cleared, "won": False, "died_at": battle_no,
                    "used": dict(ai.used), "hp": e.state.player.current_hp}

        for _ in range(30):
            if not e.state.player.is_alive:
                break
            if not [x for x in e.state.enemies if x.is_alive]:
                break
            from sim.optional_actions import round_start_relic_choices
            e.execute_action("round_start", {"relic_choices": round_start_relic_choices(e)})
            ai.new_round()
            ai.take_turn()
            if not [x for x in e.state.enemies if x.is_alive]:
                break
            mp = _resolve_monster_turn(e)
            if not mp.get("success") or mp["result"].get("player_dead"):
                break
            e.execute_action("round_end", {})

        if not e.state.player.is_alive:
            return {"cleared": cleared, "won": False, "died_at": battle_no,
                    "used": dict(ai.used), "hp": 0}
        if [x for x in e.state.enemies if x.is_alive]:
            return {"cleared": cleared, "won": False, "died_at": battle_no,
                    "used": dict(ai.used), "hp": e.state.player.current_hp}
        ended = e.execute_action("battle_end", {})
        if not ended.get("success"):
            return {"cleared": cleared, "won": False, "died_at": battle_no,
                    "used": dict(ai.used), "hp": e.state.player.current_hp}
        cleared += 1

    # 完成第7场会触发【最终的冠冕】：角色被完整封存，state.player 可能已被置空/替换。
    # 这是引擎的正确行为，此处只做防御性取值。
    final_hp = e.state.player.current_hp if e.state.player else None
    return {"cleared": cleared, "won": True, "died_at": None,
            "used": dict(ai.used), "hp": final_hp}


def bench(build: str, region: str, runs: int) -> dict:
    wins = 0
    total_cleared = 0
    deaths = Counter()
    usage = Counter()
    for s in range(1, runs + 1):
        try:
            r = run_one(build, region, seed=s)
        except Exception as ex:                      # 单局异常不应中断整体统计
            deaths["ERROR"] += 1
            if os.environ.get("WINRATE_DEBUG"):
                print(f"  [error] {build}/{region}/seed{s}: {ex}")
            continue
        total_cleared += r["cleared"]
        if r["won"]:
            wins += 1
        else:
            deaths[r["died_at"]] += 1
        usage.update(r["used"])
    return {"build": build, "region": region, "runs": runs, "wins": wins,
            "winrate": wins / runs if runs else 0,
            "avg_cleared": total_cleared / runs if runs else 0,
            "deaths": deaths, "usage": usage}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--region", default=None)
    ap.add_argument("--build", default=None)
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    if a.list:
        for k, v in BUILDS.items():
            print(f"{k:12} 初始=杀伐（自动） 学习={v['learn']}")
        return

    builds = [a.build] if a.build else list(BUILDS)
    regions = [a.region] if a.region else REGIONS

    print(f"{'流派':<12}{'副本':<10}{'胜率':>8}{'平均通关场数':>14}   死亡场次分布")
    print("-" * 78)
    rows = []
    for b in builds:
        for rg in regions:
            r = bench(b, rg, a.runs)
            rows.append(r)
            dist = "、".join(
                (f"第{k}场×{v}" if isinstance(k, int) else f"{k}×{v}")
                for k, v in sorted(r["deaths"].items(),
                                   key=lambda kv: (not isinstance(kv[0], int),
                                                   kv[0] if isinstance(kv[0], int) else 0))
            ) or "无阵亡"
            print(f"{b:<12}{rg:<10}{r['winrate']*100:>7.1f}%{r['avg_cleared']:>14.2f}   {dist}")

    print("\n=== 道纹实际使用次数（验证 AI 真的在用这些道纹）===")
    for r in rows:
        top = "、".join(f"{k}×{v}" for k, v in r["usage"].most_common(8))
        print(f"  {r['build']:<12}{r['region']:<10}{top or '（无）'}")


if __name__ == "__main__":
    main()
