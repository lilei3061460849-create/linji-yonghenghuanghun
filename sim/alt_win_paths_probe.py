#!/usr/bin/env python3
"""
另类胜利路径探针：验证穷举报告的方法论盲区。

TacticalAI 只按"保命→收割→输出"决策，从不主动利用两条规则内的胜利路径：
  A) 凡庸盾守流：庇护把怪物每回合实际伤害压到0，连续5回合后怪物触发【凡庸】
     自爆（非轮回者优先）；自己每≤4回合用杀伐X=1戳一下重置己方计数。
  B) 癌变奶怪流：对敌方持续发动再生（过量回复按双倍计入累计），
     累计回复达 2×[血限] 时怪物触发【癌变】被吸收进《死者之书》；
     同样戳一下防己方凡庸，余量法力留给庇护自保。

用法：PYTHONPATH=. python3 sim/alt_win_paths_probe.py [--seeds 20]
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.api import GameEngine
from sim.build_learner import _resolve_monster_turn
from sim.optional_actions import battle_start_relic_choices, round_start_relic_choices

INTERACTIVE_RELICS = {"回锋刀", "无所求"}   # 2026-10-13：【三相残韵盘】删除


def _setup(seed: int, region: str, learn: list[str]):
    e = GameEngine(db_path=f"/tmp/altpath_{os.getpid()}.db", rng_seed=seed)
    e.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 7, "speed_points": 8, "mana_points": 10})
    rc = list(e.state.pending_relic_choices)
    pick = next((n for n in rc if n not in INTERACTIVE_RELICS),
                next((n for n in rc if n != "无所求"), rc[0]))
    e.execute_action("choose_discovered_relic", {"relic_name": pick})
    dc = list(e.state.pending_initial_daowen_choices)
    dp = next((n for n in ("庇护", "再生", "杀伐", "连击") if n in dc), dc[0])
    e.execute_action("setup_choose_initial_daowen", {"daowen_name": dp})
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": region})
    return e


def _learn_phase(e, to_learn: list[str]):
    while e.state.energy > 0:
        if to_learn:
            name = to_learn.pop(0)
            r = e.execute_action("pre_battle_action",
                                 {"sub_action": "学习", "sub": "daowen", "name": name})
            if not r.get("success"):
                if "已经掌握" in str(r.get("error", "")):
                    continue
                to_learn.insert(0, name)
                e.execute_action("pre_battle_action",
                                 {"sub_action": "修行", "tier": 1, "to": "mana"})
        else:
            e.execute_action("pre_battle_action",
                             {"sub_action": "修行", "tier": 1, "to": "mana"})


def _expected_enemy_output(e) -> int:
    total = 0
    for m in e.state.enemies:
        if m.is_alive:
            total += max(0, m.attack_power) * max(1, m.attack_count)
    return total


def _cast(e, name: str, x: int, target: str = ""):
    if name not in e.state.player.dao_wen or x <= 0:
        return {"success": False}
    params = {"daowen_name": name, "x": x}
    if target:
        params["target_ref"] = target
    return e.execute_action("use_daowen", params)


def _alive_idx(e):
    for i, m in enumerate(e.state.enemies):
        if m.is_alive:
            return i, m
    return None, None


def player_round_stall(e):
    """凡庸盾守流：先戳(防己方计数脱同步)，再全力庇护。"""
    p = e.state.player
    idx, m = _alive_idx(e)
    if m is None:
        return
    # 己方凡庸计数≥2时就戳（保险：怪一旦蹭到伤害其计数重置，己方就会单独达阈值）
    if p.no_damage_rounds >= 2 and "杀伐" in p.dao_wen:
        _cast(e, "杀伐", 1, f"enemy:{idx}")
    need = (_expected_enemy_output(e) - p.shield + 1) // 2 + 1
    x = min(max(need, 1), p.current_mana)
    if x > 0:
        _cast(e, "庇护", x, "player:0")


def player_round_pure_stall(e):
    """纯盾守流（不戳）：只叠盾。依赖DM裁定——同拍双爆时怪先炸、
    战场清空则我方凡庸中断结算。风险：怪蹭到1点伤害即计数脱同步。"""
    p = e.state.player
    if _alive_idx(e)[1] is None:
        return
    need = (_expected_enemy_output(e) - p.shield + 1) // 2 + 1
    x = min(max(need, 1), p.current_mana)
    if x > 0:
        _cast(e, "庇护", x, "player:0")


def player_round_cancer(e):
    """癌变奶怪流：戳防凡庸→小盾自保→余量法力全部再生奶怪。"""
    p = e.state.player
    idx, m = _alive_idx(e)
    if m is None:
        return
    if p.no_damage_rounds >= 2 and "杀伐" in p.dao_wen:
        _cast(e, "杀伐", 1, f"enemy:{idx}")
    # 血线危险才起盾，否则法力全给奶
    if p.current_hp - _expected_enemy_output(e) < p.blood_limit * 0.35:
        need = (_expected_enemy_output(e) - p.shield + 1) // 2 + 1
        _cast(e, "庇护", min(max(need, 1), max(0, p.current_mana - 4)), "player:0")
    x = p.current_mana
    if x > 0:
        _cast(e, "再生", x, f"enemy:{idx}")


def player_round_hybrid(e):
    """奶大砍小（混合）：血限≤80的小怪直接杀伐收割；大怪用再生奶到癌变。
    小怪被砍时顺带重置己方凡庸计数；血线危险先起盾。"""
    p = e.state.player
    alive = [(i, m) for i, m in enumerate(e.state.enemies) if m.is_alive]
    if not alive:
        return
    if p.current_hp - _expected_enemy_output(e) < p.blood_limit * 0.35:
        need = (_expected_enemy_output(e) - p.shield + 1) // 2 + 1
        _cast(e, "庇护", min(max(need, 1), max(0, p.current_mana - 4)), "player:0")
    small = [(i, m) for i, m in alive if m.blood_limit <= 80]
    if small:
        i, m = min(small, key=lambda t: t[1].current_hp)
        x = min(p.current_mana, max(1, (m.current_hp + 1) // 2))
        _cast(e, "杀伐", x, f"enemy:{i}")
    elif p.no_damage_rounds >= 2 and "杀伐" in p.dao_wen:
        i, m = min(alive, key=lambda t: t[1].current_hp)
        _cast(e, "杀伐", 1, f"enemy:{i}")
    big = [(i, m) for i, m in alive if m.is_alive and m.blood_limit > 80]
    if big and p.current_mana > 0:
        i, m = max(big, key=lambda t: t[1].blood_limit)
        _cast(e, "再生", p.current_mana, f"enemy:{i}")


def run_one(policy, seed: int, region: str, learn: list[str], battles: int = 7) -> dict:
    e = _setup(seed, region, list(learn))
    to_learn = list(learn)
    cleared = 0
    win_paths: Counter = Counter()
    for battle_no in range(1, battles + 1):
        _learn_phase(e, to_learn)
        st = e.execute_action("battle_start",
                              {"relic_choices": battle_start_relic_choices(e)})
        if not st.get("success"):
            return {"cleared": cleared, "won": False, "died_at": battle_no,
                    "paths": dict(win_paths)}
        hp_before = {id(m): True for m in e.state.enemies}
        for _ in range(40):
            if not e.state.player.is_alive:
                break
            if not [x for x in e.state.enemies if x.is_alive]:
                break
            e.execute_action("round_start",
                             {"relic_choices": round_start_relic_choices(e)})
            policy(e)
            if not [x for x in e.state.enemies if x.is_alive]:
                break
            mp = _resolve_monster_turn(e)
            if not mp.get("success") or mp["result"].get("player_dead"):
                break
            e.execute_action("round_end", {})
        if not e.state.player.is_alive or [x for x in e.state.enemies if x.is_alive]:
            return {"cleared": cleared, "won": False, "died_at": battle_no,
                    "paths": dict(win_paths)}
        # 统计非常规移出路径
        for m in e.state.enemies:
            if getattr(m, "is_proliferated", False):
                win_paths["癌变"] += 1
            elif m.current_hp <= 0 and getattr(m, "no_damage_rounds", 0) == 0 \
                    and getattr(e.state, "last_mediocrity", None):
                pass
        if not e.execute_action("battle_end", {}).get("success"):
            return {"cleared": cleared, "won": False, "died_at": battle_no,
                    "paths": dict(win_paths)}
        cleared += 1
    return {"cleared": cleared, "won": True, "died_at": None, "paths": dict(win_paths)}


def bench(policy, name: str, seeds, regions):
    print(f"\n=== {name} ===")
    for region in regions:
        wins, cleared_sum, deaths = 0, 0, Counter()
        paths: Counter = Counter()
        for s in seeds:
            try:
                r = run_one(policy, s, region,
                            ["庇护", "再生", "杀伐"] if policy is not None else [])
            except Exception as ex:
                deaths["ERR"] += 1
                continue
            cleared_sum += r["cleared"]
            paths.update(r["paths"])
            if r["won"]:
                wins += 1
            else:
                deaths[r["died_at"]] += 1
        n = len(seeds)
        print(f"  {region}: 胜率 {wins}/{n} = {wins/n*100:.0f}%  "
              f"平均通关 {cleared_sum/n:.2f}  死亡分布 {dict(deaths)}  "
              f"非常规移出 {dict(paths)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    args = ap.parse_args()
    seeds = list(range(1, args.seeds + 1))
    regions = ["罪孽都市", "扭曲都市", "龙心谷"]
    bench(player_round_stall, "A) 凡庸盾守流（庇护挡满5回合→怪自爆；带保险戳）", seeds, regions)
    bench(player_round_pure_stall,
          "A2) 纯盾守流（不戳；依赖DM裁定：怪炸完战场清空则我方凡庸中断）", seeds, regions)
    bench(player_round_cancer, "B) 癌变奶怪流（再生奶怪到2×血限→吸入死者之书）", seeds, regions)


if __name__ == "__main__":
    main()
