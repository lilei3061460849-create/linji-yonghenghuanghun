"""【造生】妖怪与【煌妖幡】的共享规则。

保留 ``is_created_being`` / ``created_by_runtime_id`` 字段名以兼容既有存档；
对外的规则名称统一为“妖怪”。本模块不持有第二份角色列表，所有查询都读取
GameState 中真实的朋友／员工实体。
"""

BANNER_NAME = "煌妖幡"
LEGACY_BANNER_NAME = "万魂幡"
BANNER_CAPACITY = 6
UNHOUSED_PAGE_COST = 5


def has_banner(state) -> bool:
    """轮回者是否持有煌妖幡；旧存档名称只在读取兼容层承认。"""
    owned = set(getattr(state, "artifacts_owned", []) or [])
    owned.update(getattr(state, "artifacts", []) or [])
    return bool(owned & {BANNER_NAME, LEGACY_BANNER_NAME})


def owned_yaoguai(state, owner_runtime_id: str | None = None) -> list:
    """按既有列表顺序返回主人的全部妖怪（含休眠者）。"""
    player = getattr(state, "player", None)
    owner_id = owner_runtime_id or getattr(player, "runtime_id", "")
    if not owner_id:
        return []
    result = []
    for field in ("friends", "employees"):
        for entity in getattr(state, field, []) or []:
            if (getattr(entity, "is_created_being", False)
                    and getattr(entity, "created_by_runtime_id", "") == owner_id):
                result.append(entity)
    return result


def banner_residents(state) -> list:
    """持有幡时，前六只妖怪由幡统一承载；旧档超额部分不会凭空豁免页数。"""
    if not has_banner(state):
        return []
    return owned_yaoguai(state)[:BANNER_CAPACITY]


def unhoused_yaoguai(state) -> list:
    housed = {entity.runtime_id for entity in banner_residents(state)}
    return [entity for entity in owned_yaoguai(state)
            if entity.runtime_id not in housed]


def find_owned_yaoguai(state, reference) -> object | None:
    """以 runtime_id 或唯一名字显式定位妖怪，拒绝不透明的模糊匹配。"""
    if not isinstance(reference, str) or not reference:
        return None
    owned = owned_yaoguai(state)
    by_id = next((entity for entity in owned if entity.runtime_id == reference), None)
    if by_id is not None:
        return by_id
    matches = [entity for entity in owned if entity.name == reference]
    return matches[0] if len(matches) == 1 else None
