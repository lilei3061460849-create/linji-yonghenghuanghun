"""
开局流程处理器（Setup Handler）
负责初始属性分配、残韵选择、副本选择与遗物发现初始化。
"""
from __future__ import annotations
from typing import Any, Dict
from ..models import Entity
from ..enums import EntityType
from ..gamedata import SHAFA_LOOP_DAOWEN


def handle_setup_attributes(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """分配初始属性点：2属性点 = 1速限 = 1法限；1属性点 = 6血限

    DM裁定 2026-09-10：轮回者的攻击次数=当前速度、攻击力=当前法力（换算仅限轮回者），
    攻次/攻力不再是可购买的独立面板，两个加点池随之撤销；速限/法限因为兼任攻次/攻力
    而变贵（原 1点=1速限=2法限 → 现 2点=1速限=1法限）。25 点总预算不变。
    """
    if engine.state.player is not None:
        return {"success": False, "error": "初始属性已经分配，不能重复开局"}
    blood_points = params.get("blood_points", 0)
    speed_points = params.get("speed_points", 0)
    mana_points = params.get("mana_points", 0)
    total = blood_points + speed_points + mana_points

    if total > 25:
        return {
            "success": False,
            "error": f"属性点总和不能超过25，当前为{total}",
            "instruction": "2属性点=1速限=1法限；1属性点=6血限；没花完的点会存进属性点池，之后随时可兑"
        }
    # 速限/法限按 2 点一档计价，买不出半档 → 点数须为偶数。
    # DM裁定 2026-09-10：余点不再强塞血限，允许留着（total<25 的部分进属性点池）。
    if speed_points % 2 or mana_points % 2:
        return {
            "success": False,
            "error": f"速限/法限按2属性点一档计价，点数必须是偶数（当前 速{speed_points}/法{mana_points}）",
            "instruction": "改成偶数即可；剩下的点会存进属性点池，攒够2点随时再兑"
        }

    blood_limit = blood_points * 6
    speed_limit = speed_points // 2
    mana_limit = mana_points // 2

    player = Entity(
        name=params.get("name", "轮回者"),
        entity_type=EntityType.REINCARNATOR.value,
        blood_limit=blood_limit,
        current_hp=blood_limit,
        mana_limit=mana_limit,
        current_mana=mana_limit,
        speed_limit=speed_limit,
        current_speed=speed_limit,
        # DM裁定 2026-09-10：轮回者的攻次/攻力由当前速度/当前法力换算，不再存面板值。
        # 字段留 0 只是占位——所有取值一律走 effective_attack_count/power()。
        attack_count=0,
        attack_power=0,
    )

    engine.state.player = player
    # DM裁定 2026-09-10：属性点可存储，随时兑换（2点=1速限=1法限）
    engine.state.attribute_points = 25 - total
    engine.state.allocated_blood = blood_limit
    engine.state.shards = 20

    engine._init_relic_pool()
    discovery = engine._offer_relic_discovery("开局发现")
    if not discovery.get("success"):
        return discovery

    return {
        "success": True,
        "action": "分配属性点",
        "result": {
            "name": player.name,
            "blood_limit": blood_limit,
            "mana_limit": mana_limit,
            "speed_limit": speed_limit,
            "attack_count": player.effective_attack_count(),
            "attack_power": player.effective_attack_power(),
            "action_count": player.action_count,
            "attribute_points_banked": engine.state.attribute_points,
            "shards": 20,
            "relic_choices": discovery["choices"],
        },
        "next_actions": ["choose_discovered_relic"],
        "note": "属性已分配；开局先发现遗物：请从随机列出的3件遗物候选中显式选择1件，随后再发现初始道纹。",
    }


def handle_setup_choose_region(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """选择副本（开局遗物与初始道纹均已在此前发现完毕）"""
    if engine.state.player is None:
        return {"success": False, "error": "请先分配初始属性"}
    if not engine.state.relics:
        return {"success": False, "error": "选择副本前必须先完成开局遗物发现"}
    if not engine.state.player.dao_wen or sum(engine.state.resonance.values()) != 1:
        return {"success": False, "error": "选择副本前必须先获得初始道纹并选择1种初始残韵"}
    region = params.get("region", "")
    valid = engine.unlocked_regions()
    if region not in valid:
        from ..gamedata import REGION_TIERS
        if region not in getattr(engine, "monster_pool", {}):
            return {"success": False,
                    "error": f"副本【{region}】尚未接入运行时（未实现草案），只能从{valid}中选择"}
        from ..monsters import PROGRESSION_ENABLED
        if (PROGRESSION_ENABLED and not engine.state.endless_mode
                and int(REGION_TIERS.get(region, 1)) > int(engine.state.unlocked_tier)):
            need = int(REGION_TIERS.get(region, 1)) - 1
            return {"success": False,
                    "error": f"副本【{region}】是{REGION_TIERS.get(region, 1)}阶，"
                             f"当前只解锁到{engine.state.unlocked_tier}阶"
                             f"（通过{need}阶最终死斗后解锁下一阶级）；只能从{valid}中选择"}
        return {"success": False, "error": f"只能从{valid}中选择"}
    engine.state.current_region = region
    engine.state.phase = "pre_battle"
    # 过路商人是独立中立角色，不是随机事件；首次进入本局副本时取得5件有限遗物库存。
    engine._initialize_merchant_inventory()
    owned = [r.name for r in engine.state.relics]
    endless_note = ""
    if engine.state.endless_mode:
        endless_note = (f"｜无尽模式第{engine.state.endless_cycle}轮：怪物池=全部已实现副本，"
                        f"局外精力{engine.state.energy_budget}点，【探索】不开放")
    return {
        "success": True,
        "action": "选择副本",
        # relic_choices 为兼容回显：开局遗物已在属性分配后发现并选定（新流程：先遗物后道纹）。
        "result": {"region": region, "relic_choices": owned, "relics_owned": owned,
                   "unlocked_tier": int(engine.state.unlocked_tier),
                   "endless_mode": bool(engine.state.endless_mode),
                   "endless_cycle": int(engine.state.endless_cycle)},
        "next_actions": ["world_initialize"],
        "note": "副本已选择；开局配置完成（遗物与初始道纹均已在此前发现）。"
                "默认从开放地图进入本副本，下一步先执行world_initialize；"
                "仅在用户明确选择旧线性模式时，才继续旧线性局外行动并在精力耗尽后使用battle_start。" + endless_note,
    }


def handle_setup_choose_initial_daowen(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """从开局发现的3个杀伐闭环候选中显式选择1种作为初始道纹。"""
    if engine.state.player is None:
        return {"success": False, "error": "请先分配初始属性"}
    choices = list(engine.state.pending_initial_daowen_choices)
    if not choices:
        return {"success": False, "error": "当前没有待选择的初始道纹发现"}
    if engine.state.player.dao_wen:
        return {"success": False, "error": "初始道纹已经选择，不能重复选择"}
    name = params.get("daowen_name", "")
    if name not in choices:
        return {"success": False, "error": "只能选择本次发现列出的道纹", "choices": choices}
    if name not in SHAFA_LOOP_DAOWEN:
        return {"success": False, "error": f"【{name}】不属于杀伐闭环，不能作为初始道纹"}
    engine._grant_named_daowen(engine.state.player, name)
    engine.state.pending_initial_daowen_choices = []
    engine.state.pending_initial_daowen_source = ""
    return {
        "success": True,
        "action": "选择初始道纹",
        "result": {"daowen": name, "player_daowen": list(engine.state.player.dao_wen)},
        "next_actions": ["setup_choose_resonance"],
    }


def handle_setup_choose_resonance(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """选择初始残韵"""
    if engine.state.player is None:
        return {"success": False, "error": "请先分配初始属性"}
    if not engine.state.player.dao_wen:
        return {"success": False, "error": "选择残韵前必须先发现初始道纹"}
    if sum(engine.state.resonance.values()) > 0:
        return {"success": False, "error": "初始残韵已经选择，不能重复选择"}
    rtype = params.get("resonance_type", "")
    valid = ["转换", "反转", "曲解"]

    if rtype not in valid:
        return {"success": False, "error": f"只能从{valid}中选择"}

    engine.state.resonance[rtype] = engine.state.resonance.get(rtype, 0) + 1

    return {
        "success": True,
        "action": "选择残韵",
        "result": {"resonance_type": rtype, "count": engine.state.resonance[rtype]},
        "next_actions": ["setup_choose_region"]
    }
