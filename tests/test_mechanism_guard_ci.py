"""护栏自身进 CI：已迁移机制不得在核心管线里重新长出硬编码分支。

此前 `validator.check_migrated_mechanism_guards()` 只在几个迁移专项测试里被顺带调用
一次，而且扫描范围写死在 `_combat_guard_files()` 里：**新增文件不会被扫**。
2026-11-03 把 action 处理器搬进 `engine/handlers/*.py` 之后，这个洞变大了——曾经的
`api.py` 方法体现在住在那儿，护栏不扩张就等于静默失效。

本文件做三件事：

1. 断言当前零违规；
2. **禁令测试**：造一个确实含违规行的临时文件，证明扫描器真的会报（防止"永远绿"）；
3. 覆盖性：新分片 / handlers 模块必须自动出现在扫描范围内。
"""
from __future__ import annotations

from pathlib import Path

from engine.mechanisms import MECHANISMS
from engine.validator import (_MIGRATION_GUARD_PROTECTED_FILES,
                              check_migrated_mechanism_guards)

ROOT = Path(__file__).resolve().parents[1]


def test_no_hardcoded_branch_for_migrated_mechanism():
    assert check_migrated_mechanism_guards() == []


def test_guard_actually_reports_violations(tmp_path):
    """禁令测试：扫描器对违规行必须报错，不能是空实现。"""
    name = MECHANISMS.names()[0]
    fake = tmp_path / "fake_pipeline.py"
    fake.write_text(f'    if combat.has_status("{name}"):\n        pass\n', encoding="utf-8")

    violations = check_migrated_mechanism_guards([str(fake)])
    assert len(violations) == 1
    assert violations[0]["severity"] == "error"
    assert violations[0]["context"]["mechanism"] == name


def test_guard_ignores_comments(tmp_path):
    """注释里的同名分支不算违规（否则护栏会逼人删注释）。"""
    name = MECHANISMS.names()[0]
    fake = tmp_path / "commented.py"
    fake.write_text(f'# 旧实现：has_status("{name}")\n', encoding="utf-8")
    assert check_migrated_mechanism_guards([str(fake)]) == []


def test_guard_scope_covers_combat_shards_and_action_handlers():
    """覆盖性：战斗分片与 handlers 全在扫描范围内，新增文件不得漏。

    这两条覆盖关系以前只写在注释里（"新分片必须自动纳入"），没有任何测试兜底；
    现在写成断言：往 `engine/combat_parts/` 或 `engine/handlers/` 加一个 .py，
    忘了纳入护栏会立刻红。
    """
    protected = set(_MIGRATION_GUARD_PROTECTED_FILES)
    shards = {f"engine/combat_parts/{p.name}"
              for p in (ROOT / "engine" / "combat_parts").glob("*.py")}
    handlers = {f"engine/handlers/{p.name}"
                for p in (ROOT / "engine" / "handlers").glob("*.py")}
    assert shards, "combat_parts 目录为空，护栏范围断言失效"
    assert handlers, "handlers 目录为空，护栏范围断言失效"
    assert shards <= protected
    assert handlers <= protected
    assert {"engine/api.py", "engine/combat.py", "engine/combat_hooks.py"} <= protected
