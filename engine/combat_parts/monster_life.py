"""CombatEngine 分片：怪物困境/进化/多路径胜利（癌变·赎罪·雕塑·还债）/许愿/谈判；
含类级阈值常量（PROLIFERATION/CANCER/DEBT/SCULPTURE/REDEMPTION）

方法体自 engine/combat.py 原样搬来（逐字节），行为与对外契约不变；
CombatEngine 仍是唯一入口（门面类继承本 Mixin）。
"""
from __future__ import annotations
import math
import weakref
from typing import Optional, Any
from ..models import Entity, StatusEffect, GameState, DaoWenInstance, DaoWen, Spell, Consumable
from ..daowen import DaoWenEngine, ResonanceEngine
from ..dice import DiceEngine
from ..enums import (ActionPhase, TriggerTiming, InterruptType, DamageType,
                     EffectScope, EffectPolarity, CostType)
from ..dm_rulings import Interrupt
from ..combat_events import CombatEvent, CombatEventType, register_combat_event_observer
from ..combat_hooks import CombatHookManager
from ..effect_context import EffectContext, make_context, normalize_context
from ..mechanisms import MECHANISMS, Phase, TriggerBus, TriggerContext
from ..resolution import KIND_EFFECT, KIND_EVOLVE, resolution_frame
from ..personality import remove_personality
from ..models import MONSTER_MANA_RELIC
from ..rules_source import special_events


class MonsterLifeMixin:
    # ========== 困境检查 ==========
    
    def check_monster_difficulty(self, monster: Entity) -> Optional[dict]:
        """
        怪物困境检查
        规则：困境是指怪物的主要优势被针对、原有取胜路线被稳定限制
        核心道纹被变化或失效、连续无法有效攻击、对方建立稳定压制循环、
        被特殊手段破解主要优势时，均应立即进行困境检查
        
        返回None表示未陷入困境，返回dict表示需要DM裁定（进化/逃跑）
        """
        if not monster.is_alive:
            return None
        
        hp_ratio = monster.hp_ratio
        
        # 检查是否陷入困境
        difficulty_signals = []
        
        # 1. 生命低于30%
        if hp_ratio <= 0.3:
            difficulty_signals.append("生命低于30%")
        
        # 2. 被束缚/主动行动受限（仍可施放允许的法术）
        if monster.has_status("束缚"):
            difficulty_signals.append("被控制")
        
        # 3. 攻击力被弱化到极低
        if monster.attack_power <= 1:
            difficulty_signals.append("攻击力极低")
        
        # 4. 退化效果叠加
        if monster.has_status("退化"):
            difficulty_signals.append("道纹数值被退化")
        
        # 5. 定型阻止改变
        if monster.has_status("定型"):
            difficulty_signals.append("被定型无法改变属性")
        
        # 6. 坏死无法回复
        if monster.has_status("坏死"):
            difficulty_signals.append("无法获得回复")
        
        # 困境探针（裁定⑦ 2026-08-10）：≥1个劣势信号即判定困境
        # （原口径≥2，导致进化在模拟策略下结构性不可达：4574场埋点15314次检查，信号分布{0:14267,1:1047,≥2:0}）
        if len(difficulty_signals) >= 1:
            return {
                "monster": monster.name,
                "hp_ratio": round(hp_ratio, 2),
                "signals": difficulty_signals,
                "action_required": "进化或逃跑（二选一，本场战斗限一次）",
                "note": "困境检查以当前胜率为依据，而非当前生命"
            }
        
        return None
    
    # 2026-10-13 用户令：逃跑改制——【逃跑】是轮回者行动，一次付清5次出手、
    # 以当前状态强制脱离本场战斗，不再走DM追击裁定。原 initiate_escape（追击中断）
    # 随改制删除；declare_escape 的新实现见 engine/api.py。
    # ========== 进化（原初X，引擎直接结算，无需DM中断） ==========
    
    def execute_evolution(self, monster: Entity, daowen_name: str, x: int) -> dict:
        """【进化】的公开入口（开帧后转实现体 `_execute_evolution_impl`）。"""
        with resolution_frame(self, KIND_EVOLVE,
                              getattr(monster, "name", "?"), daowen_name):
            return self._execute_evolution_impl(monster, daowen_name, x)

    def _execute_evolution_impl(self, monster: Entity, daowen_name: str, x: int) -> dict:
        """
        特殊事件【进化】：怪物发动【原初X】（规则正文·特殊事件）。
        原初X：代价：异变5X。选择一种**当前轮回者已持有**、且自身未持有的道纹，
        [战终]前视为持有该道纹（其数值固定为本次X），借用的道纹发动时照常支付其自身代价。

        设计意图：借用对象改为"轮回者的道纹"而非固定的7种原始怪物道纹，
        使玩家的构筑本身成为风险来源——越依赖某条公式化路线，被复制时反噬越重，
        从而抑制"无脑最优解"。同时保持怪物与轮回者的身份区隔
        （怪物仍不持有法力/速度/残韵/局外阶段，只是临时借用道纹）。
        前置（规则正文·怪物准则·困境）：须处于困境；逃跑与进化二选一，每场战斗限一次。
        """
        if not monster.is_alive:
            return {"success": False, "error": f"{monster.name}已命零"}
        if id(monster) in self._monster_evolved:
            return {"success": False, "error": f"{monster.name}本场已选择过逃跑/进化（每场战斗限一次）"}
        difficulty = self.check_monster_difficulty(monster)
        if not difficulty:
            return {"success": False, "error": f"{monster.name}未陷入困境，不能进化"}
        player = self.state.player
        player_daowen = list(player.dao_wen.keys()) if player else []
        if daowen_name not in player_daowen:
            return {"success": False,
                    "error": f"【{daowen_name}】不在轮回者当前持有的道纹中，"
                             f"原初X只能借用：{'、'.join(player_daowen) if player_daowen else '（轮回者无道纹）'}"}
        if daowen_name in monster.dao_wen:
            return {"success": False, "error": f"{monster.name}已持有【{daowen_name}】，原初X只能借用自身未持有的原始怪物道纹"}
        if not isinstance(x, int) or isinstance(x, bool) or x < 1:
            return {"success": False, "error": "X必须为≥1的整数"}
        
        # 支付代价：异变5X（代价从做出选择开始生效，优先于效果结算）
        cost = self.YUANCHU_COST_RATE * x
        pay = monster.add_mutation(cost)
        self._monster_evolved.add(id(monster))
        log = [f"{monster.name}发动【原初{x}】：异变+{cost}（当前{pay['mutation_total']}层）"]
        
        # 怪物只累计异变；进化不会因异变达到50层而中断。

        # 借用：战终前视为持有（enemies于[战终]清空，借用自动到期）
        borrowed = DaoWen(
            name=daowen_name,
            formula=f"{daowen_name}X",
            cost_type="代价",
            cost_formula="异变5X",
            effect_formula="",
            is_monster_original=True,
            tags=["原初借用"],
        )
        monster.dao_wen[daowen_name] = DaoWenInstance(dao_wen=borrowed, x_value=x)
        log.append(f"{monster.name}[战终]前视为持有【{daowen_name}{x}】，发动时照常支付其自身代价")
        return {"success": True, "action": "进化·原初X", "collapsed": False,
                "borrowed": {"name": daowen_name, "x": x},
                "difficulty_signals": difficulty.get("signals", []),
                "log": log, "mutation": pay,
                "state": self._get_combat_state()}
    
    def get_plight_evolution_options(self) -> list[dict]:
        """
        供AI决策（事实源计算）：当前存活、处于困境、且本场未选择过逃跑/进化的怪物，
        及其【原初X】可用参数。规则正文·怪物准则·困境：陷入困境时强制逃跑/进化二选一，每场限一次；
        AI扮演怪物方，自行决定是否调用 declare_evolution 及参数。
        """
        options = []
        for m in self.state.enemies:
            if not m.is_alive or id(m) in self._monster_evolved:
                continue
            difficulty = self.check_monster_difficulty(m)
            if not difficulty:
                continue
            # None 表示异变不限制 X；不能伪造一个有限的迷失预算给免疫者。
            max_x = (max(0, (Entity.MUTATION_COLLAPSE_THRESHOLD - 1 - m.mutation_count)
                         // self.YUANCHU_COST_RATE) if m.can_trigger_mutation_lost() else None)
            options.append({
                "monster": m.name,
                "difficulty_signals": difficulty.get("signals", []),
                "mutation_layers": m.mutation_count,
                "max_x_by_mutation": max_x,
                "mutation_lost_exempt": not m.can_trigger_mutation_lost(),
                # 借用池 = 轮回者当前持有、且该怪物尚未持有的道纹
                "borrowable_daowen": [d for d in (self.state.player.dao_wen if self.state.player else {})
                                      if d not in m.dao_wen],
            })
        return options
    
    # ========== 多路径胜利系统 ==========
    # 所有阈值数值均为占位初值，需经测试调整（见 AI_EXPERIENCE.md）

    PROLIFERATION_THRESHOLD = Entity.CANCER_HEAL_MULTIPLIER  # 癌变：规则正文「累计恢复量达血限×2」；过量回复按原值计（阈值唯一事实源在 Entity，DM裁定2026-08-18）
    CANCER_THRESHOLD = PROLIFERATION_THRESHOLD  # 别名：增生旧名已统一为癌变，二者同阈值
    DEBT_THRESHOLD = special_events.threshold("huanzhai")  # 动态阈值基数10来自 TOML；实际阈值=基数+角色历史最高碎片余额
    # 雕塑数值唯一事实源在 data/rules/special_events.toml（与 Entity 阈值同口径接线）
    SCULPTURE_DAMAGE = special_events.damage_per_durability("diaosu")    # 每点耐久可造成的伤害
    SCULPTURE_SHIELD = special_events.shield_per_durability("diaosu")    # 每点耐久可获得的格挡

    def cancer_threshold_of(self, entity: Entity) -> int:
        """规则正文：累计恢复量达到血限×2（过量按原值计入 total_healed，双倍机制已删）。"""
        if entity.blood_limit <= 0:
            return 0
        return math.ceil(entity.blood_limit * self.PROLIFERATION_THRESHOLD)

    def check_cancer(self, entity: Entity) -> Optional[dict]:
        """任一角色恢复量达阈值即癌变。怪物仍吸收进书；轮回者/同伴直接命零。"""
        if entity is None or not entity.is_alive or entity.is_proliferated:
            return None
        # 【烬翼·熄事】：本回合不触发特殊事件（癌变在此拦下，进度照常累计）。
        if self.state.suppresses_special_events(entity):
            return None
        # 2026-09-23：【第一杯】重做，旧「持有者免疫癌变」条款废止（原为钱袋并入的效果）。
        # 现在持有者照样癌变——而且因为「受到的回复翻倍」，累计回复更快撞上 2×血限。
        # 新条文只有「受到的[回复]与失去的生命翻倍」，两条倍率的唯一事实源在
        # GameState.heal_multiplier / life_loss_multiplier。
        threshold = self.cancer_threshold_of(entity)
        if threshold <= 0 or entity.total_healed < threshold:
            return None
        parent_heal = None
        heal_events = getattr(entity, "_heal_events", []) or []
        if heal_events:
            parent_heal = normalize_context(heal_events[-1])
        cancer_ctx = make_context(
            timing=parent_heal.timing if parent_heal else self._current_context_timing(),
            source="癌变", source_type="system", actor=None, target=entity, owner=None,
            mechanic="cancer", subtype="heal_threshold", amount=entity.total_healed,
            tags={"threshold", "heal_listener"},
            parent_event_id=parent_heal.event_id if parent_heal else None,
        )
        if entity.entity_type == "怪物":
            return self._proliferate_monster(entity, ctx=cancer_ctx)
        return self._cancer_character(entity, ctx=cancer_ctx)

    REDEMPTION_HP_RATIO = 0.10

    def monster_has_original_daowen(self, monster: Entity) -> bool:
        from ..gamedata import ORIGINAL_MONSTER_DAOWEN
        return any(name in ORIGINAL_MONSTER_DAOWEN for name in monster.dao_wen)

    def redemption_hp_threshold(self, monster: Entity) -> int:
        if monster is None or monster.blood_limit <= 0:
            return 0
        return math.ceil(monster.blood_limit * self.REDEMPTION_HP_RATIO)

    def check_redemption(self, monster: Entity, *, force: bool = False) -> Optional[dict]:
        """救赎：通常要求残血且没有原始怪物道纹（全力／疯狂／减速／必中）；自由结晶可强制触发。"""
        if monster is None or monster.entity_type != "怪物" or not monster.is_alive:
            return None
        # 烬翼【熄事】本回合抑制所有落在该角色身上的特殊事件，救赎（含自由结晶强制救赎）也不例外。
        if self.state.suppresses_special_events(monster):
            return None
        if monster.is_sculptured or monster.is_proliferated or monster.is_debt_bound:
            return None
        if getattr(monster, "removed_without_kill", False):
            return None
        if self.state.pending_redemption:
            return None
        if not force and self.monster_has_original_daowen(monster):
            return None
        if not force and monster.current_hp > self.redemption_hp_threshold(monster):
            return None
        return self._queue_redemption(monster, "free_crystal" if force else "low_hp_no_original")

    def _queue_redemption(self, monster: Entity, cause: str) -> dict:
        """怪物融化离场，等待【接纳】或【终结】（2026-09-15 用户令，终结取代旧「无视」）。

        离场当时不产碎片；若玩家选【终结】，该怪物会被还原为一次正常[命零]，
        [战终]按普通击杀公式产出[碎片]；选【接纳】则成为待命员工，不产碎片。
        """
        snapshot = {
            "name": monster.name,
            "attack_count": monster.attack_count,
            "attack_power": monster.attack_power,
            "blood_limit": monster.blood_limit,
            "dao_wen": {name: inst.x_value for name, inst in monster.dao_wen.items()},
            "cause": cause,
            "mutation": monster.mutation_count,
        }
        self._remove_from_combat(monster, "救赎", ctx={
            "timing": self._current_context_timing(), "source": "救赎", "source_type": "system",
            "target": monster, "mechanic": "leave", "subtype": "redemption",
            "tags": {"leave", "no_shards"},
        })
        monster._redeemed = True
        self.state.pending_redemption = snapshot
        return {
            "type": "redemption",
            "monster": monster.name,
            "cause": cause,
            "note": (
                f"随着最后一缕恶意消散，{monster.name}的身躯开始融化，"
                "原地只剩下一个昏迷的微光者"
            ),
        }
    def _can_be_sculptured(self, entity: Entity) -> bool:
        """雕塑对任何角色生效（DM裁定 2026-09-09）。

        旧口径排除轮回者，理由是轮回者攻次/攻力恒为 0×0、没有普攻面板，
        「归 0」对他们没有意义。轮回者既有普攻（初始 1×1，属性点 1:1 追加），
        攻次/攻力归 0 就是真的失去攻击手段，与怪物同理，故不再排除。
        """
        return True

    def settle_victory_paths(self) -> list[dict]:
        """
        回终多路径胜利结算（依次检查：雕塑 / 癌变 / 还债）
        雕塑：任何非轮回者（怪物/微光者/赤族等），不视为击杀，不提供碎片。
        还债：怪物转为债务员工；轮回者、朋友、员工等其他角色被债务吞噬。
        癌变对任一角色生效。
        """
        results = []
        for monster in list(self.state.enemies):
            if not monster.is_alive or monster.is_sculptured \
                    or monster.is_proliferated or monster.is_debt_bound:
                continue
            # 【烬翼·熄事】：本回合不触发任何特殊事件（还债/救赎/癌变/雕塑一并跳过）。
            if self.state.suppresses_special_events(monster):
                continue

            # 1. 雕塑：攻击次数**和**攻击力都归0（DM裁定 2026-09-10，原为「之一归0」）
            if self._can_be_sculptured(monster) and (
                    monster.effective_attack_count() <= 0
                    and monster.effective_attack_power() <= 0):
                results.append(self._sculpture_monster(monster))
                continue

            # 2. 救赎：残血且没有原始怪物道纹（全力／疯狂／减速／必中）
            redemption = self.check_redemption(monster)
            if redemption:
                results.append(redemption)
                continue

            # 3. 癌变：累计受到恢复量达阈值
            cancer = self.check_cancer(monster)
            if cancer:
                results.append(cancer)
                continue

            # 3. 动态还债：M 为该角色本局历史最高真实碎片余额，阈值=10+M。
            debt_balance = monster.shards
            debt_peak = max(0, int(getattr(monster, "shards_peak", 0)))
            if debt_balance <= -(self.DEBT_THRESHOLD + debt_peak):
                if monster.entity_type == "怪物":
                    results.append(self._debt_bind_monster(monster))
                else:
                    monster.current_hp = 0
                    monster.depart_battle("还债")
                    results.append({"type": "debt_death", "entity": monster.name,
                                    "debt": -debt_balance,
                                    "threshold": self.DEBT_THRESHOLD + debt_peak,
                                    "note": f"{monster.name}碎片余额{debt_balance}触及动态还债线，已被吞噬并死亡"})
                continue

        seen = {id(e) for e in self.state.enemies}
        for ally in list(self.state.get_all_player_side()):
            if id(ally) in seen:
                continue
            if not ally.is_alive or ally.is_sculptured or ally.is_proliferated or ally.is_debt_bound:
                continue
            # 【烬翼·熄事】：本回合不触发任何特殊事件（含还债吞噬）。
            if self.state.suppresses_special_events(ally):
                continue
            debt_balance = self.state.shards if ally is self.state.player else ally.shards
            debt_peak = max(0, int(getattr(self.state, "shards_peak", 0) if ally is self.state.player
                                   else getattr(ally, "shards_peak", 0)))
            if debt_balance <= -(self.DEBT_THRESHOLD + debt_peak):
                ally.current_hp = 0
                ally.depart_battle("还债")
                results.append({"type": "debt_death", "entity": ally.name,
                                "debt": -debt_balance,
                                "threshold": self.DEBT_THRESHOLD + debt_peak,
                                "note": f"{ally.name}碎片余额{debt_balance}触及动态还债线，被祭坛嚼碎并死亡"})
                continue
            if (self._can_be_sculptured(ally)
                    and ally.effective_attack_count() <= 0
                    and ally.effective_attack_power() <= 0):
                results.append(self._sculpture_monster(ally))
                continue
            cancer = self.check_cancer(ally)
            if cancer:
                results.append(cancer)
        return results

    def _remove_from_combat(
        self, monster: Entity, reason: str = "离场",
        ctx: Optional[EffectContext | dict] = None,
    ):
        """将怪物移出战斗（不视为击杀）——统一走【离场】。"""
        parent = normalize_context(ctx)
        leave_ctx = make_context(
            timing=parent.timing if parent else self._current_context_timing(),
            source=reason, source_type="system", actor=parent.actor if parent else None,
            target=monster, owner=parent.owner if parent else None,
            mechanic="leave", subtype=reason, amount=0,
            tags=(set(parent.tags) if parent else set()) | {"leave", "no_shards"},
            parent_event_id=parent.event_id if parent else None,
        )
        monster._leave_ctx = leave_ctx.to_dict()
        monster.depart_battle(reason)

    def _delay_monster_reentry(self, monster: Entity, delay_rounds: int) -> dict:
        """【封印X】让一个仍存活的目标暂离，按当前回合+X在[回始]原样回场。

        这不是命零，也不是 Entity.depart_battle() 意义上的永久离场：对象从场上
        暂时移入专门队列，回场后仍沿用原生命、状态、法力和碎片，并可正常被击杀。

        2026-10-08 用户令：目标不再限于怪物——[朋友]/[员工]/临时朋友/敌对轮回者、
        乃至施法者自己都可以被封印。脱场/回场因此统一走 `GameState.detach_from_field`
        与 `GameState.reattach_to_field` 这对接口，由 home/index 记住原址：
        不能一律塞回 enemies，否则封印自己的[朋友]会把它变成敌人、封印自己会把
        玩家变成敌人。玩家（state.player 是单字段）摘不走，用 `_delayed_by_seal`
        标记表达"不在场上"，回场时清标记。
        """
        delay_rounds = max(1, int(delay_rounds))
        return_round = self.state.current_round + delay_rounds
        home, index = self.state.detach_from_field(monster)
        # 暂离单位仍然是活的；只是暂时不在场上（enemies/friends/… 列表或玩家位）。
        monster.is_alive = True
        monster.is_departed = False
        monster.departure_reason = ""
        monster.removed_without_kill = False
        monster._delayed_by_seal = True
        entry = {"monster": monster, "return_round": return_round,
                 "delay_rounds": delay_rounds, "home": home, "index": index}
        self.state.delayed_monster_reentries.append(entry)
        return {"monster": monster.name, "return_round": return_round,
                "delay_rounds": delay_rounds, "home": home, "index": index}

    # 2026-10-08 起的正式名字：封印的目标已不限于怪物。旧名保留为别名，
    # 既有调用方与测试（test_effect_composition / test_resolution_fuzz）仍在用。
    _delay_entity_reentry = _delay_monster_reentry


    def _cancer_character(self, entity: Entity, ctx: Optional[EffectContext | dict] = None) -> dict:
        """轮回者/同伴癌变：累计恢复达血限×2 → 直接命零。不吸收进书、不产生奖励。"""
        cancer_ctx = normalize_context(ctx)
        entity.is_proliferated = True
        entity.is_cancer = True
        self._hp_loss_recording += 1  # 癌变直接命零=特殊死因，不触发「失去生命后」
        try:
            entity.current_hp = 0
        finally:
            self._hp_loss_recording -= 1
        if entity is self.state.player:
            self.state.last_death_cause = "cancer"
        self._check_hp_zero_death(entity, ctx=cancer_ctx)
        return {
            "type": "cancer",
            "type_alias": "proliferation",
            "entity": entity.name,
            "entity_type": entity.entity_type,
            "absorbed_heal": entity.total_healed,
            "threshold": self.cancer_threshold_of(entity),
            "ctx": cancer_ctx.to_dict() if cancer_ctx else None,
            "note": f"{entity.name}累计承受{entity.total_healed}点恢复，触发【癌变】：直接[命零]",
        }

    def _sculpture_monster(self, monster: Entity) -> dict:
        """雕塑：任一角色攻击次数和攻击力同时归0→化为雕塑消耗品（耐久=血限5%）"""
        if self.state.suppresses_special_events(monster):
            # 【烬翼·熄事】：本回合不触发特殊事件；雕塑不成立，角色留在场上。
            return {"type": "sculpture", "monster": monster.name, "suppressed": True,
                    "note": f"{monster.name}受到【熄事】保护，本次不触发【雕塑】"}
        # 耐久 = ceil([血限] × durability_ratio)，至少 durability_min（事实源见 toml）
        durability = max(special_events.durability_min("diaosu"),
                         math.ceil(monster.blood_limit * special_events.durability_ratio("diaosu")))
        count_zero = monster.effective_attack_count() <= 0
        power_zero = monster.effective_attack_power() <= 0
        if count_zero and power_zero:
            reason = "攻击次数和攻击力归0"
        elif count_zero:
            reason = "攻击次数归0"
        elif power_zero:
            reason = "攻击力归0"
        else:
            reason = "攻击手段归0"
        monster.is_sculptured = True
        self._remove_from_combat(monster, "雕塑", ctx={
            "timing": self._current_context_timing(), "source": "雕塑", "source_type": "system",
            "target": monster, "mechanic": "leave", "subtype": "sculpture", "tags": {"leave", "no_shards"},
        })
        consumable = Consumable(
            name=f"{monster.name}雕塑",
            effect=(f"每消耗1点耐久，对1个目标造成{self.SCULPTURE_DAMAGE}点伤害，"
                    f"或使自身获得{self.SCULPTURE_SHIELD}点格挡"),
            current_uses=durability,
            max_uses=durability,
            kind="sculpture",
        )
        self.state.consumables.append(consumable)
        return {
            "type": "sculpture",
            "monster": monster.name,
            "reason": reason,
            "consumable": consumable.name,
            "durability": durability,
            "note": (f"{monster.name}{reason}，化为雕塑【{consumable.name}】（{durability}/{durability}）"),
        }

    def _proliferate_monster(self, monster: Entity, ctx: Optional[EffectContext | dict] = None) -> dict:
        """癌变：累计受到恢复量达阈值→吸收进死者之书，获得【无限肉块】（旧名 增生）。

        2026-10-05 用户令：癌变不再为【休整】提供永久恢复量加成（原每只+8），
        奖励仅保留【无限肉块】（1/1）；恢复量 5→10。
        """
        cancer_ctx = normalize_context(ctx)
        monster.is_proliferated = True
        # 兼容：同时写入癌变别名，便于外部以新名读取
        monster.is_cancer = True  # type: ignore[attr-defined]
        self._remove_from_combat(monster, "癌变", ctx=cancer_ctx)
        absorbed = monster.total_healed
        meat = Consumable(
            name="无限肉块",
            effect="使用后恢复10生命，不计入癌变累计治疗量；战终自愈80%已损耐久",
            current_uses=1,
            max_uses=1,
            kind="infinite_meat",
        )
        self.state.consumables.append(meat)
        return {
            "type": "proliferation",  # 保留旧 key 兼容；新 key 见下一行
            "type_alias": "cancer",
            "monster": monster.name,
            "absorbed_heal": absorbed,
            "consumable": meat.name,
            "consumable_uses": "1/1",
            "ctx": cancer_ctx.to_dict() if cancer_ctx else None,
            "note": (f"{monster.name}累计承受{absorbed}点恢复被癌变吸收进《死者之书》，"
                     "轮回者获得【无限肉块】（1/1）")
        }

    def _debt_bind_monster(self, monster: Entity) -> dict:
        """还债：负债达阈值→视为员工；负债还清后离开（走独立的负债经济轨道，不受出战支援/工资/黑名单约束）"""
        monster.is_debt_bound = True
        monster.is_departed = True
        monster.departure_reason = "还债"
        # 转为员工（保留当前面板），其待还负债记录于 shards（负值）。
        # 注意：还债者以员工身份继续参战，不置 is_alive=False，故不走 depart_battle，
        # 仅记录 is_departed/departure_reason 供战报分类；其已从 enemies 列表移除。
        monster.entity_type = "员工"
        monster.is_deployed = True  # "视为其参战"：立即出战，不需要玩家消耗出手派遣
        self.state.employees.append(monster)
        self.state.enemies.remove(monster)
        return {
            "type": "debt_bind",
            "monster": monster.name,
            "debt": -monster.shards,
            "note": (f"{monster.name}负债达{-monster.shards}，触发还债，视为[员工]参战；"
                     f"还清负债（支付{-monster.shards}碎片）后该员工离队"),
        }

    def use_sculpture(self, consumable: Consumable, target: Entity = None,
                      mode: str = "damage") -> dict:
        """
        使用雕塑：消耗1点耐久，造成15伤害或获得20格挡
        mode: "damage"(对target造伤) / "shield"(自身格挡)
        """
        if consumable.kind != "sculpture":
            return {"success": False, "error": "非雕塑消耗品"}
        if consumable.is_depleted:
            return {"success": False, "error": "雕塑已耗尽"}
        if mode not in ("damage", "shield"):
            return {"success": False, "error": "雕塑mode必须是damage或shield"}
        if mode == "damage" and target is None:
            return {"success": False, "error": "伤害模式需指定目标"}
        if mode == "shield" and self.state.player is None:
            return {"success": False, "error": "没有玩家，无法获得格挡"}
        consumable.use()
        if mode == "shield":
            player = self.state.player
            player.gain_shield(self.SCULPTURE_SHIELD)
            return {
                "success": True,
                "type": "sculpture_shield",
                "shield": self.SCULPTURE_SHIELD,
                "remaining": consumable.current_uses,
                "note": f"雕塑赋能：获得{self.SCULPTURE_SHIELD}点格挡",
            }
        else:
            dmg = self._apply_hostile_damage(target, self.SCULPTURE_DAMAGE, source=self.state.player, ctx={
                "timing": self._current_context_timing(), "source": "雕塑", "source_type": "consumable",
                "actor": self.state.player, "target": target, "mechanic": "damage", "subtype": "sculpture",
                "amount": self.SCULPTURE_DAMAGE, "tags": {"consumable", "sculpture"},
            })
            return {
                "success": True,
                "type": "sculpture_damage",
                "target": target.name,
                "damage": self.SCULPTURE_DAMAGE,
                "target_hp_after": dmg["hp_after"],
                "target_died": dmg["died"],
                "remaining": consumable.current_uses,
                "note": f"雕塑赋能：对{target.name}造成{self.SCULPTURE_DAMAGE}点伤害",
            }

    # 兼容旧接口名（降服已删，改为指代多路径胜利结算）
    def init_monster_shards(self, monster: Entity) -> int:
        """
        罪孽都市怪物[战始]自带碎片=其全部专属道纹数值之和×2
        其他副本怪物碎片默认0。返回初始化后的碎片数。
        """
        if self.state.current_region != "罪孽都市":
            return monster.shards
        exclusive = self.REGION_EXCLUSIVE_DAOWEN.get("罪孽都市", set())
        total = 0
        for name, inst in monster.dao_wen.items():
            if name in exclusive:
                total += getattr(inst, "x_value", 0) or 0
        monster.shards = total * 2
        return monster.shards
    # 一阶副本集合
    TIER1_REGIONS = {"罪孽都市", "扭曲都市", "龙心谷"}

    @classmethod
    def monster_spawn_count(cls, battle_number: int, region: str) -> int:
        """出怪数量=战斗场数；一阶副本直接-3，最低1（实测定值，原-2通关率仅6%）"""
        if region in cls.TIER1_REGIONS:
            return max(1, battle_number - 3)
        return max(1, battle_number)

    # ========== 许愿（2026-08-19 新增，替代急中生智） ==========

    def initiate_wish(self, wisher: Entity, wish_text: str, target: Optional[Entity] = None) -> Interrupt:
        """特殊事件【许愿】：轮回者向"某人"祈求时触发。

        规则（2026-08-19）：
        1. 轮回者许下一个愿望；愿望本身没有固定的可行范围，也不存在"无法实现"的愿望；
        2. "某人"会以能够实现愿望、但最符合其扭曲本质的方式实现愿望；
        3. 愿望的代价与扭曲方式由愿望本身决定，不预先公开；
        4. 引擎只负责抛出中断并提交现场状态，实现方式与代价完全由 DM 裁定。
        """
        ctx = {
            "wisher": wisher.name,
            "wish_text": wish_text,
            "target": target.name if target is not None else None,
            "wisher_hp": wisher.current_hp,
            "wisher_daowen": list(wisher.dao_wen.keys()),
            "current_round": self.state.current_round,
        }
        return Interrupt(
            interrupt_type=InterruptType.WISH,
            context=ctx,
            description=(
                f"{wisher.name}向「某人」许下一个愿望：「{wish_text}」\n\n"
                f"规则：\n"
                f"1. 愿望没有固定的可行范围，不存在「无法实现」的愿望；\n"
                f"2. 「某人」会以能够实现愿望、但最符合其扭曲本质的方式实现；\n"
                f"3. 愿望的代价与扭曲方式由愿望本身决定，不预先公开。\n\n"
                f"请DM裁定「某人」以何种扭曲方式实现该愿望、以及轮回者付出的代价。"
            ),
            options=[
                {"id": "wish_resolved", "label": "实现愿望（扭曲方式）",
                 "description": "「某人」以最符合其扭曲本质的方式实现愿望，代价由愿望本身决定"},
            ],
            state_snapshot=self.state.to_dict()
        )

