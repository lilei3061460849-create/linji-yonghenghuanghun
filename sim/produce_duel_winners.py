#!/usr/bin/env python3
"""产出「经历过死斗」的二阶候选人（用户要求）。

一阶死斗规则（规则正文）：第1位通关者无候选→封存成守擂者；
之后每位通关者（有候选）→ 触发第8场死斗 → 死斗胜利者领取终音法器后完整封存，
成为下一位候选人（进入二阶）。败者失去轮回者身份（死者之页）。

本脚本模拟完整擂台循环：
  1. 第1位通关者 → 封存（守擂槽），不算候选人
  2. 后续每位通关者 → 真实死斗（PvP对称驱动，守擂方走玩家侧接口）
     挑战者胜 → 领取终音法器 → 封存 → 计入候选人池（带 duel_won 标记）
     守擂者胜 → 挑战者败（死者之页），守擂者继续守擂，池不增
  3. 直到候选人池满 target 人

用法：python3 sim/produce_duel_winners.py --count 10 --max-games 400
"""
import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.produce_real_winners import play_first_tier, WINNER_DIR, REGIONS

SLOT = "/tmp/duel_slot.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--max-games", type=int, default=400)
    ap.add_argument("--no-spend", action="store_true")
    ap.add_argument("--resume", action="store_true", help="续跑：保留现有候选人+守擂槽")
    ap.add_argument("--start-seed", type=int, default=1)
    args = ap.parse_args()

    os.makedirs(WINNER_DIR, exist_ok=True)
    if not args.resume:
        for fn in os.listdir(WINNER_DIR):
            os.remove(os.path.join(WINNER_DIR, fn))
        if os.path.exists(SLOT):
            os.remove(SLOT)

    guard_name = None       # 当前守擂者（第1位通关封存）
    candidates = len([f for f in os.listdir(WINNER_DIR) if f.startswith("winner_")])
    seed = args.start_seed
    played = 0
    duels_fought = 0
    while candidates < args.count and played < args.max_games:
        region = REGIONS[seed % len(REGIONS)]
        # 守擂槽跨局保留：SLOT 存在 → 本局通关者会触发死斗（对守擂者）。
        # 仅在死斗胜利读走候选人后清空（败者守擂者继续守擂）。
        r = play_first_tier(seed, region, SLOT, spend_shards=not args.no_spend)
        played += 1

        if r.get("invalid"):
            print(f"#{played:>3} seed={seed:<4} {region} ⚠ invalid: {r.get('reason','')[:60]}")
            seed += 1
            continue

        if r.get("won") and os.path.exists(SLOT):
            # 通关并写槽
            if guard_name is None and not r.get("duel_won"):
                # 第1位：无候选 → 封存成守擂者
                guard_name = r.get("sealed_name") or "守擂者"
                print(f"#{played:>3} seed={seed:<4} {region} 第1位通关 → 封存为守擂者「{guard_name}」")
            elif r.get("duel_won"):
                # 死斗胜利：领取终音法器后封存 → 候选人
                duels_fought += 1
                candidates += 1
                out = os.path.join(WINNER_DIR, f"winner_{candidates:02d}.json")
                with open(SLOT, encoding="utf-8") as f:
                    snapshot = json.load(f)
                snapshot["origin"] = {
                    "region": region, "seed": seed, "rank": candidates,
                    "duel_won": True, "duels_fought": duels_fought,
                    "terminal_artifact": True,
                }
                with open(out, "w", encoding="utf-8") as f:
                    json.dump(snapshot, f, ensure_ascii=False, indent=2)
                p = snapshot["player"]
                print(f"#{played:>3} seed={seed:<4} {region} ⚔死斗胜({duels_fought}场) → 候选人#{candidates} "
                      f"法{p['mana_limit']} 碎片{snapshot.get('shards')} 法器{snapshot.get('artifacts_owned')}")
                # 死斗胜利者已进入二阶（成为候选人），清空守擂槽等新守擂者
                guard_name = None
                if os.path.exists(SLOT):
                    os.remove(SLOT)
            else:
                print(f"#{played:>3} seed={seed:<4} {region} ⚠ 通关但未触发死斗（槽状态异常）")
        elif r.get("duel") == "lost":
            duels_fought += 1
            print(f"#{played:>3} seed={seed:<4} {region} ⚔死斗败({duels_fought}场) {r.get('duel_reason','')[:40]} 守擂者「{guard_name}」继续守擂")
        else:
            print(f"#{played:>3} seed={seed:<4} {region} 通关{r.get('cleared',0)}/7 ❌")
        seed += 1

    print(f"\n完成：{played} 局 | 死斗 {duels_fought} 场 | 候选人 {candidates} 位（全部为死斗胜利者，带终音法器）→ {WINNER_DIR}/")


if __name__ == "__main__":
    main()
