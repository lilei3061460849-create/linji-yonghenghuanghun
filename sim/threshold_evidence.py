"""保险丝阈值的实测依据（Phase 5）。

用法：

    .venv/bin/python sim/threshold_evidence.py            # 默认 24 局
    .venv/bin/python sim/threshold_evidence.py 40

为什么必须有这个脚本：`ResolutionContext.MAX_DEPTH / MAX_EFFECTS` 两个阈值
**不能拍脑袋**——它们唯一合法的取值区间是「合法对局永远够不着」。
本脚本在**全路径记账**（Phase 4 之后的入口 + 汇点 + 触发全部开帧）下跑真实对局，
逐行动统计：

  * 深度峰值（同时存在多少层未结束的结算帧）
  * 单行动效果数峰值（一次顶层行动里一共产生了多少次结算）

输出最大值与阈值之间的余量。余量 < 10 倍时脚本会以非零码退出——
那时候要么是出现了真的失控（先查），要么是阈值需要重新论证（后改）。

统计口径说明：只统计**合法完成**的行动；行动抛异常会被计入 `errors`。
"""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine                                    # noqa: E402
from engine.resolution import ResolutionContext                      # noqa: E402
from sim.build_learner import play                                   # noqa: E402

DAOWEN_SETS = [
    ["杀伐", "连击", "再生", "庇护", "透支"],
    ["杀伐", "坠落", "再生", "分裂", "压制"],
    ["连击", "再生", "庇护", "透支", "坠落"],
]
REGIONS = ["龙心谷", "坠落", "罪孽都市", "扭曲都市"]


def main(games: int = 24) -> int:
    peak_depth = 0
    peak_effects = 0
    peak_action = ""
    peak_frame = None
    errors = 0

    real_enter = ResolutionContext.enter
    real_leave = ResolutionContext.leave
    stats = {"depth": 0, "effects": 0, "kind": "", "game": ""}

    def enter(ctx_self, kind, label=""):
        token = real_enter(ctx_self, kind, label)
        if ctx_self.depth > stats["depth"]:
            stats["depth"] = ctx_self.depth
            stats["kind"] = kind
        return token

    def leave(ctx_self, token=None):
        # 行动内的累计效果数在 leave 时最接近「本次行动一共结算了多少效果」
        # （begin_action 每次归零，因此全局最大值 = 单行动峰值）。
        if ctx_self.effect_count > stats["effects"]:
            stats["effects"] = ctx_self.effect_count
            stats["game"] = stats.get("game", "")
        return real_leave(ctx_self, token)

    ResolutionContext.enter = enter
    ResolutionContext.leave = leave
    try:
        for i in range(games):
            daowen = DAOWEN_SETS[i % len(DAOWEN_SETS)]
            region = REGIONS[i % len(REGIONS)]
            seed = i + 1
            stats["depth"] = 0
            stats["effects"] = 0
            stats["game"] = f"{region}/{seed}"
            try:
                play(region, daowen, "罪孽都市" if i % 2 else "龙心谷",
                     seed=seed, rng=random.Random(seed))
            except Exception as exc:      # noqa: BLE001 —— 只统计，不掩盖
                errors += 1
                print(f"  局 {i}: {type(exc).__name__}: {exc}")
                continue
            if stats["depth"] > peak_depth:
                peak_depth = stats["depth"]
                peak_frame = stats["kind"]
            if stats["effects"] > peak_effects:
                peak_effects = stats["effects"]
                peak_action = stats["game"]
    finally:
        ResolutionContext.enter = real_enter
        ResolutionContext.leave = real_leave

    ctx = ResolutionContext()
    print(f"对局数: {games}（异常 {errors}）")
    print(f"深度峰值: {peak_depth}（最深处 {peak_frame}）"
          f"  ← 阈值 {ctx.MAX_DEPTH}，余量 {ctx.MAX_DEPTH / max(1, peak_depth):.1f}×")
    print(f"单行动效果数峰值: {peak_effects}（{peak_action}）"
          f"  ← 阈值 {ctx.MAX_EFFECTS}，余量 {ctx.MAX_EFFECTS / max(1, peak_effects):.1f}×")
    ok = (peak_depth * 10 <= ctx.MAX_DEPTH) and (peak_effects * 10 <= ctx.MAX_EFFECTS)
    print("结论:", "阈值余量充足（>=10×）" if ok else "余量不足，必须重新论证阈值")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 24))
