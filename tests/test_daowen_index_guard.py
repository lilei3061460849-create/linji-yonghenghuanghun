"""护栏：`全道纹索引.md` 必须与引擎/事实源逐字一致。

为什么需要这条（2026-10-11）：
`AI_CONTEXT.md` 有 `tests/test_ai_context.py` 逐字节比对，《死者之书.md》的「## 规则」节
有 `tests/test_lethal_rules_source.py` 调 `gen_rules.py --check`——唯独 `全道纹索引.md`
没有任何一致性护栏。它是**派生文档**（由 `sim/gen_daowen_index.py` 从引擎 docstring、
`engine/rule_engine.py`、`engine/gamedata.py`、`副本/*.md` 生成），一旦忘记重生成就会
静默漂移，而且没有任何测试会失败。

真实事故：【分裂】早在 2026-09-17 就废掉了「[命零]时触发」、2026-10-08 用户令又把代价
从【衰老】改为【流血】，但生成器里硬编码的 `NOTES["分裂"]` 一直写着「【命零】时触发」，
于是**每次重生成都会把废止口径重新印进索引**，潜伏到 2026-10-11 才被发现。

这条用例等价于跑 `python3 sim/gen_daowen_index.py --check`，因此 CI（.github/workflows/
tests.yml 在 pull_request 上跑全量 pytest）会在合并前拦住漂移——不需要每次改完手动跑。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GEN = ROOT / "sim" / "gen_daowen_index.py"
INDEX = ROOT / "全道纹索引.md"


def test_daowen_index_is_up_to_date_with_the_engine():
    """索引必须等于当前生成器输出；漏重生成即为失败。"""
    index_text = INDEX.read_text(encoding="utf-8")
    assert "当前索引收录道纹（39种）" in index_text
    assert "（反转）→？？（反向）" in index_text
    assert "（反转）→加害（反向）" not in index_text
    if not INDEX.exists():
        pytest.skip("全道纹索引.md 不存在")
    proc = subprocess.run(
        [sys.executable, str(GEN), "--check"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        "全道纹索引.md 与引擎/事实源不一致，请跑 `python3 sim/gen_daowen_index.py`。\n"
        f"{proc.stdout}{proc.stderr}"
    )


def test_generator_has_no_notes_for_deleted_daowen():
    """生成器的 NOTES 不得给已删除的道纹留条目（死条目会被下一次重生成复活）。

    NOTES 是人写死的一段话，不跟引擎走；`sim/gen_daowen_index.py` 里
    `assert set(CATEGORY) == set(DaoWenEngine._registry)` 只约束分类表，管不到 NOTES。
    【尸爆】2026-10-03 删除后，它的 NOTES 条目就一直留在字典里。
    """
    # 草案道纹：规则已写进副本文档、但刻意不接入运行时，因此不在引擎注册表里。
    # 这类条目是"规划中"，不是"已删除"，不算漂移（见生成器「未实现（荒疫古城·1）」段：
    # 「感染/出土/篡改/尘封/催化/重演/原初等草案道纹，均不在当前版本引擎内」）。
    DRAFT_ONLY = {"原初"}

    src = GEN.read_text(encoding="utf-8")
    from engine.daowen import DaoWenEngine

    DaoWenEngine.register_all()
    registered = set(DaoWenEngine._registry)
    block = src[src.index("NOTES = {"):src.index("\n}\n", src.index("NOTES = {"))]
    noted = {line.split('"')[1] for line in block.splitlines()
             if line.strip().startswith('"') and '":' in line}
    stale = noted - registered - DRAFT_ONLY
    assert not stale, f"NOTES 里留着已删除道纹的条目：{sorted(stale)}"
