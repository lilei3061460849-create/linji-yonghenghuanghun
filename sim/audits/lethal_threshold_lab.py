#!/usr/bin/env python3
"""致死特殊事件阈值对照实验。

在真实 PvP 引擎中对同一批随机种子重复运行当前阈值与候选阈值。
所有覆盖仅存在于当前 Python 进程，不改 TOML、规则正文或生产默认值。
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from contextlib import contextmanager
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import engine.combat as combat_module
import sim.duel_pvp as duel_pvp
from engine.combat import CombatEngine
from engine.models import Entity
from sim.audits.random_pvp_lab import run_one


PROFILES = {
    "baseline": {"cancer": 2.0, "mediocrity": 5, "debt": 10},
    "cancer_3x": {"cancer": 3.0, "mediocrity": 5, "debt": 10},
    "cancer_4x": {"cancer": 4.0, "mediocrity": 5, "debt": 10},
    "mediocrity_7": {"cancer": 2.0, "mediocrity": 7, "debt": 10},
    "mediocrity_8": {"cancer": 2.0, "mediocrity": 8, "debt": 10},
    "debt_20": {"cancer": 2.0, "mediocrity": 5, "debt": 20},
    "debt_30": {"cancer": 2.0, "mediocrity": 5, "debt": 30},
}

SPECIAL_CATEGORIES = {
    "cancer_death", "sculpture_loss", "mediocrity_death",
    "debt_death", "lost_death", "mutation_collapse",
}


@contextmanager
def apply_profile(profile_name: str):
    """临时覆盖引擎阈值，并确保凡庸规则与模拟器死锁保护同步。"""
    values = PROFILES[profile_name]
    originals = {
        "entity_cancer": Entity.CANCER_HEAL_MULTIPLIER,
        "entity_mediocrity": Entity.MEDIOCRITY_ROUNDS,
        "combat_mediocrity": combat_module.MEDIOCRITY_ROUNDS,
        "deadlock_min": duel_pvp.DEADLOCK_MIN_ROUNDS,
        "combat_cancer": CombatEngine.PROLIFERATION_THRESHOLD,
        "combat_cancer_alias": CombatEngine.CANCER_THRESHOLD,
        "combat_debt": CombatEngine.DEBT_THRESHOLD,
    }
    try:
        Entity.CANCER_HEAL_MULTIPLIER = values["cancer"]
        Entity.MEDIOCRITY_ROUNDS = values["mediocrity"]
        combat_module.MEDIOCRITY_ROUNDS = values["mediocrity"]
        duel_pvp.DEADLOCK_MIN_ROUNDS = values["mediocrity"] + 1
        CombatEngine.PROLIFERATION_THRESHOLD = values["cancer"]
        CombatEngine.CANCER_THRESHOLD = values["cancer"]
        CombatEngine.DEBT_THRESHOLD = values["debt"]
        yield
    finally:
        Entity.CANCER_HEAL_MULTIPLIER = originals["entity_cancer"]
        Entity.MEDIOCRITY_ROUNDS = originals["entity_mediocrity"]
        combat_module.MEDIOCRITY_ROUNDS = originals["combat_mediocrity"]
        duel_pvp.DEADLOCK_MIN_ROUNDS = originals["deadlock_min"]
        CombatEngine.PROLIFERATION_THRESHOLD = originals["combat_cancer"]
        CombatEngine.CANCER_THRESHOLD = originals["combat_cancer_alias"]
        CombatEngine.DEBT_THRESHOLD = originals["combat_debt"]


def classify(row: dict) -> str:
    if row.get("error") and "duel" not in row:
        return "simulation_error"
    duel = row.get("duel", {})
    if duel.get("error"):
        return "simulation_error"
    reason = str(duel.get("reason", ""))
    if "癌变" in reason:
        return "cancer_death"
    if "化雕塑" in reason or "雕塑" in reason:
        return "sculpture_loss"
    if "凡庸" in reason:
        return "mediocrity_death"
    if "还债" in reason:
        return "debt_death"
    if "迷失" in reason:
        return "lost_death"
    if "崩解" in reason:
        return "mutation_collapse"
    if "自伤命零" in reason or "自付" in reason:
        return "self_inflicted_death"
    if "击杀" in reason or "攻击致死" in reason:
        return "direct_kill"
    if duel.get("unresolved") or duel.get("outcome_status") == "round_limit":
        return "round_limit"
    if duel.get("winner") in {"challenger", "defender"}:
        return "other_elimination"
    return "unresolved"


def summarize(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    rounds = []
    actions = []
    events = []
    for row in rows:
        category = row["category"]
        counts[category] = counts.get(category, 0) + 1
        duel = row.get("duel", {})
        if isinstance(duel.get("rounds"), (int, float)):
            rounds.append(duel["rounds"])
        if isinstance(row.get("action_history"), list):
            actions.append(len(row["action_history"]))
        if isinstance(row.get("combat_event_count"), int):
            events.append(row["combat_event_count"])
    completed = len(rows) - counts.get("simulation_error", 0)
    decisive = sum(v for k, v in counts.items()
                   if k not in {"simulation_error", "round_limit", "unresolved"})
    return {
        "runs": len(rows),
        "completed": completed,
        "decisive": decisive,
        "outcome_counts": counts,
        "special_deaths": {k: counts.get(k, 0) for k in sorted(SPECIAL_CATEGORIES)},
        "mean_rounds": round(statistics.mean(rounds), 3) if rounds else None,
        "median_rounds": statistics.median(rounds) if rounds else None,
        "mean_actions": round(statistics.mean(actions), 3) if actions else None,
        "mean_combat_events": round(statistics.mean(events), 3) if events else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="致死特殊事件阈值真实 PvP 对照实验")
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20261118)
    parser.add_argument("--max-rounds", type=int, default=40)
    parser.add_argument("--max-wall-seconds", type=float, default=12.0)
    parser.add_argument("--profiles", nargs="+", choices=sorted(PROFILES), default=list(PROFILES))
    parser.add_argument("--output-dir", default="artifacts/lethal-threshold-lab")
    args = parser.parse_args()
    if not 1 <= args.runs <= 100:
        raise SystemExit("--runs 必须在1至100之间")
    if "baseline" not in args.profiles:
        raise SystemExit("对照实验必须包含 baseline")
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    all_rows: list[dict] = []
    by_profile: dict[str, list[dict]] = {}
    for profile_name in args.profiles:
        profile_rows = []
        for offset in range(args.runs):
            seed = args.seed + offset
            try:
                with apply_profile(profile_name):
                    result = run_one(seed, args.max_rounds, args.max_wall_seconds)
                row = {
                    "profile": profile_name,
                    "seed": seed,
                    "thresholds": dict(PROFILES[profile_name]),
                    "category": "",
                    **result,
                }
                row["category"] = classify(row)
            except Exception as exc:
                row = {
                    "profile": profile_name,
                    "seed": seed,
                    "thresholds": dict(PROFILES[profile_name]),
                    "category": "simulation_error",
                    "error": True,
                    "exception": repr(exc),
                }
            profile_rows.append(row)
            all_rows.append(row)
            print(f"[THRESHOLD] profile={profile_name} seed={seed} "
                  f"category={row['category']} rounds={row.get('duel', {}).get('rounds')}")
        by_profile[profile_name] = profile_rows

    summaries = {name: summarize(rows) for name, rows in by_profile.items()}
    paired = {}
    baseline_by_seed = {r["seed"]: r for r in by_profile["baseline"]}
    for name, rows in by_profile.items():
        if name == "baseline":
            continue
        compared = []
        for candidate in rows:
            base = baseline_by_seed.get(candidate["seed"])
            if not base:
                continue
            b_rounds = base.get("duel", {}).get("rounds")
            c_rounds = candidate.get("duel", {}).get("rounds")
            if isinstance(b_rounds, (int, float)) and isinstance(c_rounds, (int, float)):
                compared.append({
                    "seed": candidate["seed"],
                    "baseline_category": base["category"],
                    "candidate_category": candidate["category"],
                    "round_delta": c_rounds - b_rounds,
                    "builds_identical": (
                        base.get("challenger") == candidate.get("challenger")
                        and base.get("defender") == candidate.get("defender")
                    ),
                })
        paired[name] = {
            "paired_samples_with_rounds": len(compared),
            "mean_round_delta_vs_baseline": (
                round(statistics.mean(x["round_delta"] for x in compared), 3)
                if compared else None
            ),
            "identical_build_pairs": sum(x["builds_identical"] for x in compared),
            "category_changes": sum(
                x["baseline_category"] != x["candidate_category"] for x in compared
            ),
            "samples": compared,
        }

    report = {
        "experiment": "lethal_special_event_thresholds",
        "seed_start": args.seed,
        "runs_per_profile": args.runs,
        "profiles": {name: PROFILES[name] for name in args.profiles},
        "summaries": summaries,
        "paired_vs_baseline": paired,
        "results": all_rows,
        "limitations": [
            "同一 seed 在各阈值组重复运行；若构筑一致则构成配对样本。",
            "雕塑是攻次与攻力同时归零的条件，不是可直接调高的数值阈值。",
            "迷失阈值保持50，本轮只统计是否出现，不做阈值候选。",
            "PvP 对局中未自然出现的机制不能据此证明其在开放副本中不常见。",
        ],
    }
    (output / "lethal_threshold_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output / "lethal_threshold_samples.csv").open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["profile", "seed", "category", "rounds", "winner", "reason",
                  "combat_event_count", "action_count", "error"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in all_rows:
            duel = row.get("duel", {})
            writer.writerow({
                "profile": row["profile"], "seed": row["seed"],
                "category": row["category"], "rounds": duel.get("rounds"),
                "winner": duel.get("winner"), "reason": duel.get("reason"),
                "combat_event_count": row.get("combat_event_count"),
                "action_count": len(row.get("action_history", [])),
                "error": row.get("exception") or duel.get("error", False),
            })

    lines = ["# 致死特殊事件阈值对照实验", "",
             f"- 每组对局数：{args.runs}", f"- 起始 seed：{args.seed}",
             "- 所有组使用相同 seed 序列；候选值只在模拟进程内覆盖，不修改正式规则。", "",
             "## 汇总", "",
             "| 组别 | 完成 | 明确结局 | 癌变 | 凡庸 | 雕塑 | 还债 | 迷失 | 直接击杀 | 平均回合 | 平均行动 | 异常 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, summary in summaries.items():
        c = summary["outcome_counts"]
        lines.append(
            f"| {name} | {summary['completed']} | {summary['decisive']} | "
            f"{c.get('cancer_death', 0)} | {c.get('mediocrity_death', 0)} | "
            f"{c.get('sculpture_loss', 0)} | {c.get('debt_death', 0)} | "
            f"{c.get('lost_death', 0)} | {c.get('direct_kill', 0)} | "
            f"{summary['mean_rounds']} | {summary['mean_actions']} | "
            f"{c.get('simulation_error', 0)} |")
    lines += ["", "## 相对基线的配对变化", ""]
    for name, item in paired.items():
        lines.append(
            f"- **{name}**：配对样本 {item['paired_samples_with_rounds']}；"
            f"平均回合差 {item['mean_round_delta_vs_baseline']}；"
            f"构筑完全相同 {item['identical_build_pairs']}；"
            f"结局分类变化 {item['category_changes']}。")
    lines += ["", "## 解读限制", "",
              "- 零次触发只表示这批 PvP 样本没有触发，不能证明该机制在开放副本不常见。",
              "- 雕塑不是倍率/回合数阈值，本轮只记录触发数，不虚构一个可调参数。",
              "- 迷失维持50层，本轮只统计触发情况。",
              "- 若某组异常率偏高，先排除模拟可靠性问题，不把异常局当作平衡结果。", ""]
    (output / "lethal_threshold_report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:30]))


if __name__ == "__main__":
    main()
