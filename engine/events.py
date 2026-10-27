"""
事件系统
解析规则正文（规则正文.md）中的通用事件池与各副本专属事件，构建事件池，触发与结算。
规则：当前事件池 = 所有未遇到的通用事件 + 当前区域中符合条件且未遇到的专属事件（通用在前）。
"""
from __future__ import annotations
import os
import re
import math
from pathlib import Path
from typing import Optional

from .document_sources import COMMON_RULES_FILE
from .dungeons import load_dungeon_documents
from .resolution import KIND_EFFECT, resolution_frame


# 各池事件名（与规则正文一致）
EVENT_RELICS = {
    # 2026-10-14 用户令重做：旧「战始可选流血10 → 战终血限+2」移交道纹【增殖】，改为被动。
    "猩红果实": "你的当前生命可以超出[血限]",
    # 2026-10-11 用户令重做：旧「战始可选疲惫5 → 战终精力+1」废止，改为被动。
    "苍白之花": "你的当前法力可以超出[法限]",
    "缄默面具": "无法再使用任何附带「代价」的道纹，每场[战终][法限]+X",
    "焦黑发丝": "每当场上有一个怪物死亡时，你的速度+2",
    "皮衣": "上回合失去生命时，下回合获得等量格挡",
    "帮派令": "[战始]获得【洗劫3】",
    "防弹插板": "[血限]+10，且[战始]获得15格挡",
    "负岳索": "[战始]选择一名[朋友]或[员工]；其首次受到伤害时，你[回复]等量生命",
    "炉心坠": "[战始]选择一枚自身拥有的龙心，使其当前耐久+10",
    "烙痕钉": "[战始]必中，选择一个[目标]；你每付出一次代价，其失去10生命",
    # 2026-10-14 用户令：【余火印】整段删除（原 extra 并入方案废弃，龙心只走通用抵扣）。
}

EVENT_CONSUMABLES = {
    "绝息淤泥": (1, "使用后屏蔽自身灵魂位置，使本次[战终]立刻逃脱"),
    "活性土壤": (1, "[战始]可失去X法力，以X点属性点打造一名[朋友]"),
    "假钞贴": (2, "使用后获得20[假碎片]"),
    "穿甲弹": (2, "对[目标]造成15点忽略格挡与闪避的伤害"),
    "怒火针": (1, "对一名存活目标施加愤怒，使其法力消耗减半，持续2回合"),
    "洗劫面具": (2, "使自身下2次攻击附带【必中】"),
    "赤泉囊": (6, "局外使用后产生8点恢复量；下两场[战始]失去4生命"),
    "龙血瓶": (10, "储存超出血限的回复量，并可自由提取分配"),
}

EVENT_NAMES = {
    # 过路商人的事件定义保留供地图接触时打开交易会话；build_pool会将其排除于随机事件池。
    "通用": ["遗忘书屋", "祭坛", "过路商人", "猩红暴雨", "无名碑林", "回音长廊", "回忆当铺", "无魂泥潭"],
    "扭曲都市": ["医生", "乞丐", "血肉温室", "绝望来电", "皮衣店", "生锈邮筒", "尖叫下水道", "恶魔医务室"],
    "罪孽都市": ["遗落的赌局", "高利贷钱庄", "地下角斗场", "黑市军火贩", "通缉悬赏榜", "假钞印钞厂", "帮派断指酒吧"],
    "龙心谷": ["断桥余烬", "熔炉余火", "逆行者", "裂隙温泉", "追求者", "埋骨之地"],
    "乱葬岗": ["纸人冥婚", "镇尸棺材钉", "悬木红煞", "孤坟香案", "赶尸栈房", "无名将军墓", "无名冢"],
}


# 规则正文中承载「通用事件池」的文件。2026-10-09 精确中文规则自 README.md
# 迁至「规则正文.md」：README 只作入口与速览。此处必须跟着规范文件名——指错文件
# 不会报错，只会让 10 个通用事件静默解析不出来（实测：resolve_event 一律回
# 「未知事件: 祭坛」，一阶探索也探不到任何通用事件）。集中定义见 document_sources。
# 保留 RULES_TEXT_FILE 名称，供现有调用方/测试兼容。
RULES_TEXT_FILE = COMMON_RULES_FILE


def load_event_sources(index_path: str | Path) -> tuple[str, dict]:
    """读取事件解析的全部输入：规则正文 + 已实现副本文档正文。

    规则正文（规则正文.md「第四宇宙规则正文」）承载通用事件，副本文档承载副本
    专属事件，两者合起来就是事件解析的**唯一**输入。缓存层（rule_repository）
    用同一份输入算摘要，避免「缓存读了哪几个文件」与「解析读了哪几个文件」
    各写一份而漂移。
    """
    index = Path(index_path)
    root = index.parent
    content = (root / RULES_TEXT_FILE).read_text(encoding="utf-8")
    documents = load_dungeon_documents(index)
    return content, documents


def parse_events(index_path: str | Path) -> dict:
    """从全副本索引及副本文档解析事件。通用事件位于规则正文.md。"""
    content, documents = load_event_sources(index_path)
    lines = content.split("\n")
    # 每个专属副本独立文档追加到解析输入；事件名白名单阻止标题被误判。
    for document in documents.values():
        lines.extend(["", *document.split("\n")])
    # 建立 name→region 反查
    name_region = {}
    for region, names in EVENT_NAMES.items():
        for n in names:
            name_region[n] = region
    all_names = set(name_region.keys())
    events = {}
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # 检测事件名行：含某事件名 + 描述符（：或换行后描述）
        matched_name = None
        for n in all_names:
            # 行以 "name"： 或 name： 开头，或行就是 name（无冒号，描述在下一行）
            if re.match(rf'^["“]?{re.escape(n)}["”]?\s*[：:]', line) or line.strip('“”"') == n:
                matched_name = n
                break
        if matched_name:
            region = name_region[matched_name]
            desc = line
            options = []
            j = i + 1
            # 读描述（若上行只有名字）+ 选项
            while j < len(lines):
                lj = lines[j].strip()
                if not lj:
                    # 空行：若已有选项则事件结束；否则可能是描述段间空行
                    if options:
                        break
                    j += 1
                    continue
                om = re.match(r'^(\d+)\.\s*(.+)', lj)
                if om:
                    options.append({"id": int(om.group(1)), "text": om.group(2)})
                    j += 1
                    continue
                # 非选项非空行：若遇到下一个事件名则结束；否则视为描述续行
                next_name = None
                for n in all_names:
                    if re.match(rf'^["“]?{re.escape(n)}["”]?\s*[：:]', lj) or lj.strip('“”"') == n:
                        next_name = n; break
                if next_name:
                    break
                if not options:
                    desc += lj  # 描述续行
                j += 1
            events[matched_name] = {"region": region, "desc": desc, "options": options}
            i = j
        else:
            i += 1
    return events


class EventPool:
    """事件池：跟踪已触发，按区域构建当前池"""
    def __init__(self, events: dict):
        self.events = events
        self.triggered: set[str] = set()
        self.current: Optional[str] = None  # 当前待结算的事件

    def build_pool(self, region: str) -> list[str]:
        """当前池 = 未触发通用 + 未触发本区域专属（通用在前）"""
        # 过路商人是地图上的独立中立角色，不属于随机地点事件池；由世界地图相遇逻辑显式打开交易会话。
        pool = [n for n in EVENT_NAMES["通用"]
                if n in self.events and n not in self.triggered and n != "过路商人"]
        pool += [n for n in EVENT_NAMES.get(region, [])
                 if n in self.events and n not in self.triggered and n != "过路商人"]
        return pool

    def resolve(self, name: str):
        """标记事件已触发；祭坛可重复投资，过路商人可在地图上反复遭遇。"""
        if name not in {"祭坛", "过路商人"}:
            self.triggered.add(name)
        if self.current == name:
            self.current = None


def _event_preflight(text: str, engine, params: dict) -> Optional[str]:
    """在任何支付或随机前校验条件与完整参数。"""
    player = engine.state.player
    if player is None:
        return "没有玩家，无法结算事件"

    def _positive_x() -> Optional[int]:
        x = params.get("x")
        return x if isinstance(x, int) and not isinstance(x, bool) and x > 0 else None

    x = _positive_x()
    if any(token in text for token in ("流血X", "失忆X", "押注X")) and x is None:
        return "该选项必须显式提交正整数x"

    bleed = sum(int(v) for v in re.findall(r"流血\s*(\d+)", text))
    if "流血X" in text and x is not None:
        bleed += x
    aging = sum(int(v) for v in re.findall(r"衰老\s*(\d+)", text))
    exhaustion = sum(int(v) for v in re.findall(r"枯竭\s*(\d+)", text))
    shrink = sum(int(v) for v in re.findall(r"萎缩\s*(\d+)", text))
    fatigue = sum(int(v) for v in re.findall(r"疲惫\s*(\d+)", text))
    cost_share_ref = params.get("cost_share_target_ref", "")
    numeric_costs = {
        "流血": bleed, "衰老": aging, "枯竭": exhaustion,
        "萎缩": shrink, "疲惫": fatigue,
    }
    try:
        for cost_type, amount in numeric_costs.items():
            if amount > 0:
                engine.combat.validate_numeric_cost(
                    player, cost_type, amount, cost_share_ref)
    except ValueError as exc:
        return str(exc)
    if cost_share_ref and not any(numeric_costs.values()):
        return "该事件选项没有可由【血契】共同承担的数值代价"

    shard_cost = sum(int(v) for v in re.findall(r"(?:失去|消耗)\s*(\d+)\s*\[?碎片\]?", text))
    if params.get("_event_name") == "医生" and text.startswith("雇佣医生"):
        shard_cost = 10  # 后半句5碎片是未来升级价格，不是本选项即时支出。
    # 赌局明确允许负债，单独由具名分支校验-50边界。
    if event_name := params.get("_event_name"):
        allow_debt = event_name == "遗落的赌局"
    else:
        allow_debt = False
    # 负债口径：冻结的是碎片**支出**(shard_cost>0)；
    # 0费选项不属于支出，不得因负债被误拒——否则负债玩家遇到全收费选项事件
    # 时事件永不可结算，而待结算事件门禁一切其它行动（api.py:742）=全死锁。
    if shard_cost > 0 and shard_cost > engine.state.shards and not allow_debt:
        return f"碎片不足，无法支付{shard_cost}（当前{engine.state.shards}）"

    event_name = params.get("_event_name", "")
    if event_name == "祭坛" and text.startswith("投资"):
        x = params.get("x", params.get("principal", params.get("amount")))
        # 旧调用方未传倍率时兼容按最低倍率1结算；新接口仍可显式选择1..上限。
        multiplier = params.get("multiplier", 1)
        if not isinstance(x, int) or isinstance(x, bool) or x < 1:
            return "祭坛投资必须显式提交正整数x"
        if x > engine.state.shards:
            return f"碎片不足，无法投入{x}（当前{engine.state.shards}）"
        max_multiplier = 10 + (x // 100)
        if (not isinstance(multiplier, int) or isinstance(multiplier, bool)
                or multiplier < 1 or multiplier > max_multiplier):
            return f"祭坛倍率必须是1到{max_multiplier}之间的整数（本金{x}，每100本金提高1点倍率上限）"
    if event_name == "过路商人" and text.startswith("遗物交易"):
        direction = params.get("trade_direction")
        relic_name = params.get("relic_name", "")
        price = params.get("price")
        if direction not in {"buy", "sell"}:
            return "遗物交易必须指定trade_direction为buy或sell"
        if not isinstance(relic_name, str) or not relic_name:
            return "遗物交易必须指定relic_name"
        if not isinstance(price, int) or isinstance(price, bool) or price < 0:
            return "遗物交易必须提交非负整数price"
        if params.get("confirmed") is not True:
            return "必须在成交前显式确认交易双方、遗物名称与碎片价格（confirmed=true）"
        if isinstance(price, bool) or not isinstance(price, int) or price < 0:
            return "交易价格必须是非负整数碎片"
        merchant = getattr(engine.state, "merchant", None)
        if merchant is None:
            return "过路商人尚未进入本局副本，当前没有可交易的商人库存"
        if direction == "buy":
            if any(r.name == relic_name for r in engine.state.relics):
                return "你已经持有这件遗物，不可重复购买"
            if not any(r.name == relic_name for r in merchant.relics):
                return "过路商人当前没有这件遗物；售出后不会自动补货"
            if price > engine.state.shards:
                return f"你没有足够碎片购买该遗物：需要{price}，当前{engine.state.shards}"
            # 2026-10-22 用户令：遗物持有不再受承载拦截，可照常买入；
            # 生效数超承载时先不生效，[战始]用 active_relics 自定义启用。
        else:
            if not any(r.name == relic_name for r in engine.state.relics):
                return "你当前没有这件可出售遗物"
            if any(r.name == relic_name for r in merchant.relics):
                return "商人已经持有同名遗物，交易不能复制遗物"
            if price > merchant.shards:
                return f"商人碎片不足，无法支付{price}（商人当前{merchant.shards}）"
    if ("销毁一件当前遗物" in text or "失去一件当前遗物" in text):
        relic_name = params.get("relic_name", "")
        if not isinstance(relic_name, str) or not any(r.name == relic_name for r in engine.state.relics):
            return "必须用relic_name显式指定一件当前持有的遗物"
    memory_cost = sum(int(v) for v in re.findall(r"失忆\s*(\d+)", text))
    if "失忆X" in text and x is not None:
        memory_cost += x
    if memory_cost:
        names = params.get("daowen_names")
        if (not isinstance(names, list) or len(names) != memory_cost or len(set(names)) != memory_cost
                or any(n not in player.dao_wen for n in names)):
            return f"必须用daowen_names显式指定{memory_cost}种当前持有的不同道纹"
    if "自选一件遗物" in text:
        relic_name = params.get("relic_name", "")
        engine._init_relic_pool()
        if not isinstance(relic_name, str) or not any(r.name == relic_name for r in engine.state.relics_pool):
            return "必须用relic_name显式指定遗物池中的一件遗物"
    if "选择获得两种法术定义" in text:
        names = params.get("spell_names")
        if (not isinstance(names, list) or len(names) != 2 or len(set(names)) != 2
                or any(n not in engine.SPELL_REGISTRY for n in names)):
            return "必须用spell_names显式提交两种不同的合法法术"
    if "使一名[朋友]获得[防弹插板]" in text:
        legal = {f"friend:{i}" for i, friend in enumerate(engine.state.friends) if friend.is_alive}
        if params.get("friend_ref") not in legal:
            return "必须用friend_ref显式指定一名存活[朋友]"
    return None


def resolve_option_effect(text: str, engine, event_name: str = "", params=None) -> dict:
    """事件选项效果的公开入口（开帧后转实现体 `_resolve_option_effect_impl`）。"""
    with resolution_frame(engine, KIND_EFFECT, "选项", event_name or text[:12]):
        return _resolve_option_effect_impl(text, engine, event_name, params)


def _resolve_option_effect_impl(text: str, engine, event_name: str = "", params=None) -> dict:
    """
    结算事件选项效果（关键字解释器）。
    自动扣除常见代价（流血/失去碎片/衰老/枯竭/失去精力）与应用常见收益（获碎片/血限/残韵/遗物/法术）。
    无法解析的特殊效果返回 instruction 交DM。
    event_name不为空时，优先匹配需要精确具名结算的专属事件分支(如龙心谷"追求者")，
    这类事件的收益包含固定面板与固定道纹数值，无法用通用正则安全推断，需按事件名单独处理。
    """
    from .models import Relic, Consumable, DaoWen, DaoWenInstance, Spell, Entity
    player = engine.state.player
    applied = []
    instructions = []
    params = dict(params or {})
    params["_event_name"] = event_name
    preflight_error = _event_preflight(text, engine, params)
    if preflight_error:
        return {"applied": [], "instructions": [], "error": preflight_error}
    creative = (
        (event_name == "无名冢" and "设计一种新的遗物" in text)
        or (event_name == "过路商人" and text.startswith("限制选择权"))
        or (event_name == "绝望来电" and text.startswith("接听"))
        or (event_name == "生锈邮筒" and text.startswith("写信"))
    )
    if creative and not params.get("dm_approved"):
        return {"applied": [], "instructions": [],
                "interrupt_required": {"event": event_name, "option": text, "params": params}}

    def _record_shards_peak() -> None:
        engine.state.shards_peak = max(
            int(getattr(engine.state, "shards_peak", 0)),
            max(0, int(engine.state.shards)))

    def _pay_numeric(cost_type: str, amount: int) -> dict:
        return engine.combat.pay_numeric_cost(
            player, cost_type, amount,
            cost_share_target_ref=params.get("cost_share_target_ref", ""),
            cost_context={"timing": "pre_battle_event", "source": event_name or "事件", "source_type": "event", "tags": {"active_payment"}})

    # ---- 祭坛：玩家自选倍率，随机整数本身就是最终盈亏倍数 ----
    if event_name == "祭坛" and text.startswith("投资"):
        principal = int(params.get("x", params.get("principal", params.get("amount"))))
        multiplier = int(params.get("multiplier", 1))
        engine.state.shards -= principal
        roll = engine.dice.auto_roll(
            "altar_investment_multiplier",
            list(range(-multiplier, multiplier + 1)),
            context=f"祭坛·自选倍率{multiplier}·随机盈亏倍数")
        outcome_multiplier = int(roll["selected"])
        # 先扣本金，再返还本金加净盈亏；净变化严格等于本金×随机数。
        delta = principal * outcome_multiplier
        engine.state.shards += principal * (outcome_multiplier + 1)
        outcome = "盈利" if outcome_multiplier > 0 else "亏损" if outcome_multiplier < 0 else "持平"
        engine.state.shards_peak = max(
            int(getattr(engine.state, "shards_peak", 0)),
            max(0, int(engine.state.shards)))
        applied.append(
            f"祭坛投资：本金{principal}，自选倍率{multiplier}，"
            f"随机结果{outcome_multiplier:+d}倍，{outcome}{abs(delta)}碎片，"
            f"净变化{delta:+d}，当前碎片{engine.state.shards}")
        debt_result = None
        player_peak = max(0, int(getattr(engine.state, "shards_peak", 0)))
        threshold = 10 + player_peak
        if engine.state.shards <= -threshold and player.is_alive:
            player.current_hp = 0
            player.depart_battle("还债")
            debt_result = {"type": "debt_death", "entity": player.name,
                           "debt": -engine.state.shards, "threshold": threshold,
                           "note": f"{player.name}被祭坛嚼碎：碎片余额{engine.state.shards}触及动态还债线"}
            applied.append(debt_result["note"])
        return {"applied": applied, "instructions": instructions,
                "random": roll["record"], "outcome": outcome,
                "outcome_multiplier": outcome_multiplier, "multiplier": multiplier,
                "principal": principal, "net_delta": delta, "debt_result": debt_result}

    # ---- 过路商人：买入/卖出遗物，价格必须事前确认 ----
    if event_name == "过路商人" and text.startswith("遗物交易"):
        from .models import Relic
        direction = params["trade_direction"]
        relic_name = params["relic_name"]
        price = params["price"]
        merchant = engine.state.merchant
        if merchant is None:
            return {"applied": [], "instructions": [], "error": "过路商人尚未进入本局副本"}
        if direction == "buy":
            relic = next(r for r in merchant.relics if r.name == relic_name)
            engine.state.shards -= price
            merchant.shards += price
            merchant.relics.remove(relic)
            engine.state.relics.append(relic)
            engine.state.note_relic_gained(relic)
            applied.append(f"与过路商人成交：买入遗物{relic_name}，支付{price}碎片；商人余额{merchant.shards}，剩余{len(merchant.relics)}件遗物")
        else:
            relic = next(r for r in engine.state.relics if r.name == relic_name)
            engine.state.relics.remove(relic)
            merchant.relics.append(relic)
            merchant.shards -= price
            engine.state.shards += price
            applied.append(f"与过路商人成交：卖出遗物{relic_name}，获得{price}碎片；商人余额{merchant.shards}，现有{len(merchant.relics)}件遗物")
        return {"applied": applied, "instructions": instructions,
                "trade": {"merchant": "过路商人", "direction": direction,
                          "relic_name": relic_name, "price": price, "confirmed": True}}

    # ---- 罪孽都市·遗落的赌局：正式随机只走DiceEngine，结果不可由调用方注入 ----
    if event_name == "遗落的赌局" and text.startswith("下注"):
        x = params["x"]
        if text.startswith("下注[碎片]"):
            if engine.state.shards - 2 * x < -50:
                return {"applied": [], "instructions": [], "error": "赌局失败后负债不得低于-50"}
            roll = engine.dice.auto_roll("event_lost_gamble_shards", ["win", "lose"], context="遗落的赌局·碎片")
            delta = 2 * x if roll["selected"] == "win" else -2 * x
            engine.state.shards += delta
            if delta > 0:
                _record_shards_peak()
            applied.append(f"赌局{roll['selected']}：碎片{delta:+d}")
            return {"applied": applied, "instructions": [], "random": roll["record"]}
        if text.startswith("下注生命"):
            _pay_numeric("流血", x)
            applied.append(f"流血{x}")
            roll = engine.dice.auto_roll("event_lost_gamble_life", ["win", "lose"], context="遗落的赌局·生命")
            if roll["selected"] == "win":
                engine.state.shards += 2 * x
                _record_shards_peak()
                applied.append(f"获得{2*x}碎片")
            else:
                applied.append("无事发生")
            return {"applied": applied, "instructions": [], "random": roll["record"]}

    # ---- 龙族起源事件：埋骨之地（龙心谷专属） ----
    # 结算覆盖：记入12点代价累积（选项1）/ 获得龙心资源（选项2）/ 拒绝（选项3）。
    # 2026-10-23 用户令：龙性货币废除；选项1 改为直接记入真龙之心的兑换门槛。
    if event_name == "埋骨之地":
        if text.startswith("继承龙骨"):
            engine.state.dragon_cost_points += 12
            applied.append("代价累积+12（真龙之心兑换门槛）")
            return {"applied": applied, "instructions": instructions}
        if text.startswith("拾取龙心"):
            from .models import Consumable
            existing = next((c for c in engine.state.consumables
                             if c.name == "衰老龙心" and c.kind == "dragon_heart"), None)
            if existing:
                existing.current_uses += 6
                existing.max_uses += 6
            else:
                engine.state.consumables.append(Consumable(
                    name="衰老龙心", effect="消耗Y点耐久可抵消Y点衰老代价",
                    current_uses=6, max_uses=6,
                    kind="dragon_heart", dragon_heart_type="衰老"))
            applied.append("获得衰老龙心(6/6)：消耗耐久可抵消等量衰老代价")
            return {"applied": applied, "instructions": instructions}
        # 选项3"掩埋龙骨"落入通用"无事发生"分支。

    # ---- 乱葬岗（二阶）专属事件 ----
    from .models import Relic, Consumable

    def _grant_item(name, effect, kind="relic"):
        if kind == "consumable":
            engine.state.consumables.append(Consumable(
                name=name, effect=effect, current_uses=1, max_uses=1))
            applied.append(f"获得消耗品{name}")
        else:
            gained = Relic(name=name, effect=effect)
            engine.state.relics.append(gained)
            engine.state.note_relic_gained(gained)
            applied.append(f"获得遗物{name}")

    if event_name == "纸人冥婚":
        if text.startswith("替新郎交拜"):
            _pay_numeric("流血", 15)
            applied.append("流血15")
            _mut = player.add_mutation(3)
            applied.append("获得异变3")
            if _mut.get("lost"):
                # 非怪物局外触发【迷失】：此处尚未进入战斗，直接判命零——
                # 战斗中触发才会走「异变为怪物与原队伍开战」分支（见 _resolve_mutation_lost）。
                player.current_hp = 0
                player.is_alive = False
                engine.combat._on_entity_death(
                    player, ctx=engine.combat._lost_context(player, {
                        "timing": "pre_battle_event", "source": "纸人冥婚", "source_type": "event",
                        "actor": player, "target": player, "mechanic": "cost",
                        "subtype": "mutation", "amount": 3, "tags": {"event"}}, subtype="collapse"))
                applied.append("异变达阈值，触发【迷失】：身边无可战之敌，异变为怪物的你独自命零")
            _grant_item("冥婚契约", "[战始]选择一名[目标]，自身与其共享受到的伤害")
            return {"applied": applied, "instructions": instructions}
        if text.startswith("抢撒殡钱"):
            _pay_numeric("萎缩", 2)
            applied.append("萎缩2")
            engine.state.shards += 25
            applied.append("获得25碎片")
            engine.state.event_modifiers["next_battle_no_dodge"] = True
            applied.append("下一场首回合敌人非必中攻击无法闪避")
            return {"applied": applied, "instructions": instructions}
        # 选项3避让旁观：无事发生

    if event_name == "镇尸棺材钉":
        if text.startswith("拔出"):
            _pay_numeric("枯竭", 3)
            applied.append("枯竭3")
            engine.state.consumables.append(Consumable(
                name="镇魂铁钉", effect="对[目标]施加【束缚2】；若[目标]是轮回者，可选择耐久额外-1使其速度归零",
                current_uses=3, max_uses=3))
            applied.append("获得消耗品镇魂铁钉（耐久3/3）")
            return {"applied": applied, "instructions": instructions}
        if text.startswith("贴符加固"):
            memory_cost = 1
            names = params.get("daowen_names")
            if not isinstance(names, list) or len(names) != memory_cost or names[0] not in player.dao_wen:
                return {"applied": [], "instructions": [], "error": "失忆1必须用daowen_names指定1种持有的道纹"}
            del player.dao_wen[names[0]]
            applied.append(f"失忆1：失去{names[0]}")
            engine.state.resonance["反转"] = engine.state.resonance.get("反转", 0) + 1
            applied.append("获得残韵·反转×1")
            engine.state.shards += 15
            applied.append("获得15碎片")
            return {"applied": applied, "instructions": instructions}
        # 选项3绕道远离：无事发生

    if event_name == "悬木红煞":
        if text.startswith("许诺替身"):
            if engine.state.employees:
                emp = engine.state.employees.pop(0)
                applied.append(f"失去员工{emp.name}")
            elif engine.state.friends:
                friend = engine.state.friends.pop(0)
                applied.append(f"失去朋友{friend.name}")
            else:
                _pay_numeric("衰老", 10)
                applied.append("衰老10（无员工/朋友，自身血限-10）")
            _grant_item("替死鬼", "当自身即将受到致死伤时，将其转移给攻击者", "consumable")
            return {"applied": applied, "instructions": instructions}
        if text.startswith("割血点唇"):
            _pay_numeric("流血", 18)
            applied.append("流血18")
            # 使杀伐获得红煞（对格挡造成双倍伤害）
            target_dw = next((n for n in ("杀伐",) if n in player.dao_wen), None)
            if target_dw:
                inst = player.dao_wen[target_dw]
                inst.sha_qi = "红煞"
                applied.append(f"{target_dw}获得红煞：对[目标]格挡造成双倍伤害")
            return {"applied": applied, "instructions": instructions}
        # 选项3默念心经：无事发生

    if event_name == "孤坟香案":
        if text.startswith("上前续香"):
            _pay_numeric("衰老", 6)
            applied.append("衰老6")
            _grant_item("三香通冥",
                        "每场战斗前三回合开始[回始]，所有敌方[目标]受到12点伤害；第3回合后熄灭")
            return {"applied": applied, "instructions": instructions}
        if text.startswith("踢翻香炉"):
            _pay_numeric("萎缩", 2)
            applied.append("萎缩2")
            engine.state.shards += 30
            applied.append("获得30碎片")
            return {"applied": applied, "instructions": instructions}
        # 选项3躬身施礼：无事发生

    if event_name == "赶尸栈房":
        if text.startswith("摇动赶尸铃"):
            _pay_numeric("流血", 12)
            applied.append("流血12")
            _pay_numeric("疲惫", 2)
            applied.append("疲惫2")
            _grant_item("赶尸铃", "召唤2具【行尸】1×4/36作为[临时朋友]加入本场战斗，战终尸体解体",
                        "consumable")
            return {"applied": applied, "instructions": instructions}
        if text.startswith("剥取黄符"):
            x = params.get("x")
            if not isinstance(x, int) or isinstance(x, bool) or x < 1:
                return {"applied": [], "instructions": [], "error": "剥取黄符必须显式提交正整数x"}
            _pay_numeric("枯竭", x)
            applied.append(f"枯竭{x}")
            for _ in range(x):
                engine.state.consumables.append(Consumable(
                    name="黄符", effect="将已学法术刻印其中，交给朋友/员工以法力/代价发动",
                    current_uses=1, max_uses=1))
            applied.append(f"获得{x}张黄符")
            return {"applied": applied, "instructions": instructions}
        # 选项3挂门离开：无事发生

    if event_name == "无名将军墓":
        if text.startswith("拔戟试锋"):
            _pay_numeric("流血", 20)
            applied.append("流血20")
            target_dw = params.get("daowen_name", "")
            if target_dw not in player.dao_wen:
                return {"applied": [], "instructions": [],
                        "error": "拔戟试锋必须用daowen_name指定自身一种道纹"}
            inst = player.dao_wen[target_dw]
            inst.sha_qi = "兵煞"
            applied.append(f"{target_dw}获得兵煞：该道纹造成的伤害额外+4")
            return {"applied": applied, "instructions": instructions}
        if text.startswith("供奉"):
            engine.state.shards = max(0, engine.state.shards - 20)
            applied.append("失去20碎片")
            ally = next((a for a in engine.state.friends + engine.state.employees if a.is_alive), None)
            if ally is not None:
                from .models import Relic
                ally.relics.append(Relic(name="重甲兵躯", effect="[血限]+15，受到≥20的伤害前将其减半"))
                ally.blood_limit += 15
                ally.current_hp = min(ally.current_hp + 15, ally.blood_limit)
                applied.append(f"{ally.name}获得重甲兵躯（血限+15）")
            else:
                player.blood_limit += 15
                # 2026-10-14：【猩红果实】持有者当前生命可超血限，不削溢出部分。
                if engine.state.hp_overflow_exempt(player):
                    player.current_hp = player.current_hp + 15
                else:
                    player.current_hp = min(player.current_hp + 15, player.blood_limit)
                applied.append("无队友，自身血限+15并获得重甲兵躯")
            return {"applied": applied, "instructions": instructions}
        # 选项3拜祭退避：无事发生

    # ---- 专属具名事件：龙心谷"追求者"（面板与道纹数值均为文档写死的固定值，不走通用正则） ----
    if event_name == "追求者":
        if text.startswith("雇佣"):
            engine.state.shards -= 10
            applied.append("失去10碎片")
            emp = Entity(name="追求者", entity_type="员工", blood_limit=96, current_hp=96,
                         attack_count=8, attack_power=2, is_deployed=False)
            for dw_name in ("逆鳞", "固执"):
                # 2026-09-17：面板不写死 X（与 副本/龙心谷.md 的「96/2/8，逆鳞，固执」一致）
                # 2026-10-03：【活血】道纹删除，面板同步去掉
                emp.dao_wen[dw_name] = DaoWenInstance(
                    DaoWen(name=dw_name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
                    x_value=0, x_free=True)
            engine.state.employees.append(emp)
            applied.append("获得追求者(96/2/8，逆鳞，固执)作为员工，默认待命，需deploy_employee派遣")
            return {"applied": applied, "instructions": instructions}
        elif text.startswith("拿走口粮"):
            engine.state.shards += 50
            applied.append("获得50碎片")
            engine.state.forced_monsters_next_battle.append({
                "name": "追求者", "attack_count": 8, "attack_power": 2, "blood_limit": 96,
                # x=None → x_free，与雇佣分支一致
                "dao_wen": {"逆鳞": None, "固执": None},
            })
            applied.append("已登记：下一场战斗追求者将作为怪物额外出现"
                            "(记录于 state.forced_monsters_next_battle，出怪流程本身另行接入时读取)")
            return {"applied": applied, "instructions": instructions}
        # 选项3"离开"落入下方通用的"无事发生"分支，无需特殊处理

    # ---- 已写死面板/跨战斗结果的确定性事件登记；创造性文本才进入Interrupt。 ----
    def _grant_daowen(entity, name, x):
        # 2026-09-17：微光者面板与怪物同格式，不再写死 X（x_free），
        # X 由发动时自选，上限只受[法限]或代价限制。x 参数仅为兼容旧调用。
        entity.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="", cost_formula="", effect_formula=""),
            x_value=0, x_free=True)

    if event_name == "遗忘书屋" and text.startswith("阅读《禁忌法典》"):
        # 文本“自选一件遗物与20[碎片]”中的“与20”不匹配通用“获得X碎片”正则，此处直补；
        # 不return，失忆/自选遗物继续走下方通用分支。
        engine.state.shards += 20
        applied.append("获得20碎片")
    if event_name == "遗忘书屋" and text.startswith("阅读《自我剖析》"):
        resonance_type = params.get("resonance_type")
        if resonance_type not in ("转换", "反转", "曲解"):
            return {"applied": [], "instructions": [], "error": "必须用resonance_type显式选择一种残韵"}
        _pay_numeric("枯竭", 1)
        engine.state.resonance[resonance_type] = engine.state.resonance.get(resonance_type, 0) + 1
        return {"applied": ["枯竭1", f"获得{resonance_type}残韵"], "instructions": []}
    if event_name in {"手术", "恶魔医务室"} and text.startswith("强制移植"):
        refs = {f"friend:{i}": entity for i, entity in enumerate(engine.state.friends) if entity.is_alive}
        refs.update({f"employee:{i}": entity for i, entity in enumerate(engine.state.employees) if entity.is_alive})
        target = refs.get(params.get("target_ref"))
        if target is None:
            return {"applied": [], "instructions": [], "error": "强制移植必须显式提交微光者target_ref"}
        from .gamedata import ORIGINAL_MONSTER_DAOWEN, MONSTER_TRANSFORM_DAOWEN
        candidates = sorted((ORIGINAL_MONSTER_DAOWEN | MONSTER_TRANSFORM_DAOWEN) - set(target.dao_wen))
        if not candidates:
            return {"applied": [], "instructions": [], "error": "目标没有可移植的怪物道纹"}
        roll = engine.dice.auto_roll("event_transplant_daowen", candidates, context="手术·强制移植")
        _grant_daowen(target, roll["selected"], 1)
        target._transplanted_daowen = roll["selected"]
        target._transplant_rounds_unchanged = 0
        return {"applied": [f"{target.name}被移植{roll['selected']}1"], "instructions": [],
                "random": roll["record"]}
    if event_name in {"手术", "恶魔医务室"} and text.startswith("抽取灵魂"):
        refs = {f"friend:{i}": entity for i, entity in enumerate(engine.state.friends) if entity.is_alive}
        refs.update({f"employee:{i}": entity for i, entity in enumerate(engine.state.employees) if entity.is_alive})
        target = refs.get(params.get("target_ref"))
        if target is None:
            return {"applied": [], "instructions": [], "error": "抽取灵魂必须显式提交微光者target_ref"}
        gain = math.ceil(target.blood_limit * 0.5)
        if target in engine.state.friends: engine.state.friends.remove(target)
        if target in engine.state.employees: engine.state.employees.remove(target)
        engine.state.shards += gain
        return {"applied": [f"失去{target.name}", f"获得{gain}碎片"], "instructions": []}
    if event_name == "黑市军火贩" and text.startswith("购买安保雇佣"):
        ref = params.get("friend_ref")
        friends = {f"friend:{i}": entity for i, entity in enumerate(engine.state.friends) if entity.is_alive}
        target = friends.get(ref)
        if target is None:
            return {"applied": [], "instructions": [], "error": "必须用friend_ref显式选择一名存活朋友"}
        engine.state.shards -= 15
        target.relics.append(Relic("防弹插板", EVENT_RELICS["防弹插板"]))
        target.blood_limit += 10; target.current_hp += 10
        return {"applied": ["失去15碎片", f"{target.name}获得防弹插板并血限+10"], "instructions": []}
    if event_name == "医生" and text.startswith("雇佣医生"):
        doctor = Entity("医生", "员工", blood_limit=50, current_hp=50,
                        attack_count=1, attack_power=1, is_deployed=False)
        engine.state.shards -= 10
        engine.state.employees.append(doctor)
        return {"applied": ["失去10碎片", "医生作为待命员工加入"], "instructions": []}
    elif event_name == "乞丐" and text.startswith("给予庇护"):
        beggar = Entity("乞丐", "朋友", blood_limit=50, current_hp=50,
                        attack_count=2, attack_power=3)
        # 2026-10-03：【狂暴】道纹删除，乞丐不再附带道纹
        beggar.mutation_count = 3
        engine.state.friends.append(beggar)
        applied.append("乞丐作为朋友加入")
    elif event_name == "断桥余烬" and text.startswith("接过伤者"):
        friend = Entity("岩行者", "朋友", blood_limit=54, current_hp=54,
                        attack_count=2, attack_power=4)
        _grant_daowen(friend, "背负", 1)
        _pay_numeric("流血", 10)
        applied.append("流血10")
        engine.state.friends.append(friend)
        applied.append("岩行者作为朋友加入")
        return {"applied": applied, "instructions": instructions}
    elif event_name == "逆行者" and text.startswith("让他同行"):
        friend = Entity("赴火者", "朋友", blood_limit=60, current_hp=60,
                        attack_count=3, attack_power=3)
        _grant_daowen(friend, "逆鳞", 1)
        engine.state.shards = max(0, engine.state.shards - 10)
        applied.append("失去10碎片")
        engine.state.friends.append(friend)
        applied.append("赴火者作为朋友加入")
        return {"applied": applied, "instructions": instructions}
    elif event_name == "逆行者" and text.startswith("接过余火"):
        # 2026-10-14：【余火印】整段删除（extra 方案废弃），改授【透支】道纹；
        # 已掌握时改为获得10碎片，避免流血10买空气。
        _pay_numeric("流血", 10)
        applied.append("流血10")
        if "透支" not in engine.state.player.dao_wen:
            _grant_daowen(engine.state.player, "透支", 1)
            applied.append("学会道纹·透支")
        else:
            engine.state.shards += 10
            applied.append("已掌握透支，改为获得10碎片")
        return {"applied": applied, "instructions": instructions}
    elif event_name == "皮衣店" and text.startswith("试穿"):
        engine.state.event_modifiers["next_battle_first_round_shield"] = 30
        applied.append("已登记：下一场第一回始获得30格挡")
    elif event_name == "尖叫下水道" and "缄默面具" in text:
        engine.state.event_modifiers["silent_mask_x"] = params["x"]
    elif event_name == "高利贷钱庄" and text.startswith("获得债务"):
        engine.state.event_modifiers["loan_active"] = True
        applied.append("已登记高利贷战始还款与负债利息")
    elif event_name == "地下角斗场" and text.startswith("签署下场打擂"):
        engine.state.event_modifiers.update({"arena_health_percent": 20, "arena_double_loot": True})
        applied.append("已登记下一场敌方血限+20%且战利品翻倍")
    elif event_name == "地下角斗场" and text.startswith("押注盘外博彩"):
        engine.state.event_modifiers["arena_bet_three_rounds"] = True
        applied.append("已登记三回合押注")
    elif event_name == "通缉悬赏榜" and text.startswith("撕下巨头"):
        engine.state.event_modifiers.update({"bounty_extra_monster": True, "bounty_reward": 30})
        applied.append("已登记下一场额外怪物与30碎片悬赏")
    elif event_name == "通缉悬赏榜" and text.startswith("举报"):
        engine.state.event_modifiers["next_battle_full_information"] = True
    elif event_name == "假钞印钞厂" and text.startswith("启动"):
        engine.state.event_modifiers["next_battle_fake_shards"] = 50
        applied.append("已登记下一场战始获得50假碎片")
    elif event_name == "裂隙温泉" and text.startswith("饮下泉水"):
        allocations = params.get("heal_allocations")
        refs = {"player:0": engine.state.player}
        refs.update({f"friend:{i}": e for i, e in enumerate(engine.state.friends) if e.is_alive})
        refs.update({f"employee:{i}": e for i, e in enumerate(engine.state.employees) if e.is_alive})
        if (not isinstance(allocations, list)
                or sum(entry.get("amount", -1) for entry in allocations if isinstance(entry, dict)) != 48
                or any(not isinstance(entry, dict) or entry.get("target_ref") not in refs
                       or not isinstance(entry.get("amount"), int) or isinstance(entry.get("amount"), bool)
                       or entry["amount"] < 0 for entry in allocations)):
            return {"applied": [], "instructions": [], "error": "必须用heal_allocations完整分配48点恢复量"}
        for entry in allocations:
            heal_target = refs[entry["target_ref"]]
            detail = engine.state.apply_heal(heal_target, entry["amount"], ctx={
                "timing": "pre_battle_event", "source": event_name, "source_type": "event",
                "actor": engine.state.player, "target": heal_target, "mechanic": "heal",
                "subtype": "event", "amount": entry["amount"], "tags": {"event", "pre_battle"},
            })
            applied.append(f"{heal_target.name}获得回复{detail['actual_heal']}")
        engine.state.pending_energy_penalty += 1
        return {"applied": applied, "instructions": instructions}
    elif event_name == "回忆当铺" and text.startswith("典当"):
        engine.state.event_modifiers["memory_gain_locked"] = True
    elif event_name == "回忆当铺" and text.startswith("赎回"):
        engine.state.event_modifiers["past_memory_count"] = engine.state.event_modifiers.get("past_memory_count", 0) + 1

    # ---- 回音长廊：错误遗言 / 清除遗言直接改《死者之书.md》 ----
    if event_name == "回音长廊":
        store = getattr(engine, "death_book", None)
        if "错误遗言" in text or text.startswith("聆听"):
            engine.state.shards += 10
            applied.append("获得10碎片")
            if store is not None:
                from .death_book import CAUSE_DRAFTS, validate_legacy
                written = store.append(validate_legacy(CAUSE_DRAFTS["echo_error"]))
                engine.state.death_book_legacies = store.load()
                applied.append(f"写入错误遗言：{written['text']}")
            return {"applied": applied, "instructions": instructions}
        if "清除" in text and "遗言" in text:
            params = params or {}
            pages = store.load() if store is not None else []
            if not pages:
                if player:
                    _pay_numeric("流血", 5)
                    applied.append("流血5")
                applied.append("无遗言可清除")
                return {"applied": applied, "instructions": instructions}
            listed = [{"index": i + 1,
                       "title": p.get("title") or f"遗言{i+1}",
                       "text": p.get("text", "")}
                      for i, p in enumerate(pages)]
            idx = params.get("legacy_index")
            title = params.get("legacy_title")
            removed = None
            if isinstance(idx, int) and not isinstance(idx, bool):
                removed = store.remove_at(idx - 1)
            elif isinstance(title, str) and title.strip():
                removed = store.remove_by_title(title)
            else:
                return {
                    "applied": [],
                    "instructions": [],
                    "error": "打碎镜子必须自选一页遗言（legacy_index 从1起，或 legacy_title）",
                    "pages": listed,
                    "instruction": "请重新调用 resolve_event，并带上要清除的那一页",
                }
            if removed is None:
                return {
                    "applied": [],
                    "instructions": [],
                    "error": "指定的遗言不存在",
                    "pages": listed,
                }
            if player:
                _pay_numeric("流血", 5)
                applied.append("流血5")
            engine.state.death_book_legacies = store.load()
            applied.append(f"清除遗言：{removed.get('title') or removed.get('text')}")
            return {"applied": applied, "instructions": instructions}

    def hurt(hp):
        if player:
            _pay_numeric("流血", hp)
            applied.append(f"流血{hp}")

    # 流血
    for m in re.finditer(r'流血\s*(\d+)', text):
        hurt(int(m.group(1)))
    if "流血X" in text:
        hurt(params["x"])
    # 数值代价统一走代价总线，允许【血契】按显式引用共同承担。
    for m in re.finditer(r'衰老\s*(\d+)', text):
        x = int(m.group(1)); _pay_numeric("衰老", x)
        applied.append(f"衰老{x}(血限-{x})")
    for m in re.finditer(r'枯竭\s*(\d+)', text):
        x = int(m.group(1)); _pay_numeric("枯竭", x)
        applied.append(f"枯竭{x}(法限-{x})")
    for m in re.finditer(r'萎缩\s*(\d+)', text):
        x = int(m.group(1)); _pay_numeric("萎缩", x)
        applied.append(f"萎缩{x}(速限-{x})")
    for m in re.finditer(r'疲惫\s*(\d+)', text):
        x = int(m.group(1)); _pay_numeric("疲惫", x)
        applied.append(f"疲惫{x}")
    # 失忆（显式指定失去的道纹）
    memory_cost = sum(int(v) for v in re.findall(r'失忆\s*(\d+)', text))
    if '失忆X' in text:
        memory_cost += params['x']
    if memory_cost:
        forgotten = params['daowen_names']
        for name in forgotten:
            del player.dao_wen[name]
        applied.append(f"失忆{memory_cost}：失去{'、'.join(forgotten)}")
    # 失去精力
    if '失去1次精力' in text or '精力-1' in text:
        engine.state.energy = max(0, engine.state.energy - 1); applied.append("失去1精力")
    # 支付碎片（“失去”与“消耗”同为支出）
    for m in re.finditer(r'(?:失去|消耗)\s*(\d+)\s*\[?碎片\]?', text):
        x = int(m.group(1)); engine.state.shards -= x; applied.append(f"失去{x}碎片")
    # 获得碎片（含"获得X碎片"/"获得X[碎片]"；跳过"双倍"等需随机的）
    for m in re.finditer(r'获得\s*(\d+)\s*\[?碎片\]?', text):
        x = int(m.group(1)); engine.state.shards += x; _record_shards_peak(); applied.append(f"获得{x}碎片")
    # 血限+X
    for m in re.finditer(r'\[?血限\]?\s*\+\s*(\d+)', text):
        x = int(m.group(1)); player.blood_limit += x; player.current_hp += x; applied.append(f"血限+{x}")
    # 获得残韵
    for rtype in ["曲解", "反转", "转换"]:
        if rtype in text and ('残韵' in text or '获得' in text):
            engine.state.resonance[rtype] = engine.state.resonance.get(rtype, 0) + 1
            applied.append(f"获得{rtype}残韵")
    # 明确失去/销毁当前遗物：严格使用预检过的relic_name，不得默认弹出最后一件。
    if "销毁一件当前遗物" in text or "失去一件当前遗物" in text:
        relic_name = params["relic_name"]
        relic = next(r for r in engine.state.relics if r.name == relic_name)
        engine.state.relics.remove(relic)
        applied.append(f"销毁遗物·{relic_name}")

    # 正文具名事件物品按名称授予，不得误抽普通遗物池。
    named_relics = [name for name in EVENT_RELICS if name in text]
    for name in named_relics:
        relic = Relic(name=name, effect=EVENT_RELICS[name], tags=["事件"])
        if name == "防弹插板":
            friend = next(f for f in engine.state.friends if f.name == params["friend"] and f.is_alive)
            friend.relics.append(relic)
            friend.blood_limit += 10
            friend.current_hp += 10
            applied.append(f"{friend.name}获得事件遗物·{name}")
        else:
            engine.state.relics.append(relic)
            engine.state.note_relic_gained(relic)
            applied.append(f"获得事件遗物·{name}")
    for name, (durability, effect) in EVENT_CONSUMABLES.items():
        if name in text:
            engine.state.consumables.append(Consumable(
                name=name, effect=effect, current_uses=durability, max_uses=durability,
            ))
            applied.append(f"获得消耗品·{name}({durability}/{durability})")

    # 自选遗物直接按显式名称取得；“随机遗物”则随机列3件后等待显式选1。
    if "自选一件遗物" in text:
        relic_name = params["relic_name"]
        relic = next(r for r in engine.state.relics_pool if r.name == relic_name)
        engine.state.relics_pool.remove(relic)
        engine.state.relics.append(relic)
        engine.state.note_relic_gained(relic)
        applied.append(f"获得遗物·{relic_name}")
    elif "随机" in text and "遗物" in text and not named_relics:
        discovery = engine._offer_relic_discovery(f"事件【{event_name}】")
        if not discovery.get("success"):
            return {"applied": applied, "instructions": instructions,
                    "error": discovery.get("error", "无法发现遗物")}
        applied.append(f"随机列出遗物候选：{'、'.join(discovery['choices'])}")

    # 法术大全中的预定义法术在事件奖励中直接写入角色的法术定义，
    # 后续统一通过自定义施法入口使用。
    if "选择获得两种法术定义" in text:
        granted = []
        for name in params["spell_names"]:
            required = engine.SPELL_REGISTRY.get(name)
            if required is None:
                instructions.append(f"未知法术【{name}】，需DM裁定")
                continue
            if not all(d in player.dao_wen for d in required):
                instructions.append(f"缺少道纹{required}，无法使用法术【{name}】，需DM裁定")
                continue
            if not any(sp.name == name for sp in player.spells):
                flow = engine.combat.SPELL_FLOWS.get(name, {})
                from .spell_dsl import spell_tier
                player.spells.append(Spell(
                    name=name, required_daowen=list(required),
                    trigger_condition=flow.get("trigger", ""),
                    effect_flow=flow.get("effect_flow", ""),
                    rank=spell_tier(flow.get("trigger", ""),
                                    engine.combat._flow_program(flow)),
                    automatic=bool(flow.get("automatic"))))
            granted.append(name)
        if granted:
            applied.append(f"取得法术定义：{'、'.join(granted)}")
    # 获得N点[速限]/[法限]：按正文「2属性点 = 1[速限] = 1[法限]」，
    # 速限与法限同价，各按字面点数加到对应上限（2026-09-16 裁定，清单 A4：
    # 旧实现给法限加 2×x，与速限口径不一致）。
    for m in re.finditer(r'获得(\d+)点\s*\[?速限\]?', text):
        x = int(m.group(1)); player.speed_limit += x; player.current_speed = player.speed_limit; applied.append(f"获得{x}速限")
    for m in re.finditer(r'获得(\d+)点\s*\[?法限\]?', text):
        x = int(m.group(1)); player.mana_limit += x; player.current_mana = player.mana_limit; applied.append(f"获得{x}法限")
    # 属性点：入属性点池，由玩家按正文自行兑换（2026-09-16 裁定，清单 A3：
    # 旧实现把任意含"属性点"的文本直接吞成 +1 速限，既绕过属性点池，
    # 也比价错误——1点属性点买不到1点速限，速限是2点一档）。
    m_attr = re.search(r'获得(\d+)点?\s*属性点|属性点\s*\+(\d+)', text)
    if m_attr:
        pts = int(m_attr.group(1) or m_attr.group(2) or 1)
        engine.state.attribute_points += pts
        applied.append(f"获得{pts}属性点（属性点池 {engine.state.attribute_points}）")
    # 拒绝/无事
    if ('无事发生' in text or text.startswith('拒绝：') or text.startswith('拒绝:')
            or text.startswith('观棋') or text.startswith('无视') or text.startswith('离开')
            or text.startswith('目送') or text.startswith('绕桥') or text.startswith('让炉')):
        applied.append("无事发生")
    # 特殊效果（下注/设计/限制/移植/抽取/雇佣/自定义等）→交DM
    special_kw = ['下注', '设计', '限制选择权', '强制移植', '抽取灵魂', '雇佣', 'diy', '定制', '押注', '负债', '双倍', '随机数', '写信', '寄']
    if any(k in text for k in special_kw) and not applied:
        instructions.append("含随机/自定义效果，需DM裁定")
    return {"applied": applied, "instructions": instructions}
