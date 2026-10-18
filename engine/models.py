"""
核心数据模型
"""
from __future__ import annotations
from .rules_source import lethal_events
from dataclasses import dataclass, field
from typing import Optional, Any, Callable
import math
import json
import uuid

from .enums import EffectScope, EffectPolarity
from .effect_context import EffectContext, make_context, normalize_context
from .resolution import KIND_HEAL, note_delta, resolution_frame
from .combat_events import (CombatEvent, CombatEventType, engine_for_state,
                            get_combat_event_observer)
from .personality import export_for_ai as personality_export_for_ai
from .rule_engine import apply_final_life_loss_cap, validate_damage_amount
from .rule_engine import apply_final_life_loss_cap


@dataclass
class DaoWen:
    """道纹定义"""
    name: str                    # 道纹名称（两字）
    formula: str                 # 公式描述
    cost_type: str               # 代价/消耗类型
    cost_formula: str            # 消耗公式
    effect_formula: str          # 效果公式
    trigger_timing: str = ""     # 触发时点
    is_monster_original: bool = False  # 是否原始怪物道纹
    is_monster_transform: bool = False  # 是否怪物转化道纹
    tags: list[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "formula": self.formula,
            "cost_type": self.cost_type,
            "cost_formula": self.cost_formula,
            "effect_formula": self.effect_formula,
            "trigger_timing": self.trigger_timing,
            "is_monster_original": self.is_monster_original,
            "is_monster_transform": self.is_monster_transform,
            "tags": self.tags
        }


@dataclass
class DaoWenInstance:
    """道纹实例 - 角色持有的道纹"""
    dao_wen: DaoWen
    x_value: int = 0            # 当前X值（自由控X规则）
    # 2026-09-16 用户令：面板不再写死 X，改由怪物 AI 在发动时自选，
    # 上限只受[法限]或代价限制。x_free=True 表示面板没写 X、X 待发动时自选；
    # x_free=False 表示面板写了固定 X（兼容旧面板，迁移期两种写法都必须能跑）。
    x_free: bool = False
    cooldown_remaining: int = 0 # 冷却剩余
    is_frozen: bool = False     # 是否被封印
    sha_qi: str = ""            # 乱葬岗附煞：法煞/魂煞/冥煞/血煞/锁煞/心煞
    # 【唯一】代价：规则正文「唯一：使用后，本次轮回中无法再次使用」。
    # 与冷却同族但作用域更大——冷却按战斗场数递减，唯一是轮回级一次性，
    # 跨场不恢复，只有开新轮回（状态重置）才清零。
    spent_unique: bool = False
    # 时间的偏爱：将按战斗场数计的冷却改为逐回合递减。
    cooldown_rounds_remaining: int = 0

    def can_use(self) -> bool:
        return (not self.is_frozen and not self.spent_unique
                and self.cooldown_remaining <= 0
                and getattr(self, "cooldown_rounds_remaining", 0) <= 0)


@dataclass
class Spell:
    """法术定义"""
    name: str
    required_daowen: list[str]   # 所需道纹列表
    trigger_condition: str       # 触发条件（文本，保留以兼容旧存档/旧 API）
    effect_flow: str             # 生效流程
    rank: int = 1                # 阶级 = 所需道纹种数
    custom_conditions: list[str] = field(default_factory=list)
    automatic: bool = False      # 是否由引擎在触发时点自动提交，不占主动出手
    # Phase 2 新增：lifecycle（instant / battle / permanent）。
    # 旧存档/旧 define_spell 调用未传此字段时默认 permanent，避免旧法术意外消失
    # （Phase 5 会把 define_spell 默认改为 battle；本阶段保持 backward compat）。
    lifecycle: str = "permanent"
    # trigger 字段：spell_dsl 解析出的规范 trigger 名（"瞬发"/"受到伤害前"/...）。
    # 旧对象未传时由 trigger_condition 懒解析得到；不序列化强制要求，
    # 保持与旧存档 pickled Spell 兼容。
    trigger: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "required_daowen": self.required_daowen,
            "trigger_condition": self.trigger_condition,
            "effect_flow": self.effect_flow,
            "rank": self.rank,
            "custom_conditions": self.custom_conditions,
            "automatic": self.automatic,
            "lifecycle": self.lifecycle,
            "trigger": self.trigger,
        }


@dataclass
class Relic:
    """遗物"""
    name: str
    effect: str
    tags: list[str] = field(default_factory=list)

    def __post_init__(self):
        # 实体、本局遗物与死斗对手的旧档都会重新构造 Relic，描述统一随规则升级。
        if self.name == MONSTER_MANA_RELIC:
            self.effect = MONSTER_MANA_RELIC_EFFECT

    def to_dict(self) -> dict:
        return {"name": self.name, "effect": self.effect, "tags": self.tags}


# 每只怪物出厂自带的遗物：[回始]获得法限等量的法力和异变。
# 定义在此而非 monsters.py：授予发生在 Entity.__post_init__，效果判定在 combat.py，
# 两侧都要引用，放 models 可避免 monsters ↔ combat 的循环导入。
MONSTER_MANA_RELIC = "痛苦的偏爱"
MONSTER_MANA_RELIC_EFFECT = "[回始]获得等同[法限]的法力和【异变】"


@dataclass
class Consumable:
    """消耗品"""
    name: str
    effect: str
    current_uses: int = 1
    max_uses: int = 1
    # kind: normal(普通) / sculpture(雕塑，1耐久=15伤害或20格挡)
    #       / dragon_heart(龙心谷"炼心"产出，耐久=可抵消的同类型代价点数)
    kind: str = "normal"
    # dragon_heart 专用：可抵消的代价类型（流血/衰老/疲惫）
    dragon_heart_type: str = ""

    @property
    def is_depleted(self) -> bool:
        return self.current_uses <= 0

    def use(self) -> int:
        if self.is_depleted:
            return 0
        self.current_uses -= 1
        return self.current_uses

    def merge(self, other: 'Consumable') -> bool:
        """合并相同消耗品（雕塑等绑定特定怪物的不可合并）"""
        if self.name != other.name or self.effect != other.effect:
            return False
        if self.kind != "normal" or other.kind != "normal":
            return False
        self.current_uses += other.current_uses
        self.max_uses += other.max_uses
        return True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "effect": self.effect,
            "current_uses": self.current_uses,
            "max_uses": self.max_uses,
            "is_depleted": self.is_depleted,
            "kind": self.kind,
            "dragon_heart_type": self.dragon_heart_type,
        }


@dataclass
class StatusEffect:
    """持续效果及其来源道纹的序列位置。

    道纹排序只看 ``daowen_order``（越小越先）；X 值和状态施加/刷新时间
    不参与排序。旧字段 activation_x/application_sequence 暂保留用于旧调用兼容，
    但不再是结算顺序依据。没有道纹位置的普通状态保持原有列表顺序。
    """
    name: str
    remaining_rounds: int        # 剩余回合（-1=∞；仍只代表本场战斗内的无限）
    value: int = 0               # 效果数值
    source: str = ""             # 来源
    scope: str = EffectScope.BATTLE.value
    polarity: str = EffectPolarity.NEUTRAL.value
    activation_x: Optional[int] = None  # 旧版兼容字段，不参与排序
    application_sequence: int = 0  # 旧版兼容字段，不生成新排序序号
    daowen_order: Optional[int] = None  # 发动该状态的道纹在持有者序列中的位置，越小越先
    
    def __post_init__(self):
        # 即使调用方直接append而不经过Entity.add_status，代价标记也不能被战终误清。
        if (self.name in {"流血", "衰老", "枯竭", "萎缩", "疲惫", "异变", "迷失"}
                and self.scope == EffectScope.BATTLE.value):
            self.scope = EffectScope.COST.value

    @property
    def ordering_key(self) -> tuple[int, int]:
        """道纹位置优先；未知位置的状态稳定地排在已知道纹之后。"""
        if self.daowen_order is None:
            return (1, 0)
        return (0, int(self.daowen_order))

    def refresh_order_from(self, other: 'StatusEffect') -> None:
        """刷新状态来源与道纹序列位置；施加时间和 X 不影响结算顺序。"""
        self.daowen_order = other.daowen_order
        self.source = other.source

    @property
    def is_permanent(self) -> bool:
        return self.remaining_rounds == -1
    
    @property
    def is_expired(self) -> bool:
        return not self.is_permanent and self.remaining_rounds <= 0
    
    def tick(self) -> bool:
        """回合递减，返回是否仍有效"""
        if self.is_permanent:
            return True
        self.remaining_rounds -= 1
        return self.remaining_rounds > 0
    
    def merge_with(self, other: 'StatusEffect') -> bool:
        """合并同名效果，并按最后一次施加刷新来源及统一排序锚点。"""
        if self.name != other.name:
            return False
        if self.is_permanent or other.is_permanent:
            self.remaining_rounds = -1
        else:
            self.remaining_rounds += other.remaining_rounds
        self.value += other.value
        # 同名持续状态重施/合并时，来源道纹位置随最新来源更新；X 和施加时间不参与排序。
        self.refresh_order_from(other)
        return True


@dataclass
class LongJiXin:
    """龙心（消耗品）"""
    cost_type: str               # 代价类型（流血/衰老/枯竭/萎缩）
    current_durability: int
    max_durability: int
    
    def consume(self, amount: int) -> int:
        """消耗耐久，返回实际抵消的代价"""
        actual = min(amount, self.current_durability)
        self.current_durability -= actual
        return actual
    
    @property
    def is_depleted(self) -> bool:
        return self.current_durability <= 0
    
    def to_dict(self) -> dict:
        return {
            "cost_type": self.cost_type,
            "current_durability": self.current_durability,
            "max_durability": self.max_durability
        }


@dataclass
class Entity:
    """游戏实体（轮回者/怪物/朋友/员工通用基础）"""
    name: str
    entity_type: str
    runtime_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # 分裂复制体的创建者身份；供遗物被动追踪归属，不依赖可能重名的实体名称。
    copy_creator_runtime_id: str = ""
    # 开放世界位置字段：所有角色共用；地图系统尚未分配位置时为None。
    world_region: str = ""
    world_x: Optional[int] = None
    world_y: Optional[int] = None
    is_neutral: bool = False  # 中立特殊角色不会自动进入任一战斗阵营
    # 怪物/造物共用的固有偏爱；每种偏爱在同一实体上最多出现一次。
    preferences: list[str] = field(default_factory=list)

    # 基础属性
    blood_limit: int = 0         # 血限
    current_hp: int = 0          # 当前生命
    mana_limit: int = 0          # 法限
    current_mana: int = 0        # 当前法力
    speed_limit: int = 0         # 速限
    current_speed: int = 0       # 当前速度
    
    # 攻击
    attack_count: int = 0        # 攻击次数
    attack_power: int = 0        # 攻击力
    
    # 道纹与法术
    dao_wen: dict[str, DaoWenInstance] = field(default_factory=dict)
    spells: list[Spell] = field(default_factory=list)
    # 旧存档兼容字段：现行法术系统不读取该字段，法术统一存入 spells。
    armed_spells: list[str] = field(default_factory=list)
    # 残韵库存：每个轮回者实体独立持有（{转换: n, 反转: n, 曲解: n}）。
    # 早期版本残韵只挂在 State（仅玩家侧），导致守擂者同为轮回者却无残韵可用。
    # 现下放为实体级，使挑战者/守擂者共用同一套残韵机制（玩家侧仍经 State.resonance 兼容）。
    resonance: dict[str, int] = field(default_factory=dict)
    relics: list[Relic] = field(default_factory=list)  # 由正文明确授予该角色的随身物品（如防弹插板）
    # 统一 AI 的当前轮回记忆：只属于活着的实体；命零前压缩成遗言后清空。
    # 这是主观叙事，不是规则事实，不能改变引擎数值或合法性。
    ai_memory: dict = field(default_factory=dict)
    
    # 状态
    shield: int = 0              # 格挡
    status_effects: list[StatusEffect] = field(default_factory=list)
    is_flying: bool = False      # 飞行状态

    # 非击杀永久离场标记（雕塑/癌变/还债/逃跑等；【封印】暂离不使用此标记）
    removed_without_kill: bool = False
    # 统一【离场】标记：永久使角色脱离本场战斗的特殊事件
    # （雕塑/癌变/还债/救赎/逃跑及未来新增）一律经 depart_battle() 置位。
    # 【封印】是暂离回场，不使用此标记；战斗胜利另由暂离队列门禁处理。
    is_departed: bool = False
    departure_reason: str = ""   # 离场原因（雕塑/癌变/还债/救赎/逃跑/...）
    hp_lost_this_round: int = 0   # 本回合累计失去的生命（回始归零）
    actions_used_this_round: int = 0  # 本回合已消耗的出手次数（回始归零，用于出手预算校验）
    # ---- 蓄锐：消耗1出手换取下回合+1出手 ----
    # 用 Entity.field 方式声明：
    _xurui_pending: int = field(default=0)
    # 新规则：本回合你受到的攻击伤害 - 你10%当前生命（向下取整，最低0）；
    # 每回合可使用次数 = 你当前生命（每次受击消耗1次，用完后本回合后续攻击不再减伤）。
    # 不再有"上回合招架过→本回合禁用"的锁定。
    parrying_this_round: bool = False   # 本回合是否已声明招架姿态
    parry_uses_remaining_this_round: int = 0  # 本回合剩余可招架次数（声明时设为 current_hp）
    # 旧 parry_locked_this_round 字段 2026-09-28 废弃，保留以兼容旧存档反序列化（不再读取/写入）。
    parry_locked_this_round: bool = False
    # ---- 承露盏（遗物）：每累计失去10生命获得1法力，每场战斗累计 ----
    hp_lost_this_battle: int = 0   # 本场累计失去的生命（战始归零）
    chenglu_paid: int = 0       # 本场已按此兑换过的生命总额（每10一档，战始归零）

    # 多路径胜利追踪
    shards_peak: int = 0          # 本局历史最高真实碎片余额 M，只升不降
    shards: int = 0              # 怪物自带碎片（罪孽都市）/ 负值表示负债（还债）
    fake_shards: int = 0         # 假碎片（罪孽都市：假钞产出；战斗中失去碎片时优先失去假碎片）
    total_healed: int = 0        # 累计受到的恢复量（癌变；含过量部分，按原值计，双倍机制已删）
    is_sculptured: bool = False  # 已化为雕塑（攻击次数和攻击力同时归0）
    is_proliferated: bool = False  # 已被癌变吸收进死者之书（旧名 增生，已统一为 癌变；保留字段名兼容）
    is_debt_bound: bool = False  # 已因还债成为员工

    # ---- 罪孽都市专属道纹的回始记账（F2 全量） ----
    # 清算：目标侧挂账 [{x, caster}]，[回始]逐条结算，状态消失即清账
    # （2026-10-13 用户令删【逼债】：_bizhai 字段与【逼债·结算/对账】机制一并移除）
    _qingsuan: list = field(default_factory=list)
    # 抵扣（旧）：被封印的遗物 {遗物名: 剩余回合}，[回终]-1，归零解封
    sealed_relics: dict = field(default_factory=dict)
    # 豪夺：从他人处夺取的遗物临时持有
    # 结构：{遗物名: {"remaining": int, "from_side": "player"|"enemy", "from_entity_name": str}}
    # 夺取期间归夺取方（caster）持有并触发被动；[回终] -1，归零归还原持有者。
    stolen_relics: dict = field(default_factory=dict)

    def __setattr__(self, name, value):
        if name == "shards_peak" and "shards_peak" in self.__dict__:
            value = max(int(value), int(self.__dict__.get("shards_peak", 0)))
        object.__setattr__(self, name, value)
        if name == "shards" and "shards_peak" in self.__dict__:
            if int(value) > int(self.__dict__.get("shards_peak", 0)):
                object.__setattr__(self, "shards_peak", int(value))

    def lose_shards(self, amount: int) -> int:
        """失去碎片（假碎片优先，余额不足则不足额损失）。返回实际失去的真碎片数。"""
        amount = max(0, amount)
        use_fake = min(self.fake_shards, amount)
        self.fake_shards -= use_fake
        real = amount - use_fake
        self.shards -= real
        return real

    def depart_battle(self, reason: str):
        """统一【离场】入口：任何使角色脱离本场战斗的特殊事件都必须调用此方法。

        离场不是击杀：is_alive 置 False 仅表示脱离战场，不产生[碎片]奖励；
        奖励与分类逻辑可读取 departure_reason。新增特殊事件只需调用本方法，
        无须改动任何战斗胜利判定。
        """
        self.is_departed = True
        self.departure_reason = reason
        self.is_alive = False
        self.removed_without_kill = True  # 兼容旧字段：离场一律不视为击杀

    # 兼容：is_proliferated 旧名（增生）→ 现名 癌变，is_cancer 为别名
    @property
    def is_cancer(self) -> bool:
        return self.is_proliferated

    @is_cancer.setter
    def is_cancer(self, value: bool):
        self.is_proliferated = value

    # 异变计数：非怪物角色达到迷失阈值时交由战斗层处理；怪物照常累计但不触发迷失。
    mutation_count: int = 0

    # 存活
    is_alive: bool = True

    # 出战支援（罪孽都市专属机制1，全局对所有[员工]生效）：
    # [员工]默认待命不占场；is_deployed=True 才计入战场与工资结算。
    # 玩家/怪物/[朋友]/[临时朋友]与"还债"转化来的[员工]默认直接为True，保持既有行为不变。
    is_deployed: bool = True
    deployed_at_round: int = 0  # 派遣时 state.current_round 的原始值（用于结算"实际出场回合数"）

    # 2026-09-17 用户令：[员工]**出场**（is_deployed 参战）并**存活**满
    # EMPLOYEE_PROMOTION_BATTLES 场战斗后转为[朋友]。
    # 只统计"本场实际参战且[战终]仍存活"的场次：待命未上场不计，阵亡清零不计。
    survived_battles_as_employee: int = 0

    # 撤退（任意[朋友]/[员工]即将受到足以使当前命零的伤害时自动触发）：
    # 保留当前生命，不再计入本场战斗(get_all_player_side排除)，无法再次加入本场战斗；
    # 但未死亡，[战终]后随存活[朋友]/[员工]一同留存，下一场重置为False可正常参战。
    has_retreated: bool = False

    # 血誓戒：本回合是否已经触发过"首次主动支付流血代价"奖励（回始归零）
    blood_oath_used_this_round: bool = False
    # [战始]时的血限快照，用于击杀碎片按战始血限结算（增殖等战斗中改变血限不影响此值）
    battle_start_blood_limit: int = 0

    # 寒冰法力（初拥之夜遗物）：本回合内，持有该遗物者对我方发动道纹累计消耗的法力（含对自己发动）
    # 回始归零；每满10点使当前回合出手次数-1（以叠加"无力"状态实现）
    mana_inflicted_this_round: int = 0

    # 血族血脉（初拥之夜遗物）：本回合是否已造成过伤害（[回终]判定：造成过则回复等量，否则流血20）
    damage_dealt_this_round: int = 0
    # 特殊事件【凡庸】：连续五回合未出手 / 五回合未能使敌对角色生命减少时触发。
    # 两个条件是"或"关系，故分别计数。多个角色同时达阈值时，非轮回者优先结算。
    no_action_rounds: int = 0   # 连续未出手回合数
    no_damage_rounds: int = 0   # 连续未使敌方生命减少的回合数
    # 本场战斗内累计获得的[回复]量。规则正文：[战终]清除局内增益(包括回复)，
    # 故战终须把这部分回血扣除（不低于进场时的生命）。
    healed_this_battle: int = 0
    # 本场[战始]时的当前生命，作为回复清除后的生命下限
    battle_start_hp: int = 0
    # 赤族诅咒标记：entity_type=="赤族"的实体[回终]固定流血20；is_chizu_of记录其主人名字(仅用于血食校验)
    is_chizu_of: str = ""

    def __post_init__(self):
        if self.battle_start_blood_limit == 0:
            self.battle_start_blood_limit = self.blood_limit
        # ---- 2026-09-16 属性模型统一的向上兼容 ----
        # 统一后 [攻击次数]=[当前速度]、[攻击力]=[当前法力] 对全体生效，但历史代码里
        # 怪物/[朋友]/[员工] 只带 attack_count/attack_power 两个面板值、没有上限字段，
        # 换算后会被读成 0×0。故：上限缺省时由旧面板值推定，并在战始把当前值给满。
        # 轮回者不在此列——它的当前法力/速度是战斗中的消耗状态，允许为 0，
        # 在这里回填等于凭空回蓝，会直接抹掉"蓝即拳"的全部张力。
        if self.mana_limit == 0 and self.attack_power > 0:
            self.mana_limit = self.attack_power
        if self.speed_limit == 0 and self.attack_count > 0:
            self.speed_limit = self.attack_count
        # 每个怪物出厂自带【痛苦的偏爱】，覆盖工厂、手造与读档全部构造路径。
        # 2026-10-13：回始获得法限等量法力与异变；是否触发迷失由角色类型决定，
        # 不由遗物决定。持有者不限于怪物，失去遗物的怪物仍免于迷失。
        if self.entity_type == "怪物" and not any(
                r.name == MONSTER_MANA_RELIC for r in self.relics):
            self.relics.append(
                Relic(name=MONSTER_MANA_RELIC, effect=MONSTER_MANA_RELIC_EFFECT,
                      tags=["怪物自带"]))
        if self.entity_type != "轮回者":
            if self.current_mana == 0 and self.mana_limit > 0:
                self.current_mana = self.mana_limit
            if self.current_speed == 0 and self.speed_limit > 0:
                self.current_speed = self.speed_limit
        # 构造期结束标志：__init__ 里 attack_count/attack_power 的赋值会先于本方法触发
        # 下面的写穿垫片，若垫片在构造期就生效，会和这里的"由旧面板推定上限"重复计数
        # （例如 speed_limit=2 + attack_count=1 会被顶成 3）。故构造期只由本方法负责，
        # 之后的运行时写入才交给垫片走增量。
        object.__setattr__(self, "_panel_sync_ready", True)

    # ---- 「失去生命后」统一拦截 (2026-08-30) ---- 
    # 用户要求：不要再逐个效果开窗调 _fire_after_life_lost，只要当前生命
    # 下降就统一触发。因此 current_hp 的每次写入都经 __setattr__ 捕获：
    # 若数值变小，则把 (old, new) 报给所属战斗引擎（若有绑定）。
    # 引擎侧以 _hp_loss_recording 计数器表示“这段降血正由既有入口
    # （_record_hp_loss_event / _apply_blood_limit_change 等）接管”，
    # 期间本钩子被抑制，避免双发；未被接管、或未来全新效果的直接
    # current_hp-= 写法则由本钩子兜底自动触发，无需再手工接线。
    # _hp_engine_ref 为可选弱引用（非字段，不参与序列化/深拷贝判定）。
    def __setattr__(self, name, value):
        old = self.__dict__.get(name, None)
        object.__setattr__(self, name, value)
        if name == "current_hp" and old is not None and value < old:
            self._fire_hp_loss(old, value)
        # ---- 2026-09-16 属性模型统一：旧面板字段写穿到新的上限/当前值 ----
        # 统一后 [攻次]=[当前速度]、[攻力]=[当前法力]，但历史代码（含【变形】互换）
        # 仍直接写 attack_count/attack_power。非轮回者在此同步写穿，否则换算读到 0×0。
        # 轮回者**不写穿**：它的 attack_count/attack_power 是建号遗留字段、换算根本不读
        # （如「龙族利爪」曾写死 3/1，若写穿会把玩家真实的法限/速限直接冲掉）。
        if name in ("attack_power", "attack_count") and self.__dict__.get("_panel_sync_ready"):
            etype = self.__dict__.get("entity_type")
            if etype is not None and etype != "轮回者":
                # **增量**写穿（而非把上限设成绝对值）：attack_power/attack_count 与
                # 法限/速限在战始是相等的，但战斗中两者会被不同来源各自位移
                # （血契分摊、疲惫代价、弱化…），用绝对值会抹掉那些真实消耗。
                # 上限同步位移以维持「当前值永远落在 0~上限 闭区间」这条封顶规则。
                d = value - (old if isinstance(old, int) else 0)
                if name == "attack_power":
                    self.mana_limit = max(0, self.mana_limit + d)
                    self.current_mana = max(0, min(self.mana_limit, self.current_mana + d))
                else:
                    self.speed_limit = max(0, self.speed_limit + d)
                    self.current_speed = max(0, min(self.speed_limit, self.current_speed + d))

    def _fire_hp_loss(self, old: int, new: int) -> None:
        eng = getattr(self, "_hp_engine_ref", None)
        if eng is None:
            return  # 未绑定引擎（模型层/局外/深拷贝快照）→ 纯数据，不触发战斗反应
        owner = eng()
        if owner is None:
            return
        # _resolving_life_lost_reactions>0 表示正结算反应自身 → 不再连锁。
        if getattr(owner, "_resolving_life_lost_reactions", 0) > 0:
            return
        owner._on_entity_hp_fallen(self, old, new)

    def __getstate__(self):
        """存档/快照序列化时剔除战斗期引擎弱引用（不可 pickle）。"""
        state = self.__dict__.copy()
        state.pop("_hp_engine_ref", None)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.__dict__["_hp_engine_ref"] = None

    
    def effective_attack_count(self) -> int:
        """攻击次数＝ 当前速度。

        此前口径把换算限定在轮回者身上，理由是怪物不持有法力；怪物与微光者现已与轮回者
        同口径持有[速限]/[法限]，该理由不再成立，故去掉类型分支。
        普攻不会支付速度；只有当前速度已经因闪避等明确机制变化时，后续派生攻击次数才会随面板变化。

        【全速】覆盖：生效期间攻击次数锁定 = [速限]。
        由于 clamp_immortal_body 已让「当前速度≤[速限]」无条件成立，本效果实为
        增益——把被削的速度补满到上限，并免疫后续减速。走状态层，对全体角色生效。
        """
        if self.get_status_value("全速"):
            return max(0, self.speed_limit)
        return max(0, self.current_speed)

    def effective_attack_power(self) -> int:
        """攻击力＝ 当前法力。

        实时读取当前法力，不做快照：法力掉到 0 时每击 0 点，法力被补回来
        （守夜灯/承露盏/血契/透支/再生等任何回蓝、怪物的[回始]回满）后攻击力**同步回升**，
        并非单调下降。花蓝前要先算清这一笔对后续每击的连带影响。

        两处覆盖（均走状态层，故对轮回者/怪物/朋友/员工同口径生效）：
        - 【龙族利爪】：攻击力 = 当前法力×2。
        - 【全力】：攻击力锁定 = [法限]，覆盖上一步的结果，
          不再随当前法力下降。顺序上"全力"最后生效，故它压过龙族利爪的倍率。
        """
        power = self.current_mana
        if self.get_status_value("龙族利爪"):
            power *= 2
        if self.get_status_value("全力"):
            # 锁定为法限：花法力不再掉攻击力
            power = self.mana_limit
        return max(0, power)

    @property
    def load_capacity(self) -> int:
        """可承载的道纹/遗物**种类**上限 = [血限] // 30（2026-10-12 用户令）。

        原文口径：**30X[血限] = 允许拥有 X 种道纹和 X 种遗物**。
        两个额度各自独立计数、同为 X，不互相挤占；不足 30[血限] 的余数不折算。
        血限被【衰老】等压低时额度同步收紧——已超出新额度的部分不会被自动没收
        （正文没有写剥夺条款），但**再获得**会被 `GameEngine` 的获得入口拒绝。
        道纹按"种"计（`dao_wen` 的键数），遗物也按"种"计（同名不重复计入）。
        """
        return max(0, self.blood_limit // 30)

    @property
    def daowen_capacity(self) -> int:
        """道纹种类上限（= `load_capacity`，单列出来是为了在报错文案里说清楚）。"""
        return self.load_capacity

    @property
    def relic_capacity(self) -> int:
        """遗物种类上限（= `load_capacity`）。"""
        return self.load_capacity

    @property
    def action_count(self) -> int:
        """出手次数：**全体角色基础 2 次**。

        不再由速限/攻击次数推导——速限已改作攻击次数的来源，再拿它算出手会重复记账；
        微光者旧的「攻击次数/3」口径同步废止（该式会让高攻次微光者白拿第3、4次出手）。
        在基础值之上叠加：遗物、【疯狂】+X、【无力】-X、【蓄锐·增】+1（持续1回合）。
        LLM 侧按「每个动作槽位一条决策」描述，不得写死每回合固定 2 次。
        怪物行动仍由CombatEngine的prepare/resolve两阶段接口独立计算。"""
        base = 2
        base += self.get_status_value("疯狂")
        base -= self.get_status_value("无力")
        # 蓄锐·增：每层+1出手
        base += self.get_status_value("蓄锐·增")
        return max(0, base)

    @property
    def hp_ratio(self) -> float:
        return self.current_hp / self.blood_limit if self.blood_limit > 0 else 0
    
    def take_damage(self, amount: int, damage_type: str = "普通", *,
                    life_loss_multiplier: int = 1,
                    status_adjuster: Optional[Callable[[int], int]] = None) -> dict:
        """
        受到伤害，返回结算详情。

        格挡先在伤害资源层吸收；随后（若由战斗引擎传入）在同一伤害窗口调用
        ``status_adjuster``，以统一的「发动 X 降序、同 X 施加先后」处理所有
        持续状态。adjuster 只要收到一笔仍有效的伤害，就必须自行允许 0 被后续
        状态继续修正。代价不经该持续状态调整区。

        ``life_loss_multiplier``：【第一杯】的「失去的生命翻倍」发生在格挡及
        持续状态调整之后、扣血之前。直接调用 Entity 的旧入口未传 adjuster 时，
        保留【固执】压帽兼容行为；完整战斗管线由 CombatHookManager 接管。
        """
        amount = validate_damage_amount(amount, "Entity.take_damage.amount")
        life_loss_multiplier = validate_damage_amount(
            life_loss_multiplier, "Entity.take_damage.life_loss_multiplier",
            allow_negative=False)
        if life_loss_multiplier < 1:
            raise ValueError("Entity.take_damage.life_loss_multiplier 必须至少为 1")
        if not isinstance(damage_type, str) or not damage_type:
            raise ValueError("Entity.take_damage.damage_type 必须是非空字符串")
        detail = {
            "raw_damage": amount,
            "shield_absorbed": 0,
            "actual_damage": 0,
            "actual_life_loss": 0,
            "hp_before": self.current_hp,
            "hp_after": self.current_hp,
            "blood_limit_before": self.blood_limit,
            "died": False,
            "damage_type": damage_type
        }
        
        if amount <= 0:
            return detail
        
        remaining = amount
        shield_before = self.shield
        try:
            # 格挡抵消（代价类型的伤害不被格挡抵消；"无视格挡"为【贯穿】等效果，同样跳过格挡）
            if self.shield > 0 and damage_type not in ("代价", "无视格挡"):
                absorbed = min(self.shield, remaining)
                self.shield -= absorbed
                remaining -= absorbed
                detail["shield_absorbed"] = absorbed

            # 完整战斗管线：格挡后的所有持续状态由同一排序器结算。这里不以
            # remaining==0 作为状态区内部的短路条件——回调一旦开始，其后的效果
            # 仍可把 0 改回正数；但护盾已完全吸收的事件不进入持续状态区。
            if remaining > 0 and damage_type != "代价" and status_adjuster is not None:
                remaining = max(0, validate_damage_amount(
                    status_adjuster(remaining), "Entity.take_damage.status_adjuster.result"))
            # 兼容直接 Entity.take_damage 的历史调用（没有战斗引擎上下文时）。
            # 2026-10-11 用户令：固执简化为「每次失去的生命不会超过1」，代价路径同样封顶。
            elif remaining > 0 and self.has_status("固执"):
                remaining = min(remaining, 1)
                detail["capped_by"] = "固执"

            # 【第一杯】：失去的生命翻倍（格挡/持续状态调整之后、扣血之前）
            if life_loss_multiplier > 1 and remaining > 0:
                remaining = validate_damage_amount(
                    remaining * life_loss_multiplier,
                    "Entity.take_damage.after_life_loss_multiplier")
                detail["life_loss_multiplier"] = life_loss_multiplier
            detail["damage_after_life_loss_multiplier"] = remaining

            # 【固执】是最终失血边界，位于所有倍率之后。
            before_final_cap = remaining
            remaining = apply_final_life_loss_cap(remaining, self, damage_type)
            if remaining < before_final_cap:
                detail["capped_by"] = "固执"
                detail["guzhi_final_cap"] = True
        except Exception:
            # 状态执行/数值校验失败时，尚未扣血；把此前吸收的护盾一并回滚，
            # 不留下半次结算已消耗防御资源的状态。
            self.shield = shield_before
            raise

        # 扣除生命；actual_damage 是经过倍率的待扣数值，actual_life_loss 是
        # HP 实际下降量，过量伤害时二者可不同。
        hp_before = self.current_hp
        self.current_hp = max(0, self.current_hp - remaining)
        actual_life_loss = max(0, hp_before - self.current_hp)
        detail["actual_damage"] = remaining
        detail["actual_life_loss"] = actual_life_loss
        detail["hp_after"] = self.current_hp
        self.hp_lost_this_round += remaining  # 保持既有追踪字段的待扣数口径

        if self.current_hp <= 0:
            # 模型层只翻标记，不知道战斗上下文。命零的“通知 + 死后效果”必须由调用方
            # 交给 CombatEngine._check_hp_zero_death()（唯一统一死亡入口）。
            detail["died"] = True
            self.is_alive = False
            detail["xuanyun_broken"] = True
        
        return detail
    
    # 2026-10-08 起：这三个阈值的**事实源在 data/rules/lethal_events.toml**，
    # 经 engine/rules_source.py 在 import 时载入。保留成类属性是为了让全仓库
    # 那几十处 `Entity.MUTATION_COLLAPSE_THRESHOLD` 的写法一个都不用改——
    # 改规则现在只动 toml，这里不再是数字的家。
    MUTATION_COLLAPSE_THRESHOLD = lethal_events.threshold("mishi")  # 【迷失】：异变达到 50 层触发
    # 2026-09-17 用户令：[员工]出场并存活满这么多场战斗即转为[朋友]（唯一事实源）。
    EMPLOYEE_PROMOTION_BATTLES = 3
    CANCER_HEAL_MULTIPLIER = lethal_events.threshold_multiplier("aibian")  # 【癌变】：本场累计回复量 ≥ 血限×该系数 即命零
    MEDIOCRITY_ROUNDS = lethal_events.threshold("fanyong")  # 【凡庸】：连续 N 回合未出手、或连续 N 回合未使敌对角色掉血 即命零

    def can_trigger_mutation_lost(self) -> bool:
        """是否受【迷失】阈值约束（类型豁免来自结构化事实源）。"""
        return lethal_events.applies_to("mishi", self.entity_type)

    def lethal_counters(self) -> dict:
        """致死类特殊事件的进度：{名称: (当前值, 阈值)}。

        非怪物异变达阈值：有队友→变怪物开战；无队友→命零。
        2026-10-13：怪物不触发迷失，因此不输出虚假的迷失致死进度。
        进度必须能被 AI 在面板上直接读到，不允许只留一个布尔结果。口径：
          * 迷失 = 异变层数 / MUTATION_COLLAPSE_THRESHOLD；
          * 癌变 = 本场累计回复量 / ⌈血限×CANCER_HEAL_MULTIPLIER⌉；
          * 凡庸 = 连续未出手（或连续未致敌掉血）回合数 / MEDIOCRITY_ROUNDS，取更接近线的那条。
        """
        out: dict[str, tuple[int, int]] = {}
        if self.can_trigger_mutation_lost():
            out["迷失"] = (int(self.mutation_count), int(self.MUTATION_COLLAPSE_THRESHOLD))
        if self.blood_limit > 0:
            out["癌变"] = (int(self.total_healed),
                           int(math.ceil(self.blood_limit * self.CANCER_HEAL_MULTIPLIER)))
        if self.no_action_rounds or self.no_damage_rounds:
            # 显示键来自 data/rules/lethal_events.toml 的 progress_keys（[0]=未出手，[1]=未致敌掉血）
            keys = lethal_events.progress_keys("fanyong") or ["凡庸·未出手", "凡庸·未致敌掉血"]
            if self.no_action_rounds >= self.no_damage_rounds:
                out[keys[0]] = (int(self.no_action_rounds), int(self.MEDIOCRITY_ROUNDS))
            else:
                out[keys[1]] = (int(self.no_damage_rounds), int(self.MEDIOCRITY_ROUNDS))
        return out

    def lethal_progress(self) -> list[str]:
        """致死进度的显示串，例如 ['迷失（10/50）', '癌变（30/84）']。

        格式串的事实源在 data/rules/lethal_events.toml 的 progress_format，
        不再在这里写死「（{}/{})」的括号样式。
        """
        out = []
        for name, (current, limit) in self.lethal_counters().items():
            fmt = lethal_events.by_name(name)["progress_format"]
            out.append(fmt.format(name=name, current=current, limit=limit))
        return out

    def add_mutation(self, layers: int) -> dict:
        """增减异变层数。正值累加，负值削减，可降到负数。

        特殊事件【迷失】：
          * 怪物：照常增减异变层数，但无论累积多少层都不会触发【迷失】。
          * 非怪物角色（轮回者/朋友/员工/临时朋友/赤族等）：达到阈值时不在模型层直接命零，
            返回 `lost=True` 交由战斗层 _resolve_mutation_lost 判定——
              - 己方还有其他存活角色（队友/朋友/员工/临时朋友/其他轮回者阵营角色）：
                该角色异变为怪物，立刻与原队伍开战；
              - 否则：直接[命零]死亡。
        模型层只负责数值与阈值，"变谁的敌/是否命零"由战斗引擎处理。
        正在结算的效果是否中断由调用方判定（与中断规则同精神：代价先付）。
        """
        if not isinstance(layers, int) or isinstance(layers, bool):
            raise ValueError("异变层数必须是整数")
        if layers != 0:
            self.mutation_count += layers
        threshold_hit = self.mutation_count >= self.MUTATION_COLLAPSE_THRESHOLD
        # 模型层只标记非豁免角色的迷失，变身/命零仍由战斗层处理。
        lost = threshold_hit and self.is_alive and self.can_trigger_mutation_lost()
        return {
            "mutation_added": layers,
            "mutation_total": self.mutation_count,
            "collapsed": False,       # 兼容旧返回形状；怪物不再异变爆体
            "lost": lost,             # 非怪物触发"迷失"，等待战斗层处理
        }
    
    def heal(self, amount: int, allow_overflow: bool = False) -> dict:
        """回复生命。allow_overflow=True 时当前生命可超出[血限]（遗物【猩红果实】）。"""
        before = self.current_hp
        if allow_overflow:
            self.current_hp = self.current_hp + amount
        else:
            self.current_hp = min(self.blood_limit, self.current_hp + amount)
        actual = self.current_hp - before
        overheal = amount - actual
        # 癌变追踪：DM裁定删除过量回复双倍计入机制，
        # 受到的全部回复（含过量部分）一律按原值计入累计恢复量。
        self.total_healed += amount
        self.healed_this_battle += actual
        return {
            "heal_amount": amount,
            "actual_heal": actual,
            "hp_before": before,
            "hp_after": self.current_hp,
            "overheal": overheal
        }
    
    def gain_shield(self, amount: int) -> int:
        """获得格挡。

        格挡**没有**上限：格挡只写
        「可抵消等量伤害」，正文从未规定它不得超过[血限]，血限 36 的角色叠到
        68 盾是合法面板，不是假账。曾误以为这是"永远消耗不完的堆积"而擅自压帽，
        属自造规则，已撤销。负 amount（扣盾）只做下限 0 保护。
        """
        self.shield = max(0, self.shield + amount)
        return self.shield
    
    def clear_shield(self):
        """清空格挡（回终）"""
        self.shield = 0
    
    def mana_cost(self, amount: int) -> int:
        """返回实际法力支出；愤怒状态与贪婪的偏爱均使正数消耗减半、向上取整。"""
        amount = max(0, int(amount))
        if amount > 0 and (self.has_status("愤怒") or "贪婪的偏爱" in getattr(self, "preferences", [])):
            amount = math.ceil(amount / 2)
        return amount

    def spend_mana(self, amount: int) -> bool:
        """消耗法力。愤怒与贪婪的偏爱：法力消耗减半（向上取整）。

        版本史（【勾魂】）：
        - 旧版「[回始]失去2X法力，持续∞」——已废止。
        - 2026-08-30 版「持续X回合无法获得法力」：随法力改为一池制
          （不再每[回始]回填）而一度失去作用对象。
        - 2026-09-09 DM裁定：改为**法力消耗翻倍**，在 spend_mana 里乘 2。
        - 2026-10-08 用户令：恢复「无法获得[法力]」原意，翻倍逻辑撤销。
          此时战斗内已有多个法力来源（聚能/储能电池/守夜灯/承露盏/
          透支/法术），效果重新有对象；判定位统一收敛到
          `can_gain_mana()` / `gain_mana()`，不再散落各处。
        """
        amount = self.mana_cost(amount)
        if self.current_mana < amount:
            return False
        self.current_mana -= amount
        return True

    def can_gain_mana(self) -> bool:
        """当前是否允许获得[法力]。

        【勾魂】期间恒为 False——「无法获得[回复]」的姊妹判定：
        只压制**增益**，已持有的法力一分不扣（【勾魂】不减少当前法力）。
        这是「无法获得[法力]」的唯一判定位，所有来源都必须走 `gain_mana()`。
        """
        return not self.has_status("勾魂")

    def gain_mana(self, amount: int) -> int:
        """获得[法力]的统一入口，返回**实际**获得量。

        - 【勾魂】期间任何来源的增益都不生效，返回 0（法力纹丝不动）；
        - 其余情况按 [法限] 封顶，返回封顶后真正落到账上的数量。

        调用方若要 clamp_immortal_body（不朽之躯钳制），请在拿到返回值后
        按既有口径自行调用——那是 CombatEngine 的方法，Entity 层拿不到。
        """
        if amount <= 0:
            return 0
        if not self.can_gain_mana():
            return 0
        before = self.current_mana
        self.current_mana += amount
        limit = getattr(self, "mana_limit", None)
        if isinstance(limit, int) and self.current_mana > limit:
            self.current_mana = limit
        return self.current_mana - before

    def get_status_effects(self, name: str) -> list[StatusEffect]:
        return [s for s in self.status_effects if s.name == name]
    
    def has_status(self, name: str) -> bool:
        return any(s.name == name and not s.is_expired for s in self.status_effects)
    
    def get_status_value(self, name: str) -> int:
        return sum(s.value for s in self.status_effects if s.name == name and not s.is_expired)
    
    def tick_status_effects(self, skip_names: tuple = ()) -> list[str]:
        """回合递减，返回已过期的效果名。skip_names 本拍不减。"""
        expired = []
        remaining = []
        for s in self.status_effects:
            if s.name in skip_names:
                remaining.append(s)
                continue
            if not s.tick():
                expired.append(s.name)
            else:
                remaining.append(s)
        self.status_effects = remaining
        return expired
    
    def ordered_statuses(self, names: Optional[set[str]] = None) -> list[StatusEffect]:
        """按来源道纹的序列位置返回状态；未知位置的旧状态稳定保留原列表顺序。

        调用者只决定自然时机与候选状态集合，不得用 X 值或施加/刷新时间重排。
        """
        statuses = (status for status in self.status_effects
                    if names is None or status.name in names)
        return sorted(statuses, key=lambda status: status.ordering_key)

    def add_status(self, effect: StatusEffect):
        """添加状态效果；未显式给出极性时按规则表标注，生命周期仍由scope独立决定。"""
        if effect.polarity == EffectPolarity.NEUTRAL.value:
            buffs = {
                "固执", "贯穿", "急速", "洞察", "飞行",
                "全力", "疯狂", "必中", "洗劫", "逆鳞", "背负",   # 2026-10-11 删【嫁祸】
                "负岳索", "加速", "愤怒", "蓄锐·增",
            }
            debuffs = {
                "弱化", "无力", "减速", "全速", "束缚", "封印",
                "坏死", "退化", "定型", "畸变", "加害", "伤痕",
                "寄生", "蒙蔽", "手雷减攻", "被背负",   # 2026-10-11 删【衰败】
            }
            if effect.name in buffs:
                effect.polarity = EffectPolarity.BUFF.value
            elif effect.name in debuffs:
                effect.polarity = EffectPolarity.DEBUFF.value
        for existing in self.status_effects:
            if existing.merge_with(effect):
                return
        self.status_effects.append(effect)
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "entity_type": self.entity_type,
            "runtime_id": self.runtime_id,
            "world_region": self.world_region,
            "world_x": self.world_x,
            "world_y": self.world_y,
            "is_neutral": self.is_neutral,
            "preferences": list(getattr(self, "preferences", [])),
            "blood_limit": self.blood_limit,
            "current_hp": self.current_hp,
            "mana_limit": self.mana_limit,
            "current_mana": self.current_mana,
            "speed_limit": self.speed_limit,
            "current_speed": self.current_speed,
            "attack_count": self.attack_count,
            "attack_power": self.attack_power,
            "action_count": self.action_count,
            "shield": self.shield,
            "is_flying": self.is_flying,
            "is_alive": self.is_alive,
            "removed_without_kill": self.removed_without_kill,
            "is_departed": self.is_departed,
            "departure_reason": self.departure_reason,
            "is_deployed": self.is_deployed,
            "has_retreated": self.has_retreated,
            "battle_start_blood_limit": self.battle_start_blood_limit,
            "deployed_at_round": self.deployed_at_round,
            "shards": self.shards,
            "shards_peak": self.shards_peak,
            "fake_shards": self.fake_shards,
            "total_healed": self.total_healed,
            "mutation_count": self.mutation_count,
            "no_action_rounds": self.no_action_rounds,
            "no_damage_rounds": self.no_damage_rounds,
            # 致死类特殊事件的进度：AI 必须能在面板上直接读到
            # 「迷失（10/50）」这种进度，禁止只给结果不给进度。
            "lethal_counters": {k: list(v) for k, v in self.lethal_counters().items()},
            "lethal_progress": self.lethal_progress(),
            "hp_ratio": round(self.hp_ratio, 2),
            "ai_memory": self.ai_memory,
            "dao_wen": {k: v.dao_wen.name for k, v in self.dao_wen.items()},
            "spells": [s.name for s in self.spells],
            "relics": [r.to_dict() for r in self.relics],
            "status_effects": [
                {"name": s.name, "value": s.value, "rounds": s.remaining_rounds,
                 "source": s.source, "scope": s.scope, "polarity": s.polarity}
                for s in self.status_effects
            ]
        }


@dataclass
class ScopedStatDelta:
    """一笔可逆的字段变化；使用稳定实体引用，存档/事务回滚后仍指向当前状态对象。"""
    entity_id: str
    entity_name: str
    field_name: str
    delta: int
    scope: str
    polarity: str
    source: str

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "entity": self.entity_name,
            "field": self.field_name,
            "delta": self.delta,
            "scope": self.scope,
            "polarity": self.polarity,
            "source": self.source,
        }


@dataclass
class GameState:
    """完整游戏状态"""
    # 游戏元数据
    game_id: str = ""
    phase: str = "setup"
    # 战斗内强制顺序；正式战始会置为 await_round_start。
    combat_subphase: str = "player_actions"
    current_round: int = 0
    current_battle: int = 0     # 第几场战斗
    current_region: str = ""    # 当前副本
    # 过路商人是独立中立角色，不属于轮回者/怪物阵营；库存与世界坐标单独保存。
    merchant: Optional[Entity] = None
    # 开放世界状态采用纯dict快照，避免地图/行动模块与战斗实体模型形成循环依赖。
    world_state: dict = field(default_factory=dict)
    # 开放世界战斗时暂存未参与本次遭遇的地图敌方单位，战终后恢复。
    world_enemy_reserve: list[Entity] = field(default_factory=list)
    world_encounter_entities: list[Entity] = field(default_factory=list)
    world_encounter_id: str = ""
    energy: int = 3             # 局外精力
    
    # 轮回者
    player: Optional[Entity] = None
    
    # 队友
    friends: list[Entity] = field(default_factory=list)
    employees: list[Entity] = field(default_factory=list)
    temp_friends: list[Entity] = field(default_factory=list)
    
    # 敌方
    enemies: list[Entity] = field(default_factory=list)

    # 角色性格特征：{runtime_id: {name, traits:{dimension: entry}}}
    # 实例级数据（键=Entity.runtime_id，同名不同实例互不共享）；
    # 只由 engine/personality.py 读写：行为推断写入、命零时经统一死亡管线删除；
    # 纯 dict 结构，随本类 deepcopy 快照 / pickle 存档自然往返，不写入任何角色模板。
    personality_traits: dict[str, dict] = field(default_factory=dict)
    
    # 资源
    shards_peak: int = 0        # 玩家本局历史最高真实碎片余额 M，只升不降
    shards: int = 20            # 碎片
    fake_shards: int = 0        # 假碎片（罪孽都市假钞X产出；战斗中失去碎片时优先失去假碎片）

    def __setattr__(self, name, value):
        if name == "shards_peak" and "shards_peak" in self.__dict__:
            value = max(int(value), int(self.__dict__.get("shards_peak", 0)))
        object.__setattr__(self, name, value)
        if name == "shards" and "shards_peak" in self.__dict__:
            if int(value) > int(self.__dict__.get("shards_peak", 0)):
                object.__setattr__(self, "shards_peak", int(value))
        # 玩家真实碎片余额存于 GameState；镜像同步峰值，避免状态快照出现两套玩家峰值。
        if name in {"shards", "shards_peak", "player"} and "shards_peak" in self.__dict__:
            player = self.__dict__.get("player")
            if player is not None and hasattr(player, "shards_peak"):
                peak = max(int(self.__dict__.get("shards_peak", 0)),
                           int(getattr(player, "shards_peak", 0)))
                object.__setattr__(self, "shards_peak", peak)
                object.__setattr__(player, "shards_peak", peak)

    def lose_shards(self, amount: int) -> int:
        """玩家失去碎片（假碎片优先）。返回实际失去的真碎片数。"""
        amount = max(0, amount)
        use_fake = min(self.fake_shards, amount)
        self.fake_shards -= use_fake
        real = amount - use_fake
        self.shards -= real
        return real
    
    # 遗物与消耗品
    relics: list[Relic] = field(default_factory=list)
    relics_pool: list[Relic] = field(default_factory=list)  # 遗物池（未获取的）
    relic_pool_initialized: bool = False
    # 【发现】：随机列出3个未持有候选后，必须由角色显式选1件。
    pending_relic_choices: list[str] = field(default_factory=list)
    pending_relic_source: str = ""
    # 开局从杀伐闭环【发现】初始道纹。
    pending_initial_daowen_choices: list[str] = field(default_factory=list)
    pending_initial_daowen_source: str = ""
    # 【典当】：轮回者发动后待领取的发现（每组3选1，choose_diandang_daowen 逐组领取）。
    pending_diandang_discoveries: list[list[str]] = field(default_factory=list)
    # 救赎：怪物融化后等待接纳/无视。
    pending_redemption: dict = field(default_factory=dict)
    # 消耗品【发现】候选（如扭曲都市完成事件后的工具发现）。
    pending_item_choices: list[str] = field(default_factory=list)
    pending_item_source: str = ""
    # 两阶段怪物决策：prepare产生的合法选项与一次性token；resolve后清空。
    pending_monster_phase: dict = field(default_factory=dict)
    # 两阶段攻击：prepare绑定行动者、逐击合法目标与反应选项；resolve后清空。
    pending_attack: dict = field(default_factory=dict)
    # 二档【探索】一次抽取两个未遇事件，首个结算后按顺序激活其余事件。
    pending_event_queue: list[str] = field(default_factory=list)
    # 事件产生的跨行动/跨战斗确定性状态，键均由事件handler登记。
    event_modifiers: dict[str, Any] = field(default_factory=dict)
    # 显式作用域账本：只登记需要按回合/战斗回滚的面板字段变化。
    # 代价、伤害、资源支出与永久成长不进入可逆账本。
    scoped_effect_ledger: list[ScopedStatDelta] = field(default_factory=list)
    # 死斗对手自己的遗物。可选效果由对手决定是否发动，不跟挑战者的 state.relics 混用。
    opponent_relics: list[Relic] = field(default_factory=list)
    opponent_artifacts_owned: list[str] = field(default_factory=list)
    opponent_dragon_body_shield_rounds: int = 0
    opponent_dragon_tail_sacrifice_declared: str = ""
    consumables: list[Consumable] = field(default_factory=list)
    # 本场战斗轮回者出手选中过的目标 runtime_id（自愈X「目标死亡时」用）。
    # 只记成功提交的攻击/道纹/法术的显式目标（闪避/挡掉照样算选中；X=0、非法提交、
    # 朋友/员工代发动、消耗品使用不记）；每场战始清空。
    player_selected_targets: list[str] = field(default_factory=list)
    # 抵扣X封印的玩家遗物 {遗物名: 剩余回合}，[回终]-1，归零解封（封印期间不触发 process_relics）
    sealed_relics: dict = field(default_factory=dict)
    # 豪夺：玩家/敌人互相夺取的遗物临时寄存
    #   player_stolen_from_player：{name: {"remaining":int}}——理论上罕见，留结构占位
    #   实体.stolen_relics 在 Entity 上（玩家侧实际使用 state.stolen_relics 统一管理）
    stolen_relics: dict = field(default_factory=dict)
    
    # 战场公开频道：双方与观战者共享的台词记录。
    # 条目只含 {round, battle, speaker, posture, text}——**禁止**任何真伪字段
    # （"你都标出来了，还猜什么"）。纯 dict 结构，随 deepcopy 快照 / pickle 存档往返。
    # 只有 sim/duel_pvp.py 之类的表现层调用 engine.dialogue.utter() 写入；
    # TacticalAI 一律不读（红线 E：不碰 AI 判断）。
    battle_channel: list[dict] = field(default_factory=list)

    # 残韵
    resonance: dict[str, int] = field(default_factory=dict)  # {转换: 1, 反转: 2, ...}
    
    # 龙心
    dragon_hearts: list[LongJiXin] = field(default_factory=list)
    
    # 法器
    artifacts: list[dict] = field(default_factory=list)
    
    # 死者之书
    # 系统记录与玩家遗言分开保存，避免结构化遗言退化为日志字符串。
    # 遗言的事实源是 死者之书.md；death_book_legacies 只是启动/审核后从文件装回的缓存。
    # 2026-10-05 用户令：癌变不再为【休整】提供永久恢复量加成（原 rest_heal_bonus
    # 字段已删除），吸收癌变怪物的奖励统一为【无限肉块】（见 规则正文·癌变）。
    death_book_wisdom: list[str] = field(default_factory=list)
    death_book_legacies: list[dict[str, str]] = field(default_factory=list)
    death_book_capacity: int = 20  # 遗言每段字数上限
    death_inheritance_queued: bool = False
    pending_death_draft: dict[str, str] = field(default_factory=dict)
    last_death_cause: str = ""
    
    # 封存候选人（最终的冠冕）
    sealed_candidate: Optional[dict] = None
    # 死斗规则：通过死斗的角色进入"进阶封存"，封存按阶级分槽存放。
    # 每个阶级封存槽是一份先来后到的候选队列；该阶级的挑战者依次与队首死斗。
    # 当前正在进行的死斗所属阶级（1=一阶挑战者/胜者死斗，2=二阶…；0=非死斗）。
    sealed_candidates: dict = field(default_factory=dict)  # {阶级: [候选快照, ...]}
    duel_tier: int = 0
    # 当前死斗中被挑战的擂主原始快照（触发时从队列取出后暂存）：
    # 挑战者落败（擂主卫冕成功）时须按 规则正文规则放回队首重新封存。
    duel_defending_snapshot: dict = field(default_factory=dict)

    # 阶级推进与无尽模式
    # unlocked_tier：已解锁的最高副本阶级。开局只能选阶级≤它的副本；通过某阶最终死斗
    # 后解锁下一阶（持久化在 data/progression.json，跨轮回有效——"胜者进入下一阶级副本"
    # 落在轮回者血脉上，而不是落在已被封存的单个角色身上）。
    # endless_mode/endless_cycle：通过五阶死斗后进入无尽模式；cycle 是第几轮无尽，
    # 每轮结束时+1，用于强度递增与精力递减。
    MAX_TIER = 5  # 阶级推进的终点（五阶·启示录）；再往上就是无尽模式

    # 默认值在引擎构造时被进一步处理：跨轮回持久文件不存在时视为当前已实现阶级都已解锁，
    # 以便与"旧版本开局能直接选二阶乱葬岗"兼容；只有从持久文件显式读出 1（新轮回从一阶起）
    # 或通过 _advance_region_unlock 把值压回 1 时，阶级门禁才真的生效。
    # PROGRESSION_ENABLED=False时此值实际不参与门禁，保留是为未来启用做准备。
    unlocked_tier: int = 1
    endless_mode: bool = False
    endless_cycle: int = 0
    
    # 员工相关
    blacklist_level: int = 0     # 黑名单计数（每累计3名员工因拒付工资/解雇/死亡离队+1，≥3触发is_blacklisted）
    is_blacklisted: bool = False

    # 战终工资结算（出战支援）：{员工名: 应付工资}，非空时 battle_end 阻塞，需先逐个 pay_employee_wage 决策
    pending_wage_decisions: dict[str, int] = field(default_factory=dict)

    # 雇佣后"发现并选择一种转化道纹"：{员工名: [3个候选道纹名]}，未选择前该员工暂不持有转化道纹
    pending_daowen_choices: dict[str, list[str]] = field(default_factory=dict)

    # 事件登记的"下一场战斗额外出现的怪物"（如龙心谷"追求者·拿走口粮"），[战始]出怪时读取并额外加入
    forced_monsters_next_battle: list[dict] = field(default_factory=list)

    # 出怪配方的增援队列：
    # [战始]只让 S=随机(1,N) 只首发进场，其余按第 i 波 `T_i=随机(1,5)` 的间隔、
    # 每波 R_i 只（可>1）在对应[回始]进场。元素为怪物定义dict + "arrive_round"，
    # 见 monsters.roll_spawn_plan / make_monster_entity 入参。
    monster_reinforcements: list[dict] = field(default_factory=list)
    # 【封印X】的暂离队列：元素为
    #   {"monster": Entity, "return_round": int, "delay_rounds": int,
    #    "home": str, "index": Optional[int]}
    # 暂离不是死亡/永久离场，仍阻塞战终；到达回合始时把原实体放回它原本所在的容器。
    # 2026-10-08 用户令：【封印】放开到任意目标（含[朋友]/[员工]/临时朋友与施法者
    # 自己），因此队列不再只装怪物，home/index 记录原址——
    #   * "enemies"/"friends"/"employees"/"temp_friends" —— 从对应 list 摘走再插回原下标；
    #   * "player" —— state.player 是单字段，摘不走，改为就地打 `_delayed_by_seal`
    #     标记（见 detach_from_field），回场时清标记即可。
    # 字段名保留 delayed_monster_reentries 是为了不打断既有调用方与存档。
    delayed_monster_reentries: list[dict] = field(default_factory=list)

    # 员工背叛：待处理标记（[战终]检查命中后置真，三个处理分支任一生效后清空）
    rebellion_active: bool = False
    # 员工背叛·镇压子战斗：进行中标记（employees已搬入enemies，需resolve_rebellion_battle结算）
    rebellion_in_progress: bool = False
    # 员工背叛·让利：每场工资在原公式基础上的固定加成（本次轮回持续生效）
    wage_bonus: int = 0

    # 养蛊场（怪物互斗）训练模式：开启后怪物阶段的合法目标从「玩家侧」扩为「其它怪物」，
    # 并允许逐个 actor 提交（严格交替，避免先手方一次结算全场）。默认 False——
    # 正式玩法与既有测试完全不受影响，只有训练沙盒会打开它。
    arena_ffa: bool = False
    # 怪物行动自由化：打开后不再强制怪物发动道纹，
    # 每只怪物每回合有 2 个「行动槽」——1 次攻击或 1 次道纹各占 1 槽，
    # 允许「连续两次攻击」「连续两次道纹」「一攻一道纹」，也可以只出 1 槽。
    # 默认 False——正式玩法与既有测试完全不受影响。
    monster_free_actions: bool = False
    # 最终的冠冕/第8场死斗：进行中标记 + 当前该谁出手("player_side"/"opponent_side")
    in_final_duel: bool = False
    duel_turn: str = ""
    # 四项先手属性完全相同时：首回合随机，之后每回合交换先手方。
    duel_tie_alternating: bool = False
    duel_round_first: str = ""
    duel_rounds_started: int = 0

    # 龙心谷"炼心"：待生效标记；下一次玩家实际支付数值型代价后转化为对应类型的【××龙心】消耗品
    pending_lianxin: bool = False
    pending_sha_qi_choices: list = field(default_factory=list)
    dead_monsters: list = field(default_factory=list)  # 乱葬岗招魂：本场已命零的怪物尸体
    # 战斗事件流（唯一事实源，只增不改）。CombatEngine.event_stream 是它的别名视图。
    # 只登记“发生了什么”，每条事件的 ctx 字段挂 EffectContext 快照回答“为什么发生”。
    # 纯观测用：任何战斗规则都不得依赖本列表做判定。
    combat_events: list = field(default_factory=list)
    pending_sha_qi_mode: str = ""
    # 炼心在战斗中发动时不消耗出手，改为"下次局外行动多消耗1点精力"，此处累计待结算的额外精力消耗
    pending_energy_penalty: int = 0

    # 熔谷终音"真龙之心"：龙性资源池。
    # 已解锁的龙族项目本身以【遗物】形式存放在 self.relics（tags含"龙族"），
    # 不再维护独立名单——dragon_traits 是对 relics 的只读视图，见下方 property。
    dragon_nature: int = 0
    # 震岳龙躯剩余持续回合数（0=未激活）；断尾求生已消耗的遗物记录见dragon_traits变化
    dragon_body_shield_rounds: int = 0
    # 断尾求生：预先声明"若本次伤害会使自身命零，移除该龙族遗物来抵消伤害"；为空=未声明保护
    dragon_tail_sacrifice_declared: str = ""

    # 终音法器（三选一/四选一后获得的具名法器，跨副本共享同一个列表）
    artifacts_owned: list[str] = field(default_factory=list)
    # 红头绳解锁的局外行动"献祭"
    has_sacrifice_action: bool = False
    # 罪业金库/教父左轮等法器自身状态
    godfather_revolver_uses: int = 0
    # 死斗胜利后待选择的终音法器所属副本（非空=等待choose_terminal_artifact）
    pending_terminal_region: str = ""
    # 共心环：本场战斗选定的可共享龙心类型（空=未选定/未持有共心环）
    shared_dragon_heart_type: str = ""
    # 负岳碑：预先声明保护的稳定实体引用（friend:index / employee:index）。
    fuyuebei_declared: list[str] = field(default_factory=list)

    # 初拥之夜：待选择标记 + 已选过的1~8号遗物(每项限一次，9号不计入) + 已获得的赤族
    pending_first_embrace: bool = False
    # 仅当初拥之夜是由死斗胜利(领取猩红尖牙)触发时为True：完成本次选择(非"封存血脉")后应紧接着完整封存
    seal_pending_after_embrace: bool = False
    # 初拥之夜所得同样以【遗物】形式存放在 self.relics（tags含"血族"）；
    # first_embrace_traits 是对 relics 的只读视图，见下方 property。
    chizu_names: list[str] = field(default_factory=list)
    # 真理眼冷却：剩余需要经过的战斗场数(战终-1，0=可用)
    truth_eye_cooldown: int = 0

    
    # 属性点
    attribute_points: int = 0
    allocated_blood: int = 0     # 已分配血限（从属性点）
    
    # ---- 遗物视图：血族/龙族项目一律以遗物形式存放，遗物是唯一事实源 ----
    # 这样它们自动继承遗物的全部通用规则（可被销毁、交换、封印、计入"一件当前遗物"）。

    def _relic_names_by_tag(self, tag: str) -> list[str]:
        return [r.name for r in self.relics if tag in r.tags]

    @property
    def dragon_traits(self) -> list[str]:
        """龙族遗物名单（对 relics 的只读视图）。"""
        return self._relic_names_by_tag("龙族")

    @property
    def first_embrace_traits(self) -> list[str]:
        """血族遗物名单（对 relics 的只读视图）。"""
        return self._relic_names_by_tag("血族")

    @property
    def opponent_dragon_traits(self) -> list[str]:
        """死斗对手的龙族遗物名单（对 opponent_relics 的只读视图）。"""
        return [r.name for r in self.opponent_relics if "龙族" in r.tags]

    @property
    def opponent_first_embrace_traits(self) -> list[str]:
        """死斗对手的血族遗物名单（对 opponent_relics 的只读视图）。"""
        return [r.name for r in self.opponent_relics if "血族" in r.tags]

    def apply_heal(
        self, entity: Entity, amount: int,
        ctx: Optional[EffectContext | dict] = None,
    ) -> dict:
        """统一回复入口；玩家侧溢出回复会被【龙血瓶】转为可提取耐久。

        ctx 为兼容层来源上下文；未传时保持原回复行为，并在返回明细中给出 warning。
        """
        # 结算生命周期记账（engine/resolution.py）：状态层拿不到引擎，
        # 经既有观察者表解析（combat_events.engine_for_state）；拿不到就跳过记账
        # ——本函数也可在纯 GameState 单测里独立调用。
        _heal_engine = engine_for_state(self)
        _hp_before = entity.current_hp
        with resolution_frame(_heal_engine, KIND_HEAL,
                              getattr(entity, "name", "?"), amount):
            try:
                return self._apply_heal_inner(entity, amount, ctx)
            finally:
                note_delta(_heal_engine, "hp", _hp_before, entity.current_hp)

    def _apply_heal_inner(self, entity: Entity, amount: int,
                          ctx: Optional[EffectContext | dict] = None) -> dict:
        """回复的实现体（对外契约见 apply_heal）。

        【第一杯】：持有者受到的回复翻倍（唯一事实源 `heal_multiplier`）。
        倍率在进入实现体的第一行生效，所以回复明细、`total_healed`
        （癌变累计）与战斗事件里的数值都是**翻倍后**的真实值，不存在
        "账上一份、面板另一份"。
        """
        amount = amount * self.heal_multiplier(entity)
        heal_ctx = normalize_context(ctx)
        if heal_ctx is None:
            heal_ctx = make_context(
                timing=self.phase or "unknown", source="legacy_heal", source_type="legacy",
                target=entity, mechanic="heal", subtype="legacy", amount=amount,
                tags={"legacy_context"},
            )
            legacy = True
        else:
            legacy = False
            if heal_ctx.mechanic != "heal":
                heal_ctx = make_context(
                    timing=heal_ctx.timing, source=heal_ctx.source,
                    source_type=heal_ctx.source_type, actor=heal_ctx.actor,
                    target=heal_ctx.target or entity, owner=heal_ctx.owner,
                    mechanic="heal", subtype=heal_ctx.subtype, amount=heal_ctx.amount or amount,
                    tags=heal_ctx.tags, event_id=heal_ctx.event_id,
                    parent_event_id=heal_ctx.parent_event_id,
                )
        # 2026-10-14：【猩红果实】持有者当前生命可超出[血限]，回复不再封顶
        # （溢出量归零，【忘忧香】/【龙血瓶】无溢出可吃——没有溢出，只有实打实的血）。
        detail = entity.heal(amount, allow_overflow=self.hp_overflow_exempt(entity))
        detail["heal_ctx"] = heal_ctx.to_dict()
        heal_events = getattr(entity, "_heal_events", None)
        if heal_events is None:
            entity._heal_events = []
            heal_events = entity._heal_events
        heal_events.append(detail["heal_ctx"])
        if legacy:
            detail["context_warning"] = "回复缺少EffectContext；已按legacy来源兼容记录"
        overheal = detail.get("overheal", 0)
        if overheal > 0 and self.on_player_side(entity):
            # 【忘忧香】：战斗被动。受到溢出回复时将其转化为等量格挡。
            # 这是遗物级被动：只要持有者（己方实体/轮回者阵营）持有忘忧香，溢出回复1:1转格挡。
            wangyou = next((r for r in self.relics if r.name == "忘忧香"), None)
            if wangyou is not None:
                entity.gain_shield(overheal)
                detail["wangyouxiang_block"] = overheal
                detail["wangyouxiang_ctx"] = make_context(
                    timing=heal_ctx.timing, source="忘忧香", source_type="relic",
                    actor=entity, target=entity, owner=entity,
                    mechanic="shield", subtype="overheal_to_block", amount=overheal,
                    tags={"overheal", "block", "wangyouxiang"},
                    parent_event_id=heal_ctx.event_id,
                ).to_dict()
            bottle = next((item for item in self.consumables if item.name == "龙血瓶"), None)
            if bottle is not None:
                # 忘忧香先转化格挡，剩余溢出再进龙血瓶；两者同时存在时忘忧香优先。
                remaining = overheal - detail.get("wangyouxiang_block", 0)
                if remaining > 0:
                    bottle.current_uses += remaining
                    bottle.max_uses += remaining
                    detail["dragon_blood_bottle_stored"] = remaining
                    detail["dragon_blood_bottle_ctx"] = make_context(
                        timing=heal_ctx.timing, source="龙血瓶", source_type="consumable",
                        actor=entity, target=entity, owner=entity,
                        mechanic="heal_storage", subtype="overheal", amount=remaining,
                        tags={"overheal", "storage"}, parent_event_id=heal_ctx.event_id,
                    ).to_dict()
        self.emit_combat_event(
            CombatEventType.HEAL_APPLIED,
            actor=heal_ctx.actor, target=entity, ctx=detail["heal_ctx"],
            heal_amount=amount,
            actual_heal=detail.get("actual_heal", 0),
            overheal=detail.get("overheal", 0),
            hp_before=detail.get("hp_before"),
            hp_after=detail.get("hp_after"),
        )
        return detail

    def emit_combat_event(
        self, event_type: CombatEventType, *,
        actor: Any = None, target: Any = None,
        ctx: Optional[dict] = None, **data: Any,
    ) -> CombatEvent:
        """向战斗事件流登记一条“发生了什么”。纯观测，不参与任何规则判定。"""
        def _name(ref: Any) -> str:
            # ref 可能是实体，也可能已经是 EffectContext.to_dict() 降级后的名字字符串
            # （例如 Hook 用 normalize_context(detail["ctx"]) 还原出来的上下文）。
            if ref is None:
                return ""
            if isinstance(ref, str):
                return ref
            return getattr(ref, "name", "")

        event = CombatEvent(
            event_type=event_type,
            battle_no=self.current_battle,
            round_no=self.current_round,
            actor_name=_name(actor),
            target_name=_name(target),
            data=data,
            ctx=ctx,
        )
        self.combat_events.append(event)
        # 通用事件分发观察者（CombatEngine 在战斗实例构造时注册）：
        # 所有事件类型经此进入机制系统；无观察者（局外/无战斗上下文）时零行为变化。
        observer = get_combat_event_observer(self)
        if observer is not None:
            observer(event, raw_actor=actor, raw_target=target)
        return event

    def on_player_side(self, entity: Entity) -> bool:
        """必须用 is，不能用 dataclass 相等。"""
        if entity is None:
            return False
        if self.player is not None and entity is self.player:
            return True
        for e in self.friends:
            if e is entity:
                return True
        for e in self.employees:
            if e is entity:
                return True
        for e in self.temp_friends:
            if e is entity:
                return True
        return False

    def on_enemy_side(self, entity: Entity) -> bool:
        """必须用 is，不能用 dataclass 相等。"""
        if entity is None:
            return False
        for e in self.enemies:
            if e is entity:
                return True
        return False

    # ---- 【第一杯】倍率----
    # 旧条文「每次回复额外+50%、[回终]未回复则流血10、免疫癌变」全部废止；
    # 新条文「你受到的[回复]与失去的生命翻倍」。两条倍率都只读这一处，
    # 引擎其它地方不得再各写一份判断（朋友/员工不继承由 side_has 保证）。
    FIRST_CUP = "第一杯"
    FIRST_CUP_MULTIPLIER = 2

    def heal_multiplier(self, entity: Entity) -> int:
        """【第一杯】：该实体受到的[回复]倍率（非持有者=1）。

        入口唯一：所有回复都经 `apply_heal` → `_apply_heal_inner`，
        倍率在那里生效，因此累计回复量（癌变阈值）、回复事件与「回复量」本身
        三者天然一致。
        """
        return self.FIRST_CUP_MULTIPLIER if self.side_has(entity, self.FIRST_CUP) else 1

    def life_loss_multiplier(self, entity: Entity) -> int:
        """【第一杯】：该实体失去的生命倍率（非持有者=1）。

        只作用于**有明确数值的失去生命**：伤害、数值型【代价】（流血）、
        直接失血（千荆甲反噬等）。血限被压低导致的当前生命封顶、以及
        「当前生命直接置0」的命零类效果（癌变/迷失·迷失/雕塑等）不带数值、
        也不翻倍——它们不是"失去生命"，是判定归零。
        """
        return self.FIRST_CUP_MULTIPLIER if self.side_has(entity, self.FIRST_CUP) else 1

    def _relic_active(self, entity: Entity, name: str) -> bool:
        """遗物持有判定。

        - 归属查 `side_has`（玩家＝本局 relics；敌方轮回者＝opponent_relics）；
        - 玩家侧再查 sealed_relics：被【豪夺/封印】封住的遗物不生效（原口径）。
        新遗物（千荆甲等）的触发一律经本函数，避免各处各写一份持有判定。
        """
        if entity is None or not self.side_has(entity, name):
            return False
        if entity is self.player:
            return self.sealed_relics.get(name, 0) <= 0
        return True

    def huangfu_grants(self) -> set:
        """【黄符】授予登记：{(ally_runtime_id, spell_name)}。

        2026-10-14 审计F4：朋友/员工经黄符获得的法术，发动时 bypass
        「持有全部所需道纹」门禁、改以法力为能源（每阶1点）。授权只在本场
        战斗有效，战终随 battle 法术一并清除（`clear_battle_scoped_spells`）。
        私有属性而非 dataclass 字段：旧存档反序列化缺啥都不报错。
        """
        grants = getattr(self, "_huangfu_grants", None)
        if grants is None:
            grants = set()
            self._huangfu_grants = grants
        return grants

    def hp_overflow_exempt(self, entity: Entity) -> bool:
        """当前生命是否允许超出[血限]（唯一豁免：遗物【猩红果实】）。

        2026-10-14 用户令：【猩红果实】重做为纯被动（`物品索引.md`
        「你的当前生命可以超出[血限]。」），旧效果「战始可选流血10、
        战终血限+2」移交道纹【增殖】。与【苍白之花】的法力豁免
        （`CombatEngine.mana_overflow_exempt`）镜像：只豁免玩家本人，
        被封印时豁免消失。
        """
        if entity is None or entity is not self.player:
            return False
        return self.sealed_relics.get("猩红果实", 0) <= 0 and self.side_has(entity, "猩红果实")

    def side_has(self, entity: Entity, name: str) -> bool:
        """该实体所属轮回者是否持有该终音/初拥/龙族项目。朋友/员工不继承。"""
        if entity is None:
            return False
        if entity is self.player:
            return (name in self.dragon_traits
                    or name in self.first_embrace_traits
                    or name in self.artifacts_owned
                    or any(r.name == name for r in self.relics))
        if entity.entity_type == "轮回者" and self.on_enemy_side(entity):
            return (name in self.opponent_dragon_traits
                    or name in self.opponent_first_embrace_traits
                    or name in self.opponent_artifacts_owned
                    or any(r.name == name for r in self.opponent_relics))
        return False

    def side_body_shield(self, entity: Entity) -> int:
        if entity is self.player:
            return self.dragon_body_shield_rounds
        if entity is not None and entity.entity_type == "轮回者" and self.on_enemy_side(entity):
            return self.opponent_dragon_body_shield_rounds
        return 0

    def side_tail_declared(self, entity: Entity) -> str:
        if entity is self.player:
            return self.dragon_tail_sacrifice_declared
        if entity is not None and entity.entity_type == "轮回者" and self.on_enemy_side(entity):
            return self.opponent_dragon_tail_sacrifice_declared
        return ""

    def clear_side_tail_declared(self, entity: Entity) -> None:
        if entity is self.player:
            self.dragon_tail_sacrifice_declared = ""
        elif entity is not None and entity.entity_type == "轮回者" and self.on_enemy_side(entity):
            self.opponent_dragon_tail_sacrifice_declared = ""

    def remove_side_relic(self, entity: Entity, name: str) -> bool:
        if entity is self.player:
            return self.remove_relic(name)
        if entity is not None and entity.entity_type == "轮回者" and self.on_enemy_side(entity):
            for i, r in enumerate(self.opponent_relics):
                if r.name == name:
                    del self.opponent_relics[i]
                    return True
        return False

    def entity_ref(self, entity: Entity) -> str:
        if entity is self.player:
            return "player:0"
        for prefix, entities in (
            ("friend", self.friends), ("employee", self.employees),
            ("temp_friend", self.temp_friends), ("enemy", self.enemies),
        ):
            for index, candidate in enumerate(entities):
                if candidate is entity:
                    return f"{prefix}:{index}"
        raise ValueError(f"实体{entity.name}不在当前GameState中")
    def entity_by_runtime_id(self, runtime_id: str) -> Optional[Entity]:
        entities = (([self.player] if self.player else []) + self.friends + self.employees
                    + self.temp_friends + self.enemies)
        return next((entity for entity in entities if entity.runtime_id == runtime_id), None)

    def apply_scoped_delta(
        self,
        entity: Entity,
        field_name: str,
        delta: int,
        *,
        scope: str = EffectScope.BATTLE.value,
        polarity: str = EffectPolarity.NEUTRAL.value,
        source: str,
    ) -> int:
        """应用并登记一笔可逆面板变化；仅 round/battle 作用域允许进入账本。"""
        if scope not in (EffectScope.ROUND.value, EffectScope.BATTLE.value):
            raise ValueError("可逆作用域账本只接受round或battle")
        if field_name not in {"blood_limit", "mana_limit", "speed_limit", "attack_count", "attack_power"}:
            raise ValueError(f"字段{field_name}不允许进入作用域账本")
        if not isinstance(delta, int) or isinstance(delta, bool):
            raise ValueError("作用域delta必须是整数")
        before = getattr(entity, field_name)
        after = before + delta
        if field_name in {"blood_limit", "mana_limit", "speed_limit", "attack_count", "attack_power"}:
            after = max(0, after)
        actual_delta = after - before
        setattr(entity, field_name, after)
        if actual_delta:
            self.scoped_effect_ledger.append(ScopedStatDelta(
                entity_id=entity.runtime_id,
                entity_name=entity.name,
                field_name=field_name,
                delta=actual_delta,
                scope=scope,
                polarity=polarity,
                source=source,
            ))
        return actual_delta

    def rollback_scoped_effects(self, scope: str) -> list[dict]:
        """逆序回滚指定作用域，保留代价/伤害/资源与其他作用域。"""
        rolled_back: list[dict] = []
        remaining: list[ScopedStatDelta] = []
        for entry in reversed(self.scoped_effect_ledger):
            if entry.scope != scope:
                remaining.append(entry)
                continue
            entity = self.entity_by_runtime_id(entry.entity_id)
            if entity is None:
                remaining.append(entry)
                continue
            current = getattr(entity, entry.field_name)
            setattr(entity, entry.field_name, max(0, current - entry.delta))
            if entry.field_name in ("blood_limit", "mana_limit", "speed_limit"):
                current_field = {
                    "blood_limit": "current_hp",
                    "mana_limit": "current_mana",
                    "speed_limit": "current_speed",
                }[entry.field_name]
                setattr(entity, current_field,
                        min(getattr(entity, current_field), getattr(entity, entry.field_name)))
            rolled_back.append(entry.to_dict())
        self.scoped_effect_ledger = list(reversed(remaining))
        return rolled_back

    def rollback_scoped_sources(self, entity: Entity, sources: set[str]) -> list[dict]:
        """持续效果到期时回滚同一实体、同一来源的局内面板变化。"""
        rolled_back: list[dict] = []
        remaining: list[ScopedStatDelta] = []
        entity_id = entity.runtime_id
        for entry in reversed(self.scoped_effect_ledger):
            if entry.entity_id != entity_id or entry.source not in sources:
                remaining.append(entry)
                continue
            current = getattr(entity, entry.field_name)
            setattr(entity, entry.field_name, max(0, current - entry.delta))
            if entry.field_name in ("blood_limit", "mana_limit", "speed_limit"):
                current_field = {
                    "blood_limit": "current_hp",
                    "mana_limit": "current_mana",
                    "speed_limit": "current_speed",
                }[entry.field_name]
                setattr(entity, current_field,
                        min(getattr(entity, current_field), getattr(entity, entry.field_name)))
            rolled_back.append(entry.to_dict())
        self.scoped_effect_ledger = list(reversed(remaining))
        return rolled_back

    def grant_relic(self, name: str, effect: str, tag: str = "") -> Relic:
        """授予一件遗物；tag 用于标记 血族/龙族 等来源。"""
        r = Relic(name=name, effect=effect, tags=[tag] if tag else [])
        self.relics.append(r)
        return r

    def remove_relic(self, name: str) -> bool:
        """销毁/移除一件遗物（断尾求生、熔掉遗物等）。"""
        for i, r in enumerate(self.relics):
            if r.name == name:
                del self.relics[i]
                return True
        return False

    @property
    def energy_budget(self) -> int:
        """局外[精力]预算：常规 3 点；无尽模式逐轮递减。

        递减口径（AI 拟定、可由用户一句话改数）：第 C 轮为 `max(1, 3-(C-1))`，
        即 3→2→1 后封底 1 点。[战终]恢复与【逃跑】强制脱战的恢复都走这里。
        """
        if not self.endless_mode:
            return 3
        return max(1, 3 - max(0, int(self.endless_cycle) - 1))

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "phase": self.phase,
            "combat_subphase": self.combat_subphase,
            "current_round": self.current_round,
            "current_battle": self.current_battle,
            "current_region": self.current_region,
            "merchant": self.merchant.to_dict() if getattr(self, "merchant", None) else None,
            "world_state": getattr(self, "world_state", {}),
            "world_enemy_reserve": [e.to_dict() for e in getattr(self, "world_enemy_reserve", [])],
            "world_encounter_entities": [e.to_dict() for e in getattr(self, "world_encounter_entities", [])],
            "world_encounter_id": getattr(self, "world_encounter_id", ""),
            "energy": self.energy,
            "shards": self.shards,
            "shards_peak": self.shards_peak,
            "fake_shards": self.fake_shards,
            "blacklist_level": self.blacklist_level,
            "is_blacklisted": self.is_blacklisted,
            "pending_wage_decisions": self.pending_wage_decisions,
            "pending_daowen_choices": self.pending_daowen_choices,
            "pending_relic_choices": list(self.pending_relic_choices),
            "pending_relic_source": self.pending_relic_source,
            "pending_initial_daowen_choices": list(self.pending_initial_daowen_choices),
            "pending_initial_daowen_source": self.pending_initial_daowen_source,
            "pending_diandang_discoveries": [list(g) for g in self.pending_diandang_discoveries],
            "pending_redemption": dict(self.pending_redemption),
            "pending_item_choices": list(self.pending_item_choices),
            "pending_item_source": self.pending_item_source,
            "pending_monster_phase": self.pending_monster_phase,
            "pending_attack": self.pending_attack,
            "pending_event_queue": list(self.pending_event_queue),
            "event_modifiers": self.event_modifiers,
            "scoped_effect_ledger": [entry.to_dict() for entry in self.scoped_effect_ledger],
            "forced_monsters_next_battle": self.forced_monsters_next_battle,
            "monster_reinforcements": list(self.monster_reinforcements),
            "delayed_monster_reentries": [
                {"name": entry["monster"].name, "return_round": entry["return_round"],
                 "home": entry.get("home") or "enemies"}
                for entry in getattr(self, "delayed_monster_reentries", [])
            ],
            "rebellion_active": self.rebellion_active,
            "rebellion_in_progress": self.rebellion_in_progress,
            "wage_bonus": self.wage_bonus,
            "in_final_duel": self.in_final_duel,
            "duel_turn": self.duel_turn,
            "duel_tie_alternating": self.duel_tie_alternating,
            "duel_round_first": self.duel_round_first,
            "sealed_candidates": self.sealed_candidates,
            "duel_tier": self.duel_tier,
            "duel_defending_snapshot": self.duel_defending_snapshot,
            # 阶级推进 / 无尽模式
            "unlocked_tier": self.unlocked_tier,
            "endless_mode": self.endless_mode,
            "endless_cycle": self.endless_cycle,
            "energy_budget": self.energy_budget,
            "attribute_points": self.attribute_points,
            "player": self.player.to_dict() if self.player else None,
            "friends": [f.to_dict() for f in self.friends],
            "employees": [e.to_dict() for e in self.employees],
            "temp_friends": [t.to_dict() for t in self.temp_friends],
            "enemies": [e.to_dict() for e in self.enemies],
            "relics": [r.to_dict() for r in self.relics],
            "consumables": [c.to_dict() for c in self.consumables],
            "resonance": self.resonance,
            "dragon_hearts": [d.to_dict() for d in self.dragon_hearts],
            "artifacts": self.artifacts,
            "death_book_wisdom": self.death_book_wisdom,
            "death_book_legacies": self.death_book_legacies,
            "sealed_candidate": self.sealed_candidate,
            # 角色性格特征（只导出仍被追踪的存活角色，见 engine/personality.py）
            "personality_traits": personality_export_for_ai(self),
        }
    
    # ==================== 【封印】暂离：脱场与回场 ====================
    # 2026-10-08 用户令：【封印】可作用于任意目标（含自己）。引擎侧唯一的语义是
    # "从场上摘走、X 回合后原样放回"，因此脱场/回场必须是**成对**的，且要记住
    # 原址——不能像旧实现那样一律塞回 enemies（否则封印自己的[朋友]会把它变成敌人）。

    SEAL_HOMES = ("enemies", "friends", "employees", "temp_friends")

    @staticmethod
    def is_sealed_away(entity: Optional[Entity]) -> bool:
        """该实体是否正处于【封印】暂离（不在场上、不可被选、不可行动）。"""
        return entity is not None and bool(getattr(entity, "_delayed_by_seal", False))

    def seal_home_of(self, entity: Entity) -> tuple[str, Optional[int]]:
        """该实体当前属于场上哪个容器（含下标）。玩家返回 ("player", None)。"""
        if self.player is not None and entity is self.player:
            return ("player", None)
        for key in self.SEAL_HOMES:
            container = getattr(self, key)
            for index, candidate in enumerate(container):
                if candidate is entity:
                    return (key, index)
        raise ValueError(f"实体{entity.name}不在当前场上，无法【封印】")

    def detach_from_field(self, entity: Entity) -> tuple[str, Optional[int]]:
        """把实体摘离战场，返回原址 (home, index) 供回场使用。"""
        home, index = self.seal_home_of(entity)
        if home != "player":
            getattr(self, home).pop(index)
        # 玩家是单字段摘不走，用标记表达"不在场上"；所有枚举场上单位的入口
        # （_combat_entity_refs / get_all_player_side）都必须尊重这个标记。
        entity._delayed_by_seal = True
        return (home, index)

    def reattach_to_field(self, home: str, index: Optional[int], entity: Entity) -> None:
        """把实体放回它脱场前所在的容器与下标（下标已被占满则追加到末尾）。"""
        if home != "player":
            container = getattr(self, home)
            container.insert(min(index or 0, len(container)), entity)
        entity._delayed_by_seal = False

    def get_all_player_side(self) -> list[Entity]:
        """获取己方所有实体（[员工]需 is_deployed=True 才计入战场；已【撤退】者不再计入本场战斗）

        被【封印】暂离者不计入——它不在场上，也就吃不到[回始]/[回终]的持续结算。
        """
        entities = []
        if self.player and self.player.is_alive and not self.is_sealed_away(self.player):
            entities.append(self.player)
        entities.extend(f for f in self.friends if f.is_alive and not f.has_retreated
                        and not self.is_sealed_away(f))
        entities.extend(e for e in self.employees if e.is_alive and e.is_deployed and not e.has_retreated
                        and not self.is_sealed_away(e))
        entities.extend(t for t in self.temp_friends if t.is_alive and not t.has_retreated
                        and not self.is_sealed_away(t))
        return entities
    
    def get_all_enemy_side(self) -> list[Entity]:
        """获取敌方所有存活实体（被【封印】暂离者已被摘出 enemies，不在此列）"""
        return [e for e in self.enemies if e.is_alive]

    # ==================== 战斗结束与胜利·统一判定 ====================
    # 引擎内一切"战斗是否结束/能否战终"的判断必须走以下四个方法，禁止再散落写 is_alive 组合。

    def enemy_combat_active(self, enemy: Entity) -> bool:
        """该敌人是否仍构成战斗障碍（阻塞战终）。

        DM裁定：战斗胜利＝敌方全部角色【命零】或【永久离场】。
        永久离场事件（雕塑/癌变/还债/救赎/逃跑及未来新增）经
        Entity.depart_battle() 记为离场；【封印】暂离队列单独阻塞战终。
        永久离场不视为击杀、不产碎片（分类见 battle_end 读取 departure_reason）。
        """
        return enemy.is_alive and not enemy.is_departed

    def active_enemies(self) -> list[Entity]:
        """仍构成战斗障碍的敌人列表。"""
        return [e for e in self.enemies if self.enemy_combat_active(e)]

    def battle_won(self) -> bool:
        """战斗胜利＝敌方全部角色均已命零/永久离场，且没有待进场增援或封印暂离的单位。

        2026-10-08 起 delayed_monster_reentries 可能装着己方单位（封印自己/队友），
        队列非空一律阻塞战终——否则会出现"把队友封印后直接结算战终"的漏洞。
        """
        if getattr(self, "monster_reinforcements", None):
            return False
        if getattr(self, "delayed_monster_reentries", None):
            return False
        return not self.active_enemies()

    def battle_lost(self) -> bool:
        """战斗失败＝轮回者非存活（无论死因：伤害/凡庸/癌变/迷失/代价）。"""
        return self.player is None or not self.player.is_alive

    def battle_over(self) -> bool:
        """战斗胜负已定（胜或负任一成立）。"""
        return self.battle_lost() or self.battle_won()
