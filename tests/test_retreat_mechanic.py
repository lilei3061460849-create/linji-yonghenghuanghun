"""撤退机制契约测试（规则正文：任意[朋友]/[员工]即将受到足以命零的伤害时自动撤退）。

规则：
- 触发：扣除格挡后的实际伤害 ≥ 当前生命 → 本次伤害清零、目标保留当前生命、has_retreated退出本场
- 格挡足够抵消则不触发撤退
- 撤退后目标无法再次加入本场战斗（攻击目标列表排除 has_retreated）
- 战终后 has_retreated 重置，可参加下一场
- 自身【代价】不触发撤退（damage_type=代价）
"""
import os
import sys

from tests.setup_support import begin_battle, begin_round, finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.combat import CombatEngine, DamageType
from engine.models import Entity, GameState
from engine.dice import DiceEngine


def _state_with_ally(ally_hp=10, ally_shield=0):
    st = GameState()
    st.player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    ally = Entity("岩行者", "朋友", blood_limit=20, current_hp=ally_hp,
                  attack_count=2, attack_power=4)
    ally.gain_shield(ally_shield)
    st.friends.append(ally)
    st.enemies.append(Entity("怪", "怪物", blood_limit=100, current_hp=100,
                             attack_count=1, attack_power=5))
    return st, ally


def _combat(st):
    c = CombatEngine(st, DiceEngine(seed=1))
    c.reset_monster_activation()
    return c


# ---------- 正常路径 ----------

def test_normal_ally_retreats_on_lethal_hit():
    """正常路径：朋友即将受到足以命零的伤害时自动撤退，伤害清零、保留生命。"""
    st, ally = _state_with_ally(ally_hp=10)
    c = _combat(st)
    r = c._apply_hostile_damage(ally, 15, source=st.enemies[0])
    assert r["retreated"] is True, "应触发撤退"
    assert r["actual_damage"] == 0, "撤退时本次伤害清零"
    assert ally.current_hp == 10, f"保留当前生命，实{ally.current_hp}"
    assert ally.has_retreated is True, "应标记已撤退"
    assert ally.is_alive is True


def test_normal_retreated_ally_excluded_from_attack_targets():
    """正常路径：撤退后该朋友不再出现在怪物攻击目标中（无法再次加入本场战斗）。"""
    st, ally = _state_with_ally(ally_hp=10)
    ally.has_retreated = True
    c = _combat(st)
    options = c.prepare_monster_phase()["actors"][0]["attack_target_options"]
    refs = {o["ref"] for o in options}
    assert "friend:0" not in refs, f"撤退朋友不应在攻击目标中，实{refs}"


# ---------- 边界条件 ----------

def test_boundary_shield_absorbs_no_retreat():
    """边界：格挡足够抵消伤害时不触发撤退（也不死亡）。"""
    st, ally = _state_with_ally(ally_hp=10, ally_shield=15)
    c = _combat(st)
    r = c._apply_hostile_damage(ally, 12, source=st.enemies[0])
    assert r.get("retreated") in (None, False), "格挡足够不应撤退"
    assert ally.has_retreated is False
    assert ally.is_alive is True
    assert ally.current_hp == 10, "格挡吸收，生命不变"


def test_boundary_retreat_resets_next_battle():
    """边界：战终后 has_retreated 重置，朋友可参加下一场。"""
    e = GameEngine(db_path="/tmp/test_retreat_reset.db", rng_seed=1)
    e.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    ally = Entity("岩行者", "朋友", blood_limit=20, current_hp=20,
                  attack_count=2, attack_power=4)
    ally.has_retreated = True  # 模拟本场已撤退
    e.state.friends.append(ally)
    e.state.enemies.append(Entity("怪", "怪物", blood_limit=30, current_hp=30,
                                  attack_count=1, attack_power=1))
    started = begin_battle(e)
    assert started["success"], started
    started_round = begin_round(e)
    assert started_round["success"], started_round
    for m in list(e.state.enemies):
        m.is_alive = False
    # 本场只打这一波：出怪配方排下的增援整批清掉，战终才允许结算。
    e.state.monster_reinforcements.clear()
    ended = e.execute_action("battle_end", {})
    assert ended["success"], ended
    assert ally.has_retreated is False, "战终后撤退标记应重置"


def test_boundary_damage_below_hp_no_retreat():
    """边界：伤害不足以致命时不触发撤退。"""
    st, ally = _state_with_ally(ally_hp=10)
    c = _combat(st)
    r = c._apply_hostile_damage(ally, 5, source=st.enemies[0])
    assert r.get("retreated") in (None, False)
    assert ally.has_retreated is False
    assert ally.current_hp == 5, "正常受到5伤"


# ---------- 错误输入 / 非法配置 ----------

def test_error_cost_damage_does_not_trigger_retreat():
    """错误输入：自身【代价】造成的生命损失不触发撤退（代价不可被撤退豁免）。"""
    st, ally = _state_with_ally(ally_hp=10)
    c = _combat(st)
    r = c._apply_hostile_damage(ally, 15, damage_type=DamageType.COST.value,
                                source=st.enemies[0])
    assert r.get("retreated") in (None, False), "代价不应触发撤退"
    assert ally.has_retreated is False
    assert ally.current_hp == 0, "代价正常扣除，命零"


def test_error_player_does_not_retreat():
    """错误输入：轮回者自身不触发撤退（仅朋友/员工）。"""
    st = GameState()
    st.player = Entity("P", "轮回者", blood_limit=60, current_hp=10)
    st.enemies.append(Entity("怪", "怪物", blood_limit=100, current_hp=100,
                             attack_count=1, attack_power=5))
    c = _combat(st)
    r = c._apply_hostile_damage(st.player, 15, source=st.enemies[0])
    assert r.get("retreated") in (None, False), "轮回者不撤退"
    assert st.player.is_alive is False, "轮回者直接命零"


def test_battle_report_renders_retreat_line():
    """正常路径：战报渲染撤退为「自动【撤退】」行，含保留生命。"""
    from engine import battle_report as BR
    lines = BR.format_monster_hits(3, [{
        "attacker": "碎岩鸮", "target": "岩行者", "hit_index": 2, "hit_total": 2,
        "new_action": False, "dodge_attempted": False, "dodge_success": False,
        "damage_dealt": 0, "hp_lost": 0, "target_died": False, "target_hp_after": 5,
        "retreated": True,
    }])
    assert any("撤退" in l and "保留当前生命5" in l for l in lines), lines


def test_combat_detail_passes_retreat_flag():
    """正常路径：怪物攻击致朋友撤退时，detail 透传 retreated 标记。"""
    from engine.combat import CombatEngine, DamageType
    st, ally = _state_with_ally(ally_hp=5)
    c = _combat(st)
    r = c.resolve_attack(st.enemies[0], ally, hit_index=1, dodge=False, blood_shadow=False,
                         spell_choices={"before": {}, "after": {}})
    assert r.get("retreated") is True, f"应透传撤退标记，实{r}"
    assert r["damage_dealt"] == 0
    assert ally.has_retreated is True
