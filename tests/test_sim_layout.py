"""sim/ 目录结构的护栏（2026-11-04 分目录后）。

体检结论：`sim/` 里 80+ 个脚本混在一起，靠文件名猜用途。拆成
`probes/ duels/ audits/ reports/` 之后，最容易出的事是**把"被代码 import 的库"
或"CI 点名的入口"搬进子目录**——那会静默打破 import 或让工作流找不到脚本。

本测试守四条：

1. 四个子目录都在，且放的是命令行脚本，不是被 import 的库；
2. 除 `sim/` 根以外，任何地方都不得出现 `sim.<子目录里的模块>` 这种 import
   （真需要引用就说明它该留在根）；
3. CI / 文档点名的入口仍在 `sim/` 根；
4. 子目录脚本都带"向上找 engine/"的引导注释，搬动时不需要再手改路径。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "sim"

SUBDIRS = ("probes", "duels", "audits", "reports")

# CI 工作流 / 根文档点名、必须留在 sim/ 根的入口
ROOT_ENTRYPOINTS = (
    "gen_rules.py", "gen_daowen_index.py", "gen_ai_context.py",
    "run_open_world_replay.py", "produce_real_winners.py",
    "produce_open_world_winners.py",
)

# 被 engine/ tests/ 或其它 sim 根模块 import 的库，必须留在根
STICKY_LIBRARIES = (
    "common.py", "build_learner.py", "duel_common.py", "duel_dialogue.py",
    "monster_targets.py", "balance_sim.py", "win_only_ai.py",
    "alt_path_test.py", "duel_pvp.py", "duel_chronicle.py", "duel_diff_trace.py",
)


def _subdir_modules() -> set[str]:
    names = set()
    for sub in SUBDIRS:
        names |= {p.stem for p in (SIM / sub).glob("*.py")}
    return names


def test_subdirs_exist_and_have_scripts():
    for sub in SUBDIRS:
        files = list((SIM / sub).glob("*.py"))
        assert files, f"sim/{sub}/ 空目录？"
        assert (SIM / sub).is_dir()


@pytest.mark.parametrize("filename", ROOT_ENTRYPOINTS)
def test_ci_entrypoints_stay_at_sim_root(filename):
    assert (SIM / filename).is_file(), (
        f"{filename} 被 CI/文档点名，必须留在 sim/ 根（子目录会让工作流 404）")


@pytest.mark.parametrize("filename", STICKY_LIBRARIES)
def test_imported_libraries_stay_at_sim_root(filename):
    assert (SIM / filename).is_file(), f"{filename} 被代码 import，必须留在 sim/ 根"


def test_no_code_imports_a_subdir_module():
    """子目录脚本只能当命令行工具跑；被 import 就说明搬错了。"""
    moved = _subdir_modules()
    assert moved, "没找到子目录脚本，护栏失效"
    offenders = []
    for path in ROOT.rglob("*.py"):
        if ".venv" in str(path) or "__pycache__" in str(path):
            continue
        if path.parent.parent == SIM and path.parent.name in SUBDIRS:
            continue                       # 子目录内部互相引用不算
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name in moved:
            if re.search(rf"(?:from\s+sim\.{name}\s+import|from\s+sim\s+import\s+{name}\b"
                         rf"|import\s+sim\.{name}\b)", text):
                offenders.append(f"{path.relative_to(ROOT)} -> sim.{name}")
    assert not offenders, (
        "有代码 import 了子目录里的脚本，说明它不该搬：\n" + "\n".join(offenders))


def test_subdir_scripts_carry_the_root_bootstrap():
    """子目录脚本统一用"向上找 engine/"定位仓库根，搬动时不用改路径。"""
    missing = []
    for sub in SUBDIRS:
        for path in (SIM / sub).glob("*.py"):
            text = path.read_text(encoding="utf-8")
            if '_os.path.isdir(_os.path.join(_ROOT, "engine"))' not in text:
                missing.append(f"sim/{sub}/{path.name}")
    assert not missing, "缺少统一引导：\n" + "\n".join(missing)
