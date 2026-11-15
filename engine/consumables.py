"""消耗品声明层：翼类耐久/模式常量、耐久恢复声明（删掉「自愈X」）。

****：消耗品通用关键词【自愈X】取消——该机制此前已令删除，
不得复活。这里因此：
- 不再有「自愈X」这个术语，也不解析物品文本；
- 不再有「目标死亡时」这种跨物品通用时机；
- 只剩一条：把**物品正文里逐条写明**的[战终]耐久恢复落到实处。

| kind | 条文出处 | 效果 |
| --- | --- | --- |
| `infinite_meat` | 《物品索引》无限肉块、《死者之书》癌变章 | [战终]恢复80%已损耐久，向上取整 |

将来新增同类物品，先把效果写进该物品自己的条文，再在声明表里加一行
（按 kind 索引，不按名字判断，也不做文本解析）。

**三个消耗品模块的分工（2026-11-03 补注，只写注释不改行为）**：

| 模块 | 负责什么 | 什么时候动它 |
| --- | --- | --- |
| `engine/consumables.py` | 消耗品**声明层**（翼类耐久与模式常量、写明的[战终]耐久恢复） | 新增一种"挂在某个时机上自动生效"的消耗品 |
| `engine/npc_consumables.py` | **角色化**消耗品结算与 NPC 决策（谁用、何时用、不能越权用） | 改怪物/朋友/员工的用物品策略或合法性 |
| `engine/world_consumables.py` | 副本地图上的**投放与独占拾取**、死者之页持有记录 | 改地图掉落密度/拾取归属 |

三者互不替代：不要在 `npc_consumables` 里再写一份声明，也不要在 `consumables.py`
里塞地图投放逻辑。历史上有过把同一判定写两遍的返工，护栏测试会盯"同一口径只有一个实现"。
"""
from __future__ import annotations

import math
from typing import Any

# kind → 恢复规格（"percent_lost", p = 已损耐久的 p%，向上取整）
DURABILITY_RECOVERY: dict[str, tuple[str, Any]] = {
    "infinite_meat": ("percent_lost", 80),
}


def apply_battle_end_durability_recovery(items: list | None) -> list[dict]:
    """[战终]结算条文里写明的耐久恢复，返回逐件日志。

    每件匹配条目必记一条日志（含 restored=0 的满耐久件），与旧口径一致。
    """
    logs: list[dict] = []
    for item in items or []:
        spec = DURABILITY_RECOVERY.get(getattr(item, "kind", ""))
        if spec is None:
            continue
        room = max(0, item.max_uses - item.current_uses)
        kind, param = spec
        if kind == "percent_lost":
            restored = min(room, math.ceil(room * param / 100)) if room else 0
        else:  # pragma: no cover - 声明表目前只有上面一种规格
            restored = min(room, max(0, int(param)))
        if restored:
            item.current_uses += restored
        logs.append({"item": item.name, "trigger": "battle_end", "lost": room,
                     "restored": restored, "current_uses": item.current_uses,
                     "max_uses": item.max_uses})
    return logs

# ==================== 翼类消耗品 ====================
# 血翼（原血族遗物【鲜血之翼】）与烬翼（原龙族遗物【烬翼】）都是消耗品：
# 主动使用获得【飞行】1 回合；耐久各有独立的回复触发。
WING_BLOOD = "血翼"
WING_EMBER = "烬翼"
WING_ITEM_SPECS: dict[str, dict] = {
    WING_BLOOD: {
        "max_uses": 3,
        "effect": ("主动使用（选一）：①本回合中你失去的生命改为回复；②获得【飞行】1回合；"
                   "③立刻逃跑。你每累计失去等同于[血限]的生命，耐久+1"),
    },
    WING_EMBER: {
        "max_uses": 3,
        "effect": ("主动使用（选一）：①本回合你不会触发任何特殊事件；"
                   "②获得【飞行】1回合；③立刻逃跑。任意怪物［命零］时，耐久+1"),
    },
}

# 每次使用选一个模式；模式名是引擎参数，括号内文本用于错误提示与文档。
WING_MODE_FLY = "fly"
WING_MODE_FLEE = "flee"
WING_MODE_LIFESTEAL = "lifesteal"   # 血翼专属
WING_MODE_SUPPRESS = "suppress"     # 烬翼专属
WING_MODES: dict[str, dict[str, str]] = {
    WING_BLOOD: {
        WING_MODE_LIFESTEAL: "本回合中你失去的生命改为回复",
        WING_MODE_FLY: "获得【飞行】1回合",
        WING_MODE_FLEE: "立刻逃跑",
    },
    WING_EMBER: {
        WING_MODE_SUPPRESS: "本回合你不会触发任何特殊事件",
        WING_MODE_FLY: "获得【飞行】1回合",
        WING_MODE_FLEE: "立刻逃跑",
    },
}

# 模式生效时给持有者的状态名（「本回合」= remaining_rounds=1，回终到期）。
WING_STATUS_LIFESTEAL = "噬血"
WING_STATUS_SUPPRESS = "熄事"


def make_wing_item(name: str):
    """生成一件翼类消耗品（满耐久）。"""
    from .models import Consumable

    spec = WING_ITEM_SPECS[name]
    return Consumable(name=name, effect=spec["effect"], current_uses=spec["max_uses"],
                      max_uses=spec["max_uses"], kind="wing")


def _find(state, name: str):
    for item in getattr(state, "consumables", []) or []:
        if getattr(item, "name", "") == name:
            return item
    return None


def note_blood_wing_hp_loss(state, entity, amount: int) -> list[dict]:
    """血翼：玩家每累计失去 100%[血限] 的生命，耐久+1（不超过耐久上限）。

    挂在统一失血总账（`_record_hp_loss_event`）上，所以挨打、代价、反噬一律计入。
    按「当前[血限]」分块计量，余数滚存到 `item.progress`；耐久已满时该块作废。
    """
    if state is None or entity is None or getattr(state, "player", None) is not entity:
        return []
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return []
    if amount <= 0:
        return []
    item = _find(state, WING_BLOOD)
    if item is None:
        return []
    threshold = max(1, int(getattr(entity, "blood_limit", 0) or 1))
    item.progress += amount
    logs: list[dict] = []
    while item.progress >= threshold:
        item.progress -= threshold
        if item.current_uses < item.max_uses:
            item.current_uses += 1
            logs.append({"item": item.name, "trigger": "hp_loss_total",
                         "current_uses": item.current_uses, "max_uses": item.max_uses})
    return logs


def note_ember_wing_monster_death(state, entity) -> dict | None:
    """烬翼：任意怪物［命零］时，耐久+1（不超过耐久上限）。"""
    if state is None or entity is None:
        return None
    if getattr(entity, "entity_type", "") != "怪物":
        return None
    item = _find(state, WING_EMBER)
    if item is None or item.current_uses >= item.max_uses:
        return None
    item.current_uses += 1
    return {"item": item.name, "trigger": "monster_death",
            "current_uses": item.current_uses, "max_uses": item.max_uses}
