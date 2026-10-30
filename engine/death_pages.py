"""死者之页的统一承载：每角色默认十页，资源按种类共用。

普通道纹／遗物／法器／消耗品每种一页；未收入【煌妖幡】的妖怪每只五页。
妖怪页仍使用相同的二元资源键与快照，因此既有原子成长回滚无需另设接口。
"""
from .yaoguai import UNHOUSED_PAGE_COST, unhoused_yaoguai


def resource_keys(entity):
    resources = getattr(entity, "_page_resources", {})
    keys = {("道纹", name) for name in entity.dao_wen}
    for kind, field in (("遗物", "relics"), ("消耗品", "consumables"), ("法器", "artifacts")):
        for item in resources.get(field, getattr(entity, field, [])) or []:
            name = item if isinstance(item, str) else item.get("name") if isinstance(item, dict) else item.name
            if name:
                keys.add((kind, name))
    for being in resources.get("unhoused_yaoguai", []) or []:
        for page_index in range(UNHOUSED_PAGE_COST):
            keys.add(("妖怪", f"{being.runtime_id}:{page_index + 1}"))
    return keys


def entities(state):
    result = [state.player] if state.player else []
    for field in ("friends", "employees", "temp_friends", "enemies"):
        result.extend(getattr(state, field, []) or [])
    merchant = getattr(state, "merchant", None)
    if merchant is not None:
        result.append(merchant)
    return {entity.runtime_id: entity for entity in result}.values()


def bind_resources(state):
    """兼容玩家资源存于GameState的既有布局，不建立第二份物品或背包。"""
    if state.player:
        state.player._page_resources = {
            "relics": state.relics, "consumables": state.consumables,
            "artifacts": list(state.artifacts_owned) + list(state.artifacts),
            "unhoused_yaoguai": unhoused_yaoguai(state)}

    if getattr(state, "in_final_duel", False):
        leader = next((e for e in state.enemies if e.entity_type == "轮回者"), None)
        if leader is not None:
            leader._page_resources = {
                "relics": list(leader.relics) + list(state.opponent_relics),
                "consumables": leader.consumables,
                "artifacts": list(leader.artifacts) + list(state.opponent_artifacts_owned)}


def can_accept(entity, kind, name):
    keys = resource_keys(entity)
    return (kind, name) in keys or len(keys) < entity.load_capacity


def usage(state):
    bind_resources(state)
    return {entity.runtime_id: (entity, resource_keys(entity)) for entity in entities(state)}


def growth_error(state, before):
    for actor_id, (entity, keys) in usage(state).items():
        old_keys = before.get(actor_id, (None, set()))[1]
        if len(keys) > entity.load_capacity and len(keys) > len(old_keys):
            return (f"【{entity.name}】死者之页承载不足：共{entity.load_capacity}张，"
                    f"本次将占用{len(keys)}页（普通资源每种1页，未入幡妖怪每只5页）；"
                    "未获得，原行动已回滚")
    return None
