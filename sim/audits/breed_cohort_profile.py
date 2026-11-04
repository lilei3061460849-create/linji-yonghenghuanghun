#!/usr/bin/env python3
"""养蛊胜者档案器：静态快照事实 + 参考死斗性格 + 循环赛战术/战绩（报告.md 表格的数据源）。

口径（2026-08-31）：
  · **不复制任何引擎公式**。面板/道纹/法术/遗物/残韵读 `data/breed_winners/*.json` 快照；
    道纹正文由 `RuleSync` 从规则正文与副本正文抽取；性格读引擎权威记录
    `state.personality_traits`；战术与战绩来自真实死斗逐局实录。
  · 死斗一律走仓库自带的 `sim/duel_diff_trace.py::traced_duel`（内含面板不变量自检），
    出牌统计只数 `[真实]` 行——预演世界的动作不计入。
  · 每局前 `random.seed(seed)`：对白渲染默认用全局 random，不重置会让 A/B 漂移
    （见 AI_EXPERIENCE「render_line 默认用全局 random」）。

用法：
    python3 sim/audits/breed_cohort_profile.py                      # 采集 + 打印 markdown 表格
    python3 sim/audits/breed_cohort_profile.py --json out.json      # 只落 JSON
    python3 sim/audits/breed_cohort_profile.py --json out.json --render-only   # 由已有 JSON 渲染
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
import collections
import contextlib
import io
import json
import os
import random
import re
import sys


import sim.duel_diff_trace as ddt          # noqa: E402
from engine.rule_sync import RuleSync      # noqa: E402

REPO = _ROOT
WINNER_DIR = ddt.WINNER_DIR
DEFAULT_JSON = os.path.join(REPO, "data", "experiments", "breed_cohort_2026-08-31.json")

_ACTION_RE = re.compile(r"'daowen_name': '([^']+)', 'x': (\d+)")
_RESONANCE_RE = re.compile(r"'resonance_type': '([^']+)'")


def daowen_texts() -> dict:
    """道纹 → (正文, 出处)。规则正文通用道纹 + 各副本专属道纹，全部来自正文事实源。"""
    out = {d["name"]: (d["description"], "规则正文")
           for d in RuleSync().extract_daowen_from_file(os.path.join(REPO, "AI_EXPERIENCE.md"))}
    for d in RuleSync().extract_dungeon_daowen():
        out.setdefault(d["name"], (d["description"], os.path.basename(d.get("source", ""))))
    return out


def run_duel(challenger: str, defender: str, seed: int):
    """跑一局死斗，返回 (判定, 挑战席出牌计数, 守擂席出牌计数, 引擎)。

    守擂席动作带 `actor_ref: enemy:0`（死斗驱动以玩家侧接口代守擂出手），据此分席。
    """
    random.seed(seed)
    captured: dict = {}
    orig = ddt.run_duel_pvp

    def spy(e, *a, **kw):
        captured["e"] = e
        return orig(e, *a, **kw)

    ddt.run_duel_pvp = spy
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            res = ddt.traced_duel(os.path.join(WINNER_DIR, challenger),
                                  os.path.join(WINNER_DIR, defender), seed)
    finally:
        ddt.run_duel_pvp = orig
    chal: collections.Counter = collections.Counter()
    defe: collections.Counter = collections.Counter()
    for line in buf.getvalue().splitlines():
        if not line.startswith("[真实] "):
            continue
        seat = defe if "'actor_ref': 'enemy:0'" in line else chal
        if line.startswith("[真实] use_daowen"):
            m = _ACTION_RE.search(line)
            if m:
                seat[f"{m.group(1)}X={m.group(2)}"] += 1
        elif line.startswith("[真实] use_resonance"):
            m = _RESONANCE_RE.search(line)
            if m:
                seat[f"残韵·{m.group(1)}"] += 1
    return res, chal, defe, captured.get("e")


def traits_of(engine, name: str) -> dict:
    """读引擎权威性格记录（死亡角色的 get_personality 会返回 None，这里读原始表）。"""
    if engine is None:
        return {}
    for per in (getattr(engine.state, "personality_traits", {}) or {}).values():
        if per.get("name") != name:
            continue
        return {dim: {"value": t["value"], "score": t["score"],
                      "confidence": t["confidence"], "n": t["evidence_count"]}
                for dim, t in (per.get("traits") or {}).items()}
    return {}


def collect() -> list[dict]:
    files = sorted(f for f in os.listdir(WINNER_DIR) if f.endswith(".json"))
    texts = daowen_texts()
    rows = []
    for fn in files:
        snap = json.load(open(os.path.join(WINNER_DIR, fn), encoding="utf-8"))
        p, origin = snap["player"], snap.get("origin") or {}
        rows.append({
            "file": fn, "build": origin.get("build"), "seed": origin.get("seed"),
            "cleared": origin.get("cleared"),
            "血限": p["blood_limit"], "快照血": p["current_hp"],
            "法限": p["mana_limit"], "快照法": p["current_mana"],
            "速限": p["speed_limit"], "快照速": p["current_speed"],
            "道纹": sorted(p["dao_wen"]),
            "道纹正文": {n: texts.get(n, ("（正文未收录）", "—"))[0] for n in sorted(p["dao_wen"])},
            "法术": [{"name": s["name"], "req": s["required_daowen"], "rank": s.get("rank")}
                     for s in p["spells"]],
            "遗物": [r["name"] for r in snap.get("relics", [])],
            "遗物效果": {r["name"]: r.get("effect", "") for r in snap.get("relics", [])},
            "法器": list(snap.get("artifacts_owned") or []),
            "残韵": dict(snap.get("resonance") or {}),
            "碎片": snap.get("shards"), "region": snap.get("current_region"),
        })

    # 参考死斗：本人坐挑战席、seed = 其养成种子 → 取该局承载的死斗名与性格
    print(f"[1/2] 参考死斗（每档 1 局，seed=其养成种子）：{len(rows)} 局", file=sys.stderr)
    for row in rows:
        opp = next(f for f in files if f != row["file"])
        res, chal, _defe, engine = run_duel(row["file"], opp, int(row["seed"]))
        name = engine.state.player.name if engine is not None else "?"
        row["参考死斗"] = {"seed": int(row["seed"]), "对手": opp, "名字": name,
                          "性格": traits_of(engine, name), "出牌": dict(chal), "判定": res}

    # 循环赛：全有序配对 × seed=1 → 战术统计与战绩
    pairs = [(a, b) for a in files for b in files if a != b]
    print(f"[2/2] 循环赛 seed=1：{len(pairs)} 组有序配对", file=sys.stderr)
    tally = {f: {"chal_plays": collections.Counter(), "defe_plays": collections.Counter(),
                 "chal_win": 0, "chal_loss": 0, "defe_win": 0, "defe_loss": 0,
                 "rounds": [], "deaths": collections.Counter()} for f in files}
    for a, b in pairs:
        res, chal, defe, _engine = run_duel(a, b, 1)
        ta, tb = tally[a], tally[b]
        ta["chal_plays"].update(chal)
        tb["defe_plays"].update(defe)
        ta["rounds"].append(res.get("rounds") or 0)
        tb["rounds"].append(res.get("rounds") or 0)
        reason = res.get("reason") or ""
        if res.get("winner") == "challenger":
            ta["chal_win"] += 1
            tb["defe_loss"] += 1
            ta["deaths"][f"守擂席时：{reason}"] += 1
        elif res.get("winner") == "defender":
            tb["defe_win"] += 1
            ta["chal_loss"] += 1
            tb["deaths"][f"挑战席时：{reason}"] += 1
        else:
            ta["deaths"][f"无判定：{reason}"] += 1
            tb["deaths"][f"无判定：{reason}"] += 1
    for row in rows:
        t = tally[row["file"]]
        row["循环赛"] = {
            "挑战": f"{t['chal_win']}胜{t['chal_loss']}负",
            "守擂": f"{t['defe_win']}胜{t['defe_loss']}负",
            "均回合": round(sum(t["rounds"]) / max(1, len(t["rounds"])), 2),
            "出牌_挑战": dict(t["chal_plays"].most_common()),
            "出牌_守擂": dict(t["defe_plays"].most_common()),
            "死因": dict(t["deaths"].most_common()),
        }
    return rows


# ------------------------------ markdown 渲染 ------------------------------

def _cohort(seed) -> str:
    return "续养(08-31)" if int(seed) >= 121 else "首批"


def _top(plays, n=3):
    items = sorted(plays.items(), key=lambda kv: -kv[1])[:n]
    return "、".join(f"{k}×{v}" for k, v in items) or "—"


def _deaths(d, n=2):
    """死因原文照显示（判词本身已含座位与是否对手击杀），只取前 n 项。"""
    items = sorted(d.items(), key=lambda kv: -kv[1])[:n]
    return "；".join(f"{k.split('：', 1)[-1]}×{v}" for k, v in items) or "—"


def render_markdown(rows: list[dict]) -> str:
    texts = daowen_texts()
    out: list[str] = []
    A = out.append

    A("### 表1 · 轮回者档案（快照事实，`data/breed_winners/*.json`）\n")
    A("| 档名 | 批次·构建·种子 | 血限(第7场末血) | 法限 | 速限 | 道纹 | 法术（依赖道纹） | 遗物 | 残韵 | 碎片 |")
    A("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        spells = "、".join(f"{s['name']}({'/'.join(s['req'])})" for s in r["法术"]) or "—"
        reso = "、".join(f"{k}{v}" for k, v in r["残韵"].items() if v) or "0"
        A(f"| {r['file'][:-5]} | {_cohort(r['seed'])}·{r['build']}·{r['seed']} "
          f"| {r['血限']}（{r['快照血']}） | {r['法限']}（{r['快照法']}） | {r['速限']}（{r['快照速']}） "
          f"| {'、'.join(r['道纹'])} | {spells} | {'、'.join(r['遗物']) or '—'} | {reso} | {r['碎片']} |")

    held = sorted({n for r in rows for n in r["道纹"]})
    A("\n### 表2 · 上表道纹的正文（`RuleSync` 从规则正文／副本正文抽取，非手写）\n")
    A("| 道纹 | 正文 | 出处 |")
    A("|---|---|---|")
    for n in held:
        text, src = texts.get(n, ("（正文未收录）", "—"))
        A(f"| {n} | {text} | {src} |")

    A("\n### 表3 · 性格（引擎 `state.personality_traits` 实测，非推断）\n")
    A("> 参考死斗 = 本人坐挑战席、seed = 其养成种子；名字由 `_assign_duelist_names`（性格反差最大配对）"
      "按 seed 定，性格再由名字哈希 seed 出来——**性格绑死斗身份、不存在快照里**，同一档换 seed 就换性格。"
      "列的是「绝对分×置信度」最高的 4 维（即 `TacticalAI._w()` 实际读的权重）。\n")
    A("| 档名 | 参考死斗 seed | 对手 | 死斗名 | 性格四维（score/置信） | 该局判定 |")
    A("|---|---|---|---|---|---|")
    for r in rows:
        ref = r["参考死斗"]
        top = sorted(ref["性格"].items(),
                     key=lambda kv: -abs(kv[1]["score"]) * kv[1]["confidence"])[:4]
        cell = "、".join(f"{t['value']}{t['score']:+.2f}/{t['confidence']:.2f}"
                        for _, t in top) or "—"
        v = ref["判定"] or {}
        A(f"| {r['file'][:-5]} | {ref['seed']} | {ref['对手'][:-5]} | {ref['名字']} | {cell} "
          f"| {v.get('winner')}（{v.get('reason')}，第{v.get('rounds')}回合） |")

    n_pairs = len(rows) * (len(rows) - 1)
    A(f"\n### 表4 · 战术与战绩（{n_pairs} 局全有序配对循环赛，seed=1，逐局 `traced_duel`）\n")
    A("> 出牌统计只数 `[真实]` 动作行（预演不计入）。**注意**：seed 固定时死斗名只由座位决定，"
      "故循环赛里所有挑战者共用同一套性格画像、所有守擂共用另一套——战术差异全部来自"
      "**构筑本身**（道纹／遗物／面板），不来自性格。\n")
    A("| 档名 | 挑战席战绩 | 守擂席战绩 | 均回合 | 挑战席最常出（前3） | 守擂席最常出（前3） | 阵亡归因（本人命零时） |")
    A("|---|---|---|---|---|---|---|")
    for r in rows:
        t = r["循环赛"]
        A(f"| {r['file'][:-5]} | {t['挑战']} | {t['守擂']} | {t['均回合']} "
          f"| {_top(t['出牌_挑战'])} | {_top(t['出牌_守擂'])} | {_deaths(t['死因'])} |")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=DEFAULT_JSON, help="逐档原始数据落盘路径")
    ap.add_argument("--render-only", action="store_true", help="不跑死斗，直接用已有 JSON 渲染")
    ap.add_argument("--no-markdown", action="store_true", help="只落 JSON，不打印表格")
    args = ap.parse_args()

    if args.render_only:
        rows = json.load(open(args.json, encoding="utf-8"))
    else:
        rows = collect()
        os.makedirs(os.path.dirname(args.json), exist_ok=True)
        json.dump(rows, open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"逐档数据 → {args.json}", file=sys.stderr)
    if not args.no_markdown:
        print(render_markdown(rows))


if __name__ == "__main__":
    main()