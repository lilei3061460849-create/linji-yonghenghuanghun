"""朋友/员工指挥系统契约测试。

需求：轮回者可通过语言命令朋友/员工行为（command_ally）；
无命令时朋友/员工自主出手（resolve_ally_phases，规则正文：微光者会根据情况对敌方出手）。

覆盖：正常路径（语言命令攻击/道纹、自主出手）/ 边界（目标缺省、出手预算、无敌人停手）/
错误输入（非法指令、未知目标、未持有道纹、已用完出手）。
"""
import os
import sys

from tests.setup_support import begin_battle, begin_round, finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance


def _engine(suffix: str) -> GameEngine:
    e = GameEngine(db_path=f"/tmp/test_ally_cmd_{suffix}.db", rng_seed=1)
    e.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.energy = 0
    return e


def _start_battle(e, ally_name="岩行者", ally_atk=2, ally_ap=4, enemy_hp=100):
    started = begin_battle(e)
    assert started["success"], started
    # 清默认怪，放一个受控敌人
    e.state.enemies.clear()
    e.state.enemies.append(Entity("靶怪", "怪物", blood_limit=enemy_hp, current_hp=enemy_hp,
                                  attack_count=0, attack_power=0))
    e.state.friends.clear()
    ally = Entity(ally_name, "朋友", blood_limit=54, current_hp=54,
                  attack_count=ally_atk, attack_power=ally_ap)
    e.state.friends.append(ally)
    started_round = begin_round(e)
    assert started_round["success"], started_round
    return ally


def _give_daowen(ally, name):
    ally.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="", cost_formula="", effect_formula=""), x_value=1)


# ---------- 正常路径 ----------

def test_normal_command_ally_attack():
    """正常路径：语言命令「攻击 靶怪」让朋友攻击目标。"""
    e = _engine("atk")
    ally = _start_battle(e)
    r = e.execute_action("command_ally", {"ally_ref": "friend:0", "instruction": "攻击 靶怪"})
    assert r["success"], r
    assert r["result"]["attacked"] == "靶怪"
    # 朋友 2×4，出手⌈2/3⌉=1，攻击一轮=2次命中×4伤=8
    assert e.state.enemies[0].current_hp == 100 - 8, f"应造成8伤，实{e.state.enemies[0].current_hp}"
    assert ally.actions_used_this_round == 1


def test_normal_command_ally_daowen():
    """正常路径：语言命令「发动 杀伐 打 靶怪」让朋友发动道纹。"""
    e = _engine("dw")
    ally = _start_battle(e)
    _give_daowen(ally, "杀伐")
    r = e.execute_action("command_ally", {"ally_ref": "friend:0", "instruction": "发动 杀伐 打 靶怪"})
    assert r["success"], r
    assert r["result"]["daowen"] == "杀伐"
    assert ally.actions_used_this_round == 1


def test_normal_resolve_ally_phases_auto_acts():
    """正常路径：无命令时 resolve_ally_phases 让朋友自主出手（攻击）。"""
    e = _engine("auto")
    ally = _start_battle(e)
    r = e.execute_action("resolve_ally_phases", {})
    assert r["success"], r
    assert r["result"]["acted_count"] >= 1, "朋友应自主出手"
    assert e.state.enemies[0].current_hp < 100, "自主攻击应造成伤害"
    assert ally.actions_used_this_round >= 1


def test_normal_auto_prefers_daowen_when_owned():
    """正常路径：朋友持进攻道纹时自主出手优先发动道纹。"""
    e = _engine("autodw")
    ally = _start_battle(e)
    _give_daowen(ally, "杀伐")  # 现行进攻道纹；加害已转为消耗品
    ally.mana_limit = ally.current_mana = 30
    r = e.execute_action("resolve_ally_phases", {})
    assert r["success"]
    kinds = [a["kind"] for entry in r["result"]["allies"] for a in entry["actions"]]
    assert "daowen" in kinds, f"持进攻道纹时应优先道纹，实{kinds}"


def test_boundary_auto_skips_self_harm_daowen():
    """边界：持【背负】（替敌方承担伤害）的朋友自主出手跳过道纹改用攻击。

    背负对敌使用=施法者替敌方挡刀，自主出手不得帮敌人。"""
    e = _engine("beifu")
    ally = _start_battle(e)
    _give_daowen(ally, "背负")
    r = e.execute_action("resolve_ally_phases", {})
    assert r["success"]
    kinds = [a["kind"] for entry in r["result"]["allies"] for a in entry["actions"]]
    # 2026-09-16：出手次数全体固定 2（旧口径为攻次/3=1），故这里会得到 2 次行动。
    # 本测试的意图是"跳过道纹"而非"只出手一次"，故断言全部行动都是攻击、不含道纹。
    assert kinds and set(kinds) == {"attack"}, f"持背负应跳过道纹、全部改用攻击，实{kinds}"


# ---------- 边界条件 ----------

def test_boundary_command_without_target_defaults_to_lowest_hp_enemy():
    """边界：指令缺省目标名时选当前生命最少的存活敌人。"""
    e = _engine("notgt")
    _start_battle(e)
    e.state.enemies.append(Entity("弱怪", "怪物", blood_limit=50, current_hp=10,
                                  attack_count=0, attack_power=0))
    r = e.execute_action("command_ally", {"ally_ref": "friend:0", "instruction": "攻击"})
    assert r["success"], r
    assert r["result"]["attacked"] == "弱怪", "应默认攻击血最少的敌人"


def test_boundary_auto_stops_when_no_enemies():
    """边界：无存活敌人时自主出手不行动、不报错。"""
    e = _engine("noenemy")
    _start_battle(e)
    e.state.enemies.clear()
    r = e.execute_action("resolve_ally_phases", {})
    assert r["success"]
    assert r["result"]["acted_count"] == 0


def test_boundary_command_respects_action_budget():
    """边界：朋友出手用完后再命令被拒。"""
    e = _engine("budget")
    ally = _start_battle(e)
    # 单轮行动窗口按战斗层预算校验，不拿角色总预算代替窗口预算。
    budget = e.combat.single_round_action_count(ally)
    for i in range(budget):
        r = e.execute_action("command_ally", {"ally_ref": "friend:0", "instruction": "攻击 靶怪"})
        assert r["success"], f"第{i+1}次指令应成功（窗口预算{budget}次）"
    r2 = e.execute_action("command_ally", {"ally_ref": "friend:0", "instruction": "攻击 靶怪"})
    assert not r2["success"]
    assert "出手已用完" in r2["error"]


# ---------- 错误输入 / 非法配置 ----------

def test_error_invalid_instruction_rejected():
    """错误输入：无法解析的指令被拒绝并给合法示例。"""
    e = _engine("badins")
    _start_battle(e)
    r = e.execute_action("command_ally", {"ally_ref": "friend:0", "instruction": "去死吧你"})
    assert not r["success"]
    assert "合法格式" in r["error"]


def test_error_unknown_target_rejected():
    """错误输入：指令目标不存在被拒绝。"""
    e = _engine("badtgt")
    _start_battle(e)
    r = e.execute_action("command_ally", {"ally_ref": "friend:0", "instruction": "攻击 不存在"})
    assert not r["success"]
    assert "找不到目标" in r["error"]


def test_error_ally_without_owned_daowen_rejected():
    """错误输入：命令朋友发动未持有道纹被拒绝。"""
    e = _engine("badw")
    _start_battle(e)
    r = e.execute_action("command_ally", {"ally_ref": "friend:0", "instruction": "发动 杀伐 打 靶怪"})
    assert not r["success"]
    assert "未持有道纹" in r["error"]


def test_error_command_player_or_unknown_ally_rejected():
    """错误输入：ally_ref指向轮回者或不存在的朋友被拒绝。"""
    e = _engine("badref")
    _start_battle(e)
    r = e.execute_action("command_ally", {"ally_ref": "player:0", "instruction": "攻击 靶怪"})
    assert not r["success"]
    r2 = e.execute_action("command_ally", {"ally_ref": "friend:9", "instruction": "攻击 靶怪"})
    assert not r2["success"]
