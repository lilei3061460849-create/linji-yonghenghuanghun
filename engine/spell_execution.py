"""法术统一执行模型（执行器自持控制流）。

设计原则（见 报告.md 与 法术索引.md「三大法则」）：

    道纹是积木，法术是由道纹写成的程序。

    DSL 文本
        ↓ 解析（spell_dsl）
    校验过的 AST / SpellDefinition
        ↓ 提交"程序 + 每步决策"
    SpellExecution
        ↓ 一次执行一条指令
        ↓ 读当前 GameState
        ↓ 求值条件 / 判断循环是否继续
        ↓ 通过**唯一**的单步道纹结算器执行一步
        ↓ 结算反应/代价/效果
        ↓ 再读当前 GameState
        ↓ 继续

即：控制流（IfStep/LoopStep）由执行器拥有，调用方不再把整个程序预展开成
固定列表，也不再手工计算 `cycles`。

五个核心概念：

- SpellDefinition  法术静态定义（名称/触发器/生命周期/程序体/元数据）
- SpellStepPolicy  一类法术步骤与"直接发动道纹"的显式语义差异（不再散落 if）
- SpellCastRequest 一次施法的决策输入（调用方提交的每步 X/目标/闪避）
- SpellExecution   一次施法的运行时状态机（拥有控制栈）
- StepResult       单步执行的事实结果

中断模型（Phase 2 语义，继续保留）：
- 提交结构非法（句式/步数/X/目标引用）= 契约错误，由调用方在提交时拒绝；
- 运行期资源不足/目标失效/道纹不可用 = 正常中断，用 StepResult 表达，
  已结算的步骤保留，不回滚；
- 引擎/程序错误（道纹计算异常、AST 非法）= FAILED，不伪装成游戏中断。

循环安全阀 MAX_SPELL_LOOP_ITERATIONS 是**工程保险丝**，不是游戏规则：
正常法力池/生命池在触发它之前必然已满足某个规则终止条件（法力耗尽、
施法者命零、目标离场、条件不再成立）。它是最后一道防线，用于防止
手写的自持程序（例如无消耗无产出的死循环）卡死程序。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .spell_dsl import ActionStep, IfStep, LoopStep, iter_action_steps


# ---------------------------------------------------------------------------
# 触发器与生命周期
# ---------------------------------------------------------------------------

class TriggerType(str, Enum):
    """法术启动时机。"""
    BEFORE_DAMAGE_TAKEN = "受到伤害前"
    AFTER_DAMAGE_TAKEN = "受到伤害后"
    BEFORE_LIFE_LOST = "失去生命前"
    AFTER_LIFE_LOST = "失去生命后"
    TARGET_BEFORE_DAOWEN = "目标发动道纹前"
    BATTLE_START = "战始"
    BATTLE_END = "战终"
    ROUND_START = "回始"
    ROUND_END = "回终"
    SELF_TURN_END = "自身回合结束"
    ENEMY_ROUND_START = "敌回始"
    ENEMY_ROUND_END = "敌回终"
    DODGE = "闪避时"
    IMMEDIATE = "瞬发"


class Lifecycle(str, Enum):
    """法术绑定存在多久（行为见 CombatEngine 的 battle-end 清理与 undefine_spell）。"""
    INSTANT = "instant"      # 本次施法完即弃，不建立绑定
    BATTLE = "battle"        # 本场战斗有效，战终自动清除
    PERMANENT = "permanent"  # 进入存档，跨战斗保留


class ExecutionStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class StepStatus(str, Enum):
    COMPLETED = "completed"      # 正常完成，效果已落地
    DODGED = "dodged"            # 目标闪避，未造成效果但代价已付
    SKIPPED = "skipped"          # 该步有条件跳过（目标已失效/坠落目标未飞行等）
    INTERRUPTED = "interrupted"  # 本步失败导致**整个法术**中断
    FAILED = "failed"            # 系统级失败（引擎/程序错误）


class InterruptReason(str, Enum):
    NONE = ""
    MANA_INSUFFICIENT = "mana_insufficient"
    TARGET_INVALID = "target_invalid"
    TARGET_UNTARGETABLE = "target_untargetable"
    DAOWEN_UNUSABLE = "daowen_unusable"
    SPEED_INSUFFICIENT = "speed_insufficient"
    INVALID_X = "invalid_x"
    INVALID_INPUT = "invalid_input"
    SHARDS_INSUFFICIENT = "shards_insufficient"
    CASTER_DEAD = "caster_dead"
    CASTER_UNAVAILABLE = "caster_unavailable"
    TRIGGER_CHOICES_INVALID = "trigger_choices_invalid"
    # 工程安全阀：循环次数超过 MAX_SPELL_LOOP_ITERATIONS。
    # 正常规则循环不会触达；触达说明程序不满足任何规则终止条件。
    LOOP_GUARD = "loop_guard"


# 工程安全阀（不是游戏规则）。与旧 MAX_SPELL_LOOP_CYCLES 同值：法术索引
# 已向用户承诺"1 万次"这个数量级，且正常法力池远不可能触达。
MAX_SPELL_LOOP_ITERATIONS = 10_000


# ---------------------------------------------------------------------------
# 1. SpellDefinition
# ---------------------------------------------------------------------------

@dataclass
class SpellDefinition:
    """法术静态定义（纯 AST，不持有任何运行时状态）。

    body 的元素是 spell_dsl 的 ActionStep / IfStep / LoopStep（旧内置法术表
    仍使用 (daowen, role) 元组，执行器同构处理，不再在本阶段展开）。
    """
    name: str
    required_daowen: list[str]
    trigger: TriggerType
    lifecycle: Lifecycle
    body: list
    rank: int = 1
    automatic: bool = False
    effect_flow_text: str = ""
    custom_conditions: list[str] = field(default_factory=list)
    loop_stop_conditions: list[str] = field(default_factory=list)

    @property
    def loop(self) -> bool:
        """程序体是否含循环（派生值；旧字段曾是调用方是否预展开 cycles 的开关）。"""
        return any(isinstance(step, LoopStep) for step in self.body)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "required_daowen": list(self.required_daowen),
            "trigger": self.trigger.value,
            "lifecycle": self.lifecycle.value,
            "rank": self.rank,
            "automatic": self.automatic,
            "loop": self.loop,
            "effect_flow": self.effect_flow_text,
            "custom_conditions": list(self.custom_conditions),
            "loop_stop_conditions": list(self.loop_stop_conditions),
        }


# ---------------------------------------------------------------------------
# 2. 单步语义策略：显式声明一类法术与"直接发动道纹"的差异
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SpellStepPolicy:
    """法术步骤的语义策略。

    规则结论（R-1，2026-10-02 定稿）：**法术的每一步都是一次"发动道纹"**，
    与 `use_daowen` 走同一批前置环节（目标合法/飞行、X、道纹可用、缄默面具、
    碎片代价、施法者死亡中止）。唯一显式差异是反应链层数：

    - 反应链只允许 1 层。执行器在"正在结算反应法术"（reaction_depth>0）时
      不再开启「目标发动道纹前」窗口——否则 A→B→A→… 无限递归。
      这个差异编码在 allow_trigger_reactions 里，由执行位置决定，而不是
      在每种法术类型里各写一份分支。

    declares_daowen=True 时：
      * 目标不可选中（飞行）→ interrupted(target_untargetable)
      * 缄默面具阻止附带代价的道纹 → interrupted(daowen_unusable)
      * 允许开窗时，调用方必须提交该步的 trigger_spell_choices（结构同 use_daowen）
      * 反应把施法者打死后 → interrupted(caster_dead)
      * 消灾碎片不足 → interrupted(shards_insufficient)
    """
    declares_daowen: bool = True
    allow_trigger_reactions: bool = True
    trigger_choices_required: bool = True


# ---------------------------------------------------------------------------
# 3. SpellCastRequest —— 一次施法的决策输入
# ---------------------------------------------------------------------------

@dataclass
class StepRequest:
    """单步输入：决策方为某个 ActionStep 给出的 X/target/dodge。

    循环体里的同一步在每一轮复用这一条决策；X 在提交后不可在执行中途更改。
    """
    x: int = 0
    target_ref: Optional[str] = None    # 仅 role=any 的步骤需要；其余身份自动解析
    dodge: bool = False
    dodge_relic_target_ref: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)  # trigger_spell_choices 等

    def to_dict(self) -> dict:
        return {"x": self.x, "target_ref": self.target_ref, "dodge": self.dodge,
                "dodge_relic_target_ref": self.dodge_relic_target_ref,
                **(self.extra or {})}


@dataclass
class SpellCastRequest:
    """一次施法的决策输入。

    steps 按 `spell_dsl.iter_action_steps(definition.body)` 的规范顺序给出，
    每项对应程序里一个 ActionStep（含 if 两个分支的步骤；未执行到的分支跳过）。
    循环由执行器迭代，不需要（也不接受）调用方预展开 cycles。
    """
    use: bool = True
    steps: list[StepRequest] = field(default_factory=list)
    # 调用方可选的循环轮数上限（不是程序；程序里每个动作仍只提交一条决策）。
    # None = 按规则一直循环到不能继续（法力耗尽/命零/无进展/DSL定次）。
    max_iterations: Optional[int] = None
    # 事件级分支冻结：同一次提交（同一个 decision dict）在多次命中/多次结算中
    # 复用首次执行时的条件分支选择。空 dict 表示尚未冻结；由执行器原地写入。
    branch_snapshot: Optional[dict[str, int]] = None
    source: str = "external"  # "external"（决策方提交）/ "engine"（引擎自动提交）


# ---------------------------------------------------------------------------
# 4. StepResult —— 单步执行结果
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    """单步执行后对事实的描述。不决定法术是否继续（那是 Execution 的责任）。"""
    status: StepStatus
    daowen: str = ""
    x: int = 0
    target_ref: Optional[str] = None
    target_name: str = ""
    cost_paid: int = 0
    mana_gained: int = 0
    execution: Optional[dict] = None   # apply_daowen_effect 返回值
    reason: InterruptReason = InterruptReason.NONE
    detail: str = ""
    iteration: int = 0                 # 0=不在循环里；≥1=第几轮
    step_key: str = ""                 # 程序内的稳定步骤下标（字符串）
    # 本步作为一次"发动道纹"所触发的「目标发动道纹前」反应日志。
    trigger_spell_logs: list = field(default_factory=list)

    def to_log(self) -> dict:
        """转换为既有日志 dict 格式，保持对外 API 兼容。"""
        if self.status == StepStatus.COMPLETED:
            log = {"daowen": self.daowen, "x": self.x, "target": self.target_name,
                   "execution": self.execution}
        elif self.status == StepStatus.DODGED:
            log = {"daowen": self.daowen, "target": self.target_name, "dodged": True}
        elif self.status == StepStatus.SKIPPED:
            log = {"daowen": self.daowen, "skipped": self.detail or True}
        else:
            log = {"daowen": self.daowen, "interrupted": self.reason.value,
                   "detail": self.detail}
        if self.iteration:
            log["iteration"] = self.iteration
        return log


# ---------------------------------------------------------------------------
# 5. SpellExecution —— 运行时状态机（拥有控制流）
# ---------------------------------------------------------------------------

def _index_ifs(steps, prefix: str = ""):
    """给程序里每个 IfStep 一个跨进程稳定的路径键（用于事件级分支冻结）。"""
    for i, step in enumerate(steps):
        key = f"{prefix}{i}"
        if isinstance(step, IfStep):
            yield key, step
            yield from _index_ifs(step.then_steps, f"{key}T")
            yield from _index_ifs(step.else_steps, f"{key}E")
        elif isinstance(step, LoopStep):
            yield from _index_ifs(step.body, f"{key}L")


class SpellExecution:
    """一次具体施法的运行时状态。执行器自己推进控制流。

    调用方只需要：提交程序（definition.body）+ 每步决策（request.steps）。
    执行器在每次要判定条件/循环继续时读取**当前** GameState。
    """

    def __init__(self, engine, definition: SpellDefinition, caster,
                 attacker, refs: dict[str, Any], request: SpellCastRequest,
                 *, trigger_label: str = "", policy: Optional[SpellStepPolicy] = None,
                 target_resolver=None, skip_predicate=None, on_step=None):
        self.engine = engine
        self.definition = definition
        self.caster = caster
        self.attacker = attacker
        self.refs = refs
        self.request = request
        self.trigger_label = trigger_label
        self.policy = policy or SpellStepPolicy()
        self.target_resolver = target_resolver
        self.skip_predicate = skip_predicate
        self.on_step = on_step   # callback(StepResult) 每步结束后调用

        self.status: ExecutionStatus = ExecutionStatus.RUNNING
        self.interrupt_reason: InterruptReason = InterruptReason.NONE
        self.interrupt_detail: str = ""
        self.results: list[StepResult] = []
        self.step_extras: list[dict] = []
        self.loop_stop_reason: str = ""
        self.loop_iterations: int = 0

        self._step_index = {id(step): idx for idx, step in iter_action_steps(definition.body)}
        self._if_paths = {id(node): key for key, node in _index_ifs(definition.body)}
        # 事件级冻结：只认"本次执行开始前"已冻结的分支；同一次执行内部
        # （包括循环的每一轮）始终按当前状态重新求值。
        self._frozen_at_start = dict(request.branch_snapshot or {})
        self._run_decisions: dict[str, int] = {}

    # -- 快捷访问 --
    @property
    def is_running(self) -> bool:
        return self.status == ExecutionStatus.RUNNING

    def _stop(self, status: ExecutionStatus, reason: InterruptReason, detail: str = "") -> None:
        self.status = status
        self.interrupt_reason = reason
        self.interrupt_detail = detail

    # -- 主驱动 --
    def run_all(self) -> list[StepResult]:
        """执行整段程序；遇中断/失败立即停机，已完成步骤保留。"""
        self._run_steps(self.definition.body, iteration=0)
        # 事件级分支冻结：把本次首轮做过的分支选择写回提交对象（原地）。
        if self.request.branch_snapshot is not None:
            for key, choice in self._run_decisions.items():
                self.request.branch_snapshot.setdefault(key, choice)
        if self.status == ExecutionStatus.RUNNING:
            self.status = ExecutionStatus.COMPLETED
        return self.results

    # -- 控制流 --
    def _run_steps(self, steps, *, iteration: int) -> None:
        for step in steps:
            if not self.is_running:
                return
            if not self._caster_alive():
                # 施法者在前一步（含自身的【癌变】等）命零：程序中止，
                # 后面的步骤不再结算（法术索引·血炼周天第 11 轮的既定行为）。
                self._stop(ExecutionStatus.INTERRUPTED, InterruptReason.CASTER_DEAD,
                           f"{self.caster.name}已命零，后续步骤不再结算")
                return
            if isinstance(step, ActionStep):
                self._run_action(step, iteration=iteration)
            elif isinstance(step, IfStep):
                self._run_if(step, iteration=iteration)
            elif isinstance(step, LoopStep):
                self._run_loop(step, iteration=iteration)
            else:  # 非法 AST = 程序错误，不伪装成游戏中断
                self._stop(ExecutionStatus.FAILED, InterruptReason.INVALID_INPUT,
                           f"未知步骤类型: {step!r}")

    def _run_if(self, node: IfStep, *, iteration: int) -> None:
        key = self._if_paths.get(id(node), "")
        frozen = self._frozen_at_start.get(key)
        if frozen is not None:
            taken = int(frozen)
        else:
            taken = 0 if self._evaluate_condition(node.condition) else 1
            if self.is_running:
                self._run_decisions.setdefault(key, taken)
        branch = node.then_steps if taken == 0 else node.else_steps
        self._run_steps(branch, iteration=iteration)

    def _run_loop(self, node: LoopStep, *, iteration: int) -> None:
        rounds = 0
        while True:
            if not self.is_running:
                break
            if not self._caster_alive():
                self._stop(ExecutionStatus.INTERRUPTED, InterruptReason.CASTER_DEAD,
                           f"{self.caster.name}已命零，循环中止")
                break
            bound = node.max_iterations
            if self.request.max_iterations is not None:
                bound = (self.request.max_iterations if bound is None
                         else min(bound, self.request.max_iterations))
            guard = getattr(self.engine, "spell_loop_guard", None)
            if guard is not None:
                stop, reason = guard(self.definition, self.caster, self.attacker)
                if stop:
                    self.loop_stop_reason = reason
                    break
            if bound is not None and rounds >= bound:
                self.loop_stop_reason = "max_iterations"
                break
            if rounds >= MAX_SPELL_LOOP_ITERATIONS:
                self._stop(ExecutionStatus.INTERRUPTED, InterruptReason.LOOP_GUARD,
                           f"循环超过工程安全阀{MAX_SPELL_LOOP_ITERATIONS}次："
                           f"程序可能不满足任何规则终止条件")
                break
            rounds += 1
            self.loop_iterations = rounds
            executed_before = self._executed_count()
            self._run_steps(node.body, iteration=rounds if iteration == 0 else iteration)
            if not self.is_running:
                break
            if self._executed_count() == executed_before:
                # 本轮没有任何一步真正执行（例如条件不再成立、目标全部失效）：
                # 没有进展就不再空转——这是循环的规则终止条件之一，不是中断。
                self.loop_stop_reason = "no_progress"
                break

    def _evaluate_condition(self, node) -> bool:
        evaluator = getattr(self.engine, "_evaluate_spell_condition", None)
        if evaluator is None:  # 引擎契约错误
            self._stop(ExecutionStatus.FAILED, InterruptReason.INVALID_INPUT,
                       "引擎缺少 _evaluate_spell_condition 条件求值口")
            return False
        try:
            return bool(evaluator(node, self.caster, self.attacker))
        except Exception as exc:  # 条件求值异常 = 程序错误
            self._stop(ExecutionStatus.FAILED, InterruptReason.INVALID_INPUT,
                       f"条件求值失败: {exc}")
            return False

    def _caster_alive(self) -> bool:
        alive = getattr(self.caster, "is_alive", None)
        if callable(alive):
            alive = alive()
        if alive is False:
            return False
        if alive is None:
            hp = getattr(self.caster, "current_hp", 1)
            return hp > 0
        return True

    def _executed_count(self) -> int:
        return sum(1 for r in self.results
                   if r.status in (StepStatus.COMPLETED, StepStatus.DODGED))

    def _run_action(self, step, *, iteration: int) -> None:
        index = self._step_index.get(id(step))
        if index is None:
            # 决策槽位缺失（例如调用方给的 AST 与索引表不一致）= 程序错误
            self._stop(ExecutionStatus.FAILED, InterruptReason.INVALID_INPUT,
                       f"步骤未登记进决策索引表: {step!r}")
            return
        entry = self.request.steps[index] if index < len(self.request.steps) else StepRequest()
        result = self.engine._execute_single_daowen_step(
            definition=self.definition,
            step=step,
            entry=entry,
            caster=self.caster,
            attacker=self.attacker,
            refs=self.refs,
            trigger_label=self.trigger_label,
            target_resolver=self.target_resolver,
            skip_predicate=self.skip_predicate,
            policy=self.policy,
            step_key=str(index),
            iteration=iteration,
        )
        self.results.append(result)
        self.step_extras.append(
            {"trigger_spell_logs": list(result.trigger_spell_logs or [])})
        if self.on_step is not None:
            self.on_step(result)
        if result.status in (StepStatus.INTERRUPTED, StepStatus.FAILED):
            self._stop(
                ExecutionStatus.INTERRUPTED if result.status == StepStatus.INTERRUPTED
                else ExecutionStatus.FAILED,
                result.reason, result.detail,
            )

    # -- 日志 --
    def logs(self) -> list[dict]:
        """转换为旧 API 返回的 log 列表（对调用方透明）。

        若程序是在两个步骤之间被中止的（例如第 1 步的【再生】触发【癌变】
        命零，后面的步骤不再结算），补一条不带 daowen 的中止记录，
        让中断原因在日志里可见，而不是只在 execution.status 里。
        """
        out = []
        for idx, r in enumerate(self.results):
            entry = r.to_log()
            if self.definition.name:
                entry["spell"] = self.definition.name
            if r.iteration:
                entry["cycle"] = r.iteration
            extra = self.step_extras[idx] if idx < len(self.step_extras) else {}
            if extra.get("trigger_spell_logs"):
                entry["trigger_spell_logs"] = extra["trigger_spell_logs"]
            out.append(entry)
        if self.loop_stop_reason == "no_progress" and out:
            out[-1]["loop_stop"] = "no_progress"
        if self.interrupt_reason and (
                not self.results
                or self.results[-1].status not in (StepStatus.INTERRUPTED, StepStatus.FAILED)):
            tail = {"interrupted": self.interrupt_reason.value,
                    "detail": self.interrupt_detail}
            if self.definition.name:
                tail["spell"] = self.definition.name
            if self.loop_iterations:
                tail["cycle"] = self.loop_iterations
            out.append(tail)
        return out

    def step_summaries(self) -> list[dict]:
        """可 JSON 序列化的逐步结果摘要（cast 的返回值使用）。"""
        out = []
        for idx, r in enumerate(self.results):
            extra = self.step_extras[idx] if idx < len(self.step_extras) else {}
            out.append({
                "step": idx + 1, "status": r.status.value, "reason": r.reason.value or None,
                "daowen": r.daowen, "x": r.x, "target": r.target_name or None,
                "cost_paid": r.cost_paid, "mana_gained": r.mana_gained,
                "detail": r.detail or None,
                "iteration": r.iteration or None,
                "trigger_spell_logs": extra.get("trigger_spell_logs", []),
            })
        return out
