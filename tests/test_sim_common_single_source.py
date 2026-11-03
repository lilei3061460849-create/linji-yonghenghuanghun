"""sim/ 里的公共助手只留一份（`sim/common.py`），不许再复制回去。

体检（2026-11-03）发现的现状：`best_cultivate_tier` 7 份、`_resolve_monster_turn_smart`
6 份、`_learn` 7 份（其中 3 份是"自己调用自己"的死代码）。它们逐字相同，
改一处忘六处 → 批量跑局的数据对不上。

本测试守三件事：
1. 已经统一的三组，脚本里**不能再出现同名 def**（要改去改 `sim/common.py`）；
2. `sim/common.py` 必须真的导出这三个符号（防空转）；
3. 两份**故意保留**的分叉实现（闪避阈值不同）还在，且带标注——否则以后有人
   把分叉当漏网之鱼"顺手合并"，会静默改掉 sim 的统计数据。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "sim"

UNIFIED = {
    "best_cultivate_tier": [
        "authentic_asymmetric_duel.py", "generate_duel_report.py", "high_iq_duel.py",
        "inter_dungeon_duel.py", "manual_handplay.py", "multi_reincarnation_duel.py",
    ],
    "_resolve_monster_turn_smart": [
        "authentic_asymmetric_duel.py", "generate_duel_report.py", "high_iq_duel.py",
        "inter_dungeon_duel.py",
    ],
    "_learn": [
        "probe_custom_spell_triggers.py", "probe_damage_pipeline_triggers.py",
        "probe_global_trigger_spells.py", "probe_trigger_condition_syntax.py",
    ],
}

# 刻意保留分叉的两份（阈值 14/7 vs 12/6），只有它们还允许有本地 def
DELIBERATE_FORKS = {
    "_resolve_monster_turn_smart": [
        "manual_handplay.py", "multi_reincarnation_duel.py",
    ],
}


def _top_level_defs(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_common_module_exports_the_shared_helpers():
    import sim.common as common

    assert callable(common.best_cultivate_tier)
    assert callable(common._resolve_monster_turn_smart)
    assert callable(common.learn_spell_probe_shortcut)
    # 边界值锁住：6 档碎片梯度是 sim 侧评分的基础，改这里等于改所有对比数据
    assert common.best_cultivate_tier(150) == (6, 150, 6)
    assert common.best_cultivate_tier(0) == (1, 0, 1)


@pytest.mark.parametrize("name,filenames", sorted(UNIFIED.items()))
def test_unified_helpers_are_no_longer_defined_locally(name, filenames):
    offenders = []
    allowed = set(DELIBERATE_FORKS.get(name, []))
    for filename in filenames:
        if filename in allowed:
            continue
        path = SIM / filename
        if name in _top_level_defs(path):
            offenders.append(filename)
    assert not offenders, (
        f"{name} 又在 {offenders} 里长出了本地副本；公共版本在 sim/common.py")


def test_deliberate_forks_stay_and_are_documented():
    """两份分叉实现必须还在，且带着"已分叉"标注（防止被当成漏网之鱼合并掉）。"""
    for filename in DELIBERATE_FORKS["_resolve_monster_turn_smart"]:
        text = (SIM / filename).read_text(encoding="utf-8")
        assert "def _resolve_monster_turn_smart(" in text, (
            f"{filename} 的本地实现没了？若是有意统一，请同时删掉 tests 里的分叉白名单")
        assert "已分叉" in text, f"{filename} 缺少'已分叉'标注，后来者会误以为是漏网重复"


def test_no_recursive_stub_left():
    """历史遗留：3 个探针里有 `def _learn(...): return _learn(...)` 的死代码。"""
    for filename in UNIFIED["_learn"]:
        text = (SIM / filename).read_text(encoding="utf-8")
        assert "def _learn(e, definition):\n    return _learn(e, definition)" not in text
