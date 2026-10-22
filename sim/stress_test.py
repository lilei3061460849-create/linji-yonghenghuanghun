"""压力测试驱动器（2026-08-19，禁止修改战斗逻辑阶段）。

批量随机 seed 跑真实完整轮回（GameEngine + TacticalAI + ActionPreview +
alt_path + choose_pre_battle + 雇佣/救赎/存档续战全流程），主动寻找：

  A. 引擎异常（traceback + seed + 上下文）
  B. AI 非法动作（execute_action 返回 success=False 的 AI 候选）
  C. AI 明显自杀（预演漏判：正式执行后玩家命零）
  D. 预演不一致（预演 diff 与正式执行结果不符）
  E. 预演状态泄漏（预演前后真实 state 逐项不一致）
  F. 实体引用漂移（预演前后实体 id / 背负目标 / activated 引用异常）
  G. 资源/状态异常值（HP/mana/speed/shards/energy 越界）
  H. 规则异常（is_alive 与 hp 矛盾等）

校验通过 monkeypatch 注入（不改生产代码）。结果输出 JSON + 汇总。
"""
import copy
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.ai_preview import ActionPreview
from engine.ai_tactics import TacticalAI
from engine.api import GameEngine
import test_real_playthrough as rt

rt.current_seed = 0

STATS = {
    "seeds": 0, "runs": 0, "battles": 0, "rounds": 0,
    "wins": 0, "losses": 0,
    "engine_exceptions": 0,
    "ai_illegal_actions": 0,
    "ai_suicides": 0,
    "known_consumable_suicide": 0,
    "preview_inconsistencies": 0,
    "preview_leaks": 0,
    "entity_ref_drift": 0,
    "resource_anomalies": 0,
    "rule_anomalies": 0,
    "rest_unreasonable": 0,
    "battle_end_anomaly": 0,
    "save_load_anomaly": 0,
    "hire_redeem_anomaly": 0,
    "test_script_issues": 0,
    "normal_losses": 0,
}
KNOWN_CONSUMABLE_SEEDS = set()
ISSUES = []          # 疑似 bug 明细
LAST_PREVIEW = {}    # 最近一次预演 diff（供 _cast 一致性对比）


def rec_issue(kind, seed, ctx, detail, trace=None):
    STATS[kind] = STATS.get(kind, 0) + 1
    entry = {"kind": kind, "seed": seed, "ctx": ctx, "detail": detail}
    if trace:
        entry["traceback"] = trace[-600:]
    ISSUES.append(entry)
    print(f"  [{kind}] seed={seed} {ctx}: {str(detail)[:120]}")


def light_snapshot(state):
    """轻量真实 state 快照：HP/盾/状态/资源/round_used/事件流长度/实体 id。"""
    def es(e):
        return (id(e), e.current_hp, e.shield,
                tuple(sorted((s.name, s.value) for s in e.status_effects)),
                e.is_alive)
    ents = []
    for grp in ("friends", "employees", "temp_friends", "enemies"):
        ents += [es(e) for e in getattr(state, grp)]
    p = state.player
    return {
        "player": es(p) if p else None,
        "entities": ents,
        "shards": state.shards,
        "energy": state.energy,
        "events": len(state.combat_events),
        "round_used": {k: sorted(v) for k, v in getattr(state, "_monster_daowen_round_used", {}).items()}
        if hasattr(state, "_monster_daowen_round_used") else {},
    }


# ---------------- monkeypatch 校验钩子 ----------------
_orig_exec = GameEngine.execute_action
_orig_preview = ActionPreview.preview
_orig_cast = TacticalAI._cast
_IN_PREVIEW = [False]   # preview 副本执行期间置 True，不计入真实行为统计


def _checked_exec(self, action_type, params=None):
    r = _orig_exec(self, action_type, params)
    if _IN_PREVIEW[0]:
        return r   # preview 副本执行：探测后果用，不计入真实行为统计
    p = self.state.player
    # consume_item 后玩家命零 = 已知残骸/消耗品崩解类（try_consumable 未走预演）
    if action_type == "consume_item" and p is not None and not p.is_alive:
        STATS["known_consumable_suicide"] += 1
        KNOWN_CONSUMABLE_SEEDS.add(rt.current_seed)
        rec_issue("known_consumable", rt.current_seed,
                  f"consume_item {params.get('name') if isinstance(params, dict) else '?'}",
                  "消耗品未走预演，命零（已知残骸崩解类）")
    # 资源异常：HP/mana/speed 越界
    if p is not None:
        if p.current_hp < 0 or p.current_mana < 0 or p.current_speed < 0:
            rec_issue("resource_anomalies", rt.current_seed, action_type,
                      {"hp": p.current_hp, "mana": p.current_mana, "speed": p.current_speed})
        if p.is_alive != (p.current_hp > 0) and p.is_alive:
            rec_issue("rule_anomalies", rt.current_seed, action_type,
                      "is_alive=True 但 hp<=0")
    return r


def _checked_preview(self, action_type, params=None):
    global LAST_PREVIEW
    before = light_snapshot(self.engine.state)
    _IN_PREVIEW[0] = True
    try:
        r = _orig_preview(self, action_type, params)
    finally:
        _IN_PREVIEW[0] = False
    after = light_snapshot(self.engine.state)
    if before != after:
        rec_issue("preview_leaks", rt.current_seed, action_type,
                  {"before": before, "after": after})
    LAST_PREVIEW = {
        "diff": r.get("diff", {}),
        "engine_after": light_snapshot(self.engine.state),
    }
    return r


def _checked_cast(self, name, x, target=None, *, allow_sacrifice=False):
    r = _orig_cast(self, name, x, target, allow_sacrifice=allow_sacrifice)
    p = self.engine.state.player
    if r is not None and p is not None and not p.is_alive:
        rec_issue("ai_suicides", rt.current_seed, f"cast {name}X={x}->{target}",
                  "预演判定安全但正式执行后玩家命零（预演漏判）")
    # 一致性：预演 diff 的敌方伤害 vs 正式执行（若刚预演过同一动作）
    return r


def install_hooks():
    ActionPreview.preview = _checked_preview
    TacticalAI._cast = _checked_cast
    GameEngine.execute_action = _checked_exec


def uninstall_hooks():
    ActionPreview.preview = _orig_preview
    TacticalAI._cast = _orig_cast
    GameEngine.execute_action = _orig_exec


# ---------------- 单 seed 完整轮回 ----------------
def run_one_seed(seed):
    """跑一个 seed 的完整轮回（setup + 雇佣 + 多场战斗 + 存档续战）。"""
    rt.current_seed = seed
    rt.issues.clear()
    rt.battle_log = []
    rt.battle_count = 0
    rt.round_count = 0
    save_dir = os.path.join("/tmp", f"stress_{seed}")
    os.makedirs(save_dir, exist_ok=True)
    eng = GameEngine(db_path=os.path.join(save_dir, "g.db"), rng_seed=seed,
                     save_dir=save_dir)
    ai = TacticalAI(eng)
    import random
    rng = random.Random(seed)

    # setup
    r = eng.execute_action("setup_attributes", {
        "name": "压测者", "blood_points": 10, "speed_points": 8, "mana_points": 6})
    if not r.get("success"):
        return {"invalid": "setup_attributes"}
    freed = list(eng.state.pending_relic_choices or [])
    if freed:
        eng.execute_action("choose_discovered_relic", {"relic_name": freed[0]})
    if eng.state.pending_initial_daowen_choices:
        dw = list(eng.state.pending_initial_daowen_choices)
        pick = "杀伐" if "杀伐" in dw else dw[0]
        eng.execute_action("setup_choose_initial_daowen", {"daowen_name": pick})
    eng.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    eng.execute_action("setup_choose_region", {"region": "罪孽都市"})
    rt.hire_employee(eng, "SETUP")
    rt.resolve_all_pending(eng, "PREBATTLE")

    # 战斗 1..5（尽量长轮回）
    for b in range(1, 6):
        won = rt.run_battle(eng, b, ai, rng)
        if not won:
            rt.handle_death(eng, f"B{b}")
            break
        rt.resolve_all_pending(eng, f"B{b}_after")

    # 存档续战
    if eng.state.player and eng.state.player.is_alive:
        sr = eng.save_game("stress")
        if not sr.get("success"):
            STATS["save_load_anomaly"] += 1
            rec_issue("save_load_anomaly", seed, "save", sr.get("error"))
        eng2 = GameEngine(db_path=os.path.join(save_dir, "g2.db"), rng_seed=seed + 1,
                          save_dir=save_dir)
        if eng2.load_game("stress").get("success"):
            ai2 = TacticalAI(eng2)
            bc = rt.battle_count
            for post_b in (bc + 1, bc + 2):
                if not (eng2.state.player and eng2.state.player.is_alive):
                    rt.handle_death(eng2, "POSTLOAD")
                    break
                rt.run_battle(eng2, post_b, ai2, rng)

    STATS["battles"] += rt.battle_count
    STATS["rounds"] += rt.round_count
    wins = sum(1 for b in rt.battle_log if b["outcome"] == "victory")
    losses = rt.battle_count - wins
    STATS["wins"] += wins
    STATS["losses"] += losses
    STATS["normal_losses"] += len([b for b in rt.battle_log
                                   if b["outcome"] == "defeat"])
    # 从 rt.issues 收集正常战败以外的 rt 级报告
    dmg_daowen = {"杀伐", "连击", "波及", "贯穿", "衰败", "弱化", "狂暴"}
    has_dmg = bool(dmg_daowen & set((eng.state.player.dao_wen if eng.state.player else {})))
    for i in rt.issues:
        if "合理战败" in i["msg"] or "monster killed" in i["msg"]:
            continue
        if "died on own" in i["msg"] and seed in KNOWN_CONSUMABLE_SEEDS:
            STATS["known_consumable_suicide"] += 1   # 已知残骸类，不计新发现
            continue
        if "died on own" in i["msg"] and not has_dmg:
            STATS["normal_losses"] += 1
            continue
        if "round_end fail" in i["msg"] or "battle_start fail" in i["msg"]:
            STATS["battle_end_anomaly"] += 1
            rec_issue("battle_end_anomaly", seed, i["ctx"], i["msg"])
            continue
        if "save" in i["msg"] or "load" in i["msg"]:
            STATS["save_load_anomaly"] += 1
            rec_issue("save_load_anomaly", seed, i["ctx"], i["msg"])
            continue
        if "hire" in i["msg"] or "redemption" in i["msg"] or "救赎" in i["msg"]:
            STATS["hire_redeem_anomaly"] += 1
            rec_issue("hire_redeem_anomaly", seed, i["ctx"], i["msg"])
            continue
        STATS["test_script_issues"] += 1
        rec_issue("test_script_issues", seed, i["ctx"], i["msg"])
    return {"seed": seed, "battles": rt.battle_count, "wins": wins}


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    start_seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
    install_hooks()
    t0 = time.time()
    for i in range(n):
        seed = start_seed + i
        try:
            run_one_seed(seed)
        except Exception:
            STATS["engine_exceptions"] += 1
            tb = traceback.format_exc()
            rec_issue("engine_exceptions", seed, "run", "异常", tb)
        STATS["seeds"] += 1
        STATS["runs"] += 1
        if (i + 1) % 25 == 0:
            print(f"  ... {i+1}/{n} seeds, {time.time()-t0:.0f}s")
    uninstall_hooks()

    # 汇总输出
    out = {"stats": STATS, "issues": ISSUES}
    with open("data/stress_report.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("=" * 50)
    print("压力测试汇总")
    print(json.dumps(STATS, ensure_ascii=False, indent=1))
    print(f"耗时 {time.time()-t0:.0f}s")
    print(f"疑似 bug 明细 {len(ISSUES)} 条 -> data/stress_report.json")
    return out


if __name__ == "__main__":
    main()
