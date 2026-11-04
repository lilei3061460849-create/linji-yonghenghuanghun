"""副本地图消耗品：真实地点投放、独占拾取与死者之页持有记录。

不引入背包容量/格子/负重。玩家复用 state.consumables，其他角色使用同型持有记录。

**三个消耗品模块的分工（2026-11-03 补注，只写注释不改行为）**：

| 模块 | 负责什么 | 什么时候动它 |
| --- | --- | --- |
| `engine/consumables.py` | 消耗品**被动/触发**声明表（自愈X、翼类耐久与模式常量、战终/目标死亡等时机） | 新增一种"挂在某个时机上自动生效"的消耗品 |
| `engine/npc_consumables.py` | **角色化**消耗品结算与 NPC 决策（谁用、何时用、不能越权用） | 改怪物/朋友/员工的用物品策略或合法性 |
| `engine/world_consumables.py` | 副本地图上的**投放与独占拾取**、死者之页持有记录 | 改地图掉落密度/拾取归属 |

三者互不替代：不要在 `npc_consumables` 里再写一份被动表，也不要在 `consumables.py`
里塞地图投放逻辑。历史上有过把同一判定写两遍的返工，护栏测试会盯"同一口径只有一个实现"。
"""
from __future__ import annotations
from dataclasses import asdict
from .models import Consumable

# 可调密度：约每5个尚未占用的可通行格投放1件，不设稀疏的3/5件上限。
CONSUMABLE_CELL_STRIDE = 5


def scatter_consumables(world, dice, specs, available):
    """specs为有真实处理器的满耐久物品；种类有放回，格子无放回。"""
    if any(event.get("type") == "consumables_scattered" for event in world.event_log):
        return 0
    if not specs:
        raise ValueError("地图消耗品池不能为空")
    cells = sorted({tuple(pos) for pos in available
                    if world.map.is_passable(tuple(pos))
                    and world.map.location_at(tuple(pos)) is None
                    and all(actor.position != tuple(pos) for actor in world.actors.values())})
    names = sorted(specs)
    count = (len(cells) + CONSUMABLE_CELL_STRIDE - 1) // CONSUMABLE_CELL_STRIDE
    for index in range(count):
        pos = tuple(dice.auto_roll(f"world_consumable_cell_{index}", cells,
                                  context=f"副本消耗品位置{index + 1}")["selected"])
        name = dice.auto_roll(f"world_consumable_type_{index}", names,
                              context=f"副本消耗品种类{index + 1}")["selected"]
        world.map.add_location(f"consumable_{index + 1}", {
            "kind": "consumable", "name": name, "x": pos[0], "y": pos[1],
            "consumable": asdict(specs[name]), "active": True, "discovered": False})
        cells.remove(pos)
    # Marker persists even if all locations have since been collected or swallowed by the ring.
    world.record("consumables_scattered", None, count=count, cell_stride=CONSUMABLE_CELL_STRIDE)
    return count


def discover_consumables(world):
    """玩家当前真实视野内的物品可见；NPC私有目击由world原有记忆系统记录。"""
    for actor in world.actors.values():
        if actor.kind != "player" or not actor.alive or actor.is_illusion:
            continue
        visible = set(world.visible_cells(actor.actor_id))
        for location in world.map.locations.values():
            if (location.get("kind") == "consumable"
                    and (location["x"], location["y"]) in visible):
                location["discovered"] = True
                known_by = location.setdefault("discovered_by", [])
                if actor.actor_id not in known_by:
                    known_by.append(actor.actor_id)


def collect_consumables(world, state):
    from .death_pages import bind_resources, can_accept
    bind_resources(state)
    entities = ([state.player] if state.player else [])
    for group in ("friends", "employees", "temp_friends", "enemies"):
        entities.extend(getattr(state, group, []) or [])
    if getattr(state, "merchant", None) is not None:
        entities.append(state.merchant)
    by_id = {entity.runtime_id: entity for entity in entities}
    pickups = []
    for location_id, location in world.map.locations.items():
        if location.get("kind") != "consumable" or not location.get("active", True):
            continue
        pos = (location["x"], location["y"])
        for actor in world.actors.values():
            entity = by_id.get(actor.actor_id)
            if (actor.is_illusion or not actor.alive or actor.position != pos
                    or entity is None or not entity.is_alive or entity.is_departed):
                continue
            if not can_accept(entity, "消耗品", location["name"]):
                continue
            item = Consumable(**location["consumable"])
            if entity is state.player:
                owned = state.consumables
                entity.consumables = owned
            else:
                if not hasattr(entity, "consumables"):
                    entity.consumables = []
                owned = entity.consumables
            owned.append(item)
            location.update(active=False, collected=True, collected_by=actor.actor_id,
                            collected_turn=world.clock.turn)
            world.deactivate_location_sightings(location_id, reason="collected")
            world.record("consumable_collected", actor.actor_id, location_id=location_id,
                         name=item.name, position=list(pos))
            pickups.append({"actor_id": actor.actor_id, "name": item.name,
                            "location_id": location_id, "player_visible": entity is state.player})
            break
    return pickups
