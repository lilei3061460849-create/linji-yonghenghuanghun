"""道纹条文速查：`全道纹索引.md` → 名称到「支付/效果」一行的映射。

演武场与对局页都要「点一下就看到完整条文」，条文唯一事实源是
《全道纹索引》的 `### 名称` 小节：标题后第一条不以 `>` 开头的非空行就是该道纹
的支付/效果条文。两个界面共用这一份解析，避免再出现「一边能看条文、另一边只能
看名字」的半套实现。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_FILE = "全道纹索引.md"


@lru_cache(maxsize=4)
def _parse(path_str: str, mtime: float) -> dict[str, str]:
    effects: dict[str, str] = {}
    current = ""
    for raw in Path(path_str).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("### "):
            current = line[4:].strip()
            continue
        if not current or not line or line.startswith(">"):
            continue
        effects[current] = line
        current = ""
    return effects


def daowen_effects() -> dict[str, str]:
    """道纹名 → 条文；索引缺失或解析失败时返回空表（界面只显示名字）。"""
    path = ROOT / INDEX_FILE
    if not path.exists():
        return {}
    return _parse(str(path), path.stat().st_mtime)
