"""微光者（[朋友]/[员工]）面板不写死 X 时，发动时自选 X 的契约测试。

2026-09-17 用户令：所有角色（含微光者）面板与怪物同格式，**道纹名后不带 X**，
X 由发动时自选，上限只受[法限]或代价限制。

微光者**不能**套用怪物侧的预演评分（sim/monster_targets.py::pick_monster_daowen_x），
两个独立阻断见 sim/ally_targets.py 模块文档串：
  1. 怪物靠遗物【某人的偏爱】每[回始]回满法力＝每回合预算；
     微光者按 AI_EXPERIENCE.md:1254 是一池制、[回始]不回填＝**整场预算**。
     按每回合最大化会在第一回合把整池砸光。
  2. _split_diff 只在 diff["player"]/["enemies"] 里找自己，不看 ["friends"]，
     朋友 actor 会退化成挑战者视角、评分反向。

验收判据不是"微光者变强了"，而是**它不会在第一回合把池子花光**。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity
from sim.ally_targets import pick_ally_daowen_x


def _ally(name="岩行者", hp=54, ac=2, ap=4, mut=0):
    e = Entity(name, "朋友", blood_limit=hp, current_hp=hp,
               attack_count=ac, attack_power=ap)
    e.mutation_count = mut
    return e


def _give(ally, name, cost_type="消耗", cost_formula="X", x=0):
    ally.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type=cost_type,
               cost_formula=cost_formula, effect_formula=""),
        x_value=x, x_free=(x == 0))
    return ally


def _engine_and_ally(ally, monster_hp=234):
    e = GameEngine()
    player = Entity("轮回者", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=100, current_mana=100,
                    speed_limit=9, current_speed=9, attack_count=1, attack_power=2)
    monster = Entity("测试怪", "怪物", blood_limit=monster_hp, current_hp=monster_hp,
                     attack_count=3, attack_power=14, mana_limit=14, current_mana=14,
                     speed_limit=3, current_speed=3)
    e.state.player = player
    e.state.friends = [ally]
    e.state.employees = []
    e.state.enemies = [monster]
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.current_round = 2
    return e, player, monster


# ---------------------------------------------------------------- 代价预算

def test_mana_pool_is_not_dumped_on_first_cast():
    """一池制：单次发动不得动用超过一半法力池，避免第一回合砸光整池。"""
    ally = _ally("岩行者", hp=54, ac=2, ap=4)      # 法力池 = 攻击力 = 4
    _give(ally, "背负")                             # 消耗 2X
    assert ally.current_mana == 4
    x = pick_ally_daowen_x(ally, "背负", ally)
    assert x == 1, f"池子 4、单次上限 2、代价 2X → 应取 X=1，实得 {x}"
    assert 2 * x <= ally.current_mana // 2 + 1


def test_pool_spreads_across_casts_rather_than_one_burst():
    """反复发动时池子被摊开使用，且不会退化成"永远只能发动一次"。"""
    ally = _ally("岩行者", hp=54, ac=2, ap=4)
    _give(ally, "背负")
    picks = []
    for _ in range(4):
        x = pick_ally_daowen_x(ally, "背负", ally)
        if x < 1:
            break
        picks.append(x)
        ally.current_mana -= 2 * x
    assert picks == [1, 1], f"4 点池子应摊成两次 X=1，实得 {picks}"
    assert ally.current_mana == 0, "余量规则不该把法力永久闲置"


def test_cannot_afford_returns_zero_instead_of_error():
    """连 X=1 都付不起时返回 0，调用方跳过而非硬报错。"""
    ally = _ally("穷光蛋", hp=54, ac=2, ap=0)       # 法力池 0
    _give(ally, "背负")
    assert ally.current_mana == 0
    assert pick_ally_daowen_x(ally, "背负", ally) == 0


def test_mutation_stays_clear_of_collapse_line():
    """异变累加到迷失阈值即命零且跨战斗不回退，必须留足安全边距。"""
    ally = _ally("乞丐", hp=50, ac=2, ap=3, mut=3)
    x = pick_ally_daowen_x(ally, "全力", ally)      # 异变 +5X（2026-10-03 起用【全力】替代已删的【狂暴】）
    after = ally.mutation_count + 5 * x
    assert after < Entity.MUTATION_COLLAPSE_THRESHOLD
    assert after <= Entity.MUTATION_COLLAPSE_THRESHOLD * 0.8 + 5 * 1


def test_mutation_choice_shrinks_as_mutation_accumulates():
    """异变是累加且不可逆的预算：层数越高，后续可选 X 越小。"""
    first = pick_ally_daowen_x(_ally("乞丐", hp=50, ac=2, ap=3, mut=3), "全力", None)
    ally = _ally("乞丐", hp=50, ac=2, ap=3, mut=3)
    ally.mutation_count += 5 * first
    second = pick_ally_daowen_x(ally, "全力", None)
    assert second < first, f"异变累积后应收敛：{first} → {second}"


def test_cooldown_daowen_never_locks_for_many_battles():
    """冷却类代价跨战斗生效，X 越大锁的场次越多 —— 一律取 1。"""
    ally = _ally("追求者", hp=96, ac=8, ap=2)
    assert pick_ally_daowen_x(ally, "固执", ally) == 1


def test_duration_scaling_daowen_respects_battle_horizon():
    """duration 随 X 一起涨时，超出战斗视野的持续回合是白付的代价。"""
    ally = _ally("赴火者", hp=60, ac=3, ap=3)
    x = pick_ally_daowen_x(ally, "贯穿", ally)      # duration = X，受战斗视野上限约束
    assert 1 <= x <= 5, f"应压在战斗视野内，实得 {x}"


def test_hp_cost_never_kills_the_ally():
    """流血类不得把自己流死。"""
    ally = _ally("赴火者", hp=4, ac=3, ap=3)
    x = pick_ally_daowen_x(ally, "连击", ally)
    assert x < ally.current_hp, f"X={x} 会把 {ally.current_hp} 血流干"


# ---------------------------------------------------------------- 迁移期兼容

def test_fixed_x_panel_still_uses_the_written_value():
    """面板仍写死 X 时沿用固定值，双向兼容（与怪物侧同口径）。"""
    e, player, monster = _engine_and_ally(_ally("老朋友", hp=54, ac=2, ap=4))
    ally = e.state.friends[0]
    _give(ally, "背负", x=3)
    assert e._ally_daowen_x(ally, "背负", monster) == 3


def test_x_free_panel_uses_the_chooser_not_hardcoded_one():
    """x_free 时走决策器，不再是硬编码的 x=1。"""
    e, player, monster = _engine_and_ally(_ally("赴火者", hp=60, ac=3, ap=3))
    ally = e.state.friends[0]
    _give(ally, "连击")
    chosen = e._ally_daowen_x(ally, "连击", monster)
    assert chosen == pick_ally_daowen_x(ally, "连击", monster)
    assert chosen > 1, "赴火者血 60、连击流血 1X，X=1 过于保守"


# ---------------------------------------------------------------- 端到端

def test_autonomous_ally_cast_uses_chosen_x_not_one():
    """自主出手路径（resolve_ally_phases）不再硬编码 x=1。"""
    e, player, monster = _engine_and_ally(_ally("赴火者", hp=60, ac=3, ap=3))
    ally = e.state.friends[0]
    _give(ally, "连击")
    expected = pick_ally_daowen_x(ally, "连击", monster)
    r = e.execute_action("resolve_ally_phases", {})
    assert r.get("success") is True, r
    allies = r["result"]["allies"]
    cast = next((a for a in allies if a.get("actions")), None)
    assert cast is not None, "微光者应已出手"
    action = cast["actions"][0]
    assert action["kind"] == "daowen"
    assert action["x"] == expected, f"应发动 X={expected}，实为 {action['x']}"


def test_command_ally_uses_chosen_x():
    """手动指令路径（command_ally）同样走决策器。"""
    e, player, monster = _engine_and_ally(_ally("赴火者", hp=60, ac=3, ap=3))
    ally = e.state.friends[0]
    _give(ally, "连击")
    r = e.execute_action("command_ally", {"ally_ref": "friend:0",
                                           "instruction": "发动 连击 打 测试怪"})
    assert r.get("success") is True, r
    assert r["result"]["daowen"] == "连击"


def test_command_ally_refuses_when_unaffordable():
    """付不起时给出明确错误，而不是静默按 X=1 硬上。"""
    e, player, monster = _engine_and_ally(_ally("穷光蛋", hp=54, ac=2, ap=0))
    ally = e.state.friends[0]
    _give(ally, "背负")
    r = e.execute_action("command_ally", {"ally_ref": "friend:0",
                                           "instruction": "发动 背负 打 测试怪"})
    assert r.get("success") is False
    assert "不宜发动" in r.get("error", "")
