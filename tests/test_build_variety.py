"""
pytest - AI 道纹多样性（数据驱动，不写死道纹名）

背景：早前 AI 把"杀伐/庇护/再生/冲击/切割"硬编码在 if 分支里，
无法测试切割系与副本专属道纹。2026-08-26 起战术表已删（防公式化），
AI 改为预演实时决策——本文件验证"持有即可能被发动"的多样性不变量。

覆盖：正常路径 / 边界条件 / 错误输入
"""
import os
import sys

import pytest

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.ai_tactics import TacticalAI
from engine.models import Entity


def _engine(starter="杀伐", learn=(), region="龙心谷", seed=1, tmp="/tmp/bv.db"):
    e = GameEngine(db_path=tmp, rng_seed=seed)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": region})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    planned = ([] if starter == "杀伐" else [starter]) + [dw for dw in learn if dw != "杀伐"]
    for dw in planned:
        e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": dw})
    e.state.energy = 0
    choices = {}
    relic = e.state.relics[0].name
    # 2026-10-13：【三相残韵盘】删除，战始显式决策预填不再需要。
    e.execute_action("battle_start", {"relic_choices": choices})
    e.execute_action("round_start", {})
    return e


# ---------- 正常路径 ----------

def test_boba_marks_targets_after_start():
    """正常路径：波及开局后可真实发动并标记目标（X=2 标两个目标；2026-10-09 起 X 下限 repealed，X=1 也合法）。"""
    e = _engine(starter="杀伐")
    # 只测现役多目标结算；专属道纹直接注入，不走其它副本的学习门禁。
    from engine.models import DaoWen, DaoWenInstance
    e.state.player.dao_wen["波及"] = DaoWenInstance(
        DaoWen("波及", "", "消耗", "2X", ""))
    assert set(e.state.player.dao_wen) >= {"杀伐", "波及"}
    # 2026-10-12 出怪下界 1→2：第1场可能已有 2 只怪，本条要的靶子是「enemy:0 + 陪标怪」
    # 两个，故先把出怪收敛到 1 只（并清掉增援队列，免得后续回合又进场一只挤掉索引）。
    e.state.enemies[:] = e.state.enemies[:1]
    e.state.monster_reinforcements = []
    enemy = e.state.enemies[0]
    second = Entity("陪标怪", "怪物", blood_limit=100, current_hp=100,
                    speed_limit=2, current_speed=2, attack_count=1, attack_power=1)
    e.state.enemies.append(second)
    e.state.player.current_mana = max(e.state.player.current_mana, 10)   # X=2 → 4 法力
    result = e.execute_action("use_daowen", {
        "daowen_name": "波及", "x": 2,
        "dodge_targets": [{"target_ref": "enemy:0", "dodge": False, "blood_shadow": False},
                          {"target_ref": "enemy:1", "dodge": False, "blood_shadow": False}],
        "trigger_spell_choices": {},
    })
    assert result["success"], result
    assert enemy.has_status("波及") and second.has_status("波及")


# 专属道纹 → 其所属副本（学习受门禁限制，须在对应副本内）
# 2026-10-12：失忆 已回退为 点金；2026-10-13：【逼债】删除——【典当】不进本表：
# 本用例只注入被测一条道纹，而典当代价失忆2X（单持付不起），「持有即发动」不变量
# 对它不成立；其可发动性见 tests/test_escape_and_diandang.py。
_REGION_OF = {"超频": "扭曲都市", "勾魂": "乱葬岗",
              "坏死": "扭曲都市",
              "点金": "罪孽都市"}


@pytest.mark.parametrize("dw", ["超频", "勾魂", "坏死", "点金"])
def test_ai_uses_region_specific_daowen(dw, monkeypatch):
    """
    正常路径：各副本专属道纹只要持有就应被实际发动。

    注：专属道纹不能直接学习（须先经残韵从本副本怪物身上转化获得），
    故此处在对应副本内直接注入到玩家身上，只验证"持有后AI会用"。
    被测对象是「道纹使用」：用 LJ_AI_BASIC_ATTACK=0 关掉普攻竞争者以隔离变量
    （DM裁定 2026-09-10：普攻默认在池，会按分数正常抢出手）。
    """
    monkeypatch.setenv("LJ_AI_BASIC_ATTACK", "0")
    from engine.models import DaoWen, DaoWenInstance
    e = _engine(starter="杀伐", learn=[], region=_REGION_OF[dw])
    # 只保留被测道纹，避免 AI 选了同角色的其他道纹而误判。
    # 2026-09-13：连【杀伐】也一并清掉——改为 X² 伤害后它在任何局面都能靠高 X
    # 档压过 debuff 的分数，并把法力一次抽干，被测牌再没有出手窗口。本例要验的
    # 不变量是「持有专属道纹就会发动」，不是「debuff 能不能赢过主力输出」。
    e.state.player.dao_wen = {}
    e.state.player.dao_wen[dw] = DaoWenInstance(
        DaoWen(name=dw, formula="", cost_type="消耗",
               cost_formula="X", effect_formula=""))
    # 法力需满足：单次出手预算(总法力÷出手次数) 足以让该 debuff 达到 X≥2，
    # 同时怪物血量要高到不会被一击收割（否则 AI 直接击杀，debuff 轮不到）。
    _ai0 = TacticalAI(e)
    _probe = _ai0._probe(dw)
    _cost = (_probe or {}).get("cost_per_x", 1)
    e.state.player.current_mana = max(12, _cost * 2 * 3)
    for _m in e.state.enemies:
        _m.blood_limit = max(_m.blood_limit, 300)
        _m.current_hp = _m.blood_limit
        if "全力" not in _m.dao_wen:
            _m.dao_wen["全力"] = DaoWenInstance(
                DaoWen(name="全力", formula="", cost_type="异变",
                       cost_formula="5X", effect_formula=""), x_value=1)
    ai = TacticalAI(e)
    # 6 回合而非 3：杀伐自 2026-09-13 改为 X² 后高 X 档分数陡增，会连吃前几手；
    # 不变量仍是「持有即会发动」，给它把法力打空的时间，别把窗口卡在 3 手。
    for _ in range(6):
        ai.new_round()
        ai.take_turn()
    assert ai.used.get(dw, 0) > 0, f"{dw} 从未被使用，实际使用：{ai.used}"


def test_every_role_query_is_reachable():
    """正常路径：每个旧角色名都有对应的 owned() 查询路径（兼容壳）"""
    e = _engine()
    ai = TacticalAI(e)
    for r in ("nuke", "aoe", "finisher", "mark", "shield", "heal",
              "control", "debuff", "buff", "remove", "ramp", "reroll"):
        ai.owned(r)  # 不应抛异常


# ---------- 边界条件 ----------

def test_damage_ranking_comes_from_probe_facts():
    """边界：输出牌按预演事实（每X伤害/法力单价）排序，不查表。"""
    e = _engine(learn=["杀伐"])
    ai = TacticalAI(e)
    probe = ai._probe("杀伐")
    assert probe["kind"] == "damage"
    assert probe["dmg"] == 1 and probe["cost_per_x"] == 1   # 引擎事实：X=1→X²=1伤害/1法力（2026-09-13）


def test_owned_nuke_only_contains_damage_kind():
    """正常：owned('nuke') 只含预演/文本归纳为输出的道纹。"""
    e = _engine(learn=["杀伐"])
    ai = TacticalAI(e)
    assert "杀伐" in ai.owned("nuke")


def test_control_not_repeated_on_same_target_in_one_round():
    """边界：同一回合不得对同一目标重复施加控制（控制不叠加，重复即浪费）"""
    e = _engine(starter="切割", learn=["束缚"])
    e.state.player.current_mana = 99
    ai = TacticalAI(e)
    ai.new_round()
    first = ai.try_control()
    second = ai.try_control()
    if first is not None:
        assert second is None, "同回合对同一目标重复上控"


def test_new_round_resets_control_bookkeeping():
    """边界：new_round 后应可再次控制同一目标"""
    e = _engine()
    ai = TacticalAI(e)
    ai._controlled_this_round.add("某怪")
    ai.new_round()
    assert ai._controlled_this_round == set()


# ---------- 错误输入 ----------

def test_ai_candidate_pool_matches_engine_registry():
    """错误输入检出：AI 可主动发动的道纹池必须由引擎注册表派生（非人工表）。

    防公式化清理后构筑候选池=注册表非怪物原始道纹；注册表外不得有任何
    AI 侧道纹名单（否则又是第二张表）。
    """
    from engine.daowen import DaoWenEngine
    DaoWenEngine.register_all()
    import engine.ai_tactics as mod
    for attr in dir(mod):
        assert "TACTICAL_ROLES" not in attr and "HIGH_VALUE" not in attr


def test_ai_skips_daowen_it_does_not_own():
    """错误输入：未持有的道纹不得被发动"""
    e = _engine(starter="杀伐")
    ai = TacticalAI(e, verbose=True)
    for _ in range(3):
        ai.new_round()
        ai.take_turn()
    assert "封印" not in ai.used
    assert "退化" not in ai.used


# ---------- 贯穿（无视格挡）回归 ----------

def test_pierce_bypasses_shield():
    """
    正常路径：贯穿/无视格挡 的伤害不得被格挡吸收。
    此前 take_damage 只豁免"代价"，导致【贯穿】完全失效，
    格挡因此成为无解的万能防御，是公式化的成因之一。
    """
    from engine.models import Entity
    t = Entity(name="靶", entity_type="怪物", blood_limit=100, current_hp=100)
    t.shield = 50
    d = t.take_damage(20, "无视格挡")
    assert d["shield_absorbed"] == 0, "无视格挡的伤害不该被格挡吸收"
    assert d["actual_damage"] == 20
    assert t.shield == 50, "格挡不应被消耗"


def test_normal_damage_still_blocked():
    """边界：普通伤害必须仍然被格挡正常吸收（不能改坏原有规则）"""
    from engine.models import Entity
    t = Entity(name="靶", entity_type="怪物", blood_limit=100, current_hp=100)
    t.shield = 50
    d = t.take_damage(20, "普通")
    assert d["shield_absorbed"] == 20
    assert d["actual_damage"] == 0
    assert t.shield == 30


def test_cost_damage_still_ignores_shield():
    """边界：代价类伤害绝对不被格挡吸收（README 明确规定）"""
    from engine.models import Entity
    t = Entity(name="靶", entity_type="怪物", blood_limit=100, current_hp=100)
    t.shield = 50
    d = t.take_damage(15, "代价")
    assert d["shield_absorbed"] == 0
    assert d["actual_damage"] == 15
    assert t.shield == 50


def test_pierce_status_drives_attack_resolution():
    """正常路径：持有【贯穿】状态的攻击者，其攻击应无视目标格挡"""
    from engine.models import StatusEffect
    e = _engine(starter="杀伐", learn=[], region="龙心谷", tmp="/tmp/pierce.db")
    p, m = e.state.player, e.state.enemies[0]
    p.shield = 60
    m.add_status(StatusEffect(name="贯穿", value=1, remaining_rounds=-1, source="test"))
    r = e.combat.resolve_attack(m, p, dodge=False)
    assert r["hp_lost"] > 0, "贯穿攻击应造成实际生命损失"
    assert r["shield_absorbed"] == 0


# ---------- 进化·原初X 借用轮回者道纹（裁定）----------

def _plight_engine(player_daowen=("杀伐", "庇护", "透支")):
    from engine.api import GameEngine
    from engine.models import Entity, DaoWen, DaoWenInstance
    e = GameEngine(db_path="/tmp/evo.db", rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    for n in player_daowen:
        e.state.player.dao_wen[n] = DaoWenInstance(
            dao_wen=DaoWen(name=n, formula="", cost_type="消耗",
                           cost_formula="X", effect_formula=""), x_value=1)
    e.combat.reset_monster_activation()
    e.state.phase = "in_combat"
    m = Entity(name="困境怪", entity_type="怪物", blood_limit=120, current_hp=30,
               attack_count=2, attack_power=1)
    e.state.enemies.append(m)
    return e, m


def test_evolution_borrows_from_player_daowen():
    """正常路径：进化只能借用轮回者当前持有的道纹"""
    e, m = _plight_engine(("杀伐", "庇护", "透支"))
    r = e.execute_action("declare_evolution", {"monster": "困境怪", "daowen": "透支", "x": 1})
    assert r["success"], r.get("error")
    assert "透支" in m.dao_wen, "应借用到轮回者的【透支】"


def test_evolution_rejects_daowen_player_lacks():
    """错误输入：轮回者没有的道纹不可被借用"""
    e, m = _plight_engine(("杀伐",))
    r = e.execute_action("declare_evolution", {"monster": "困境怪", "daowen": "自愈", "x": 1})
    assert not r["success"]
    assert "不在轮回者当前持有的道纹中" in r["error"]


def test_evolution_pool_tracks_player_build():
    """
    边界：借用池随玩家构筑变化 —— 这正是该裁定的设计目的。
    玩家越依赖某条公式化路线，越可能被怪物复制反制。
    """
    e1, _ = _plight_engine(("杀伐", "庇护", "透支"))
    opts1 = e1.combat.get_plight_evolution_options()[0]["borrowable_daowen"]
    e2, _ = _plight_engine(("切割", "贯穿"))
    opts2 = e2.combat.get_plight_evolution_options()[0]["borrowable_daowen"]
    assert set(opts1) == {"杀伐", "庇护", "透支"}
    assert set(opts2) == {"杀伐", "切割", "贯穿"}
    assert opts1 != opts2, "借用池必须随玩家构筑变化"


def test_evolution_still_costs_mutation():
    """边界：改动不得绕过原有代价（异变5X）与每场一次限制"""
    e, m = _plight_engine(("庇护",))
    r = e.execute_action("declare_evolution", {"monster": "困境怪", "daowen": "庇护", "x": 2})
    assert r["success"]
    assert m.mutation_count == 10, "异变代价应为5X=10"
    r2 = e.execute_action("declare_evolution", {"monster": "困境怪", "daowen": "庇护", "x": 1})
    assert not r2["success"], "每场战斗限一次"
