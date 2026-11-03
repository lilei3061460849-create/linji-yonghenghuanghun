"""覆盖率排行：把 `.coverage` 里"行数不少、覆盖率最低"的文件列出来。

用途是**排序参考**，不是门禁——仓库没有覆盖率门槛（写门槛只会逼人加
`# pragma: no cover`）。CI 的 coverage job 允许失败，只负责把排行贴进日志。

用法：
    python -m pytest --cov=engine --cov=webapp        # 先产出 .coverage
    python3 tools/coverage_ranking.py --min-statements 30 --top 20
"""
from __future__ import annotations

import argparse
import os

import coverage


def main() -> int:
    parser = argparse.ArgumentParser(description="按覆盖率升序列出模块")
    parser.add_argument("--min-statements", type=int, default=30,
                        help="只统计语句数不低于此值的文件（默认 30，过滤小文件噪声）")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--data-file", default=".coverage")
    args = parser.parse_args()

    cov = coverage.Coverage(data_file=args.data_file)
    try:
        cov.load()
    except coverage.exceptions.DataError as exc:      # 数据文件不存在/损坏
        print(f"读不到 {args.data_file}：{exc}\n先跑 python -m pytest --cov=engine --cov=webapp")
        return 2

    rows = []
    for filename in cov.get_data().measured_files():
        _, statements, _, missing, _ = cov.analysis2(filename)
        if len(statements) < args.min_statements:
            continue
        pct = 100.0 * (len(statements) - len(missing)) / len(statements)
        rows.append((pct, len(statements), len(missing), filename))
    rows.sort()
    root = os.getcwd() + os.sep

    print(f"| 覆盖率 | 语句数 | 未覆盖 | 文件 |")
    print("| ---: | ---: | ---: | --- |")
    for pct, statements, missing, filename in rows[: args.top]:
        shown = filename[len(root):] if filename.startswith(root) else filename
        print(f"| {pct:.0f}% | {statements} | {missing} | `{shown}` |")
    print(f"\n共 {len(rows)} 个文件（语句数 ≥ {args.min_statements}）；"
          f"整体覆盖率见 `coverage report`。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
