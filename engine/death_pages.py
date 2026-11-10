"""死者之页的统一承载：每角色默认十页，资源按种类共用。

普通道纹／法器／消耗品每种一页；未收入【煌妖幡】的妖怪每只五页。
妖怪页仍使用相同的二元资源键与快照，因此既有原子成长回滚无需另设接口。

★ （重构1）：**遗物不再占死者之页**。
持有遗物的总数量不受十页限制，因此战斗中的【交易】临时遗物、【豪夺】
夺取/归还、世界事件授予遗物都不会触发页数超载或整行动回滚。
遗物的"承载"改为**已启用件数上限**＝`ceil(血限 / 30)`，见
`Entity.enabled_relic_limit` / `Entity.enabled_relic_names`。
"""
from .yaoguai import UNHOUSED_PAGE_COST, unhoused_yaoguai


def resource_keys(entity):
    resources = getattr(entity, "_page_resources", {})
    keys = {("道纹", name) for name in entity.dao_wen}
    # 2026-11-01 重构1：遗物已脱离页数体系，只统计道纹/消耗品/法器。
    for kind, field in (("消耗品", "consumables"), ("法器", "artifacts")):
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


# 新建妖怪的死者之页默认值（与 `Entity.death_page_count` 的默认值同源）。
DEFAULT_PAGE_COUNT = 10


def yaoguai_creation_error(state, signature_daowen_name: str = "") -> str | None:
    """【造生】付费前的页数预检，口径与事务层 `growth_error` **完全一致**。

    做法是直接模拟"造生完成后"的键集合，而不是另写一份近似算式：

    - 主人：现有键 + `UNHOUSED_PAGE_COST` 个妖怪页（妖怪本体）；
    - 新妖怪自身：`{("道纹", 签名道纹)}`——签名道纹记在妖怪自己的页上；
      **偏爱遗物在重构1（遗物脱离十页）后不再占任何页，故不计**。

    任一方的键数超过其自身 `load_capacity` 才报错，所以本函数放行的结果
    与 `growth_error` 放行的结果一致（不存在"预检过、事务层再拦"的错位）。
    """
    bind_resources(state)
    player = getattr(state, "player", None)
    if player is None:
        return None
    keys = set(resource_keys(player))
    for page_index in range(UNHOUSED_PAGE_COST):
        keys.add(("妖怪", f"new-being:{page_index + 1}"))
    if len(keys) > player.load_capacity:
        return (f"未入幡妖怪每只占{UNHOUSED_PAGE_COST}张死者之页；"
                f"当前已用{len(resource_keys(player))}/{player.load_capacity}页，"
                f"无法创造新妖怪")
    # 新妖怪自带签名道纹：记在它自己的页上（新实体固定 10 页，仅1页占用）。
    if signature_daowen_name and 1 > DEFAULT_PAGE_COUNT:
        return "新妖怪的签名道纹占页超过其自身死者之页上限"
    return None
