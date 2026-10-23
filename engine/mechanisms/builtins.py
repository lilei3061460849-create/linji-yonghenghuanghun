"""已迁移到声明层的机制（当前注册表共22项，以下为原16项历史说明：加害、龙鳞、帮派令、畸变·结算、
焦黑发丝、洞察·结算、狂暴·标记、畸变·标记、洗劫·夺碎片、缄默面具、龙爪、
清算·结算、灾厄轮盘·结算（遗物被动）、清算·对账；2026-10-13 用户令删【逼债】，
【逼债·结算/对账】与【三相残韵盘·战终】随道纹/遗物删除一并移除）。

迁移协议（迁移前后必须同时满足）：
  1. 规则语义与旧实现完全一致（加害：amount+状态值；龙鳞：max(0, amount-状态值)；
     帮派令：[战始]获得【洗劫3】；
     衰败：[回始]对自己造成 ceil(当前生命×10X/100) 点伤害，走完整伤害管线；
     畸变·结算：[回终]失去(攻击力×攻击次数)点血限，血限压 0 连带命零统一判定；
     焦黑发丝：怪物命零 → 玩家速度+2（经统一速度入口）；
     洞察·结算：[回始]待结算法力经 mana 动词获得（含不朽之躯钳制）；
     畸变·标记：纯报告条目，无动词；狂暴·标记随【狂暴】道纹删除）；
  2. priority 保持原值或按旧代码位置固化：加害=20、龙鳞=30（伤害加减区）；
     衰败=10、洞察·结算=20、畸变·标记=60（狂暴·标记随道纹删除）
     （勾魂=40 已于 2026-08-30 随【勾魂】改版移除：不再回始扣法力）
     （回始效果循环，现已全部声明化）；帮派令=10（战始遗物段）；
     畸变·结算=10（回终第一循环顶部、凡庸前）；焦黑发丝=10（命零反应第一位）；
  3. 执行路径唯一：伤害相位经 CombatHookManager 上的 MechanismHookAdapter，
     回合/战始/回终相位经 CombatEngine._dispatch_phase，事件机制经 TriggerBus
     （订阅于战斗实例构造时）——旧类/旧 if 已删除。
     注意：洞察状态的【闪避→pending+10】站点（_note_dodge）与（已删除的）狂暴的怪物行动逻辑
     属于其它字面规则，不在迁移范围（机制名带后缀以示区分）。
"""
from __future__ import annotations

import math

from ..combat_events import CombatEventType
from ..models import StatusEffect
from ..rule_engine import (
    DAOWEN_RULES, DamageResolutionContext, RULE_EXECUTOR, rule_matches,
)
from .conditions import (
    all_, any_, damage_type_not, entity_type, has_status, is_alive,
    not_, relic_active,
)
from .registry import MECHANISMS, Mechanism
from .ledger import clear_ledger, ledger_of
from .targets import RANDOM_ALL, SELF, TARGET, roll_pick
from .triggers import Phase, Trigger, TriggerContext
from .verbs import apply_verb


def _rule_context(rule, ctx: TriggerContext) -> DamageResolutionContext:
    amount = ctx.amount
    return DamageResolutionContext(
        event_id=f"mechanism:{rule.rule_id}:{getattr(ctx.target, 'name', '?')}",
        attacker=ctx.source,
        recipient=ctx.target,
        damage_type=ctx.damage_type or "普通",
        original_damage=amount,
        incoming_damage=amount,
        current_damage=amount,
        mode="commit",
    )


def _rule_condition(rule):
    def condition(ctx: TriggerContext) -> bool:
        status = next((item for item in getattr(ctx.target, "status_effects", ())
                       if item.name == rule.status_name and not item.is_expired), None)
        return rule_matches(rule, _rule_context(rule, ctx), status)
    return condition


def _rule_effect(rule):
    def effect(ctx: TriggerContext, targets: list) -> dict:
        context = _rule_context(rule, ctx)
        status = next((item for item in getattr(ctx.target, "status_effects", ())
                       if item.name == rule.status_name and not item.is_expired), None)
        RULE_EXECUTOR.execute(rule, context, status)
        return {"amount": context.current_damage}
    return effect


def _numeric_rule_mechanism(name: str) -> Mechanism:
    definition = DAOWEN_RULES[name]
    rule = definition.rules[0]
    return Mechanism(
        name=name,
        when=Trigger.phase(Phase.INCOMING_ADJUST),
        effect=_rule_effect(rule),
        target=TARGET,
        status_name=rule.status_name,
        condition=_rule_condition(rule),
        priority=rule.priority,
        rule_definition=rule,
    )



# 数值事实与运行时摘要共用 engine.rule_engine.DAOWEN_RULES；这里仅保留
# MechanismHookAdapter 兼容壳，不另写条件、运算或组合配方。
JIAHAI = _numeric_rule_mechanism("加害")
LONGLIN = _numeric_rule_mechanism("龙鳞")
GUZHI = _numeric_rule_mechanism("固执")

def _gangpailing_effect(ctx: TriggerContext, targets: list) -> str:
    """旧 process_relics 帮派令块语义（逐字复刻）：

    player.add_status(StatusEffect("洗劫", 3, 3, "帮派令")) → 洗劫 value=3、
    remaining_rounds=3、source=帮派令；经统一 status 动词（统一状态授予入口），
    返回与旧代码完全相同的日志串。
    """
    apply_verb(ctx.combat, "status", {
        "target": ctx.target,
        "name": "洗劫",
        "duration": 3,   # 旧实现 StatusEffect("洗劫", 3, 3, "帮派令")：回合数=3
        "value": 3,      # 层数=3
        "source": "帮派令",
    })
    return "帮派令：获得洗劫3"




def _jibian_settle_effect(ctx: TriggerContext, targets: list) -> dict:
    """旧 round_end 畸变·结算块语义（逐字复刻）：

    失去 blood_loss = max(0, 攻击力×攻击次数) 点血限（不是伤害）；
    经 blood_limit 动词（统一血限入口：clamp_hp/lethal 默认 True=旧调用同参）；
    即使 blood_loss=0（攻击面板为 0）也照常产生报告条目。
    """
    entity = ctx.target
    blood_loss = max(0, entity.attack_count * entity.attack_power)
    before_limit = entity.blood_limit
    delta = max(0, entity.blood_limit - blood_loss) - entity.blood_limit
    apply_verb(ctx.combat, "blood_limit", {
        "target": entity,
        "delta": delta,
        "source": "畸变",
        "polarity": "debuff",
        "ctx": {
            "timing": "round_end", "source": "畸变", "source_type": "daowen",
            "actor": entity, "target": entity,
            "mechanic": "blood_limit_change", "subtype": "deform",
            "amount": delta, "tags": {"daowen", "round_end"},
        },
        "source_type": "daowen",
        "subtype": "deform",
        "tags": {"daowen", "round_end", "blood_limit_loss"},
    })
    return {
        "type": "deform_blood_limit_loss",
        "entity": entity.name,
        "blood_loss": before_limit - entity.blood_limit,
        "blood_limit_after": entity.blood_limit,
        "hp_after": entity.current_hp,
        "died": not entity.is_alive,
    }


def _has_dongcha_pending(ctx: TriggerContext) -> bool:
    return getattr(ctx.target, "_dongcha_pending", 0) > 0


def _dongcha_effect(ctx: TriggerContext, targets: list) -> dict:
    """旧 round_start 洞察块语义（逐字复刻）：

    待结算法力 pending 经 mana 动词获得（含不朽之躯钳制），随后清零；
    只有 轮回者 且 存活 且有 pending 才触发（与旧条件逐字同义）。
    """
    entity = ctx.target
    pending = getattr(entity, "_dongcha_pending", 0)
    apply_verb(ctx.combat, "mana", {"target": entity, "delta": pending})
    entry = {"type": "dongcha_mana", "entity": entity.name, "gained": pending}
    entity._dongcha_pending = 0
    return entry


DONGCHA = Mechanism(
    name="洞察·结算",
    when=Trigger.phase(Phase.ROUND_START),
    effect=_dongcha_effect,
    target=SELF,
    status_name="洞察",
    condition=all_(
        _has_dongcha_pending,
        entity_type("轮回者", of="self"),
        is_alive(of="self"),
    ),
    # 自愈删除后，洞察回始结算位前移；独立闪避记账规则不变。
    # 注：洞察状态另有【闪避→pending+10】的独立字面规则站点（_note_dodge），
    # 不在本次迁移范围（本机制只迁移回始结算部分，故名为"洞察·结算"）。
    priority=30,
)

def _jibian_marker_effect(ctx: TriggerContext, targets: list) -> dict:
    """旧 round_start 畸变标记块语义（逐字复刻）：纯报告条目，无动词。

    注意与【畸变·结算】的区别：此处 blood_loss 是原始乘积
    （无 max(0, ...) 封底——旧块原文如此），仅作回终结算预告展示。
    """
    entity = ctx.target
    return {
        "type": "deform_pending",
        "entity": entity.name,
        "blood_loss": entity.attack_count * entity.attack_power,
        "note": "回终结算",
    }


JIBIAN_MARKER = Mechanism(
    name="畸变·标记",
    when=Trigger.phase(Phase.ROUND_START),
    effect=_jibian_marker_effect,
    target=SELF,
    status_name="畸变",
    condition=has_status("畸变", of="self"),
    # 旧位置=回始效果循环第六位（最后一位）
    priority=60,
)

def _xijie_passive_effect(ctx: TriggerContext, targets: list) -> None:
    """旧 _apply_hostile_damage_inner 洗劫被动块语义（逐字复刻，承载方式迁移）：

    造成伤害的实体（事件 actor）经 _xijie_steal 夺取受伤目标等量碎片；
    _xijie_steal 内部保留全部既有门闩（damage<=0 / 自伤 / 无洗劫状态 /
    目标无碎片 → 无操作），是夺碎片的唯一实现。

    旧代码写回 detail["xijie_stolen"] 并在 resolve_attack 透传的孤儿诊断
    字段随迁移正式废弃（全仓零消费方，2026-08-19 审计确认）。
    """
    if ctx.source is None or ctx.target is None or ctx.event is None:
        return None
    actual = (ctx.event.data or {}).get("actual_damage", 0)
    ctx.combat._xijie_steal(ctx.source, ctx.target, actual)
    return None


XIJIE_PASSIVE = Mechanism(
    name="洗劫·夺碎片",
    when=Trigger.event(CombatEventType.DAMAGE_APPLIED),
    effect=_xijie_passive_effect,
    target=None,
    status_name="洗劫",
    status_owner="source",
    condition=has_status("洗劫", of="source"),
    # 旧位置=DAMAGE_APPLIED 发出点（伤害管线 emit 之后），事件机制在此同步执行
    priority=10,
)

def _silent_mask_effect(ctx: TriggerContext, targets: list) -> str:
    """【缄默面具】2026-09-17 用户令改版：[战终][法限]+X。

    旧效果为「[战始]获得 20×X 点法力」（X=event_modifiers.silent_mask_x，
    来自事件「献出声音：失忆X」）。属性模型统一后法力=攻击力，[战始]一次性
    给 20X 法力等于同时白送攻击力，强度跳变；且法力每场都会回满，[战始]给蓝
    的边际价值很低。故改为**战终永久成长**：[战终][法限]+X。

    注意：缄默面具的【无法发动附带代价的道纹】是 api.py 的静态校验规则
    （另一字面规则），不在本机制范围，保持不变。

    X=0（未登记 silent_mask_x）时不发放，保持与旧块一致的无操作语义。
    2026-10-23 起固定 +2，不再读 silent_mask_x（遗物无 X 变量）。
    """
    # 2026-10-23 用户令：遗物不得有 X 变量，改为固定 +2。
    # 事件「献出声音」里的失忆X 仍然计入玩家当场支付的代价，但不再驱动本遗物数值。
    x = 2
    player = ctx.target
    if x > 0:
        player.mana_limit += x
    # X=0 时旧块仍会执行钳制（+=0 后无条件 clamp），此处保持逐字等价：
    # 法限提高不连带回血/回蓝，但当前法力不得超过（新）上限——全局钳制规则。
    ctx.combat.clamp_immortal_body(player)
    if x <= 0:
        return "缄默面具：+0法限"
    return f"缄默面具：[法限]+{x}（现为{player.mana_limit}）"


SILENT_MASK = Mechanism(
    name="缄默面具",
    when=Trigger.phase(Phase.BATTLE_END),
    effect=_silent_mask_effect,
    target=SELF,
    condition=relic_active("缄默面具", of="target"),
    # 2026-09-17 改版：由 BATTLE_START(priority=5) 移到 BATTLE_END；
    # 新相位首个注册者，取 10 留出入位空间
    priority=10,
)


def _dragon_claw_effect(ctx: TriggerContext, targets: list) -> str:
    """【龙族利爪】（2026-09-17 用户令改版）：攻击力 = 当前法力×2。

    旧版「初始3点攻击次数与1点攻击力；每完成一次行动后，攻击次数+1、攻击力+2」
    写的是遗留字段 attack_power / attack_count。属性模型统一后
    （攻击力=当前法力、攻击次数=当前速度），而 models.py 的写穿对轮回者不生效，
    故旧版**对轮回者完全无效**——实测遗留字段 6→7 / 20→22，真实攻次与攻力恒定
    6 / 20，整场每次行动都在空转。

    新版改为被动倍率，走状态层，由 models.py::effective_attack_power 读取，
    对轮回者/怪物/朋友/员工同口径生效。
    """
    player = ctx.target
    player.add_status(StatusEffect(
        name="龙族利爪", value=1, remaining_rounds=999, source="龙族利爪"))
    return "龙族利爪：攻击力=当前法力×2"


DRAGON_CLAW = Mechanism(
    name="龙族利爪",
    when=Trigger.phase(Phase.BATTLE_START),
    effect=_dragon_claw_effect,
    target=SELF,
    condition=relic_active("龙族利爪", of="target"),
    # 战始遗物段，排在缄默面具(5)之前无依赖，取 4
    priority=4,
)

GANGPAILING = Mechanism(
    name="帮派令",
    when=Trigger.phase(Phase.BATTLE_START),
    effect=_gangpailing_effect,
    target=SELF,
    condition=relic_active("帮派令", of="target"),
    # 旧位置=战始遗物段（缄默面具之后、负岳索之前）；后续战始遗物机制按 20/30/... 递增
    priority=10,
)

# 2026-10-11 用户令删除【衰败】道纹 → 其 ROUND_START 机制（priority 20）一并删除。
# 回始相位机制现为：洞察·结算(30) → 畸变·标记(60)。

def _jiaohheifasi_effect(ctx: TriggerContext, targets: list) -> None:
    """旧 _on_entity_death 焦黑发丝块语义（逐字复刻）：

    怪物命零 → 玩家速度 +2，经 speed 动词 → _gain_speed（加速等既有语义原样）；
    速度事件 ctx 挂在死亡事件下（timing/parent_event_id 取自死亡事件 ctx）。
    """
    death_ctx = (ctx.event.ctx or {}) if ctx.event is not None else {}
    player = ctx.resolve("player")
    apply_verb(ctx.combat, "speed", {
        "target": player,
        "delta": 2,
        "ctx": {
            "timing": death_ctx.get("timing", ""),
            "source": "焦黑发丝", "source_type": "relic",
            "actor": player, "target": player, "owner": player,
            "mechanic": "speed_change", "subtype": "current_speed", "amount": 2,
            "tags": {"relic", "death_trigger"},
            "parent_event_id": death_ctx.get("event_id"),
        },
    })
    return None


JIBIAN_SETTLE = Mechanism(
    name="畸变·结算",
    when=Trigger.phase(Phase.ROUND_END),
    effect=_jibian_settle_effect,
    target=SELF,
    status_name="畸变",
    condition=all_(
        has_status("畸变", of="self"),
        is_alive(of="self"),
    ),
    # 旧位置=回终第一逐实体循环顶部（凡庸 tick 之前）；后续回终机制按 20/30/... 递增
    priority=10,
)

JIAOHHEIFASI = Mechanism(
    name="焦黑发丝",
    when=Trigger.event(CombatEventType.ENTITY_DIED),
    effect=_jiaohheifasi_effect,
    target=None,   # 无目标列表：效果对象是玩家，经 ctx.resolve("player") 获取
    condition=all_(
        entity_type("怪物", of="target"),
        relic_active("焦黑发丝", of="player"),   # 玩家持有且未封印（抵扣X）
    ),
    # 旧位置=_on_entity_death 内 ENTITY_DIED 发出点（招魂/分裂之前）；命零反应第一位
    priority=10,
)

# ==========================================================================
# 罪孽都市·账本三机制（2026-10-03 迁移）
# 旧实现：combat.py 回始效果循环后的内嵌 for（2026-10-03 迁移时为逼债→清算→赌命三段；
# 2026-10-13 用户令删【逼债】后剩清算→赌命两段）；2026-10-14 原道纹【赌命】改为遗物【灾厄轮盘】，
# 仍复用该回始锚点但不再由道纹状态或道纹序列驱动。清算对账仍在回终「状态消失即清账」块。
# 共用一份账本存储（engine/mechanisms/ledger.py）与一个碎片动词（shards）。
# 结算顺序服从各状态来源道纹在施法者道纹序列中的位置；
# priority 仅作为缺少序列锚点时的后备。
# ==========================================================================

def _qingsuan_effect(ctx: TriggerContext, targets: list) -> dict | None:
    """【清算·结算】[回始]目标失去[施法者当前碎片]点格挡（每回始重读施法者碎片）。"""
    entity = ctx.target
    entries = list(ledger_of(entity, "清算"))
    if not entries:
        return None
    results = []
    for entry in entries:
        caster = entry.get("caster")
        if caster is None:
            continue
        drain = max(0, ctx.combat._shards_of(caster))
        lost = min(entity.shield, drain)
        entity.shield -= lost
        results.append({"type": "qingsuan", "entity": entity.name,
                        "lost_shield": lost, "drain": drain})
    return results or None


def _duming_effect(ctx: TriggerContext, targets: list) -> dict | None:
    """遗物【灾厄轮盘】（原道纹【赌命】）的[回始]被动：按场上存活角色（玩家侧在前）投随机数，
    对应目标失去30%当前生命；持有者不在场时不会触发。

    随机目标经 targets.roll_pick（DiceEngine，池名含回合号 → 可复现）；一次调用只投一次，
    roll/of 直接进战报。伤害走 hp_loss 动词 = 旧 `_raw_hp_loss`（绕过格挡、计入失血追踪、
    含命零判定与「失去生命」反应窗口）。
    """
    holder = ctx.target
    state = ctx.state or getattr(ctx.combat, "state", None)
    stream = f"赌命_r{state.current_round}"
    picked = ctx.picks.get(stream)      # RANDOM_ALL 选择器已投过，这里只读结果
    if picked is None:
        return None
    tgt = picked["entity"]
    d = math.ceil(tgt.current_hp * 30 / 100)   # 2026-09-28 用户口径：失去30%**当前**生命
    rd = apply_verb(ctx.combat, "hp_loss", {
        "target": tgt, "amount": d,
        "ctx": {"timing": "round_start", "source": "灾厄轮盘", "source_type": "relic",
                "actor": holder, "target": tgt, "mechanic": "hp_loss", "subtype": "percent",
                "amount": d, "tags": {"relic", "round_start"}},
    })
    return {"type": "duming", "caster": holder.name, "target": tgt.name,
            "roll": picked["roll"], "of": picked["of"], "damage": rd["lost"], **rd}


# 2026-10-13 用户令删【逼债】：BIZHAI_SETTLE（【逼债·结算】）一并移除。

QINGSUAN_SETTLE = Mechanism(
    name="清算·结算",
    when=Trigger.phase(Phase.ROUND_START_SETTLE),
    effect=_qingsuan_effect,
    target=SELF,
    status_name="清算",
    condition=None,  # 旧实现按“是否有账”判定，账在状态在；无账即无报告条目
    priority=20,
)

DUMING_SETTLE = Mechanism(
    name="灾厄轮盘·结算",
    when=Trigger.phase(Phase.ROUND_START_SETTLE),
    effect=_duming_effect,
    target=RANDOM_ALL,   # 持有遗物者触发一次；双方存活角色中随机取目标。
    status_name="",     # 遗物常驻被动，不挂状态，也不受道纹序列排序。
    condition=all_(relic_active("灾厄轮盘", of="target"),
                   entity_type("轮回者", of="target"), is_alive(of="target")),
    priority=30,
)


def _qingsuan_reconcile_effect(ctx: TriggerContext, targets: list) -> None:
    """【清算·对账】持续X到期（状态被 tick 掉）后清账，防止多结算一轮。"""
    if not ctx.target.has_status("清算"):
        clear_ledger(ctx.target, "清算")
    return None


# 2026-10-13 用户令删【逼债】：BIZHAI_RECONCILE（【逼债·对账】）一并移除。

QINGSUAN_RECONCILE = Mechanism(
    name="清算·对账",
    when=Trigger.phase(Phase.ROUND_END_RECONCILE),
    effect=_qingsuan_reconcile_effect,
    target=SELF,
    status_name="清算",
    condition=None,
    priority=20,
)


MECHANISMS.register(JIAHAI)
MECHANISMS.register(LONGLIN)
MECHANISMS.register(GUZHI)
MECHANISMS.register(GANGPAILING)
MECHANISMS.register(JIBIAN_SETTLE)
MECHANISMS.register(JIAOHHEIFASI)
MECHANISMS.register(DONGCHA)
MECHANISMS.register(JIBIAN_MARKER)
MECHANISMS.register(XIJIE_PASSIVE)
MECHANISMS.register(SILENT_MASK)
MECHANISMS.register(DRAGON_CLAW)
MECHANISMS.register(QINGSUAN_SETTLE)
MECHANISMS.register(DUMING_SETTLE)
MECHANISMS.register(QINGSUAN_RECONCILE)


# 2026-10-13：挂起机制的原位迁移。后段锚点不能合并到已有的前段相位。
from ..effect_context import make_context
from .conditions import entity_has_relic, player_run_relic_active, side_has


def _blood_lineage_effect(ctx: TriggerContext, targets: list) -> dict:
    entity = ctx.target
    if entity.damage_dealt_this_round > 0:
        detail = apply_verb(ctx.combat, "heal", {"target": entity, "amount": entity.damage_dealt_this_round}, {
            "timing": "round_end", "source": "血族血脉", "source_type": "relic",
            "actor": entity, "target": entity, "owner": entity,
            "mechanic": "heal", "subtype": "blood_lineage", "amount": entity.damage_dealt_this_round,
            "tags": {"relic", "round_end"}})
        return {"type": "blood_lineage_heal", "entity": entity.name,
                "amount": detail["actual_heal"], "heal_ctx": detail.get("heal_ctx")}
    # 2026-10-13 用户裁定：自动流血付不起照扣，正常命零，不报错中断回终。
    payment = apply_verb(ctx.combat, "cost", {
        "payer": entity, "cost_type": "流血", "amount": 20, "forced": True,
        "cost_share_target_ref": (ctx.picks.get("blood_lineage_cost_share_target_ref", "")
                                  if entity is ctx.state.player else ""),
        "cost_context": {"timing": "round_end", "source": "血族血脉", "source_type": "relic", "tags": {"automatic"}}})
    return {"type": "blood_lineage_bleed", "entity": entity.name,
            "cost": payment, "amount": payment["actual_paid"]}


def _red_curse_effect(ctx: TriggerContext, targets: list) -> dict:
    entity = ctx.target
    curse = make_context(timing="round_end", source="赤族诅咒", source_type="bloodline",
                         actor=entity, target=entity, owner=entity,
                         mechanic="cost", subtype="bleed", amount=20,
                         tags={"bloodline", "round_end", "automatic"})
    detail = apply_verb(ctx.combat, "direct_damage", {"target": entity, "amount": 20, "damage_type": "代价"})
    loss = ctx.combat._record_hp_loss_event(entity, detail.get("actual_damage", 0), curse, subtype="cost")
    if detail.get("died"):
        ctx.combat._check_hp_zero_death(entity, ctx=loss or curse)
    return {"type": "chizu_curse_bleed", "entity": entity.name,
            "amount": detail["actual_damage"], "died": detail["died"]}


def _first_round_shield_effect(ctx: TriggerContext, targets: list):
    if ctx.state.current_round != 0:
        return None
    shield = apply_verb(ctx.combat, "mapping", {
        "mapping": ctx.state.event_modifiers, "key": "next_battle_first_round_shield", "op": "pop"})
    if shield:
        apply_verb(ctx.combat, "shield", {"target": ctx.target, "amount": shield})
        return {"type": "event_first_round_shield", "amount": shield}


def _leather_shield_effect(ctx: TriggerContext, targets: list):
    # 即使皮衣已丢失也照旧 pop，不能留下过期的下一回合奖励。
    shield = apply_verb(ctx.combat, "mapping", {
        "mapping": ctx.state.event_modifiers, "key": "leather_shield_next", "op": "pop"})
    if shield and ctx.state.side_has(ctx.target, "皮衣"):
        apply_verb(ctx.combat, "shield", {"target": ctx.target, "amount": shield})
        return {"type": "leather_shield", "amount": shield}


def _leather_record_effect(ctx: TriggerContext, targets: list):
    events = getattr(ctx.target, "_hp_loss_events", []) or []
    event_loss = sum(int(e.get("amount") or 0) for e in events)
    loss = event_loss if event_loss > 0 else ctx.target.hp_lost_this_round
    if loss > 0:
        apply_verb(ctx.combat, "mapping", {
            "mapping": ctx.state.event_modifiers, "key": "leather_shield_next", "op": "set", "value": loss})


def _round_tracking_reset_effect(ctx: TriggerContext, targets: list):
    apply_verb(ctx.combat, "reset_fields", {"target": ctx.target, "fields": {
        "hp_lost_this_round": 0, "_hp_loss_events": [], "_speed_change_events": [], "_blood_limit_events": []}})


def _bulletproof_effect(ctx: TriggerContext, targets: list) -> str:
    apply_verb(ctx.combat, "shield", {"target": ctx.target, "amount": 15})
    return f"防弹插板：{ctx.target.name}+15格挡"


def _player_subject(ctx: TriggerContext) -> bool:
    return ctx.target is ctx.state.player and ctx.target is not None


def _ember_wing_effect(ctx: TriggerContext, targets: list) -> str:
    """【烬翼】2026-10-23 重做为被动：回始时生命不高于血限一半，自动获得飞行1回合。"""
    entity = ctx.target
    if entity is None or entity.current_hp * 2 > entity.blood_limit:
        return ""
    apply_verb(ctx.combat, "status", {
        "target": entity, "name": "飞行", "duration": 1, "value": 1,
        "source": "烬翼"})
    return f"烬翼：{entity.name}获得【飞行】1回合"


EMBER_WING = Mechanism(
    name="烬翼·回始飞行", when=Trigger.phase(Phase.ROUND_START), target=SELF,
    condition=all_(_player_subject, side_has("烬翼")), effect=_ember_wing_effect, priority=25)

BLOOD_LINEAGE = Mechanism(
    name="血族血脉·回终", when=Trigger.phase(Phase.ROUND_END_POST_MEDIOCRITY), target=SELF,
    condition=all_(entity_type("轮回者"), side_has("血族血脉")), effect=_blood_lineage_effect, priority=10)
RED_CURSE = Mechanism(
    name="赤族诅咒", when=Trigger.phase(Phase.ROUND_END_POST_MEDIOCRITY), target=SELF,
    condition=all_(entity_type("赤族"), is_alive()), effect=_red_curse_effect, priority=20)
FIRST_ROUND_SHIELD = Mechanism(
    name="事件·首回合格挡", when=Trigger.phase(Phase.ROUND_START_PREPARE), target=SELF,
    condition=_player_subject, effect=_first_round_shield_effect, priority=5)
LEATHER_SHIELD = Mechanism(
    name="皮衣·获得格挡", when=Trigger.phase(Phase.ROUND_START_PREPARE), target=SELF,
    condition=_player_subject, effect=_leather_shield_effect, priority=10)
LEATHER_RECORD = Mechanism(
    name="皮衣·记录", when=Trigger.phase(Phase.ROUND_END_TRACKING), target=SELF,
    condition=all_(_player_subject, side_has("皮衣")), effect=_leather_record_effect, priority=10)
ROUND_TRACKING_RESET = Mechanism(
    name="回终·计账清理", when=Trigger.phase(Phase.ROUND_END_TRACKING), target=SELF,
    effect=_round_tracking_reset_effect, priority=20)
BULLETPROOF = Mechanism(
    name="防弹插板", when=Trigger.phase(Phase.BATTLE_START_AFTER_CHOICES), target=SELF,
    condition=all_(is_alive(), entity_has_relic("防弹插板")), effect=_bulletproof_effect, priority=10)
DRAGON_BLOODLINE = Mechanism(
    name="龙族血脉·乘区", when=Trigger.phase(Phase.MULTIPLIER_ADJUST), target=TARGET,
    condition=all_(side_has("龙族血脉", of="source"), not_(entity_type("怪物")), damage_type_not("代价")),
    effect=lambda ctx, targets: {"amount": ctx.amount * 2}, priority=10)
# 2026-10-13 用户令：【三相残韵盘】遗物删除，SANXIANG_END（【三相残韵盘·战终】）一并移除。

for _mechanism in (BLOOD_LINEAGE, RED_CURSE, FIRST_ROUND_SHIELD, LEATHER_SHIELD,
                   LEATHER_RECORD, ROUND_TRACKING_RESET, BULLETPROOF, DRAGON_BLOODLINE,
                   EMBER_WING):
    MECHANISMS.register(_mechanism)
