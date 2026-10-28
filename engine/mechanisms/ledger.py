"""通用账本访问接口。逼债与清算均已删除，当前无生产账本登记。"""
from __future__ import annotations

from typing import Any

# 机制名 → Entity 存储字段。新增账本制机制时在此登记一行即可。
_STORAGE: dict[str, str] = {}


def ledger_names() -> list[str]:
    """已登记的账本机制名（供审计/测试枚举）。"""
    return sorted(_STORAGE)


def _storage_attr(mechanism: str) -> str:
    try:
        return _STORAGE[mechanism]
    except KeyError:
        raise ValueError(
            f"机制[{mechanism}]未登记账本存储；请在 engine/mechanisms/ledger.py 的 _STORAGE 中登记"
        ) from None


def ledger_of(entity: Any, mechanism: str) -> list:
    """取得该实体在该机制上的账目列表（惰性创建，返回**可写**列表）。"""
    attr = _storage_attr(mechanism)
    entries = getattr(entity, attr, None)
    if entries is None:
        entries = []
        setattr(entity, attr, entries)
    return entries


def clear_ledger(entity: Any, mechanism: str) -> int:
    """清空账目，返回被清掉的笔数（0 = 本来就是空的，调用方可据此跳过）。"""
    attr = _storage_attr(mechanism)
    entries = getattr(entity, attr, None) or []
    count = len(entries)
    if count:
        setattr(entity, attr, [])
    return count
