"""消耗品通用被动：自愈X（2026-10-14 用户令）。

**自愈X**：恢复 X 点当前耐久，不超过耐久上限。X 为固定点数（如自愈1）；
【无限肉块】的「自愈80%已损耐久」是比例特例（同族不同参）。

触发时机写在消耗品文本里，由本表的 kind 条目声明；将来剧情新增消耗品
只需加一行声明，不写新引擎代码（A（5/10）即按此形状举例，未落地）。

时机：
- battle_end（规则文本：战终）；
- target_death（规则文本：目标死亡时）＝持有者本场战斗出手选中过的目标死亡
  （2026-10-14 用户裁定：你选中的目标才算，没打过的不算；命中与否不影响“选中”，
  被闪避/挡掉照样算。只看选中集合，不限敌我、不问击杀者）。
  离场/暂离不是死亡，不走统一命零管线，天然排除。

数值规格 spec：
- int：固定点数（如 1＝自愈1）；
- ("percent_lost", p)：已损耐久的 p%，向上取整（仅无限肉块在用）。
"""
from __future__ import annotations

import math
from typing import Any

ZIYU_TRIGGER_BATTLE_END = "battle_end"
ZIYU_TRIGGER_TARGET_DEATH = "target_death"

# kind → [(时机, 数值规格)]
CONSUMABLE_ZIYU_TRIGGERS: dict[str, list[tuple[str, Any]]] = {
    "infinite_meat": [(ZIYU_TRIGGER_BATTLE_END, ("percent_lost", 80))],
}


def apply_zi_yu_trigger(items: list | None, trigger: str) -> list[dict]:
    """结算一批消耗品在某时机上的自愈，返回逐件日志。

    每件匹配条目必记一条日志（含 restored=0 的满耐久件，与旧口径一致）。
    """
    logs: list[dict] = []
    for item in items or []:
        entries = CONSUMABLE_ZIYU_TRIGGERS.get(getattr(item, "kind", ""), ())
        for trg, spec in entries:
            if trg != trigger:
                continue
            room = max(0, item.max_uses - item.current_uses)
            if isinstance(spec, tuple) and spec and spec[0] == "percent_lost":
                restored = min(room, math.ceil(room * spec[1] / 100)) if room else 0
            else:
                restored = min(room, max(0, int(spec)))
            if restored:
                item.current_uses += restored
            logs.append({"item": item.name, "trigger": trigger, "lost": room,
                         "restored": restored, "current_uses": item.current_uses,
                         "max_uses": item.max_uses})
    return logs


def note_action_targets(state, actor, targets) -> None:
    """记录一次成功出手的显式目标（本场战斗选中集合）。

    只记持有者（轮回者）本人的选择：朋友/员工代发动、怪物的出手不记。
    调用点保证：只在成功提交后调用；X=0、非法提交、默认自身（非显式目标）不传进来。
    """
    if actor is None or state is None or actor is not getattr(state, "player", None):
        return
    seen = set(getattr(state, "player_selected_targets", None) or [])
    for t in targets or []:
        rid = getattr(t, "runtime_id", "")
        if rid and rid not in seen:
            seen.add(rid)
            state.player_selected_targets.append(rid)

# ==================== 翼类消耗品（2026-10-23 用户令） ====================
# 血翼（原血族遗物【鲜血之翼】）与烬翼（原龙族遗物【烬翼】）都是消耗品：
# 主动使用获得【飞行】1 回合；耐久各有独立的回复触发。
WING_BLOOD = "血翼"
WING_EMBER = "烬翼"
WING_ITEM_SPECS: dict[str, dict] = {
    WING_BLOOD: {
        "max_uses": 3,
        "effect": "主动使用后获得【飞行】持续1回合；你每累计失去100%[血限]的生命，耐久+1",
    },
    WING_EMBER: {
        "max_uses": 5,
        "effect": "主动使用后获得【飞行】持续1回合；任意怪物［命零］时，耐久+1",
    },
}


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
