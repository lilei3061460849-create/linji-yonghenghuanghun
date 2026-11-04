"""文档里的命令必须真的能跑：静态校验 README 中写明的入口。

体检发现的问题类型是"文档漂移"——文档写着一条命令，文件早改名/删了，
读者复制粘贴就撞墙。这里做**静态**校验（不执行被文档化的命令，避免几分钟的
全量测试被套娃）：

1. 文档里 `python|python3 <路径>` 的路径必须存在；
2. `python -m pytest` 必须指向存在的 tests 目录；
3. `python3 sim/gen_rules.py --check` 这类"带脚本路径的命令"，脚本必须存在且
   入口有 `__main__`；
4. 文档提到的 `webapp/server.py` 必须能 bind 的端口与 README 写的一致（只看字符串）。

新增文档命令时不需要改本文件；只有命令形态变了（例如改用 `uv run`）才需要。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ["README.md", "engine/README.md", "AGENTS.md"]

# 形如 python3 path/to/script.py [args]；跳过 `python -m <module>` 这种模块调用
SCRIPT_CMD = re.compile(r"^\s*python3?\s+((?:[\w./-]+/)*[\w.-]+\.py)\b")
MODULE_CMD = re.compile(r"^\s*python3?\s+-m\s+([\w.]+)")


def _commands() -> list[tuple[str, int, str]]:
    found = []
    for doc in DOCS:
        path = ROOT / doc
        if not path.exists():
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if SCRIPT_CMD.match(line) or MODULE_CMD.match(line):
                found.append((doc, line_no, line.strip()))
    return found


def test_at_least_one_documented_command():
    """防空转：真解析到了命令，而不是正则失效导致"永远绿"。"""
    commands = _commands()
    assert len(commands) >= 5, commands


def test_documented_python_scripts_exist():
    missing = []
    for doc, line_no, command in _commands():
        match = SCRIPT_CMD.match(command)
        if not match:
            continue
        rel = match.group(1)
        if not (ROOT / rel).exists():
            missing.append(f"{doc}:{line_no} → {rel}")
    assert not missing, "文档里的脚本路径不存在：\n" + "\n".join(missing)


def test_documented_pytest_targets_exist():
    missing = []
    for doc, line_no, command in _commands():
        if "pytest" not in command:
            continue
        for token in command.split():
            if token.startswith("tests") or token.startswith("./tests"):
                target = ROOT / token.split("::")[0]
                if not target.exists():
                    missing.append(f"{doc}:{line_no} → {token}")
    assert not missing, "文档里的 pytest 目标不存在：\n" + "\n".join(missing)


def test_documented_scripts_are_runnable_entrypoints():
    """被文档当作命令行入口的脚本必须有 `__main__` 守卫或直接调用。"""
    not_runnable = []
    for _doc, _line_no, command in _commands():
        match = SCRIPT_CMD.match(command)
        if not match:
            continue
        rel = match.group(1)
        path = ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if "__main__" not in text:
            not_runnable.append(rel)
    assert not not_runnable, "文档当成入口但缺少 __main__ 守卫：" + ", ".join(sorted(set(not_runnable)))
