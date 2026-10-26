"""
pytest 风格测试 - 里程碑5：撤退机制

原文："撤退（任意[朋友]/[员工]即将受到足以使当前[命零]的攻击时触发）：
该角色在本次致死攻击结算前自动撤退；撤退后本次攻击失去目标，该[朋友]/[员工]保留当前生命，
无法再次加入本场战斗。"

设计取舍(已在消息中说明，非阻塞性判断，供复核)：
1. "攻击"按广义理解 = 任意外部/敌对伤害来源(普通攻击 + 敌对道纹伤害)，不含自身承担的【代价】。
2. 判定用"扣除格挡后的实际伤害"而不是原始伤害数字——格挡足够抵消则不触发撤退也不死亡。
3. 玩家(轮回者)与怪物不适用本机制，只有[朋友]/[员工]。
4. "无法再次加入本场战斗"实现为：has_retreated=True 时从 get_all_player_side() 排除、
   无法被 deploy_employee/use_daowen(actor=) 选中；[战终]后重置，可参加下一场。
5. "负岳碑"法器的覆盖选项(流血20取消撤退)不在本里程碑实现范围(该法器本身未实现)。

运行方式：
    python -m pytest tests/test_retreat.py -v
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance
from engine.combat import CombatEngine
from engine.dice import DiceEngine


def _setup(db_suffix: str):
    engine = GameEngine(db_path=f"data/test_retreat_{db_suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.relics = []
    # 2026-10-13：遗物池删2件后随机发放漂移，本文件断言与开局遗物无关，清空钉死环境。
    engine.state.energy = 0
    engine.execute_action("battle_start", {"relic_choices": {}})
    return engine


# ========================================================================
# 正常路径
# ========================================================================

def test_lethal_attack_on_friend_triggers_retreat_instead_of_death():
    """正常路径：致死攻击命中[朋友]时触发撤退，保留当前生命，不死亡"""
    engine = _setup("lethal_friend")
    friend = Entity(name="岩行者", entity_type="朋友", blood_limit=20, current_hp=10,
                     attack_count=2, attack_power=4)
    engine.state.friends.append(friend)
    monster = Entity(name="测试怪", entity_type="怪物", blood_limit=100, current_hp=100,
                      attack_count=1, attack_power=50)
    engine.state.enemies.clear()
    engine.state.monster_reinforcements.clear()
    engine.state.enemies.append(monster)
    engine.execute_action("round_start", {})

    r = engine.combat.resolve_attack(monster, friend, dodge=False)
    assert r.get("retreated") is True or r["damage_dealt"] == 0
    assert friend.current_hp == 10, "应保留当前生命，不掉血"
    assert friend.is_alive is True, "不应死亡"
    assert friend.has_retreated is True


def test_retreated_ally_removed_from_active_combat_but_stays_on_roster():
    """正常路径：撤退后不再计入get_all_player_side(不能再被选为目标/行动)，但仍在friends/employees名单里"""
    engine = _setup("removed_from_combat")
    friend = Entity(name="哨兵", entity_type="朋友", blood_limit=20, current_hp=5,
                     attack_count=2, attack_power=4)
    engine.state.friends.append(friend)
    monster = Entity(name="测试怪", entity_type="怪物", blood_limit=100, current_hp=100,
                      attack_count=1, attack_power=99)
    engine.state.enemies.clear()
    engine.state.monster_reinforcements.clear()
    engine.state.enemies.append(monster)
    engine.execute_action("round_start", {})

    assert friend in engine.state.get_all_player_side()
    engine.combat.resolve_attack(monster, friend, dodge=False)
    assert friend not in engine.state.get_all_player_side(), "撤退后不应再计入战场"
    assert friend in engine.state.friends, "撤退不等于移出名单，仍应留存"


def test_employee_debt_bound_immediate_participation_can_also_retreat():
    """正常路径：还债转化员工(is_debt_bound=True，已豁免出战支援)一样受撤退保护(它仍是[员工])"""
    state_engine = _setup("debt_bound_retreat")
    monster_enemy = Entity(name="毒枭", entity_type="怪物", blood_limit=100, current_hp=100,
                            attack_count=2, attack_power=10, shards=-15)
    state_engine.state.enemies.clear()
    state_engine.state.monster_reinforcements.clear()
    state_engine.state.enemies.append(monster_enemy)
    debt_result = state_engine.combat._debt_bind_monster(monster_enemy)
    emp = next(e for e in state_engine.state.employees if e.name == "毒枭")
    emp.current_hp = 5
    attacker = Entity(name="猛攻怪", entity_type="怪物", blood_limit=50, current_hp=50,
                       attack_count=1, attack_power=99)
    state_engine.combat.resolve_attack(attacker, emp, dodge=False)
    assert emp.current_hp == 5
    assert emp.has_retreated is True


def test_battle_end_resets_retreat_flag_for_next_battle():
    """正常路径：[战终]后has_retreated重置，下一场可以重新参战"""
    engine = _setup("reset_next_battle")
    friend = Entity(name="赴火者", entity_type="朋友", blood_limit=20, current_hp=5,
                     attack_count=3, attack_power=3)
    engine.state.friends.append(friend)
    monster = Entity(name="测试怪", entity_type="怪物", blood_limit=100, current_hp=100,
                      attack_count=1, attack_power=99)
    engine.state.enemies.clear()
    engine.state.monster_reinforcements.clear()
    engine.state.enemies.append(monster)
    engine.execute_action("round_start", {})
    engine.combat.resolve_attack(monster, friend, dodge=False)
    assert friend.has_retreated is True

    monster.current_hp = 0
    monster.is_alive = False
    r = engine.execute_action("battle_end", {})
    assert r["success"] is True
    assert friend.has_retreated is False, "战终应重置撤退状态"

    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    engine.state.monster_reinforcements.clear()
    engine.execute_action("round_start", {})
    assert friend in engine.state.get_all_player_side(), "下一场应可正常参战"


def test_hostile_daowen_damage_also_triggers_retreat():
    """正常路径：敌对道纹造成的致命伤害(而不仅是普通攻击)同样应触发撤退"""
    state = None
    from engine.models import GameState
    state = GameState()
    state.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60)
    friend = Entity(name="岩行者", entity_type="朋友", blood_limit=20, current_hp=8)
    state.friends.append(friend)
    monster = Entity(name="毒枭", entity_type="怪物", blood_limit=100, current_hp=100)
    state.enemies.append(monster)
    combat = CombatEngine(state, DiceEngine())
    calc = {"dao_wen": "杀伐", "target_damage": 999, "cost_type": "消耗", "cost": 5}
    result = combat.apply_daowen_effect("杀伐", calc, monster, friend)
    assert friend.current_hp == 8, "杀伐999点应触发撤退而非直接打死"
    assert friend.has_retreated is True


# ========================================================================
# 边界条件
# ========================================================================

def test_shield_fully_absorbing_lethal_hit_does_not_trigger_retreat():
    """边界：格挡足以完全抵消本可致死的伤害时，不触发撤退(因为根本没有生命受损风险)"""
    engine = _setup("shield_absorbs")
    friend = Entity(name="铁壁", entity_type="朋友", blood_limit=20, current_hp=10, shield=45)
    engine.state.friends.append(friend)
    monster = Entity(name="测试怪", entity_type="怪物", blood_limit=100, current_hp=100,
                      attack_count=1, attack_power=50)
    engine.state.enemies.clear()
    engine.state.monster_reinforcements.clear()
    engine.state.enemies.append(monster)
    engine.execute_action("round_start", {})

    r = engine.combat.resolve_attack(monster, friend, dodge=False)
    assert not r.get("retreated")
    assert friend.current_hp == 5, "45格挡吸收后剩5点伤害，应正常扣血而不是撤退"
    assert friend.has_retreated is False


def test_player_and_monster_never_trigger_retreat():
    """边界：撤退只对[朋友]/[员工]生效，轮回者与怪物本身承受致死伤害应正常判定死亡/命零"""
    engine = _setup("player_monster_no_retreat")
    player = engine.state.player
    player.current_hp = 5
    monster = Entity(name="测试怪", entity_type="怪物", blood_limit=100, current_hp=5,
                      attack_count=1, attack_power=99)
    engine.state.enemies.clear()
    engine.state.monster_reinforcements.clear()
    engine.state.enemies.append(monster)
    engine.execute_action("round_start", {})

    r1 = engine.combat.resolve_attack(monster, player, dodge=False)
    assert player.current_hp == 0 and not player.is_alive, "玩家应正常命零，不受撤退保护"

    other_monster = Entity(name="打手", entity_type="怪物", blood_limit=50, current_hp=50,
                            attack_count=1, attack_power=99)
    r2 = engine.combat.resolve_attack(other_monster, monster, dodge=False)
    assert monster.current_hp == 0 and not monster.is_alive, "怪物应正常命零，不受撤退保护"


def test_self_inflicted_cost_damage_does_not_trigger_retreat():
    """边界：自身承担的【代价】伤害(如流血)不触发撤退——代价必须真实生效，不能被撤退规避"""
    from engine.models import GameState
    state = GameState()
    friend = Entity(name="牺牲者", entity_type="朋友", blood_limit=20, current_hp=5)
    state.friends.append(friend)
    combat = CombatEngine(state, DiceEngine())
    result = combat._apply_hostile_damage(friend, 20, "代价")
    assert friend.current_hp == 0 and not friend.is_alive, "代价伤害应真实生效直至死亡，不受撤退保护"
    assert friend.has_retreated is False


def test_already_retreated_ally_cannot_retreat_again_and_takes_real_damage():
    """边界：已撤退的[朋友]理论上不该再被选中；若仍被结算伤害(异常路径)，不应无限触发撤退保护"""
    from engine.models import GameState
    state = GameState()
    friend = Entity(name="二次受击", entity_type="朋友", blood_limit=20, current_hp=10, has_retreated=True)
    state.friends.append(friend)
    combat = CombatEngine(state, DiceEngine())
    result = combat._apply_hostile_damage(friend, 999, "普通")
    assert friend.is_alive is False, "已撤退状态下若仍被结算伤害，应正常处理为死亡，不重复触发撤退"


# ========================================================================
# 错误输入 / 非法配置
# ========================================================================

def test_retreated_employee_cannot_be_redeployed_this_battle():
    """错误输入：本场已撤退的员工，deploy_employee必须拒绝，不能重新参战"""
    engine = _setup("redeploy_rejected")
    emp = Entity(name="老张", entity_type="员工", blood_limit=204, current_hp=204,
                 attack_count=1, attack_power=2, is_deployed=False)
    engine.state.employees.append(emp)
    monster = Entity(name="测试怪", entity_type="怪物", blood_limit=100, current_hp=100,
                      attack_count=1, attack_power=99)
    engine.state.enemies.clear()
    engine.state.monster_reinforcements.clear()
    engine.state.enemies.append(monster)
    engine.execute_action("round_start", {})
    engine.execute_action("deploy_employee", {"name": "老张"})
    emp.current_hp = 3
    engine.combat.resolve_attack(monster, emp, dodge=False)
    assert emp.has_retreated is True

    r = engine.execute_action("deploy_employee", {"name": "老张"})
    assert r["success"] is False
    assert "撤退" in r["error"]


def test_retreated_ally_cannot_be_commanded_via_use_daowen():
    """错误输入：撤退后的盟友不能再被指令发动道纹"""
    engine = _setup("cmd_rejected")
    friend = Entity(name="被击退者", entity_type="朋友", blood_limit=20, current_hp=3, has_retreated=True)
    friend.dao_wen["杀伐"] = DaoWenInstance(DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    engine.state.friends.append(friend)
    monster = Entity(name="测试怪", entity_type="怪物", blood_limit=100, current_hp=100)
    engine.state.enemies.clear()
    engine.state.monster_reinforcements.clear()
    engine.state.enemies.append(monster)
    r = engine.execute_action("use_daowen", {"actor": "被击退者", "daowen_name": "杀伐", "x": 1, "target": "测试怪"})
    assert r["success"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
