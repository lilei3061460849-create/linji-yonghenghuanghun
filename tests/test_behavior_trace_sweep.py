"""`sim/behavior_trace.py --cases N` 扫参门禁的用例表契约（不跑对局，秒级）。

扫参指纹 `SWEEP_SHA256` 只有在「用例表逐位固定」的前提下才有意义：
种子必须恰好是 1..N，起手/学习表/地区按内置三例轮流，任何改动都会改变指纹。
本文件把这件事锁死，改动用例表时会立刻失败（届时需要重新发布指纹）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim.behavior_trace import CASES, _sweep_cases  # noqa: E402

# 验收指纹 TRACE_SHA256 依赖的三例（改这三行等于重新发布验收指纹）。
EXPECTED_ACCEPTANCE = [
    ("杀伐", ["再生", "庇护", "束缚", "贯穿", "固执"], "扭曲都市", 2),
    ("坠落", ["杀伐", "连击", "再生", "庇护", "透支"], "罪孽都市", 3),
    ("封印", ["杀伐", "再生", "庇护", "贯穿", "固执"], "龙心谷", 1),
]


def test_acceptance_cases_are_frozen():
    """三例验收用例（既有契约）不得被扫参功能顺手改动。"""
    assert CASES == EXPECTED_ACCEPTANCE


def test_sweep_count_matches_request():
    for count in (1, 3, 4, 50, 60):
        assert len(_sweep_cases(count)) == count


def test_sweep_seeds_are_one_to_n():
    """种子固定为 1..N：同一次扫参在任何树上都必须给出同一个指纹。"""
    for count in (7, 60):
        seeds = [case[3] for case in _sweep_cases(count)]
        assert seeds == list(range(1, count + 1))


def test_sweep_cycles_the_three_builtin_scenarios():
    """起手/学习表/地区按内置三例轮流，保证扫参覆盖三种构筑与地区。"""
    cases = _sweep_cases(7)
    for index, (starter, learn, region, _seed) in enumerate(cases):
        reference = CASES[index % len(CASES)]
        assert (starter, learn, region) == (reference[0], reference[1], reference[2])
    regions = {case[2] for case in _sweep_cases(3)}
    assert regions == {"扭曲都市", "罪孽都市", "龙心谷"}


def test_sweep_prefix_is_stable():
    """前缀稳定：--cases 60 的前 3 例与 --cases 3 完全一致（便于逐步扩大规模比对）。"""
    assert _sweep_cases(60)[:3] == _sweep_cases(3)
