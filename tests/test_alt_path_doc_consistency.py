"""`sim/alt_path_test.py` 与引擎口径的一致性护栏。

`alt_path_test.py` 是"非击杀路径"的实测脚本（癌变/凡庸/雕塑/封印），它的模块
docstring 里写着三条阈值（癌变 = 累计恢复 ≥ 血限×2、凡庸 = 连续 5 回合…）。
这类"脚本里抄一份规则"最容易和引擎悄悄分叉：脚本不变、引擎改了阈值，跑出来的
数据就没人能解释。

本测试不执行对局，只比对**声明**：

1. docstring 里写死的 ×2 / 5 回合，必须等于引擎的唯一事实源常量；
2. 脚本结尾靠 `is_sculptured` / `is_proliferated` / `removed_without_kill` 三个
   标记分类，这三个标记必须真实存在于 `Entity` 上（改名即红）；
3. 阈值本身必须仍然只有一处定义（防止有人再抄一份到 sim/）。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from engine.combat import MEDIOCRITY_ROUNDS
from engine.models import Entity

ROOT = Path(__file__).resolve().parents[1]
ALT_PATH = ROOT / "sim" / "alt_path_test.py"


def _doc() -> str:
    """模块 docstring（文件以 shebang 开头，不能靠找引号）。"""
    return ast.get_docstring(ast.parse(ALT_PATH.read_text(encoding="utf-8"))) or ""


def test_cancer_threshold_matches_engine():
    """docstring 的「血限×2」必须等于 Entity.CANCER_HEAL_MULTIPLIER。"""
    assert Entity.CANCER_HEAL_MULTIPLIER == 2, (
        f"引擎癌变倍率已改为 {Entity.CANCER_HEAL_MULTIPLIER}，"
        "sim/alt_path_test.py 的 docstring 也要同步")
    assert "血限×2" in _doc()


def test_mediocrity_rounds_matches_engine():
    """docstring 的「连续5回合」必须等于 MEDIOCRITY_ROUNDS。"""
    assert MEDIOCRITY_ROUNDS == 5, (
        f"引擎凡庸回合数已改为 {MEDIOCRITY_ROUNDS}，sim/alt_path_test.py 的 docstring 也要同步")
    assert "连续5回合" in _doc()


def test_classification_flags_exist_on_entity():
    """脚本按这三个标记判定走的是哪条路径；改名会让统计静默归零。"""
    for attr in ("is_sculptured", "is_proliferated", "removed_without_kill"):
        assert hasattr(Entity, attr) or hasattr(Entity("探针", "怪物"), attr), (
            f"Entity 上找不到 {attr}：sim/alt_path_test.py 的路径统计会静默失效")


def test_sim_does_not_redefine_the_thresholds():
    """阈值只能在 engine/ 里定义一处；sim/ 出现同名赋值即为第二份事实源。"""
    offenders = []
    for path in sorted((ROOT / "sim").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if re.match(r"\s*(PROLIFERATION_THRESHOLD|CANCER_THRESHOLD|MEDIOCRITY_ROUNDS)\s*=", line):
                offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, "sim/ 里出现了第二份阈值定义：\n" + "\n".join(offenders)
