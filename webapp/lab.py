"""自定义角色演武场：先配装，再直接开打。

与 ``webapp/server.py`` 的分工：

* 本模块只负责把用户的选择翻译成**一次合法的引擎开局**——三维属性、遗物、
  道纹、消耗品、碎片/精力，以及本场对手；
* 界面仍然只是输入器，所有结算仍走 ``GameEngine.execute_action``。

口径（2026-11-09 用户需求：「手动实时控制角色，设定三维/已有道纹/消耗品后
开始战斗」）：

* 属性默认遵守《规则正文》的 25 点预算；自由模式只在演武场里放开，
  **不写回规则、不改任何事实源**，退出本页即与正式对局无关；
* 道纹/遗物/消耗品只能从运行时已登记的名单里挑，避免造出引擎不认识的条目；
* 指定对手时走真实地图遭遇（``world.start_encounter`` + ``battle_start``），
  不另开一套战斗入口；按副本随机则沿用引擎自己的出怪我配方。
"""
from __future__ import annotations

import os
import tempfile
from typing import Any

from engine.api import GameEngine
from engine.consumables import WING_ITEM_SPECS, make_wing_item
from engine.daowen import DaoWenEngine
from engine.events import EVENT_CONSUMABLES, TWISTED_TOOL_LIBRARY
from engine.gamedata import (
    FIRST_EMBRACE_DAOWEN,
    MONSTER_TRANSFORM_DAOWEN,
    ORIGINAL_MONSTER_DAOWEN,
    RACE_EXCLUSIVE_DAOWEN,
    REGION_EXCLUSIVE_DAOWEN,
    REGION_TIERS,
    SHAFA_LOOP_DAOWEN,
    UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN,
)
from engine.models import Consumable, Relic
from engine.monsters import assign_monster_preferences, make_monster_entity
from engine.world import WorldState

#: 演武场单场最多几只对手（地图遭遇的实际承载上限由 UI 与引擎共同决定）。
MAX_ENEMIES = 6
#: 属性自由模式的人为护栏：防止浏览器提交天文数字把引擎算崩。
MAX_BLOOD_LIMIT = 999
MAX_TIER_LIMIT = 99
MAX_SHARDS = 999_999
MAX_USES = 99

_OPTIONS: dict[str, Any] | None = None


# ---------------------------------------------------------------- 目录


def _daowen_groups() -> list[dict[str, Any]]:
    """把运行时道纹按获得方式分组，供界面勾选。

    「运行时」= ``DaoWenEngine.list_all()``。只在文档里出现、尚未注册效果的道纹
    一律不出现在名单里（用户勾到也结算不了）。
    """
    runtime = list(DaoWenEngine.list_all())
    available = set(runtime)
    groups: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(label: str, names: list[str] | tuple[str, ...], note: str = "") -> None:
        picked = [name for name in dict.fromkeys(names) if name in available and name not in seen]
        if not picked:
            return
        seen.update(picked)
        groups.append({"label": label, "names": picked, "note": note})

    add("通用核心（开局可发现）", list(SHAFA_LOOP_DAOWEN),
        "开局【发现】只从这八种里抽；演武场里可任选。")
    add("原始怪物", sorted(ORIGINAL_MONSTER_DAOWEN))
    add("怪物转化", sorted(MONSTER_TRANSFORM_DAOWEN))
    for region, names in REGION_EXCLUSIVE_DAOWEN.items():
        add(f"{region}专属", sorted(names),
            "未接入运行时的副本道纹同样可在此勾选，仅用于演武场对局。")
    for race, names in RACE_EXCLUSIVE_DAOWEN.items():
        add(f"{race}专属", sorted(names))
    add("初拥之夜", sorted(FIRST_EMBRACE_DAOWEN))
    add("永夜庭（副本未接入）", sorted(UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN))
    add("其他已实装", runtime)
    return groups


def _consumables() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for name, (uses, effect) in TWISTED_TOOL_LIBRARY.items():
        items.append({"name": name, "kind": "工具库", "uses": int(uses), "effect": effect})
    for name, (uses, effect) in EVENT_CONSUMABLES.items():
        items.append({"name": name, "kind": "事件", "uses": int(uses), "effect": effect})
    for name, spec in WING_ITEM_SPECS.items():
        items.append({"name": name, "kind": "翼", "uses": int(spec["max_uses"]),
                      "effect": spec["effect"]})
    return items


def _regions(engine: GameEngine) -> list[dict[str, Any]]:
    regions = []
    for region, pool in engine.monster_pool.items():
        monsters = []
        for monster in pool:
            monsters.append({
                "name": monster["name"],
                "blood_limit": int(monster.get("blood_limit", 0)),
                "speed_limit": int(monster.get("speed_limit",
                                               monster.get("attack_count", 0)) or 0),
                "mana_limit": int(monster.get("mana_limit",
                                              monster.get("attack_power", 0)) or 0),
                "daowen": sorted((monster.get("dao_wen") or {}).keys()),
            })
        regions.append({"name": region, "tier": int(REGION_TIERS.get(region, 1)),
                        "monsters": monsters})
    return regions


def options(refresh: bool = False) -> dict[str, Any]:
    """演武场可选项（进程内缓存；引擎数据不变时无需重算）。"""
    global _OPTIONS
    if _OPTIONS is not None and not refresh:
        return _OPTIONS
    engine = GameEngine(autosave=False, rng_seed=1)
    _OPTIONS = {
        "attributes": {
            "total_points": 25,
            "blood_per_point": 6,
            "points_per_speed": 2,
            "points_per_mana": 2,
            "limits": {"blood": MAX_BLOOD_LIMIT, "speed": MAX_TIER_LIMIT,
                       "mana": MAX_TIER_LIMIT, "shards": MAX_SHARDS, "uses": MAX_USES},
            "note": "规则模式：1 点=6 血限，2 点=1 速限=1 法限，总和不超过 25；"
                    "自由模式直接填三个上限，仅用于演武场。",
        },
        "regions": _regions(engine),
        "resonance": ["转换", "反转", "曲解"],
        "relics": [{"name": name, "effect": effect} for name, effect in GameEngine.RELIC_DEFS],
        "daowen_groups": _daowen_groups(),
        "consumables": _consumables(),
        "max_enemies": MAX_ENEMIES,
        "notes": [
            "演武场只写本进程的临时对局，不修改《规则正文》与任何数据源。",
            "指定对手时，角色会被直接放到地图遭遇里开打；战斗结算与正式对局完全一致。",
        ],
    }
    return _OPTIONS


# ---------------------------------------------------------------- 校验


def _as_int(value: Any, label: str, minimum: int = 0, maximum: int | None = None) -> int:
    if value in (None, ""):
        return minimum
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是整数") from None
    if number < minimum:
        raise ValueError(f"{label}不能小于{minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label}不能大于{maximum}")
    return number


def _as_name_list(value: Any, label: str) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ValueError(f"{label}必须是名字列表")
    names = []
    for entry in value:
        if isinstance(entry, dict):
            entry = entry.get("name")
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"{label}含有空名字")
        names.append(entry.strip())
    return names


# ---------------------------------------------------------------- 开局


def _new_engine(payload: dict[str, Any]) -> GameEngine:
    workdir = tempfile.mkdtemp(prefix="linji-lab-")
    seed = payload.get("seed")
    return GameEngine(
        db_path=os.path.join(workdir, "rulings.db"),
        save_dir=os.path.join(workdir, "saves"),
        sealed_candidate_path=os.path.join(workdir, "sealed.json"),
        death_book_path=os.path.join(workdir, "death_book.md"),
        rng_seed=int(seed) if seed not in (None, "") else None,
        autosave=False,
    )


def _set_limits(player: Any, blood: int, speed: int, mana: int) -> None:
    """自由模式：直接改三个上限，并把当前值回满。

    先写上限、再写当前值——实体绑定战斗引擎后 ``__setattr__`` 会把当前值压到
    上限以内，顺序反过来会白写一次。
    """
    player.blood_limit = max(1, blood)
    player.current_hp = player.blood_limit
    player.speed_limit = max(0, speed)
    player.current_speed = player.speed_limit
    player.mana_limit = max(0, mana)
    player.current_mana = player.mana_limit


def _battle_slots(world: WorldState, origin: tuple[int, int], count: int) -> list[tuple[int, int]]:
    """找 ``count`` 个离玩家最近的空可通行格，供指定对手入场。"""
    occupied = {actor.position for actor in world.actors.values()}
    candidates: list[tuple[int, int]] = []
    width, height = world.map.width, world.map.height
    for y in range(height):
        for x in range(width):
            cell = (x, y)
            if cell == origin or cell in occupied or not world.map.is_passable(cell):
                continue
            candidates.append(cell)
    candidates.sort(key=lambda cell: (abs(cell[0] - origin[0]) + abs(cell[1] - origin[1]),
                                      cell[1], cell[0]))
    if len(candidates) < count:
        raise ValueError(f"地图太小，放不下{count}个对手；请换个更大的地图尺寸")
    return candidates[:count]


# ---------------------------------------------------------------- 角色卡

try:  # 包导入（from webapp import lab）与脚本导入（server.py 的 sys.path 口径）都支持
    from . import characters as roster
except ImportError:  # pragma: no cover - 只在直接跑脚本时走到
    import characters as roster


def _sheet_limits(sheet: dict[str, Any]) -> tuple[int, int, int]:
    """角色卡的三维上限（血限/速限/法限）。"""
    if sheet["mode"] == "rule":
        return (int(sheet["blood_points"]) * 6,
                int(sheet["speed_points"]) // 2,
                int(sheet["mana_points"]) // 2)
    return (int(sheet["blood_limit"]), int(sheet["speed_limit"]),
            int(sheet["mana_limit"]))


def _check_daowen(names: list[str]) -> list[str]:
    runtime = set(DaoWenEngine.list_all())
    unknown = [name for name in names if name not in runtime]
    if unknown:
        raise ValueError(f"未实装的道纹：{'、'.join(unknown)}")
    return list(dict.fromkeys(names))


def _check_relics(names: list[str]) -> list[str]:
    defs = dict(GameEngine.RELIC_DEFS)
    unknown = [name for name in names if name not in defs]
    if unknown:
        raise ValueError(f"未知道的遗物：{'、'.join(unknown)}")
    return list(dict.fromkeys(names))


def _resolve_consumables(entries: list[Any]) -> list[dict[str, Any]]:
    """把「名字」或 ``{name, uses}`` 统一成 ``{name, uses}``（缺省取登记耐久）。"""
    catalog = {item["name"]: item for item in _consumables()}
    resolved = []
    for entry in entries:
        if isinstance(entry, str):
            name, uses = entry, None
        elif isinstance(entry, dict):
            name, uses = entry.get("name"), entry.get("uses")
        else:
            raise ValueError("消耗品必须是名字或 {name, uses}")
        if name not in catalog:
            raise ValueError(f"未登记的消耗品：{name}")
        if uses in (None, ""):
            uses = catalog[name]["uses"]
        resolved.append({"name": name, "uses": _as_int(uses, f"{name}的耐久", 1, MAX_USES)})
    return resolved


def normalize_sheet(payload: dict[str, Any]) -> dict[str, Any]:
    """把请求体/旧存档规范成一张角色卡配方（只校验，不碰引擎）。"""
    if not isinstance(payload, dict):
        raise ValueError("角色卡必须是对象")
    name = str(payload.get("name") or "轮回者").strip()[:12] or "轮回者"
    mode = "free" if str(payload.get("mode") or "rule") == "free" else "rule"
    resonance = str(payload.get("resonance") or "转换")
    if resonance not in options()["resonance"]:
        raise ValueError(f"残韵必须从 {options()['resonance']} 中选择")

    if mode == "rule":
        blood_points = _as_int(payload.get("blood_points", 11), "血限点", 0, 25)
        speed_points = _as_int(payload.get("speed_points", 8), "速限点", 0, 25)
        mana_points = _as_int(payload.get("mana_points", 6), "法限点", 0, 25)
        if blood_points + speed_points + mana_points > 25:
            raise ValueError("规则模式下三项加点之和不能超过 25")
        if speed_points % 2 or mana_points % 2:
            raise ValueError("速限/法限按 2 点一档计价，点数必须是偶数")
        sheet = {
            "mode": "rule",
            "blood_points": blood_points, "speed_points": speed_points,
            "mana_points": mana_points,
            "attribute_points": 25 - blood_points - speed_points - mana_points,
        }
    else:
        sheet = {
            "mode": "free",
            "blood_points": 0, "speed_points": 0, "mana_points": 0,
            "blood_limit": _as_int(payload.get("blood_limit", 90), "血限", 1, MAX_BLOOD_LIMIT),
            "speed_limit": _as_int(payload.get("speed_limit", 5), "速限", 0, MAX_TIER_LIMIT),
            "mana_limit": _as_int(payload.get("mana_limit", 5), "法限", 0, MAX_TIER_LIMIT),
            "attribute_points": _as_int(payload.get("attribute_points", 0), "属性点池", 0, 999),
        }
    blood, speed, mana = _sheet_limits({**sheet, "name": name})
    sheet.update({
        "id": str(payload.get("id") or "").strip(),
        "name": name,
        "resonance": resonance,
        # 规则模式的三个上限是派生值；存下来只为列表/对战展示，不当作独立事实源。
        "blood_limit": blood, "speed_limit": speed, "mana_limit": mana,
        "daowen": _check_daowen(_as_name_list(payload.get("daowen"), "道纹")) or ["杀伐"],
        "relics": _check_relics(_as_name_list(payload.get("relics"), "遗物")),
        "consumables": _resolve_consumables(payload.get("consumables") or []),
        "shards": _as_int(payload.get("shards", 20), "碎片", 0, MAX_SHARDS),
        "energy": _as_int(payload.get("energy", 3), "精力", 0, MAX_TIER_LIMIT),
    })
    return sheet


def save_sheet(payload: dict[str, Any]) -> dict[str, Any]:
    """保存 / 修改一张角色卡。"""
    sheet = normalize_sheet(payload)
    if not sheet["daowen"]:
        sheet["daowen"] = ["杀伐"]
    return roster.save_character(sheet)


def list_sheets() -> list[dict[str, Any]]:
    return roster.list_characters()


def delete_sheet(character_id: str) -> bool:
    return roster.delete_character(str(character_id or ""))


# ---------------------------------------------------------------- 开局装配


def _regions_by_name() -> dict[str, dict[str, Any]]:
    return {region["name"]: region for region in options()["regions"]}


def _replace_relics(engine: GameEngine, names: list[str]) -> list[str]:
    """把开局随机发现的遗物换成用户勾选的名单。

    启用槽位沿用引擎口径 ``ceil(血限/30)``：超出的照常持有、只是停用。
    """
    defs = dict(GameEngine.RELIC_DEFS)
    engine.state.pending_relic_choices = []
    engine.state.pending_relic_source = ""
    limit = engine.state.player.enabled_relic_limit
    engine.state.relics = [
        Relic(name=name, effect=defs[name], enabled=index < limit)
        for index, name in enumerate(names)
    ]
    taken = {relic.name for relic in engine.state.relics}
    pool = list(getattr(engine.state, "relics_pool", []) or [])
    engine.state.relics_pool = [relic for relic in pool if relic.name not in taken]
    return [relic.name for relic in engine.state.relics]


def _grant_consumables(engine: GameEngine, resolved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    catalog = {item["name"]: item for item in _consumables()}
    granted = []
    for entry in resolved:
        name, uses = entry["name"], int(entry["uses"])
        spec = catalog[name]
        if name in WING_ITEM_SPECS:
            item = make_wing_item(name)
            item.current_uses = uses
            item.max_uses = max(uses, int(WING_ITEM_SPECS[name]["max_uses"]))
        else:
            item = Consumable(name=name, effect=spec["effect"],
                              current_uses=uses, max_uses=uses)
        engine.state.consumables.append(item)
        granted.append({"name": name, "uses": item.current_uses, "max_uses": item.max_uses})
    return granted


def _apply_loadout(engine: GameEngine, sheet: dict[str, Any], region: str,
                   notes: list[str]) -> list[dict[str, Any]]:
    """按角色卡把引擎配置成开局状态（属性→遗物→道纹→残韵→副本→资源）。"""
    name = sheet["name"]
    if sheet["mode"] == "rule":
        setup = engine.execute_action("setup_attributes", {
            "name": name, "blood_points": sheet["blood_points"],
            "speed_points": sheet["speed_points"], "mana_points": sheet["mana_points"],
        })
    else:
        # 先用一组合法点数过开局流程，再把三个上限直接写成用户要的值。
        setup = engine.execute_action("setup_attributes", {
            "name": name, "blood_points": 25, "speed_points": 0, "mana_points": 0,
        })
        if setup.get("success"):
            blood, speed, mana = _sheet_limits(sheet)
            _set_limits(engine.state.player, blood, speed, mana)
            engine.state.attribute_points = int(sheet["attribute_points"])
            notes.append(f"自由模式：三维 {blood}/{speed}/{mana}（血限/速限/法限）"
                         f"已直接写入，不经过 25 点预算。")
    if not setup.get("success"):
        raise ValueError(f"属性分配失败：{setup.get('error')}")

    relics = list(sheet["relics"])
    if not relics:
        # 没选就沿用引擎本次开局随机发现的第一件（副本选择要求至少持有一件）。
        relics = list(engine.state.pending_relic_choices)[:1]
        if relics:
            notes.append(f"未选遗物，沿用开局随机发现的第一件：{relics[0]}")
    if relics:
        _replace_relics(engine, relics)

    for daowen_name in sheet["daowen"]:
        engine._grant_named_daowen(engine.state.player, daowen_name)

    chosen = engine.execute_action("setup_choose_resonance",
                                   {"resonance_type": sheet["resonance"]})
    if not chosen.get("success"):
        raise ValueError(f"残韵选择失败：{chosen.get('error')}")

    # 演武场不推进阶级：把已实现副本全部解锁，省得用户还要先打一场死斗。
    engine.state.unlocked_tier = max(int(REGION_TIERS.get(key, 1)) for key in _regions_by_name())
    entered = engine.execute_action("setup_choose_region", {"region": region})
    if not entered.get("success"):
        raise ValueError(f"进入副本失败：{entered.get('error')}")

    granted = _grant_consumables(engine, sheet["consumables"])
    engine.state.shards = int(sheet["shards"])
    engine.state.energy = int(sheet["energy"])
    return granted


def _monster_entities(engine: GameEngine, names: list[str]) -> list[Any]:
    pool = {monster["name"]: monster for monster in engine._monster_pool_for_battle()}
    unknown = [name for name in names if name not in pool]
    if unknown:
        raise ValueError(f"当前副本没有这些怪物：{'、'.join(unknown)}")
    entities = []
    for name in names:
        monster = make_monster_entity(pool[name])
        assign_monster_preferences(monster, engine._current_tier(), engine.dice)
        engine.combat.init_monster_shards(monster)
        entities.append(monster)
    return entities


def _make_rival(engine: GameEngine, sheet: dict[str, Any], a_name: str) -> Any:
    """把一张角色卡造成 B 方实体（轮回者）。

    与 A 方共用同一套口径：三维直接写上限，道纹/残韵/消耗品挂在该实体上。
    遗物同时写两处：

    * ``rival.relics``：实体自己的持有记录（展示、以及怪物/队友口径的回退读取）；
    * ``state.opponent_relics``：引擎给「敌方轮回者」准备的阵营遗物通道
      （死斗同款）。战斗里绝大多数遗物判定走 ``state.side_has`` / ``_relic_active``，
      它们对敌方轮回者读的正是这份名单——不登记进去，B 方遗物就只是摆设。
    """
    from engine.models import Entity

    blood, speed, mana = _sheet_limits(sheet)
    name = sheet["name"]
    if name == a_name:
        name = f"{name}·B"
    rival = Entity(
        name=name, entity_type="轮回者",
        blood_limit=blood, current_hp=blood,
        speed_limit=speed, current_speed=speed,
        mana_limit=mana, current_mana=mana,
    )
    for daowen_name in sheet["daowen"]:
        engine._grant_named_daowen(rival, daowen_name)
    if sheet["relics"]:
        defs = dict(GameEngine.RELIC_DEFS)
        # 启用槽位与 A 方同口径：ceil(血限/30)，超出的照常持有但停用。
        limit = rival.enabled_relic_limit
        squad = [
            Relic(name=relic_name, effect=defs.get(relic_name, ""), enabled=index < limit)
            for index, relic_name in enumerate(dict.fromkeys(sheet["relics"]))
        ]
        rival.relics.extend(squad)
        engine.state.opponent_relics = squad
    catalog = {item["name"]: item for item in _consumables()}
    for entry in sheet["consumables"]:
        spec = catalog[entry["name"]]
        rival.consumables.append(Consumable(
            name=entry["name"], effect=spec["effect"],
            current_uses=int(entry["uses"]), max_uses=int(entry["uses"])))
    if sheet["resonance"]:
        rival.resonance[sheet["resonance"]] = 1
    return rival


# ---------------------------------------------------------------- 开战


def _start_custom_battle(engine: GameEngine, entities: list[Any],
                         width: int, height: int) -> dict[str, Any]:
    """按指定对手开场：先建地图，再把对手摆到玩家身边并登记遭遇。"""
    if not entities:
        raise ValueError("没有对手，无法开战")
    engine.state.enemies = list(entities)

    initialized = engine.execute_action("world_initialize", {"width": width, "height": height})
    if not initialized.get("success"):
        raise ValueError(f"地图初始化失败：{initialized.get('error')}")

    world = WorldState.from_dict(engine.state.world_state)
    player = engine.state.player
    player_actor = world.actors.get(player.runtime_id)
    if player_actor is None:
        raise ValueError("玩家没有登记到地图上，无法开战")
    origin = player_actor.position
    for entity, cell in zip(engine.state.enemies,
                            _battle_slots(world, origin, len(engine.state.enemies))):
        actor = world.actors.get(entity.runtime_id)
        if actor is None:
            continue
        actor.x, actor.y = cell
        actor.mode = "manual"
        entity.world_x, entity.world_y = cell
    encounter = world.start_encounter(
        [player.runtime_id] + [entity.runtime_id for entity in engine.state.enemies],
        origin)
    engine.state.world_state = world.to_dict()

    started = engine.execute_action("battle_start", {
        "world_encounter_id": encounter.encounter_id,
        "relic_choices": {},
    })
    if not started.get("success"):
        raise ValueError(f"开战失败：{started.get('error')}")
    return {
        "mode": "指定对手",
        "encounter_id": encounter.encounter_id,
        "enemies": [entity.name for entity in engine.state.enemies],
    }


def _start_random_battle(engine: GameEngine) -> dict[str, Any]:
    """按副本随机：沿用引擎出怪配方，不需要地图。"""
    engine.state.energy = 0
    started = engine.execute_action("battle_start", {"relic_choices": {}})
    if not started.get("success"):
        raise ValueError(f"开战失败：{started.get('error')}")
    return {
        "mode": "按副本随机",
        "encounter_id": "",
        "enemies": [monster.name for monster in engine.state.enemies],
    }


def _resolve_side(side: Any, label: str) -> dict[str, Any]:
    """解析 A/B 方：``{id}`` 取存档卡，或直接内联一张卡。"""
    if not isinstance(side, dict):
        raise ValueError(f"{label}没有选择角色")
    inner = side.get("character") if isinstance(side.get("character"), dict) else side
    if side.get("id") and not any(key in inner for key in ("mode", "blood_points", "blood_limit")):
        record = roster.get_character(str(side["id"]))
        if record is None:
            raise ValueError(f"{label}的角色卡不存在（可能已被删除）")
        inner = record
    if not inner.get("name"):
        raise ValueError(f"{label}还没选角色")
    return normalize_sheet(inner)


def start(payload: dict[str, Any]) -> dict[str, Any]:
    """按一份配装开一局单人演武场，返回 ``{"engine": ..., "notes": [...]}``。

    抛 ``ValueError`` 表示是用户输入问题（界面直接显示）；其它异常按 500 处理。
    """
    sheet = normalize_sheet(payload)
    regions = _regions_by_name()
    region = str(payload.get("region") or "")
    if region not in regions:
        raise ValueError(f"副本必须从 {list(regions)} 中选择")

    engine = _new_engine(payload)
    notes: list[str] = []
    granted = _apply_loadout(engine, sheet, region, notes)

    width = _as_int(payload.get("width", 9), "地图宽", 5, 41)
    height = _as_int(payload.get("height", width), "地图高", 5, 41)

    if payload.get("start_battle"):
        enemies = _as_name_list(payload.get("enemies"), "对手")
        if len(enemies) > MAX_ENEMIES:
            raise ValueError(f"指定对手最多 {MAX_ENEMIES} 只")
        battle = (_start_custom_battle(engine, _monster_entities(engine, enemies),
                                       width, height) if enemies
                  else _start_random_battle(engine))
        notes.append(f"{battle['mode']}：{'、'.join(battle['enemies']) or '（无）'}")

    player = engine.state.player
    notes.append(
        f"{player.name} · 血限{player.blood_limit} 速限{player.speed_limit} "
        f"法限{player.mana_limit} · 道纹{len(player.dao_wen)} 种 · "
        f"遗物{len(engine.state.relics)} 件 · 消耗品{len(granted)} 件")
    return {"engine": engine, "notes": notes}


def duel(payload: dict[str, Any]) -> dict[str, Any]:
    """A 方 vs B 方：两边都填角色卡（B 方也可以填副本怪物）。"""
    regions = _regions_by_name()
    region = str(payload.get("region") or "")
    if region not in regions:
        raise ValueError(f"副本必须从 {list(regions)} 中选择")

    a_sheet = _resolve_side(payload.get("a"), "A方")
    b_side = payload.get("b") if isinstance(payload.get("b"), dict) else {}
    b_is_monster = str(b_side.get("kind") or "") == "monster"
    b_names: list[str] = []
    b_sheet: dict[str, Any] | None = None
    if b_is_monster:
        b_names = _as_name_list(b_side.get("monsters"), "B方怪物")
        if not b_names:
            raise ValueError("B方还没有选怪物")
        if len(b_names) > MAX_ENEMIES:
            raise ValueError(f"B方最多 {MAX_ENEMIES} 只怪物")
    else:
        b_sheet = _resolve_side(b_side, "B方")

    engine = _new_engine(payload)
    notes: list[str] = []
    _apply_loadout(engine, a_sheet, region, notes)

    if b_is_monster:
        entities = _monster_entities(engine, b_names)
    else:
        assert b_sheet is not None
        entities = [_make_rival(engine, b_sheet, a_sheet["name"])]

    width = _as_int(payload.get("width", 11), "地图宽", 5, 41)
    height = _as_int(payload.get("height", width), "地图高", 5, 41)
    battle = _start_custom_battle(engine, entities, width, height)

    a_blood, a_speed, a_mana = _sheet_limits(a_sheet)
    notes.append(f"A方 {a_sheet['name']}：{a_blood}/{a_speed}/{a_mana}（血限/速限/法限）")
    if b_sheet is not None:
        b_blood, b_speed, b_mana = _sheet_limits(b_sheet)
        notes.append(f"B方 {b_sheet['name']}：{b_blood}/{b_speed}/{b_mana}")
        if b_sheet["relics"]:
            enabled = min(len(b_sheet["relics"]), max(0, (b_blood + 29) // 30))
            notes.append(
                f"B方遗物按敌方轮回者通道（opponent_relics）生效："
                f"启用 {enabled}/{len(b_sheet['relics'])} 件；"
                "回锋刀[回始]、减速、忘忧香、无所求、同魂笔 仍以玩家为中枢，只对 A 方生效。")
    else:
        notes.append(f"B方怪物：{'、'.join(b_names)}")
    notes.append(f"开战：{'、'.join(battle['enemies'])}")
    return {"engine": engine, "notes": notes}
