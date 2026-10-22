"""行为轨迹探针：固定种子跑 3 场完整 PvE，把结果规范化后打成一个 SHA256。

用途只有验收：性能优化**不该改变对外可观测行为**，本脚本就是把这句话变成
一个可比的指纹。同一棵树、同一组用例必然得到同一串指纹（跑两次即可自证）。

    cd <仓库根>                       # 必须从被测树的根目录运行
    .venv/bin/python sim/behavior_trace.py
    .venv/bin/python sim/behavior_trace.py --json /tmp/trace.json

**扫参门禁**（上一轮报告「下一步建议 2」的落地，默认关闭）：

    .venv/bin/python sim/behavior_trace.py --cases 60

  内置三组「起手道纹/学习表/地区」轮流用、种子取 1..N，打印 `SWEEP_SHA256`
  与 `SWEEP_CASES … cleared/won/invalid` 统计。用途是发版前跑一次、与上一版对比：
  指纹不同就说明 AI 决策链在实盘上漂移了（单元测试覆盖不到这一层）。
  与 3 例验收指纹分开打印（`TRACE_SHA256`），避免两个口径混用。
  用例表的契约由 `tests/test_behavior_trace_sweep.py` 锁死（种子/轮换/前缀稳定）。

两个**诊断开关**（默认关闭，只用于定位"为什么两棵树的指纹不同"）：

  --force-preview-transaction
      把预演内部的 `_execute_action_core` 强制升级回 transaction=True，
      即 2026-09-19 性能提交 fe738b8 之前的口径（预演里再建一份事务快照）。
      若开启后指纹变化，说明预演语义真的影响实盘；若不变化，说明那次提交
      本身是行为中性的。

  --patch-runtime-leak
      在预演进出沙盒时补上 combat 运行态（`_monster_activated` /
      `_monster_daowen_round_used` / `_resonance_rewrites` / …）的保存与恢复。
      这些字典按 `id(entity)` 建索引，而沙盒实体是深拷贝副本，沙盒写入若不
      恢复就会在真实引擎里留下"老键"，被后续沙盒复用地址后读走。
      在**已修复**的树上这是一个 no-op（指纹必须与不开开关时一致，可作自检）；
      在 fe738b8~a2e2161 之前（含基线 adfd9d8）的树上，它能把"泄漏造成的
      轨迹偏移"单独摘出来。

验收口径（2026-09-19，结果见 报告.md 第四节）：
    基线 adfd9d8                          → 98f2bfe2…
    adfd9d8 + --patch-runtime-leak        → 6aa26df8…   ← 与优化树逐字节相同
    优化树 HEAD                            → 6aa26df8…
    优化树 HEAD --force-preview-transaction → 6aa26df8…   ← 事务层无观测差异
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from pathlib import Path

# 从被测树的根目录运行：先钉死 import 根，避免"在 A 树跑出 B 树结果"。
ROOT = Path(__file__).resolve().parents[1]
CWD = Path.cwd().resolve()
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(CWD))

def _sweep_cases(count: int) -> list:
    """扫参用例：内置 3 组「起手道纹/学习表/地区」轮流用，种子取 1..count。

    这是上一轮报告「下一步建议 2」的落地：把 3 个固定种子扩成 N 个种子的
    **发版前行为回归门禁**（同一种子在任何树上都必须给出同一个 SWEEP_SHA256）。
    """
    out = []
    for index in range(count):
        starter, learn, region, _seed = CASES[index % len(CASES)]
        out.append((starter, learn, region, index + 1))
    return out


CASES = [
    ("杀伐", ["再生", "庇护", "束缚", "贯穿", "固执"], "扭曲都市", 2),
    ("坠落", ["杀伐", "连击", "再生", "庇护", "透支"], "罪孽都市", 3),
    ("封印", ["杀伐", "再生", "庇护", "贯穿", "固执"], "龙心谷", 1),
]

# 预演沙盒要换回的 combat 运行态（与 engine/ai_preview.py 同一份名单）
# 2026-10-13：【三相残韵盘】删除，_sanxiang_consumed 移出运行态名单。
RUNTIME_ATTRS = ("_monster_activated", "_monster_daowen_round_used",
                 "_resonance_rewrites",
                 "_split_clones_spawned", "_monster_evolved",
                 "_effect_chain_depth")


def _copy_runtime_value(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(v, set):
                out[k] = set(v)
            elif isinstance(v, dict):
                out[k] = dict(v)
            elif isinstance(v, (tuple, list)):
                out[k] = type(v)(set(x) if isinstance(x, set) else x for x in v)
            else:
                out[k] = v
        return out
    if isinstance(value, set):
        return set(value)
    return value


def _apply_force_preview_transaction() -> None:
    from engine.api import GameEngine
    original = GameEngine._execute_action_core

    def core(self, action_type, params=None, *, transaction=False):
        return original(self, action_type, params, transaction=True)

    GameEngine._execute_action_core = core


def _apply_runtime_leak_patch() -> None:
    from engine.ai_preview import ActionPreview
    original = ActionPreview.preview_sequence

    def patched(self, steps):
        combat = self.engine.combat
        saved = {k: _copy_runtime_value(getattr(combat, k, None))
                 for k in RUNTIME_ATTRS}
        try:
            return original(self, steps)
        finally:
            for k, v in saved.items():
                setattr(combat, k, v)

    ActionPreview.preview_sequence = patched


def _norm(value):
    """只留"规则事实"：去掉身份标识（id/令牌）与对象引用细节。"""
    if isinstance(value, dict):
        return {str(k): _norm(v) for k, v in sorted(value.items(),
                                                    key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


def main() -> int:
    parser = argparse.ArgumentParser(description="固定种子行为轨迹指纹")
    parser.add_argument("--force-preview-transaction", action="store_true",
                        help="预演内部强制 transaction=True（fe738b8 之前的口径）")
    parser.add_argument("--patch-runtime-leak", action="store_true",
                        help="补上预演对 combat 运行态的保存/恢复")
    parser.add_argument("--json", default="", help="把逐例结果写到此路径")
    parser.add_argument("--cases", type=int, default=0,
                        help="扫参模式：跑 N 个种子（内置 3 组构筑/地区轮流用），"
                             "打印 SWEEP_SHA256 作为发版前行为回归门禁；"
                             "0 = 只跑内置 3 例（验收指纹 TRACE_SHA256）")
    args = parser.parse_args()

    import engine  # noqa: F401  （确认被测树已可导入）
    print(f"[trace] 被测树 {CWD}")
    if args.force_preview_transaction:
        _apply_force_preview_transaction()
        print("[trace] 开关：--force-preview-transaction")
    if args.patch_runtime_leak:
        _apply_runtime_leak_patch()
        print("[trace] 开关：--patch-runtime-leak")

    from sim import build_learner as bl

    cases = _sweep_cases(args.cases) if args.cases > 0 else CASES
    digest = hashlib.sha256()
    rows = []
    quiet = len(cases) > 20 and not args.json
    for starter, learn, region, seed in cases:
        result = bl.play(starter, learn, region, seed=seed,
                         rng=random.Random(seed))
        payload = json.dumps(_norm(result), ensure_ascii=False, sort_keys=True)
        case_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        digest.update(payload.encode("utf-8"))
        rows.append({"region": region, "seed": seed,
                     "cleared": result.get("cleared"), "won": result.get("won"),
                     "invalid": result.get("invalid"), "timeout": bl._is_resource_timeout(result), "hash": case_hash})
        if not quiet:
            print(f"{region} seed={seed}: cleared={result.get('cleared')} "
                  f"won={result.get('won')} invalid={result.get('invalid')} "
                  f"hash={case_hash}")
    # 验收指纹（内置 3 例）与扫参指纹（--cases N）分开打印，避免混淆两个口径。
    print(("TRACE_SHA256" if args.cases <= 0 else "SWEEP_SHA256"),
          digest.hexdigest())
    if args.cases > 0:
        cleared = sum(1 for r in rows if r["cleared"])
        won = sum(1 for r in rows if r["won"])
        invalid = sum(1 for r in rows if r["invalid"])
        print(f"SWEEP_CASES {len(rows)} cleared={cleared} won={won} invalid={invalid}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"tree": str(CWD), "cases": rows,
                       "trace_sha256": digest.hexdigest()}, fh,
                      ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
