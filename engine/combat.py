"""
战斗计算引擎
负责战斗中的数值对撞、回合推进、伤害结算
所有数值计算在此完成，AI禁止自行计算
"""
from __future__ import annotations
import math
import weakref
from typing import Optional, Any
from .models import Entity, StatusEffect, GameState, DaoWenInstance, DaoWen, Spell, Consumable
from .daowen import DaoWenEngine, ResonanceEngine
from .dice import DiceEngine
from .enums import (ActionPhase, TriggerTiming, InterruptType, DamageType,
                    EffectScope, EffectPolarity, CostType)
from .dm_rulings import Interrupt
from .combat_events import CombatEvent, CombatEventType, register_combat_event_observer
from .combat_hooks import CombatHookManager
from .effect_context import EffectContext, make_context, normalize_context
from .mechanisms import MECHANISMS, Phase, TriggerBus, TriggerContext, apply_verb
from .personality import remove_personality
from .resolution import KIND_EFFECT, ResolutionContext, resolution_frame
# 常量定义在 models（授予点在 Entity.__post_init__），此处只读取以判定效果。
from .models import MONSTER_MANA_RELIC

# 【凡庸】连续无所作为的回合阈值：连续 N 回合未出手、或连续 N 回合未能使敌对角色
# 生命减少 → 凭空全身炸裂。这是**规则层**的反乌龟机制，必须优先于 sim 层的死锁
# 防护（后者只是防卡死的程序兜底，不得抢在规则之前结束战斗，更不得擅定胜负）。
# sim/duel_pvp.py 直接导入本常量推导兜底阈值，避免两处硬编码各自漂移。
# 阈值唯一事实源在 Entity（engine/models.py::Entity.MEDIOCRITY_ROUNDS）。
MEDIOCRITY_ROUNDS = Entity.MEDIOCRITY_ROUNDS


# 实现分片：方法体在 engine/combat_parts/*.py，本文件保留门面与核心结算
# （__init__ / 伤害与数值管线 / resolve_attack / 回合管理 / 死者之页 /
#  遗物结算 / 凡庸与目标合法性）。拆分不改变行为与对外契约：CombatEngine 仍是
# 唯一入口，全部引用方（含 self.combat.X 与 CombatEngine.X）经 MRO 解析不变。
# 新增分片时请同步 engine/validator.py 的机制护栏扫描范围。
from .combat_parts.cost_payment import CostPaymentMixin
from .combat_parts.damage_death import DamageDeathMixin
from .combat_parts.daowen_effect import DaowenEffectMixin
from .combat_parts.monster_life import MonsterLifeMixin
from .combat_parts.monster_phase import MonsterPhaseMixin
from .combat_parts.spells import SpellReactionMixin


class CombatEngine(DamageDeathMixin, CostPaymentMixin, MonsterLifeMixin,
                   DaowenEffectMixin, SpellReactionMixin, MonsterPhaseMixin):
    """战斗计算引擎（门面；实现见 engine/combat_parts/*.py）"""
    
    # 副本专属道纹
    REGION_EXCLUSIVE_DAOWEN = {
        "扭曲都市": {"变形","定型","超频","坏死"},  # 【畸变】【退化】已遗物化
        "罪孽都市": {"点金","典当","豪夺","清算","抢劫","假钞","消灾","勒索"},   # 2026-10-13 删【逼债】【典当】顶替；2026-10-14 +【勒索】
        "龙心谷":   {"加害","龙鳞","裂变","背负"},  # 【逆鳞】【伤痕】已遗物化
    }
    
    # 原始怪物道纹（道纹归属规则：各组起点）——【原初X】可借用范围
    ORIGINAL_MONSTER_DAOWEN = ("全力", "必中")  # 2026-10-25 用户令：【疯狂】【减速】遗物化
    # 原始怪物道纹每次实际发动时支付异变5X（X 恒为面板/借用时写定的值；
    # 2026-09-16 用户令：道纹递增机制已废止）；效果持续期间（未再次发动）不再重复计费。
    # 必中为次数型（下X次选择[目标]无法闪避），余数记在 entity._bizhong_left。
    YUANCHU_COST_RATE = 5
    # 波及X（2026-08-21）：你发动的道纹同时作用于所有拥有波及效果的目标。
    # 数值键：效果的总数值在所有目标（本次[目标]+波及目标，均排除施法者自身）
    # 之间平均分配；无法整除时余数按随机数分配。多目标不会复制或增加总数值。
    # 状态类效果（减速减半/固定面板/持续状态等）对波及目标原样生效，不入下表。
    WAVE_NUMERIC_KEYS = (
        "target_damage", "total_damage", "hits", "aoe_damage", "hp_percent_loss",
        "target_heal", "heal_percent", "mutation_reduction",
        "target_shield", "shield_drain",
        "blood_limit_reduction", "hp_reduction", "blood_limit_increase", "blood_limit_penalty",
        "zengzhi_battle_end",  # 2026-10-14：【增殖】重做，战终血限增长参与波及平分
        "attack_boost", "attack_reduction",
        "speed_boost", "speed_penalty",
        "self_attack_count", "invalid_damage_hits",
    )
    
    def __init__(self, state: GameState, dice: DiceEngine):
        self.state = state
        self.dice = dice
        self.combat_log: list[dict] = []  # 完整战斗日志
        self.hook_manager = CombatHookManager()
        self.mechanism_bus = TriggerBus()
        for mechanism in MECHANISMS.event_mechanisms():
            self.mechanism_bus.register(mechanism)
        # 唯一事件分发点：本引擎作为 GameState 的事件观察者，所有经
        # state.emit_combat_event 发出的事件（_emit 的三种 + apply_heal 的
        # HEAL_APPLIED）统一进入 TriggerBus，杜绝双发路径。
        # 观察者是可 pickle 的模块级类实例（持引擎 id，经弱引用表解析），
        # 不会把引擎带进存档/快照（见 combat_events.py 注释）。
        register_combat_event_observer(self.state, self)
        # 残韵改写：entity_id → {源道纹: 变化后道纹}，只改下一次发动结算，不改持有
        self._resonance_rewrites: dict[int, dict[str, str]] = {}
        # 效果链深度保险丝（见 MAX_EFFECT_CHAIN_DEPTH / engine/resolution.py）。
        # 2026-09-20：不再是独立计数器——ResolutionContext 是唯一事实源
        # （下面的 self.resolution 创建之后，`_effect_chain_depth` 只是它的视图）。
        # AFTER_LIFE_LOST(失去生命后) 反应法术在“非攻击失血”路径的自动触发状态：
        #   _resolving_life_lost_reactions > 0 表示正在结算某次反应法术——反应
        #     自身引发的失血不得再次触发反应（否则以牙还牙/连击会互相连锁死循环）。
        #   _attack_after_window_target 是当前攻击中、其失血由 resolve_attack 的
        #     反应窗口结算的目标；该目标失血不再重复走 hook（避免与窗口双发）。
        self._resolving_life_lost_reactions = 0
        self._attack_after_window_target = None
        # 「失去生命后」统一拦截（2026-08-30）：
        #   _hp_loss_recording>0 表示当前正由既有降血入口（_record_hp_loss_event /
        #     _apply_blood_limit_change / _apply_numeric_cost_part / 血限压迫等）接管，
        #     Entity.__setattr__ 的兜底钩子被抑制，避免与既有触发点双发。
        #   _hp_loss_ctx 是当前一次降血的来源上下文，供兜底钩子还原 attacker。
        self._hp_loss_recording = 0
        self._hp_loss_ctx = None
        # 把本引擎绑定到已有战斗实体上：之后任何 current_hp 下降都会经
        # Entity._fire_hp_loss → _on_entity_hp_fallen 上报（仅对引擎自有的实体生效）。
        self._bind_existing_hp_hooks()
        # 怪物战斗记述的实例化（2026-08-23）：这三个集合原本是类属性，
        # 仅靠 reset_monster_activation 在战始降级为实例属性——绕过战始的引擎
        # （测试夹具/模拟器直驱）会把 add() 写进跨实例共享的类集合，且 id()
        # 复用会让后建的怪物“被已进化”，发生跨用例/跨局串扰。
        self._monster_activated: dict = {}
        self._monster_evolved: set = set()
        self._monster_daowen_round_used: dict = {}
        # 效果结算生命周期（见 engine/resolution.py）：深度/预算保险丝 + trace。
        # 每个引擎一份；预演沙盒会在进出时保存/恢复它（engine/sandbox.py）。
        self.resolution = ResolutionContext()

    # 效果链深度上限。这是**防御性保险丝**，不是游戏规则：
    # 任何合法的 背负 重定向链都远低于此值（重定向每跳都会递减 _beifu_left，
    # 本身就收敛；【嫁祸】2026-10-11 已删除）。设成这么大是为了保证它永远不会改变任何现有战斗结果，
    # 只在真的出现 A→B→A→B 死循环时把它截断成一次可诊断的异常。
    # 2026-09-20：计数器迁到 ResolutionContext（engine/resolution.py），
    # 本常量作为兼容别名保留（同一数值）。
    MAX_EFFECT_CHAIN_DEPTH = ResolutionContext.MAX_DEPTH

    @property
    def _effect_chain_depth(self) -> int:
        """兼容别名（2026-09-20 起为 ResolutionContext.depth 的只读视图）。

        历史上这是一个实例属性，被 sim/win_only_ai.py 的沙盒保存/恢复、
        tests/test_effect_chain_audit.py 的保险丝用例直接读写。语义保持不变：
        进入结算 +1、离开 -1、越界抛 RecursionError（现在由 context 抛
        ResolutionDepthError，它是 RecursionError 的子类）。
        """
        return self.resolution.depth

    @_effect_chain_depth.setter
    def _effect_chain_depth(self, value: int) -> None:
        self.resolution._depth = int(value)

    @property
    def event_stream(self) -> list[CombatEvent]:
        """战斗事件流（“发生了什么”）。事实源在 state 上，随存档一起走。"""
        return self.state.combat_events

    def _emit(self, event_type: CombatEventType, *, actor=None, target=None,
              ctx: Optional[EffectContext | dict] = None, **data) -> CombatEvent:
        """登记一条 CombatEvent。ctx 只作为来源快照附带，不参与任何判定。

        机制分发由 GameState.emit_combat_event 内的事件观察者统一完成
        （见 __init__ 的 register_combat_event_observer）——本方法不再单独分发，
        避免同一事件被分发两次。
        """
        if isinstance(ctx, EffectContext):
            ctx = ctx.to_dict()
        return self.state.emit_combat_event(
            event_type, actor=actor, target=target, ctx=ctx, **data)

    def _dispatch_phase(self, phase: str, *, target=None, source=None,
                        amount: int = 0, damage_type: str = "", picks: Optional[dict] = None) -> list:
        """宣布一个管线相位时点（机制系统，通用分发，不含任何机制判断）。

        同一实体/相位的持续状态机制按「发动 X 降序、同 X 施加先后」执行；
        无状态锚点的遗物/系统机制才使用静态 priority 后备。自然相位本身并不
        倒流。返回值是各机制的报告条目（由调用点并入 effects/战报）。

        调用约定：管线只负责"在既有语义位置上宣布时点"，具体机制逻辑
        全部在声明层（engine/mechanisms/）。当前接线相位：
        INCOMING_ADJUST（Hook 路径）与 ROUND_START（本方法）。
        """
        results = []
        mechanisms = MECHANISMS.phase_mechanisms(phase)
        for mechanism in sorted(mechanisms, key=lambda item: item.ordering_key(target)):
            ctx = TriggerContext(combat=self, state=self.state, phase=phase,
                                 target=target, source=source,
                                 amount=amount, damage_type=damage_type, picks=dict(picks or {}))
            if mechanism.condition is not None and not mechanism.condition(ctx):
                continue
            targets = mechanism.target.select(ctx) if mechanism.target is not None else []
            result = mechanism.effect(ctx, targets)
            if isinstance(result, list):
                results.extend(result)
            elif result is not None:
                results.append(result)
        return results

    def _dispatch_phase_all(self, phase: str, entities: list) -> list:
        """全场相位分发：同一待结算窗口按统一状态排序，而非旧机制优先循环。

        自然时机仍由调用点锚定（例如回始·全场结算）；进入该窗口后，带状态
        锚点的效果不论来自哪个实体均按发动 X、同 X 施加先后排列。遗物、账本等
        无状态锚点才保留 priority/实体列表顺序作为稳定后备。
        """
        results = []
        # 这里的实体×机制对会在同一个全场自然窗口内待结算；扁平化后才能让
        # 不同实体上的持续状态也遵守同一套 X/施加先后规则，而不是被旧的
        # 「一个机制跑完整个全场」循环人为隔开。没有状态锚点的账本/遗物仍按
        # 原 priority，再按实体列表顺序稳定落位。
        pending = [
            (mechanism, entity, entity_index)
            for mechanism in MECHANISMS.phase_mechanisms(phase)
            for entity_index, entity in enumerate(entities)
        ]
        pending.sort(key=lambda row: (*row[0].ordering_key(row[1]), row[2]))
        for mechanism, entity, _ in pending:
            ctx = TriggerContext(combat=self, state=self.state, phase=phase, target=entity)
            if mechanism.condition is not None and not mechanism.condition(ctx):
                continue
            targets = mechanism.target.select(ctx) if mechanism.target is not None else []
            result = mechanism.effect(ctx, targets)
            if result is None:
                continue
            if isinstance(result, list):
                results.extend(result)     # 一次结算多笔账目 → 多条报告条目
            else:
                results.append(result)
        return results

    def _battle_delta(self, entity: Entity, field_name: str, delta: int,
                      source: str, polarity: str) -> int:
        """战斗中未注明永久且不是代价/伤害的面板变化统一登记为局内效果。"""
        return self.state.apply_scoped_delta(
            entity, field_name, delta,
            scope=EffectScope.BATTLE.value,
            polarity=polarity,
            source=source,
        )

    # ========== 伤害计算 ==========
    
    def calculate_attack_damage(
        self, 
        attacker: Entity, 
        target: Entity,
        hit_index: int = 0,
        is_must_hit: bool = False
    ) -> dict:
        """
        计算单次攻击伤害
        规则：
        - 闪避：消耗1点当前速度完全闪避
        - 必中：无法闪避
        - 格挡：仅抵消外部伤害，不抵消代价
        - 伤害类型决定是否被格挡
        """
        result = {
            "attacker": attacker.name,
            "target": target.name,
            "hit_index": hit_index,
            "attack_power": attacker.attack_power,
            "is_must_hit": is_must_hit,
            # 2026-10-12 用户令：目标带【必中】debuff 时闪避/招架都不开放，
            # prepare 侧如实回报，免得推演端提交一个注定失败的回合外行动。
            "out_of_turn_blocked": self.out_of_turn_blocked(target),
            "can_dodge": (not is_must_hit and not self.out_of_turn_blocked(target)
                          and target.current_speed >= 1),
            "dodge_available": target.current_speed >= 1,
        }
        
        return result
    
    def _is_flying(self, entity: Entity) -> bool:
        return bool(getattr(entity, "is_flying", False)
                    or entity.has_status("飞行"))

    def _wanjunyin_holder(self, target: Entity) -> bool:
        """己方被攻击角色是否有剩余耐久的【万钧印】消耗品。"""
        if target is None or not self.state.on_player_side(target):
            return False
        return any(item.name == "万钧印" and not item.is_depleted
                   for item in self.state.consumables)

    def _incoming_adjust(self, target: Entity, amount: int, damage_type: str = "普通") -> int:
        if amount <= 0 or damage_type == "代价" or target is None:
            return amount
        return self.hook_manager.apply_incoming_adjust(
            target, amount, damage_type, None, self.state)

    def _record_speed_change_event(
        self, entity: Entity, amount: int,
        ctx: Optional[EffectContext | dict] = None,
        *, field: str = "current_speed",
    ) -> Optional[dict]:
        if entity is None or amount == 0:
            return None
        parent = normalize_context(ctx)
        if parent is not None and parent.mechanic == "speed_change":
            speed_ctx = parent
        else:
            speed_ctx = make_context(
                timing=parent.timing if parent else self._current_context_timing(),
                source=parent.source if parent else "legacy_speed_change",
                source_type=parent.source_type if parent else "legacy",
                actor=parent.actor if parent else entity,
                target=entity,
                owner=parent.owner if parent else None,
                mechanic="speed_change",
                subtype=field,
                amount=amount,
                tags=(set(parent.tags) if parent else {"legacy_context"}),
                parent_event_id=parent.event_id if parent else None,
            )
        record = speed_ctx.to_dict()
        events = getattr(entity, "_speed_change_events", None)
        if events is None:
            entity._speed_change_events = []
            events = entity._speed_change_events
        events.append(record)
        return record

    def _gain_speed(
        self, entity: Entity, amount: int,
        ctx: Optional[EffectContext | dict] = None,
    ) -> int:
        if amount <= 0:
            return 0
        if entity.has_status("加速"):
            amount *= 2
        # 倍速齿轮：遗物只监听“实际获得速度”入口；与道纹【加速】可叠乘。
        if self._relic_active(entity, "倍速齿轮"):
            amount *= 2
        before = entity.current_speed
        entity.current_speed += amount
        self.clamp_immortal_body(entity)
        gained = entity.current_speed - before
        self._record_speed_change_event(entity, gained, ctx, field="current_speed")
        return gained

    def _relic_active(self, entity: Entity, name: str) -> bool:
        """该实体身上的遗物【name】此刻是否生效（通用的封存判定，18 处调用）。

        注意：这是**通用助手**，不是某件遗物的专用逻辑。2026-10-11 摘除
        「苍白之花 → 回锋刀」战始联动时曾把它一并删掉，导致活血衣/增生药剂等
        全部走这条判定的遗物同时失效（628 个测试炸），故恢复。
        删联动要删的是**调用点**，不是这个共享判定本身。

        判定口径与 `GameState._relic_active` 完全一致（含 2026-10-22 启用
        归类：enabled=False 的遗物不生效），故直接委托状态层，不在此复制。
        """
        return self.state._relic_active(entity, name)

    def _any_active_relic(self, name: str) -> bool:
        """查询场上是否存在未封印的该遗物持有者，用于“任意角色/所有角色”的全场效果。"""
        entities = self.state.get_all_player_side() + self.state.get_all_enemy_side()
        seen: set[int] = set()
        for entity in entities:
            if id(entity) in seen:
                continue
            seen.add(id(entity))
            if self._relic_active(entity, name):
                return True
        return False

    def out_of_turn_blocked(self, entity) -> bool:
        """该角色是否被禁止「回合外行动」（唯一闸门，2026-10-12 用户令）。

        来源是道纹【必中】X：「你选中的目标无法进行回合外行动，持续X」。
        回合外行动 = 在自己回合之外做出的任何响应，当前含：
          · 闪避（消耗 1 点当前速度完全闪避本次判定）
          · 招架（逐击提交 parry，免疫攻击伤害）
          · 反应型法术（受到伤害前 / 失去生命后 / 目标发动道纹前 / 闪避时 …）
        **未来新增的回合外行动必须走这道闸**，不要在各自分支里另写一份判断，
        否则【必中】会漏管——这正是把它抽成方法的原因（同 mana_overflow_exempt）。

        只认真正的持续型【必中】状态（remaining_rounds > 0，即道纹必中X 挂上去的
        debuff）。`grant_bizhong` 留下的次数型状态 remaining_rounds == -1（仅作显示，
        必中与否看 _bizhong_left 次数），那是遗物/怪物旧口径，不在此列。
        """
        if entity is None:
            return False
        st = next((s for s in entity.status_effects if s.name == "必中"), None)
        return bool(st and (st.remaining_rounds or 0) > 0)

    def mana_overflow_exempt(self, entity) -> bool:
        """当前法力是否允许超出[法限]（唯一豁免：遗物【苍白之花】）。

        2026-10-11 用户令，`物品索引.md:110`「你的当前法力可以超出[法限]。」。
        只豁免**当前法力**这一项；当前速度≤[速限] 对持有者同样成立。
        当前生命≤[血限] 另有自己的豁免（2026-10-14【猩红果实】，见
        `GameState.hp_overflow_exempt`），不在本方法内。
        因为 [攻击力]=当前法力，溢出直接抬高普攻输出——这就是该遗物的全部效果。
        遗物被封印时豁免同时消失（sealed_relics 计数 > 0）。
        抽成方法是为了让代价路径（`cost_payment.py` 的【枯竭】）读同一条判定，
        避免两处各写一份而漂移。
        """
        if entity is not self.state.player:
            return False
        return self._relic_active(entity, "苍白之花")

    def clamp_immortal_body(self, entity: Entity) -> None:
        """任何属性都不得超过其上限（用户裁定 2026-09-13，全局化）。

        原先这条只在持有【不朽之躯】时生效，其余情况允许当前法力/速度超池。
        现改为**无条件对所有角色生效**：当前生命≤[血限]、当前法力≤[法限]、
        当前速度≤[速限]。理由是上限不再只是"初始值"，它同时定义了攻次(速度)
        与攻力(法力)，超池等于凭空突破面板；【不朽之躯】的原文效果因此成为
        通用规则的一部分（该遗物本身保留，不再独占此项）。

        只限制"获得"的当前值，不动上限本身；属性点（修行/无所求）提升的是
        上限，不受本限制。方法名保留兼容既有 12 处调用点与测试。
        """
        if entity is None:
            return
        if not self.mana_overflow_exempt(entity):
            entity.current_mana = min(entity.current_mana, entity.mana_limit)
        entity.current_speed = min(entity.current_speed, entity.speed_limit)
        # 2026-10-14：【猩红果实】持有者当前生命可超出[血限]，与法力侧镜像。
        if not self.state.hp_overflow_exempt(entity):
            entity.current_hp = min(entity.current_hp, entity.blood_limit)

    def _note_dodge(self, entity: Entity, relic_target_ref: Optional[str] = None) -> dict:
        """闪避专属句：正文写「每次闪避后」的效果。失速/归零不在这里。"""
        extra = {}
        if self._relic_active(entity, "避风铃"):
            entity.gain_shield(3)
            extra["avoid_wind_shield"] = 3
        if entity.has_status("急速"):
            entity._jisu_dodges = getattr(entity, "_jisu_dodges", 0) + 1
            if entity._jisu_dodges >= 2:
                entity._jisu_dodges -= 2
                extra["jisu_speed"] = self._gain_speed(entity, 1, ctx={
                    "timing": self._current_context_timing(), "source": "急速", "source_type": "daowen",
                    "actor": entity, "target": entity, "mechanic": "speed_change", "subtype": "current_speed",
                    "amount": 1, "tags": {"daowen", "dodge_followup"},
                })
        # 洞察状态与【窥隙法匣】是两个独立来源：前者由 ROUND_START 的
        # 「洞察·结算」机制领取，后者由遗物回始结算。分开记账，避免遗物门闩
        # 清理时误删洞察 pending，或持有遗物时把洞察额度错误地当成遗物额度领取。
        if entity.has_status("洞察"):
            entity._dongcha_pending = getattr(entity, "_dongcha_pending", 0) + 10
            extra["dongcha_pending"] = entity._dongcha_pending
        if self._relic_active(entity, "窥隙法匣"):
            entity._kuixi_pending_mana = getattr(entity, "_kuixi_pending_mana", 0) + 10
            extra["kuixi_pending_mana"] = entity._kuixi_pending_mana
        if self._relic_active(entity, "逐影胫骨"):
            entity._zhuying_dodges = getattr(entity, "_zhuying_dodges", 0) + 1
            if entity._zhuying_dodges >= 2:
                entity._zhuying_dodges -= 2
                extra["zhuying_speed"] = self._gain_speed(entity, 1, ctx={
                    "timing": self._current_context_timing(), "source": "逐影胫骨", "source_type": "relic",
                    "actor": entity, "target": entity, "owner": entity, "mechanic": "speed_change",
                    "subtype": "current_speed", "amount": 1, "tags": {"relic", "dodge_followup"},
                })
        return extra

    def _spend_dodge_speed(self, entity: Entity, relic_target_ref: Optional[str] = None) -> dict:
        """闪避：先走失速总线，再结算闪避专属句。"""
        extra = {}
        extra["lost_speed"] = self._lose_current_speed(
            entity, 1, relic_target_ref, require_huifeng=self._huifeng_active(entity),
            ctx={"timing": self._current_context_timing(), "source": "闪避", "source_type": "action",
                 "actor": entity, "target": entity, "mechanic": "speed_change", "subtype": "current_speed",
                 "amount": -1, "tags": {"dodge", "active_payment"}})
        extra.update(self._note_dodge(entity))
        return extra

    def _huifeng_active(self, entity: Entity) -> bool:
        return self._relic_active(entity, "回锋刀")

    def _huifeng_ref_from_choice(self, decision: Any) -> str:
        if not isinstance(decision, dict):
            return ""
        ref = decision.get("target_ref") or decision.get("dodge_relic_target_ref") or ""
        if isinstance(ref, str) and ref:
            return ref
        index = decision.get("enemy_index")
        if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
            return f"enemy:{index}"
        return ""

    def _remember_huifeng_target(self, holder: Entity, ref: str) -> None:
        if not ref:
            return
        if holder is self.state.player:
            self.state.event_modifiers["huifeng_target_ref"] = ref
        elif holder is not None:
            holder._huifeng_target_ref = ref
            self.state.event_modifiers["huifeng_target_ref_opponent"] = ref

    def _resolve_huifeng_target(self, holder: Entity, explicit_ref: Optional[str] = None):
        refs = self._combat_entity_refs()
        stored = ""
        if holder is self.state.player:
            stored = self.state.event_modifiers.get("huifeng_target_ref") or ""
        elif holder is not None:
            stored = (getattr(holder, "_huifeng_target_ref", "")
                      or self.state.event_modifiers.get("huifeng_target_ref_opponent")
                      or "")
        for ref in (explicit_ref or "", stored):
            if not ref:
                continue
            target = refs.get(ref)
            if (target is not None and target.is_alive
                    and self.state.on_player_side(target) != self.state.on_player_side(holder)):
                return ref, target
        return None, None

    def _trigger_huifeng_on_speed_loss(
        self, holder: Entity, lost: int, explicit_ref: Optional[str] = None,
        *, require_target: bool = False,
        speed_ctx: Optional[dict] = None,
    ) -> Optional[dict]:
        """回锋刀：每失去1点当前速度后，对已显式提交的[目标]造成3点伤害。不自动选目标。"""
        if lost <= 0 or not self._huifeng_active(holder):
            return None
        ref, target = self._resolve_huifeng_target(holder, explicit_ref)
        if target is None:
            if require_target:
                raise ValueError("回锋刀触发必须显式提交合法敌方目标引用")
            return None
        self._remember_huifeng_target(holder, ref)
        detail = self._apply_hostile_damage(target, 3 * lost, source=holder, ctx={
            "timing": (speed_ctx or {}).get("timing") or self._current_context_timing(),
            "source": "回锋刀", "source_type": "relic", "actor": holder, "target": target,
            "owner": holder, "mechanic": "damage", "subtype": "relic", "amount": 3 * lost,
            "tags": {"relic", "speed_loss_followup"},
            "parent_event_id": (speed_ctx or {}).get("event_id"),
        })
        if holder is not None and detail.get("actual_damage", 0) > 0:
            holder.damage_dealt_this_round += detail["actual_damage"]
        return {"target": target.name, "lost_speed": lost, **detail}

    def _lose_current_speed(
        self, entity: Entity, amount: int, relic_target_ref: Optional[str] = None,
        *, require_huifeng: bool = False,
        ctx: Optional[EffectContext | dict] = None,
    ) -> int:
        """失去当前速度的唯一入口。回锋刀按失去点数造伤；避风铃在归零时+15。"""
        if entity is None or amount <= 0:
            return 0
        before = entity.current_speed
        entity.current_speed = max(0, entity.current_speed - amount)
        lost = before - entity.current_speed
        speed_ctx = self._record_speed_change_event(entity, -lost, ctx, field="current_speed")
        if lost and before > 0 and entity.current_speed == 0:
            self._trigger_bifengling_zero(entity)
        # 失速冥灯：只要场上有未封印持有者，任何角色每次实际失速都损失2点速限。
        # 用战斗账本登记，战终逆向清除；不改速度变化事件本身，也不重复扣当前速度。
        if lost and self._any_active_relic("失速冥灯"):
            self._battle_delta(entity, "speed_limit", -2, "失速冥灯",
                               EffectPolarity.DEBUFF.value)
            entity.current_speed = min(entity.current_speed, entity.speed_limit)
        self._trigger_huifeng_on_speed_loss(
            entity, lost, relic_target_ref, require_target=require_huifeng,
            speed_ctx=speed_ctx)
        # 冥气X：[目标]每失去一次速度[速限]-2，持续X。
        # 累计局内后果（同畸变/伤痕），经 BATTLE 账本登记，[战终]逆向清除。
        if lost and entity.has_status("冥气"):
            penalty = entity.get_status_value("冥气") or 0
            if penalty:
                self._battle_delta(entity, "speed_limit", -penalty, "冥气",
                                   EffectPolarity.DEBUFF.value)
                entity.current_speed = min(entity.current_speed, entity.speed_limit)
        return lost

    def _trigger_bifengling_zero(self, entity: Entity) -> None:
        """避风铃：当前速度归零时获得15点格挡。只认字段变为0。"""
        if not self._relic_active(entity, "避风铃"):
            return
        entity.gain_shield(15)

    def note_mana_inflicted(self, source: Entity, target: Entity, amount: int) -> None:
        """寒冰法力：对[目标]每累计施加10法力，使其本回合出手次数-1。"""
        if amount <= 0 or source is None or target is None:
            return
        if not self._relic_active(source, "寒冰法力"):
            return
        before_tier = target.mana_inflicted_this_round // 10
        target.mana_inflicted_this_round += amount
        new_stacks = target.mana_inflicted_this_round // 10 - before_tier
        if new_stacks > 0:
            target.add_status(StatusEffect(
                name="无力", value=new_stacks, remaining_rounds=1, source="寒冰法力"))

    def _find_named(self, name: str) -> Optional[Entity]:
        for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            if e.name == name:
                return e
        return None

    def single_round_action_count(self, entity: Entity) -> int:
        if entity is None:
            return 0
        if entity.entity_type == "怪物":
            n = 2
            # 状态通道照旧：怪物读自身【疯狂】/【无力】层数。
            # 2026-10-25 用户令：【疯狂】遗物化——遗物效果见下方全场判定，不是道纹。
            n += entity.get_status_value("疯狂")
            n -= entity.get_status_value("无力")
            if self._relic_active(entity, "癫狂之脑"):
                n += 1
            if self._any_active_relic("疯狂"):
                n += 1
            # 【沉重枷锁】与【疯狂】对称：出手预算直接 -1，不经速度绕道
            # （2026-10-25 用户令：出手预算＝基础 2 次，不再由速度换算）。
            if self._any_active_relic("沉重枷锁"):
                n -= 1
            return max(0, n)
        n = DaoWenEngine.single_round_action_count(entity)
        # 癫狂之脑（通用遗物池，2026-10-03 新增）：你每回合的出手次数+1。
        if self._relic_active(entity, "癫狂之脑"):
            n += 1
        # 【疯狂】（2026-10-25 遗物化）：所有角色每回合出手次数+1。
        if self._any_active_relic("疯狂"):
            n += 1
        # 【沉重枷锁】：所有角色每回合出手次数-1。
        if self._any_active_relic("沉重枷锁"):
            n -= 1
        return n

    def _current_context_timing(self) -> str:
        forced = getattr(self, "_forced_context_timing", "")
        if forced:
            return forced
        if getattr(self.state, "phase", "") == "pre_battle":
            return "pre_battle"
        sub = getattr(self.state, "combat_subphase", "") or ""
        return sub or getattr(self.state, "phase", "") or "unknown"

    @staticmethod
    def _damage_context_subtype(damage_type: str) -> str:
        return {
            DamageType.NORMAL.value: "normal",
            DamageType.IGNORE_SHIELD.value: "ignore_shield",
            DamageType.IGNORE_DODGE.value: "ignore_dodge",
            DamageType.MUST_HIT.value: "must_hit",
            DamageType.REFLECT.value: "reflect",
            DamageType.COST.value: "cost",
            "无视格挡": "ignore_shield",
            "无视闪避": "ignore_dodge",
            "必中": "must_hit",
            "普通": "normal",
            "代价": "cost",
        }.get(damage_type, damage_type or "normal")

    def resolve_attack(
        self,
        attacker: Entity,
        target: Entity,
        hit_index: int = 0,
        is_must_hit: bool = False,
        dodge: bool = False,
        parry: bool = False,
        allocated_damage: Optional[int] = None,
        blood_shadow: bool = False,
        spell_choices: Optional[dict] = None,
        entity_refs: Optional[dict[str, Entity]] = None,
        dodge_relic_target_ref: Optional[str] = None,
        cost_share_target_ref: str = "",
        wanjunyin: Optional[bool] = None,
    ) -> dict:
        """攻击结算的公开入口（开帧后转实现体 `_resolve_attack_impl`）。"""
        with resolution_frame(self, KIND_EFFECT, "攻击",
                              getattr(attacker, "name", "?"),
                              "->", getattr(target, "name", "?")):
            return self._resolve_attack_impl(
                attacker, target, hit_index, is_must_hit, dodge, parry, allocated_damage,
                blood_shadow, spell_choices, entity_refs, dodge_relic_target_ref,
                cost_share_target_ref, wanjunyin)

    def _resolve_attack_impl(
        self,
        attacker: Entity,
        target: Entity,
        hit_index: int = 0,
        is_must_hit: bool = False,
        dodge: bool = False,
        parry: bool = False,
        allocated_damage: Optional[int] = None,
        blood_shadow: bool = False,
        spell_choices: Optional[dict] = None,
        entity_refs: Optional[dict[str, Entity]] = None,
        dodge_relic_target_ref: Optional[str] = None,
        cost_share_target_ref: str = "",
        wanjunyin: Optional[bool] = None,
        _wanjunyin_converted: bool = False,
    ) -> dict:
        """
        解析一次攻击
        dodge: 目标是否选择闪避（由AI决策）
        blood_shadow: 目标是否选择用【血影】遗物(流血10取消本次判定)代替常规闪避
        """
        entity_refs = entity_refs or self._combat_entity_refs()
        spell_choices = spell_choices if spell_choices is not None else {"before": {}, "after": {}}
        # 攻击失血由本方法的反应窗口结算，标记在下方置位。先清掉可能残留的旧标记，
        # 避免异常中断后误伤后续失血的自动触发。
        self._attack_after_window_target = None
        result = {
            "attacker": attacker.name,
            "target": target.name,
            "hit_index": hit_index,
            "dodge_attempted": dodge,
            "dodge_success": False,
            "parry_attempted": parry,
            "parry_success": False,
            "allocated_damage": allocated_damage,
            "damage_dealt": 0,
            "shield_absorbed": 0,
            "hp_lost": 0,
            "target_died": False,
        }
        # 万钧印（消耗品）：己方角色被选为攻击目标时自动触发并消耗1点耐久，
        # 将攻击改为攻击者对自己发动。显式传 wanjunyin=False 可拒绝本次；
        # 被改道的一次自攻不会再被改道（_wanjunyin_converted 防 A→B→A 递归）。
        if (not _wanjunyin_converted and wanjunyin is not False
                and attacker is not target and attacker.is_alive
                and self._wanjunyin_holder(target)):
            item = next(item for item in self.state.consumables
                        if item.name == "万钧印" and not item.is_depleted)
            item.use()
            # 原受击者的反应提交随改道失效；自攻目标按自身可用反应法术生成
            # 显式「不发动」提交，避免把原目标的法术选择错配给攻击者。
            redirected_choices = {
                key: {name: {"use": False} for name in self._eligible_spell_flows(attacker, trigger)}
                for key, trigger in self._REACTION_SPELL_SLOTS
            }
            converted = self._resolve_attack_impl(
                attacker, attacker, hit_index, is_must_hit, False, False, allocated_damage,
                False, redirected_choices, entity_refs, None, cost_share_target_ref,
                None, True)
            converted["wanjunyin"] = {
                "relic": "万钧印", "original_target": target.name,
                "converted_to": attacker.name,
                "note": "万钧印：本次攻击改为攻击者对自己发动",
            }
            return converted

        # 反应法术提交必须按最终受击者校验。万钧印改道时，原目标并不承受攻击，
        # 不能拿原目标的 spell_choices 去校验攻击者对自己造成的自攻。
        self.validate_spell_reaction_submission(target, attacker, spell_choices, entity_refs)

        # 飞行：非飞行者无法选中飞行目标
        if not self.is_targetable(attacker, target):
            result["cant_target"] = True
            result["note"] = "飞行目标无法被非飞行者选中"
            return result

        # 必中：三种口径（2026-09-28 必中X=self-buff最终口径）
        #   1) 显式必中（法术/遗物/状态显式标记 is_must_hit）；
        #   2) 攻击者自身【必中】buff（必中X：自身必中姿态持续X回合，期间你选中的目标无法闪避）；
        #   3) 旧版 _bizhong_left 次数型必中余数（兼容老 grant_bizhong 内部接口/怪物旧道纹/旧【蒙蔽】反制）。
        # 自身【必中】buff 判定：
        # - 新必中X（self-buff，持续X回合）：status.remaining_rounds > 0 → 必中但不消耗次数
        # - grant_bizhong 留下的次数型状态：status.remaining_rounds == -1（永久），仅作显示，
        #   必中与否看 _bizhong_left 次数，由 consume_bizhong 负责扣减
        def _has_real_bizhong_buff(ent):
            if ent is None: return False
            s = next((s for s in ent.status_effects if s.name == "必中"), None)
            return bool(s and s.remaining_rounds > 0)  # -1 是次数型标记，不算真buff
        caster_has_bizhong_buff = _has_real_bizhong_buff(attacker)
        # 2026-10-12 用户令：必中X 改挂**目标**，故目标身上的【必中】debuff 同样封死闪避。
        target_bizhonged = self.out_of_turn_blocked(target)
        must_hit = (is_must_hit or caster_has_bizhong_buff or target_bizhonged
                    or self.consume_bizhong(attacker))

        # 血影（初拥之夜遗物，仅玩家自身持有）：非必中判定下，可流血10取消本次判定，是常规闪避外的另一选项
        if (blood_shadow and not must_hit and self.state.side_has(target, "血影")):
            self.pay_numeric_cost(
                target, "流血", 10,
                cost_share_target_ref=cost_share_target_ref,
                cost_context={"timing": "reaction", "source": "血影", "source_type": "relic", "tags": {"active_payment"}})
            result["blood_shadow_success"] = True
            result["note"] = "血影：流血10，本次判定被取消"
            return result

        # 闪避判定
        if dodge:
            if must_hit:
                result["dodge_success"] = False
                result["dodge_fail_reason"] = (
                    "目标被【必中】封锁回合外行动，无法闪避" if target_bizhonged
                    else "必中攻击无法闪避")
            elif target.current_speed >= 1:
                extra = self._spend_dodge_speed(target, dodge_relic_target_ref)
                result["dodge_success"] = True
                result["speed_after_dodge"] = target.current_speed
                if extra:
                    result["dodge_extra"] = extra
                # 「闪避时」开放可扩展触发：持有者成功闪避后触发（注册/接线见
                # spell_dsl.EXTRA_TRIGGERS 与 _fire_auto_reaction）。此分支位于
                # _attack_after_window_target 置位之前，不会与普攻伤害窗口冲突。
                result["spell_logs"] = self._fire_auto_reaction(
                    target, "闪避时", make_context(
                        timing=self._current_context_timing(), source="普通攻击",
                        source_type="attack", actor=attacker, target=target,
                        mechanic="dodge", subtype="dodge_success", amount=0,
                        tags={"attack", "dodge"}, parent_event_id=None))
                return result
            else:
                result["dodge_success"] = False
                result["dodge_fail_reason"] = "速度不足"
        
        # 逐击招架：与闪避一样先支付1速度，但只免疫攻击伤害，不取消判定。
        # 真刀（分配伤害>0）成功后返还1速度；空刀不返还，因此可骗取速度。
        if parry:
            if target_bizhonged:
                # 2026-10-12 用户令：招架是回合外行动，【必中】下不得进行。
                # 注意与闪避不同——招架失败不退速度（它本来就没支付）。
                result["parry_success"] = False
                result["parry_fail_reason"] = "目标被【必中】封锁回合外行动，无法招架"
            elif target.current_speed < 1:
                result["parry_fail_reason"] = "速度不足"
            else:
                target.current_speed -= 1
                result["speed_after_parry_cost"] = target.current_speed
                revealed = (attacker.effective_attack_power()
                            if allocated_damage is None else allocated_damage)
                if revealed > 0:
                    target.current_speed = min(target.speed_limit, target.current_speed + 1)
                    result["parry_success"] = True
                    result["damage_dealt"] = 0
                    result["speed_after_parry"] = target.current_speed
                    result["note"] = "招架真刀：免疫本次攻击伤害并返还1速度"
                    return result
                result["note"] = "招架空刀：未返还速度"
                return result

        # 伤害结算。新白刃战由攻击者为每击自由分配非负整数伤害（玩家侧与怪物阶段
        # 均显式传 allocated_damage）；未传的旧调用仍按当前攻击力结算，兼容
        # NPC 遭遇战等尚未接入逐击契约的入口。
        damage = (attacker.effective_attack_power()
                  if allocated_damage is None else allocated_damage)
        # 逆鳞（F2）：下次伤害+全部层数后清空
        if hasattr(attacker, "_nilin") and getattr(attacker, "_nilin", 0) > 0:
            bonus = attacker._nilin
            damage += bonus
            result["nilin_bonus"] = bonus
            attacker._nilin = 0
        # 检查蒙蔽状态
        if attacker.has_status("蒙蔽"):
            stacks = attacker.get_status_value("蒙蔽")
            if stacks > 0:
                damage = 0
                # 减少蒙蔽层数
                for s in attacker.status_effects:
                    if s.name == "蒙蔽" and s.value > 0:
                        s.value -= 1
                        if s.value <= 0:
                            attacker.status_effects.remove(s)
                        break
                result["damage_dealt"] = 0
                result["blocked_by"] = "蒙蔽"
                return result
        
        # 检查贯穿（无视格挡）
        ignore_shield = attacker.has_status("贯穿")
        # 震岳龙躯（真龙之心遗物）：激活期间，自身受到超出15点的伤害无效
        if self.state.side_body_shield(target) > 0:
            damage = min(damage, 15)
        # 本次攻击造成的失血由 resolve_attack 的既有反应窗口结算；标记该目标，
        # 使失血后 hook 不再对同一目标重复触发。
        self._attack_after_window_target = target
        # 反应法术由攻击prepare列出、resolve显式提交；计算层不再自动选择X或目标。
        if damage > 0:
            slogs = self._resolve_spell_reactions(
                ActionPhase.BEFORE_DAMAGE_TAKEN.value, target, attacker,
                spell_choices["before"], entity_refs,
            )
            if slogs:
                result["spell_logs"] = slogs
            if not attacker.is_alive:
                damage = 0
        # 失去生命前：受到伤害前反应已结算完毕、伤害数值已最终确定，但生命尚未
        # 真正扣减——这是"失去生命前"与"受到伤害前"语义区分之处（前者关心
        # "即将失去多少生命"，后者关心"即将挨这一下打"）。复用同一套反应法术
        # 流水线，只是挂接点更靠近扣血这一刻。
        if damage > 0 and target.is_alive:
            slogs_life_before = self._resolve_spell_reactions(
                ActionPhase.BEFORE_LIFE_LOST.value, target, attacker,
                spell_choices.get("life_before", {}), entity_refs,
            )
            if slogs_life_before:
                result.setdefault("spell_logs", []).extend(slogs_life_before)
            if not attacker.is_alive:
                damage = 0
        # 裂变：受到伤害分X次结算；依全局整数规则，每次除法向上取整。
        if target.has_status("裂变") and damage > 0:
            xv = target.get_status_value("裂变") or 1
            if xv > 1:
                per = math.ceil(damage / xv)
                ta = ts = 0; died = False
                for _ in range(xv):
                    dr = self._apply_hostile_damage(
                        target, per, "普通" if not ignore_shield else "无视格挡", attacker,
                        ctx={"timing": self._current_context_timing(), "source": "普通攻击", "source_type": "attack",
                             "actor": attacker, "target": target, "mechanic": "damage", "subtype": "attack",
                             "amount": per, "tags": {"attack", "split_hit"}})
                    ta += dr["actual_damage"]; ts += dr["shield_absorbed"]; died = died or dr["died"]
                damage_result = {"actual_damage": ta, "shield_absorbed": ts, "hp_after": target.current_hp, "died": died, "split": xv}
            else:
                damage_result = self._apply_hostile_damage(
                    target, damage, "普通" if not ignore_shield else "无视格挡", attacker,
                    ctx={"timing": self._current_context_timing(), "source": "普通攻击", "source_type": "attack",
                         "actor": attacker, "target": target, "mechanic": "damage", "subtype": "attack",
                         "amount": damage, "tags": {"attack"}})
        else:
            damage_result = self._apply_hostile_damage(
                target, damage, "普通" if not ignore_shield else "无视格挡", attacker,
                ctx={"timing": self._current_context_timing(), "source": "普通攻击", "source_type": "attack",
                     "actor": attacker, "target": target, "mechanic": "damage", "subtype": "attack",
                     "amount": damage, "tags": {"attack"}})
        result["damage_dealt"] = damage_result["actual_damage"]
        result["shield_absorbed"] = damage_result["shield_absorbed"]
        result["hp_lost"] = damage_result["actual_damage"]
        result["target_died"] = damage_result["died"]
        result["target_hp_after"] = damage_result["hp_after"]
        # 撤退：朋友/员工即将命零时自动撤退（伤害清零、保留生命、退出本场），透传给战报渲染
        if damage_result.get("retreated"):
            result["retreated"] = True
        if damage_result["actual_damage"] > 0:
            attacker.damage_dealt_this_round += damage_result["actual_damage"]
        if "split" in damage_result:
            result["split"] = damage_result["split"]
        # 受到伤害后：这一击已经完整落地（格挡/固执/伤害减免均已算完），与
        # "受到伤害前"对称的挂接点。判定用damage（这一击最终确定的伤害数值，
        # 落地前就已确定，不受格挡是否吸收影响）而不是actual_damage——
        # 即使格挡把伤害全部吸收，也应算"受到了一次伤害"，只是生命没有
        # 实际减少（与"失去生命后"要求actual_damage>0严格区分）。
        if damage > 0 and target.is_alive:
            slogs_damage_after = self._resolve_spell_reactions(
                ActionPhase.AFTER_DAMAGE_TAKEN.value, target, attacker,
                spell_choices.get("damage_after", {}), entity_refs,
            )
            if slogs_damage_after:
                result.setdefault("spell_logs", []).extend(slogs_damage_after)
        if damage_result["actual_damage"] > 0 and target.is_alive:
            slogs2 = self._resolve_spell_reactions(
                ActionPhase.AFTER_LIFE_LOST.value, target, attacker,
                spell_choices["after"], entity_refs,
            )
            if slogs2:
                result.setdefault("spell_logs", []).extend(slogs2)
        self._attack_after_window_target = None
        
        return result

    # ========== 回合管理 ==========

    def round_start(self, relic_choices: Optional[dict] = None) -> dict:
        """
        回始结算
        1. 【某人的偏爱】持有者获得等同当前法限的法力和异变（法力走统一获得入口）
        2. 结算回始类效果
        3. 返回需要决策的信息
        """
        effects = []
        player = self.state.player
        if player is not None:
            effects.extend(self._dispatch_phase(Phase.ROUND_START_PREPARE, target=player))

        # 活血追踪归零 + 出手预算归零（回始重置本回合已用出手次数）+ 血誓戒每回合限一次归零 + 血族血脉判定归零
        seen_entities: set[int] = set()
        for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            # 迷失变身后 player 引用可能同时出现在 enemies；每个持有者每回始只触发一次。
            if id(e) in seen_entities:
                continue
            seen_entities.add(id(e))
            # 窥隙法匣：成功闪避时记下的法力只在下一回始领取，不与当次闪避即时结算。
            # 它使用独立 pending；绝不能清理洞察状态的 _dongcha_pending。
            pending_kuixi = int(getattr(e, "_kuixi_pending_mana", 0) or 0)
            if pending_kuixi > 0 and self._relic_active(e, "窥隙法匣"):
                before_mana = e.current_mana
                e.gain_mana(pending_kuixi)
                self.clamp_immortal_body(e)
                effects.append({"type": "relic", "relic": "窥隙法匣", "entity": e.name,
                                "mana_requested": pending_kuixi,
                                "mana_gained": e.current_mana - before_mana})
                e._kuixi_pending_mana = 0
            elif pending_kuixi > 0:
                # 遗物被移除/封印后，旧待领取额度不应凭空在以后生效。
                e._kuixi_pending_mana = 0
            # 2026-10-13：谁持有谁获得法限等量法力与异变；这不是代价，不走代价结算。
            # 兼容实体遗物、轮回者本局遗物及死斗对手遗物，并尊重玩家的遗物封印。
            owns_relic = (any(r.name == MONSTER_MANA_RELIC and getattr(r, "enabled", True) for r in e.relics)
                          or self._relic_active(e, MONSTER_MANA_RELIC))
            relic_unsealed = (e is not player
                              or self.state.sealed_relics.get(MONSTER_MANA_RELIC, 0) <= 0)
            if e.mana_limit > 0 and owns_relic and relic_unsealed:
                amount = e.mana_limit
                mana_before = e.current_mana
                e.gain_mana(amount)  # 遵守法限与【勾魂】；异变不按实际回充量缩减
                self.clamp_immortal_body(e)
                mana_gained = e.current_mana - mana_before
                mutation = apply_verb(self, "mutation", {"target": e, "layers": amount}, {
                    "timing": "round_start", "source": MONSTER_MANA_RELIC,
                    "source_type": "relic", "actor": e, "target": e, "owner": e,
                    "mechanic": "mutation", "subtype": "relic_grant", "amount": amount,
                    "tags": {"relic", "round_start"},
                })
                effects.append({
                    "type": "relic", "relic": MONSTER_MANA_RELIC, "entity": e.name,
                    "mana_requested": amount, "mana_gained": mana_gained,
                    "mutation_added": mutation["mutation_added"],
                    "mutation_total": mutation["mutation_total"],
                    "log": f"{e.name}的【{MONSTER_MANA_RELIC}】：法力+{mana_gained}，"
                           f"异变+{amount}（累计{mutation['mutation_total']}层）",
                })
            apply_verb(self, "reset_fields", {"target": e, "fields": {
                "hp_lost_this_round": 0,
                "_hp_loss_events": [], "_speed_change_events": [], "_blood_limit_events": [],
                "actions_used_this_round": 0, "blood_oath_used_this_round": False,
                "mana_inflicted_this_round": 0, "damage_dealt_this_round": 0}})
        # DM裁定 2026-09-09：[回始]不再回填法力。法力改为**一池制**——[战始]给满
        # 等同[法限]的一池，整场只出不进，[战终]复原（与[速度]同口径）。
        # 因这一段，"[回始]不获得法力"曾一度失去作用对象，【勾魂】在 2026-09-09
        # 改为消耗法力翻倍；2026-10-08 用户令恢复「无法获得[法力]」原意——
        # 战斗内已有多个法力来源（聚能/储能电池/守夜灯/承露盏/
        # 透支/法术），效果重新有对象。判定位见 models.py::gain_mana。

        # 遗物：回始触发（回锋刀按速限缺口造伤）。
        relic_logs = self.process_relics(TriggerTiming.ROUND_START, {"relic_choices": relic_choices or {}})
        effects.extend({"type": "relic", "log": l} for l in relic_logs)
        # 【守夜灯】2026-10-25 用户令改到[回终]结算（见 round_end），[回始]不再授予。

        # 结算回始效果
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            # 蓄锐（2026-09-28）：上回合施放蓄锐积攒的 pending 转成【蓄锐·增】1回 buff（+1出手）。
            pending = int(getattr(entity, "_xurui_pending", 0) or 0)
            if pending > 0:
                entity._xurui_pending = 0
                existed = next((s for s in entity.status_effects if s.name == "蓄锐·增"), None)
                if existed is not None:
                    existed.remaining_rounds = 1
                    existed.value = (existed.value or 0) + pending
                else:
                    entity.add_status(StatusEffect(
                        name="蓄锐·增", remaining_rounds=1, value=pending,
                        source=entity.name))
                effects.append({"type": "xurui_burst", "entity": entity.name, "bonus": pending})
            # 机制系统：ROUND_START 相位分发。自愈删除后不再有回始回血机制。
            # round_start 只负责宣布时点，具体机制由声明层按 priority 执行；
            # 机制的报告条目并入 effects，战报格式与迁移前一致。
            effects.extend(self._dispatch_phase(Phase.ROUND_START, target=entity))

        # ---- 罪孽【清算】与遗物【灾厄轮盘】的回始结算相位 ----
        # 本位置只宣布相位时点（锚点 = 回始效果循环之后、current_round+1 之前）；
        # 结算由声明层的 清算·结算/灾厄轮盘·结算 执行。原道纹【赌命】现为常驻遗物【灾厄轮盘】。
        # 分发语义=统一状态排序：按各账本状态记录的来源道纹序列位置结算；不再保留旧的
        # 清算账本结算在前；遗物灾厄轮盘机制随后按持有者触发。账本访问走 mechanisms.ledger。
        effects.extend(self._dispatch_phase_all(
            Phase.ROUND_START_SETTLE,
            self.state.get_all_player_side() + self.state.get_all_enemy_side()))

        self.state.current_round += 1
        # 【回增】统一相位：回合序号增加后触发，守夜灯等效果走声明机制层。
        effects.extend(self._dispatch_phase_all(
            Phase.ROUND_INCREASE,
            self.state.get_all_player_side() + self.state.get_all_enemy_side()))

        # 【封印X】延迟回场：在第 R+X 回合始把原实体放回它脱场前所在的阵营。
        # 2026-10-08 用户令：封印的目标已不限于怪物，home/index 记录原址
        # （"player" 表示玩家位），一律回原址，不得一律塞回 enemies。
        # 回场当回合记录 spawned_round；2026-09-15 用户令取消白板后，回场当回合
        # 同样可以发动道纹（spawned_round 仅作出生回合记录）。
        delayed = list(getattr(self.state, "delayed_monster_reentries", []) or [])
        due = [entry for entry in delayed if entry.get("return_round", 0) <= self.state.current_round]
        if due:
            for entry in due:
                monster = entry["monster"]
                monster.is_alive = True
                monster.is_departed = False
                monster.departure_reason = ""
                monster.removed_without_kill = False
                monster.spawned_round = self.state.current_round
                # home/index 缺省视为旧存档：那只可能是怪物，回 enemies。
                self.state.reattach_to_field(entry.get("home") or "enemies",
                                             entry.get("index"), monster)
                monster._delayed_by_seal = False
                self.state.delayed_monster_reentries.remove(entry)
                effects.append({"type": "seal_reentry", "entity": monster.name,
                                "round": self.state.current_round,
                                "delay_rounds": entry.get("delay_rounds", 0),
                                "home": entry.get("home") or "enemies"})

        # 出怪配方（2026-09-28 用户令）：增援在第 i 波 `T_i` 累加的回合进场，
        # 一波可进 R_i（≥1）只；旧的固定波次 R4/R7/R10 已废止。
        # 死斗无增援；增援怪进场当回合即可发动道纹（2026-09-15 用户令删除白板）。
        queue = list(getattr(self.state, "monster_reinforcements", []) or [])
        if queue and not self.state.in_final_duel:
            from .monsters import make_monster_entity
            due = [entry for entry in queue
                   if int(entry.get("arrive_round", 1)) <= self.state.current_round]
            for monster_def in due:
                self.state.monster_reinforcements.remove(monster_def)
                m = make_monster_entity(monster_def)
                m.spawned_round = self.state.current_round
                self.init_monster_shards(m)
                self.state.enemies.append(m)
                effects.append({"type": "wave_spawn", "entity": m.name,
                                "round": self.state.current_round,
                                "queued_left": len(self.state.monster_reinforcements)})

        return {
            "round": self.state.current_round,
            "phase": "回始",
            "effects": effects,
            "state": self._get_combat_state()
        }
    
    def advance_target_rounds(self, target: Entity, rounds: int, source: str = "凝滞") -> list[dict]:
        """令单一目标真实经历额外回合：逐回推进全局回合号、目标的回增/回终事件、
        凡庸计数和目标自身限时状态；不替其他角色执行行动或递减其状态。"""
        effects: list[dict] = []
        rounds = max(0, int(rounds))
        global_round = self.state.current_round
        for index in range(rounds):
            if not target.is_alive or self.state.battle_over():
                break
            # 当前回合号只在本次目标局部结算期间临时前进，以便回增类机制读取正确序号；
            # 额外回合结束后恢复全局回合号，避免推进封印、增援及其他全局倒计时。
            self.state.current_round = global_round + index
            # 额外回合从目标自己的回始开始；只分发给目标，避免其他角色也被推进。
            target.actions_used_this_round = 0
            target.damage_dealt_this_round = 0
            target.blood_oath_used_this_round = False
            target.mana_inflicted_this_round = 0
            effects.extend(self._dispatch_phase(Phase.ROUND_START_PREPARE, target=target))
            effects.extend(self._dispatch_phase(Phase.ROUND_START, target=target))
            effects.extend(self._dispatch_phase(Phase.ROUND_START_SETTLE, target=target))
            self.state.current_round = global_round + index + 1
            effects.extend(self._dispatch_phase(Phase.ROUND_INCREASE, target=target))
            effects.extend(self._dispatch_phase(Phase.ROUND_END, target=target))
            why = self._tick_mediocrity_counters(target) if target.is_alive else None
            if why:
                effects.extend(self._apply_mediocrity(target, why))
            expired = target.tick_status_effects()
            if expired:
                if "无限血限" in expired and not self.state.hp_overflow_exempt(target):
                    before_hp = target.current_hp
                    target.current_hp = min(target.current_hp, target.blood_limit)
                    if target.current_hp <= 0 and target.is_alive:
                        self._check_hp_zero_death(target)
                    effects.append({"type": "infinite_blood_limit_expired", "entity": target.name,
                                    "blood_limit": target.blood_limit, "hp_before": before_hp,
                                    "hp_after": target.current_hp})
                panel_modifier_sources = {"全力", "弱化"}
                rolled_back = self.state.rollback_scoped_sources(target, set(expired) & panel_modifier_sources)
                effects.append({"type": "status_expired", "entity": target.name,
                                "expired_effects": expired, "rolled_back_deltas": rolled_back})
                if "逆鳞" in expired and hasattr(target, "_nilin"):
                    target._nilin = 0
                if "飞行" in expired and not self._is_flying(target):
                    target.is_flying = False
                if "变形" in expired and hasattr(target, "_bianxing_original"):
                    target.current_speed, target.current_mana = target._bianxing_original
                    delattr(target, "_bianxing_original")
                    self.clamp_immortal_body(target)
                    effects.append({"type": "bianxing_restore", "entity": target.name,
                                    "current_speed": target.current_speed, "current_mana": target.current_mana})
            effects.extend(self._dispatch_phase(Phase.ROUND_END_POST_MEDIOCRITY, target=target))
            effects.extend(self._dispatch_phase(Phase.ROUND_END_RECONCILE, target=target))
            effects.append({"type": "extra_target_round", "source": source, "target": target.name,
                            "round": self.state.current_round, "index": index + 1, "total": rounds})
            self.state.current_round = global_round
        return effects
    def round_end(self, blood_lineage_cost_share_target_ref: str = "") -> dict:
        """
        回终结算
        1. 回终类效果结算
        2. 格挡清空
        3. 持续X剩余回合-1
        """
        effects = []
        mediocrity_ready: list[tuple[Entity, str]] = []

        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            # 蚀界残钟：场上有持有者时，每个角色在回终第一拍失去「当前法力×当前速度」的血限。
            # 保持旧【畸变】的回终锚点，走统一血限变化/命零管线，不手改 current_hp。
            if entity.is_alive and self._any_active_relic("蚀界残钟"):
                erosion = max(0, int(entity.current_mana) * int(entity.current_speed))
                if erosion > 0:
                    change = self._apply_blood_limit_change(
                        entity, -erosion, "蚀界残钟", EffectPolarity.DEBUFF.value,
                        ctx={"timing": "round_end", "source": "蚀界残钟", "source_type": "relic",
                             "actor": entity, "target": entity, "owner": None,
                             "mechanic": "blood_limit_change", "subtype": "relic_round_end",
                             "amount": -erosion, "tags": {"relic", "round_end", "blood_limit_loss"}},
                    )
                    effects.append({"type": "relic", "relic": "蚀界残钟", "entity": entity.name,
                                    "blood_limit_loss": erosion, **change})
            # 机制系统：ROUND_END 相位分发。锚定语义：回终第一循环顶部、凡庸 tick 之前
            # （原畸变·结算位置）。凡庸之后的回终机制禁止注册到 ROUND_END
            # （会改变既有顺序），详见机制迁移台账。
            effects.extend(self._dispatch_phase(Phase.ROUND_END, target=entity))
            # 凡庸只在此拍更新计数；达阈值者稍后按「非轮回者优先」结算。
            if entity.is_alive:
                why = self._tick_mediocrity_counters(entity)
                if why:
                    mediocrity_ready.append((entity, why))

        # 多个角色同时触发凡庸时，非轮回者优先；同档保持原遍历顺序。
        # DM裁定（2026-08-18）：「非轮回者优先」的意义在于——按优先序逐个结算，
        # 一旦先炸裂的角色清空了某一方战场（战斗胜负因此已定），立即中断剩余凡庸结算；
        # 幸存侧尚未结算的待爆者不再炸裂（其计数随战斗结束清零）。
        # 注意：只有本拍凡庸「炸出来」的清空才中断；战场在结算开始前就已空置
        # （如战斗尚未开始的空场脚手架）不援引此裁定。
        mediocrity_ready.sort(key=lambda item: item[0].entity_type == "轮回者")
        decided_before_tick = self._mediocrity_battle_decided()
        for entity, why in mediocrity_ready:
            if not decided_before_tick and self._mediocrity_battle_decided():
                entity.no_action_rounds = 0
                entity.no_damage_rounds = 0
                effects.append({
                    "type": "mediocrity_interrupted", "entity": entity.name,
                    "note": f"{why}，但战场已因先前的【凡庸】清空、战斗结束：剩余凡庸中断结算"})
                continue
            effects.extend(self._apply_mediocrity(entity, why))

        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            # 凡庸之后的原第二实体循环；保持实体优先，不能提前到 ROUND_END。
            effects.extend(self._dispatch_phase(
                Phase.ROUND_END_POST_MEDIOCRITY, target=entity,
                picks={"blood_lineage_cost_share_target_ref": blood_lineage_cost_share_target_ref}))

            # DM裁定 2026-09-10：格挡不再[敌回终]每回合清除。
            # 原口径下 4 法力换 8 点格挡、一回合就被抹掉，等于没有价值；改为保留到
            # 被打掉或[战终]统一清除（api.py 战终处理里仍会 clear_shield）。
            # 注意：这使「庇护」的持续1回合失效为整场有效，是本次裁定的预期结果。
            
        # 法力清空（敌回终）
        # 规则：[法限]用于发动道纹与法术，法力[敌回终]清空。
        # 死斗双方都是轮回者，必须与回始同一套循环：每个存活轮回者各自清空。
        # 朋友/员工/怪物没有法限，不走这条。
        # DM裁定 2026-09-09：[敌回终]不再清空法力（法力一池制，只在[战终]复原）。
        
        # 持续效果递减。
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            expired = entity.tick_status_effects()
            if expired:
                # 【增殖】到期后恢复基础血限；若当前生命超出基础血限，按恢复后的血限封顶。
                if "无限血限" in expired and not self.state.hp_overflow_exempt(entity):
                    before_hp = entity.current_hp
                    entity.current_hp = min(entity.current_hp, entity.blood_limit)
                    if entity.current_hp <= 0 and entity.is_alive:
                        self._check_hp_zero_death(entity)
                    effects.append({
                        "type": "infinite_blood_limit_expired",
                        "entity": entity.name,
                        "blood_limit": entity.blood_limit,
                        "hp_before": before_hp,
                        "hp_after": entity.current_hp,
                    })
                # 只有“持续期间直接改写面板”的效果到期即还原；畸变/伤痕等
                # 已经产生的累计局内后果保留到战终，再由battle作用域统一回滚。
                panel_modifier_sources = {"全力", "弱化"}
                rolled_back = self.state.rollback_scoped_sources(
                    entity, set(expired) & panel_modifier_sources)
                effects.append({
                    "type": "status_expired",
                    "entity": entity.name,
                    "expired_effects": expired,
                    "rolled_back_deltas": rolled_back,
                })
                # 逆鳞（F2）：状态到期时清空计层
                if "逆鳞" in expired and hasattr(entity, "_nilin"):
                    entity._nilin = 0
                if "飞行" in expired and not self._is_flying(entity):
                    entity.is_flying = False
                if "变形" in expired and hasattr(entity, "_bianxing_original"):
                    # 2026-09-17 重做：还原的是互换前的**当前速度与当前法力**
                    # （旧版还原遗留字段 attack_power/attack_count）。
                    # 注意：互换时被上限钳掉的部分不会随还原回来——那是永久损失。
                    entity.current_speed, entity.current_mana = entity._bianxing_original
                    delattr(entity, "_bianxing_original")
                    self.clamp_immortal_body(entity)
                    effects.append({"type": "bianxing_restore", "entity": entity.name,
                                    "current_speed": entity.current_speed,
                                    "current_mana": entity.current_mana})
                # 干扰/手雷减攻到期自动由 tick 清理，无需额外
            # F2：清算状态消失即清账（∞/持续X到期后不再逐回始结算）。
            # 2026-10-03 迁移：判定与清账都在声明层（清算·对账）；2026-10-13 删【逼债】。
            # 本行只宣布相位时点，位置必须在 status tick 之后（否则会多结算一轮）。
            effects.extend(self._dispatch_phase(Phase.ROUND_END_RECONCILE, target=entity))
            # F2：抵扣封印/豪夺夺取回合递减统一走 _tick_sealed_and_stolen_relics，
            # 不再在此处逐实体递减。
        # 统一处理：玩家侧 + 每个存活实体的封印/豪夺遗物回终递减与归还
        ticked = self._tick_sealed_and_stolen_relics()
        if ticked.get("returned_steals"):
            effects.append({"type": "haoduo_returned",
                            "returned": ticked["returned_steals"]})

        # 震岳龙躯：两边各自递减
        if self.state.dragon_body_shield_rounds > 0:
            self.state.dragon_body_shield_rounds -= 1
            effects.append({"type": "dragon_body_tick", "side": "player",
                            "remaining": self.state.dragon_body_shield_rounds})
        if self.state.opponent_dragon_body_shield_rounds > 0:
            self.state.opponent_dragon_body_shield_rounds -= 1
            effects.append({"type": "dragon_body_tick", "side": "opponent",
                            "remaining": self.state.opponent_dragon_body_shield_rounds})

        # 原皮衣记录→原逐实体追踪清理，保持机制优先的两个独立锚点顺序。
        effects.extend(self._dispatch_phase_all(
            Phase.ROUND_END_TRACKING,
            self.state.get_all_player_side() + self.state.get_all_enemy_side()))

        # 手术·强制移植：本场第三回终仍保持原移植道纹时转为怪物。
        if self.state.current_round >= 3:
            for entity in list(self.state.friends + self.state.employees):
                transplanted = getattr(entity, "_transplanted_daowen", "")
                if transplanted and transplanted in entity.dao_wen:
                    if entity in self.state.friends: self.state.friends.remove(entity)
                    if entity in self.state.employees: self.state.employees.remove(entity)
                    entity.entity_type = "怪物"; entity.is_deployed = True
                    self.state.enemies.append(entity)
                    effects.append({"type": "transplant_monster", "entity": entity.name,
                                    "daowen": transplanted})

        # 多路径胜利结算（雕塑/癌变/还债）
        settled = self.settle_victory_paths()
        if settled:
            effects.extend(settled)

        return {
            "round": self.state.current_round,
            "phase": "回终",
            "effects": effects,
            "state": self._get_combat_state()
        }
    
    # ========== 辅助方法 ==========
    

    # ========== 大流程：死者之页 ==========

    def trigger_death_legacy(self, legacy: dict[str, str] | str) -> dict:
        """新增一页遗言；一句话，上限=`state.death_book_capacity`（默认20字）。

        DM裁定 2026-08-31：遗言废止三段式，只留一句话。校验统一委托
        `engine/death_book.py::validate_legacy`（一个效果只有一个正式执行入口）。
        """
        from .death_book import validate_legacy

        normalized = validate_legacy(legacy, self.state.death_book_capacity)
        self.state.death_book_legacies.append(normalized)
        return {
            "triggered": True,
            "legacy": normalized,
            "total_legacies": len(self.state.death_book_legacies),
        }

    def validate_battle_start_relic_choices(self, choices: dict) -> None:
        """校验所有可选[战始]遗物参数；缺项不得由计算层猜测或代选。"""
        if not isinstance(choices, dict):
            raise ValueError("relic_choices必须是对象")
        active = {r.name for r in self.state.relics if getattr(r, "enabled", True) and self.state.sealed_relics.get(r.name, 0) <= 0}
        player = self.state.player
        # 2026-10-13 用户令：【三相残韵盘】遗物删除，原战始显式决策分支一并移除。
        # 2026-10-11 用户令：【苍白之花】改为纯被动（物品索引.md:110「你的当前法力可以
        # 超出[法限]。」），旧效果「战始可选择疲惫5；战终精力+1」废止——它不再出现在
        # 战始可选遗物里，也不再有 use 布尔值需要提交。
        # 2026-10-14 用户令：【猩红果实】重做为纯被动（当前生命可超出[血限]），
        # 旧效果「战始可选流血10、战终血限+2」移交道纹【增殖】——战始 use 决策取消。
        refs = self._combat_entity_refs()
        if "负岳索" in active:
            decision = choices.get("负岳索")
            legal = {ref for ref, entity in refs.items()
                     if entity in self.state.friends + self.state.employees and entity.is_alive}
            if not isinstance(decision, dict) or decision.get("target_ref") not in legal:
                raise ValueError(f"负岳索必须显式选择朋友/员工target_ref，可选{sorted(legal)}")
        if "炉心坠" in active:
            decision = choices.get("炉心坠")
            hearts = {item.name for item in self.state.consumables if item.kind == "dragon_heart"}
            if not isinstance(decision, dict) or decision.get("heart_name") not in hearts:
                raise ValueError(f"炉心坠必须显式选择龙心heart_name，可选{sorted(hearts)}")
        if "烙痕钉" in active:
            decision = choices.get("烙痕钉")
            legal = {ref for ref, entity in refs.items() if self.state.on_enemy_side(entity)}
            target_ref = decision.get("target_ref") if isinstance(decision, dict) else None
            # 普通战斗的第一次战始静态校验发生在抽怪前，此时敌方引用尚未生成。
            # 允许形如 enemy:0 的稳定引用先通过语法校验；抽怪后 process_relics 会再次
            # 调用本校验，并按真实 enemies 集合完成存在性/存活性校验。
            deferred_enemy_ref = (
                not legal and isinstance(target_ref, str)
                and target_ref.startswith("enemy:") and target_ref[6:].isdigit()
            )
            if (not isinstance(decision, dict)
                    or (target_ref not in legal and not deferred_enemy_ref)):
                raise ValueError(f"烙痕钉必须显式选择敌方target_ref，可选{sorted(legal)}")
        # 2026-10-11 用户令：按 物品索引.md 重写【回锋刀】——索引只给了
        # 「每失去1点速度后对[目标]造成3伤害」与「[回始]对[目标]造成3×([速限]-当前速度)伤害」，
        # 没有战始效果。原先这里的「苍白之花已发动 ⇒ 回锋刀必须提交目标」是遗物之间的
        # 硬编码联动（AI_EXPERIENCE.md 规则18 禁止），已删除。

    def validate_round_start_relic_choices(self, choices: dict) -> None:
        if not isinstance(choices, dict):
            raise ValueError("relic_choices必须是对象")
        active = {r.name for r in self.state.relics if getattr(r, "enabled", True) and self.state.sealed_relics.get(r.name, 0) <= 0}
        player = self.state.player
        damage = 3 * max(0, player.speed_limit - player.current_speed) if player else 0
        if "回锋刀" in active and damage > 0:
            legal = [i for i, enemy in enumerate(self.state.enemies) if enemy.is_alive]
            if not legal:
                # 2026-10-14（修 bone_angel/wushen 红）：回始场上无存活敌人
                # （如增援/延迟回场未到）→ 无目标可打，不发 demand；否则引擎要求
                # 不可能的提交，整局被误标 invalid。
                return
            decision = choices.get("回锋刀")
            index = decision.get("enemy_index") if isinstance(decision, dict) else None
            if (not isinstance(index, int) or isinstance(index, bool) or index < 0
                    or index >= len(self.state.enemies) or not self.state.enemies[index].is_alive):
                raise ValueError(f"回锋刀触发时必须显式提交合法relic_choices.回锋刀.enemy_index，可选{legal}")
        # 2026-10-14 用户令：【余火印】整段删除（extra 方案废弃）——回始 use 决策取消。
        # 2026-10-12 用户令：【血契】删掉与道纹【透支】重合的「[回始]流血4X换X法力」，
        # 只保留「数值型代价可与朋友/员工平分」作为其主要功能。它因此不再是回始主动
        # 遗物，`血契`/`对手血契` 两个回始决策键随之取消（平分仍在各代价提交处走
        # cost_share_target_ref，不受影响）。

    def process_relics(self, trigger: str | TriggerTiming, ctx: dict = None) -> list:
        """遗物效果触发框架。可选效果只读取AI显式提交的ctx，不设置启发式默认值。"""
        timing_map = {
            TriggerTiming.BATTLE_START: "battle_start", TriggerTiming.BATTLE_END: "battle_end",
            TriggerTiming.ROUND_START: "round_start", TriggerTiming.ROUND_END: "round_end",
        }
        trigger = timing_map.get(trigger, trigger)
        ctx = ctx or {}
        player = self.state.player
        logs = []
        if not player:
            return logs
        # 抵扣X（F2）：被封印的遗物在封印期间不触发任何效果
        relics = {r.name for r in self.state.relics if getattr(r, "enabled", True) and self.state.sealed_relics.get(r.name, 0) <= 0}

        if trigger == "round_start":
            choices = ctx.get("relic_choices", {})
            self.validate_round_start_relic_choices(choices)
            if "回锋刀" in relics:
                d = 3 * max(0, player.speed_limit - player.current_speed)
                if d > 0 and any(enemy.is_alive for enemy in self.state.enemies):
                    enemy = self.state.enemies[choices["回锋刀"]["enemy_index"]]
                    self._remember_huifeng_target(player, f"enemy:{choices['回锋刀']['enemy_index']}")
                    self._apply_hostile_damage(enemy, d, source=player, ctx={
                        "timing": "round_start", "source": "回锋刀", "source_type": "relic",
                        "actor": player, "target": enemy, "owner": player,
                        "mechanic": "damage", "subtype": "round_start_gap", "amount": d,
                        "tags": {"relic", "round_start"},
                    })
                    logs.append(f"回锋刀：对{enemy.name}造{d}伤")
            # 2026-10-14 用户令：【余火印】整段删除（extra 方案废弃），本分支移除。
        if trigger == "battle_start":
            choices = ctx.get("relic_choices", {})
            self.validate_battle_start_relic_choices(choices)
            # 2026-10-13 用户令：【三相残韵盘】遗物删除（残韵生产改由道纹【典当】承接发现侧）。
            # 2026-10-14 用户令：【猩红果实】重做为纯被动（当前生命可超出[血限]），
            # 旧「战始流血10→战终血限+2」移交道纹【增殖】，本分支移除。
            # 机制系统：BATTLE_START 相位分发。位置即原缄默面具/帮派令结算位置
            # （缄默面具=5、帮派令=10 同相位按 priority 保持原序；负岳索之前）——顺序与迁移前一致。
            # 帮派令已迁移为声明层 Mechanism（engine/mechanisms/builtins.py）；
            # process_relics 只宣布时点，具体机制条件/效果都在声明层。
            logs.extend(self._dispatch_phase(Phase.BATTLE_START, target=player))
            refs = self._combat_entity_refs()
            if "负岳索" in relics:
                target = refs[choices["负岳索"]["target_ref"]]
                target.add_status(StatusEffect("负岳索", -1, 1, "负岳索"))
                logs.append(f"负岳索：保护{target.name}首次受伤")
            if "减速" in relics:
                # 【减速】（2026-10-25 遗物化）：战始所有敌人当前速度减半，
                # 走唯一失速入口，速度类遗物照常收到真实信号。
                for foe in self.state.get_all_enemy_side():
                    lost = foe.current_speed - math.ceil(foe.current_speed / 2)
                    if lost <= 0:
                        continue
                    self._lose_current_speed(foe, lost, ctx={
                        "timing": "battle_start", "source": "减速", "source_type": "relic",
                        "actor": player, "target": foe, "owner": player,
                        "mechanic": "speed_change", "subtype": "current_speed",
                        "amount": -lost, "tags": {"relic", "battle_start"},
                    })
                    logs.append(f"减速：{foe.name}当前速度-{lost}")
            if "炉心坠" in relics:
                heart = next(item for item in self.state.consumables
                             if item.name == choices["炉心坠"]["heart_name"] and item.kind == "dragon_heart")
                heart.current_uses += 10; heart.max_uses += 10
                logs.append(f"炉心坠：{heart.name}耐久+10")
            if "烙痕钉" in relics:
                self.state.event_modifiers["brand_nail_target_ref"] = choices["烙痕钉"]["target_ref"]
                logs.append("烙痕钉：已锁定目标")
            for ally in self.state.friends + self.state.employees:
                logs.extend(self._dispatch_phase(Phase.BATTLE_START_AFTER_CHOICES, target=ally))
        # 机制系统：BATTLE_END 相位分发。位置=战终遗物段顶部（2026-09-17 新增相位，
        # 首个注册者是改版后的【缄默面具】：[战终]法限+X）。
        # process_relics 只宣布时点，具体机制条件/效果都在声明层。
        if trigger == "battle_end":
            logs.extend(self._dispatch_phase(Phase.BATTLE_END, target=player))
        return logs

    # 2026-10-13 用户令：【买路财】遗物删除（撤退改制为轮回者【逃跑】行动，
    # 一次付清5次出手强制脱战；见 api._action_declare_escape）。原 buyaicai_escape_cost
    # 与 api._action_retreat_via_toll 一并移除。

    def apply_opposing_longxi(self, actor: Entity) -> Optional[dict]:
        """若对方持有龙息，actor 行动前受 10×当前回合必中伤害。"""
        if actor is None or not actor.is_alive:
            return None
        foe_has = False
        if self.state.on_player_side(actor):
            foe_has = "龙息" in self.state.opponent_dragon_traits
        elif self.state.on_enemy_side(actor):
            foe_has = "龙息" in self.state.dragon_traits
        if not foe_has:
            return None
        dmg = 10 * max(1, self.state.current_round)
        source = (self.state.player if self.state.on_enemy_side(actor)
                  else next((e for e in self.state.enemies if e.entity_type == "轮回者"), None))
        detail = self._apply_hostile_damage(actor, dmg, "必中", source, ctx={
            "timing": self._current_context_timing(), "source": "龙息", "source_type": "relic",
            "actor": source, "target": actor, "mechanic": "damage", "subtype": "dragon_breath",
            "amount": dmg, "tags": {"relic", "must_hit"},
        })
        detail["dragon_breath"] = dmg
        return detail

    def _mediocrity_battle_decided(self) -> bool:
        """凡庸中断判定：战斗胜负已定（统一判定 GameState.battle_over）。

        DM裁定（2026-08-18）：非轮回者优先炸裂后若战斗胜负已定，
        另一方尚未结算的凡庸不再触发。
        """
        return self.state.battle_over()

    def _tick_mediocrity_counters(self, entity: Entity) -> Optional[str]:
        """更新凡庸连续计数；达阈值返回原因，不立刻结算。"""
        if entity.actions_used_this_round <= 0:
            entity.no_action_rounds += 1
        else:
            entity.no_action_rounds = 0
        if entity.damage_dealt_this_round <= 0:
            entity.no_damage_rounds += 1
        else:
            entity.no_damage_rounds = 0
        if (entity.no_action_rounds >= MEDIOCRITY_ROUNDS
                or entity.no_damage_rounds >= MEDIOCRITY_ROUNDS):
            return ("连续五回合未出手" if entity.no_action_rounds >= MEDIOCRITY_ROUNDS
                    else "连续五回合未能使敌对角色生命减少")
        return None

    def _apply_mediocrity(self, entity: Entity, why: str) -> list[dict]:
        """结算一名角色的凡庸。调用方必须已按非轮回者优先排好序。"""
        if not entity.is_alive:
            return []
        # 【烬翼·熄事】：本回合不触发特殊事件（凡庸不结算，计数照常清零）。
        if self.state.suppresses_special_events(entity):
            entity.no_action_rounds = 0
            entity.no_damage_rounds = 0
            return [{"type": "mediocrity", "entity": entity.name, "suppressed": True,
                     "note": f"{entity.name}受到【熄事】保护，本次不触发【凡庸】"}]
        self._hp_loss_recording += 1  # 凡庸直接命零=特殊死因，不触发「失去生命后」
        try:
            entity.current_hp = 0
        finally:
            self._hp_loss_recording -= 1
        self._check_hp_zero_death(entity, ctx={
            "timing": "round_end", "source": "凡庸", "source_type": "system",
            "actor": entity, "target": entity, "mechanic": "death", "subtype": "mediocrity",
            "amount": 0, "tags": {"system", "round_end", "mediocrity"}})
        entity.no_action_rounds = 0
        entity.no_damage_rounds = 0
        if entity is self.state.player:
            self.state.last_death_cause = "mediocrity"
        effects = [{"type": "mediocrity", "entity": entity.name,
                    "note": f"{why}，触发【凡庸】：凭空全身炸裂，[命零]"}]
        if entity.entity_type == "怪物":
            # 2026-10-05 用户令：旧【残骸】（恢复20生命+异变10）与旧【自由结晶】
            # （喂3次触发救赎）合并重做为单一【自由结晶】，效果见 物品索引.md。
            self.state.consumables.append(
                Consumable(name="自由结晶",
                           effect=("对[目标]使用后使其随机付出一种代价，共计10；"
                                   "若目标是生命≤20%血限的怪物，强制触发救赎事件"),
                           current_uses=1, max_uses=1))
            effects.append({"type": "mediocrity_loot", "entity": entity.name,
                            "note": "轮回者获得消耗品【自由结晶】(1/1)"})
        return effects

    def can_act(self, entity: Entity, action_type: str | None = None) -> bool:
        """判断实体能否执行指定主动行动。

        【束缚】期间仅放行法术行动（瞬发施法或自定义法术定义）；不传行动类型时按
        普通行动判断，仍返回不可行动。被【封印】暂离者不在场上，任何行动都不可用。
        反应选择与自动触发不通过此主动行动门禁。
        """
        return (entity.is_alive
                and not self.state.is_sealed_away(entity)
                and (not entity.has_status("束缚")
                     or action_type in {"cast", "define_spell"}))

    def is_targetable(self, attacker: Entity, target: Entity) -> bool:
        """目标是否可被选中：非飞行者无法选中飞行目标。"""
        if self._is_flying(target):
            return self._is_flying(attacker)
        return True

    def _get_combat_state(self) -> dict:
        """获取当前战斗状态摘要（含封印暂离队列）。"""
        return {
            "round": self.state.current_round,
            "player_side": [e.to_dict() for e in self.state.get_all_player_side()],
            "enemy_side": [e.to_dict() for e in self.state.get_all_enemy_side()],
            "delayed_monster_reentries": [
                {"name": entry["monster"].name, "return_round": entry["return_round"],
                 "home": entry.get("home") or "enemies"}
                for entry in getattr(self.state, "delayed_monster_reentries", [])
            ],
        }
