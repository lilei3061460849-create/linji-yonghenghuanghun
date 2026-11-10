"""衍生道纹：母本道纹 → 单途径收益置换的事实源与计算接口。

2026-11-09 用户令（【衍生道纹】核心规则）：

    母本道纹 → 支付【转换】×1 + 血限2 + 灵魂碎片3 →
    从该母本的衍生道纹池中【发现】一种衍生道纹获取。

- 每个衍生道纹独立占用 1 张死者之页；不满意可重新支付血限2刷新选项。
- 衍生道纹严格受限于母本道纹的法则领域，不可跨界产生其它道纹的法则效果；
  本质是法则内部的收益置换：改支付途径，或牺牲灵活性／安全性／适用性，
  换更高数值汇率或特殊机制。
- 多途径道纹一律收成**一条途径**；其余途径同效果的道纹全部改为衍生道纹。

本模块是「衍生关系 + 单途径汇率」唯一事实源；《规则正文》抄录条文，
`DaoWenEngine` 从这里生成计算函数，`sim/gen_daowen_index.py` 从这里生成索引小节，
`engine/gamedata.py` 从这里把衍生道纹踢出学习／残韵／开局发现池。
"""
from __future__ import annotations

from dataclasses import dataclass

from .enums import CostType


# 一次衍生发现／刷新的代价（局内 0 出手行动，按《规则正文》原文口径）。
DERIVE_RESONANCE = "转换"
DERIVE_RESONANCE_COST = 1
DERIVE_BLOOD_LIMIT_COST = 2
DERIVE_SHARD_COST = 3
REFRESH_BLOOD_LIMIT_COST = 2
DISCOVERY_OPTION_COUNT = 3  # 发现＝从随机抽取的 3 个未持有选项中选择 1 个


@dataclass(frozen=True)
class DerivativeSpec:
    """一条衍生道纹：母本、唯一支付途径、汇率、以及它置换出的同域效果。"""

    name: str
    parent: str
    cost_kind: str          # 生命／法限／血限／速度／碎片／攻击力
    rate: int               # 代价 = rate × X
    effect_kind: str        # multi_hit / damage / ignore_shield / life_loss /
                            # others_life_loss / mana_gain / heal
    cost_text: str          # 条文「支付：…」的原样短句（X 保留字面）
    effect_text: str        # 条文效果句（X 保留字面）
    note: str = ""          # 汇率裁定理由（只进注释/索引，不进结算）
    effect_rate: int = 0    # 效果量 = effect_rate × X；0 表示按种类取默认（见 _DEFAULT_EFFECT_RATE）

    def effect_amount(self, x: int) -> int:
        """这条衍生道纹在 X 下的效果量（段数／伤害／法力／回复同理）。"""
        rate = self.effect_rate or _DEFAULT_EFFECT_RATE[self.effect_kind]
        return rate * x


# 各效果种类的默认每 X 效果量（没写 effect_rate 时用它）。
_DEFAULT_EFFECT_RATE = {
    "multi_hit": 5,          # 5X 段
    "damage": 5,             # 5X 点
    "ignore_shield": 5,      # 5X 点
    "life_loss": 5,          # 5X 点
    "others_life_loss": 5,   # 5X 点
    "mana_gain": 1,          # X 点法力
    "heal": 4,               # 4X 点回复
}


# ---- 杀伐衍生：效果同在「让目标失去生命」这一法则领域内，形态各不相同 ----
# 2026-11-09 用户令（第二轮）：
#   ① 取消假碎片途径，碎片途径只留一条，名为【买命】；
#   ② 原「法力通道」改成【法限】——法力本身已经是母本【杀伐】的支付途径，
#      衍生再收法力就等于照抄母本；
#   ③ 只保留一条多段低伤（【连击】），其余改伤害表现：
#      单段／无视格挡／直接失去生命／除目标外全体。
_SHAFA_MULTI_HIT = "选择[目标]5X次，每次对其造成1点伤害。"
_SHAFA_DAMAGE = "对[目标]造成12X点伤害。"
_SHAFA_PIERCE = "对[目标]造成5X点伤害，无视格挡。"
_SHAFA_LIFE_LOSS = "使[目标]直接失去5X点生命。"
_SHAFA_OTHERS = "除[目标]外，所有角色失去5X点生命。"

# ---- 透支衍生：母本只留【枯竭X】一条途径 ----
# 生命4X、血限2X、速度X 三条沿用原倍率：4:1 是【再生】闭环净值归零的硬约束，
# 速度 k=1 是「卖速买速」不永动的硬约束，均不可抬汇率。
_TOZHI_GAIN = "你获得X点法力。"
_TOZHI_GAIN_2X = "你获得2X点法力。"

# ---- 再生衍生：母本只留【法力X】一条途径 ----
# 攻击力4X 沿用原倍率：与【透支】生命4X⇄X法力构成严格 4:1 闭环，净值归零。
_ZAISHENG_HEAL = "为[目标]回复4X点生命。"


# 2026-11-09 用户令（第三轮）：**汇率一律压成最简整数比**。
# 「连击 10:5」要写成「2:1」——比率不变，但发动门槛低、X 值自由。
# 同一条规则套到九条上：能约掉的公因子全部约掉（10:5→2:1、5:5→1:1、4:4→1:1、
# 2:2→1:1），已经是互质的不动（1:12、1:5、4:1、1:1）。
DERIVATIVE_SPECS: tuple[DerivativeSpec, ...] = (
    # 杀伐：效果同在「让目标失去生命」这一法则领域内，形态各不相同。
    DerivativeSpec("连击", "杀伐", "生命", 2, "multi_hit",
                   "支付：生命2X", "选择[目标]X次，每次对其造成1点伤害。",
                   "杀伐池里唯一的低伤多段：单次 1 点，格挡/闪避按段结算。"
                   "费率 2生命:1伤害，写成最简比（原 10:5）。", 1),
    DerivativeSpec("虚耗", "杀伐", "法限", 1, "damage",
                   "支付：法限X", _SHAFA_DAMAGE,
                   "原「法力通道」改收法限：法力已是母本杀伐的途径。"
                   "法限库存极薄（初始 3），所以 1 点法限换 12 点伤害（1:12 已是最简比）。", 12),
    DerivativeSpec("断脉", "杀伐", "血限", 1, "ignore_shield",
                   "支付：血限X", "对[目标]造成X点伤害，无视格挡。",
                   "单段伤害但无视格挡：换形态不换总量。费率 1:1（原 5:5）。", 1),
    DerivativeSpec("买命", "杀伐", "碎片", 1, "life_loss",
                   "支付：碎片X", "使[目标]直接失去X点生命。",
                   "碎片是全池唯一的局外资源；直接失去生命不吃格挡。费率 1:1（原 5:5）。", 1),
    DerivativeSpec("冲击", "杀伐", "法力", 1, "others_life_loss",
                   "支付：法力X", _SHAFA_OTHERS,
                   "2026-11-09 用户令：代价是法力X（不是速度）。除目标外所有角色失去生命；"
                   "把[目标]选成自己就是「敌方与友方全体失血、你不失血」。", 5),
    # 透支：母本只留【枯竭X】一条途径。
    DerivativeSpec("燃命", "透支", "生命", 4, "mana_gain",
                   "支付：生命4X", _TOZHI_GAIN,
                   "生命通道：4:1 已是最简比，且与【再生】闭环守恒，不可抬。"),
    DerivativeSpec("折年", "透支", "血限", 1, "mana_gain",
                   "支付：血限X", _TOZHI_GAIN,
                   "血限通道：费率 1:1（原 2:2 约成最简比，效率不变，门槛减半）。"),
    DerivativeSpec("搏命", "透支", "速度", 1, "mana_gain",
                   "支付：速度X", _TOZHI_GAIN,
                   "速度通道：k=1，卖 X 速度得 X 法力，避免与【超频】永动。"
                   "2026-11-09 用户令：名字由【夺步】回退为【搏命】。"),
    # 再生：母本只留【法力X】一条途径。
    DerivativeSpec("献锋", "再生", "攻击力", 1, "heal",
                   "支付：本回合攻击力X", "为[目标]回复X点生命。",
                   "2026-11-09 用户令：攻击力是**本回合**的减益（回终自动复原），"
                   "不是永久失去——否则配上【波及】就完爆【弱化】。费率 1:1（原 4:4）。", 1),
)

BY_NAME: dict[str, DerivativeSpec] = {spec.name: spec for spec in DERIVATIVE_SPECS}
DERIVED_DAOWEN: frozenset[str] = frozenset(BY_NAME)
DERIVATIVE_PARENTS: tuple[str, ...] = tuple(dict.fromkeys(spec.parent for spec in DERIVATIVE_SPECS))

# 旧名字 → 现名（只用于读档/授予/怪物面板的读入边界，不用于发动提交）。
# 2026-11-09 第二轮改名：连珠→虚耗（并改收法限）、血伐→断脉、碎伐／伪伐→买命、
# 燃血→燃命、折寿→折年、疾贷→搏命、舍力→献锋；竭伐（枯竭通道）与伪伐（假碎片
# 通道）两条途径被用户取消，旧档按「最接近的现役条目」落地：竭伐→连击、伪伐→买命。
DERIVATIVE_ALIASES: dict[str, str] = {
    "连珠": "虚耗",
    "血伐": "断脉",
    "竭伐": "连击",
    "碎伐": "买命",
    "伪伐": "买命",
    "燃血": "燃命",
    "折寿": "折年",
    "疾贷": "搏命",
    "舍力": "献锋",
}


def is_derivative(name: str) -> bool:
    return name in BY_NAME


def parent_of(name: str) -> str | None:
    spec = BY_NAME.get(name)
    return spec.parent if spec else None


def pool_of(parent: str) -> tuple[str, ...]:
    """母本的衍生道纹池（按条文顺序，稳定可复现）。"""
    return tuple(spec.name for spec in DERIVATIVE_SPECS if spec.parent == parent)


def known_parents() -> tuple[str, ...]:
    return DERIVATIVE_PARENTS


def spec_of(name: str) -> DerivativeSpec:
    try:
        return BY_NAME[name]
    except KeyError:
        raise KeyError(f"未登记衍生道纹: {name}") from None


# ---- 计算层：所有衍生道纹都只是「母本同域效果 + 一条固定支付途径」 ----

_COST_FIELDS = {
    "生命": (CostType.BLEED.value, "cost_hp"),
    "法力": (CostType.MANA.value, "cost"),
    "法限": (CostType.SHRINK.value, "cost_mana_cap"),
    "血限": (CostType.AGING.value, "cost_blood_limit"),
    "枯竭": (CostType.EXHAUST.value, "cost_mana_limit"),
    "速度": (CostType.FATIGUE.value, "cost_speed"),
    # 【买命】走通用【碎片】代价口径（假碎片优先、真碎片补足），不再像旧【碎伐】
    # 那样只认真碎片、也不再保留单独的假碎片途径。
    "碎片": ("碎片", "cost_shards"),
    "假碎片": ("碎片", "cost_fake_shards"),
    "攻击力": ("攻击力", "cost_attack"),
}

# 效果形态 → calc 键。每种形态都给引擎一份**唯一**的伤害表达，不能靠名字硬编码：
#   multi_hit       多段低伤（5X 次各 1 点）
#   damage          单段伤害
#   ignore_shield   单段伤害、无视格挡（走 damage_type="无视格挡"）
#   life_loss       直接失去生命（不吃格挡，走 target_life_loss）
#   others_life_loss  除目标外所有角色失去生命（群体，含自己与友方）
#   mana_gain / heal  同母本同域收益
_EFFECT_FIELDS = {
    "multi_hit": "hits",
    "damage": "target_damage",
    "ignore_shield": "target_damage",
    "life_loss": "target_life_loss",
    "others_life_loss": "others_life_loss",
    "mana_gain": "mana_gain",
    "heal": "target_heal",
}


def calculation(name: str, x: int, target=None) -> dict:
    """生成一条衍生道纹的标准 calc（与 DaoWenEngine 其它条目同形）。"""
    spec = spec_of(name)
    cost_type, cost_key = _COST_FIELDS[spec.cost_kind]
    amount = spec.rate * x
    target_name = target.name if target is not None else "未选定目标"
    calc: dict = {
        "dao_wen": name,
        "x": x,
        "cost_type": cost_type,
        cost_key: amount,
        "derivative_of": spec.parent,
    }
    kind = spec.effect_kind
    assert kind in _EFFECT_FIELDS, f"衍生道纹{name}的效果种类非法：{kind}"
    if kind == "multi_hit":
        hits = spec.effect_amount(x)
        calc["hits"] = hits
        calc["damage_per_hit"] = 1
        calc["total_damage"] = hits
        effect = f"选择{target_name}{hits}次，每次造成1点伤害"
    elif kind == "damage":
        calc["target_damage"] = spec.effect_amount(x)
        effect = f"对{target_name}造成{spec.effect_amount(x)}点伤害"
    elif kind == "ignore_shield":
        calc["target_damage"] = spec.effect_amount(x)
        # 引擎按 damage_type 判定格挡：这一条走「无视格挡」，规则正文同步写明。
        calc["ignore_shield"] = True
        effect = f"对{target_name}造成{spec.effect_amount(x)}点伤害（无视格挡）"
    elif kind == "life_loss":
        calc["target_life_loss"] = spec.effect_amount(x)
        effect = f"使{target_name}直接失去{spec.effect_amount(x)}点生命"
    elif kind == "others_life_loss":
        calc["others_life_loss"] = spec.effect_amount(x)
        effect = f"除{target_name}外，所有角色失去{spec.effect_amount(x)}点生命"
    elif kind == "mana_gain":
        calc["mana_gain"] = spec.effect_amount(x)
        effect = f"获得{spec.effect_amount(x)}点法力"
    elif spec.effect_kind == "heal":
        calc["target_heal"] = spec.effect_amount(x)
        effect = f"为{target_name}回复{spec.effect_amount(x)}点生命"
    else:  # pragma: no cover - 事实源自检兜底
        raise ValueError(f"衍生道纹{name}的效果种类非法：{spec.effect_kind}")
    # 摘要里写的是**实际金额**，不是把字面 "X" 替换成 X 值：
    # 「法力4X」在 X=7 时应为「法力28」，若用 str.replace 会得到「法力47」。
    # 【献锋】的攻击力是**本回合**减益，摘要必须写清楚，免得被读成永久失去。
    cost_label = ("本回合攻击力" if spec.cost_kind == "攻击力" else spec.cost_kind)
    calc["summary"] = f"支付：{cost_label}{amount}，{effect}"
    return calc


def docstring_of(name: str) -> str:
    """供 `全道纹索引` 生成器解析的单行条文（格式与其它 calculate_* 一致）。"""
    spec = spec_of(name)
    return f"{name}X：{spec.cost_text}。{spec.effect_text}"


def calculator_for(name: str):
    """返回一个带正确签名/docstring 的计算函数，供 DaoWenEngine 注册。

    【自身】类衍生（产法力）没有 target 形参：这里用 ``__signature__`` 把
    对外可见的签名收成 ``(x)``，既让生成器标「自身」，又避免 mypy 对同一
    函数名在两个分支里定义不同签名的报错。
    """
    import inspect

    spec = spec_of(name)

    def calc(x: int, target=None) -> dict:
        return calculation(name, x, target=target)

    calc.__name__ = f"calculate_{name}"
    calc.__doc__ = docstring_of(name)
    if spec.effect_kind == "mana_gain":
        setattr(calc, "__signature__", inspect.Signature([
            inspect.Parameter("x", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                              annotation=int),
        ]))
    return calc


def validate() -> None:
    """事实源自检：名字二字、母本非衍生、途径唯一、池内不重名。"""
    assert len(DERIVATIVE_SPECS) == len(BY_NAME), "衍生道纹重名"
    for spec in DERIVATIVE_SPECS:
        assert len(spec.name) == 2, f"衍生道纹名必须二字：{spec.name}"
        assert spec.cost_kind in _COST_FIELDS, f"非法支付途径：{spec.name}"
        assert spec.effect_kind in _EFFECT_FIELDS, f"非法效果形态：{spec.name}"
        assert not is_derivative(spec.parent), f"母本不得是衍生道纹：{spec.parent}"
        assert spec.rate >= 1, f"汇率必须为正整数：{spec.name}"
        # 母本与衍生必须同域：杀伐域只产出「失去生命」，透支域只产法力，再生域只回复。
        domain = {"multi_hit": "杀伐", "damage": "杀伐", "ignore_shield": "杀伐",
                  "life_loss": "杀伐", "others_life_loss": "杀伐",
                  "mana_gain": "透支", "heal": "再生"}[spec.effect_kind]
        assert domain == spec.parent, f"{spec.name}的效果越过母本法则领域：{domain}≠{spec.parent}"
    # 旧名一个都不能还留在现役表里，否则改名等于没改。
    for legacy, current in DERIVATIVE_ALIASES.items():
        assert legacy not in BY_NAME, f"旧名仍在现役表：{legacy}"
        assert current in BY_NAME, f"旧名映射到不存在的道纹：{legacy}→{current}"


validate()
