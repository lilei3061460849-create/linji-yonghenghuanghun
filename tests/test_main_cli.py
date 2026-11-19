"""主入口演示的冒烟测试。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_api_demo_completes_full_current_setup_flow() -> None:
    """--api 必须按当前开局顺序走完，而不是因旧属性口径或流程顺序崩溃。"""
    result = subprocess.run(
        [sys.executable, "main.py", "--api"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "API演示结束。" in result.stdout
    assert "[7] 当前状态" in result.stdout
    assert "【阶段】pre_battle" in result.stdout
    assert "精力:" not in result.stdout
