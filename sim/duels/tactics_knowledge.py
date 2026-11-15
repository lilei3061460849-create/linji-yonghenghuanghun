#!/usr/bin/env python3
"""战术知识库工具（战报驱动：根据对局结果持续更新优化战术，淘汰过时打法）。

用户要求：决策受知识库影响 → 给知识库加规则，根据战报不断更新优化战术并淘汰过时战术打法。

本工具把"战报/测试结果"沉淀进 data/build_knowledge.json 的 tactics 区：
  - record：把一局结果记入指定战术（win/loss）
  - import_results：从测试结果 JSON（如 handplay_dungeon_results.json）批量导入
  - retire：对局数达标且胜率低于阈值 → 标记 retired（过时打法，不再推荐）
  - report：展示当前生效战术与已淘汰战术
  - recommend：按胜率返回当前推荐战术（供 agent / 测试脚本查询决策）

用法：
    python3 sim/duels/tactics_knowledge.py --report
    python3 sim/duels/tactics_knowledge.py --record 花碎片提升战力 --won
    python3 sim/duels/tactics_knowledge.py --import-results data/handplay_dungeon_results.json --tactic 护卫命令流
    python3 sim/duels/tactics_knowledge.py --retire
    python3 sim/duels/tactics_knowledge.py --recommend
"""

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
import json
import os
import sys


from sim.build_learner import (KNOWLEDGE, load, save, retire_stale_tactics,
                               report_tactics, RETIRE_MIN_N, RETIRE_WINRATE)

# 战报初始战术（用户裁定 + 实测结论；首次运行写入知识库）
SEED_TACTICS = {
    "花碎片提升战力": {
        "status": "active", "n": 0, "wins": 0,
        "rule": "七场局外把碎片花成战力(修行/附煞/共鸣)。不花碎片怎么快速提高战力？实测：旧池(法40-46)挑战spend池(法60-68)守擂 0:6 完败。"},
    "护卫命令流": {
        "status": "active", "n": 0, "wins": 0,
        "rule": "command_ally 护卫X 无消耗强制挡伤，怪物打轮回者的伤害转给盟友。实测：乱葬岗1-6场83~100%。"},
    "封印流多怪波": {
        "status": "active", "n": 0, "wins": 0,
        "rule": "封印X=10法/只直接移出，4怪墙唯一稳定解法(18/18)；代价零碎片收入(打赢但穷，用户认可为合理下限)。"},
    "死斗PvP规则": {
        "status": "active", "n": 0, "wins": 0,
        "rule": "死斗守擂方走玩家侧接口(法力制/action_count当前固定2次/自由控X)，勿当怪物处理。"},
    "不花碎片": {
        "status": "retired", "n": 6, "wins": 0,
        "rule": "已淘汰：旧池(法40-46,碎片留250)挑战spend池(法60-68)守擂 0:6 完败——碎片必须花。"},
    "守擂当怪物": {
        "status": "retired", "n": 12, "wins": 0,
        "rule": "已淘汰：死斗守擂走怪物阶段无PvP规则(无法力/道纹X=0全废/白板回合)，镜像12/12守擂必胜，是机制bug。"},
}


def _ensure_seed(k: dict) -> None:
    tactics = k.setdefault("tactics", {})
    changed = False
    for name, t in SEED_TACTICS.items():
        if name not in tactics:
            tactics[name] = dict(t)
            changed = True
    return changed


def record(k: dict, tactic: str, won: bool) -> None:
    _ensure_seed(k)
    t = k["tactics"].setdefault(tactic, {"status": "active", "n": 0, "wins": 0, "rule": ""})
    if t.get("status") == "retired":
        print(f"⚠ {tactic} 已淘汰，不再累计（保留淘汰记录）")
        return
    t["n"] = t.get("n", 0) + 1
    if won:
        t["wins"] = t.get("wins", 0) + 1
    print(f"✓ 记录：{tactic} {'胜' if won else '负'}（{t['wins']}/{t['n']}）")


def import_results(k: dict, path: str, tactic: str, win_key: str = "cleared") -> int:
    """从测试结果 JSON 批量导入。JSON 为 [{...}, ...]，每条的胜负由 win_key 判定。
    返回导入条数。"""
    _ensure_seed(k)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("results 必须是列表")
    n = 0
    for entry in data:
        if win_key == "cleared":
            won = bool(entry.get("cleared", 0))  # 通关≥1场算胜
        else:
            won = bool(entry.get(win_key, False))
        record(k, tactic, won)
        n += 1
    return n


def recommend(k: dict) -> list:
    """按胜率返回推荐战术（仅 active，按胜率降序）。"""
    _ensure_seed(k)
    out = []
    for name, t in k["tactics"].items():
        if t.get("status") != "active":
            continue
        n = t.get("n", 0)
        rate = t.get("wins", 0) / n if n else 0.0
        out.append((name, rate, n))
    out.sort(key=lambda x: -x[1])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", metavar="战术名", help="记录一局：--won 或 --loss")
    ap.add_argument("--won", action="store_true", help="--record 的胜负")
    ap.add_argument("--loss", action="store_true", help="--record 的胜负")
    ap.add_argument("--import-results", metavar="JSON", help="从测试结果批量导入")
    ap.add_argument("--tactic", metavar="战术名", help="--import-results 对应的战术")
    ap.add_argument("--retire", action="store_true", help="执行过时战术淘汰")
    ap.add_argument("--report", action="store_true", help="展示战术库")
    ap.add_argument("--recommend", action="store_true", help="推荐当前战术")
    a = ap.parse_args()

    k = load()
    _ensure_seed(k)

    if a.record:
        if a.won == a.loss:
            print("必须且只能指定 --won 或 --loss")
            return
        record(k, a.record, won=a.won)
        save(k)

    if a.import_results:
        if not a.tactic:
            print("--import-results 需要 --tactic 指定战术名")
            return
        n = import_results(k, a.import_results, a.tactic)
        print(f"已导入 {n} 局 → {a.tactic}")
        save(k)

    if a.retire:
        retired = retire_stale_tactics(k)
        if retired:
            print("本轮淘汰：", retired)
        else:
            print("无新淘汰（数据不足或均达标）")
        save(k)

    if a.report:
        report_tactics(k)

    if a.recommend:
        recs = recommend(k)
        print("\n【当前推荐战术】（按胜率降序）")
        for name, rate, n in recs:
            print(f"  {name:<12} 胜率{rate:.0%}（{n}局）")
        if not recs:
            print("  （暂无 active 战术）")

    if not (a.record or a.import_results or a.retire or a.report or a.recommend):
        ap.print_help()


if __name__ == "__main__":
    main()