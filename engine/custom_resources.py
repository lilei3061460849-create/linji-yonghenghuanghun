"""自定义资源（v1）：用现有词汇声明新资源，不改核心管线。

设计定位
--------
「当场设计」＝把新资源现场装进战场再重打。道纹可以现场注册并真结算，遗物过去
只能加牌子（`Relic` 只有 name/effect/tags，行为靠硬接线）。本模块把这条缝补上：
声明式资源 → 编译成引擎已有的执行构件，而不是在 combat.py 里加新的 if。

复用的现有机制（不新增结算管线）
--------------------------------
- 效果执行：`engine.mechanisms.verbs.apply_verb` 的全部动词
  （damage/heal/hp_loss/blood_limit/cost/status/speed/shield/mutation/mana/
   shards/depart/execute/direct_damage）。
- 触发时机：`engine.mechanisms.triggers.Phase` 的既有相位（战始/回始/回终/战终…），
  以及 `Trigger(kind="event")` 的事件型触发。
- 被动资源：编译成 `Mechanism` 注册进 `MECHANISMS`，与内建遗物走同一条分发路径
  （`CombatEngine._dispatch_phase`），conditions 复用 `relic_active` 等组合子。
- 代价：数值代价走 `CombatEngine.pay_numeric_cost`（龙心抵扣、共罪拆分、反应链
  全部照常），法力走 `pay_daowen_mana`，碎片走 `shards` 动词。

四条设计标准的机械校验
----------------------
- 总纲：道纹/遗物/法器必须填 `uniqueness`（为什么现有资源做不到）；消耗品豁免。
- 道纹：名称二字；必须有 X；X 必须改变至少一处数值；默认局内，局外须 `out_of_combat=True`。
- 遗物：不得出现 X；必须有 `trigger`。
- 法器：必须有 `effects` 与 `uniqueness`；不受遗物的 X/触发限制。
- 消耗品：`uses ≥ 1`；`effects` 必填；可有 `trigger` 变为被动型。

明确不做（v1 边界，勿假装已支持）
--------------------------------
- 随机目标（`random_enemy`）：需要与随机源对齐，留给后续版本；现在不支持。
- `失忆`/`唯一` 代价：需要玩家/怪物选择失忆对象，v1 不支持。
- 残韵转化、法术 DSL 内部步骤：自定义道纹按普通道纹结算，不参与闭环路径。
"""
from __future__ import annotations

import ast
import copy
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .mechanisms import (
    MECHANISMS, Mechanism, Phase, Trigger,
    apply_verb, relic_active, verb_names,
)
from .enums import normalize_payment_type, RESOURCE_TYPES

CATEGORIES = ("道纹", "遗物", "法器", "消耗品")

# 与《规则正文·代价》一致的可数值化代价；法力单独走法力通道。
NUMERIC_COST_TYPES = ("生命", "血限", "枯竭", "法限", "速度", "速限", "异变")
SUPPORTED_COST_TYPES = ("法力",) + NUMERIC_COST_TYPES + ("碎片", "假碎片", "冷却")

TARGET_TOKENS = ("self", "target", "all_enemies", "all_allies")

# 动词需要的键名（amount 之外的固定键）
_VERB_REQUIRED_KEYS = {
    "cost": ("cost_type",),
    "status": ("name",),
    "depart": (),
    "execute": (),
}
_VERB_NEEDS_AMOUNT = {"damage", "heal", "hp_loss", "blood_limit", "speed",
                      "shield", "mutation", "mana", "shards", "direct_damage"}

# 遗物不写 X：这些词一旦出现即视为声明了变量。
_X_TOKENS = ("X", "x")


class CustomResourceError(ValueError):
    """声明不合法：拒绝注册，绝不静默降级成牌子。"""


# ==================== 数值表达式（X 的受控白话） ====================

_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Num, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.USub, ast.UAdd, ast.Name, ast.Load,
)


def evaluate(expression: Any, x: int) -> int:
    """计算 `X` 表达式：只允许 X、整数、四则运算与括号；结果按整数规则向上取整。

    拒绝一切函数调用、属性访问与名字（X 除外）——配置是数据，不是代码。
    """
    if isinstance(expression, bool):
        raise CustomResourceError("数值不能是布尔值")
    if isinstance(expression, int):
        return expression
    if not isinstance(expression, str):
        raise CustomResourceError(f"数值必须写成整数或表达式字符串：{expression!r}")
    text = expression.strip()
    if not text:
        raise CustomResourceError("数值表达式为空")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise CustomResourceError(f"数值表达式无法解析：{text}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise CustomResourceError(f"数值表达式含不允许的语法：{type(node).__name__}")
        if isinstance(node, ast.Name) and node.id != "X":
            raise CustomResourceError(f"数值表达式只允许变量 X，收到 {node.id}")
    value = eval(compile(tree, "<resource>", "eval"), {"__builtins__": {}}, {"X": x})
    return int(math.ceil(value))


def declares_x(expression: Any) -> bool:
    if not isinstance(expression, str):
        return False
    return any(token in expression for token in _X_TOKENS)


# ==================== 声明校验 ====================

def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CustomResourceError(message)


def _validate_effect(effect: Any, *, category: str, index: int) -> None:
    _require(isinstance(effect, dict), f"effects[{index}] 必须是对象")
    verb = effect.get("verb")
    _require(isinstance(verb, str) and verb, f"effects[{index}] 缺少 verb")
    _require(verb in verb_names(), f"effects[{index}] 的动词未注册：{verb}")
    token = effect.get("target", "target")
    _require(token in TARGET_TOKENS,
             f"effects[{index}] 的 target 只支持 {TARGET_TOKENS}，收到 {token!r}")
    for key in _VERB_REQUIRED_KEYS.get(verb, ()):
        _require(key in effect, f"effects[{index}] 的动词 {verb} 缺少 {key}")
    if verb == "cost":
        _require(effect["cost_type"] in NUMERIC_COST_TYPES + ("法力",),
                 f"effects[{index}] 的 cost_type 不支持：{effect['cost_type']}")
    if verb in _VERB_NEEDS_AMOUNT:
        _require("amount" in effect, f"effects[{index}] 的动词 {verb} 缺少 amount")
        evaluate(effect["amount"], 1)  # 语法校验；X=1 只为解析
    if category == "遗物" and declares_x(effect.get("amount")):
        raise CustomResourceError(
            f"effects[{index}]：遗物不得含 X 变量（遗物是常驻触发，没有 X 旋钮）")


def _validate_trigger(trigger: Any, *, category: str) -> None:
    _require(isinstance(trigger, dict), f"{category} 的 trigger 必须是对象")
    kind = trigger.get("kind")
    _require(kind in ("phase", "event"), "trigger.kind 只支持 phase / event")
    key = trigger.get("key")
    _require(isinstance(key, str) and key, "trigger.key 不能为空")
    if kind == "phase":
        phases = {v for k, v in vars(Phase).items() if not k.startswith("_")}
        _require(key in phases, f"未知相位：{key}；可用：{sorted(phases)}")


def validate_spec(spec: dict) -> dict:
    """校验一份声明；通过则返回深拷贝，不修改调用方对象。"""
    _require(isinstance(spec, dict), "资源声明必须是对象")
    spec = copy.deepcopy(spec)
    # 旧存档/示例只在支付类型字段迁移；状态名、道纹名不经过别名表。
    cost = spec.get("cost")
    if isinstance(cost, dict) and isinstance(cost.get("type"), str):
        cost["type"] = normalize_payment_type(cost["type"])
    for effect in spec.get("effects", []) if isinstance(spec.get("effects"), list) else []:
        if isinstance(effect, dict) and isinstance(effect.get("cost_type"), str):
            effect["cost_type"] = normalize_payment_type(effect["cost_type"])
    name = spec.get("name")
    category = spec.get("category")
    _require(isinstance(name, str) and name.strip(), "name 不能为空")
    _require(category in CATEGORIES, f"category 只支持 {CATEGORIES}")

    # 总纲：道纹/遗物/法器必须说明「现有资源为什么做不到」；消耗品豁免。
    if category != "消耗品":
        _require(isinstance(spec.get("uniqueness"), str) and spec["uniqueness"].strip(),
                 f"{category}必须填写 uniqueness：现有任何资源都无法实现的效果，才有资格新增")

    effects = spec.get("effects")
    _require(isinstance(effects, list) and effects, "effects 必须是非空列表")
    for index, effect in enumerate(effects):
        _validate_effect(effect, category=category, index=index)

    if category == "道纹":
        _require(len(name) == 2, f"道纹名称仅二字，收到「{name}」（{len(name)} 字）")
        cost = spec.get("cost")
        _require(isinstance(cost, dict), "道纹必须声明 cost")
        _require(cost.get("type") in SUPPORTED_COST_TYPES,
                 f"道纹 cost.type 只支持 {SUPPORTED_COST_TYPES}")
        cost_amount = cost.get("amount", "X")
        evaluate(cost_amount, 1)
        # 必有 X，且 X 必须真的改变点什么（强度或持续时间）。
        scales = any(
            declares_x(effect.get("amount")) or declares_x(effect.get("duration"))
            for effect in effects
        )
        _require(scales, "道纹必须声明 X，且 X 必须改变效果强度或持续时间")
        _require(isinstance(spec.get("out_of_combat", False), bool),
                 "out_of_combat 必须是布尔值")
        _require(spec.get("target", "target") in TARGET_TOKENS,
                 f"道纹 target 只支持 {TARGET_TOKENS}")

    elif category == "遗物":
        _require(not any(declares_x(effect.get("amount")) for effect in effects),
                 "遗物不得含 X 变量")
        _require("trigger" in spec, "遗物必须声明触发时机 trigger")
        _validate_trigger(spec["trigger"], category=category)

    elif category == "法器":
        _require(spec.get("effects"), "法器必须声明明确效果")
        if "trigger" in spec:
            _validate_trigger(spec["trigger"], category=category)
        _require(isinstance(spec.get("costs_action", False), bool),
                 "costs_action 必须是布尔值")

    else:  # 消耗品
        uses = spec.get("uses", 1)
        _require(isinstance(uses, int) and not isinstance(uses, bool) and uses >= 1,
                 "消耗品 uses 必须是 ≥1 的整数")
        if "trigger" in spec:
            _validate_trigger(spec["trigger"], category=category)

    return spec


# ==================== 运行期：目标与效果 ====================

def resolve_targets(token: str, *, combat, actor, target) -> list:
    """把声明里的目标词翻译成当前场上的实体列表。"""
    if token == "self":
        return [actor] if actor is not None else []
    if token == "target":
        return [target] if target is not None else []
    state = getattr(combat, "state", None)
    if state is None:
        return []
    side = state.get_all_player_side() if _on_player_side(state, actor) else state.get_all_enemy_side()
    if token == "all_allies":
        return [entity for entity in side if getattr(entity, "is_alive", False)]
    return [entity for entity in side if getattr(entity, "is_alive", False)
            and entity is not actor]


def _on_player_side(state, actor) -> bool:
    if actor is None or not hasattr(state, "on_player_side"):
        return True
    return bool(state.on_player_side(actor))


def _verb_spec(effect: dict, *, target, actor, amount: int, source: str) -> dict:
    """把声明效果翻译成动词要求的 spec（键名与 verbs.py 的既有约定一致）。

    `source` 一词在不同动词里含义不同：damage 要的是**实体**（用于反伤/龙鳞判定），
    status/blood_limit 要的是**标签字符串**。这里按动词分开给，不混用。
    """
    verb = effect["verb"]
    spec = {"target": target, "actor": actor,
            "source_type": "custom_resource"}
    spec["source"] = actor if verb == "damage" else source
    if verb in _VERB_NEEDS_AMOUNT:
        spec["amount"] = amount
    if verb == "damage":
        spec["damage_type"] = effect.get("damage_type", "普通")
    if verb == "blood_limit":
        spec["delta"] = amount
        spec["polarity"] = effect.get("polarity", "debuff")
    if verb == "speed":
        spec["delta"] = amount
    if verb == "mutation":
        spec["layers"] = amount
    if verb == "mana":
        spec["delta"] = amount
    if verb == "shards":
        spec["delta"] = amount
    if verb == "cost":
        spec["payer"] = target
        spec["cost_type"] = effect["cost_type"]
    if verb == "status":
        spec["name"] = effect["name"]
        spec["duration"] = int(evaluate(effect.get("duration", -1), 1))
        spec["value"] = amount
    if verb == "depart":
        spec["reason"] = effect.get("reason", "custom_resource")
    return spec


def apply_effects(combat, effects: Iterable[dict], *, actor, target, x: int = 1,
                  source: str = "") -> list[dict]:
    """按声明顺序执行效果；每一步都走 verbs 的统一入口。"""
    logs: list[dict] = []
    for effect in effects:
        amount = evaluate(effect.get("amount", 0), x)
        token = effect.get("target", "target")
        targets = resolve_targets(token, combat=combat, actor=actor, target=target)
        if not targets:
            logs.append({"verb": effect["verb"], "target": token, "skipped": "没有合法目标"})
            continue
        for entity in targets:
            spec = _verb_spec(effect, target=entity, actor=actor, amount=amount,
                              source=source)
            if effect["verb"] == "status" and "duration" in effect:
                spec["duration"] = int(evaluate(effect["duration"], x))
            result = apply_verb(combat, effect["verb"], spec,
                                ctx={"timing": "custom_resource", "source": source,
                                     "actor": actor, "target": entity})
            logs.append({"verb": effect["verb"], "target": getattr(entity, "name", ""),
                         "amount": amount, "result": _compact(result)})
    return logs


def _compact(result: Any) -> Any:
    """日志只留可复核的数值，避免整份 state 进战报。"""
    if isinstance(result, dict):
        return {k: v for k, v in result.items()
                if isinstance(v, (int, float, str, bool)) or v is None}
    if isinstance(result, (int, float, str, bool)):
        return result
    return None


# ==================== 注册表 ====================

@dataclass
class CustomResource:
    spec: dict
    mechanism: Optional[Mechanism] = None
    registered_at: str = ""

    @property
    def name(self) -> str:
        return self.spec["name"]

    @property
    def category(self) -> str:
        return self.spec["category"]


_REGISTRY: dict[str, CustomResource] = {}


def get(name: str) -> Optional[CustomResource]:
    return _REGISTRY.get(name)


def all_resources() -> list[CustomResource]:
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def clear() -> None:
    """仅供测试与显式回滚：注销自定义机制与注册表项。"""
    for resource in list(_REGISTRY.values()):
        if resource.mechanism is not None:
            MECHANISMS.unregister(resource.mechanism.name)
    _REGISTRY.clear()
    _CUSTOM_DAOWEN.clear()


# ---- 遗物 / 被动消耗品 → Mechanism ----

def _make_mechanism(spec: dict) -> Mechanism:
    name = spec["name"]
    category = spec["category"]

    def effect(ctx, targets):
        combat = ctx.combat
        actor = ctx.target
        if combat is None or actor is None:
            return None
        if category == "消耗品" and not _consume_durability(combat.state, name):
            return None
        logs = apply_effects(combat, spec["effects"], actor=actor, target=actor,
                             x=1, source=name)
        return [f"{name}：{log['verb']}→{log['target']}（{log['amount']}）"
                for log in logs]

    condition = None
    if category == "遗物":
        condition = relic_active(name)

    trigger = spec["trigger"]
    return Mechanism(
        name=f"自定义{category}·{name}",
        when=Trigger(kind=trigger["kind"], key=trigger["key"]),
        effect=effect,
        target=None,
        condition=condition,
        priority=int(spec.get("priority", 60)),
    )


def _consume_durability(state, name: str) -> bool:
    """被动消耗品触发时扣 1 点耐久；没有可用耐久则不结算。"""
    for item in getattr(state, "consumables", []):
        if item.name == name and getattr(item, "current_uses", 0) > 0:
            item.current_uses -= 1
            return True
    return False


# ---- 道纹 → DaoWenEngine 注册表 ----

_CUSTOM_DAOWEN: dict[str, Callable[..., dict]] = {}
_BRIDGED = {"installed": False}


def _daowen_calculator(spec: dict) -> Callable[..., dict]:
    name = spec["name"]
    cost = spec["cost"]
    effects = spec["effects"]
    target_token = spec.get("target", "target")

    def calculate(x: int, target=None, caster=None, **_: Any) -> dict:
        cost_amount = evaluate(cost.get("amount", "X"), x)
        calc = {
            "dao_wen": name,
            "x": x,
            "cost_type": "消耗" if cost["type"] == "法力" else cost["type"],
            "cost": cost_amount,
            "custom_effects": effects,
            "custom_target": target_token,
            "custom_cost_type": cost["type"],
            "custom_cost_amount": cost_amount,
            "summary": f"{cost['type']}{cost_amount}：{spec.get('summary', name)}",
        }
        return calc

    return calculate


def install_daowen_bridge() -> None:
    """把自定义道纹挂到 `register_all` 之后（该函数每次都会重建注册表）。"""
    if _BRIDGED["installed"]:
        return
    from .daowen import DaoWenEngine

    original = DaoWenEngine.register_all.__func__

    def register_all_patched(cls):
        original(cls)
        cls._registry.update(_CUSTOM_DAOWEN)

    DaoWenEngine.register_all = classmethod(register_all_patched)
    _BRIDGED["installed"] = True


def register(spec: dict) -> CustomResource:
    """校验并注册一份资源声明；种类内名称唯一，冲突即拒绝。"""
    spec = validate_spec(spec)
    name = spec["name"]
    if name in _REGISTRY:
        raise CustomResourceError(f"自定义资源【{name}】已存在")

    resource = CustomResource(spec=spec)
    if spec["category"] == "道纹":
        install_daowen_bridge()
        _CUSTOM_DAOWEN[name] = _daowen_calculator(spec)
        from .daowen import DaoWenEngine
        DaoWenEngine.register_all()  # 立即生效，不必等下一次重建
    elif "trigger" in spec:
        mechanism = _make_mechanism(spec)
        MECHANISMS.register(mechanism)
        resource.mechanism = mechanism

    _REGISTRY[name] = resource
    return resource


# ---- 代价支付（道纹） ----

def pay_custom_cost(engine, actor, instance, calc: dict) -> Optional[str]:
    """支付自定义道纹中「法力以外」的代价；返回错误文案（None=成功）。

    法力代价已在 `_action_use_daowen` 的既有 `pay_daowen_mana` 里支付，
    这里只处理其余通道，全部走既有统一入口。
    """
    amount = int(calc.get("custom_cost_amount", 0))
    cost_type = calc.get("custom_cost_type", "法力")
    if amount <= 0 or cost_type == "法力":
        # 法力已由引擎既有的 pay_daowen_mana 扣过，这里不重复收取。
        return None
    combat = engine.combat
    state = engine.state
    cost_type = normalize_payment_type(cost_type)
    if cost_type in RESOURCE_TYPES:
        try:
            combat.pay_resource(
                actor, cost_type, amount,
                cost_context={"timing": state.combat_subphase or state.phase,
                              "source": calc["dao_wen"], "source_type": "custom_daowen",
                              "actor": actor, "target": actor,
                              "tags": {"active_payment"}})
        except ValueError as exc:
            return str(exc)
        return None
    if cost_type in {"枯竭", "异变"}:
        try:
            combat.pay_numeric_cost(
                actor, cost_type, amount,
                cost_context={"timing": state.combat_subphase or state.phase,
                              "source": calc["dao_wen"], "source_type": "custom_daowen",
                              "actor": actor, "target": actor,
                              "tags": {"active_payment"}})
        except ValueError as exc:
            return str(exc)
        return None
    if cost_type == "碎片":
        if state.on_player_side(actor):
            if state.shards < amount:
                return f"碎片{amount}无法支付：当前{state.shards}"
            state.shards -= amount
        else:
            if actor.shards < amount:
                return f"碎片{amount}无法支付：当前{actor.shards}"
            actor.shards -= amount
        return None
    if cost_type == "假碎片":
        pool = state if state.on_player_side(actor) else actor
        if getattr(pool, "fake_shards", 0) < amount:
            return f"假碎片{amount}无法支付：当前{getattr(pool, 'fake_shards', 0)}"
        pool.fake_shards -= amount
        return None
    if cost_type == "冷却":
        instance.cooldown_remaining = max(instance.cooldown_remaining, amount)
        return None
    return f"暂不支代价类型：{cost_type}"


# ---- 消耗品 ----

def allows_out_of_combat(name: str) -> bool:
    """道纹声明里的局外许可（默认 False：只有明写 out_of_combat=True 才允许）。"""
    resource = _REGISTRY.get(name)
    return bool(resource is not None
                and resource.category == "道纹"
                and resource.spec.get("out_of_combat", False))


def get_consumable(name: str) -> Optional[CustomResource]:
    resource = _REGISTRY.get(name)
    return resource if resource is not None and resource.category == "消耗品" else None


# ---- 文件加载 ----

def load_specs(path: str | Path) -> list[CustomResource]:
    """从 JSON（单个对象或数组）或目录（*.json，按文件名排序）加载声明。"""
    path = Path(path)
    if path.is_dir():
        registered: list[CustomResource] = []
        for file in sorted(path.glob("*.json")):
            registered.extend(load_specs(file))
        return registered
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload if isinstance(payload, list) else [payload]
    return [register(item) for item in items]
