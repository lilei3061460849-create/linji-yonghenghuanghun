"""
道纹系统 - 所有道纹效果的数学计算
规则：道纹是概念，不是技能。两字词，世界最小规则单元。
"""
from __future__ import annotations
from typing import Optional, Any
from .models import Entity, StatusEffect, DaoWen, DaoWenInstance
from .enums import CostType, normalize_payment_type
from .rule_engine import daowen_migration_status, get_daowen_rule
from .gamedata import UNIMPLEMENTED_DAOWEN, RACE_EXCLUSIVE_DAOWEN, BLOODLINE_EXCLUSIVE_DAOWEN
import math


# 残韵闭环上的非道纹占位节点：
#   RESERVED_SLOT（2026-10-08）：一般删纹后尚待补设计的空位，如乱葬岗旧【镇尸】节点；
#   RELICIZED_SLOT（2026-10-14 用户令）：道纹遗物化后统一用「？？」替代原闭环节点。
# 两者都保留环的边数与入度/出度；查得到，但绝不能作为残韵结果获得（不耗残韵、不改牌面、不授予）。
# 名字避开真实道纹词汇，闭环结构仍可完整验证。
RESERVED_SLOT = "（待补）"
RELICIZED_SLOT = "？？"
PLACEHOLDER_SLOTS = frozenset({RESERVED_SLOT, RELICIZED_SLOT})
RELICIZED_DAOWEN_SLOTS = {name: f"？？〔{name}〕" for name in (
    "逆鳞", "伤痕", "寄生", "加速", "急速", "洞察", "无力",
    "冥气", "退化", "畸变", "缄默", "加害", "龙鳞", "固执", "贯穿", "裂变", "背负", "蒙蔽", "清算", "瓦解",
)}
# 迁入其它闭环的原位也要独立命名，避免不同闭环的待补节点在残韵查询中互相碰撞。
RELOCATED_DAOWEN_SLOTS = {name: f"（待补）〔{name}〕" for name in ("增殖",)}


def is_reserved_slot(name) -> bool:
    """该名字是否为闭环上的非道纹占位节点；带内部标识的空位也算占位。"""
    return name in PLACEHOLDER_SLOTS or (
        isinstance(name, str)
        and (name.startswith("？？〔") or name.startswith("（待补）〔"))
        and name.endswith("〕"))


# 道纹改名别名（旧档/旧面板读入时映射；引擎注册表只收新名，旧名提交照常报未知道纹）。
# 2026-10-22 用户令：【血债】改名【连击】并扩为多支付道纹，旧名只许在读入边界被映射。
LEGACY_DAOWEN_ALIASES = {"血债": "连击"}


def normalize_daowen_name(name):
    """旧道纹名 → 现名；非旧名原样返回。只用于读入/授予边界，不用于发动提交。"""
    return LEGACY_DAOWEN_ALIASES.get(name, name)


# 【净化】可逆的数值类代价：只包含可以按层数回退的当前状态。
# 失忆（已失去的道纹）、冷却、唯一、生命／流血、法力、碎片等属于已经发生的历史结果，
# 不在净化范围内——它们没有可逆层数账本，也不允许凭空退回。
PURIFIABLE_COST_KINDS = ("血限", "枯竭", "法限", "速度", "异变")


def purifiable_cost_layers(entity: Entity, kind: str) -> int:
    """目标当前持有、可被【净化】逆转的某种数值类代价层数。"""
    if entity is None:
        return 0
    kind = normalize_payment_type(kind)
    if kind == "异变":
        return max(0, int(getattr(entity, "mutation_count", 0) or 0))
    if kind == "枯竭":
        return max(0, int(getattr(entity, "pending_exhaust_mana", 0) or 0))
    ledger = getattr(entity, "reversible_cost_layers", None) or {}
    return max(0, int(ledger.get(kind, 0) or 0))


def display_resonance_node(name):
    """内部空位独立编号；玩家/索引统一显示为「？？」或「（待补）」。"""
    if isinstance(name, str) and name.startswith("？？〔"):
        return RELICIZED_SLOT
    if isinstance(name, str) and name.startswith("（待补）〔"):
        return RESERVED_SLOT
    return name


class DaoWenEngine:
    """道纹计算引擎"""

    # 怪物转化道纹（原始怪物道纹经残韵变化后的分支，与规则正文《原始怪物道纹与转化道纹》一致）
    # 用于"雇佣"后"发现并选择一种转化道纹"等需要从此类别中随机抽取的场景
    # 2026-10-08 清理：原孤儿转化道纹已移除（曾挂在【狂暴】的反转分支下）。
    TRANSFORMED_DAOWEN = [
        "弱化", "全速", "压迫",  # 愤怒已转消耗品怒火针，不得由转化授予入口复活。
        # 2026-10-11 用户令（A14）：【无神】【借力】【衰败】整条删除，不补位。
    ]

    # 波及X：选择X个目标建立/解除无限期效果分摊关系；X决定目标数与法力消耗。

    # X上限规则（代价类型 → 最大值函数）
    X_LIMITS = {
        "消耗": lambda state: float('inf'),     # 无上限，受法力限制
        "冷却": lambda state: 7,                 # 0≤X≤7
        "生命": lambda state: state.get("current_hp", math.inf),    # 0≤X≤当前生命
        "血限": lambda state: state.get("blood_limit", math.inf),   # 0≤X≤当前血限
        "枯竭": lambda state: state.get("mana_limit", math.inf),    # 0≤X≤当前法限
        "法限": lambda state: state.get("mana_limit", math.inf),     # 0≤X≤当前法限
        "速限": lambda state: state.get("speed_limit", math.inf),   # 0≤X≤当前速限
        "速度": lambda state: state.get("current_speed", math.inf), # 0≤X≤当前速度
        "失忆": lambda state: state.get("daowen_count", math.inf),  # 0≤X≤当前道纹数量
        "异变": lambda state: 50,                # 0≤X≤50
    }
    
    # ========== 核心道纹效果表 ==========
    # 每个道纹返回标准化的计算结果字典
    
    @staticmethod
    def ceil(value: float) -> int:
        """整数规则：所有计算都向上取整"""
        return math.ceil(value)
    
    # ---- 杀伐闭环 ----
    
    @staticmethod
    def calculate_shaifa(x: int, target: Entity = None) -> dict:
        """杀伐X：支付：法力X。使[目标]生命-X²。

        DM裁定 2026-09-10：法力改一池制后（战终才复原，不再每回合回填），道纹数值
        的约束从「速率」变成「预算」，可以放开。
        用户裁定 2026-09-13：由线性 5X 改为平方 X²——小X时弱于旧值（X≤4），
        大X时远强（X=10 打 100），把「攒法力一次性爆发」从习惯变成硬性最优解，
        与一池制预算的设计意图一致。
        """
        target_name = target.name if target is not None else "未选定目标"
        life_loss = x * x
        return {
            "dao_wen": "杀伐",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": x,
            "target_life_loss": life_loss,
            "summary": f"支付：法力{x}，{target_name}生命-{life_loss}"
        }
    
    @staticmethod
    def calculate_zaisheng(x: int, target: Entity = None, cost_channel: str = "法力") -> dict:
        """再生X：支付：法力X／攻击力4X；为[目标]回复4X点生命。"""
        target_name = target.name if target is not None else "未选定目标"
        heal = 4 * x
        if cost_channel == "攻击力":
            return {
                "dao_wen": "再生", "x": x,
                "cost_type": "攻击力", "cost_attack": heal,
                "target_heal": heal,
                "summary": f"支付：攻击力{heal}，为{target_name}回复{heal}点生命",
            }
        if cost_channel != "法力":
            raise ValueError("再生支付必须选择法力或攻击力")
        return {
            "dao_wen": "再生", "x": x,
            "cost_type": CostType.MANA.value, "cost": x,
            "target_heal": heal,
            "summary": f"支付：法力{x}，为{target_name}回复{heal}点生命",
        }
    
    @staticmethod
    def calculate_bihu(x: int, target: Entity = None) -> dict:
        """庇护X：支付：法力X。使[目标]免疫非[目标]导致的下X次失去生命，持续X。"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "庇护",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": x,
            "shelter_charges": x,
            "duration": x,
            "summary": f"支付：法力{x}，使{target_name}免疫非{target_name}导致的下{x}次失去生命，持续{x}"
        }
    
    # 连击六通道价格（2026-10-22 用户令：与【透支】同类多代价道纹，价格与效果全表同乘五）：
    # 生命5X／法力5X／血限X／枯竭X／碎片5X／假碎片50X；效果 选择[目标]5X次、每次1点伤害。
    # 比率锚现有体系：消耗品【逆命筹】承接重投效果；【假钞】1法力=10假碎片、
    # 【透支】上限类按整X计价；全乘五后血限/枯竭回到整数X计价，不留⌈X/5⌉零头，
    # 生命也回到旧【血债】「1点生命换1点伤害」的老比率（只是以5为一段）。
    # 各通道效果完全相同，只有倍率不同，不带任何通道专属机制
    # （演出差异不进引擎，用户 2026-10-22 令）。
    LIANJI_CHANNELS = ("生命", "法力", "血限", "枯竭", "碎片", "假碎片")

    @staticmethod
    def calculate_lianji(x: int, target: Entity = None, cost_channel: str = "生命") -> dict:
        """连击X（2026-10-22 由【血债】改名）：支付六选一（见 LIANJI_CHANNELS 价格表）。

        选择[目标] 5X 次，每次对其造成 1 点伤害。支付途径只改倍率，
        效果恒同；cost_channel 缺省生命（怪物/旧调用/预设法术不传参即原行为），
        碎片与假碎片是两个独立通道，只扣各自池子（不走「假碎片优先」的合并失去）。
        """
        cost_channel = normalize_payment_type(cost_channel)
        target_name = target.name if target is not None else "未选定目标"
        hits = 5 * x
        damage_per_hit = 1
        if cost_channel == "生命":
            extra = {"cost_type": CostType.BLEED.value, "cost_hp": 5 * x}
            price = f"生命{5 * x}"
        elif cost_channel == "法力":
            extra = {"cost_type": CostType.MANA.value, "cost": 5 * x}
            price = f"法力{5 * x}"
        elif cost_channel == "血限":
            extra = {"cost_type": CostType.AGING.value, "cost_blood_limit": x}
            price = f"血限{x}"
        elif cost_channel == "枯竭":
            extra = {"cost_type": CostType.EXHAUST.value, "cost_mana_limit": x}
            price = f"枯竭{x}"
        elif cost_channel == "碎片":
            extra = {"cost_type": "碎片", "cost_real_shards": 5 * x}
            price = f"碎片{5 * x}"
        elif cost_channel == "假碎片":
            extra = {"cost_type": "碎片", "cost_fake_shards": 50 * x}
            price = f"假碎片{50 * x}"
        else:
            raise ValueError(
                f"连击cost_channel非法：{cost_channel!r}，可选生命/法力/血限/枯竭/碎片/假碎片")
        return {
            "dao_wen": "连击",
            "x": x,
            **extra,
            "cost_channel": cost_channel,
            "hits": hits,
            "damage_per_hit": damage_per_hit,
            "total_damage": hits * damage_per_hit,
            "summary": f"{price}，选择{target_name} {hits}次，每次造成{damage_per_hit}点伤害"
        }
    
    @staticmethod
    def calculate_boba(x: int) -> dict:
        """波及X：支付：法力2X。选择X个目标，目标组彼此平分道纹效果，持续∞。"""
        return {
            "dao_wen": "波及",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "mark_targets": x,
            "duration": -1,
            "summary": f"支付：{2 * x}法力。选择{x}个目标，目标组彼此平分道纹效果，持续∞。"
        }
    
    # ---- 杀伐闭环后半（连击至封印）----
    
    @staticmethod
    def calculate_gongzui(x: int, target1: Entity = None, target2: Entity = None) -> dict:
        """共罪X：支付：法力X。两个[目标]与发动者共同组成共罪组，持续X。

        发动者始终额外占一个支付名额；即使已被选为目标，也仍追加这一份。
        组成员支付资源时，由全部存活支付名额平分，实际支付者同样承担自己的
        份额；不分摊特殊机制代价。共罪不是局外道纹。
        """
        if x < 1:
            raise ValueError("X必须为正整数")
        if target1 is not None or target2 is not None:
            if target1 is None or target2 is None or target1 is target2:
                raise ValueError("共罪需要两个不同的目标")
            if not target1.is_alive or not target2.is_alive:
                raise ValueError("共罪的两个目标都必须存活")
        name1 = target1.name if target1 is not None else "目标1"
        name2 = target2.name if target2 is not None else "目标2"
        return {
            "dao_wen": "共罪", "x": x, "target1": target1, "target2": target2,
            "cost_type": CostType.MANA.value, "cost": x, "duration": x,
            "summary": f"支付：法力{x}，{name1}、{name2}与发动者额外一份组成共罪支付名额，持续{x}",
        }

    @staticmethod
    def calculate_xueqi(x: int, target: Entity = None) -> dict:
        """血契X：支付：法力X。你支付资源时，[目标]额外支付X倍同种名义资源，持续X。

        按名义支付量计算；目标资源不足时扣至0，生命不足正常命零。血契不是局外道纹。
        """
        if x < 1:
            raise ValueError("X必须为正整数")
        if target is not None and not target.is_alive:
            raise ValueError("血契需要一个存活目标")
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "血契", "x": x, "cost_type": CostType.MANA.value, "cost": x,
            "blood_pact_multiplier": x, "duration": x,
            "summary": (f"支付：法力{x}，与{target_name}建立血契：你支付资源时，"
                        f"{target_name}额外支付{x}倍相同资源，持续{x}"),
        }

    @staticmethod
    def calculate_zengzhi(x: int, target: Entity = None) -> dict:
        """增殖X：支付：血限X。使[目标]的[血限]变为∞，持续X回合（战终清除）。"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "增殖",
            "x": x,
            "cost_type": CostType.AGING.value,
            "cost_blood_limit": x,
            "duration": x,
            "infinite_blood_limit": True,
            "summary": f"血限{x}，使{target_name}的血限变为∞，持续{x}回合"
        }
    
    @staticmethod
    def calculate_shufu(x: int, target: Entity = None) -> dict:
        """束缚X：支付：冷却2X。使[目标]只能施法（瞬发或自定义法术），其余主动行动不可用，持续X。"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "束缚",
            "x": x,
            "cost_type": CostType.COOLDOWN.value,
            "cost": 2 * x,
            "duration": x,
            "effect": "只能施法（瞬发或自定义法术），其余主动行动不可用",
            "summary": (f"冷却{2 * x}场，使{target_name}只能施法（瞬发或自定义法术），"
                        f"其余主动行动不可用，持续{x}回合")
        }
    
    @staticmethod
    def calculate_touzhi(x: int, cost_channel: str = "生命") -> dict:
        """透支X：支付：（四选一）：生命4X／血限2X／枯竭X／速度X。你获得X点法力

        用户裁定 2026-09-12：与【再生X】构成生命⇄法力闭环，净值不产生免费资源；
        实际上限由【癌变】(本场累计回复=2×血限) 自然封死。
        用户裁定 2026-09-13：3X→4X。与【再生X】(支付：法力X→回复4X生命) 对齐为
        严格 4:1 双向汇率，闭环净值归零，不再每轮白赚生命。
        2026-10-14 用户令：改为多代价道纹——合并【搏命】（速度X换X法力）并删除搏命；
        遗物【余火印】删除（其发动时额外耗龙心模式一并取消，龙心只走通用抵扣）。
        速度通道沿用搏命的 k=1 结论：卖X速度得kX法力可经【超频】买回 kX/2 速度，
        k≥2 即永动，故取 k=1（每卖1点速度净亏0.5点，循环必然收敛）。
        cost_channel 缺省生命（向后兼容：怪物/旧调用不传参即原行为）。
        """
        cost_channel = normalize_payment_type(cost_channel)
        if cost_channel == "生命":
            cost_type, cost_key, amount = CostType.BLEED.value, "cost_hp", 4 * x
        elif cost_channel == "血限":
            cost_type, cost_key, amount = CostType.AGING.value, "cost_blood_limit", 2 * x
        elif cost_channel == "枯竭":
            cost_type, cost_key, amount = CostType.EXHAUST.value, "cost_mana_limit", x
        elif cost_channel == "速度":
            cost_type, cost_key, amount = CostType.FATIGUE.value, "cost_speed", x
        else:
            raise ValueError(f"透支cost_channel非法：{cost_channel!r}，可选生命/血限/枯竭/速度")
        return {
            "dao_wen": "透支",
            "x": x,
            "cost_type": cost_type,
            cost_key: amount,
            "mana_gain": x,
            "summary": f"{cost_channel}{amount}，获得{x}点法力"
        }
    
    @staticmethod
    def calculate_fengyin(x: int, target: Entity = None) -> dict:
        """封印X：支付：异变X，使一个[目标]延后X回合再入场。

        2026-10-08 用户令：目标由"只限怪物"放开为**任意目标**——敌人、[朋友]、
        [员工]、造物、敌对轮回者，以及施法者自己都可以被封印。
        暂离的语义不变：从场上摘走，X 回合后的[回始]回到它原本所在的阵营与位置
        （见 GameState.detach_from_field / reattach_to_field）。
        """
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "封印",
            "x": x,
            "cost_type": CostType.MUTATION.value,
            "cost_mutation": x,
            "delay_monster_reentry": True,
            "delay_rounds": x,
            "target_name": target_name,
            "summary": f"异变+{x}，使{target_name}延后{x}回合再入场"
        }
    
    # ---- 怪物原始道纹 ----
    
    @staticmethod
    def calculate_quanli(x: int, target: Entity = None) -> dict:
        """全力X：支付：异变5X。使[目标]攻击力等同其法限，持续X

        2026-09-17 用户令重做。旧版「攻击力+X，持续∞」写的是遗留字段 attack_power，
        属性模型统一后（攻击力=当前法力）对不写穿的轮回者完全无效。

        新版改为**锁定**：生效期间[目标]的攻击力恒等于其[法限]，不再随当前法力
        下降而下降——即"不用担心法力降低导致攻击输出降低"。法力本身照常被支付：
        （它仍是施法资源），只是攻击力不再跟着掉。

        实现走状态层（状态名"全力"），由 models.py::effective_attack_power 读取，
        故对轮回者/怪物/朋友/员工同口径生效。
        """
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "全力",
            "x": x,
            "cost_type": CostType.MUTATION.value,
            "cost_mutation": 5 * x,
            "attack_power_to_mana_limit": True,
            "duration": x,
            "summary": f"异变+{5*x}，使{target_name}攻击力等同其法限，持续{x}回合"
        }
    
    @staticmethod
    def calculate_qianghua(x: int, target: Entity = None) -> dict:
        """强化X：支付：法力X／生命2X。使[目标]获得2X额外攻击力，持续∞。"""
        if x < 1:
            raise ValueError("X必须为正整数")
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "强化", "x": x, "cost_type": CostType.MANA.value,
            "cost": x, "cost_hp": 2 * x, "attack_bonus": 2 * x, "duration": -1,
            "summary": f"支付：法力{x}／生命{2*x}，使{target_name}获得{2*x}额外攻击力，持续∞",
        }

    @staticmethod
    def calculate_jinghua(x: int, target: Entity = None, cost_choices: list[dict] | None = None) -> dict:
        """净化X：支付：法力20X。选择目标拥有的数值类代价及各层数，可混选，总层数不超过X。

        仅血限、枯竭、法限、速度、异变；失忆、冷却与已经失去的生命不在范围内。
        可混选多种代价：每项显式给出 layers（单选项省略 layers 时默认吃满 X），
        所有 layers 之和不得超过 X；代价恒按声明的 X 支付 20X，未用完的层数不返还。
        """
        target_name = target.name if target is not None else "未选定目标"
        allowed = PURIFIABLE_COST_KINDS
        if not isinstance(cost_choices, list) or not cost_choices:
            raise ValueError("净化必须显式选择目标拥有的数值类代价")
        normalized: list[dict] = []
        for item in cost_choices:
            if not isinstance(item, dict):
                raise ValueError("净化选择必须是对象")
            kind = normalize_payment_type(item.get("type", ""))
            if kind not in allowed:
                raise ValueError("净化仅支持数值类代价：血限/枯竭/法限/速度/异变，不支持失忆、冷却或已经失去的生命")
            normalized.append({"type": kind, "layers": item.get("layers")})
        kinds = [item["type"] for item in normalized]
        if len(set(kinds)) != len(kinds):
            raise ValueError("净化同一种代价只能选择一次；请合并为一项并给出总层数")
        if any(item["layers"] is None for item in normalized) and len(normalized) > 1:
            raise ValueError("净化选择多种代价时必须显式给出每种代价的层数")
        chosen: dict[str, int] = {}
        for item in normalized:
            kind = item["type"]
            layers = x if item["layers"] is None else item["layers"]
            if type(layers) is not int or isinstance(layers, bool):
                raise ValueError("净化层数必须是正整数")
            if layers < 1:
                raise ValueError("净化层数必须为正整数")
            chosen[kind] = layers
        total = sum(chosen.values())
        if total > x:
            raise ValueError(f"净化总层数{total}超过X={x}")
        if target is not None:
            for kind, layers in chosen.items():
                owned = purifiable_cost_layers(target, kind)
                if owned <= 0:
                    raise ValueError(f"目标未拥有可净化的【{kind}】层数")
                if layers > owned:
                    raise ValueError(
                        f"净化选择{kind}{layers}层超过目标实际持有的{owned}层")
        detail = "、".join(f"{kind}-{n}" for kind, n in chosen.items())
        return {
            "dao_wen": "净化", "x": x, "cost_type": CostType.MANA.value, "cost": 20 * x,
            "cost_reductions": chosen, "purified_layers": total,
            "summary": f"支付：法力{20*x}。使{target_name}的{detail}（合计{total}层，≤X={x}）",
        }

    @staticmethod
    def calculate_feixing(x: int) -> dict:
        """飞行X：支付：冷却X。非飞行者无法把飞行者选为[目标]，持续X回合。"""
        return {
            "dao_wen": "飞行", "x": x,
            "cost_type": CostType.COOLDOWN.value, "cost": x,
            "duration": x,
            "summary": f"冷却{x}场，非飞行者无法把你选为[目标]，持续{x}回合",
        }

    @staticmethod
    def calculate_bizhong(x: int) -> dict:
        """必中X：支付：异变X。你选中的目标无法进行回合外行动，持续X。

        口径（2026-10-12 用户令增强，取代 2026-09-28 的 self-buff 口径）：
        必中是给**目标**挂 debuff，不是给自己挂姿态。效果从「无法闪避」扩大为
        「无法进行回合外行动」——即目标在该状态下不能 闪避 / 招架，也不能发动
        任何反应型法术（受到伤害前/失去生命后/目标发动道纹前 等窗口）。
        「未来可能新增的回合外行动」同样受此约束：新增反应窗口一律走
        CombatEngine.out_of_turn_blocked() 这道闸，不要各自另立判断。
        """
        return {
            "dao_wen": "必中",
            "x": x,
            "cost_type": CostType.MUTATION.value,
            "cost_mutation": x,
            "bizhong_target_debuff": x,   # 给目标挂持续X回合的必中debuff
            "summary": f"异变+{x}，你选中的[目标]无法进行回合外行动，持续{x}回合"
        }
    

    @staticmethod
    def calculate_ruhua(x: int, target: Entity = None) -> dict:
        """弱化X：支付：法力2X。使[目标]攻击力-X，持续∞"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "弱化",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "attack_reduction": x,
            "duration": -1,
            "summary": f"支付：{2 * x}法力，使{target_name}攻击力-{x}，永久"
        }
    

    @staticmethod
    def calculate_quansu(x: int, target: Entity = None) -> dict:
        """全速X（原名【迟滞】）：支付：冷却X。使[目标]攻击次数等同其速限，持续X

        2026-09-17 用户令重做并改名。旧版「攻击次数固定为1」是减益，写的是遗留字段
        attack_count，属性模型统一后（攻击次数=当前速度）对轮回者无效。

        新版改为**锁定为[速限]**：生效期间[目标]的攻击次数恒等于其[速限]。
        由于 2026-09-13 全局钳制规则（clamp_immortal_body）已让「当前速度≤[速限]」
        无条件成立，本效果实为**增益**：把被削的速度补满到上限，并免疫后续减速。
        因语义由减益翻转为增益，原名「迟滞」名不副实，故改名【全速】。

        走状态层（状态名"全速"），由 models.py::effective_attack_count 读取，
        对轮回者/怪物/朋友/员工同口径生效。
        """
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "全速",
            "x": x,
            "cost_type": CostType.COOLDOWN.value,
            "cost": x,
            "attack_count_to_speed_limit": True,
            "duration": x,
            "summary": f"冷却{x}场，使{target_name}攻击次数等同其速限，持续{x}回合"
        }
    



    @staticmethod
    # 2026-10-11 用户令（A14）：【shuaibai】整条删除——不做闭环补位、不设计替换道纹。

    

    @staticmethod
    def calculate_bianxing(x: int) -> dict:
        """变形X：支付：法力X。使[目标]当前速度与当前法力互换，持续X

        注意**故意不声明 target 形参**：api.py 的判定是"计算函数声明了 target 就
        强制要求显式指定目标，禁止静默改为自身"。而 2026-09-17 用户令要的是
        **可选目标**：指定了就作用于该目标，不指定则作用于自身。故此处不声明，
        由 combat.py 的 ``swap_target = target if target else caster`` 兜底。

        2026-09-17 用户令重做。旧版「使自身攻击力与攻击次数互换」写的是遗留字段
        attack_power / attack_count，属性模型统一后（攻击力=当前法力、攻击次数=
        当前速度）对不写穿的轮回者无效，且只能对自己用。

        新版直接互换**当前速度**与**当前法力**，可指定目标（不指定时默认自身）。
        互换后两者各自被上限钳制（当前速度≤[速限]、当前法力≤[法限]），
        **被钳掉的部分凭空消失**——这正是本道纹的收益来源：
            例：敌方 20/3/10（血限/速度/法力，速限3）
                互换 → 速度10、法力3 → 速度被速限钳回3
                结果 20/3/3 —— 目标凭空失去 7 点法力（攻击力同步下降 7）。
        持续X结束后还原互换前的当前速度/当前法力，但被钳掉的部分不返还。
        """
        return {
            "dao_wen": "变形",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": x,
            "duration": x,
            "effect": "当前速度与当前法力互换",
            "summary": f"支付：{x}法力，使[目标]当前速度与当前法力互换（超出上限部分蒸发），持续{x}回合"
        }
    
    @staticmethod
    def calculate_dingxing(x: int, target: Entity = None) -> dict:
        """定型X：支付：法力2X。使[目标]攻击次数与攻击力无法被改变，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "定型",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "duration": x,
            "effect": "攻击次数与攻击力无法被改变",
            "summary": f"支付：{2 * x}法力，使{target_name}攻击次数与攻击力无法被改变，持续{x}回合"
        }
    

    @staticmethod
    def calculate_chaopin(x: int) -> dict:
        """超频X：支付：法力2X。使[目标]速度+X

        目标由发动方自由指定，选到谁就给谁加速度——可以给自己，也可以给队友
        或敌人。旧版写作"使自身速度+X"，但实现一直是给 target 加速，文案与
        行为不符；现按用户裁定统一为"自由选择目标"。
        """
        return {
            "dao_wen": "超频",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "speed_boost": x,
            "summary": f"支付：{2*x}法力，[目标]速度+{x}"
        }
    
    @staticmethod
    def calculate_weisuo(x: int, target: Entity = None) -> dict:
        """萎缩X：支付：法力2X。使[目标]无法获得速度，持续X。

        仅禁止获得速度；不削减[速限]，也不阻止失去或支付速度。
        """
        if x < 1:
            raise ValueError("X必须为正整数")
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "萎缩", "x": x, "cost_type": CostType.MANA.value,
            "cost": 2 * x, "no_speed_gain": True, "duration": x,
            "summary": f"支付：法力{2*x}，使{target_name}无法获得速度，持续{x}",
        }

    @staticmethod
    def calculate_huaisi(x: int, target: Entity = None) -> dict:
        """坏死X：支付：法力5X。使[目标]无法获得回复，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "坏死",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 5 * x,
            "duration": x,
            "effect": "无法获得回复",
            "summary": f"支付：{5 * x}法力，使{target_name}无法获得回复，持续{x}回合"
        }
    

    @staticmethod
    def calculate_dianjin(x: int) -> dict:
        """点金X：代价失忆X；你获得80X碎片。"""
        return {
            "dao_wen": "点金",
            "x": x,
            "cost_type": CostType.AMNESIA.value,
            "cost": x,
            "forget_daowen": x,
            "shard_gain": 80 * x,
            "summary": f"失忆{x}，获得{80*x}个碎片",
        }

    # ---- 罪孽都市专属道纹 ----
    
    @staticmethod
    def calculate_diandang(x: int) -> dict:
        """典当X：代价失忆2X；发现获得X种道纹（2026-10-13 用户令顶替【逼债】的闭环格）。

        发现口径 = 局外【学习】口径（见 engine/gamedata.py::learnability_error）：
        已持有/异副本专属/未解锁门禁/怪物转化/未实装的一律不出池。
        轮回者发动→每种 pending 一组3选1（choose_diandang_daowen 逐组领取）；
        非轮回者发动（如怪物经【原初】借用）→即时随机授予（怪物没有发现选择界面）。
        授予一律受【承载上限】约束，超额不授予。
        """
        return {
            "dao_wen": "典当",
            "x": x,
            "cost_type": CostType.AMNESIA.value,
            "cost": 2 * x,
            "forget_daowen": 2 * x,
            "discover_daowen": x,
            "summary": f"失忆{2 * x}，发现获得{x}种道纹",
        }
    
    @staticmethod
    def calculate_haoduo(x: int, target: Entity = None) -> dict:
        """豪夺X（2026-09-28 用户令，旧【抵扣】改名）：支付：5X碎片，夺取[目标]1件遗物持续X回合。

        夺取=把目标的一件遗物转移到己方（施法者）临时持有，持续X回合，到期归还。
        与旧【抵扣】（只封印）的区别：夺取期间施法者**真正持有**该遗物并能触发其被动，
        目标在这段时间里不能触发该遗物。
        """
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "豪夺", "x": x,
            # 2026-09-28 用户令：支付：5X碎片（沿用现有 cost_shards 付费通道；
            # 不写 cost_type=MANA，避免被当成法力支付）。
            "cost_type": "代价", "cost_shards": 5 * x,
            "relic_steal": 1, "duration": x,
            "summary": f"支付：{5*x}碎片，夺取{target_name}一件遗物，持续{x}回合"
        }

    # 旧名 calculate_dikou 不再保留（避免注册时与"豪夺"双注册）。
    
    @staticmethod
    def calculate_qiangjie(x: int, target: Entity = None) -> dict:
        """抢劫X：支付：法力3X。夺取目标10X碎片；若无碎片则失去X点速度"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "抢劫", "x": x, "cost_type": CostType.MANA.value, "cost": 3 * x,
            "shard_steal": 10 * x, "speed_penalty": x,
            "summary": f"支付：{3 * x}法力，夺取{target_name} {10*x}碎片或{x}速度"
        }

    @staticmethod
    def calculate_lesuo(x: int, target: Entity = None) -> dict:
        """勒索X：支付：法力3X。目标失去10X碎片，不足部分记负债（真碎片扣负；2026-10-14 补点金反转分叉，还债新生产者）。

        与【抢劫】同价同量（3X→10X），形状不同：抢劫是偷取（施法者获得，不足不记负债；
        无碎片改失速），勒索是销毁+记负债（谁都不获得，不足部分真碎片扣负，可触发【还债】）。
        单次结算，无持续（用户 2026-10-14 选单次，不 mirror 逼债的回始∞）。
        """
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "勒索", "x": x, "cost_type": CostType.MANA.value, "cost": 3 * x,
            "shard_debt": 10 * x,
            "summary": f"支付：{3 * x}法力，{target_name}失去{10*x}碎片，不足记负债"
        }
    
    @staticmethod
    def calculate_jiachao(x: int) -> dict:
        """假钞X：支付：法力X。获得10X假碎片"""
        return {
            "dao_wen": "假钞", "x": x, "cost_type": CostType.MANA.value, "cost": x,
            "fake_shards": 10 * x,
            "summary": f"支付：{x}法力，获得{10*x}假碎片"
        }
    
    # ---- 龙心谷专属道纹 ----
    
    @staticmethod
    # 2026-10-11 用户令（A13）：【嫁祸】整条删除——效果与遗物【贪婪契约】重复。

    
    @staticmethod
    def calculate_chao(x: int) -> dict:
        """唤潮X：支付：枯竭X。在开放地图中，以自身所在格为中心选择周围X个可用地图格，将其改为【水流】地形；只有鲛族可以进入或穿越【水流】格。"""
        return {"dao_wen": "唤潮", "x": x, "cost_type": CostType.EXHAUST.value,
                "cost_mana_limit": x, "map_terrain": "water_current", "map_terrain_count": x,
                "summary": f"支付：枯竭{x}，在开放地图周围设置{x}格水流地形"}

    @staticmethod
    def calculate_shenjing(x: int) -> dict:
        """蜃景X：支付：枯竭X。在开放地图的施术者所在格创造X个外观与施术者相同的地图幻象；生成后，施术者与幻象分别向不同方向自主移动，欺骗追踪与寻路。幻象不参与战斗；被任何其他角色接触时消散，施术者死亡时其全部幻象消散。进入战斗本身不会清除幻象。"""
        return {"dao_wen": "蜃景", "x": x, "cost_type": CostType.EXHAUST.value,
                "cost_mana_limit": x, "map_illusions": x,
                "summary": f"支付：枯竭{x}，在开放地图创造{x}个地图幻象"}

    @staticmethod
    def calculate_liudong(x: int, target: Entity = None) -> dict:
        """流动X：支付：法力10X。将[目标]身上的X个“持续Y”转移至另一名[目标]。"""
        if target is None:
            raise ValueError("流动必须显式选择来源目标")
        return {
            "dao_wen": "流动", "x": x, "cost_type": CostType.MANA.value,
            "cost": 10 * x, "transfer_status_count": x,
            "summary": f"支付：{10*x}法力，将{target.name}的{x}个持续状态转移至另一目标",
        }

    @staticmethod
    def calculate_yiwang(x: int, target: Entity = None) -> dict:
        """遗忘X：支付：法力20X。移除[目标]身上的X种“持续Y”。"""
        if target is None:
            raise ValueError("遗忘必须显式选择目标")
        return {
            "dao_wen": "遗忘", "x": x, "cost_type": CostType.MANA.value,
            "cost": 20 * x, "remove_status_count": x,
            "summary": f"支付：{20*x}法力，移除{target.name}的{x}种持续状态",
        }

    @staticmethod
    def calculate_bingdong(x: int, target: Entity = None) -> dict:
        """冰冻X：支付：冷却X。选择[目标]拥有的X种道纹，使其无法生效，已有效果一并暂停，持续∞。

        目标不足X种时，支付与效果使用的X均在发动前自动降至实际种数；
        目标0种时按通用X=0规则不发动。
        """
        if target is None:
            raise ValueError("冰冻必须显式选择目标")
        return {
            "dao_wen": "冰冻", "x": x, "cost_type": CostType.COOLDOWN.value,
            "cost": x, "duration": -1, "freeze_count": x,
            "summary": f"冷却{x}，冻结{target.name}的{x}种道纹及其已有效果，持续∞",
        }

    @staticmethod
    def calculate_dameng(x: int, target: Entity = None) -> dict:
        """大梦X：支付：法力5X。使[目标]额外经历X个回合；世界回合数不变，不提供额外行动机会。"""
        if target is None:
            raise ValueError("大梦必须显式选择目标")
        return {
            "dao_wen": "大梦", "x": x, "cost_type": CostType.MANA.value,
            "cost": 5 * x, "extra_target_rounds": x,
            "summary": f"支付：{5*x}法力，使{target.name}真实额外经历{x}回合",
        }

    @staticmethod
    def calculate_chenni(x: int, target: Entity = None) -> dict:
        """沉溺X：支付：法力10X。使[目标]无法经历X个回合。"""
        if target is None:
            raise ValueError("沉溺必须显式选择目标")
        return {
            "dao_wen": "沉溺", "x": x, "cost_type": CostType.MANA.value,
            "cost": 10 * x, "duration": -1 if x > 0 else 0,
            "block_target_rounds": x,
            "summary": f"支付：{10*x}法力，使{target.name}无法经历{x}个回合",
        }

    @staticmethod
    def calculate_huixiang(x: int, target: Entity = None, status_index: int = None) -> dict:
        """回响X：支付：法力5X。使[目标]的一种“持续Y”变为“持续Y＋X”。"""
        return {"dao_wen": "回响", "x": x, "cost_type": CostType.MANA.value,
                "cost": 5 * x, "extend_duration": x, "status_index": status_index,
                "summary": f"支付：{5*x}法力，将目标一种持续时间增加{x}"}

    @staticmethod
    def calculate_chutu(x: int) -> dict:
        """出土X：支付：生命10X。自身接下来X次道纹变化保留原道纹。"""
        return {"dao_wen": "出土", "x": x, "cost_type": CostType.BLEED.value,
                "cost_hp": 10 * x, "preserve_daowen_changes": x,
                "summary": f"生命{10*x}，自身接下来{x}次道纹变化保留原道纹"}

    @staticmethod
    def calculate_busi(x: int, target: Entity = None) -> dict:
        """不死X：支付：冷却X。使[目标]的生命无法降至1以下，持续X。"""
        return {"dao_wen": "不死", "x": x, "cost_type": CostType.COOLDOWN.value,
                "cost": x, "duration": x, "hp_floor": 1,
                "summary": f"冷却{x}，目标生命下限1，持续{x}"}

    @staticmethod
    def calculate_cuangai(x: int, target: Entity = None) -> dict:
        """篡改X：支付：生命3X。使[目标]接下来X次发动道纹的目标由你选择。"""
        return {"dao_wen": "篡改", "x": x, "cost_type": CostType.BLEED.value,
                "cost_hp": 3*x, "tamper_activations": x,
                "summary": f"生命{3*x}，接管目标接下来{x}次道纹的目标选择"}

    @staticmethod
    def calculate_cuihua(x: int, target: Entity = None) -> dict:
        """催化X：支付：冷却2X。使自身或[目标]X种处于【冷却】状态的道纹立即发动一次。"""
        return {"dao_wen": "催化", "x": x, "cost_type": CostType.COOLDOWN.value,
                "cost": 2*x, "catalysis_count": x,
                "summary": f"冷却{2*x}，使目标{x}种冷却中的道纹立即发动一次"}

    # ========== 乱葬岗（二阶）专属道纹 ==========

    @staticmethod
    def calculate_fenlie(x: int) -> dict:
        """分裂X：支付：生命3X。创造X个1/1/1的造物为你而战。

        每只造物固定为1血限/当前生命、1速限/当前速度、1法限/当前法力；
        按通用面板换算得到1次击数与1攻击力；造物与所有角色共用基础1次出手及通用修正，
        不另设专用上限。不携带道纹，不继承施法者的面板、偏爱或遗物，[战终]消散。
        """
        # 提交端要求 X≥1；此处允许【退化】在 resolve 入口把有效 X 降为0。
        if not isinstance(x, int) or isinstance(x, bool) or x < 0:
            raise ValueError("分裂X必须是非负整数")
        return {
            "dao_wen": "分裂", "x": x,
            "cost_type": CostType.BLEED.value, "cost_hp": 3 * x,
            "split_creations": x,
            "creation_blood_limit": 1, "creation_speed_limit": 1,
            "creation_mana_limit": 1,
            "creation_attack_power": 1, "creation_attack_count": 1,
            "summary": f"生命{3 * x}，创造{x}个1/1/1的造物",
        }


    @staticmethod
    def calculate_yapo(x: int, target: Entity = None) -> dict:
        """压迫X：支付：法力10X。除[目标]外，所有角色仅能显式选择自身为[目标]，持续X。

        压迫是飞行的反转：持续状态挂在本次显式[目标]身上；除该目标外，
        其他角色仅能显式选择自身为[目标]。不检查生命、不判死。
        """
        target_name = target.name if target is not None else "[目标]"
        return {
            "dao_wen": "压迫", "x": x,
            "cost_type": CostType.MANA.value, "cost": 10 * x,
            "duration": x,
            "summary": f"支付：{10*x}法力；除{target_name}外，所有角色仅能显式选择自身为[目标]，持续{x}回合",
        }

    @staticmethod
    def calculate_diaoling(x: int, target: Entity = None) -> dict:
        """凋零X：支付：法力10X。除[目标]外，当前生命≤X的角色立即死亡。

        一次性瞬发效果：只在施放当次按场上快照判定，不挂持续状态；后续生命变化
        或新角色入场不会再次触发。判定当前生命而非血限，施法者并非目标时同样
        参与判定；直接判死而非伤害。
        """
        target_name = target.name if target is not None else "[目标]"
        return {
            "dao_wen": "凋零", "x": x,
            "cost_type": CostType.MANA.value, "cost": 10 * x,
            "diaoling_threshold": x,
            "summary": (f"支付：{10 * x}法力，除{target_name}外，"
                        f"当前生命≤{x}的角色立即死亡"),
        }

    @staticmethod
    def calculate_gouhun(x: int, target: Entity = None) -> dict:
        """勾魂X：支付：法力X。使[目标]无法获得[法力]，持续X。

        改版（2026-08-30，DM 裁定见 报告.md 硬伤2-C）：
        旧版为「[回始]使[目标]失去2X点当前法力，持续∞」——永久扣法力对输出决策
        是单向碾压，且玩家只能靠残韵改掉怪物道纹来止损。新版改为**持续X回合
        无法获得法力**：[回始]法力回填被压制（不扣已有法力），X 回合后自然恢复。
        这样威胁是"暂时断蓝"而非"永久死刑"，且期限明确（与【坏死】禁回复同构）。

        修复（2026-08-21）：补上 target 参数使该道纹正确声明需要[目标]，
        否则 requires_target=False 导致怪物只能自施（寄骨蝇勾魂自吸无法力=空放）。

        2026-09-09 DM裁定：法力改一池制后一度改为「支付：法力翻倍」；
        2026-10-08 用户令：恢复「无法获得[法力]」原意，翻倍逻辑撤销。
        此时战斗内已有多个法力来源（聚能／储能电池／守夜灯／承露盏／
        透支／法术），效果重新有对象。判定位统一在
        `Entity.can_gain_mana()` / `Entity.gain_mana()`，所有法力增益都必须过它。
        """
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "勾魂", "x": x,
            "cost_type": CostType.MANA.value, "cost": x,
            "no_mana_gain": True, "duration": x,
            "summary": f"支付：{x}法力，{target_name}无法获得法力，持续{x}回合"
        }

    # 2026-10-08 用户令删除【镇尸】（支付：法力2X。使一个[目标]无法获得[回复]，持续X）。
    # 它与【坏死】是同一效果的**两套实现**（坏死=effect 字符串状态，镇尸=no_heal 布尔钩子），
    # 属硬重复。保留【坏死】作为「无法获得[回复]」的唯一实现；乱葬岗闭环上这一格
    # 留成空占位符（见 RESERVED_SLOT），不直连、不拿别的道纹顶替。

    @staticmethod
    def calculate_zhaohun(x: int) -> dict:
        """招魂X：支付：法力3X。唤回1具已击灭的怪物尸体作为[造物]，生命为20X。"""
        return {
            "dao_wen": "招魂", "x": x,
            "cost_type": CostType.MANA.value, "cost": 3 * x,
            "revive_temp_friend": True, "temp_hp": 20 * x,
            "summary": f"支付：{3 * x}法力，唤回1具已灭怪物作为造物（生命{20*x}）"
        }

    @staticmethod
    def calculate_zhenli(x: int, target: Entity = None) -> dict:
        """真理X：支付：冷却2场。提出 X 句陈述，要求[目标]逐句言明真理。

        2026-10-23 用户裁定：X 不再是固定值，改为「X 句话」——X 决定陈述句数，
        每有一句无法言明真理，[目标]即无法开口，持续 X 回合（X 同时放大强度与持续）。
        任一句的真伪均由 DM 裁定。
        """
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "真理", "x": x,
            "cost_type": CostType.COOLDOWN.value, "cost": 2,
            "truth_required": True,
            "truth_sentences": x,
            "silence_rounds": x,
            "summary": (f"冷却2场，向{target_name}提出{x}句陈述，要求其逐句言明真理；"
                        f"任一句无法言明则无法开口，持续{x}回合；真伪由DM裁定"),
        }

    @staticmethod
    def calculate_weiyi(x: int, target: Entity = None) -> dict:
        """唯一X：支付：法力5X。使[目标]无法使用用过的道纹，持续X。

        只要成功使用过就计入历史，不限直接发动、法术步骤、瞬发或其它使用方式。
        """
        if x < 1:
            raise ValueError("X必须为正整数")
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "唯一", "x": x, "cost_type": CostType.MANA.value,
            "cost": 5 * x, "used_daowen_lock": True, "duration": x,
            "summary": f"支付：法力{5*x}，使{target_name}无法使用用过的道纹，持续{x}",
        }

    # ========== 统一调度入口 ==========
    
    _registry: dict = {}  # 运行时注册
    
    @classmethod
    def register_all(cls):
        """注册所有道纹计算函数"""
        cls._registry = {
            "杀伐": cls.calculate_shaifa,
            "再生": cls.calculate_zaisheng,
            "庇护": cls.calculate_bihu,
            "连击": cls.calculate_lianji,
            "波及": cls.calculate_boba,
            "共罪": cls.calculate_gongzui,
            "血契": cls.calculate_xueqi,
            "增殖": cls.calculate_zengzhi,
            "束缚": cls.calculate_shufu,
            "透支": cls.calculate_touzhi,
            "封印": cls.calculate_fengyin,
            # 怪物原始（2026-10-25 用户令：【疯狂】【减速】遗物化，退出道纹体系）
            "全力": cls.calculate_quanli,
            "强化": cls.calculate_qianghua,
            "飞行": cls.calculate_feixing,
            "唯一": cls.calculate_weiyi,
            "净化": cls.calculate_jinghua,
            "出土": cls.calculate_chutu,
            "篡改": cls.calculate_cuangai,
            "催化": cls.calculate_cuihua,
            "不死": cls.calculate_busi,
            "必中": cls.calculate_bizhong,
            # 怪物转化（2026-10-11 A14：无神/借力/衰败 已删）
            "弱化": cls.calculate_ruhua,
            "全速": cls.calculate_quansu,
            "压迫": cls.calculate_yapo,
            # 扭曲都市
            "变形": cls.calculate_bianxing,
            "定型": cls.calculate_dingxing,
            "超频": cls.calculate_chaopin,
            "萎缩": cls.calculate_weisuo,
            "坏死": cls.calculate_huaisi,
            # 罪孽都市
            "点金": cls.calculate_dianjin,
            # 2026-10-13 用户令：【逼债】删除，【典当】顶替其闭环格（点金—转换—典当—反转—豪夺）。
            "典当": cls.calculate_diandang,
            "豪夺": cls.calculate_haoduo,
            "抢劫": cls.calculate_qiangjie,
            # 2026-10-14 用户令：补点金反转分叉，还债新生产者。
            "勒索": cls.calculate_lesuo,
            "假钞": cls.calculate_jiachao,
            # 龙心谷
            # ---- 沉沦海（二阶）----
            "唤潮": cls.calculate_chao,
            "蜃景": cls.calculate_shenjing,
            "流动": cls.calculate_liudong,
            "遗忘": cls.calculate_yiwang,
            "冰冻": cls.calculate_bingdong,
            "大梦": cls.calculate_dameng,
            "沉溺": cls.calculate_chenni,
            "回响": cls.calculate_huixiang,
            # ---- 乱葬岗（二阶）----
            "分裂": cls.calculate_fenlie,
            "凋零": cls.calculate_diaoling,
            "勾魂": cls.calculate_gouhun,
            "招魂": cls.calculate_zhaohun,
            # 初拥之夜专属道纹；只允许事件授予，不进入常规学习/残韵池。
            "真理": cls.calculate_zhenli,
        }
    
    @staticmethod
    def single_round_action_count(entity: Entity) -> int:
        """所有角色基础1出手；禁止用攻击次数冒充，也不按造物/怪物另分基础值。"""
        return max(0, entity.action_count) if entity is not None else 0

    @classmethod
    def migration_status(cls, dao_wen_name: str) -> dict[str, str]:
        """返回该道纹是否已迁入统一数值规则；未迁移项仍走旧行为。"""
        return daowen_migration_status(dao_wen_name)

    @classmethod
    def resolve(cls, dao_wen_name: str, x: int, **kwargs) -> dict:
        """
        统一道纹计算入口
        AI必须通过此接口调用，禁止自行计算
        自动检查X上限
        """
        if not cls._registry:
            cls.register_all()
        
        if dao_wen_name not in cls._registry:
            raise ValueError(f"未知道纹: {dao_wen_name}。可用道纹: {list(cls._registry.keys())}")
        
        # 唯一：任何方式已经成功使用过的道纹都受同一历史门禁。
        caster = kwargs.get("caster")
        if (caster is not None and hasattr(caster, "has_status")
                and caster.has_status("唯一")
                and dao_wen_name in getattr(caster, "daowen_use_history", set())):
            raise ValueError(f"唯一生效中：{caster.name}已经用过【{dao_wen_name}】，不能再次使用")

        # 退化X（扭曲都市专属，持续∞）：使目标每次发动道纹时该次数值-X（最低0）。
        # 在 resolve 统一入口削减（玩家/怪物/同伴所有发动路径都经此），与 sim/balance_sim eff_x 同口径。
        if (not kwargs.get("_skip_status_decay", False)
                and caster is not None and hasattr(caster, "has_status")
                and caster.has_status("退化")):
            x = max(0, x - caster.get_status_value("退化"))

        # 获取该道纹的代价类型，检查X上限
        # 先调用一次获取cost_type
        func = cls._registry[dao_wen_name]

        # 2026-10-09 用户令：波及 X≥2 下限 repealed——X_MIN 装置整套拔除，
        # 不再有「低于下限」的拦截；波及 X=1 合法（单目标全值，见 daowen_effect 分路）。

        import inspect
        sig = inspect.signature(func)
        params = {}
        for param_name in sig.parameters:
            if param_name in kwargs:
                params[param_name] = kwargs[param_name]
            elif param_name == 'x':
                params['x'] = x
        
        result = func(**params)
        
        # 检查X上限
        cost_type = result.get("cost_type", "支付：")
        if cost_type in cls.X_LIMITS:
            state = kwargs.get("_state", {})
            x_max = cls.X_LIMITS[cost_type](state)
            if x > x_max:
                raise ValueError(
                    f"X={x}超过上限{cost_type}≤{x_max}。"
                    f"道纹: {dao_wen_name}, 代价类型: {cost_type}"
                )
        
        return result
    
    @classmethod
    def list_all(cls) -> list[str]:
        """列出所有已注册道纹"""
        if not cls._registry:
            cls.register_all()
        return list(cls._registry.keys())


# 残韵系统
class ResonanceEngine:
    """
    残韵计算引擎
    转换（平向支流）：平移法则维度
    反转（极性对冲）：逆转因果极性
    曲解（概念腐化）：扭曲代数逻辑

    2026-10-07 用户令：残韵路径**双向**。`CLOSED_LOOPS` 里的每条边 `(src, 类型, dst)`
    是无向边：src 与 dst 互为闭环上的相邻节点，两个方向都走**同一种**残韵、
    **同样**支付：一次（规则正文：沿闭环选择一条相邻的变化路径）。边表只登记一次，
    不重复登记反向边——双向由 `find_transformations` 在查询时展开，避免边数与
    「闭环 N 条边」的口径翻倍。
    """

    # 闭环结构定义
    CLOSED_LOOPS = {
        "杀伐闭环": [
            ("杀伐", "反转", "再生"),
            ("再生", "曲解", "庇护"),
            ("庇护", "曲解", RELICIZED_DAOWEN_SLOTS["固执"]),
            (RELICIZED_DAOWEN_SLOTS["固执"], "反转", "连击"),
            # 波及迁入沉沦海、增殖迁入扭曲都市后，原位保留待补节点，不直连。
            ("连击", "转换", RELOCATED_DAOWEN_SLOTS["增殖"]),
            # 删除慈悲/切割/缓慢后保留既有空位；贯穿→(曲解)束缚：概念腐化。
            (RELOCATED_DAOWEN_SLOTS["增殖"], "曲解", "透支"),
            ("透支", "转换", RELICIZED_DAOWEN_SLOTS["贯穿"]),
            (RELICIZED_DAOWEN_SLOTS["贯穿"], "曲解", "束缚"),
            ("束缚", "曲解", "封印"),
            ("封印", "反转", "杀伐"),
        ],
        # ---- 副本专属闭环（规则正文·副本专属道纹网络）----
        "扭曲都市闭环": [
            ("变形", "转换", "定型"),
            ("定型", "反转", RELICIZED_DAOWEN_SLOTS["畸变"]),
            (RELICIZED_DAOWEN_SLOTS["畸变"], "曲解", "超频"),
            # 增殖由杀伐闭环迁入扭曲都市，占据超频与坏死之间的原待补位。
            ("超频", "反转", "增殖"),
            ("增殖", "转换", "坏死"),
            # 【退化】已遗物化：该节点保留独立「？？」占位。
            ("坏死", "曲解", RELICIZED_DAOWEN_SLOTS["退化"]),
            (RELICIZED_DAOWEN_SLOTS["退化"], "转换", "变形"),
        ],
        "扭曲都市分叉": [
            ("超频", "反转", "萎缩"),
        ],
        "罪孽都市闭环": [
            ("点金", "转换", "典当"),
            ("典当", "反转", "豪夺"),
            ("豪夺", "曲解", RELICIZED_DAOWEN_SLOTS["清算"]),
            (RELICIZED_DAOWEN_SLOTS["清算"], "反转", "抢劫"),
            ("抢劫", "转换", "假钞"),
            # 2026-10-29：既有【勒索】补入假钞—点金之间的通用空位；点金反转分叉另表保留。
            ("假钞", "曲解", "勒索"),
            ("勒索", "曲解", "点金"),
        ],
        # ---- 副本专属分叉（非闭环：单边spur，不参与合拢断言）----
        # 2026-10-14 用户令：补【点金】的【反转】→【勒索】。点金已有出边
        # （转换→典当），此边是第二条出边——find_transformations 按（源，残韵类型）
        # 查询天然支持（杀伐/全力先例），且点金+反转此前无路径、无歧义。
        # 不进「罪孽都市闭环」表：该表的合拢断言要求入度=出度=1（test_region_loops_are_closed），
        # spur 进去会破坏合拢；索引 loop_chain 也假设出度1（dict 会静默吞边）。
        # 查询侧（get_available_resonance/find_transformations）遍历全表，分叉自动生效。
        "罪孽都市分叉": [
            ("点金", "反转", "勒索"),
        ],
        "龙心谷闭环": [
            (RELICIZED_DAOWEN_SLOTS["加害"], "反转", RELICIZED_DAOWEN_SLOTS["龙鳞"]),
            (RELICIZED_DAOWEN_SLOTS["龙鳞"], "曲解", RELICIZED_DAOWEN_SLOTS["逆鳞"]),
            (RELICIZED_DAOWEN_SLOTS["逆鳞"], "转换", RELICIZED_DAOWEN_SLOTS["裂变"]),
            # 2026-10-11 用户令（A13）：【嫁祸】整条删除（效果与遗物【贪婪契约】重复）
            #   → 沿用活血/尸爆的删除先例：断边**重连**为 裂变—转换→背负，环长 7→6。
            (RELICIZED_DAOWEN_SLOTS["裂变"], "转换", RELICIZED_DAOWEN_SLOTS["背负"]),
            (RELICIZED_DAOWEN_SLOTS["背负"], "曲解", RELICIZED_DAOWEN_SLOTS["伤痕"]),
            (RELICIZED_DAOWEN_SLOTS["伤痕"], "转换", RELICIZED_DAOWEN_SLOTS["加害"]),
        ],
        "荒疫古城闭环": [
            ("感染", "反转", "净化"),
            ("净化", "曲解", "出土"),
            ("出土", "转换", "篡改"),
            ("篡改", "曲解", "唯一"),
            ("唯一", "反转", "催化"),
            ("催化", "转换", "重演"),
            ("重演", "反转", "原初"),
            ("原初", "转换", "感染"),
        ],
        "荒疫古城分叉": [
            ("重演", "反转", "唯一"),
        ],
        "乱葬岗闭环": [
            # 【缄默】已遗物化：该节点保留独立「？？」占位，不与相邻节点直连。
            ("分裂", "转换", RELICIZED_DAOWEN_SLOTS["缄默"]),
            (RELICIZED_DAOWEN_SLOTS["缄默"], "曲解", RELICIZED_DAOWEN_SLOTS["瓦解"]),
            (RELICIZED_DAOWEN_SLOTS["瓦解"], "转换", RELICIZED_DAOWEN_SLOTS["冥气"]),
            (RELICIZED_DAOWEN_SLOTS["冥气"], "反转", "勾魂"),
            # 2026-10-29：既有【凋零】补入勾魂—招魂之间的待补格；环仍为7条边。
            ("勾魂", "曲解", "凋零"),
            ("凋零", "曲解", "招魂"),
            ("招魂", "转换", "分裂"),
        ],
        "沉沦海闭环": [
            ("流动", "转换", "波及"),
            ("波及", "反转", "遗忘"),
            ("遗忘", "曲解", "冰冻"),
            ("冰冻", "曲解", "大梦"),
            ("大梦", "曲解", "回响"),
            ("回响", "转换", "流动"),
        ],
        # 大梦的反转分支不进六节点主环；双向查询仍由统一边表展开。
        "沉沦海分叉": [
            ("大梦", "反转", "沉溺"),
        ],
        # ---- 原始怪物道纹 → 转化道纹（规则正文）----
        # 非闭环，是以原始道纹为根的分支树；怪物面板上的道纹多属此类，
        # 残韵边保留独立占位节点；飞行属于原始怪物道纹，压迫属于其转化道纹。
        "怪物原始道纹": [
            # 2026-10-03 删除（用户令）：("狂暴", "转换", "愤怒"),
            # 2026-10-03 删除（用户令）：("狂暴", "曲解", "无神"),
            # 2026-10-11 删除（用户令 A14）：("全力", "转换", "借力"),
            #   【借力】整条删除。按用户口径「在没有整体大改之前，增加新道路纯粹
            #   增加工作量」，这条断边**不补新道路**（与活血/尸爆/嫁祸的重连先例不同）。
            #   【全力】保留 反转→弱化；曲解原通向【自食】，现并入【再生】。
            # 2026-10-26 用户令：【全速】与【全力】接上——两者同为「把某项面板顶到上限」
            # 的化身，攻击力⇄攻击次数，用【转换】相连（此前【减速】遗物化后【全速】无入边）。
            ("全力", "转换", "全速"),
            ("全力", "反转", "弱化"),
            ("弱化", "反转", "强化"),
            ("全力", "曲解", "再生"),
            # 2026-10-25 用户令：【疯狂】【减速】遗物化，退出道纹体系，
            # 各自的残韵边随节点一并撤销。
            ("必中", "转换", RELICIZED_DAOWEN_SLOTS["洞察"]),
            ("必中", "反转", RELICIZED_DAOWEN_SLOTS["蒙蔽"]),
            # 2026-10-03 删除（用户令）：("飞行", "转换", "滑翔")。
            # 2026-10-29 用户令：飞行恢复为冷却X道纹，反转为目标限制【压迫】。
            ("飞行", "反转", "压迫"),
        ],
    }
    
    @classmethod
    def find_transformations(cls, source_daowen: str, resonance_type: str) -> list[dict]:
        """
        双向查找残韵变化结果（2026-10-07 用户令：路径双向）
        source_daowen: 源道纹名
        resonance_type: 残韵类型（转换/反转/曲解）
        返回：[{resonance_type, target_daowen, direction, loop}, ...]

        每条登记边 (src, 类型, dst) 正反两向都可用：正向 src→dst、反向 dst→src，
        支付：同种残韵、同样一次。同一个源道纹在同一个残韵类型上可能通向**两个**
        相邻节点（例：【杀伐】的【反转】同时通向【再生】与【封印】），此时
        `find_transformation` 不替调用方挑，必须由发动者显式指定走哪一条。
        """
        seen: set = set()
        out: list[dict] = []
        for loop_name, edges in cls.CLOSED_LOOPS.items():
            for src, rtype, dst in edges:
                if rtype != resonance_type:
                    continue
                if src == source_daowen:
                    direction, other = "正向", display_resonance_node(dst)
                elif dst == source_daowen:
                    direction, other = "反向", display_resonance_node(src)
                else:
                    continue
                if other in seen:
                    continue
                seen.add(other)
                item = {
                    "resonance_type": rtype,
                    "target_daowen": other,
                    "direction": direction,
                    "loop": loop_name,
                }
                if is_reserved_slot(other):
                    # 空占位符：结构上保留这一格，但标记出来，调用方不得把它当可获得的道纹。
                    item["reserved"] = True
                out.append(item)
        return out

    @classmethod
    def find_transformation(cls, source_daowen: str, resonance_type: str,
                            target_daowen: str = "") -> Optional[str]:
        """
        单值查找残韵变化结果
        target_daowen: 歧义时指定要走向哪个相邻节点（正向或反向皆可）
        返回：变化后的道纹名；路径不存在、或歧义而未指定时返回 None
        """
        candidates = cls.find_transformations(source_daowen, resonance_type)
        if not candidates:
            return None
        if target_daowen:
            for cand in candidates:
                if cand["target_daowen"] == target_daowen:
                    return target_daowen
            return None
        if len(candidates) > 1:
            return None      # 歧义：不替调用方取第一项，须显式指定
        return candidates[0]["target_daowen"]

    @classmethod
    def is_ambiguous_path(cls, source_daowen: str, resonance_type: str) -> bool:
        """该（源道纹, 残韵类型）是否通向两个相邻节点，需要发动者显式选择。"""
        return len(cls.find_transformations(source_daowen, resonance_type)) > 1

    @classmethod
    def get_available_resonance(cls, source_daowen: str) -> list[dict]:
        """获取某个道纹可用的残韵变化（双向：正向在前，反向在后）"""
        forward: list[dict] = []
        backward: list[dict] = []
        for loop_name, edges in cls.CLOSED_LOOPS.items():
            for src, rtype, dst in edges:
                if src == source_daowen:
                    forward.append({
                        "resonance_type": rtype,
                        "target_daowen": display_resonance_node(dst),
                        "direction": "正向",
                        "loop": loop_name,
                    })
                elif dst == source_daowen:
                    backward.append({
                        "resonance_type": rtype,
                        "target_daowen": display_resonance_node(src),
                        "direction": "反向",
                        "loop": loop_name,
                    })
        out: list[dict] = []
        seen: set = set()
        for item in forward + backward:
            key = (item["resonance_type"], item["target_daowen"])
            if key in seen:
                continue
            seen.add(key)
            if is_reserved_slot(item["target_daowen"]):
                item["reserved"] = True
            out.append(item)
        return out

    @classmethod
    def apply_resonance(
        cls,
        source_daowen: str,
        resonance_type: str,
        caster_has_daowen: bool,
        target_has_daowen: bool,
        resonance_stock: dict = None,
        target_daowen: str = ""
    ) -> dict:
        """
        应用残韵变化
        规则：
        1. 残韵作用于任意角色拥有的道纹时，将其永久变为变化后的道纹
        2. 施法者同时永久获得变化后的道纹
        3. 通过残韵获得的道纹，X值按施法者自由控X规则自定义
        4. 路径双向：正反向同一种残韵、同样支付：（2026-10-07 用户令）
        """
        # 检查路径是否存在（双向展开）
        candidates = cls.find_transformations(source_daowen, resonance_type)
        if not candidates:
            return {
                "success": False,
                "error": f"道纹'{source_daowen}'不存在'{resonance_type}'路径"
            }

        # 空占位符：这一格在闭环上留着，但**不能获得**。
        # 走到它 = 无事发生：不支付：残韵、不改写牌面、不授予施法者。
        # 放在这里（而不是 find 层）拦，是为了让闭环结构仍可被验证（环长、入度/出度、
        # roundtrip 可达性都得照常成立），只有真正发动时才拒绝。
        if target_daowen:
            if is_reserved_slot(target_daowen):
                if target_daowen == RELICIZED_SLOT:
                    detail = "是道纹遗物化后保留的闭环占位符，不是可获得的道纹"
                else:
                    detail = "是闭环上的空占位符，尚未补入道纹，无法获得"
                return {
                    "success": False,
                    "error": f"'{target_daowen}'{detail}",
                    "reserved": True,
                }
        else:
            real = [c for c in candidates if not c.get("reserved")]
            placeholders = [c["target_daowen"] for c in candidates if c.get("reserved")]
            if not real and placeholders:
                if RELICIZED_SLOT in placeholders:
                    detail = f"仅通向遗物化道纹占位符{RELICIZED_SLOT}，不可获得"
                else:
                    detail = "仅通向闭环空占位符（待补），尚未补入道纹，无法获得"
                return {
                    "success": False,
                    "error": f"道纹'{source_daowen}'的'{resonance_type}'路径{detail}",
                    "reserved": True,
                }

        names = "／".join(cand["target_daowen"] for cand in candidates)
        if target_daowen:
            chosen = next((c for c in candidates if c["target_daowen"] == target_daowen), None)
            if chosen is None:
                return {
                    "success": False,
                    "error": (f"道纹'{source_daowen}'的'{resonance_type}'路径不通向"
                              f"'{target_daowen}'（可选：{names}）")
                }
        elif len(candidates) > 1:
            return {
                "success": False,
                "error": (f"道纹'{source_daowen}'的'{resonance_type}'通向两个相邻节点"
                          f"（{names}），须显式指定 target_daowen"),
                "ambiguous": True,
                "candidates": [c["target_daowen"] for c in candidates],
            }
        else:
            chosen = candidates[0]

        # 闭环草案节点、种族专属与血族专属都不能通过普通残韵越过获取门禁。
        target_name = chosen["target_daowen"]
        if target_name in UNIMPLEMENTED_DAOWEN:
            owner = UNIMPLEMENTED_DAOWEN[target_name]
            return {
                "success": False,
                "error": f"道纹【{target_name}】属于{owner}草案，尚未接入运行时，不能通过残韵获得",
                "unimplemented": True,
            }
        race_owner = next((race for race, pool in RACE_EXCLUSIVE_DAOWEN.items()
                           if target_name in pool), None)
        if race_owner is not None:
            return {
                "success": False,
                "error": f"道纹【{target_name}】是{race_owner}专属道纹，只能通过{race_owner}专属机制获得，不能通过残韵获得",
                "exclusive": True,
            }
        if target_name in BLOODLINE_EXCLUSIVE_DAOWEN:
            return {
                "success": False,
                "error": f"道纹【{target_name}】是血族专属道纹，只能通过血族专属机制获得，不能通过残韵获得",
                "exclusive": True,
            }

        # 检查玩家是否拥有该类型残韵
        if resonance_stock is not None:
            available = resonance_stock.get(resonance_type, 0)
            if available <= 0:
                return {
                    "success": False,
                    "error": f"没有可用的{resonance_type}残韵（当前：{resonance_stock}）"
                }

        target = chosen["target_daowen"]
        return {
            "success": True,
            "source": source_daowen,
            "resonance_type": resonance_type,
            "target": target,
            "direction": chosen["direction"],
            "permanent_change": True,
            "caster_gets_daowen": True,
            "summary": f"【{resonance_type}】{source_daowen} → {target}",
        }


# 初始化注册
DaoWenEngine.register_all()
