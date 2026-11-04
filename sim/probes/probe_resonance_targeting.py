"""探针：残韵去向 + 各副本通关率基线/对照。

用法：python3 sim/probes/probe_resonance_targeting.py [每区seed数]
输出：各副本通关率、最终道纹分布、残韵转化得到的道纹 TOP。
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

import sys, random, collections
from sim import build_learner as bl

def run(n=12):
    regions = ["扭曲都市", "罪孽都市", "龙心谷", "乱葬岗"]
    rows = []
    allgot = collections.Counter()
    for reg in regions:
        cleared = won = games = 0
        got = collections.Counter()
        for seed in range(900, 900 + n):
            r = bl.play("杀伐", ["再生", "庇护"], reg, seed=seed, rng=random.Random(seed))
            if bl._skip_resource_timeout(r, context=__name__):
                continue
            if r.get("invalid"):
                continue
            games += 1
            cleared += r.get("cleared", 0)
            won += 1 if r.get("won") else 0
            for d in (r.get("final_daowen") or []):
                got[d] += 1
                allgot[d] += 1
        rows.append((reg, games, cleared, won, got))
    print(f"{'副本':<8}{'局数':>5}{'累计通关':>9}{'通关/局':>9}{'胜':>4}")
    for reg, games, cleared, won, got in rows:
        avg = cleared / games if games else 0
        print(f"{reg:<8}{games:>5}{cleared:>9}{avg:>9.2f}{won:>4}")
    print("\n各副本专属道纹获取情况（残韵唯一入口）：")
    from engine.gamedata import REGION_EXCLUSIVE_DAOWEN as REX
    for reg, games, cleared, won, got in rows:
        ex = {d: c for d, c in got.items() if d in REX.get(reg, set())}
        print(f"  {reg}: {ex if ex else '（无）'}")
    return rows

if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 12)