"""正式运行时（production）的本地规则助手 —— 不是决策 AI。

本模块只放**生产路径自身需要**的规则级助手，判据是「引擎/正式入口在运行时
直接调用它」，而不是「模拟脚本喜欢它」：

* ``choose_dodge``：引擎在「道纹伤害 → 自动反应法术」路径上的闪避启发式
  （``engine/combat_parts/spells.py`` 调用；规则依据是闪避本身，不依赖 TacticalAI）。
* ``pick_wave_dodge_targets``：把 prepare 给出的波及候选裁成恰好 X 个目标
  （``engine/ai_player.PlaceholderBackend`` 提交怪物阶段时调用）。
* 战始/回始可选遗物与法器的显式决策（``PlaceholderBackend`` 调用）：
  引擎强制逐件显式提交，这些函数提供「可以不用，但不能不让用」的离线策略。
* ``pick_ally_daowen_x``：微光者自选 X 的整场预算分配器
  （``engine/api.py`` 结算微光者道纹时调用）。

**硬约束（2026-10-02 架构收口）**：本模块不得 import
``engine.ai_tactics`` / ``engine.ai_preview`` / ``sim.*``。
方向永远是 tests/probes/sim → production；生产不得反向依赖模拟层。
正式决策路径是 LLM → 引擎，规则 AI（TacticalAI）只作为隔离的实验工具存在。
"""
from __future__ import annotations

import math
from typing import Optional

from .daowen import DaoWenEngine
from .models import Entity


# ---------------------------------------------------------------------------
# 闪避启发式（引擎自动反应路径）
# ---------------------------------------------------------------------------


def choose_dodge(engine, per_hit_damage: int, *, budget_used: int = 0,
                 max_dodges: int = 2, min_hit_pct: float = 0.10,
                 entity=None) -> bool:
    """AI 闪避决策（供 sim 怪物阶段解析器调用，处理轮回者受到的攻击）。

    规则依据（规则正文·基础定义）：被选为[目标]后可消耗 1 点当前速度完全闪避。
    - 速度不足/必中已由引擎拒绝，这里只做预算与收益判断；
    - 每回合最多闪避 max_dodges 次（留速度应对残韵/回锋刀等）；
    - 只闪避会伤 ≥ min_hit_pct×[血限] 的命中，低伤不浪费速度。

    entity：被选定方实体。缺省时取 engine.state.player（历史行为，调用方不变）。
    2026-08-31 新增该参数，供引擎「道纹伤害 → 自动反应法术」路径指定任意被选定方
    （死斗里被反打的一方未必是 state.player），使闪避判定不再被跳过
    （DM 裁定：法术只是自定义触发条件的道纹，道纹要遵守的规则法术一样要遵守；
    规则正文·推演铁律5 禁止跳过闪避判定）。
    """
    p = entity
    if p is None:
        _state = getattr(engine, "state", None)
        p = getattr(_state, "player", None) if _state is not None else None
    if p is None or not p.is_alive:
        return False
    if p.current_speed <= budget_used:
        return False
    if p.has_status("固执"):
        return False            # 固执3：单次失去生命≤1，无需闪避
    if per_hit_damage < max(3, math.ceil(p.blood_limit * min_hit_pct)):
        return False
    if budget_used >= max_dodges:
        return False
    return True


# ---------------------------------------------------------------------------
# 怪物阶段提交助手（PlaceholderBackend）
# ---------------------------------------------------------------------------


def pick_wave_dodge_targets(option: dict) -> list[dict]:
    """波及X：从prepare的dodge_target_options中恰好选X个目标（对侧优先）。

    规则要求显式提交恰好X个不重复目标：此前各解析器把dodge_target_options全量
    提交，候选数大于X时必然被resolve拒收（2026-08-22 BUG-01配套修复）。
    DM裁定2026-08-23自适应降X：prepare在面板X>合法目标数时把有效X降到
    wave_effective_x=min(面板X, 候选数)，此处必须按有效X取目标，否则提交数≠
    结算侧mark_count必被拒。
    优先选怪物对侧（玩家方）目标——把后续道纹扩散打到敌方才符合怪物意图；
    对侧不足X时以其余合法目标补齐。
    """
    candidates = list(option.get("dodge_target_options") or [])
    need = int(option.get("wave_effective_x") or 0)
    if not need:
        need = min(int(option.get("x", 0) or 0), len(candidates))
    hostiles = [t for t in candidates if not str(t.get("ref", "")).startswith("enemy:")]
    others = [t for t in candidates if str(t.get("ref", "")).startswith("enemy:")]
    picked = (hostiles + others)[:need]
    return [{"target_ref": t["ref"], "dodge": False, "blood_shadow": False}
            for t in picked]


# ---------------------------------------------------------------------------
# 可选遗物 / 法器显式决策（战始 / 回始 / 战斗内）
# ---------------------------------------------------------------------------


def battle_start_relic_choices(engine) -> dict:
    """战始可选遗物显式决策。

    - 猩红果实：流血10 → [战终]血限+2（永久成长）。付得起就用。
    （2026-10-13 用户令：【三相残韵盘】遗物删除，决策分支一并移除。）
    """
    active = {r.name for r in engine.state.relics
              if getattr(r, "enabled", True) and engine.state.sealed_relics.get(r.name, 0) <= 0}
    p = engine.state.player
    out: dict = {}
    if "猩红果实" in active and p is not None:
        affordable = p.current_hp >= 10 + max(10, math.ceil(p.blood_limit * 0.15))
        out["猩红果实"] = {"use": affordable}
    # 2026-10-11：【苍白之花】重做为被动（当前法力可超出[法限]），战始无需决策。
    # 【回锋刀】的战始目标提交随之删除——它原先挂在「苍白之花是否付疲惫5」上，
    # 属规则18禁止的硬编码遗物联动；回锋刀只保留索引里的两个触发点：
    # 「每失去1点速度后」与[回始]（后者由 round_start_relic_choices 决策）。
    return out


# ---------------------------------------------------------------------------
# 回始遗物
# ---------------------------------------------------------------------------

def round_start_relic_choices(engine) -> dict:
    """回始遗物显式决策（回锋刀原有；余火印主动使用）。

    - 回锋刀：[回始]对目标造成 3×(速限-当前速度) 伤害，需显式目标。
    - 余火印：消耗龙心耐久X → +2X法力。有可用龙心且法力有缺口时用（X≤2）。

    2026-10-12 用户令：【共罪】删掉与【透支】重合的「流血4X换X法力」后不再是回始
    主动遗物，`共罪`/`对手共罪` 决策一并移除；它的平分功能不需要回始决策。
    """
    choices: dict = {}
    active = {r.name for r in engine.state.relics
              if getattr(r, "enabled", True) and engine.state.sealed_relics.get(r.name, 0) <= 0}
    p = engine.state.player
    if "回锋刀" in active and p is not None and p.speed_limit > p.current_speed:
        alive = [i for i, enemy in enumerate(engine.state.enemies) if enemy.is_alive]
        if alive:
            choices["回锋刀"] = {"enemy_index": alive[0]}
    if "余火印" in active and p is not None:
        heart = next((item for item in engine.state.consumables
                      if item.kind == "dragon_heart" and item.current_uses >= 1), None)
        x = 0
        if heart is not None and p.current_mana <= p.mana_limit:
            x = min(2, heart.current_uses)
        choices["余火印"] = {"use": x >= 1, "x": max(1, x),
                             "heart_name": heart.name if heart is not None else ""}
    # 2026-08-26 死斗三事故的根因是「引擎要求提交对手共罪决策、sim 从不构建该键」。
    # 2026-10-12【共罪】不再有回始主动效果后，该决策键已取消，这一类空转不会再发生。
    return choices


# ---------------------------------------------------------------------------
# 战始窗口法器（AWAIT_ROUND_START，battle_start 之后、round_start 之前）
# ---------------------------------------------------------------------------

def try_select_shared_dragon_heart(engine, commit: bool = True) -> Optional[dict]:
    """共心环：[战始]选择一枚自身拥有的【××龙心】类型，本场全员可共享抵消代价。"""
    if "共心环" not in engine.state.artifacts_owned:
        return None
    if engine.state.shared_dragon_heart_type:
        return None
    heart = next((c for c in engine.state.consumables if c.kind == "dragon_heart"), None)
    if heart is None:
        return None
    params = {"dragon_heart_type": heart.dragon_heart_type}
    if not commit:
        return {"_plan": True, "action_type": "select_shared_dragon_heart", "params": params}
    return engine.execute_action("select_shared_dragon_heart", params)


def try_use_black_card(engine, commit: bool = True) -> Optional[dict]:
    """黑金名片：[战始]所有敌方[血限]减半，付出等量碎片（负债≤50）。

    收益巨大（全体敌方血限减半），代价是碎片。负债上限50；本策略再保守留出
    20碎片缓冲（工资/后续成长也要用），超过就不发动。
    """
    if "黑金名片" not in engine.state.artifacts_owned:
        return None
    enemies = [e for e in engine.state.enemies if e.is_alive]
    if not enemies:
        return None
    total = sum(math.ceil(e.blood_limit / 2) for e in enemies)
    if total <= 0:
        return None
    if total > engine.state.shards + 20:
        return None
    if not commit:
        return {"_plan": True, "action_type": "use_black_card", "params": {}}
    return engine.execute_action("use_black_card", {})


# ---------------------------------------------------------------------------
# 回始窗口法器（AWAIT_ROUND_START，每个 round_start 之前）
# ---------------------------------------------------------------------------

def try_use_crime_vault(engine, commit: bool = True) -> Optional[dict]:
    """罪业金库：[回始]消耗X碎片（X≤2%当前碎片）获得2X格挡。

    本回合受到的威胁超过当前格挡、且碎片充足时发动，补上缺口。
    """
    if "罪业金库" not in engine.state.artifacts_owned:
        return None
    p = engine.state.player
    threat = sum(e.attack_count * e.attack_power for e in engine.state.enemies if e.is_alive)
    if p is None or threat <= p.shield:
        return None
    cap = math.floor(engine.state.shards * 0.02)
    x = min(cap, max(1, math.ceil((threat - p.shield) / 2)))
    if x < 1 or engine.state.shards < 100:
        return None
    params = {"x": x}
    if not commit:
        return {"_plan": True, "action_type": "use_crime_vault", "params": params}
    return engine.execute_action("use_crime_vault", params)


# ---------------------------------------------------------------------------
# 战斗内法器（PLAYER_ACTIONS）
# ---------------------------------------------------------------------------

def try_fire_godfather_revolver(engine, commit: bool = True) -> Optional[dict]:
    """教父左轮：对[目标]打出 30%自身血限×本场使用次数 的【必中】伤害。

    每场 6 发（[战始]回满），不占用出手。能打死就打血最少，否则打血最多。
    """
    if "教父左轮" not in engine.state.artifacts_owned:
        return None
    if engine.state.godfather_revolver_uses >= engine.GODFATHER_REVOLVER_AMMO:
        return None
    enemies = [e for e in engine.state.enemies if e.is_alive]
    if not enemies:
        return None
    uses = engine.state.godfather_revolver_uses + 1
    damage = math.ceil(engine.state.player.blood_limit * 0.3) * uses
    killable = [e for e in enemies if e.current_hp <= damage]
    target = (min(killable, key=lambda e: e.current_hp) if killable
              else max(enemies, key=lambda e: e.blood_limit))
    idx = engine.state.enemies.index(target)
    params = {"target_ref": f"enemy:{idx}"}
    if not commit:
        return {"_plan": True, "action_type": "fire_godfather_revolver", "params": params}
    return engine.execute_action("fire_godfather_revolver", params)


# ---------------------------------------------------------------------------
# 微光者自选 X：整场预算分配
# ---------------------------------------------------------------------------


# 一池制：单次发动最多动用当前法力池的比例，余量留给后面的回合。
# 取 1/2 形成几何衰减（4→2→1→1…），池子永不归零。
MANA_POOL_SPEND_RATIO = 0.5

# 流血：单次发动最多动用当前生命的比例，且绝不把自己流死。
HP_SPEND_RATIO = 0.25

# 异变：累加到 MUTATION_COLLAPSE_THRESHOLD 就【迷失】命零，且跨战斗不回退，
# 是最贵的一种"预算"。留 20% 安全边距（与 TacticalAI.SELF_PRESERVE_MARGIN 同口径）。
MUTATION_SAFE_RATIO = 0.8

# 【冷却】类代价（如【固执】：冷却X场）不计入任何"池子"，X 越大锁的**场次**
# 越多，且是跨战斗的。微光者的道纹本就稀缺，一律取 1——绝不为了一回合效果
# 把自己锁上好几场。
COOLDOWN_COST_MAX_X = 1

# 持续类效果（duration == X）的 X 上限。duration 随 X 一起涨，而战斗很少超过
# 这个回合数，超出部分的持续回合等于白付代价。与怪物侧
# _persistent_duration_value 按"预期剩余回合"折价同源，只是这里取一个保守常数
# 而非逐局估算（微光者侧没有预演评分可用，见模块文档串）。调用方可覆盖。
DEFAULT_DURATION_HORIZON = 5

# 试探上限：与 Combat._DAOWEN_X_PROBE_CAP 一致，避免长循环。
X_PROBE_CAP = 30


def _can_pay(ally: Entity, calc: dict) -> bool:
    """硬性可负担性：付不起就是非法选项（与 Combat._monster_can_pay_calc_cost 同口径）。

    不复用后者的原因：它对 `entity_type != "怪物"` 一律 return True，
    对微光者等价于**没有代价上限**，X 可以开到天上去。
    """
    for key, capacity in (
            ("cost_hp", getattr(ally, "current_hp", 0)),
            ("cost_blood_limit", getattr(ally, "blood_limit", 0)),
            ("cost_speed", getattr(ally, "current_speed", 0)),
    ):
        amount = calc.get(key, 0)
        if amount and capacity < amount:
            return False
    # 发动【消耗】类道纹必须付法力（AI_EXPERIENCE.md:278「怪物与轮回者、微光者
    # 共用同一套面板数据，同样持有[血限]/[法限]/[速限]」；:1254 微光者一池制）。
    if calc.get("cost_type") == "消耗" and calc.get("cost", 0) > 0:
        if getattr(ally, "current_mana", 0) < calc["cost"]:
            return False
    # 仅对可能迷失的角色按生存线封顶；怪物照常累计异变。
    if calc.get("cost_type") == "异变" and ally.can_trigger_mutation_lost():
        headroom = Entity.MUTATION_COLLAPSE_THRESHOLD - getattr(ally, "mutation_count", 0)
        if calc.get("cost_mutation", 0) >= headroom:
            return False
    return True


def _within_budget(ally: Entity, calc: dict, x: int,
                   horizon: int = DEFAULT_DURATION_HORIZON) -> bool:
    """整场预算余量：付得起不等于该付。只约束**不回填**的资源。"""
    import math

    if calc.get("cost_type") == "冷却":
        return x <= COOLDOWN_COST_MAX_X

    # duration 随 X 一起涨的道纹：持续回合超出战斗视野就是白付的代价。
    if calc.get("duration") == x and x > horizon:
        return False

    if calc.get("cost_type") == "消耗" and calc.get("cost", 0) > 0:
        pool = getattr(ally, "current_mana", 0)
        if calc["cost"] > max(1, math.ceil(pool * MANA_POOL_SPEND_RATIO)):
            return False

    hp_cost = calc.get("cost_hp", 0) or 0
    if hp_cost:
        hp = getattr(ally, "current_hp", 0)
        if hp_cost > max(1, math.floor(hp * HP_SPEND_RATIO)):
            return False
        if hp - hp_cost <= 0:      # 绝不把自己流死
            return False

    if calc.get("cost_type") == "异变" and ally.can_trigger_mutation_lost():
        after = getattr(ally, "mutation_count", 0) + (calc.get("cost_mutation", 0) or 0)
        if after > Entity.MUTATION_COLLAPSE_THRESHOLD * MUTATION_SAFE_RATIO:
            return False
    return True


def pick_ally_daowen_x(ally: Entity, name: str, target: Entity | None = None,
                       *, cap: int = X_PROBE_CAP,
                       horizon: int = DEFAULT_DURATION_HORIZON) -> int:
    """为微光者挑一个 X：在「可负担」∩「留足整场余量」内取最大值。

    返回 0 表示连 X=1 都不该发动（付不起，或会把自己玩死），调用方应跳过该道纹
    而不是硬报错。面板写死 X（x_value>0）时由调用方优先用固定值，不走这里。
    """
    DaoWenEngine.register_all()
    best = 0
    affordable_x1 = False
    for x in range(1, max(0, cap) + 1):
        try:
            calc = DaoWenEngine.resolve(name, x, target=target, caster=ally)
        except ValueError:
            # X_LIMITS 之类的硬性上限（如【失忆】X≤当前道纹数量）会在此抛错
            break
        if not _can_pay(ally, calc):
            break
        if x == 1:
            affordable_x1 = True
        if not _within_budget(ally, calc, x, horizon):
            break
        best = x
    # 余量规则只负责**压上限**，不能把微光者压到彻底失声：只要 X=1 付得起且不会
    # 玩死自己，就至少开 X=1。否则池子小的时候（如岩行者 4 点、背负需 2X）预算
    # 上限会低于最小代价，剩下的法力将永远闲置。
    if best == 0 and affordable_x1:
        return 1
    return best
