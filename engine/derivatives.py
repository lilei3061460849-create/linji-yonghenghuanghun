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
    cost_kind: str          # 生命／法力／血限／枯竭／速度／碎片／假碎片／攻击力
    rate: int               # 代价 = rate × X
    effect_kind: str        # multi_hit / mana_gain / heal
    cost_text: str          # 条文「支付：…」的原样短句（X 保留字面）
    effect_text: str        # 条文效果句（X 保留字面）
    note: str = ""          # 汇率裁定理由（只进注释/索引，不进结算）


# ---- 杀伐衍生：原【连击】六通道拆散，效果恒同（5X 次各 1 点伤害）----
# 生命／血限／枯竭三条沿用原通道倍率：生命⇄法力闭环（透支4X⇄再生4X）与
# 上限类整数 X 计价都不能再抬；法力／碎片／假碎片三条因「只能走一条途径」
# 牺牲了灵活性，按汇率看情况调整各降一档（5→4、5→4、50→40）。
_SHAFA_HITS = "选择[目标]5X次，每次对其造成1点伤害。"

# ---- 透支衍生：母本只留【枯竭X】一条途径 ----
# 生命4X、血限2X、速度X 三条沿用原倍率：4:1 是【再生】闭环净值归零的硬约束，
# 速度 k=1 是「卖速买速」不永动的硬约束，均不可抬汇率。
_TOZHI_GAIN = "你获得X点法力。"

# ---- 再生衍生：母本只留【法力X】一条途径 ----
# 攻击力4X 沿用原倍率：与【透支】生命4X⇄X法力构成严格 4:1 闭环，净值归零。
_ZAISHENG_HEAL = "为[目标]回复4X点生命。"


DERIVATIVE_SPECS: tuple[DerivativeSpec, ...] = (
    DerivativeSpec("连击", "杀伐", "生命", 5, "multi_hit",
                   "支付：生命5X", _SHAFA_HITS,
                   "原多途径【连击】的生命通道，保留旧名与旧汇率；旧档【血债】仍映射到它。"),
    DerivativeSpec("连珠", "杀伐", "法力", 4, "multi_hit",
                   "支付：法力4X", _SHAFA_HITS,
                   "法力通道：牺牲多途径灵活性，5X→4X。"),
    DerivativeSpec("血伐", "杀伐", "血限", 1, "multi_hit",
                   "支付：血限X", _SHAFA_HITS,
                   "血限通道：上限类按整 X 计价，不另抬倍率。"),
    DerivativeSpec("竭伐", "杀伐", "枯竭", 1, "multi_hit",
                   "支付：枯竭X", _SHAFA_HITS,
                   "枯竭通道：登记 X 点待结算枯竭，计价不变。"),
    DerivativeSpec("碎伐", "杀伐", "碎片", 4, "multi_hit",
                   "支付：碎片4X", _SHAFA_HITS,
                   "真碎片通道：5X→4X；按通用【碎片】代价口径，假碎片优先、真碎片补足。"),
    DerivativeSpec("伪伐", "杀伐", "假碎片", 40, "multi_hit",
                   "支付：假碎片40X", _SHAFA_HITS,
                   "假碎片通道：50X→40X，只扣假碎片池。"),
    DerivativeSpec("燃血", "透支", "生命", 4, "mana_gain",
                   "支付：生命4X", _TOZHI_GAIN,
                   "生命通道：4:1 与【再生】闭环守恒，不可抬汇率。"),
    DerivativeSpec("折寿", "透支", "血限", 2, "mana_gain",
                   "支付：血限2X", _TOZHI_GAIN,
                   "血限通道：上限类按 2X 计价，不另抬倍率。"),
    DerivativeSpec("疾贷", "透支", "速度", 1, "mana_gain",
                   "支付：速度X", _TOZHI_GAIN,
                   "速度通道：k=1，卖 X 速度得 X 法力，避免与【超频】永动。"),
    DerivativeSpec("舍力", "再生", "攻击力", 4, "heal",
                   "支付：攻击力4X", _ZAISHENG_HEAL,
                   "攻击力通道：4:1 与【透支】闭环守恒，不可抬汇率。"),
)

BY_NAME: dict[str, DerivativeSpec] = {spec.name: spec for spec in DERIVATIVE_SPECS}
DERIVED_DAOWEN: frozenset[str] = frozenset(BY_NAME)
DERIVATIVE_PARENTS: tuple[str, ...] = tuple(dict.fromkeys(spec.parent for spec in DERIVATIVE_SPECS))


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
    "血限": (CostType.AGING.value, "cost_blood_limit"),
    "枯竭": (CostType.EXHAUST.value, "cost_mana_limit"),
    "速度": (CostType.FATIGUE.value, "cost_speed"),
    "碎片": ("碎片", "cost_real_shards"),
    "假碎片": ("碎片", "cost_fake_shards"),
    "攻击力": ("攻击力", "cost_attack"),
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
    if spec.effect_kind == "multi_hit":
        hits = 5 * x
        calc["hits"] = hits
        calc["damage_per_hit"] = 1
        calc["total_damage"] = hits
        effect = f"选择{target_name}{hits}次，每次造成1点伤害"
    elif spec.effect_kind == "mana_gain":
        calc["mana_gain"] = x
        effect = f"获得{x}点法力"
    elif spec.effect_kind == "heal":
        calc["target_heal"] = 4 * x
        effect = f"为{target_name}回复{4 * x}点生命"
    else:  # pragma: no cover - 事实源自检兜底
        raise ValueError(f"衍生道纹{name}的效果种类非法：{spec.effect_kind}")
    # 摘要里写的是**实际金额**，不是把字面 "X" 替换成 X 值：
    # 「法力4X」在 X=7 时应为「法力28」，若用 str.replace 会得到「法力47」。
    calc["summary"] = f"支付：{spec.cost_kind}{amount}，{effect}"
    return calc


def docstring_of(name: str) -> str:
    """供 `全道纹索引` 生成器解析的单行条文（格式与其它 calculate_* 一致）。"""
    spec = spec_of(name)
    return f"{name}X：{spec.cost_text}。{spec.effect_text}"


def calculator_for(name: str):
    """返回一个带正确签名/docstring 的计算函数，供 DaoWenEngine 注册。"""
    spec = spec_of(name)
    if spec.effect_kind == "mana_gain":
        # 自身作用：无 target 形参，索引自动标「自身」。
        def calc(x: int) -> dict:
            return calculation(name, x)
    else:
        def calc(x: int, target=None) -> dict:
            return calculation(name, x, target=target)
    calc.__name__ = f"calculate_{name}"
    calc.__doc__ = docstring_of(name)
    return calc


def validate() -> None:
    """事实源自检：名字二字、母本非衍生、途径唯一、池内不重名。"""
    assert len(DERIVATIVE_SPECS) == len(BY_NAME), "衍生道纹重名"
    for spec in DERIVATIVE_SPECS:
        assert len(spec.name) == 2, f"衍生道纹名必须二字：{spec.name}"
        assert spec.cost_kind in _COST_FIELDS, f"非法支付途径：{spec.name}"
        assert not is_derivative(spec.parent), f"母本不得是衍生道纹：{spec.parent}"
        assert spec.rate >= 1, f"汇率必须为正整数：{spec.name}"


validate()
