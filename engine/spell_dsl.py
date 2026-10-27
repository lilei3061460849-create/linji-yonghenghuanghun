"""自创法术 DSL：触发时机词汇表 + 条件表达式 + 效果流程解析。

设计目标（对应用户四点诉求）：
1. 触发时机不再限定"受到伤害前/失去生命后/目标发动道纹前"三选一，
   兼容 《规则正文》中出现过的全部[XX]时点词汇（战始/战终/回始/回终/
   敌回始/敌回终/受到伤害前后/失去生命前后/目标发动道纹前），
   且接受常见同义表述（"我方受到伤害前"/"战斗开始时"等）。
2. 句式错误在【学习】提交时就地报错、附带具体原因，不会出现
   "定义成功但因解析失败而在战斗里永远不触发"的静默哑火。
3. 支持真正的条件分支：若<条件>则<效果>否则<效果>，条件支持
   且/或/非组合与对生命/法力/血限/法限/速度/速限/护盾/道纹层数/
   状态的数值与布尔比较。
4. 效果流程里每一步都必须显式声明目标身份（自身/攻击者/目标/
   施法者/任意），"任意"在实际结算提交时才指定具体单位并复用
   发动道纹的合法性校验，不再按道纹类型静默猜测。
5. 循环：效果流程可显式声明"循环"，解析结果 loop=True，交给
   既有的"法力耗尽/流程中断即停止"结算语义（校验层另加一个工程
   保险丝上限，防止极端输入导致死循环，不是对循环语义的阉割）。

本模块只做“文本 → 结构化 AST”的解析与条件求值，不触碰战斗结算，
避免把复杂度引入 combat.py 的核心路径；combat.py 只需要调用
`parse_spell(spell)` 拿到统一结构，再驱动已有的发动道纹/伤害管线。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


class SpellDslError(ValueError):
    """自创法术文本解析失败：必须携带具体、可行动的原因。"""


# ---------------------------------------------------------------------------
# 一、触发时机词汇表
# ---------------------------------------------------------------------------
# 触发条件写法不做"必须完全匹配某个固定字符串"的限制；
# 而是剥离常见的主语/助词修饰后，按核心关键词判定所属的规范时机。
# 规范时机与 engine.enums.ActionPhase / TriggerTiming 的字面量保持一致，
# 便于 combat.py 直接拿去对接既有枚举。

TRIGGER_BEFORE_DAMAGE = "受到伤害前"
TRIGGER_AFTER_DAMAGE = "受到伤害后"
TRIGGER_BEFORE_LIFE_LOST = "失去生命前"
TRIGGER_AFTER_LIFE_LOST = "失去生命后"
TRIGGER_TARGET_BEFORE_DAOWEN = "目标发动道纹前"
TRIGGER_BATTLE_START = "战始"
TRIGGER_BATTLE_END = "战终"
TRIGGER_ROUND_START = "回始"
TRIGGER_ROUND_END = "回终"
TRIGGER_SELF_TURN_END = "自身回合结束"
TRIGGER_ENEMY_ROUND_START = "敌回始"
TRIGGER_ENEMY_ROUND_END = "敌回终"
TRIGGER_INSTANT = "瞬发"

ALL_TRIGGERS = (
    TRIGGER_BEFORE_DAMAGE, TRIGGER_AFTER_DAMAGE,
    TRIGGER_BEFORE_LIFE_LOST, TRIGGER_AFTER_LIFE_LOST,
    TRIGGER_TARGET_BEFORE_DAOWEN,
    TRIGGER_BATTLE_START, TRIGGER_BATTLE_END,
    TRIGGER_ROUND_START, TRIGGER_ROUND_END, TRIGGER_SELF_TURN_END,
    TRIGGER_ENEMY_ROUND_START, TRIGGER_ENEMY_ROUND_END,
    TRIGGER_INSTANT,
)


# ---------------------------------------------------------------------------
# 开放可扩展：自定义事件触发条件（“新增时点 = 注册一条事件挂钩”）
#
# 原则：**凡是这里能让 parse_trigger 识别出的触发条件，引擎端都必须真的在
# 对应事件点调用一次 `_fire_auto_reaction(holder, "<name>", ctx)`**。二者必须
# 同步出现，否则会出现“语法接受却永不触发”的死时点（这正是用户明确否定的）。
#
# 新增一个可触发时点只需两步：
#   1) 在 EXTRA_TRIGGERS 注册一条：name（规范触发器名）+ keywords（命中规则）+
#      roles（允许的目标身份；无对手身份的用 ("self","caster","any")）。
#   2) 在引擎真正发生该事件的代码路径调用
#      self._fire_auto_reaction(holder, "<name>", ctx)。
# 注册后该时点即被 DSL 语法接受（学习/解析阶段不再拒绝），并在事件点时真正触发。
# ---------------------------------------------------------------------------
EXTRA_TRIGGERS: list[dict] = [
    # 闪避时：持有者成功闪避一次攻击后触发（引擎在 resolve_attack 的闪避成功分支接线）。
    {
        "name": "闪避时",
        "keywords": (("闪避", "避开", "躲避", "躲开"), ("时", "后", "成功")),
        "roles": ("self", "caster", "any", "target", "attacker"),
    },
]


def extra_trigger_names() -> tuple[str, ...]:
    return tuple(spec["name"] for spec in EXTRA_TRIGGERS)


def extra_trigger_roles(name: str) -> tuple[str, ...]:
    for spec in EXTRA_TRIGGERS:
        if spec["name"] == name:
            return tuple(spec["roles"])
    return ()

# 全局时点（战始/战终/回始/回终/敌回始/敌回终）没有"当次触发的对手"这个
# 天然身份——不像受到伤害前/失去生命后/目标发动道纹前那样，事件本身自带一个
# 明确的"攻击者/发动方"。因此效果步骤的目标声明在这六个时点里只能是
# 自身/施法者/任意目标；写"于攻击者"或"于目标"在【学习】阶段就直接拒绝，
# 不允许学会一个语义不成立的写法（避免结算时随便指定一个"攻击者"）。
GLOBAL_TRIGGERS = (
    TRIGGER_BATTLE_START, TRIGGER_BATTLE_END,
    TRIGGER_ROUND_START, TRIGGER_ROUND_END, TRIGGER_SELF_TURN_END,
    TRIGGER_ENEMY_ROUND_START, TRIGGER_ENEMY_ROUND_END,
)

# 去除掉不影响判定的主语/助词修饰词，只留核心时机描述。
_TRIGGER_STRIP_TOKENS = ("我方", "自身", "对方", "己方", "自己的", "时", "的", "（循环）", "(循环)")

# 每个规范时机的"核心关键词组合"判定规则：(必须包含其一的组, ...) 全部满足才算命中。
# 顺序很重要：先判定更具体/带“敌”字样的分支，避免被泛化关键词提前吃掉。
_TRIGGER_RULES: list[tuple[str, tuple[tuple[str, ...], ...], tuple[str, ...]]] = [
    (TRIGGER_ENEMY_ROUND_START,
     (("敌",), ("回合开始", "回始")), ()),
    (TRIGGER_ENEMY_ROUND_END,
     (("敌",), ("回合结束", "回终")), ()),
    (TRIGGER_ROUND_START,
     (("回合开始", "回始", "每回合开始"),), ("敌",)),
    (TRIGGER_ROUND_END,
     (("回合结束", "回终", "每回合结束"),), ("敌",)),
    (TRIGGER_BATTLE_START,
     (("战斗开始", "战始", "开局"),), ()),
    (TRIGGER_BATTLE_END,
     (("战斗结束", "战终", "结局"),), ()),
    (TRIGGER_TARGET_BEFORE_DAOWEN,
     (("发动道纹", "使用道纹", "出招", "发动法术"), ("前",)), ()),
    (TRIGGER_BEFORE_DAMAGE,
     (("受到伤害", "受伤", "承伤", "受到攻击"), ("前",)), ()),
    (TRIGGER_AFTER_DAMAGE,
     (("受到伤害", "受伤", "承伤", "受到攻击"), ("后",)), ()),
    (TRIGGER_BEFORE_LIFE_LOST,
     (("失去生命", "损失生命", "掉血", "扣血", "生命减少"), ("前",)), ()),
    (TRIGGER_AFTER_LIFE_LOST,
     (("失去生命", "损失生命", "掉血", "扣血", "生命减少"), ("后",)), ()),
]


def normalize_trigger_text(text: str) -> str:
    cleaned = (text or "").strip()
    for token in _TRIGGER_STRIP_TOKENS:
        cleaned = cleaned.replace(token, "")
    return cleaned


def parse_trigger(text: str) -> str:
    """把任意写法的触发条件解析为规范时机常量；解析失败抛 SpellDslError。"""
    raw = (text or "").strip()
    if not raw:
        raise SpellDslError("触发条件不能为空")
    # “自身回合结束”是玩家行动阶段结束、怪物阶段开始前的独立时点，
    # 不能先按“自身/的”剥词后误归入完整回合的“回终”（那发生在怪物阶段之后）。
    if any(phrase in raw for phrase in ("自身回合结束", "自己的回合结束", "己方回合结束")):
        return TRIGGER_SELF_TURN_END
    # 瞬发（trigger=immediate）是主动施法，不是事件时点：只接受"瞬发"/"immediate"
    # 这两种明确写法，不做模糊同义（"立即"等词常出现在事件触发描述里，会误判）。
    if raw in ("瞬发", "immediate", "trigger=immediate"):
        return TRIGGER_INSTANT
    cleaned = normalize_trigger_text(raw)
    if not cleaned:
        raise SpellDslError(f"触发条件【{raw}】剥离修饰词后为空，无法识别时机")
    for canonical, groups, forbidden in _TRIGGER_RULES:
        if any(bad in raw for bad in forbidden):
            continue
        if all(any(kw in cleaned for kw in group) for group in groups):
            return canonical
    # 开放可扩展：命中已注册的自定义事件触发条件也返回其规范名。
    # 这里对原始文本 raw 匹配（保留“时/后”等被 _TRIGGER_STRIP_TOKENS 剥离的措辞），
    # 与 _TRIGGER_RULES 对 cleaned 匹配不同，避免“闪避时”被剥成“闪避”而漏配。
    for spec in EXTRA_TRIGGERS:
        groups = spec["keywords"]
        if all(any(kw in raw for kw in group) for group in groups):
            return spec["name"]
    raise SpellDslError(
        f"无法识别触发条件【{raw}】。可用时机（支持常见同义写法，如“我方受到伤害前”“战斗开始时”）："
        f"{'/'.join(ALL_TRIGGERS + extra_trigger_names())}"
    )


# ---------------------------------------------------------------------------
# 二、条件表达式：且/或/非/比较，支持嵌套括号
# ---------------------------------------------------------------------------

_CMP_OPS = ("大于等于", "小于等于", "不等于", "大于", "小于", "等于")

_FIELD_ALIASES = {
    "生命": "hp", "当前生命": "hp",
    "血限": "blood_limit",
    "法力": "mana", "当前法力": "mana",
    "法限": "mana_limit",
    "速度": "speed", "当前速度": "speed",
    "速限": "speed_limit",
    "护盾": "shield", "格挡": "shield",
    "场上最高怪物攻击力": ("battle_metric", "max_monster_attack_power"),
    "场上怪物攻击力总和": ("battle_metric", "sum_monster_attack_power"),
    "癌变安全余量": ("battle_metric", "cancer_safety_margin"),
}

_SUBJECT_ALIASES = {
    "自身": "self", "自己": "self",
    "攻击者": "attacker", "敌方": "attacker", "对方": "attacker",
    "目标": "target",
    "施法者": "caster",
}


@dataclass(frozen=True)
class Cmp:
    subject: str          # self/attacker/target/caster
    field: str            # hp/mana/... 或 ("daowen_x", 道纹名) 或 ("status", 状态名)
    op: str                # >, <, >=, <=, ==, !=, has, lacks
    value: Any              # int 或 None（has/lacks 不需要数值）
    raw: str = ""


@dataclass(frozen=True)
class BoolOp:
    op: str                 # "and" / "or"
    parts: tuple


@dataclass(frozen=True)
class Not:
    inner: Any


class _CondTokenizer:
    """把条件文本切成 token 序列：主语、字段、比较词、数值、且/或/非、括号。"""

    _TOKEN_RE = re.compile(
        r"\s*(场上最高怪物攻击力|场上怪物攻击力总和|癌变安全余量|且|或|非|\(|（|\)|）|" + "|".join(_CMP_OPS) + r"|拥有|没有|层|-?\d+|"
        r"[\u4e00-\u9fa5A-Za-z]+)"
    )

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.tokens: list[str] = []
        self._tokenize()

    def _tokenize(self):
        s = self.text
        i = 0
        while i < len(s):
            m = self._TOKEN_RE.match(s, i)
            if not m:
                if s[i].isspace():
                    i += 1
                    continue
                raise SpellDslError(f"条件表达式在第{i}个字符附近无法解析：...{s[max(0,i-5):i+5]}...")
            tok = m.group(1)
            self.tokens.append(tok)
            i = m.end()
        # 括号统一
        self.tokens = ["(" if t == "（" else ")" if t == "）" else t for t in self.tokens]


class _CondParser:
    """递归下降：or_expr := and_expr (\"或\" and_expr)*；and_expr := unary (\"且\" unary)*；
    unary := \"非\" unary | \"(\" or_expr \")\" | comparison
    """

    def __init__(self, tokens: list[str], raw_text: str):
        self.tokens = tokens
        self.i = 0
        self.raw_text = raw_text

    def _peek(self) -> Optional[str]:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _advance(self) -> str:
        tok = self._peek()
        if tok is None:
            raise SpellDslError(f"条件表达式【{self.raw_text}】提前结束，缺少内容")
        self.i += 1
        return tok

    def parse(self):
        node = self._or_expr()
        if self.i != len(self.tokens):
            raise SpellDslError(f"条件表达式【{self.raw_text}】在末尾有多余内容：{self.tokens[self.i:]}")
        return node

    def _or_expr(self):
        parts = [self._and_expr()]
        while self._peek() == "或":
            self._advance()
            parts.append(self._and_expr())
        return parts[0] if len(parts) == 1 else BoolOp("or", tuple(parts))

    def _and_expr(self):
        parts = [self._unary()]
        while self._peek() == "且":
            self._advance()
            parts.append(self._unary())
        return parts[0] if len(parts) == 1 else BoolOp("and", tuple(parts))

    def _unary(self):
        if self._peek() == "非":
            self._advance()
            return Not(self._unary())
        if self._peek() == "(":
            self._advance()
            node = self._or_expr()
            if self._peek() != ")":
                raise SpellDslError(f"条件表达式【{self.raw_text}】括号未闭合")
            self._advance()
            return node
        return self._comparison()

    def _comparison(self):
        subject_tok = self._advance()
        subject = _SUBJECT_ALIASES.get(subject_tok)
        if subject is None:
            raise SpellDslError(
                f"条件表达式【{self.raw_text}】主语【{subject_tok}】非法，"
                f"必须是{'/'.join(_SUBJECT_ALIASES)}之一")
        field_tok = self._advance()
        # 状态判断："自身 拥有 <状态名>" / "自身 没有 <状态名>"
        if field_tok in ("拥有", "没有"):
            status_tok = self._advance()
            return Cmp(subject=subject, field=("status", status_tok),
                       op="has" if field_tok == "拥有" else "lacks", value=None,
                       raw=self.raw_text)
        # 道纹层数："自身 <道纹名>层数 大于 3"
        if field_tok.endswith("层数"):
            daowen_name = field_tok[:-2]
            field_key = ("daowen_stacks", daowen_name)
        elif field_tok in _FIELD_ALIASES:
            field_key = _FIELD_ALIASES[field_tok]
        else:
            raise SpellDslError(
                f"条件表达式【{self.raw_text}】字段【{field_tok}】非法，"
                f"必须是{'/'.join(_FIELD_ALIASES)}或“<道纹名>层数”")
        op_tok = self._advance()
        op_map = {"大于": ">", "小于": "<", "大于等于": ">=", "小于等于": "<=",
                  "等于": "==", "不等于": "!="}
        if op_tok not in op_map:
            raise SpellDslError(f"条件表达式【{self.raw_text}】比较词【{op_tok}】非法")
        value_tok = self._advance()
        # 支持 "血限的一半" 这种派生值：解析为 (字段, 分母)
        if self._peek() == "的" or value_tok == "的":
            pass  # 简化：不特殊处理连接词，下面统一按数值/百分比解析
        try:
            value = int(value_tok)
        except ValueError:
            raise SpellDslError(f"条件表达式【{self.raw_text}】比较值【{value_tok}】必须是整数")
        return Cmp(subject=subject, field=field_key, op=op_map[op_tok], value=value,
                   raw=self.raw_text)


def parse_condition(text: str):
    """解析条件表达式文本为 AST；解析失败抛 SpellDslError。"""
    raw = (text or "").strip()
    if not raw:
        raise SpellDslError("条件表达式不能为空")
    # 允许自然中文连续书写“自身场上最高怪物攻击力”，内部规范化为
    # “自身 场上最高怪物攻击力”，保持条件语法可扩展而不为每个字段开专门分支。
    for subject in _SUBJECT_ALIASES:
        raw = raw.replace(subject, subject + " ")
    tokens = _CondTokenizer(raw).tokens
    if not tokens:
        raise SpellDslError(f"条件表达式【{raw}】无法切分出任何有效内容")
    return _CondParser(tokens, raw).parse()


def evaluate_condition(node, resolver) -> bool:
    """resolver: 一个把 (subject, field) 映射为实际数值/布尔的可调用对象。

    resolver(subject: str, field) -> int（数值字段）或 bool（status has/lacks 已经算好的情况下不会走这里）。
    为了保持 spell_dsl 与 Entity 完全解耦（不 import engine.models），
    数值/状态的真实取值交给调用方传入的 resolver 闭包完成。
    """
    if isinstance(node, BoolOp):
        if node.op == "and":
            return all(evaluate_condition(p, resolver) for p in node.parts)
        return any(evaluate_condition(p, resolver) for p in node.parts)
    if isinstance(node, Not):
        return not evaluate_condition(node.inner, resolver)
    if isinstance(node, Cmp):
        if node.op in ("has", "lacks"):
            has_it = bool(resolver(node.subject, node.field))
            return has_it if node.op == "has" else not has_it
        actual = resolver(node.subject, node.field)
        if not isinstance(actual, (int, float)):
            raise SpellDslError(f"字段{node.field}未能解析出数值")
        ops = {">": actual > node.value, "<": actual < node.value,
               ">=": actual >= node.value, "<=": actual <= node.value,
               "==": actual == node.value, "!=": actual != node.value}
        return ops[node.op]
    raise SpellDslError(f"未知条件节点: {node!r}")


# ---------------------------------------------------------------------------
# 三、效果流程：发动道纹步骤 + 条件分支 + 循环
#
# AST 三种节点（执行器的程序体，不在解析/准备阶段展开）：
#   ActionStep  发动单个道纹（法术的一步）
#   IfStep      条件分支；条件在**执行到本节点时**按当时的真实状态求值
#   LoopStep    循环；由执行器自己迭代，条件/资源每轮重新读取
#
# 旧架构把 IfStep 在准备阶段展开、把 LoopStep 交给调用方用 cycles 预展开，
# 两个表示现在都已删除：调用方只提交"程序 + 每步决策"，控制流归执行器。
# ---------------------------------------------------------------------------

_TARGET_ALIASES = {
    "自身": "self", "自己": "self",
    "攻击者": "attacker", "敌方": "attacker",
    "目标": "target",
    "施法者": "caster",
    "任意目标": "any", "任意": "any", "指定目标": "any",
}

_ACTION_RE = re.compile(
    r"发动\s*(?P<daowen>[\u4e00-\u9fa5]{2,4})\s*X\s*(?:于|对)\s*(?P<target>[\u4e00-\u9fa5]{2,4})"
)
_ACTION_NO_TARGET_RE = re.compile(r"发动\s*(?P<daowen>[\u4e00-\u9fa5]{2,4})\s*X\b")

_LOOP_MARKERS = ("循环直到法力耗尽", "循环至法力耗尽", "循环", "（循环）", "(循环)")
# 定次循环：仅在本节点末尾识别"循环N次"（N≥1）。
_LOOP_COUNT_RE = re.compile(r"循环\s*(\d+)\s*次\s*$")


@dataclass(frozen=True)
class ActionStep:
    daowen: str
    target: str    # self/attacker/target/caster/any


@dataclass(frozen=True)
class IfStep:
    condition: Any
    then_steps: tuple
    else_steps: tuple


@dataclass(frozen=True)
class LoopStep:
    """循环体（用户法则二【循环】）。

    max_iterations=None 表示规则循环：只要还能执行（法力足够、流程未中断、
    本轮至少实际执行了一步），就继续下一轮；由执行器在每轮开始前重新读取
    当前状态。max_iterations=N 表示写法"循环N次"的定次循环。

    工程安全阀 MAX_SPELL_LOOP_ITERATIONS 不属于游戏规则，见 spell_execution。
    """
    body: tuple
    max_iterations: Optional[int] = None


def _split_top_level(text: str, sep: str) -> list[str]:
    """按分隔符切分，但跳过括号内的分隔符（本 DSL 括号只出现在条件里，效果流程本身不嵌套括号分组）。"""
    parts = []
    depth = 0
    buf = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "(（":
            depth += 1
        elif ch in ")）":
            depth -= 1
        if text[i:i + len(sep)] == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += len(sep)
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p for p in (s.strip() for s in parts) if p]


def _split_first_top_level(text: str, sep: str):
    """在括号深度 0 处第一次出现 sep 的位置切分；找不到返回 None。"""
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "(（":
            depth += 1
        elif ch in ")）":
            depth -= 1
        if depth == 0 and text[i:i + len(sep)] == sep:
            return text[:i], text[i + len(sep):]
        i += 1
    return None


def _count_top_level(text: str, sep: str) -> int:
    depth = 0
    count = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "(（":
            depth += 1
        elif ch in ")）":
            depth -= 1
        if depth == 0 and text[i:i + len(sep)] == sep:
            count += 1
            i += len(sep)
            continue
        i += 1
    return count


def _unwrap_group(text: str) -> str:
    """如果整段文本被一对括号包住（分组写法），脱掉这层括号。"""
    t = text.strip()
    if len(t) >= 2 and t[0] in "(（" and t[-1] in ")）":
        depth = 0
        for i, ch in enumerate(t):
            if ch in "(（":
                depth += 1
            elif ch in ")）":
                depth -= 1
                if depth == 0 and i != len(t) - 1:
                    return t  # 并没有包住整段
        return t[1:-1].strip()
    return t


def _split_steps_text(text: str) -> list[str]:
    """把一个分支体切成若干子句：子句分隔符"；"与"→"等价（顶层）。"""
    out: list[str] = []
    for part in _split_top_level(text, "；"):
        out.extend(_split_top_level(part, "→"))
    return [p for p in (s.strip() for s in out) if p]


def _parse_action_clause(clause: str, known_daowen: set[str]) -> ActionStep:
    m = _ACTION_RE.search(clause)
    if m:
        daowen = m.group("daowen")
        target_tok = m.group("target")
        target = _TARGET_ALIASES.get(target_tok)
        if target is None:
            raise SpellDslError(
                f"效果步骤【{clause}】目标【{target_tok}】非法，"
                f"必须显式声明为{'/'.join(sorted(set(_TARGET_ALIASES.values())))}"
                f"（写法如“于自身”“于攻击者”“于任意目标”）")
    else:
        m2 = _ACTION_NO_TARGET_RE.search(clause)
        if not m2:
            raise SpellDslError(
                f"效果步骤【{clause}】无法识别，必须是“发动<道纹>X于<目标>”的形式")
        raise SpellDslError(
            f"效果步骤【{clause}】缺少显式目标声明，必须写成"
            f"“发动{m2.group('daowen')}X于自身/攻击者/目标/施法者/任意目标”之一")
    daowen = m.group("daowen")
    if daowen not in known_daowen:
        raise SpellDslError(f"效果步骤【{clause}】引用了不存在的道纹【{daowen}】")
    tail = clause.replace(m.group(0), "", 1).strip()
    if tail:
        raise SpellDslError(
            f"效果步骤【{clause}】在动作之后还有无法识别的内容【{tail}】；"
            f"多个动作请用“→”或“；”分隔，嵌套条件分支请用括号分组")
    return ActionStep(daowen=daowen, target=target)


def parse_effect_flow(text: str, known_daowen: set[str]):
    """解析效果流程文本，返回 (steps, loop: bool)。

    steps 是 ActionStep / IfStep 的列表；若文本声明了循环，则 steps 是
    单独一个 LoopStep（循环体为其余步骤）。
    顶层用"→"分隔多个步骤；条件分支写法："若<条件>则<效果子句>[否则<效果子句>]"，
    子句内可用"；"分隔多个步骤，且允许继续嵌套条件分支（不设人为深度上限）。
    循环写法（整条流程末尾）："循环"、"循环直到法力耗尽"（规则循环），
    或"循环N次"（定次循环，N≥1）。
    """
    raw = (text or "").strip()
    if not raw:
        raise SpellDslError("效果流程不能为空")

    loop = False
    max_iterations: Optional[int] = None
    body = raw
    count_match = _LOOP_COUNT_RE.search(body)
    if count_match:
        max_iterations = int(count_match.group(1))
        if max_iterations < 1:
            raise SpellDslError("效果流程【%s】的循环次数必须≥1" % raw)
        body = body[: count_match.start()].rstrip("→ ")
        loop = True
    else:
        for marker in _LOOP_MARKERS:
            if body.endswith(marker):
                body = body[: -len(marker)].rstrip("→ ")
                loop = True
                break

    if not body:
        raise SpellDslError("效果流程去除循环标记后为空")

    steps = []
    for clause in _split_top_level(body, "→"):
        steps.append(_parse_clause(clause, known_daowen))
    if not steps:
        raise SpellDslError(f"效果流程【{raw}】未解析出任何有效步骤")
    if loop:
        steps = [LoopStep(body=tuple(steps), max_iterations=max_iterations)]
    return steps, loop


def _parse_clause(clause: str, known_daowen: set[str]):
    """解析一个子句：条件分支或单步动作（条件分支内部可继续嵌套）。"""
    if clause.startswith("若"):
        return _parse_if_clause(clause, known_daowen)
    return _parse_action_clause(clause, known_daowen)


def _parse_if_clause(clause: str, known_daowen: set[str]) -> IfStep:
    """解析"若<条件>则<子句集>[否则<子句集>]"。

    嵌套条件分支时必须用括号把分支体分组（否则无法判定"否则"属于哪一层）：
      若A则（若B则C否则D）否则E
    未用括号时，只允许一层"否则"，且"若"出现在分支体里而没有分组会直接报错，
    不会静默丢弃内容。
    """
    if not clause.startswith("若"):
        raise SpellDslError(f"条件分支【{clause}】必须以“若”开头")
    split = _split_first_top_level(clause[1:], "则")
    if split is None:
        raise SpellDslError(f"条件分支【{clause}】缺少“则”")
    cond_text, remainder = split
    else_split = _split_first_top_level(remainder, "否则")
    if else_split is None:
        then_text, else_text = remainder, ""
    else:
        then_text, else_text = else_split
    if _count_top_level(remainder, "否则") > 1:
        # 多个顶层"否则"无法判定归属：要求显式括号分组。
        if not (then_text.strip().startswith(("(", "（"))
                and then_text.strip().endswith((")", "）"))):
            raise SpellDslError(
                f"条件分支【{clause}】出现多个顶层“否则”，无法判定归属；"
                f"嵌套条件分支请用括号分组，如：若A则（若B则C否则D）否则E")
    condition = parse_condition(cond_text)
    then_text = _unwrap_group(then_text)
    else_text = _unwrap_group(else_text)
    then_steps = tuple(_parse_clause(c, known_daowen) for c in _split_steps_text(then_text))
    else_steps = tuple(_parse_clause(c, known_daowen) for c in _split_steps_text(else_text)) \
        if else_text.strip() else ()
    if not then_steps:
        raise SpellDslError(f"条件分支【{clause}】的“则”分支不能为空")
    return IfStep(condition=condition, then_steps=then_steps, else_steps=else_steps)


# ---------------------------------------------------------------------------
# 四、对外统一入口
# ---------------------------------------------------------------------------

@dataclass
class ParsedSpell:
    trigger: str
    steps: list
    loop: bool


def _check_condition_subjects_no_attacker(trigger: str, node) -> None:
    """条件表达式的主语同样不能在全局时点里引用 attacker/target。"""
    if isinstance(node, Cmp):
        if node.subject in ("attacker", "target"):
            raise SpellDslError(
                f"触发时机【{trigger}】没有“攻击者/目标”这个对手身份，"
                f"条件表达式【{node.raw}】不能以攻击者/敌方/对方/目标为主语，"
                f"请改用自身/施法者")
    elif isinstance(node, Not):
        _check_condition_subjects_no_attacker(trigger, node.inner)
    elif isinstance(node, BoolOp):
        for part in node.parts:
            _check_condition_subjects_no_attacker(trigger, part)


def _check_global_trigger_targets(trigger: str, steps) -> None:
    """按触发时点允许的目标身份校验效果步骤。

    - 全局时点（战始/战终/回始/回终/敌回始/敌回终）没有攻击者/目标身份，
      只能声明 self/caster/any；写于攻击者/于目标直接拒绝，条件表达式同理不能以
      攻击者/目标为主语。
    - 开放可扩展时点（EXTRA_TRIGGERS）按注册时的 roles 约束；未注册 roles 则不
      额外限制（等同伤害类，允许全部身份）。
    """
    if trigger in GLOBAL_TRIGGERS:
        allowed = ("self", "caster", "any")
    elif trigger == TRIGGER_INSTANT:
        # 瞬发：主动施法，没有"攻击者"；"目标"=本次施法指定的目标。
        # 条件表达式仍不能以攻击者/目标为主语（条件求值器里 target 映射到持有者，
        # 语义不成立；执行期条件求值属于 Phase 3）。
        allowed = ("self", "caster", "target", "any")
    else:
        roles = extra_trigger_roles(trigger)
        if not roles:
            return  # 非全局、非注册时点：保持原有行为（由 combat 侧结算）
        allowed = roles

    for step in steps:
        if isinstance(step, ActionStep):
            if step.target not in allowed:
                raise SpellDslError(
                    f"触发时机【{trigger}】没有“攻击者/目标”这个对手身份"
                    f"（不像受到伤害前/失去生命后那样天然存在一个触发对方），"
                    f"效果步骤【发动{step.daowen}X于...】只能声明"
                    f"“于自身”“于施法者”或“于任意目标”")
        elif isinstance(step, IfStep):
            if "self" in allowed or "caster" in allowed:
                _check_condition_subjects_no_attacker(trigger, step.condition)
            _check_global_trigger_targets(trigger, step.then_steps)
            _check_global_trigger_targets(trigger, step.else_steps)
        elif isinstance(step, LoopStep):
            _check_global_trigger_targets(trigger, step.body)



def parse_spell_definition(trigger_condition: str, effect_flow: str,
                            known_daowen: set[str]) -> ParsedSpell:
    """自创法术提交时的完整语法校验入口：解析失败抛出 SpellDslError，
    调用方（engine/api.py 的学习流程）必须把异常信息原样返回给用户，
    不允许吞掉错误静默放行——这是本次修复"句式错误要提醒"的关键点。
    """
    trigger = parse_trigger(trigger_condition)
    steps, loop = parse_effect_flow(effect_flow, known_daowen)
    _check_global_trigger_targets(trigger, steps)
    return ParsedSpell(trigger=trigger, steps=steps, loop=loop)


def parse_instant_flow(effect_flow: str, known_daowen: set[str]) -> ParsedSpell:
    """瞬发法术（cast(flow=...)）的效果流程解析入口。

    与 parse_spell_definition 共用 parse_effect_flow 与目标身份校验，只是触发
    时机固定为 TRIGGER_INSTANT，不需要触发条件文本。循环由统一的 LoopStep
    执行器处理，瞬发与触发型法术语义一致（规则循环/定次循环都支持）。
    """
    steps, loop = parse_effect_flow(effect_flow, known_daowen)
    _check_global_trigger_targets(TRIGGER_INSTANT, steps)
    return ParsedSpell(trigger=TRIGGER_INSTANT, steps=steps, loop=loop)


def describe_condition(node) -> str:
    """把条件 AST 还原成可读文本，供 prepare 接口展示给决策方（不影响判定本身）。"""
    if isinstance(node, Cmp):
        return node.raw
    if isinstance(node, Not):
        return f"非({describe_condition(node.inner)})"
    if isinstance(node, BoolOp):
        sep = " 且 " if node.op == "and" else " 或 "
        return "(" + sep.join(describe_condition(p) for p in node.parts) + ")"
    return "?"


def collect_step_daowen(steps) -> set[str]:
    """递归收集一个 steps 列表里出现过的所有道纹名（含 if 分支/循环体内部）。"""
    names: set[str] = set()
    for step in steps:
        if isinstance(step, ActionStep):
            names.add(step.daowen)
        elif isinstance(step, IfStep):
            names |= collect_step_daowen(step.then_steps)
            names |= collect_step_daowen(step.else_steps)
        elif isinstance(step, LoopStep):
            names |= collect_step_daowen(step.body)
    return names


def _walk_action_steps(steps, optional: bool = False):
    """深度优先枚举程序里的 ActionStep（含所有条件分支与循环体）。

    optional=True 表示该步位于条件分支内部，本次执行不一定会走到；
    决策列表仍然必须覆盖它（校验与结算永远看到同一组槽位）。
    """
    for step in steps:
        if isinstance(step, ActionStep):
            yield step, optional
        elif isinstance(step, IfStep):
            yield from _walk_action_steps(step.then_steps, True)
            yield from _walk_action_steps(step.else_steps, True)
        elif isinstance(step, LoopStep):
            yield from _walk_action_steps(step.body, optional)


def iter_action_steps(steps):
    """按规范的深度优先顺序枚举程序里全部 ActionStep。

    执行器用这个顺序给每一步绑定调用方提交的决策；未执行到的分支的决策被忽略，
    循环体里的步骤在每一轮复用同一条决策（X/目标由调用方一次提交）。
    产出 (index, step)：index 是稳定下标，供决策列表对齐。
    """
    for index, (step, _optional) in enumerate(_walk_action_steps(steps)):
        yield index, step


def iter_action_slots(steps):
    """同 iter_action_steps，但额外给出该步是否位于条件分支内（schema 用）。

    产出 (index, step, optional)。顺序与 iter_action_steps 完全一致。
    """
    for index, (step, optional) in enumerate(_walk_action_steps(steps)):
        yield index, step, optional


def count_action_steps(steps) -> int:
    """程序里全部 ActionStep 的数量（决策列表必须与之等长）。"""
    return sum(1 for _ in _walk_action_steps(steps))


def _count_tier_slots(steps) -> int:
    """程序的「重」数：每个效果步骤各一重（条件分支两臂各算、循环体按写出的步数算）。"""
    n = 0
    for step in steps or ():
        if isinstance(step, IfStep):
            n += _count_tier_slots(step.then_steps) + _count_tier_slots(step.else_steps)
        elif isinstance(step, LoopStep):
            n += _count_tier_slots(step.body)
        elif isinstance(step, (tuple, list)) and len(step) >= 2:
            n += 1  # 旧内置流程表的 (道纹, 身份) 写法
        else:
            n += 1
    return n


def spell_tier(trigger, steps) -> int:
    """法术阶数 = 重数（2026-10-25 用户令）。

    一重 = 生效流程里的一个环节：
    - 触发时机算一重（「失去生命后」「受到伤害前」都是流程的第一环）；
    - 每个效果步骤各算一重，同一种道纹重复出现照算，不再按种类去重；
    - 循环标记不另计重——它是「终点回到起点」，不产生新环节。

    不带触发时机的瞬发法术只按步骤计，所以「直接发动杀伐X」是 1 阶。
    阶数不设上限（2026-10-26 用户令）：阶数本身就是成本，重数越多要付的出手越多。
    """
    raw = getattr(trigger, "value", trigger)
    n = _count_tier_slots(steps)
    if raw and raw != TRIGGER_INSTANT:
        n += 1
    return max(1, n)
