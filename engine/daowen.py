"""
道纹系统 - 所有道纹效果的数学计算
规则：道纹是概念，不是技能。两字词，世界最小规则单元。
"""
from __future__ import annotations
from typing import Optional, Any
from .models import Entity, StatusEffect, DaoWen, DaoWenInstance
from .enums import CostType
from .rule_engine import daowen_migration_status, get_daowen_rule
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
    "冥气", "退化", "畸变", "缄默",
)}


def is_reserved_slot(name) -> bool:
    """该名字是否为闭环上的非道纹占位节点；带内部标识的遗物化空位也算占位。"""
    return name in PLACEHOLDER_SLOTS or (
        isinstance(name, str) and name.startswith("？？〔") and name.endswith("〕"))


# 道纹改名别名（旧档/旧面板读入时映射；引擎注册表只收新名，旧名提交照常报未知道纹）。
# 2026-10-22 用户令：【血债】改名【连击】并扩为多支付道纹，旧名只许在读入边界被映射。
LEGACY_DAOWEN_ALIASES = {"血债": "连击"}


def normalize_daowen_name(name):
    """旧道纹名 → 现名；非旧名原样返回。只用于读入/授予边界，不用于发动提交。"""
    return LEGACY_DAOWEN_ALIASES.get(name, name)


def display_resonance_node(name):
    """内部每个遗物化节点独立编号，玩家/索引统一显示为「？？」。"""
    return RELICIZED_SLOT if isinstance(name, str) and name.startswith("？？〔") else name


class DaoWenEngine:
    """道纹计算引擎"""

    # 怪物转化道纹（原始怪物道纹经残韵变化后的分支，与规则正文《原始怪物道纹与转化道纹》一致）
    # 用于"雇佣"后"发现并选择一种转化道纹"等需要从此类别中随机抽取的场景
    # 2026-10-08 清理：原孤儿转化道纹已移除（曾挂在【狂暴】的反转分支下）。
    TRANSFORMED_DAOWEN = [
        "愤怒", "弱化", "全速", "蒙蔽",
        # 2026-10-11 用户令（A14）：【无神】【借力】【衰败】整条删除，不补位。
    ]

    # 波及X：选择X个目标建立/解除无限期效果分摊关系；X决定目标数与法力消耗。

    # X上限规则（代价类型 → 最大值函数）
    X_LIMITS = {
        "消耗": lambda state: float('inf'),     # 无上限，受法力限制
        "冷却": lambda state: 7,                 # 0≤X≤7
        "流血": lambda state: state.get("current_hp", 999),    # 0≤X≤当前生命
        "衰老": lambda state: state.get("blood_limit", 999),   # 0≤X≤当前血限
        "枯竭": lambda state: state.get("mana_limit", 999),    # 0≤X≤当前法限
        "萎缩": lambda state: state.get("speed_limit", 999),   # 0≤X≤当前速限
        "疲惫": lambda state: state.get("current_speed", 999), # 0≤X≤当前速度
        "失忆": lambda state: state.get("daowen_count", 999),  # 0≤X≤当前道纹数量
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
        """杀伐X：支付：法力X。对[目标]造成X²点伤害

        DM裁定 2026-09-10：法力改一池制后（战终才复原，不再每回合回填），道纹数值
        的约束从「速率」变成「预算」，可以放开。
        用户裁定 2026-09-13：由线性 5X 改为平方 X²——小X时弱于旧值（X≤4），
        大X时远强（X=10 打 100），把「攒法力一次性爆发」从习惯变成硬性最优解，
        与一池制预算的设计意图一致。
        """
        target_name = target.name if target is not None else "未选定目标"
        cost = x
        damage = x * x
        return {
            "dao_wen": "杀伐",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": cost,
            "target_damage": damage,
            "damage_type": "普通",
            "summary": f"支付：{x}法力，对{target_name}造成{damage}点伤害"
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
        """庇护X：支付：法力X。使[目标]获得2X点格挡（可抵消等量伤害，保留到被打掉或[战终]）"""
        target_name = target.name if target is not None else "未选定目标"
        cost = x
        shield = 2 * x
        return {
            "dao_wen": "庇护",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": cost,
            "target_shield": shield,
            "duration": 1,
            # DM裁定 2026-09-10：格挡不再[敌回终]清除，故文案不再声称"持续1回合"
            "summary": f"支付：{x}法力，使{target_name}获得{shield}点格挡（可抵消等量伤害，保留到被打掉或战终）"
        }
    
    @staticmethod
    def calculate_guzhi(x: int) -> dict:
        """固执X：支付：冷却X。最终实际失血最高为1，失血倍率后仍封顶。"""
        definition = get_daowen_rule("固执")
        cap = int(definition.rules[0].operator.operand)
        return {
            "dao_wen": "固执",
            "x": x,
            "cost_type": definition.cost_type,
            "cost": definition.cost(x),
            "duration": definition.duration.value(x),
            "max_current_damage_per_hit": cap,
            "max_life_loss_per_hit": cap,
            "summary": definition.summary(x),
        }

    # 连击六通道价格（2026-10-22 用户令：与【透支】同类多代价道纹，价格与效果全表同乘五）：
    # 流血5X／法力5X／衰老X／枯竭X／碎片5X／假碎片50X；效果 选择[目标]5X次、每次1点伤害。
    # 比率锚现有体系：【消灾】1单位=5碎片/50假碎片、【假钞】1法力=10假碎片、
    # 【透支】上限类按整X计价；全乘五后衰老/枯竭回到整数X计价，不留⌈X/5⌉零头，
    # 流血也回到旧【血债】「1点生命换1点伤害」的老比率（只是以5为一段）。
    # 各通道效果完全相同，只有倍率不同，不带任何通道专属机制
    # （演出差异不进引擎，用户 2026-10-22 令）。
    LIANJI_CHANNELS = ("流血", "法力", "衰老", "枯竭", "碎片", "假碎片")

    @staticmethod
    def calculate_lianji(x: int, target: Entity = None, cost_channel: str = "流血") -> dict:
        """连击X（2026-10-22 由【血债】改名）：支付六选一（见 LIANJI_CHANNELS 价格表）。

        选择[目标] 5X 次，每次对其造成 1 点伤害。支付途径只改倍率，
        效果恒同；cost_channel 缺省流血（怪物/旧调用/预设法术不传参即原行为），
        碎片与假碎片是两个独立通道，只扣各自池子（不走「假碎片优先」的合并失去）。
        """
        target_name = target.name if target is not None else "未选定目标"
        hits = 5 * x
        damage_per_hit = 1
        if cost_channel == "流血":
            extra = {"cost_type": CostType.BLEED.value, "cost_hp": 5 * x}
            price = f"流血{5 * x}"
        elif cost_channel == "法力":
            extra = {"cost_type": CostType.MANA.value, "cost": 5 * x}
            price = f"法力{5 * x}"
        elif cost_channel == "衰老":
            extra = {"cost_type": CostType.AGING.value, "cost_blood_limit": x}
            price = f"衰老{x}"
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
                f"连击cost_channel非法：{cost_channel!r}，可选流血/法力/衰老/枯竭/碎片/假碎片")
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
    
    # ---- 杀伐11节点闭环后半（增殖至封印）----
    
    @staticmethod
    def calculate_xueqi(x: int, target: Entity = None) -> dict:
        """血契X：支付：法力10X。选择X个目标，目标组彼此平分数值型代价，持续∞。"""
        cost = 10 * x
        return {
            "dao_wen": "血契", "x": x,
            "cost_type": CostType.MANA.value, "cost": cost,
            "mark_targets": x, "duration": -1,
            "summary": f"支付：法力{cost}。选择{x}个目标，目标组彼此平分数值型代价，持续∞。",
        }

    @staticmethod
    def calculate_zengzhi(x: int, target: Entity = None) -> dict:
        """增殖X：支付：流血10X。［战终］［目标］［血限］+2X

        2026-10-14 用户令重做：承接遗物【猩红果实】旧效果
        （战始可选流血10 → 战终血限+2），按 X 等比放大为流血10X → 战终+2X；
        原「支付：X、即时血限+X」废止。发动即付流血代价（走代价管线，
        触发【血誓戒】/【烙痕钉】等），血限增长记入战终待决、战终统一结算。
        """
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "增殖",
            "x": x,
            "cost_type": CostType.BLEED.value,
            "cost_hp": 10 * x,
            "zengzhi_battle_end": 2 * x,
            "summary": f"流血{10 * x}，战终{target_name}血限+{2 * x}"
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
    def calculate_touzhi(x: int, cost_channel: str = "流血") -> dict:
        """透支X：支付：（四选一）：流血4X／衰老2X／枯竭X／疲惫X。你获得X点法力

        用户裁定 2026-09-12：与【再生X】构成生命⇄法力闭环，净值不产生免费资源；
        实际上限由【癌变】(本场累计回复=2×血限) 自然封死。
        用户裁定 2026-09-13：3X→4X。与【再生X】(支付：法力X→回复4X生命) 对齐为
        严格 4:1 双向汇率，闭环净值归零，不再每轮白赚生命。
        2026-10-14 用户令：改为多代价道纹——合并【搏命】（疲惫X换X法力）并删除搏命；
        遗物【余火印】删除（其发动时额外耗龙心模式一并取消，龙心只走通用抵扣）。
        疲惫通道沿用搏命的 k=1 结论：卖X速度得kX法力可经【超频】买回 kX/2 速度，
        k≥2 即永动，故取 k=1（每卖1点速度净亏0.5点，循环必然收敛）。
        cost_channel 缺省流血（向后兼容：怪物/旧调用不传参即原行为）。
        """
        if cost_channel == "流血":
            cost_type, cost_key, amount = CostType.BLEED.value, "cost_hp", 4 * x
        elif cost_channel == "衰老":
            cost_type, cost_key, amount = CostType.AGING.value, "cost_blood_limit", 2 * x
        elif cost_channel == "枯竭":
            cost_type, cost_key, amount = CostType.EXHAUST.value, "cost_mana_limit", x
        elif cost_channel == "疲惫":
            cost_type, cost_key, amount = CostType.FATIGUE.value, "cost_speed", x
        else:
            raise ValueError(f"透支cost_channel非法：{cost_channel!r}，可选流血/衰老/枯竭/疲惫")
        return {
            "dao_wen": "透支",
            "x": x,
            "cost_type": cost_type,
            cost_key: amount,
            "mana_gain": x,
            "summary": f"{cost_channel}{amount}，获得{x}点法力"
        }
    
    @staticmethod
    def calculate_guanchuan(x: int) -> dict:
        """贯穿X：支付：法力2X。你造成的伤害无视格挡，持续X"""
        return {
            "dao_wen": "贯穿",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "duration": x,
            "effect": "伤害无视格挡",
            "summary": f"支付：{2 * x}法力，造成的伤害无视格挡，持续{x}回合"
        }
    
    @staticmethod
    def calculate_fengyin(x: int, target: Entity = None) -> dict:
        """封印X：支付：异变X，使一个[目标]延后X回合再入场。

        2026-10-08 用户令：目标由"只限怪物"放开为**任意目标**——敌人、[朋友]、
        [员工]、临时朋友、敌对轮回者，以及施法者自己都可以被封印。
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
    def calculate_jinghua(x: int, target: Entity = None) -> dict:
        """净化X：支付：法力2X。使[目标]【异变】-X层"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "净化",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "mutation_reduction": x,
            "summary": f"支付：{2 * x}法力，使{target_name}【异变】-{x}层",
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
    def calculate_fennu(x: int, target: Entity = None) -> dict:
        """愤怒X：支付：法力2X。使[目标]法力消耗减半，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "愤怒",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "mana_cost_halved": True,
            "duration": x,
            "summary": f"支付：{2 * x}法力，使{target_name}法力消耗减半，持续{x}回合"
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
    def calculate_mengbi(x: int, target: Entity = None) -> dict:
        """蒙蔽X：支付：法力2X。使[目标]下X次造成的伤害无效"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "蒙蔽",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "invalid_damage_hits": x,
            "summary": f"支付：{2 * x}法力，使{target_name}下{x}次造成的伤害无效"
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
    def calculate_huaisi(x: int, target: Entity = None) -> dict:
        """坏死X：支付：法力2X。使[目标]无法获得回复，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "坏死",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "duration": x,
            "effect": "无法获得回复",
            "summary": f"支付：{2 * x}法力，使{target_name}无法获得回复，持续{x}回合"
        }
    

    @staticmethod
    def calculate_jiahai(x: int, target: Entity = None) -> dict:
        """加害X：支付：法力2X。数值规则见 engine.rule_engine.DAOWEN_RULES。"""
        definition = get_daowen_rule("加害")
        target_name = target.name if target is not None else "未选定目标"
        cost = definition.cost(x)
        return {
            "dao_wen": "加害",
            "x": x,
            "cost_type": definition.cost_type,
            "cost": cost,
            "duration": definition.duration.value(x),
            "status": {"name": definition.rules[0].status_name, "value": x,
                       "duration": definition.duration.value(x)},
            "summary": definition.summary(x, target_name=target_name),
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
    def calculate_qingsuan(x: int, target: Entity = None, caster_shards: int = 0) -> dict:
        """清算X：支付：法力2X。[回始]使目标失去你碎片点格挡，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "清算", "x": x, "cost_type": CostType.MANA.value, "cost": 2 * x,
            "qingsuan_register": True, "duration": x,
            "summary": f"支付：{2 * x}法力，[回始]使{target_name}失去{caster_shards}格挡，持续{x}回合"
        }
    
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
    
    @staticmethod
    def calculate_xiaozai(x: int) -> dict:
        """消灾X：支付：50X假碎片/5X碎片（局外发动支付：×2）。重置随机数X次以改变结果"""
        return {
            "dao_wen": "消灾", "x": x, "cost_type": "碎片",
            "fake_cost": 50 * x, "real_cost": 5 * x,
            "rerolls": x,
            "summary": f"支付：{50*x}假碎片或{5*x}碎片（局外×2），重置随机数{x}次"
        }
    
    # ---- 龙心谷专属道纹 ----
    
    @staticmethod
    def calculate_longlin(x: int, target: Entity = None) -> dict:
        """龙鳞X：支付：法力2X。数值规则见 engine.rule_engine.DAOWEN_RULES。"""
        definition = get_daowen_rule("龙鳞")
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "龙鳞", "x": x, "cost_type": definition.cost_type,
            "cost": definition.cost(x),
            "damage_reduction": x, "duration": definition.duration.value(x),
            "summary": definition.summary(x, target_name=target_name),
        }


    @staticmethod
    def calculate_liebian(x: int, target: Entity = None) -> dict:
        """裂变X：支付：法力2X。使目标受到伤害改为分X次结算，持续∞"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "裂变", "x": x, "cost_type": CostType.MANA.value, "cost": 2 * x,
            "split_count": x, "duration": -1,
            "summary": f"支付：{2 * x}法力，{target_name}受伤分{x}次结算，永久"
        }
    
    @staticmethod
    # 2026-10-11 用户令（A13）：【嫁祸】整条删除——效果与遗物【贪婪契约】重复。

    
    @staticmethod
    def calculate_beifu(x: int, target: Entity = None) -> dict:
        """背负X：支付：法力2X。选择目标，其下X次受到伤害由自身承担"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "背负", "x": x, "cost_type": CostType.MANA.value, "cost": 2 * x,
            "absorb_count": x,
            "summary": f"支付：{2 * x}法力，{target_name}下{x}次受伤由自身承担"
        }
    

    # ========== 乱葬岗（二阶）专属道纹 ==========

    @staticmethod
    def calculate_fenlie(x: int, y: int = 1) -> dict:
        """分裂X/Y：支付：流血X×10Y。创造X个10Y[血限]的自身复制体。

        双参数道纹（引擎首个）：
          X = 复制体**数量**
          Y = 单个复制体的**规模档**，每个血限/生命 = 10Y
        代价【流血】= X×10Y，恰好等于造出来的**总血限**——造多少血就流多少
        血，不会凭空增殖，也不会因为本体血限高低而白赚或白亏。

        代价轴沿革：旧版「支付：冷却X；[命零]时创造X个本体血限20%的复制体」
        （触发时机绑死在[命零]、代价与产出无关、本体越壮越白赚，三项均已废止）
        → 2026-09-17 改为「即时发动 + 代价【衰老】X×10Y（扣血限）」
        → 2026-10-08 用户令：代价由【衰老】改为【流血】。数值不变，但付的是
        **当前生命**而不是[血限]：血限是永久损耗、生命可再生，于是分裂从
        「拿上限换数量」变成「拿血换数量」，付不起（生命不足）则不能发动。

        调用：DaoWenEngine.resolve("分裂", x, y=Y)；不传 y 时默认 1。
        """
        clone_hp = 10 * y
        return {
            "dao_wen": "分裂", "x": x, "y": y,
            "cost_type": CostType.BLEED.value, "cost_hp": x * clone_hp,
            "split_clones": x, "clone_hp": clone_hp,
            "summary": f"流血{x * clone_hp}，创造{x}个{clone_hp}血限的自身复制体"
        }


    @staticmethod
    def calculate_wajie(x: int, target: Entity = None) -> dict:
        """瓦解X：支付：法力3X。使一个[目标]的[血限]减少10X%。"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "瓦解", "x": x,
            "cost_type": CostType.MANA.value, "cost": 3 * x,
            "blood_limit_pct": 10 * x,
            "summary": f"支付：{3 * x}法力，{target_name}血限-{10*x}%"
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
        """招魂X：支付：法力3X。唤回1具已击灭的怪物尸体作为[临时朋友]，生命为20X。"""
        return {
            "dao_wen": "招魂", "x": x,
            "cost_type": CostType.MANA.value, "cost": 3 * x,
            "revive_temp_friend": True, "temp_hp": 20 * x,
            "summary": f"支付：{3 * x}法力，唤回1具已灭怪物作为临时朋友（生命{20*x}）"
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

    # ========== 统一调度入口 ==========
    
    _registry: dict = {}  # 运行时注册
    
    @classmethod
    def register_all(cls):
        """注册所有道纹计算函数"""
        cls._registry = {
            "杀伐": cls.calculate_shaifa,
            "再生": cls.calculate_zaisheng,
            "庇护": cls.calculate_bihu,
            "固执": cls.calculate_guzhi,
            "连击": cls.calculate_lianji,
            "波及": cls.calculate_boba,
            "血契": cls.calculate_xueqi,
            "增殖": cls.calculate_zengzhi,
            "束缚": cls.calculate_shufu,
            "透支": cls.calculate_touzhi,
            "贯穿": cls.calculate_guanchuan,
            "封印": cls.calculate_fengyin,
            # 怪物原始（2026-10-25 用户令：【疯狂】【减速】遗物化，退出道纹体系）
            "全力": cls.calculate_quanli,
            "净化": cls.calculate_jinghua,
            "必中": cls.calculate_bizhong,
            # 怪物转化（2026-10-11 A14：无神/借力/衰败 已删）
            "愤怒": cls.calculate_fennu,
            "弱化": cls.calculate_ruhua,
            "全速": cls.calculate_quansu,
            "蒙蔽": cls.calculate_mengbi,
            # 扭曲都市
            "变形": cls.calculate_bianxing,
            "定型": cls.calculate_dingxing,
            "超频": cls.calculate_chaopin,
            "坏死": cls.calculate_huaisi,
            # 罪孽都市
            "加害": cls.calculate_jiahai,
            "点金": cls.calculate_dianjin,
            # 2026-10-13 用户令：【逼债】删除，【典当】顶替其闭环格（点金—转换—典当—反转—豪夺）。
            "典当": cls.calculate_diandang,
            "豪夺": cls.calculate_haoduo,
            "清算": cls.calculate_qingsuan,
            "抢劫": cls.calculate_qiangjie,
            # 2026-10-14 用户令：补点金反转分叉，还债新生产者。
            "勒索": cls.calculate_lesuo,
            "假钞": cls.calculate_jiachao,
            "消灾": cls.calculate_xiaozai,
            # 龙心谷
            "龙鳞": cls.calculate_longlin,
            "裂变": cls.calculate_liebian,
            "背负": cls.calculate_beifu,
            # ---- 乱葬岗（二阶）----
            "分裂": cls.calculate_fenlie,
            "瓦解": cls.calculate_wajie,
            "勾魂": cls.calculate_gouhun,
            "招魂": cls.calculate_zhaohun,
            # 初拥之夜专属道纹；只允许事件授予，不进入常规学习/残韵池。
            "真理": cls.calculate_zhenli,
        }
    
    @staticmethod
    def single_round_action_count(entity: Entity) -> int:
        """本回合单轮出手预算（供判定类效果使用），禁止用攻击次数冒充。"""
        if entity is None:
            return 0
        if getattr(entity, "entity_type", "") == "怪物":
            n = 2  # 1 攻 + 1 纹
            n += entity.get_status_value("疯狂")
            return max(0, n)
        return max(0, entity.action_count)

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
        
        # 退化X（扭曲都市专属，持续∞）：使目标每次发动道纹时该次数值-X（最低0）。
        # 在 resolve 统一入口削减（玩家/怪物/同伴所有发动路径都经此），与 sim/balance_sim eff_x 同口径。
        caster = kwargs.get("caster")
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
            ("庇护", "曲解", "固执"),
            ("固执", "反转", "连击"),
            ("连击", "转换", "波及"),
            # 删除慈悲/切割/缓慢后直连（2026-08-21）：
            # 波及→(反转)增殖：扩散伤害与成长的极性对冲；贯穿→(曲解)束缚：概念腐化。
            ("波及", "反转", "增殖"),
            ("增殖", "曲解", "透支"),
            ("透支", "转换", "贯穿"),
            ("贯穿", "曲解", "束缚"),
            ("束缚", "曲解", "封印"),
            ("封印", "反转", "杀伐"),
        ],
        # ---- 副本专属闭环（规则正文·副本专属道纹网络）----
        "扭曲都市闭环": [
            ("变形", "转换", "定型"),
            ("定型", "反转", RELICIZED_DAOWEN_SLOTS["畸变"]),
            (RELICIZED_DAOWEN_SLOTS["畸变"], "曲解", "超频"),
            # 2026-10-14 用户令删除【搏命】（并入【透支】）→ 该格留成空占位符
            # （沿用 2026-10-08 删【镇尸】先例：不直连、不顶替，环仍为 7 条边；
            # 用户 2026-10-14 拍板占位，否决直连方案）。
            ("超频", "反转", RESERVED_SLOT),
            (RESERVED_SLOT, "转换", "坏死"),
            # 【退化】已遗物化：该节点保留独立「？？」占位。
            ("坏死", "曲解", RELICIZED_DAOWEN_SLOTS["退化"]),
            (RELICIZED_DAOWEN_SLOTS["退化"], "转换", "变形"),
        ],
        "罪孽都市闭环": [
            ("点金", "转换", "典当"),
            ("典当", "反转", "豪夺"),
            ("豪夺", "曲解", "清算"),
            ("清算", "反转", "抢劫"),
            ("抢劫", "转换", "假钞"),
            # 2026-10-14 用户令：凡道纹遗物化，原闭环节点直接移除并以 RELICIZED_SLOT（？？）占位；保留两侧边，不重连。
            ("假钞", "曲解", RELICIZED_SLOT),
            (RELICIZED_SLOT, "反转", "消灾"),
            ("消灾", "曲解", "点金"),
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
            ("加害", "反转", "龙鳞"),
            ("龙鳞", "曲解", RELICIZED_DAOWEN_SLOTS["逆鳞"]),
            (RELICIZED_DAOWEN_SLOTS["逆鳞"], "转换", "裂变"),
            # 2026-10-11 用户令（A13）：【嫁祸】整条删除（效果与遗物【贪婪契约】重复）
            #   → 沿用活血/尸爆的删除先例：断边**重连**为 裂变—转换→背负，环长 7→6。
            ("裂变", "转换", "背负"),
            ("背负", "曲解", RELICIZED_DAOWEN_SLOTS["伤痕"]),
            (RELICIZED_DAOWEN_SLOTS["伤痕"], "转换", "加害"),
        ],
        "乱葬岗闭环": [
            # 【缄默】已遗物化：该节点保留独立「？？」占位，不与相邻节点直连。
            ("分裂", "转换", RELICIZED_DAOWEN_SLOTS["缄默"]),
            (RELICIZED_DAOWEN_SLOTS["缄默"], "曲解", "瓦解"),
            ("瓦解", "转换", RELICIZED_DAOWEN_SLOTS["冥气"]),
            (RELICIZED_DAOWEN_SLOTS["冥气"], "反转", "勾魂"),
            # 2026-10-08 用户令删除【镇尸】（与【坏死】硬重复）→ 该格留成空占位符，
            # 不直连【勾魂】与【招魂】，也不拿别的道纹顶替；环仍为 7 条边。
            ("勾魂", "曲解", RESERVED_SLOT),
            (RESERVED_SLOT, "曲解", "招魂"),
            ("招魂", "转换", "分裂"),
        ],
        # ---- 原始怪物道纹 → 转化道纹（规则正文）----
        # 非闭环，是以原始道纹为根的分支树；怪物面板上的道纹多属此类，
        # 残韵边保留独立占位节点；原始怪物道纹集合不含飞行。
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
            ("全力", "曲解", "再生"),
            # 2026-10-25 用户令：【疯狂】【减速】遗物化，退出道纹体系，
            # 各自的残韵边随节点一并撤销。
            ("必中", "转换", RELICIZED_DAOWEN_SLOTS["洞察"]),
            ("必中", "反转", "蒙蔽"),
            # 2026-10-03 删除（用户令）：("飞行", "转换", "滑翔"),
            # 2026-10-03 删除（用户令）：("飞行", "反转", "坠落"),
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
