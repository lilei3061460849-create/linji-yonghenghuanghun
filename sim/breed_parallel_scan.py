#!/usr/bin/env python3
"""并行养蛊扫描器（2026-09-10）：当前引擎下 7 通关变稀有，单进程顺序扫描太慢。

与 sim/breed_and_duel.py::breed 相同口径（同一 _play / 同一构建集 / spend_shards=True /
xiuxing 同档），只是把种子扫描并行化：每个 (build, seed) 由独立进程跑，cleared==7
的胜者快照照旧落 data/breed_winners/{build}_{seed}.json。
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.build_learner import _play
from sim.breed_and_duel import BUILDS, BREED_DIR

SEED_START = int(sys.argv[1]) if len(sys.argv) > 1 else 1
SEED_END = int(sys.argv[2]) if len(sys.argv) > 2 else 3000


def _breed_one(build_name: str, cfg: dict, seed: int) -> dict | None:
    seal_path = tempfile.mktemp(suffix=".json")
    db = tempfile.mktemp(suffix=".db")
    r = _play(cfg["starter"], cfg["learn"], "扭曲都市", seed=seed, battles=7,
              attrs=cfg["attrs"], spend_shards=True, xiuxing=cfg.get("xiuxing"),
              lab_paths={"sealed_path": seal_path, "db_path": db,
                         "death_book_path": tempfile.mktemp(suffix=".md")})
    cleared = r.get("cleared") or 0
    if cleared < 7:
        return None
    with open(seal_path, encoding="utf-8") as f:
        data = json.load(f)
    snap = None
    for tier, queue in (data.get("candidates") or {}).items():
        for s in (queue or []):
            if isinstance(s, dict) and s.get("player"):
                snap = s
                break
        if snap:
            break
    if snap is None:
        return None
    os.makedirs(BREED_DIR, exist_ok=True)
    snap["origin"] = {"build": build_name, "seed": seed, "cleared": cleared}
    out = os.path.join(BREED_DIR, f"{build_name}_{seed}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    p = snap.get("player") or {}
    return {"build": build_name, "seed": seed, "file": out, "name": p.get("name"),
            "血": p.get("blood_limit"), "法": p.get("mana_limit"), "速": p.get("speed_limit"),
            "道纹": sorted(p.get("dao_wen", {})),
            "法术": [s.get("name") for s in p.get("spells", [])],
            "遗物": [r0.get("name") for r0 in snap.get("relics", [])],
            "碎片": snap.get("shards")}


def _worker(job):
    build, seed = job
    try:
        return _breed_one(build, BUILDS[build], seed)
    except Exception as exc:  # 引擎异常=无效局，照旧跳过
        return {"build": build, "seed": seed, "error": str(exc)[:100]}


def main() -> None:
    jobs = [(b, s) for b in BUILDS for s in range(SEED_START, SEED_END + 1)]
    t0 = time.time()
    wins: list[dict] = []
    best = {}
    done = 0
    with mp.Pool(processes=os.cpu_count() or 4) as pool:
        for res in pool.imap_unordered(_worker, jobs, chunksize=4):
            done += 1
            b = res["build"] if res else "?"
            if res and res.get("file"):
                wins.append(res)
                print(f"  ✅ {res['build']} seed={res['seed']} → {res['name']} "
                      f"血{res['血']} 法{res['法']} 速{res['速']} 道纹={res['道纹']} "
                      f"法术={res['法术']} 遗物={res['遗物']} 碎片={res['碎片']}", flush=True)
            if done % 500 == 0:
                print(f"[{done}/{len(jobs)}] 胜者={len(wins)} "
                      f"耗时={time.time()-t0:.0f}s", flush=True)
            if len(wins) >= 4:
                print("已集齐 4 个胜者，提前收工", flush=True)
                pool.terminate()
                break
    print(f"\n养蛊完成：{len(wins)} 个通关胜者，扫描 {done} 局，耗时 {time.time()-t0:.0f}s", flush=True)
    for w in wins:
        print(f"  · {w['file']}", flush=True)


if __name__ == "__main__":
    main()
