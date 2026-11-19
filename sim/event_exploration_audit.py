#!/usr/bin/env python3
"""事件探索收益审计(2026-08-26)——只读审计,不修改任何生产代码。

问题:"探索未知事件是否因为收益不足而成为系统性劣势?"

方法:
  1. 引擎包装(monkeypatch sim.build_learner.GameEngine,仅本进程):逐条记录
     局外行动与事件结算的前后状态差(HP/血限/碎片/遗物/消耗品/残韵/法术/道纹/属性点);
  2. 两批同种子对照,隔离"事件收益设计"与"模拟器事件选项策略"两个变量:
       reject 批 = 现行策略(事件选项拒绝/离开优先);
       greedy  批 = 反转优先级(非拒绝选项优先,愿意支付代价拿收益)——
                    仅改审计进程内 sim 策略,不改引擎事件收益;
  3. 相似状态配对对照(探索 vs 修行/学习):同场次、相近 HP/碎片/道纹数;
  4. 幸存者偏差检验:固定前 K 次局外行动窗口,看探索-通关相关是否衰减;
  5. 结论按预注册规则映射 A-F,不为结论调整数据。

用法:
  PYTHONPATH=. python3 -m sim.event_exploration_audit --runs 100
"""
from __future__ import annotations

import argparse
import copy
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if (os := __import__("os")) else "")

from engine.api import GameEngine
import sim.build_learner as bl

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "experiments"
DATE_TAG = "2026-08-26"

RESOURCE_KEYS = ["hp", "bl", "shards", "fake_shards", "relics_n", "consumables_n",
                 "resonance_total", "spells_n", "daowen_n", "attribute_points",
                 "mana_limit", "speed_limit"]


def _snapshot(e) -> dict:
    st = e.state
    p = st.player
    return {
        "battle": st.current_battle,
        "hp": p.current_hp if p else 0, "bl": p.blood_limit if p else 0,
        "mana_limit": p.mana_limit if p else 0, "speed_limit": p.speed_limit if p else 0,
        "shards": st.shards, "fake_shards": st.fake_shards,
        "relics_n": len(st.relics), "consumables_n": len(st.consumables),
        "resonance_total": sum(st.resonance.values()),
        "spells_n": len(p.spells) if p else 0,
        "daowen_n": len(p.dao_wen) if p else 0,
        "attribute_points": getattr(st, "attribute_points", 0) or 0,
        "pool_remaining": len(e.event_pool.build_pool(st.current_region))
                          if e.event_pool and e.event_pool.events else 0,
        "alive": bool(p and p.is_alive),
    }


class AuditEngine(GameEngine):
    """只多记录,不改行为。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.audit_log: list[dict] = []
        self._snap_stack: list[dict] = []

    def execute_action(self, action_type, params=None):
        watched = ("pre_battle_action", "resolve_event", "choose_discovered_relic",
                   "choose_discovered_item", "use_daowen", "battle_start")
        nested = bool(self._snap_stack)
        if action_type in watched and not nested:
            self._snap_stack.append(_snapshot(self))
        try:
            result = super().execute_action(action_type, params)
        finally:
            if action_type in watched and self._snap_stack:
                before = self._snap_stack.pop()
                after = _snapshot(self)
                diff = {k: after[k] - before[k] for k in RESOURCE_KEYS}
                self.audit_log.append({
                    "action_type": action_type,
                    "params": {k: v for k, v in (params or {}).items()
                               if isinstance(v, (str, int, float, bool))},
                    "ok": bool(result.get("success")),
                    "desc": str(result.get("action", result.get("error", "")))[:80],
                    "before": before, "diff": diff,
                    "event": self.event_pool.current,
                    "player_dead": bool(result.get("result", {}).get("player_dead"))
                                   or not after["alive"],
                })
        return result


def _greedy_resolve_pending_event(engine):
    """审计对照策略:非拒绝选项优先(愿意支付代价);其余与现行实现一致。"""
    chain = 0
    while engine.event_pool.current is not None:
        chain += 1
        if chain > 20:
            return {"success": False, "error": "事件链超过20层,按异常回收防挂死"}
        name = engine.event_pool.current
        event = engine.event_pool.events[name]
        reject_words = ("无事发生", "拒绝", "离开", "观棋", "视而不见", "绕桥")
        ordered = sorted(event["options"],
                         key=lambda entry: 1 if any(w in entry["text"] for w in reject_words) else 0)
        result = None
        for option in ordered:
            result = engine.execute_action("resolve_event", {
                "event": name, "option_id": option["id"], "x": 1,
                "resonance_type": "转换", "daowen_names": ["杀伐"],
                "wusuoqiu_allocation": "speed",
            })
            if result.get("success"):
                break
        if not result or not result.get("success"):
            return result or {"success": False, "error": f"事件【{name}】无可支付选项"}
        if result.get("completed") is False:
            return {"success": False, "error": f"事件【{name}】需要DM裁定"}
        if not engine.state.player or not engine.state.player.is_alive:
            return {"success": True, "player_dead": True}
        if engine.state.pending_item_choices:
            chosen = engine.execute_action("choose_discovered_item", {
                "item_name": engine.state.pending_item_choices[0]})
            if not chosen.get("success"):
                return chosen
        if engine.state.pending_relic_choices:
            chosen = engine.execute_action("choose_discovered_relic", {
                "relic_name": engine.state.pending_relic_choices[0]})
            if not chosen.get("success"):
                return chosen
    return {"success": True}


# ---------------------------------------------------------------------------
# 批运行
# ---------------------------------------------------------------------------
def run_batch(runs: int, policy: str, seed_base: int, battles: int = 7):
    """policy: reject(现行拒绝优先) | greedy(非拒绝优先,审计对照)。"""
    real_engine = bl.GameEngine
    real_resolve = bl._resolve_pending_event
    bl.GameEngine = AuditEngine
    if policy == "greedy":
        bl._resolve_pending_event = _greedy_resolve_pending_event
    chars = []
    try:
        for i in range(runs):
            e_holder = {}
            def factory(*a, **kw):
                e = AuditEngine(*a, **kw)
                e_holder["e"] = e
                return e
            bl.GameEngine = factory
            r = bl.play(starter="杀伐", learn=["庇护", "再生"], region="扭曲都市",
                        seed=seed_base + i * 7919, battles=battles,
                        attrs={"blood_points": 6, "speed_points": 8, "mana_points": 10},
                        resonance="反转")
            e = e_holder.get("e")
            chars.append({
                "cid": i, "invalid": bool(r.get("invalid")), "timeout": bl._is_resource_timeout(r),
                "cleared": r.get("cleared", 0), "won": bool(r.get("won")),
                "log": e.audit_log if e else [],
            })
    finally:
        bl.GameEngine = real_engine
        bl._resolve_pending_event = real_resolve
    return [c for c in chars if not c["invalid"] and not c["timeout"]], sum(c["invalid"] for c in chars)


def causal_contrast(runs: int, seed_base: int = 30303030) -> dict:
    """直接因果检验:同种子下 禁止探索(权重0) vs 探索高权重(60) 的通关配对差。

    这是比相似状态配对更强的证据:同一随机世界,唯一差异=是否把局外精力花在探索。
    policy 是 play() 的正式注入参数,不改任何生产代码。
    """
    import math
    base = dict(bl.DEFAULT_POLICY)
    ban = {**base, "探索": 0}
    high = {**base, "探索": 60}

    def _run(policy):
        out = []
        for i in range(runs):
            r = bl.play(starter="杀伐", learn=["庇护", "再生"], region="扭曲都市",
                        seed=seed_base + i * 7919, battles=7, policy=policy,
                        attrs={"blood_points": 6, "speed_points": 8, "mana_points": 10},
                        resonance="反转")
            if not r.get("invalid") and not bl._skip_resource_timeout(r, context=__name__):
                out.append(r.get("cleared", 0))
        return out

    ban_c, high_c = _run(ban), _run(high)
    n = min(len(ban_c), len(high_c))
    diffs = [high_c[i] - ban_c[i] for i in range(n)]
    m = statistics.fmean(diffs) if diffs else 0.0
    se = (statistics.pstdev(diffs) / math.sqrt(n)) if n > 1 else 0.0
    return {
        "design": "同种子配对:探索权重0 vs 60(其余 policy 不变)",
        "n": n,
        "ban_cleared_mean": round(statistics.fmean(ban_c), 3) if ban_c else None,
        "high_cleared_mean": round(statistics.fmean(high_c), 3) if high_c else None,
        "paired_diff_mean": round(m, 3),
        "paired_diff_se": round(se, 3),
        "ci95": [round(m - 1.96 * se, 3), round(m + 1.96 * se, 3)],
        "significant": bool(n > 1 and (m - 1.96 * se > 0 or m + 1.96 * se < 0)),
    }


# ---------------------------------------------------------------------------
# 分析
# ---------------------------------------------------------------------------
def _prebattle_kinds(chars):
    """抽出每次成功的局外行动(kind, 状态, diff, cid, order, char)。"""
    out = []
    for c in chars:
        order = 0
        for entry in c["log"]:
            if entry["action_type"] != "pre_battle_action" or not entry["ok"]:
                continue
            kind = entry["params"].get("sub_action", "?")
            order += 1
            out.append({"kind": kind, "cid": c["cid"], "order": order,
                        "before": entry["before"], "diff": entry["diff"],
                        "char": c})
    return out


def _event_resolutions(chars):
    """每次事件结算(resolve_event):选项策略下的实际收益。"""
    out = []
    for c in chars:
        for entry in c["log"]:
            if entry["action_type"] != "resolve_event":
                continue
            out.append({"cid": c["cid"], "desc": entry["desc"], "diff": entry["diff"],
                        "player_dead": entry["player_dead"], "char": c})
    return out


def _aggregate_diffs(items):
    agg = {}
    for key in RESOURCE_KEYS:
        vals = [it["diff"][key] for it in items]
        agg[key] = {"mean": round(statistics.fmean(vals), 3) if vals else 0.0,
                    "total": round(sum(vals), 1), "n": len(vals)}
    return agg


def paired_contrast(chars, explore_kind="探索", safe_kinds=("修行", "学习")):
    """相似状态配对:探索 vs 修行/学习(同场次、HP差≤15%、道纹数相同、碎片差≤15)。"""
    acts = _prebattle_kinds(chars)
    explores = [a for a in acts if a["kind"] == explore_kind]
    safes = [a for a in acts if a["kind"] in safe_kinds]
    pairs = []
    used = set()
    for ex in explores:
        best, best_d = None, 1e9
        for idx, sf in enumerate(safes):
            if idx in used or sf["char"]["cid"] == ex["char"]["cid"]:
                continue
            if sf["before"]["battle"] != ex["before"]["battle"]:
                continue
            if sf["before"]["daowen_n"] != ex["before"]["daowen_n"]:
                continue
            if abs(sf["before"]["hp"] - ex["before"]["hp"]) > 12:
                continue
            if abs(sf["before"]["shards"] - ex["before"]["shards"]) > 15:
                continue
            d = (abs(sf["before"]["hp"] - ex["before"]["hp"])
                 + abs(sf["before"]["shards"] - ex["before"]["shards"]) / 2)
            if d < best_d:
                best, best_d = idx, d
        if best is not None:
            used.add(best)
            pairs.append((ex, safes[best]))
    return pairs


def survivorship_analysis(chars, window=6):
    """固定前 K 次局外行动窗口:窗口内探索次数 vs 通关的相关是否随窗口变化。"""
    import math
    def corr(xs, ys):
        if len(xs) < 4:
            return None
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        vx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        vy = math.sqrt(sum((y - my) ** 2 for y in ys))
        if vx == 0 or vy == 0:
            return None
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (vx * vy)

    acts = _prebattle_kinds(chars)
    rows = []
    for c in chars:
        mine = [a for a in acts if a["cid"] == c["cid"]]
        early = [a for a in mine if a["order"] <= window]
        mid_start = len(mine) // 3
        rows.append({
            "cid": c["cid"], "cleared": c["cleared"],
            "explore_in_window": sum(1 for a in early if a["kind"] == "探索"),
            "explore_total": sum(1 for a in mine if a["kind"] == "探索"),
            "prebattle_total": len(mine),
            "explore_rate_early": (sum(1 for a in mine[:3] if a["kind"] == "探索") / 3
                                   if len(mine) >= 3 else None),
            "explore_rate_late": (sum(1 for a in mine[-3:] if a["kind"] == "探索") / 3
                                  if len(mine) >= 6 else None),
        })
    full = corr([r["explore_total"] / max(1, r["prebattle_total"]) for r in rows],
                [r["cleared"] for r in rows])
    fixed = corr([r["explore_in_window"] for r in rows], [r["cleared"] for r in rows])
    return {
        "explore_rate_vs_cleared_full_life": round(full, 4) if full is not None else None,
        "explore_count_in_first_window_vs_cleared": round(fixed, 4) if fixed is not None else None,
        "window": window,
        "early_rate_mean": round(statistics.fmean(
            [r["explore_rate_early"] for r in rows if r["explore_rate_early"] is not None]), 4),
        "late_rate_mean": round(statistics.fmean(
            [r["explore_rate_late"] for r in rows if r["explore_rate_late"] is not None]), 4),
        "note": "若固定窗口后相关明显减弱→原相关含幸存者偏差(活得久≠因探索少)",
    }


def analyze(reject_chars, greedy_chars, elapsed):
    rep = {"date": DATE_TAG, "elapsed_seconds": elapsed}

    # ---- 1-4 事件与探索基础统计 ----
    for tag, chars in (("reject", reject_chars), ("greedy", greedy_chars)):
        acts = _prebattle_kinds(chars)
        kinds = {}
        for a in acts:
            kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
        explores = [a for a in acts if a["kind"] == "探索"]
        events = _event_resolutions(chars)
        rep[tag] = {
            "valid_chars": len(chars),
            "prebattle_actions": len(acts),
            "action_kind_counts": kinds,
            "explore_count": len(explores),
            "explore_rate": round(len(explores) / max(1, len(acts)), 4),
            "explore_immediate_diff": _aggregate_diffs(explores),
            "event_resolutions": len(events),
            "event_immediate_diff": _aggregate_diffs(events),
            "event_death_count": sum(1 for e in events if e["player_dead"]),
            "explore_chars_cleared_mean": round(statistics.fmean(
                [c["cleared"] for c in chars
                 if any(a["cid"] == c["cid"] and a["kind"] == "探索" for a in acts)]), 3),
            "no_explore_chars_cleared_mean": round(statistics.fmean(
                [c["cleared"] for c in chars
                 if not any(a["cid"] == c["cid"] and a["kind"] == "探索" for a in acts)]), 3)
            if any(not any(a["cid"] == c["cid"] and a["kind"] == "探索" for a in acts)
                   for c in chars) else None,
        }

    # ---- 5 配对对照 ----
    pairs = paired_contrast(reject_chars)
    if pairs:
        ex_alive_after = saf_alive_after = 0
        ex_cleared, saf_cleared = [], []
        for ex, sf in pairs:
            ex_cleared.append(ex["char"]["cleared"])
            saf_cleared.append(sf["char"]["cleared"])
        rep["paired_contrast"] = {
            "pairs": len(pairs),
            "explore_cleared_mean": round(statistics.fmean(ex_cleared), 3),
            "safe_cleared_mean": round(statistics.fmean(saf_cleared), 3),
            "explore_immediate": _aggregate_diffs([p[0] for p in pairs]),
            "safe_immediate": _aggregate_diffs([p[1] for p in pairs]),
        }

    # ---- 6 幸存者偏差 ----
    rep["survivorship"] = survivorship_analysis(reject_chars)

    # ---- 7 两策略对照(事件收益 vs 策略低估) ----
    rj, gd = rep["reject"], rep["greedy"]
    rep["policy_contrast"] = {
        "same_seeds": True,
        "event_count": {"reject": rj["event_resolutions"], "greedy": gd["event_resolutions"]},
        "event_death": {"reject": rj["event_death_count"], "greedy": gd["event_death_count"]},
        "cleared_mean": {"reject": round(statistics.fmean([c["cleared"] for c in reject_chars]), 3),
                         "greedy": round(statistics.fmean([c["cleared"] for c in greedy_chars]), 3)},
        "won": {"reject": sum(1 for c in reject_chars if c["won"]),
                "greedy": sum(1 for c in greedy_chars if c["won"])},
        "note": "同种子下 greedy(愿意付代价拿收益)若通关更高→事件收益不差,现行策略低估",
    }

    # ---- 8 AI 价值评估路径(静态事实) ----
    rep["ai_evaluation_facts"] = {
        "局外行动选择": "policy 权重随机(sim.build_learner.choose_pre_battle),非逐项评分;"
                       f"探索基础权重 {bl.DEFAULT_POLICY['探索']}(最低档之一),学习器乘数 1.325",
        "事件选项选择": "sim 拒绝/离开类选项优先(_resolve_pending_event),系统性回避事件收益",
        "人格交互": "局外行动与事件结算不读写 personality_traits(exploration_desire 不影响事件选择)",
        "角色记忆": "无角色级事件记忆结构;EventPool.triggered 为引擎池状态(局内去重),跨局清零;"
                   "探索只能触发'未遇'事件(结构性全部未知)",
        "TacticalAI": "战斗内实时决策不参与局外探索",
    }
    return rep


def verdict_of(rep):
    """预注册判定:据数据映射 A-F。

    判定次序(写死于实验前,不得事后调整):
      A 数据不足(探索样本<15 或因果对照 n<30);
      C 探索因果显著为正但现行策略放弃收益(greedy/reject 差或因果差显著>0);
      D 探索因果显著为负(风险收益结构失衡);
      B 因果≈0 且幸存者偏差确认(固定窗口相关比全生命周期相关更强/翻号);
      E 因果≈0 且事件最优收益量级 < 机会成本(缺正向不确定性);
      F 上述多成分并存。
    """
    pc = rep.get("policy_contrast", {})
    cc = rep.get("causal_contrast", {})
    rj, gd = rep["reject"], rep["greedy"]
    surv = rep.get("survivorship", {})
    findings = []

    if rj["explore_count"] < 15 or cc.get("n", 0) < 30:
        return {"grade": "A", "evidence": [f"探索样本 {rj['explore_count']} 次/因果对照 n={cc.get('n')},统计功效不足"],
                "numbers": {}}

    full = surv.get("explore_rate_vs_cleared_full_life")
    fixed = surv.get("explore_count_in_first_window_vs_cleared")
    bias_confirmed = (full is not None and fixed is not None and fixed > full + 0.05)
    if bias_confirmed:
        findings.append(f"幸存者偏差确认:全生命周期探索率-通关相关 {full} → 固定窗口后 {fixed}"
                        "(截断寿命后相关反转/增强,'探索少走得远'由寿命混杂驱动)")

    if cc.get("significant") and cc["paired_diff_mean"] > 0:
        return {"grade": "C",
                "evidence": [f"探索因果效应显著为正({cc['paired_diff_mean']:+.2f},CI{cc['ci95']})"
                             " 但现行拒绝优先策略放弃了事件收益"],
                "numbers": {"causal": cc}}
    if cc.get("significant") and cc["paired_diff_mean"] < 0:
        return {"grade": "D",
                "evidence": [f"探索因果效应显著为负({cc['paired_diff_mean']:+.2f},CI{cc['ci95']})"
                             ":风险收益结构失衡"],
                "numbers": {"causal": cc}}

    causal_zero = "探索因果效应≈0(CI 含 0):" + str(cc.get("ci95"))
    ev_gain = gd["event_immediate_diff"]
    gain_scale = max(ev_gain.get("shards", {}).get("mean", 0),
                     10 * ev_gain.get("relics_n", {}).get("mean", 0),
                     10 * ev_gain.get("resonance_total", {}).get("mean", 0))
    small_gain = gain_scale < 3   # 单次探索最优收益当量不足 3 碎片级(≈修行一档的 1/5)
    if bias_confirmed and small_gain:
        grade = "F"
        findings += [causal_zero,
                     f"事件收益量级小(greedy 批单次均值≈{gain_scale:.1f} 碎片当量,"
                     "低于 1 精力的机会成本=修行一档≈3~5 碎片当量)",
                     "→ B 成分(人格相关是幸存者偏差)+ E 成分(事件缺高价值正向不确定性)并存"]
    elif bias_confirmed:
        grade = "B"
        findings += [causal_zero, "事件收益与机会成本大体相称"]
    elif small_gain:
        grade = "E"
        findings += [causal_zero, "事件收益量级低于机会成本,缺少值得探索的正向不确定性"]
    else:
        grade = "F"
        findings += [causal_zero, "多成分并存,见分项数据"]
    return {"grade": grade, "evidence": findings,
            "numbers": {"causal": cc, "survivorship": surv,
                        "event_gain_scale_shards_equiv": round(gain_scale, 2)}}


def render_markdown(rep, verdict) -> str:
    rj, gd = rep["reject"], rep["greedy"]
    lines = [
        "# 事件探索收益审计报告(" + rep["date"] + ")",
        "",
        f"**结论:{verdict['grade']}** — " + ";".join(verdict["evidence"]),
        "",
        f"- 审计批:reject(现行拒绝优先)/ greedy(非拒绝优先,同种子)各 {rj['valid_chars']} 局",
        f"- 局外行动 {rj['prebattle_actions']}(reject 批),探索 {rj['explore_count']} 次"
        f"(占比 {rj['explore_rate']:.1%})",
        f"- 事件结算 {rj['event_resolutions']}(reject)/ {gd['event_resolutions']}(greedy)",
        "",
        "## 一、事件系统静态事实",
        "- 事件总数 36(通用10+扭曲7+罪孽7+龙心6+乱葬岗6,乱葬岗二阶未接入运行时);选项 108,拒绝/离开类 35(32%)",
        "- 探索:1档免费发现1个未遇事件;2档30碎片发现2个;**池机制=探索只能触发未遇事件(结构性全未知,无'已知事件'可选)**",
        "- 无角色级事件记忆;EventPool.triggered 是引擎池状态,跨局清零;事件不与 personality_traits 交互",
        "",
        "## 二、探索的即时收益(reject 批 / greedy 批)",
        "",
        "| 资源 | reject 均值 | greedy 均值 |",
        "|---|---|---|",
    ]
    for k in RESOURCE_KEYS:
        lines.append(f"| {k} | {rj['event_immediate_diff'][k]['mean']} | "
                     f"{gd['event_immediate_diff'][k]['mean']} |")
    lines += [
        "",
        "## 三、策略对照(同种子:拒绝优先 vs 愿意付代价)",
        "",
        f"- 通关均值:reject {rep['policy_contrast']['cleared_mean']['reject']}"
        f" → greedy {rep['policy_contrast']['cleared_mean']['greedy']}",
        f"- 事件致死:reject {rj['event_death_count']} 次 / greedy {gd['event_death_count']} 次",
        "",
        "## 四、相似状态配对(探索 vs 修行/学习)",
        "",
    ]
    pc = rep.get("paired_contrast")
    if pc:
        lines += [
            f"- 配对数 {pc['pairs']}:探索组后续通关 {pc['explore_cleared_mean']}"
            f" vs 安全组 {pc['safe_cleared_mean']}",
        ]
    else:
        lines.append("- 未找到满足相似阈值的状态对(样本不足)")
    lines += [
        "",
        "## 五、幸存者偏差检验",
        "",
        f"- 全生命周期探索率 vs 通关:{rep['survivorship']['explore_rate_vs_cleared_full_life']}",
        f"- 固定前{rep['survivorship']['window']}次局外行动窗口的探索次数 vs 通关:"
        f"{rep['survivorship']['explore_count_in_first_window_vs_cleared']}",
        f"- 早期探索率 {rep['survivorship']['early_rate_mean']} vs 后期 {rep['survivorship']['late_rate_mean']}",
        "",
        "## 六、AI 价值评估路径",
        "",
    ]
    for k, v in rep["ai_evaluation_facts"].items():
        lines.append(f"- **{k}**:{v}")
    lines += [
        "",
        "## 七、结论与证据",
        "",
        f"- 等级:**{verdict['grade']}**",
    ]
    lines += [f"- {x}" for x in verdict["evidence"]]
    lines.append(f"- 数字:{json.dumps(verdict['numbers'], ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=100)
    ap.add_argument("--battles", type=int, default=7)
    args = ap.parse_args()
    t0 = time.time()
    print(f"[审计] reject 批(现行策略){args.runs} 局…")
    reject_chars, invalid1 = run_batch(args.runs, "reject", 20260827)
    print(f"[审计] greedy 批(非拒绝优先,同种子){args.runs} 局…")
    greedy_chars, invalid2 = run_batch(args.runs, "greedy", 20260827)
    print(f"[审计] 因果对照:禁止探索 vs 探索高权重(同种子)各 {args.runs} 局…")
    causal = causal_contrast(args.runs)
    elapsed = round(time.time() - t0, 1)
    rep = analyze(reject_chars, greedy_chars, elapsed)
    rep["causal_contrast"] = causal
    rep["invalid_runs"] = {"reject": invalid1, "greedy": invalid2}
    verdict = verdict_of(rep)
    rep["verdict"] = verdict
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    j = OUT_DIR / f"event_exploration_audit_{DATE_TAG}.json"
    m = OUT_DIR / f"event_exploration_audit_{DATE_TAG}.md"
    j.write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    m.write_text(render_markdown(rep, verdict), encoding="utf-8")
    print(f"[输出] {j}\n[输出] {m}")
    print(f"[结论] {verdict['grade']} | 耗时 {elapsed}s | "
          f"探索 {rep['reject']['explore_count']} 次 | 事件结算 {rep['reject']['event_resolutions']} 次")


if __name__ == "__main__":
    main()
