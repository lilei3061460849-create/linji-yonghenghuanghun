"""CombatEngine 分片：法术流程表、提交校验、自动反应、统一单步道纹结算。

架构：

    道纹是积木，法术是由道纹写成的程序。

    DSL 文本
      ↓ spell_dsl 解析（触发词/条件 AST/步骤 AST，含真循环）
    校验过的程序（SpellDefinition.body）
      ↓ 调用方提交"每一步的决策"（x / target_ref / dodge / 可选 max_iterations）
    SpellExecution（执行器）
      ↓ 一次一条指令，每步前后读当前 GameState
      ├ IfStep    → 执行到该步时按当前状态求值（可嵌套）
      └ LoopStep  → 执行器逐轮迭代，逐轮重读状态；调用方不再提交 cycles
      ↓
    _execute_single_daowen_step（唯一单步结算核心）
      ↓ 通过 _daowen_step_preflight 走与 use_daowen 同一批"发动道纹"前置环节
    结果（StepResult / 日志）

本文件里的三层职责必须分清：

1. **流程表**（SPELL_FLOWS / BUILTIN_SPELL_DAOWEN / _eligible_spell_flows）：
   定义"谁在什么时机可以发动什么法术"。
2. **提交契约**（prepare/validate 系列）：只校验结构（use/steps/x/target/dodge/
   max_iterations）。法力、速度、道纹可用性等一切"当前局面"问题都属于运行期，
   由执行器返回 interrupted 结果——契约错误与游戏中断不再混为一谈。
3. **结算**（resolve 系列 + _execute_single_daowen_step）：全部走同一个单步核心。

决策列表（steps）与程序里 ActionStep 的对应关系由 spell_dsl.iter_action_slots
唯一定义：按深度优先顺序覆盖**所有**槽位（含 if 的两个分支与循环体，各列一次）。
因此"校验时看到的步数"与"结算时的步数"永远一致——旧模型里条件分支在
prepare/validate/resolve 之间漂移的问题从结构上消失。
"""
from __future__ import annotations

from typing import Optional, Any

from ..models import Entity, Spell, DaoWenInstance
from ..daowen import DaoWenEngine
from ..gamedata import FIRST_EMBRACE_DAOWEN
from ..enums import ActionPhase, TriggerTiming, CostType
from ..effect_context import EffectContext, make_context, normalize_context
from ..spell_dsl import (
    ActionStep, IfStep, LoopStep, count_action_steps, evaluate_condition,
    iter_action_slots, collect_step_daowen,
)
from ..spell_execution import (
    ExecutionStatus, InterruptReason, Lifecycle, MAX_SPELL_LOOP_ITERATIONS,
    SpellCastRequest, SpellDefinition, SpellExecution, SpellStepPolicy, StepRequest,
    StepResult, StepStatus, TriggerType,
)


class SpellReactionMixin:
    SPELL_FLOWS = {
        "先发制人": {"trigger": ActionPhase.BEFORE_DAMAGE_TAKEN.value, "steps": [("杀伐", "attacker")]},
        "后发制人": {"trigger": ActionPhase.BEFORE_DAMAGE_TAKEN.value, "steps": [("庇护", "self")]},
        "生生不息": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("再生", "self")]},
        "以牙还牙": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("再生", "self"), ("杀伐", "attacker")]},
        "借力打力": {"trigger": ActionPhase.BEFORE_DAMAGE_TAKEN.value, "steps": [("庇护", "self"), ("杀伐", "attacker")]},
        "不死不休": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("连击", "attacker")], "loop": True},
        "千刀万剐": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("再生", "self"), ("连击", "attacker")], "loop": True},
        "咎由自取": {"trigger": "目标发动道纹前", "steps": [("杀伐", "target"), ("连击", "target")]},
        "镇魔印": {"trigger": TriggerTiming.SELF_TURN_END.value,
                   "steps": [("封印", "any")],
                   "effect_flow": "自身回合结束后→发动封印X于任意目标",
                   "automatic": True},
        # 血炼周天：
        # 失去生命后→发动再生→发动透支（循环）。
        # 【透支】的流血每轮都会推进【癌变】阈值，循环由规则自然终止
        # （施法者命零 / 法力耗尽 / 调用方提交的 max_iterations 到顶）。
        "血炼周天": {"trigger": ActionPhase.AFTER_LIFE_LOST.value,
                     "steps": [("再生", "self"), ("透支", "self")],
                     "effect_flow": "失去生命后→发动再生→发动透支→循环",
                     "loop": True},
        # 三阶动态循环：目标与中断条件由通用条件/目标选择器提供，
        # 不在法术名分支里写专用结算逻辑。
        "徒劳无功": {"trigger": ActionPhase.AFTER_LIFE_LOST.value,
                     "steps": [("再生", "self"), ("透支", "self"), ("弱化", "any")],
                     "effect_flow": "失去生命后→发动再生X于自身→发动透支X于自身→发动弱化X于任意目标→循环",
                     "loop": True,
                     "loop_stop_conditions": ["自身场上最高怪物攻击力等于0", "自身癌变安全余量小于等于0"]},
    }

    # 内置法术的所需道纹（唯一事实源；api.GameEngine.SPELL_REGISTRY 是本表的别名）。
    BUILTIN_SPELL_DAOWEN = {
        "先发制人": ["杀伐"],
        "后发制人": ["庇护"],
        "生生不息": ["再生"],
        "以牙还牙": ["杀伐", "再生"],
        "借力打力": ["杀伐", "庇护"],
        "不死不休": ["连击"],
        "千刀万剐": ["连击", "再生"],
        "咎由自取": ["杀伐", "连击"],
        "镇魔印": ["封印"],
        "血炼周天": ["再生", "透支"],
        "徒劳无功": ["再生", "透支", "弱化"],
    }

    # 一步 = 一次"发动道纹"。两类策略的唯一差异是**是否开启新的
    # 「目标发动道纹前」反应窗口**（反应链只允许一层）：
    #   主动施法（cast）走 CAST_STEP_POLICY：与 use_daowen 完全同口径；
    #   事件型法术（反应/道纹前/全局）走 EVENT_STEP_POLICY：一步照常按
    #   "发动道纹"的前置环节结算（飞行/缄默面具/施法者死亡/碎片代价），
    #   但不再开新窗口，避免 A→B→A 无限递归。
    CAST_STEP_POLICY = SpellStepPolicy(declares_daowen=True,
                                       allow_trigger_reactions=True,
                                       trigger_choices_required=True)
    EVENT_STEP_POLICY = SpellStepPolicy(declares_daowen=True,
                                        allow_trigger_reactions=False,
                                        trigger_choices_required=False)

    # 已接线清单（11 项）。DSL 词汇表里的“敌回始”见下方注释：
    # 全引擎没有结算点，学习仍成功但如实标注 wired=False，不列入本清单。
    _WIRED_TRIGGERS = (
        ActionPhase.BEFORE_DAMAGE_TAKEN.value,
        ActionPhase.AFTER_LIFE_LOST.value,
        "目标发动道纹前",
        TriggerTiming.BATTLE_START.value,
        TriggerTiming.BATTLE_END.value,
        TriggerTiming.ROUND_START.value,
        TriggerTiming.ROUND_END.value,
        TriggerTiming.SELF_TURN_END.value,
        # 2026-10-02 审计：ENEMY_ROUND_START（"敌回始"）当前全引擎**没有结算点**
        # ——prepare_monster_phase 结算的是 SELF_TURN_END（"自身回合结束"，同一时刻
        # 的玩家侧视角），resolve_monster_phase 结算 ENEMY_ROUND_END。把它留在
        # 已接线清单里会让「敌回始」法术被静默标注为"会真实触发"却永不结算。
        # 故从清单移除：学习仍成功，但如实标注"该时机暂未接入战斗结算管线"。
        TriggerTiming.ENEMY_ROUND_END.value,
        ActionPhase.AFTER_DAMAGE_TAKEN.value,
        ActionPhase.BEFORE_LIFE_LOST.value,
    )

    # 具名死因：这些 mechanic 的 ctx 虽不是 mechanic="death"，但同样是调用方
    # **显式**给出的死因，必须原样留在 _death_ctx 里。
    NAMED_DEATH_MECHANICS = {
        "cancer": "cancer",
        "proliferation": "cancer",
    }

    # ==================================================================
    # 一、流程表：内置法术按道纹实时推导，自创法术读实体 spells
    # ==================================================================

    def _builtin_spell_flows(self, holder: Entity) -> dict[str, dict]:
        """当前已定义且可触发的法术大全条目。

        法术不再有“装配”状态；预定义法术与自定义法术统一存入
        ``holder.spells``，自定义施法定义后立即生效。
        """
        flows: dict[str, dict] = {}
        if holder is None or not holder.is_alive:
            return flows
        for spell in getattr(holder, "spells", []) or []:
            flow = self.SPELL_FLOWS.get(spell.name)
            if flow is None:
                continue
            if all(d in holder.dao_wen and holder.dao_wen[d].can_use()
                   for d in spell.required_daowen):
                flows[spell.name] = flow
        return flows

    def buildable_spells(self, holder: Entity) -> list[str]:
        """当前凭持有道纹可以通过自定义施法定义的法术大全条目。"""
        if holder is None or not holder.is_alive:
            return []
        defined = {sp.name for sp in getattr(holder, "spells", []) or []}
        out = []
        for name, required in self.BUILTIN_SPELL_DAOWEN.items():
            if name in defined or name not in self.SPELL_FLOWS:
                continue
            if all(d in holder.dao_wen and holder.dao_wen[d].can_use()
                   for d in required):
                out.append(name)
        return sorted(out)

    def spell_definition(self, holder: Entity, name: str):
        """取一个法术的定义：自创法术读实体 spells，内置法术按道纹即时合成。"""
        if holder is None or not name:
            return None
        spell = next((sp for sp in holder.spells if sp.name == name), None)
        if spell is not None:
            return spell
        required = self.BUILTIN_SPELL_DAOWEN.get(name)
        flow = self.SPELL_FLOWS.get(name)
        if required is None or flow is None:
            return None
        role_text = {"self": "自身", "attacker": "攻击者", "target": "目标", "any": "任意目标"}
        effect_flow = "→".join(
            f"发动{daowen}X于{role_text.get(role, role)}"
            for daowen, role in flow.get("steps", [])
        )
        if flow.get("loop"):
            effect_flow += "→循环"
        return Spell(
            name=name,
            required_daowen=list(required),
            trigger_condition=flow.get("trigger", ""),
            effect_flow=effect_flow,
            rank=len(required),
            automatic=bool(flow.get("automatic")),
        )

    def _parse_custom_spell(self, spell) -> Optional[dict]:
        """把自创法术的文本解析为流程 dict（含 LoopStep 的真程序）；失败返回 None。"""
        from ..spell_dsl import parse_spell_definition, SpellDslError
        try:
            parsed = parse_spell_definition(
                spell.trigger_condition or "", spell.effect_flow or "",
                set(DaoWenEngine.list_all()))
        except SpellDslError:
            return None
        return {"trigger": parsed.trigger, "steps": parsed.steps, "loop": parsed.loop,
                "dsl": True}

    def _eligible_spell_flows(self, holder: Entity, trigger: str) -> dict[str, dict]:
        """某角色在某触发时机可发动的全部法术（内置按道纹、自创读 spells）。"""
        flows = {}
        if holder is None or not holder.is_alive:
            return flows
        # 2026-10-12 用户令：【必中】X =「你选中的目标无法进行回合外行动，持续X」。
        # 反应型法术就是最典型的回合外行动，故在此统一拦掉——被封锁者连提案都没有，
        # prepare 侧也就不会把它列进可选反应。新增反应窗口自动继承这道闸。
        if self.out_of_turn_blocked(holder):
            return flows
        for name, flow in self._builtin_spell_flows(holder).items():
            if flow.get("trigger") == trigger:
                flows[name] = flow
        for spell in holder.spells:
            flow = self.SPELL_FLOWS.get(spell.name)
            if flow is None:
                flow = getattr(spell, "_parsed_flow", None)
                if flow is None:
                    flow = self._parse_custom_spell(spell)
                    if flow is None:
                        continue  # 解析失败=违规或格式错，不触发
                    spell._parsed_flow = flow
            if (flow["trigger"] == trigger
                    and all(name in holder.dao_wen and holder.dao_wen[name].can_use()
                            for name in spell.required_daowen)):
                flows[spell.name] = flow
        return flows

    # ==================================================================
    # 二、程序与步骤：程序表示的唯一规范化点
    # ==================================================================

    @staticmethod
    def _step_role(step) -> str:
        """统一取出一个步骤声明的目标身份：self/attacker/target/caster/any。"""
        if isinstance(step, ActionStep):
            return step.target
        return step[1]

    @staticmethod
    def _step_daowen(step) -> str:
        if isinstance(step, ActionStep):
            return step.daowen
        return step[0]

    @staticmethod
    def _flow_program(flow: dict) -> list:
        """流程 dict → 可执行程序（唯一规范化点）。

        - 内置流程表用 (道纹, 身份) 元组写步骤，这里统一转成 ActionStep；
        - 内置流程表用 {"steps": [...], "loop": True} 表达"整体循环"，
          DSL 解析产出的自创流程已经把循环写成了 LoopStep（可能带定次），
          这里只给前者补上 LoopStep。

        规范化之后，执行器/校验/schema 只面对一种程序表示（ActionStep /
        IfStep / LoopStep），不存在第二套步骤语义。
        """
        steps = []
        for raw in flow.get("steps", []):
            if isinstance(raw, (tuple, list)) and len(raw) == 2:
                steps.append(ActionStep(daowen=raw[0], target=raw[1]))
            else:
                steps.append(raw)
        if flow.get("loop") and not any(isinstance(s, LoopStep) for s in steps):
            return [LoopStep(body=tuple(steps))]
        return steps

    def _spell_definition_from_flow(self, name: str, flow: dict,
                                    trigger_str: str) -> SpellDefinition:
        program = self._flow_program(flow)
        trigger = next((t for t in TriggerType if t.value == trigger_str), None)
        if trigger is None:
            trigger = TriggerType.AFTER_LIFE_LOST
        required = list(flow.get("required_daowen") or []) or sorted(
            collect_step_daowen(program))
        lifecycle = Lifecycle.PERMANENT
        if flow.get("lifecycle"):
            try:
                lifecycle = Lifecycle(flow["lifecycle"])
            except ValueError:
                lifecycle = Lifecycle.PERMANENT
        return SpellDefinition(
            name=name, required_daowen=required, trigger=trigger,
            lifecycle=lifecycle, body=program, rank=len(required),
            automatic=bool(flow.get("automatic")),
            effect_flow_text=flow.get("effect_flow", ""),
            loop_stop_conditions=list(flow.get("loop_stop_conditions") or []),
        )

    def _resolve_step_subject(self, role: str, holder: Entity, attacker: Entity,
                              caster: Optional[Entity] = None) -> Optional[Entity]:
        """把 self/attacker/target/caster 映射为具体实体（"any" 由提交方显式挑选）。"""
        if role in ("self", "target"):
            return holder
        if role == "attacker":
            return attacker
        if role == "caster":
            return caster if caster is not None else holder
        return None

    def _trigger_spell_subject(self, role: str, holder: Entity, actor: Entity) -> Optional[str]:
        """「目标发动道纹前」触发语境下的身份映射。

        人话语义是"当[actor]即将发动道纹时，[holder]的反应法术触发"：actor 相当于
        其它触发点里的 attacker（对 holder 而言的外部行动方），因此 attacker/target
        两种写法都指向 actor，self/caster 指向 holder 自己。
        """
        if role in ("attacker", "target"):
            return "actor"
        if role in ("self", "caster"):
            return "holder"
        if role == "any":
            return "any"
        return None

    def _condition_resolver(self, holder: Entity, attacker: Entity):
        """构造 spell_dsl.evaluate_condition 需要的 resolver 闭包。"""
        subjects = {"self": holder, "attacker": attacker, "target": holder, "caster": holder}

        def _resolve(subject: str, field):
            entity = subjects.get(subject)
            if entity is None:
                raise ValueError(f"条件里的主语{subject}在当前场景下不存在")
            if isinstance(field, tuple) and field[0] == "status":
                return entity.has_status(field[1])
            if isinstance(field, tuple) and field[0] == "daowen_stacks":
                inst = entity.dao_wen.get(field[1])
                return inst.x_value if inst else 0
            if isinstance(field, tuple) and field[0] == "battle_metric":
                monsters = [m for m in self.state.active_enemies()
                             if getattr(m, "entity_type", "") == "怪物"]
                powers = [m.effective_attack_power() for m in monsters]
                if field[1] == "max_monster_attack_power":
                    return max(powers, default=0)
                if field[1] == "sum_monster_attack_power":
                    return sum(powers)
                if field[1] == "cancer_safety_margin":
                    margins = [max(0, m.blood_limit * Entity.CANCER_HEAL_MULTIPLIER
                                   - m.total_healed) for m in monsters]
                    return min(margins, default=0)
                raise ValueError(f"未知战斗级条件指标: {field[1]}")
            mapping = {
                "hp": entity.current_hp, "blood_limit": entity.blood_limit,
                "mana": entity.current_mana, "mana_limit": entity.mana_limit,
                "speed": entity.current_speed, "speed_limit": entity.speed_limit,
                "shield": entity.shield,
            }
            return mapping[field]
        return _resolve

    def spell_loop_guard(self, definition, caster: Entity, attacker: Entity):
        """通用循环守卫：由法术定义声明停止条件，执行器统一调用。"""
        from ..spell_dsl import parse_condition, evaluate_condition, SpellDslError
        for text in getattr(definition, "loop_stop_conditions", ()) or ():
            try:
                if evaluate_condition(parse_condition(text), self._condition_resolver(caster, attacker)):
                    return True, f"loop_condition:{text}"
            except SpellDslError as exc:
                return True, f"invalid_loop_condition:{exc}"
        return False, ""

    def _evaluate_spell_condition(self, node, caster: Entity, attacker: Entity) -> bool:
        """SpellExecution 在**执行到条件步骤的那一刻**求值条件（Part 3）。

        条件表达式里的 self/caster/target 都指向施法者，attacker 指向本次触发
        的对手身份；求值失败会由执行器记为 FAILED（程序错误），不伪装成中断。
        """
        return bool(evaluate_condition(node, self._condition_resolver(caster, attacker)))

    def _predict_flat_steps(self, steps, holder: Entity, attacker: Entity) -> list:
        """按**当前**状态预测条件分支会走到哪些步骤。

        仅用于引擎自动选择（_auto_after_life_lost_decision）时的 X 预算与"是否
        放弃触发"判断；执行期的分支选择永远由 SpellExecution 在执行到该步时
        自行求值，本方法不产出也没有能力产出"执行用的步骤列表"。
        """
        resolver = self._condition_resolver(holder, attacker)
        flat = []
        for step in steps:
            if isinstance(step, IfStep):
                branch = step.then_steps if evaluate_condition(step.condition, resolver) else step.else_steps
                flat.extend(self._predict_flat_steps(branch, holder, attacker))
            elif isinstance(step, LoopStep):
                flat.extend(self._predict_flat_steps(step.body, holder, attacker))
            else:
                flat.append(step)
        return flat

    @property
    def _branch_owner_token(self) -> str:
        """条件分支冻结的归属标记（字符串，随存档可序列化）。

        冻结只发生在"同一份提交在多次命中中重复结算"时：第一次结算做出的分支
        选择写进调用方那份 decision dict（branch_snapshot），后续命中复用，
        不会因为前一次命中改变了法力/生命而在两次命中之间漂移。
        标记必须是字符串——此前的引擎活引用会随 params 进 action_history
        并让 save_game 的 pickle 直接失败。
        """
        token = getattr(self, "_branch_owner_token_value", None)
        if token is None:
            token = f"combat-{id(self):x}"
            self._branch_owner_token_value = token
        return token

    # ==================================================================
    # 三、schema：把程序的每个决策槽位列给决策方
    # ==================================================================

    def _slot_schema(self, flow: dict, holder: Entity, subject_of) -> list[dict]:
        """列出程序里全部决策槽位（含 if 两分支与循环体，各列一次）。

        subject_of(step) -> ("self"|"attacker"|"target"|"caster"|"any"|..., entity|None)
        由各触发路径给出：反应/全局按角色，道纹前按 _trigger_spell_subject。
        """
        refs = self._combat_entity_refs()
        reverse = {id(entity): ref for ref, entity in refs.items()}
        out = []
        for idx, step, optional in iter_action_slots(self._flow_program(flow)):
            daowen = self._step_daowen(step)
            subject, entity = subject_of(step)
            entry: dict[str, Any] = {
                "slot": idx, "daowen": daowen,
                "x": "positive integer", "dodge": "boolean; 敌对目标必填",
                # optional=True 表示该步在条件分支里，本次执行不一定会走到；
                # 但决策列表仍必须为它留一条（校验与结算看到同一组槽位）。
                "optional": optional,
            }
            if subject == "any":
                entry["target_ref"] = None
                entry["target_options"] = [
                    ref for ref, e in refs.items() if self.is_targetable(holder, e)
                ]
            elif entity is None:
                entry["target_ref"] = None
            else:
                entry["target_ref"] = reverse.get(id(entity))
            out.append(entry)
        return out

    def _reaction_subject_of(self, holder: Entity, attacker: Entity):
        def subject_of(step):
            role = self._step_role(step)
            if role == "any":
                return "any", None
            return role, self._resolve_step_subject(role, holder, attacker)
        return subject_of

    def _daowen_trigger_subject_of(self, holder: Entity, actor: Entity):
        def subject_of(step):
            subject = self._trigger_spell_subject(self._step_role(step), holder, actor)
            if subject == "any":
                return "any", None
            if subject == "actor":
                return "attacker", actor
            return "self", holder
        return subject_of

    def _global_subject_of(self, holder: Entity):
        def subject_of(step):
            role = self._step_role(step)
            if role == "any":
                return "any", None
            # 全局时点只有 self/caster/any（DSL 层已保证）。
            return "self", holder
        return subject_of

    def _spell_schema_entry(self, name: str, flow: dict, holder: Entity, subject_of) -> dict:
        return {
            "spell_name": name,
            "steps": self._slot_schema(flow, holder, subject_of),
            "loop": bool(flow.get("loop")) or any(
                isinstance(s, LoopStep) for s in flow.get("steps", [])),
            "max_iterations": "optional positive integer; 省略=按规则循环到不能继续",
        }

    # ==================================================================
    # 四、提交契约：只校验结构，不替执行器决定资源
    # ==================================================================

    def _validate_decision(self, *, spell_name: str, flow: dict, holder: Entity,
                           attacker: Entity, refs: dict[str, Entity], decision: Any,
                           subject_of) -> None:
        """结构校验。任何"当前局面"问题（法力/速度/道纹可用性）都不在这里判。"""
        if not isinstance(decision, dict) or not isinstance(decision.get("use"), bool):
            raise ValueError(f"法术{spell_name}必须显式提交use布尔值")
        if not decision["use"]:
            return
        if "cycles" in decision:
            raise ValueError(
                f"法术{spell_name}的提交契约已更新：循环由执行器逐轮结算，"
                f"请提交steps（程序里每个决策槽位一条），不要再提交cycles")
        program = self._flow_program(flow)
        expected_slots = count_action_steps(program)
        steps = decision.get("steps")
        if not isinstance(steps, list) or len(steps) != expected_slots:
            raise ValueError(
                f"法术{spell_name}必须完整提交{expected_slots}步的steps"
                f"（程序含条件分支时，两个分支的步骤都要提交，顺序见spell_options）")
        max_it = decision.get("max_iterations")
        if max_it is not None and (not isinstance(max_it, int) or isinstance(max_it, bool)
                                   or max_it < 1 or max_it > MAX_SPELL_LOOP_ITERATIONS):
            raise ValueError(
                f"法术{spell_name}的max_iterations必须是1..{MAX_SPELL_LOOP_ITERATIONS}"
                f"之间的整数（省略=按规则循环）")
        snapshot = decision.get("branch_snapshot")
        if snapshot is not None:
            if not isinstance(snapshot, dict) or \
                    decision.get("_engine_branch_owner") != self._branch_owner_token:
                raise ValueError(f"法术{spell_name}包含未经引擎冻结的条件分支选择，拒绝执行")
        reverse = {id(entity): ref for ref, entity in refs.items()}
        for (idx, step, _optional), entry in zip(iter_action_slots(program), steps):
            if not isinstance(entry, dict):
                raise ValueError(f"法术{spell_name}第{idx + 1}步决策必须是对象")
            x = entry.get("x")
            if not isinstance(x, int) or isinstance(x, bool) or x < 1:
                raise ValueError(f"法术{spell_name}第{idx + 1}步x必须是≥1整数")
            subject, entity = subject_of(step)
            daowen = self._step_daowen(step)
            if subject == "any":
                target = refs.get(entry.get("target_ref"))
                if target is None or not self.is_targetable(holder, target):
                    raise ValueError(
                        f"法术{spell_name}第{idx + 1}步（{daowen}）的任意目标"
                        f"target_ref不是当前合法/可选中实体")
            else:
                target = entity
                expected_ref = reverse.get(id(entity)) if entity is not None else None
                given = entry.get("target_ref")
                if given is not None and given != expected_ref:
                    raise ValueError(
                        f"法术{spell_name}第{idx + 1}步（{daowen}）的target_ref={given}"
                        f"与流程声明的目标身份不符（期望{expected_ref}）")
            # dodge：敌对目标必须显式提交布尔值；非敌对步骤缺省即 False
            # （与旧契约"boolean if hostile"一致，不扩大外部提交负担）。
            given_dodge = entry.get("dodge")
            hostile = (target is not None and
                       self.state.on_player_side(holder) != self.state.on_player_side(target))
            if given_dodge is not None and not isinstance(given_dodge, bool):
                raise ValueError(f"法术{spell_name}第{idx + 1}步dodge必须是布尔值")
            if hostile and given_dodge is None:
                raise ValueError(f"法术{spell_name}第{idx + 1}步为敌对目标，必须显式提交dodge")

    def _decision_to_requests(self, decision: dict) -> list[StepRequest]:
        out = []
        for entry in decision.get("steps") or []:
            if not isinstance(entry, dict):
                continue
            out.append(StepRequest(
                x=entry.get("x", 0),
                target_ref=entry.get("target_ref"),
                dodge=bool(entry.get("dodge")),
                dodge_relic_target_ref=entry.get("dodge_relic_target_ref"),
                extra=dict(entry.get("extra") or {}),
            ))
        return out

    def _run_flow_spell(self, *, spell_name: str, flow: dict, trigger_str: str,
                        holder: Entity, attacker: Entity, refs: dict[str, Entity],
                        decision: dict, policy: SpellStepPolicy,
                        trigger_label: str = "") -> SpellExecution:
        """把一份已通过结构校验的提交交给执行器（控制流完全由执行器拥有）。"""
        definition = self._spell_definition_from_flow(spell_name, flow, trigger_str)
        snapshot = decision.get("branch_snapshot")
        live_snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
        request = SpellCastRequest(
            use=True, steps=self._decision_to_requests(decision),
            branch_snapshot=live_snapshot,
            max_iterations=decision.get("max_iterations"),
            source="engine" if decision.get("_engine_automatic") else "external",
        )
        execution = SpellExecution(
            engine=self, definition=definition, caster=holder, attacker=attacker,
            refs=refs, request=request, trigger_label=trigger_label or trigger_str,
            policy=policy,
        )
        execution.run_all()
        if snapshot is None and (live_snapshot or execution.status != ExecutionStatus.RUNNING):
            decision["branch_snapshot"] = live_snapshot
            decision["_engine_branch_owner"] = self._branch_owner_token
        return execution

    # ==================================================================
    # 五、统一单步结算核心（所有法术类型的唯一执行路径）
    # ==================================================================

    def silenced_by_mask(self, actor: Entity, calc: dict) -> bool:
        """缄默面具：持有方无法发动附带代价（非"消耗"法力）的道纹。"""
        return bool(self.state.side_has(actor, "缄默面具")
                    and calc.get("cost_type") not in (None, "", "消耗"))

    def pay_daowen_shard_cost(self, actor: Entity, name: str, calc: dict,
                              x: int) -> Optional[str]:
        """消灾X 的碎片类代价：足够则支付并返回 None，不足返回错误文本。"""
        if name == "消灾":
            in_combat = self.state.phase == "in_combat"
            mult = 1 if in_combat else 2
            fake_need = calc.get("fake_cost", 50 * x) * mult
            real_need = calc.get("real_cost", 5 * x) * mult
            if actor is self.state.player:
                have_fake, have_real = self.state.fake_shards, self.state.shards
            else:
                have_fake, have_real = getattr(actor, "fake_shards", 0), actor.shards
            if have_fake >= fake_need:
                if actor is self.state.player:
                    self.state.fake_shards -= fake_need
                else:
                    actor.fake_shards -= fake_need
            elif have_real >= real_need:
                if actor is self.state.player:
                    self.state.lose_shards(real_need)
                else:
                    actor.lose_shards(real_need)
            else:
                return (f"碎片不足：消灾X需{fake_need}假碎片或{real_need}碎片"
                        f"（局外×{mult}），当前假{have_fake}/真{have_real}")
        if name == "连击":
            # 2026-10-22：碎片/假碎片是两个独立通道，只扣各自池子，
            # 不走 lose_shards 的「假碎片优先」合并失去（双价在 calc 里分开声明）。
            real_need = int(calc.get("cost_real_shards", 0) or 0)
            fake_need = int(calc.get("cost_fake_shards", 0) or 0)
            if real_need > 0:
                have_real = self.state.shards if actor is self.state.player else actor.shards
                if have_real < real_need:
                    return f"碎片不足：连击碎片通道需{real_need}碎片，当前{have_real}"
                if actor is self.state.player:
                    self.state.shards -= real_need
                else:
                    actor.shards -= real_need
            if fake_need > 0:
                have_fake = (self.state.fake_shards if actor is self.state.player
                             else getattr(actor, "fake_shards", 0))
                if have_fake < fake_need:
                    return f"假碎片不足：连击假碎片通道需{fake_need}假碎片，当前{have_fake}"
                if actor is self.state.player:
                    self.state.fake_shards -= fake_need
                else:
                    actor.fake_shards -= fake_need
        return None

    def pay_daowen_mana(self, caster: Entity, calc: dict, target: Entity) -> tuple[int, Optional[str]]:
        """【消耗】类道纹的法力支付（use_daowen 与法术单步共用同一口径）。

        返回 (已付法力, 错误文本)。错误时不扣任何法力。
        """
        if calc.get("cost_type") != "消耗":
            return 0, None
        cost = calc.get("cost", 0)
        if cost <= 0:
            return 0, None
        if not caster.spend_mana(cost):
            return 0, f"法力不足，需{cost}，当前{caster.current_mana}"
        self.note_mana_inflicted(caster, target, cost)
        return cost, None

    def _daowen_step_preflight(self, *, daowen: str, x: int, calc: dict, caster: Entity,
                               target: Optional[Entity], refs: dict[str, Entity],
                               entry_extra: dict, policy: SpellStepPolicy):
        """一步"发动道纹"的共用前置环节（法术步骤与 use_daowen 同一顺序）。

        顺序与 use_daowen 完全一致：
          目标可选中（飞行） → 缄默面具 → 「目标发动道纹前」反应 → 施法者死亡。
        资源（法力/碎片/速度）由调用方紧跟其后支付，判定口径同样唯一
        （pay_daowen_mana / pay_daowen_shard_cost）。

        返回 (early: StepResult|None, trigger_logs: list[dict])。
        """
        if target is not None and target is not caster and not self.is_targetable(caster, target):
            return StepResult(
                status=StepStatus.INTERRUPTED, daowen=daowen, x=x,
                target_name=getattr(target, "name", ""),
                reason=InterruptReason.TARGET_UNTARGETABLE,
                detail=f"{target.name}处于飞行，无法被选中为法术目标"), []
        if policy.declares_daowen and self.silenced_by_mask(caster, calc):
            return StepResult(
                status=StepStatus.INTERRUPTED, daowen=daowen, x=x,
                target_name=getattr(target, "name", ""),
                reason=InterruptReason.DAOWEN_UNUSABLE,
                detail="缄默面具：无法发动附带代价的道纹"), []
        logs: list[dict] = []
        if policy.allow_trigger_reactions:
            choices = (entry_extra or {}).get("trigger_spell_choices", {})
            try:
                self.validate_daowen_trigger_spells(caster, choices, refs)
            except ValueError as exc:
                return StepResult(
                    status=StepStatus.INTERRUPTED, daowen=daowen, x=x,
                    target_name=getattr(target, "name", ""),
                    reason=InterruptReason.TRIGGER_CHOICES_INVALID,
                    detail=f"目标发动道纹前反应提交已不合法：{exc}"), []
            logs = self.resolve_daowen_trigger_spells(caster, choices, refs)
            if not caster.is_alive:
                return StepResult(
                    status=StepStatus.INTERRUPTED, daowen=daowen, x=x,
                    target_name=getattr(target, "name", ""),
                    reason=InterruptReason.CASTER_DEAD,
                    detail=f"{caster.name}发动道纹前被反应法术命零",
                    trigger_spell_logs=logs), logs
        return None, logs

    def _execute_single_daowen_step(self, *, definition, step, entry, caster: Entity,
                                    attacker: Optional[Entity], refs: dict[str, Entity],
                                    trigger_label: str = "",
                                    target_resolver=None,
                                    skip_predicate=None,
                                    policy: Optional[SpellStepPolicy] = None,
                                    step_key: str = "", iteration: int = 0) -> StepResult:
        """执行法术中的单步道纹（=一次"发动道纹"），返回 StepResult。

        这是唯一的单步结算核心：反应法术 / 道纹前法术 / 全局时点法术 / 瞬发法术
        全部经此执行；use_daowen 复用同一批前置与代价函数（见 _daowen_step_preflight）。

        正常游戏中断（资源不足/目标失效/道纹不可用/施法者命零）用 StepResult 表达；
        真正的程序错误（道纹计算异常、目标解析契约错）返回 FAILED 或抛异常，
        不伪装成游戏中断。
        """
        policy = policy or self.EVENT_STEP_POLICY
        reverse = {id(entity): ref for ref, entity in refs.items()}

        # --- 解析 entry（dict / StepRequest 两种） ---
        if hasattr(entry, "x"):
            x = entry.x
            target_ref_in = entry.target_ref
            dodge_flag = bool(entry.dodge)
            dodge_relic = entry.dodge_relic_target_ref
            entry_dict = {"x": x, "target_ref": target_ref_in, "dodge": dodge_flag,
                          "dodge_relic_target_ref": dodge_relic}
            entry_dict.update(entry.extra or {})
        else:
            entry_dict = entry if isinstance(entry, dict) else {}
            x = entry_dict.get("x")
            target_ref_in = entry_dict.get("target_ref")
            dodge_flag = bool(entry_dict.get("dodge"))
            dodge_relic = entry_dict.get("dodge_relic_target_ref")

        daowen = self._step_daowen(step)
        role = self._step_role(step)

        # --- 目标解析 ---
        try:
            if target_resolver is not None:
                target, expected_ref = target_resolver(step, entry_dict, caster, attacker, refs)
            elif role == "any":
                target = refs.get(target_ref_in)
                expected_ref = target_ref_in
                if target is None:
                    return StepResult(
                        status=StepStatus.INTERRUPTED, daowen=daowen, x=x or 0,
                        reason=InterruptReason.TARGET_INVALID,
                        detail=f"任意目标target_ref={target_ref_in}不是当前合法实体")
            else:
                target = self._resolve_step_subject(role, caster, attacker)
                expected_ref = reverse.get(id(target))
        except Exception as exc:  # 引擎契约错误：不吞
            return StepResult(
                status=StepStatus.FAILED, daowen=daowen, x=x or 0,
                reason=InterruptReason.INVALID_INPUT,
                detail=f"法术单步目标解析失败: {exc}")

        if role != "any" and target_ref_in is not None and target_ref_in != expected_ref:
            return StepResult(
                status=StepStatus.INTERRUPTED, daowen=daowen, x=x or 0,
                target_ref=target_ref_in, reason=InterruptReason.INVALID_INPUT,
                detail=f"步骤target_ref={target_ref_in}与期望{expected_ref}不一致")
        target_ref = expected_ref if role != "any" else target_ref_in

        # --- 目标失效：跳过本步，不中断整个法术 ---
        if target is None or not getattr(target, "is_alive", False):
            return StepResult(
                status=StepStatus.SKIPPED, daowen=daowen, x=x or 0,
                target_ref=target_ref, target_name=target.name if target else "(none)",
                reason=InterruptReason.TARGET_INVALID,
                detail="目标已失效", iteration=iteration)
        if role == "attacker" and target is caster:
            # 角色声明"攻击者"但本题里没有对位实体时，不把法术打回施法者自己。
            return StepResult(
                status=StepStatus.SKIPPED, daowen=daowen, x=x or 0,
                target_ref=target_ref, target_name=target.name,
                reason=InterruptReason.TARGET_INVALID,
                detail="无对位的失血/行动来源", iteration=iteration)

        # --- X 合法性 ---
        if not isinstance(x, int) or isinstance(x, bool) or x < 1:
            return StepResult(
                status=StepStatus.INTERRUPTED, daowen=daowen,
                target_ref=target_ref, target_name=target.name,
                reason=InterruptReason.INVALID_X,
                detail=f"X={x!r}非法，必须≥1整数")

        # --- 道纹可用性 ---
        dw_inst = caster.dao_wen.get(daowen)
        if dw_inst is None or not dw_inst.can_use():
            return StepResult(
                status=StepStatus.INTERRUPTED, daowen=daowen, x=x,
                target_ref=target_ref, target_name=target.name,
                reason=InterruptReason.DAOWEN_UNUSABLE,
                detail=f"道纹{daowen}不可用（未持有/封印/冷却/唯一已用）")

        # --- 跳过谓词（如连击需要前序伤害） ---
        if skip_predicate is not None:
            skip_reason = skip_predicate(daowen, target, entry_dict, step)
            if skip_reason:
                return StepResult(
                    status=StepStatus.SKIPPED, daowen=daowen, x=x,
                    target_ref=target_ref, target_name=target.name,
                    detail=skip_reason, iteration=iteration)

        # --- 道纹计算 ---
        try:
            calc = DaoWenEngine.resolve(daowen, x, target=target, caster=caster)
        except Exception as exc:
            return StepResult(
                status=StepStatus.FAILED, daowen=daowen, x=x,
                target_ref=target_ref, target_name=target.name,
                reason=InterruptReason.INVALID_INPUT,
                detail=f"道纹{daowen}计算失败: {exc}")

        # --- 共用前置环节（飞行/缄默面具/目标发动道纹前反应/施法者死亡） ---
        early, trigger_logs = self._daowen_step_preflight(
            daowen=daowen, x=x, calc=calc, caster=caster, target=target,
            refs=refs, entry_extra=entry_dict, policy=policy)
        if early is not None:
            early.target_ref = target_ref
            early.iteration = iteration
            if not early.trigger_spell_logs:
                early.trigger_spell_logs = trigger_logs
            return early

        # --- 法力支付（口径与 use_daowen 唯一） ---
        cost_paid, err = self.pay_daowen_mana(caster, calc, target)
        if err:
            return StepResult(
                status=StepStatus.INTERRUPTED, daowen=daowen, x=x,
                target_ref=target_ref, target_name=target.name,
                reason=InterruptReason.MANA_INSUFFICIENT, detail=err,
                trigger_spell_logs=trigger_logs, iteration=iteration)

        # --- 碎片代价（消灾X） ---
        shard_error = self.pay_daowen_shard_cost(caster, daowen, calc, x)
        if shard_error:
            return StepResult(
                status=StepStatus.INTERRUPTED, daowen=daowen, x=x,
                target_ref=target_ref, target_name=target.name, cost_paid=cost_paid,
                reason=InterruptReason.SHARDS_INSUFFICIENT, detail=shard_error,
                trigger_spell_logs=trigger_logs, iteration=iteration)

        # --- 闪避（敌对目标；速度不足=正常中断） ---
        hostile = self.state.on_player_side(caster) != self.state.on_player_side(target)
        if hostile and dodge_flag:
            if target.current_speed < 1:
                return StepResult(
                    status=StepStatus.INTERRUPTED, daowen=daowen, x=x,
                    target_ref=target_ref, target_name=target.name, cost_paid=cost_paid,
                    reason=InterruptReason.SPEED_INSUFFICIENT,
                    detail="目标速度不足以闪避",
                    trigger_spell_logs=trigger_logs, iteration=iteration)
            self._spend_dodge_speed(target, dodge_relic)
            return StepResult(
                status=StepStatus.DODGED, daowen=daowen, x=x,
                target_ref=target_ref, target_name=target.name,
                cost_paid=cost_paid, trigger_spell_logs=trigger_logs, iteration=iteration)

        # --- 真正执行道纹效果 ---
        try:
            execution = self.apply_daowen_effect(daowen, calc, caster, target)
        except Exception as exc:
            return StepResult(
                status=StepStatus.FAILED, daowen=daowen, x=x,
                target_ref=target_ref, target_name=target.name,
                cost_paid=cost_paid,
                reason=InterruptReason.INVALID_INPUT,
                detail=f"道纹{daowen}执行失败: {exc}",
                trigger_spell_logs=trigger_logs, iteration=iteration)

        return StepResult(
            status=StepStatus.COMPLETED, daowen=daowen, x=x,
            target_ref=target_ref, target_name=target.name,
            cost_paid=cost_paid, mana_gained=calc.get("mana_gain", 0) or 0,
            execution=execution, trigger_spell_logs=trigger_logs, iteration=iteration)

    # ==================================================================
    # 五点五、生命周期（Part 6）
    #
    # 持续法术的唯一事实源是 entity.spells；瞬发法术执行完即弃。
    # battle 作用域在战终由 clear_battle_scoped_spells 统一清除，
    # 因此绝不会以跨战斗绑定的形式活过存档/读档边界。
    # ==================================================================

    def spell_bindings(self, holder: Entity) -> list[dict]:
        """列出一个实体当前的绑定（供 schema/存档审计；不改动状态）。"""
        out = []
        for spell in getattr(holder, "spells", []) or []:
            out.append({"kind": "spell", "name": spell.name,
                        "lifecycle": getattr(spell, "lifecycle", Lifecycle.PERMANENT.value)})
        return out

    def clear_battle_scoped_spells(self) -> list[dict]:
        """战终清理：移除本场战斗建立/改写的 battle 作用域法术绑定。

        返回被移除的绑定清单（供战终结果与测试核对；行为本身不依赖返回值）。
        """
        removed = []
        for _ref, holder in self._combat_entity_refs().items():
            spells = list(getattr(holder, "spells", None) or [])
            kept = []
            gone: set[str] = set()
            for spell in spells:
                if getattr(spell, "lifecycle", Lifecycle.PERMANENT.value) == Lifecycle.BATTLE.value:
                    removed.append({"holder": holder.name, "spell": spell.name,
                                    "lifecycle": Lifecycle.BATTLE.value})
                    gone.add(spell.name)
                else:
                    kept.append(spell)
            if not gone:
                continue
            holder.spells = kept
        # 2026-10-14 审计F4：【黄符】授权只在本场有效，随 battle 法术一并清除。
        self.state.huangfu_grants().clear()
        return removed

    def undefine_spell(self, holder: Entity, spell_name: str) -> dict:
        """移除一个已经定义的自定义法术，不涉及出手与法力。"""
        removed = {"custom": False}
        spells = list(getattr(holder, "spells", None) or [])
        for spell in spells:
            if spell.name == spell_name:
                holder.spells = [sp for sp in spells if sp is not spell]
                removed["custom"] = True
                break
        return removed

    # ==================================================================
    # 六、反应型法术（受到伤害前/后、失去生命前/后）
    # ==================================================================

    # 四个挂接点共用同一套 prepare/validate/resolve 流水线。
    _REACTION_SPELL_SLOTS = (
        ("before", ActionPhase.BEFORE_DAMAGE_TAKEN.value),
        ("after", ActionPhase.AFTER_LIFE_LOST.value),
        ("damage_after", ActionPhase.AFTER_DAMAGE_TAKEN.value),
        ("life_before", ActionPhase.BEFORE_LIFE_LOST.value),
    )

    def prepare_spell_reactions(self, holder: Entity, attacker: Entity) -> dict:
        """列出一次受击各时点可能触发的法术及其全部决策槽位（供 prepare 嵌入）。"""
        result = {}
        subject_of = self._reaction_subject_of(holder, attacker)
        for key, trigger in self._REACTION_SPELL_SLOTS:
            entries = []
            for name, flow in self._eligible_spell_flows(holder, trigger).items():
                entries.append(self._spell_schema_entry(name, flow, holder, subject_of))
            result[key] = entries
        return result

    def validate_spell_reaction_submission(self, holder: Entity, attacker: Entity,
                                           submitted: Any, refs: dict[str, Entity]) -> None:
        """校验受击方反应法术提交（结构校验；资源问题留给执行期中断）。"""
        if not isinstance(submitted, dict):
            raise ValueError("每次攻击必须显式提交spell_choices对象")
        subject_of = self._reaction_subject_of(holder, attacker)
        for key, trigger in self._REACTION_SPELL_SLOTS:
            eligible = self._eligible_spell_flows(holder, trigger)
            choices = submitted.get(key)
            # 受到伤害后/失去生命前是本轮新增挂接点：为兼容大量既有调用点
            # 只提交{"before","after"}两个历史key——该挂接点确实没有候选法术时，
            # 缺省key按"无候选"处理；一旦存在候选，仍必须显式覆盖。
            if choices is None and not eligible and key in ("damage_after", "life_before"):
                continue
            if not isinstance(choices, dict) or set(choices) != set(eligible):
                raise ValueError(f"spell_choices.{key}必须逐一覆盖{sorted(eligible)}")
            for spell_name, flow in eligible.items():
                self._validate_decision(
                    spell_name=spell_name, flow=flow, holder=holder, attacker=attacker,
                    refs=refs, decision=choices[spell_name], subject_of=subject_of)

    def _resolve_spell_reactions(self, trigger: str, holder: Entity, attacker: Entity,
                                 submitted: dict, refs: dict[str, Entity]) -> list[dict]:
        """反应法术结算（唯一执行路径：SpellExecution + 单步核心）。"""
        self._resolving_life_lost_reactions += 1
        try:
            flows = self._eligible_spell_flows(holder, trigger)
            subject_of = self._reaction_subject_of(holder, attacker)
            logs = []
            for spell_name, flow in flows.items():
                decision = submitted.get(spell_name)
                if not decision or not decision.get("use"):
                    logs.append({"spell": spell_name, "used": False})
                    continue
                decision["_engine_automatic"] = bool(decision.get("_engine_automatic"))
                execution = self._run_flow_spell(
                    spell_name=spell_name, flow=flow, trigger_str=trigger,
                    holder=holder, attacker=attacker, refs=refs, decision=decision,
                    policy=self.EVENT_STEP_POLICY)
                logs.extend(execution.logs())
                if execution.status == ExecutionStatus.FAILED:
                    raise ValueError(
                        f"法术{spell_name}执行失败: {execution.interrupt_reason.value} "
                        f"{execution.interrupt_detail}")
            return logs
        finally:
            self._resolving_life_lost_reactions -= 1

    # ==================================================================
    # 七、「目标发动道纹前」反应法术（咎由自取等）
    # ==================================================================

    def prepare_daowen_trigger_spells(self, actor: Entity) -> dict:
        """列出[actor]即将发动道纹时，各个对手的可用反应法术及其槽位。"""
        refs = self._combat_entity_refs()
        result = {}
        for ref, holder in refs.items():
            if self.state.on_player_side(holder) == self.state.on_player_side(actor):
                continue
            flows = self._eligible_spell_flows(holder, "目标发动道纹前")
            if not flows:
                continue
            subject_of = self._daowen_trigger_subject_of(holder, actor)
            result[ref] = [self._spell_schema_entry(name, flow, holder, subject_of)
                           for name, flow in flows.items()]
        return result

    def _daowen_trigger_resolver(self, actor: Entity):
        def _resolver(step, entry_dict, caster, _attacker, refs):
            subject = self._trigger_spell_subject(self._step_role(step), caster, actor)
            if subject == "any":
                ref = entry_dict.get("target_ref")
                return refs.get(ref), ref
            if subject == "actor":
                return actor, next((r for r, e in refs.items() if e is actor), None)
            return caster, next((r for r, e in refs.items() if e is caster), None)
        return _resolver

    def validate_daowen_trigger_spells(self, actor: Entity, submitted: Any,
                                       refs: dict[str, Entity]) -> None:
        """校验「目标发动道纹前」反应法术提交（结构校验）。"""
        expected = self.prepare_daowen_trigger_spells(actor)
        if not isinstance(submitted, dict) or set(submitted) != set(expected):
            raise ValueError(f"trigger_spell_choices必须覆盖{sorted(expected)}")
        for holder_ref in expected:
            holder = refs[holder_ref]
            flows = self._eligible_spell_flows(holder, "目标发动道纹前")
            choices = submitted[holder_ref]
            if not isinstance(choices, dict) or set(choices) != set(flows):
                raise ValueError("目标发动道纹前的法术提交不完整")
            subject_of = self._daowen_trigger_subject_of(holder, actor)
            for spell_name, flow in flows.items():
                self._validate_decision(
                    spell_name=spell_name, flow=flow, holder=holder, attacker=actor,
                    refs=refs, decision=choices[spell_name], subject_of=subject_of)

    def resolve_daowen_trigger_spells(self, actor: Entity, submitted: dict,
                                      refs: dict[str, Entity]) -> list[dict]:
        """「目标发动道纹前」反应法术结算。

        流程特例（连击需要前序伤害）以 skip_predicate 表达，保持与旧实现逐字一致。
        2026-10-03：【坠落】道纹删除，【咎由自取】的坠落步骤一并移除（现为杀伐+连击）。
        """
        logs = []
        previous_damage = {"value": 0}

        def _daowen_skip(daowen, target, entry_dict, step):
            # 2026-10-02 注：以下判定沿用了旧实现的字面行为（有前序伤害时跳过连击），
            # 与其 detail 文案相反，属未经裁定的历史行为，见 报告.md「刻意未改动」。
            if daowen == "连击" and previous_damage["value"] > 0:
                return "连击需要前序伤害"
            return None

        def _on_step(result):
            if result.status == StepStatus.COMPLETED and result.execution is not None:
                previous_damage["value"] = sum(
                    e.get("actual_damage", 0) for e in result.execution.get("effects", []))
            elif result.status == StepStatus.DODGED:
                previous_damage["value"] = 0

        for holder_ref, choices in (submitted or {}).items():
            holder = refs[holder_ref]
            flows = self._eligible_spell_flows(holder, "目标发动道纹前")
            subject_of = self._daowen_trigger_subject_of(holder, actor)
            resolver = self._daowen_trigger_resolver(actor)
            for spell_name, flow in flows.items():
                decision = choices[spell_name]
                if not decision.get("use"):
                    continue
                definition = self._spell_definition_from_flow(spell_name, flow, "目标发动道纹前")
                request = SpellCastRequest(
                    use=True, steps=self._decision_to_requests(decision),
                    branch_snapshot=dict(decision.get("branch_snapshot") or {}),
                    max_iterations=decision.get("max_iterations"))
                execution = SpellExecution(
                    engine=self, definition=definition, caster=holder, attacker=actor,
                    refs=refs, request=request, trigger_label="目标发动道纹前",
                    policy=self.EVENT_STEP_POLICY, target_resolver=resolver,
                    skip_predicate=_daowen_skip, on_step=_on_step)
                execution.run_all()
                if decision.get("branch_snapshot") is None:
                    decision["branch_snapshot"] = dict(request.branch_snapshot or {})
                    decision["_engine_branch_owner"] = self._branch_owner_token
                logs.extend(execution.logs())
                if execution.status == ExecutionStatus.FAILED:
                    raise ValueError(
                        f"法术{spell_name}执行失败: {execution.interrupt_reason.value} "
                        f"{execution.interrupt_detail}")
        return logs

    # ==================================================================
    # 八、全局时点法术（战始/战终/回始/回终/敌回始/敌回终/自身回合结束）
    # ==================================================================

    def _global_trigger_holders(self, refs: dict[str, Entity]) -> dict[str, Entity]:
        """当前场上可能持有全局时点法术的实体（内置按道纹推导，不能只看 spells）。"""
        return {ref: entity for ref, entity in refs.items()
                if entity.is_alive
                and (entity.spells or self._builtin_spell_flows(entity))}

    def prepare_global_trigger_spells(self, trigger: str) -> dict:
        """列出当前时点全部持有者的可发动全局法术及其决策槽位。"""
        refs = self._combat_entity_refs()
        result: dict[str, list[dict]] = {}
        for holder_ref, holder in self._global_trigger_holders(refs).items():
            flows = self._eligible_spell_flows(holder, trigger)
            if not flows:
                continue
            subject_of = self._global_subject_of(holder)
            entries = []
            for name, flow in flows.items():
                entry = self._spell_schema_entry(name, flow, holder, subject_of)
                spell = self.spell_definition(holder, name)
                entry["automatic"] = bool(getattr(spell, "automatic", False)
                                          or flow.get("trigger") == "自身回合结束")
                entries.append(entry)
            result[holder_ref] = entries
        return result

    def is_automatic_spell(self, holder_ref: str, spell_name: str) -> bool:
        """查询一个全局法术是否由引擎自动提交。"""
        refs = self._combat_entity_refs()
        holder = refs.get(holder_ref)
        if holder is None:
            return False
        spell = self.spell_definition(holder, spell_name)
        if spell is None:
            return False
        if getattr(spell, "automatic", False):
            return True
        flow = self.SPELL_FLOWS.get(spell.name)
        if flow is None:
            flow = self._parse_custom_spell(spell)
        return bool(flow and flow.get("trigger") == "自身回合结束")

    def has_manual_global_trigger_spells(self, trigger: str) -> bool:
        """当前时点是否还存在需要外部显式提交的全局法术。"""
        expected = self.prepare_global_trigger_spells(trigger)
        return any(not entry.get("automatic", False)
                   for entries in expected.values() for entry in entries)

    def automatic_global_trigger_choices(self, trigger: str) -> dict:
        """为 automatic=True 的全局法术构造真实引擎提交。

        自动法术仍走 validate/resolve_global_trigger_spells 的同一条执行路径，
        不做旁路；X=1、目标取第一只合法敌对怪物，没有可选目标时 use=false。
        """
        refs = self._combat_entity_refs()
        expected = self.prepare_global_trigger_spells(trigger)
        submitted: dict[str, dict] = {}
        for holder_ref, entries in expected.items():
            holder = refs[holder_ref]
            submitted[holder_ref] = {}
            for entry in entries:
                name = entry["spell_name"]
                if not entry.get("automatic", False):
                    submitted[holder_ref][name] = {"use": False}
                    continue
                flow = self._eligible_spell_flows(holder, trigger)[name]
                subject_of = self._global_subject_of(holder)
                reverse = {id(e): r for r, e in refs.items()}
                steps = []
                valid = True
                for _idx, step, _optional in iter_action_slots(self._flow_program(flow)):
                    subject, entity = subject_of(step)
                    if subject == "any":
                        # 【封印】的 DSL 目标是 any：自动提交只选当前敌方怪物，
                        # 不把轮回者/队友放进自动目标。
                        candidates = [
                            ref for ref, e in refs.items()
                            if e.is_alive and e.entity_type == "怪物"
                            and self.state.on_player_side(e) != self.state.on_player_side(holder)
                            and self.is_targetable(holder, e)
                        ]
                        if not candidates:
                            valid = False
                            break
                        steps.append({"x": 1, "target_ref": candidates[0], "dodge": False})
                    else:
                        steps.append({"x": 1, "target_ref": reverse.get(id(entity)),
                                      "dodge": False})
                submitted[holder_ref][name] = (
                    {"use": True, "steps": steps, "_engine_automatic": True}
                    if valid and steps else {"use": False})
        return submitted

    def validate_global_trigger_spells(self, trigger: str, submitted: Any,
                                       refs: dict[str, Entity]) -> None:
        """校验全局时点法术提交（结构校验）。"""
        expected = self.prepare_global_trigger_spells(trigger)
        if not isinstance(submitted, dict) or set(submitted) != set(expected):
            raise ValueError(f"【{trigger}】的spell_choices必须逐一覆盖{sorted(expected)}")
        for holder_ref in expected:
            holder = refs[holder_ref]
            flows = self._eligible_spell_flows(holder, trigger)
            choices = submitted[holder_ref]
            if not isinstance(choices, dict) or set(choices) != set(flows):
                raise ValueError(f"【{trigger}】{holder.name}的法术提交必须逐一覆盖{sorted(flows)}")
            subject_of = self._global_subject_of(holder)
            for spell_name, flow in flows.items():
                self._validate_decision(
                    spell_name=spell_name, flow=flow, holder=holder, attacker=holder,
                    refs=refs, decision=choices[spell_name], subject_of=subject_of)

    def resolve_global_trigger_spells(self, trigger: str, submitted: dict,
                                      refs: dict[str, Entity]) -> list[dict]:
        """结算全局时点法术（唯一执行路径）。"""
        logs = []
        for holder_ref, choices in (submitted or {}).items():
            holder = refs.get(holder_ref)
            if holder is None or not holder.is_alive:
                continue
            flows = self._eligible_spell_flows(holder, trigger)
            subject_of = self._global_subject_of(holder)
            for spell_name, flow in flows.items():
                decision = choices.get(spell_name)
                if not decision or not decision.get("use"):
                    logs.append({"spell": spell_name, "holder": holder.name, "used": False})
                    continue
                execution = self._run_flow_spell(
                    spell_name=spell_name, flow=flow, trigger_str=trigger,
                    holder=holder, attacker=holder, refs=refs, decision=decision,
                    policy=self.EVENT_STEP_POLICY)
                for log_entry in execution.logs():
                    log_entry["holder"] = holder.name
                logs.extend(execution.logs())
                if execution.status == ExecutionStatus.FAILED:
                    raise ValueError(
                        f"法术{spell_name}执行失败: {execution.interrupt_reason.value} "
                        f"{execution.interrupt_detail}")
        return logs

    # ==================================================================
    # 九、瞬发法术：cast(flow=...) → SpellExecution（1 次出手一次施法）
    # ==================================================================

    def _instant_target_resolver(self, cast_target: Optional[Entity]):
        """瞬发的身份映射：self/caster→施法者，target→本次施法目标，any→提交的 ref。

        """
        def _resolver(step, entry_dict, caster, attacker, refs):
            reverse_ref = lambda e: next((r for r, x in refs.items() if x is e), None)
            subject = self._trigger_spell_subject(self._step_role(step), caster, attacker)
            if subject == "any":
                ref = entry_dict.get("target_ref")
                return refs.get(ref), ref
            if subject == "actor":
                return attacker, reverse_ref(attacker)
            return caster, reverse_ref(caster)
        return _resolver

    def build_instant_execution(self, caster: Entity, flow_text: Any,
                                cast_target: Optional[Entity], step_requests: Any,
                                refs: dict[str, Entity], spell_name: str = "瞬发法术",
                                max_iterations: Optional[int] = None) -> SpellExecution:
        """把一次 cast(flow=...) 提交组装成 spell + 决策的 SpellExecution。

        契约错误（句式/步数/X/目标/未持道纹/飞行目标）在此抛 ValueError，
        由调用方在扣出手之前返回失败；一切运行期问题交给执行器中断。
        """
        from ..spell_dsl import parse_instant_flow, SpellDslError
        if not isinstance(flow_text, str) or not flow_text.strip():
            raise ValueError("cast(flow=...)必须提交非空的效果流程文本")
        try:
            parsed = parse_instant_flow(flow_text, set(DaoWenEngine.list_all()))
        except SpellDslError as exc:
            raise ValueError(f"瞬发法术句式错误：{exc}") from exc
        program = list(parsed.steps)
        interrupt_only = sorted(collect_step_daowen(program) & FIRST_EMBRACE_DAOWEN)
        if interrupt_only:
            raise ValueError(
                f"道纹【{'、'.join(interrupt_only)}】必须独立发动并等待DM裁定，不能嵌入瞬发法术")
        slot_count = count_action_steps(program)
        if slot_count == 0:
            raise ValueError("瞬发法术没有任何可执行的步骤")
        for _idx, step, _optional in iter_action_slots(program):
            daowen = self._step_daowen(step)
            inst = caster.dao_wen.get(daowen)
            if inst is None:
                raise ValueError(f"{caster.name}未持有瞬发法术所需道纹【{daowen}】")
            if not inst.can_use():
                raise ValueError(f"道纹{daowen}不可用（冷却/封印），无法施放瞬发法术")
        if any(self._step_role(step) == "target" for _i, step, _o in iter_action_slots(program)):
            if cast_target is None or not cast_target.is_alive:
                raise ValueError("瞬发法术含“于目标”步骤，必须提交存活的target_ref")
        if not isinstance(step_requests, list) or len(step_requests) != slot_count:
            raise ValueError(f"瞬发法术必须一次性完整提交{slot_count}步的steps决策")
        if max_iterations is not None and (
                not isinstance(max_iterations, int) or isinstance(max_iterations, bool)
                or max_iterations < 1 or max_iterations > MAX_SPELL_LOOP_ITERATIONS):
            raise ValueError(f"max_iterations必须是1..{MAX_SPELL_LOOP_ITERATIONS}之间的整数")

        resolver = self._instant_target_resolver(cast_target)
        requests = []
        for idx, (entry, (slot_index, step, _optional)) in enumerate(
                zip(step_requests, iter_action_slots(program)), 1):
            if not isinstance(entry, dict):
                raise ValueError(f"瞬发法术第{idx}步决策必须是对象")
            x = entry.get("x")
            if not isinstance(x, int) or isinstance(x, bool) or x < 1:
                raise ValueError(f"瞬发法术第{idx}步x必须是≥1整数")
            dodge = entry.get("dodge", False)
            if not isinstance(dodge, bool):
                raise ValueError(f"瞬发法术第{idx}步dodge必须是布尔值")
            is_any = self._step_role(step) == "any"
            target, _ = resolver(step, {"target_ref": entry.get("target_ref")}, caster,
                                 cast_target, refs)
            if target is None:
                raise ValueError(f"瞬发法术第{idx}步的任意目标target_ref不是当前合法实体")
            if not is_any and "target_ref" in entry \
                    and refs.get(entry["target_ref"]) is not target:
                raise ValueError(f"瞬发法术第{idx}步target_ref与流程声明的目标身份不符")
            if target is not caster and not self.is_targetable(caster, target):
                raise ValueError(f"{target.name}处于飞行，无法被选中为法术目标")
            hostile = self.state.on_player_side(caster) != self.state.on_player_side(target)
            if dodge and not hostile:
                raise ValueError(f"瞬发法术第{idx}步目标非敌对，不能声明闪避")
            requests.append(StepRequest(
                x=x,
                # 固定身份的步骤由解析器定目标，不透传 target_ref（避免与无神改向冲突）
                target_ref=entry.get("target_ref") if is_any else None,
                dodge=dodge,
                dodge_relic_target_ref=entry.get("dodge_relic_target_ref"),
                # 每一步都是一次"发动道纹"：敌方「目标发动道纹前」反应的逐步提交
                extra={"trigger_spell_choices": entry.get("trigger_spell_choices", {})},
            ))

        required = sorted(collect_step_daowen(program))
        definition = SpellDefinition(
            name=spell_name, required_daowen=required,
            trigger=TriggerType.IMMEDIATE, lifecycle=Lifecycle.INSTANT,
            body=program, rank=len(required),
            effect_flow_text=flow_text.strip(),
        )
        return SpellExecution(
            engine=self, definition=definition, caster=caster,
            attacker=cast_target, refs=refs,
            request=SpellCastRequest(use=True, steps=requests,
                                     max_iterations=max_iterations),
            trigger_label=TriggerType.IMMEDIATE.value,
            policy=self.CAST_STEP_POLICY, target_resolver=resolver,
        )

    # ==================================================================
    # 十、非攻击路径的自动反应（道纹伤害/流血代价/直接失血等）
    # ==================================================================

    def _max_auto_life_lost_x(self, daowen: str, target: Entity, caster: Entity,
                              budget: int) -> Optional[int]:
        """自动选择反应法术时，为单步挑一个可支付的 X（至少 1）。"""
        upper = min(max(1, budget), 10_000)
        for x in range(upper, 0, -1):
            calc = DaoWenEngine.resolve(daowen, x, target=target, caster=caster)
            if calc.get("cost_type") != "消耗":
                return 1
            if calc.get("cost", 0) <= budget:
                return x
        return None

    def _dodge_budget_reset(self) -> None:
        """换回合则清空自动反应路径的闪避计数（每回合最多 2 次，与 choose_dodge 同口径）。"""
        rnd = getattr(self.state, "current_round", None)
        if getattr(self, "_dodge_round", None) != rnd:
            self._dodge_round = rnd
            self._dodge_counts = {}

    def _auto_reaction_dodge_decision(self, daowen: str, calc: dict,
                                      holder: Entity, target: Entity) -> bool:
        """自动反应法术路径：被选定方是否消耗 1 点速度闪避本次道纹。

        DM 裁定：法术说到底只是自定义了触发条件的道纹，
        **道纹要遵守的规则，法术一样要遵守**。规则正文「凡带 [目标] 道纹，
        目标被选定时均可消耗 1 点当前速度进行闪避」、规则正文「禁止跳过闪避判定」。
        """
        if holder is None or target is None or not target.is_alive:
            return False
        if self.state.on_player_side(holder) == self.state.on_player_side(target):
            return False
        dmg = calc.get("target_damage") or calc.get("total_damage") or 0
        if not dmg:
            return False
        if self.bizhong_remaining(holder) > 0:
            return False
        if target.current_speed < 1:
            return False
        if not self.is_targetable(holder, target):
            return False
        self._dodge_budget_reset()
        used = self._dodge_counts.get(id(target), 0)
        try:
            # production 本地规则助手（engine/ai_rules.py）；不依赖实验性 TacticalAI。
            from ..ai_rules import choose_dodge
            want = bool(choose_dodge(None, int(dmg), budget_used=used, entity=target))
        except Exception:
            return False
        if want:
            self._dodge_counts[id(target)] = used + 1
        return want

    def _auto_after_life_lost_decision(self, name: str, flow: dict, holder: Entity,
                                       attacker: Optional[Entity], refs: dict[str, Entity],
                                       reverse: dict[int, str],
                                       budget: Optional[int] = None) -> dict:
        """为一次非攻击失血自动生成单法术提交（steps 契约）。

        没有 AI 决策窗口，因此按"可支付且效果方向合理"自动选择：
          - 目标为 any（任意目标）时无法静态定目标 → 本法术放弃自动触发。
          - 预测会走到的步骤里任一法力步骤付不起（X=1 都超出预算）→ 放弃触发。
          - 不一定会走到的分支槽位填 x=1（结构必须完整；真走到时按运行期判定）。
          - 触发型法术最多自动跑一轮（max_iterations=1），保持旧行为。
          - budget 为"本次共用法力预算"（同一次失血的多个反应法术共享）。
        """
        program = self._flow_program(flow)
        budget = holder.current_mana if budget is None else budget
        predicted = {id(s) for s in self._predict_flat_steps(flow.get("steps", []), holder, attacker)}
        steps = []
        consumed = 0
        for _idx, step, _optional in iter_action_slots(program):
            daowen = self._step_daowen(step)
            role = self._step_role(step)
            if role == "any":
                # 通用“任意目标”自动选择器：优先选择当前有效攻击力最高的存活怪物，
                # 并列按稳定战场顺序；没有合法目标才放弃本次自动触发。
                candidates = [m for m in self.state.active_enemies()
                              if getattr(m, "entity_type", "") == "怪物" and m.is_alive]
                candidates = [m for m in candidates if m.effective_attack_power() > 0]
                target = max(candidates, key=lambda m: m.effective_attack_power(), default=None)
            else:
                target = self._resolve_step_subject(role, holder, attacker)
            if target is not None and not target.is_alive:
                target = None
            if role == "attacker" and target is holder:
                # 没有对位来源时不自动把法术打回自己（执行期同样按 SKIPPED 处理）。
                target = None
            if target is None:
                steps.append({"x": 1, "target_ref": None, "dodge": False})
                continue
            predicted_here = id(step) in predicted
            x = self._max_auto_life_lost_x(daowen, target, holder, budget)
            if x is None:
                if predicted_here:
                    return {"use": False}
                x = 1
            calc = DaoWenEngine.resolve(daowen, x, target=target, caster=holder)
            if calc.get("cost_type") == "消耗":
                cost = calc.get("cost", 0)
                if cost > budget and predicted_here:
                    return {"use": False}
                if cost <= budget:
                    budget -= cost
                    consumed += cost
            steps.append({"x": x, "target_ref": reverse.get(id(target)),
                          "dodge": self._auto_reaction_dodge_decision(
                              daowen, calc, holder, target)})
        if not steps:
            return {"use": False}
        return {"use": True, "steps": steps,
                # 通用循环法术默认由资源/条件/无进展终止；旧反应法术保留单轮保护。
                "max_iterations": None if name == "徒劳无功" else 1,
                "_cost": consumed, "_engine_automatic": True}

    def _fire_auto_reaction(self, holder: Entity, trigger: str,
                            loss_ctx: Optional[EffectContext | dict]) -> list[dict]:
        """非攻击路径 → 自动触发反应型时点（受到伤害前/后、失去生命前/后）。

        攻击路径（resolve_attack）由显式反应窗口按提交结算，不经过本方法；
        这里处理其余一切导致"伤害/失血"的通道（道纹伤害/流血代价/血限压迫/
        千荆甲反射/遗物灾厄轮盘/直接失血/未来新增效果……），对持有者而言"触发时机一到
        就触发"——只检测事件，不逐个开窗。
        """
        if holder is None or not holder.is_alive:
            return []
        # 攻击失血由 resolve_attack 的反应窗口结算，本 hook 不重复触发；
        # 反应法术自身结算期间置 _resolving_life_lost_reactions>0，避免连锁死循环。
        if self._attack_after_window_target is holder or self._resolving_life_lost_reactions > 0:
            return []
        eligible = self._eligible_spell_flows(holder, trigger)
        if not eligible:
            return []
        refs = self._combat_entity_refs()
        reverse = {id(entity): ref for ref, entity in refs.items()}
        parent = normalize_context(loss_ctx) if loss_ctx is not None else None
        attacker: Optional[Entity] = None
        if parent is not None and parent.actor is not None and parent.actor is not holder:
            if any(parent.actor is e for e in refs.values()):
                attacker = parent.actor
        after: dict[str, dict] = {}
        # 共享法力预算：同一次失血可能同时有多个反应法术可触发，各自吃满预算
        # 会逐个结算时法力不足；这里按声明顺序扣减剩余预算。
        shared_budget = holder.current_mana
        for name, flow in eligible.items():
            dec = self._auto_after_life_lost_decision(
                name, flow, holder, attacker, refs, reverse, shared_budget)
            after[name] = dec
            if dec.get("use"):
                shared_budget = max(0, shared_budget - dec.get("_cost", 0))
        return self._resolve_spell_reactions(trigger, holder, attacker, after, refs)

    def _fire_after_life_lost(self, holder: Entity,
                              loss_ctx: Optional[EffectContext | dict]) -> list[dict]:
        """非攻击失血 → 触发「失去生命后」(AFTER_LIFE_LOST) 反应法术。"""
        return self._fire_auto_reaction(holder, ActionPhase.AFTER_LIFE_LOST.value, loss_ctx)

    def _fire_before_life_lost(self, holder: Entity,
                               loss_ctx: Optional[EffectContext | dict]) -> list[dict]:
        """生命即将下降 → 触发「失去生命前」反应法术（攻击路径由显式窗口结算）。"""
        return self._fire_auto_reaction(holder, ActionPhase.BEFORE_LIFE_LOST.value, loss_ctx)
