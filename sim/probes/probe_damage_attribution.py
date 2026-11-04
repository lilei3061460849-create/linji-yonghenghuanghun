#!/usr/bin/env python3
"""探针：轮回者实际输出里，普攻 / 道纹 / 法术 / 遗物 各占多少，以及道纹使用分布。

背景（2026-09-12 用户提问）：「目前的战斗是不是大部分要靠普攻输出，道纹用的少？」
只在 sim 层包一层记账，不改引擎规则与结算。

用法：
    python3 sim/probes/probe_damage_attribution.py [局数]
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
import contextlib
import io
import os
import sys
import tempfile


from engine.api import GameEngine
from engine.combat import CombatEngine
from sim.build_learner import _play

DAMAGE = collections.Counter()   # (source_type, source) -> 对怪物造成的实际伤害
ACTION = collections.Counter()   # 动作类型 -> 次数
DAOWEN = collections.Counter()   # 道纹名 -> 主动发动次数
CLEARED = collections.Counter()

_orig_damage = CombatEngine._apply_hostile_damage
_orig_action = GameEngine.execute_action


def _patched_damage(self, target, amount, damage_type="普通", source=None, ctx=None):
    detail = _orig_damage(self, target, amount, damage_type, source, ctx)
    try:
        actual = detail.get("actual_damage", 0) or 0
        if actual > 0 and getattr(target, "entity_type", "") == "怪物":
            c = detail.get("ctx") or {}
            DAMAGE[(c.get("source_type") or "legacy",
                    c.get("source") or getattr(source, "name", "?"))] += actual
    except Exception:
        pass
    return detail


def _patched_action(self, action_type, params=None):
    result = _orig_action(self, action_type, params)
    try:
        if result.get("success"):
            if action_type == "resolve_attack":
                ACTION["普攻"] += 1
            elif action_type == "use_daowen":
                ACTION["道纹"] += 1
                DAOWEN[(params or {}).get("daowen_name", "?")] += 1
            elif action_type in ("define_spell", "cast"):
                ACTION["法术"] += 1
            elif action_type == "use_resonance":
                ACTION["残韵"] += 1
    except Exception:
        pass
    return result


def run(rounds: int = 12) -> None:
    CombatEngine._apply_hostile_damage = _patched_damage
    GameEngine.execute_action = _patched_action
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for seed in range(1, rounds + 1):
            try:
                r = _play("杀伐", ["庇护", "再生"], "扭曲都市", seed=seed, battles=7,
                          spend_shards=True,
                          lab_paths={"sealed_path": tempfile.mktemp(suffix=".json"),
                                     "db_path": tempfile.mktemp(suffix=".db"),
                                     "death_book_path": tempfile.mktemp(suffix=".md")})
                CLEARED["invalid" if r.get("invalid") else r.get("cleared", 0)] += 1
            except Exception as exc:      # 单局异常不该毁掉整批统计
                CLEARED[f"err:{type(exc).__name__}"] += 1


def report() -> None:
    total = sum(DAMAGE.values()) or 1
    by_type = collections.Counter()
    for (stype, _src), v in DAMAGE.items():
        by_type[stype] += v

    print(f"对怪物造成的总伤害 = {sum(DAMAGE.values())}")
    print("\n== 伤害按来源类型 ==")
    for k, v in by_type.most_common():
        print(f"  {k:12} {v:8}  {v / total * 100:5.1f}%")

    print("\n== 伤害按具体来源 TOP10 ==")
    for (stype, src), v in DAMAGE.most_common(10):
        print(f"  {stype:10} {src:14} {v:8}  {v / total * 100:5.1f}%")

    act_total = sum(ACTION.values()) or 1
    print("\n== 出手动作分布 ==")
    for k, v in ACTION.most_common():
        print(f"  {k:6} {v:7}  {v / act_total * 100:5.1f}%")

    dw_total = sum(DAOWEN.values()) or 1
    print(f"\n== 主动发动的道纹 TOP12（共{dw_total}次）==")
    for k, v in DAOWEN.most_common(12):
        print(f"  {k:6} {v:7}  {v / dw_total * 100:5.1f}%")

    nums = [k for k in CLEARED if isinstance(k, int)]
    if nums:
        avg = sum(k * CLEARED[k] for k in nums) / max(1, sum(CLEARED[k] for k in nums))
        print(f"\n== 通关 ==\n  分布 {dict(sorted((k, CLEARED[k]) for k in nums))}"
              f"\n  其他 { {k: v for k, v in CLEARED.items() if not isinstance(k, int)} }"
              f"\n  平均通关场数 {avg:.2f}")


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 12)
    report()