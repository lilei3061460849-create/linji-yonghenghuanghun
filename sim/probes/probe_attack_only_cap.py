#!/usr/bin/env python3
"""实验探针（2026-09-10）：一池制法力 + 攻次=速/攻力=法 的新经济下，普攻流上限在哪里。

只动 sim 层（自定义 ai_cls + 加点扫描），不碰引擎规则。结论供 DM 裁定用。
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


import collections
import itertools
import json
import os
import sys
import tempfile


from engine.ai_tactics import TacticalAI
from sim.build_learner import _play


class AttackOnlyAI(TacticalAI):
    """每回合把全部出手都花在普攻上（法力一池制下普攻不耗法、伤害=速×法/击）。"""

    def take_turn(self) -> list[dict]:
        results = []
        me = self.player
        if me is None or not me.is_alive:
            return results
        for _ in range(2):                      # 轮回者出手次数固定2（DM裁定2026-09-10）
            if not me.is_alive or not self.alive_enemies():
                break
            prep = self.engine.execute_action("prepare_attack", {"actor_ref": "player:0"})
            if not prep.get("success"):
                break
            res = prep.get("result") or {}
            opts = res.get("target_options") or []
            if not opts:
                break
            target = opts[0]["ref"]
            hits = [{"target_ref": target, "dodge": False, "blood_shadow": False,
                     "spell_choices": self._decline_spell_choices(opts[0])}
                    for _ in range(res.get("hit_count", 0))]
            r = self.engine.execute_action("resolve_attack", {"token": res.get("token"), "hits": hits})
            if r.get("success"):
                results.append(r)
        return results


def run_batch(bp, sp, mp, seeds=10, starter="杀伐"):
    dist = collections.Counter()
    attrs = {"blood_points": bp, "speed_points": sp, "mana_points": mp}
    for seed in range(1, seeds + 1):
        r = _play(starter, ["庇护", "再生"], "扭曲都市", seed=seed, battles=7,
                  attrs=attrs, spend_shards=True,
                  xiuxing={"tier3": {"speed_points": 0, "mana_points": 2},
                           "tier2": {"speed_points": 0, "mana_points": 2}},
                  ai_cls=AttackOnlyAI,
                  lab_paths={"sealed_path": tempfile.mktemp(suffix=".json"),
                             "db_path": tempfile.mktemp(suffix=".db"),
                             "death_book_path": tempfile.mktemp(suffix=".md")})
        pm = r.get("pm")
        if r.get("invalid"):
            dist["invalid"] += 1
        else:
            dist[f"c{r.get('cleared')}"] += 1
    return dist


def main() -> None:
    grid = []
    for bp in (1, 3, 5, 7, 9, 13):
        for sp in (0, 4, 8, 12, 16, 20):
            for mp in (0, 4, 8, 12, 16, 20):
                if bp + sp + mp == 25 and sp % 2 == 0 and mp % 2 == 0:
                    grid.append((bp, sp, mp))
    print(f"满预算加点组合 {len(grid)} 个 × 10 种子", flush=True)
    best = []
    for bp, sp, mp in grid:
        d = run_batch(bp, sp, mp)
        top = max((int(k[1:]) for k in d if k.startswith("c")), default=0)
        best.append((top, bp, sp, mp, dict(d)))
        print(f"  血{bp}速{sp}法{mp}: {dict(d)}", flush=True)
    best.sort(reverse=True)
    print("\nTOP5:")
    for top, bp, sp, mp, d in best[:5]:
        print(f"  血{bp}速{sp}法{mp} 最高cleared={top} {d}")
    print("\n全局最高 cleared =", best[0][0] if best else None)


if __name__ == "__main__":
    main()