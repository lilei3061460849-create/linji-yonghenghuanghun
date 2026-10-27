"""设计意图符合性审计（2026-08-23 续「还债路径断点」教训）。

问题类目：规则写了、面板配了，实战里却永不触发/永不发动——非 bug 的
"非预期效果"。两类探针：
1. 特殊事件实际触发计数（还债/雕塑/救赎/进化/崩解/凡庸/癌变/封印/逃跑…）；
2. 怪物面板道纹的 持有战斗数 vs 实际发动数（找出被优先级/门禁锁死的死纹，
   先例如：通缉犯持消灾但机制组永远被自保组假钞压死）。

用法：python3 -u sim/design_intent_audit.py
"""
from __future__ import annotations

import collections
import json
import sys
import time

sys.path.insert(0, ".")

from engine import api as api_mod                      # noqa: E402
from engine import combat as combat_mod                # noqa: E402
from engine.ai_tactics import TacticalAI               # noqa: E402
from engine.combat_events import CombatEventType       # noqa: E402
from sim.build_learner import _skip_resource_timeout, play                     # noqa: E402

SEEDS = range(1, 121)
REGIONS = ["扭曲都市", "罪孽都市", "龙心谷"]

FIRES = collections.Counter()
HELD = collections.defaultdict(collections.Counter)    # region -> 道纹 -> 持有战斗数
FIRED = collections.defaultdict(collections.Counter)   # region -> 道纹 -> 发动成功数
FAILFIRE = collections.defaultdict(collections.Counter)
GAME_REGION = {"region": ""}


# ---------------- 探针（monkeypatch） ----------------
_orig_sculpt = combat_mod.CombatEngine._sculpture_monster
def _spy_sculpt(self, m):
    FIRES["雕塑"] += 1
    return _orig_sculpt(self, m)
combat_mod.CombatEngine._sculpture_monster = _spy_sculpt

_orig_bind = combat_mod.CombatEngine._debt_bind_monster
def _spy_bind(self, m):
    FIRES["还债"] += 1
    return _orig_bind(self, m)
combat_mod.CombatEngine._debt_bind_monster = _spy_bind

_orig_redeem = combat_mod.CombatEngine._queue_redemption
def _spy_redeem(self, m, cause):
    FIRES["救赎触发"] += 1
    return _orig_redeem(self, m, cause)
combat_mod.CombatEngine._queue_redemption = _spy_redeem

_orig_evo = combat_mod.CombatEngine.execute_evolution
def _spy_evo(self, m, name, x):
    r = _orig_evo(self, m, name, x)
    FIRES["进化尝试"] += 1
    if r.get("success"):
        FIRES["进化成功"] += 1
    return r
combat_mod.CombatEngine.execute_evolution = _spy_evo

_orig_resolve = combat_mod.CombatEngine._resolve_monster_daowen_choice
def _spy_resolve(self, monster, choice, refs, activated, prepared_option):
    name = choice.get("name", "")
    region = self.state.current_region
    try:
        r = _orig_resolve(self, monster, choice, refs, activated, prepared_option)
    except Exception:
        FAILFIRE[region][name] += 1
        raise
    FIRED[region][name] += 1
    return r
combat_mod.CombatEngine._resolve_monster_daowen_choice = _spy_resolve

_orig_exec = api_mod.GameEngine.execute_action
def _spy_exec(self, action_type, params=None):
    r = _orig_exec(self, action_type, params)
    if action_type in ("battle_start", "start_battle") and r.get("success"):
        region = self.state.current_region
        seen = set()
        for en in self.state.enemies:
            if en.entity_type != "怪物":
                continue
            for dw in en.dao_wen:
                seen.add(dw)
        for dw in seen:
            HELD[region][dw] += 1
    return r
api_mod.GameEngine.execute_action = _spy_exec


def scan_end_events(e):
    """局末扫描事件流：崩解（命零 subtype=mutation）。"""
    n = 0
    for ev in e.state.combat_events:
        if ev.event_type == CombatEventType.ENTITY_DIED and (ev.ctx or {}).get("subtype") == "mutation":
            n += 1
    return n


def run_region(region):
    n = inv = deaths = 0
    cleared_sum = 0.0
    alt = collections.Counter()
    duel_fought = duel_won = sealed = 0
    for seed in SEEDS:
        tel = {}
        r = play("庇护", ["杀伐", "再生", "贯穿"], region, seed=seed, battles=7,
                 telemetry=tel, ai_cls=TacticalAI)
        if _skip_resource_timeout(r, context=__name__):
            continue
        if r.get("invalid"):
            inv += 1
            continue
        n += 1
        cleared_sum += r["cleared"]
        deaths += (r["cleared"] == 0 or not r.get("won"))
        for k, v in (tel.get("alt_victory") or {}).items():
            alt[k] += v
        dz = tel.get("duels") or {}
        duel_fought += dz.get("fought", 0)
        duel_won += dz.get("won", 0)
        sealed += dz.get("sealed_no_duel", 0)
    # 崩解计数由探针 scan（需在 play 内逐局事件流——此处用全局钩子更稳，略）
    return dict(region=region, n=n, inv=inv, avg=round(cleared_sum / max(1, n), 2),
                alt=dict(alt.most_common()), duel_fought=duel_fought,
                duel_won=duel_won, sealed=sealed)


def main():
    t0 = time.time()
    rows = []
    fire0 = dict(FIRES)
    for region in REGIONS:
        before = dict(FIRES)
        row = run_region(region)
        row["fires"] = {k: FIRES[k] - before.get(k, 0) for k in
                        set(FIRES) | set(before) if FIRES[k] - before.get(k, 0)}
        rows.append(row)
        print(f"[done] {region}: avg={row['avg']} alt={row['alt']} fires={row['fires']}",
              flush=True)

    print("\n===== 特殊事件触发率（每区 %d 局，mainline 基准 build） =====" % len(list(SEEDS)))
    for row in rows:
        n = row["n"]
        bits = []
        merged = dict(list(row["alt"].items()))
        merged.update(row["fires"])
        for k in ("凡庸", "封印", "伤害击杀", "救赎", "救赎触发", "雕塑", "癌变",
                  "还债", "逃跑", "进化尝试", "进化成功"):
            v = merged.get(k, 0)
            bits.append(f"{k}={v}")
        print(f"{row['region']}: " + " ".join(bits)
              + f"｜死斗={row['duel_fought']}胜{row['duel_won']}｜封存={row['sealed']}｜无效={row['inv']}")

    print("\n===== 怪物面板死纹（持有≥15战斗但发动≤1 或 发动率<2%） =====")
    for region in REGIONS:
        print(f"-- {region}")
        dead = []
        for dw, held in HELD[region].most_common():
            fired = FIRED[region].get(dw, 0)
            if held >= 15 and (fired <= 1 or fired / held < 0.02):
                dead.append((dw, held, fired, FAILFIRE[region].get(dw, 0)))
        for dw, held, fired, fail in dead:
            print(f"   {dw}: 持有{held}战 发动{fired}次 发动失败{fail}次")

    print("\n===== 全量发动矩阵（region/道纹: fired/held） =====")
    for region in REGIONS:
        line = " ".join(f"{dw}:{FIRED[region].get(dw,0)}/{held}"
                        for dw, held in HELD[region].most_common())
        print(f"{region}: {line}")

    json.dump({"fires": dict(FIRES), "held": {r: dict(v) for r, v in HELD.items()},
               "fired": {r: dict(v) for r, v in FIRED.items()},
               "fail": {r: dict(v) for r, v in FAILFIRE.items()},
               "rows": rows},
              open("/tmp/design_audit.json", "w"), ensure_ascii=False, indent=1)
    print(f"\n({time.time()-t0:.0f}s) 已写 /tmp/design_audit.json")


if __name__ == "__main__":
    main()
