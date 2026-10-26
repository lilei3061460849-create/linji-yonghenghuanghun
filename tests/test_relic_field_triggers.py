"""遗物触发听字段、不听途径。

避风铃归零、回锋刀失速、洗劫造伤、焦黑发丝命零、寒冰法力施加法力
必须挂在统一入口；至少两条不同来源各测一次。
"""
from __future__ import annotations

import os
import sys

from tests.setup_support import (
    begin_battle, begin_round, finish_initial_daowen, strip_monster_relics,
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity, Relic, Spell, StatusEffect


def _engine(tmp_path, suffix="field"):
    e = GameEngine(db_path=str(tmp_path / f"{suffix}.db"), rng_seed=5)
    e.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = e.execute_action("setup_choose_region", {"region": "罪孽都市"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.energy = 0
    return e


def _give(entity, name, x=1):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=x,
    )


# ---------- 避风铃 ----------

def test_lamp_zero_from_fatigue_grants_15(tmp_path):
    """正常路径：疲惫把当前速度打到0，避风铃+15；没有闪避，不加3。"""
    e = _engine(tmp_path, "bell_fat")
    begin_battle(e)
    p = e.state.player
    e.state.relics.append(Relic("避风铃", ""))
    p.current_speed = 4
    p.shield = 0
    e.combat.pay_numeric_cost(p, "疲惫", 4)
    assert p.current_speed == 0
    assert p.shield == 15


def test_lamp_zero_from_dodge_grants_3_plus_15(tmp_path):
    """边界：闪避1→0，闪避句+3，归零句+15。"""
    e = _engine(tmp_path, "bell_dodge")
    begin_battle(e)
    p, m = e.state.player, e.state.enemies[0]
    e.state.relics.append(Relic("避风铃", ""))
    p.current_speed = 1
    p.shield = 0
    extra = e.combat._spend_dodge_speed(p)
    assert p.current_speed == 0
    assert extra["avoid_wind_shield"] == 3
    assert p.shield == 18


def test_lamp_already_zero_does_not_regrant(tmp_path):
    """对照：当前速度已是0，失速总线不再发归零。"""
    e = _engine(tmp_path, "bell_zero")
    begin_battle(e)
    p = e.state.player
    e.state.relics.append(Relic("避风铃", ""))
    p.current_speed = 0
    p.shield = 0
    assert e.combat._lose_current_speed(p, 3) == 0
    assert p.current_speed == 0
    assert p.shield == 0


def test_lamp_zero_from_atrophy_overflow(tmp_path):
    """第二条来源：萎缩把速限压到当前速度以下，溢出失速归零也+15。"""
    e = _engine(tmp_path, "bell_atrophy")
    begin_battle(e)
    p = e.state.player
    e.state.relics.append(Relic("避风铃", ""))
    p.speed_limit = 6
    p.current_speed = 3
    p.shield = 0
    e.combat.pay_numeric_cost(p, "萎缩", 6)
    assert p.speed_limit == 0
    assert p.current_speed == 0
    assert p.shield == 15


def test_lamp_sealed_zero_does_not_grant(tmp_path):
    """非法：封印期间当前速度归零不加15。"""
    e = _engine(tmp_path, "bell_seal")
    begin_battle(e)
    p = e.state.player
    e.state.relics.append(Relic("避风铃", ""))
    e.state.sealed_relics["避风铃"] = 1
    p.current_speed = 2
    p.shield = 0
    e.combat.pay_numeric_cost(p, "疲惫", 2)
    assert p.current_speed == 0
    assert p.shield == 0


# ---------- 洗劫听造伤 ----------

def test_xijie_steals_from_huifeng_damage(tmp_path):
    """正常路径：洗劫听敌对伤害入口；回锋刀失速伤也夺碎片。"""
    e = _engine(tmp_path, "xijie_hf")
    begin_battle(e)
    strip_monster_relics(e)  # 活血衣会回血，断言精确掉血前先隔离随机遗物
    p, m = e.state.player, e.state.enemies[0]
    e.state.relics.append(Relic("回锋刀", ""))
    p.add_status(StatusEffect(name="洗劫", value=1, remaining_rounds=2, source=p.name))
    m.shards = 20
    e.state.shards = 0
    hp = m.current_hp
    e.combat._remember_huifeng_target(p, "enemy:0")
    e.combat._lose_current_speed(p, 2)
    assert m.current_hp == hp - 6
    assert e.state.shards == 6
    assert m.shards == 14


def test_xijie_absent_does_not_steal_from_huifeng(tmp_path):
    """对照：没有洗劫状态时，回锋刀造伤不夺碎片。"""
    e = _engine(tmp_path, "xijie_no")
    begin_battle(e)
    p, m = e.state.player, e.state.enemies[0]
    e.state.relics.append(Relic("回锋刀", ""))
    m.shards = 20
    e.state.shards = 0
    e.combat._remember_huifeng_target(p, "enemy:0")
    e.combat._lose_current_speed(p, 2)
    assert e.state.shards == 0
    assert m.shards == 20


# ---------- 焦黑发丝听命零 ----------

def test_jiaohhei_on_bleed_death(tmp_path):
    """正常路径：怪物因流血代价命零，焦黑发丝仍+2当前速度。"""
    e = _engine(tmp_path, "hair_bleed")
    begin_battle(e)
    p, m = e.state.player, e.state.enemies[0]
    e.state.relics.append(Relic("焦黑发丝", ""))
    # 2026-09-13 全局上限：满速时 +2 会被吃掉，先腾空间再验"焦黑发丝触发了"
    p.current_speed = max(0, p.speed_limit - 2)
    speed = p.current_speed
    m.current_hp = 3
    e.combat._pay_bleed_cost(m, 5)
    assert not m.is_alive
    assert p.current_speed == speed + 2


def test_jiaohhei_sealed_does_not_trigger(tmp_path):
    """非法：封印期间怪物命零不加速。"""
    e = _engine(tmp_path, "hair_seal")
    begin_battle(e)
    p, m = e.state.player, e.state.enemies[0]
    e.state.relics.append(Relic("焦黑发丝", ""))
    e.state.sealed_relics["焦黑发丝"] = 2
    # 2026-09-13 全局上限：满速时 +2 会被吃掉，先腾空间再验"焦黑发丝触发了"
    p.current_speed = max(0, p.speed_limit - 2)
    speed = p.current_speed
    e.combat._apply_hostile_damage(m, m.current_hp + 10, source=p)
    assert not m.is_alive
    assert p.current_speed == speed


def test_jiaohhei_does_not_trigger_on_monster_mutation(tmp_path):
    """怪物达到50层异变并未命零，因此不能触发焦黑发丝。"""
    e = _engine(tmp_path, "hair_collapse")
    begin_battle(e)
    p, m = e.state.player, e.state.enemies[0]
    e.state.relics.append(Relic("焦黑发丝", ""))
    # 2026-09-13 全局上限：满速时 +2 会被吃掉，先腾空间再验"焦黑发丝触发了"
    p.current_speed = max(0, p.speed_limit - 2)
    speed = p.current_speed
    m.mutation_count = 49
    e.combat.pay_numeric_cost(m, "异变", 1)
    assert m.is_alive and m.mutation_count == 50
    assert p.current_speed == speed


def test_jiaohhei_on_qianjingjia_reflect(tmp_path):
    """第三条来源：怪物被【千荆甲】反噬命零，焦黑发丝仍+2（2026-10-03 起替代爆裂）。"""
    e = _engine(tmp_path, "hair_baolie")
    begin_battle(e)
    p, m = e.state.player, e.state.enemies[0]
    e.state.relics.append(Relic("焦黑发丝", ""))
    # 2026-09-13 全局上限：满速时 +2 会被吃掉，先腾空间再验"焦黑发丝触发了"
    p.current_speed = max(0, p.speed_limit - 2)
    speed = p.current_speed
    e.state.relics.append(Relic("千荆甲", ""))
    m.current_hp = 4
    e.combat._apply_hostile_damage(p, 10, source=m, ctx={
        "timing": "monster_action", "source": "普通攻击", "source_type": "attack",
        "actor": m, "target": p, "mechanic": "damage", "subtype": "attack",
        "amount": 10, "tags": {"attack"}})
    assert not m.is_alive
    assert p.current_speed == speed + 2


# ---------- 寒冰法力听施加法力 ----------

def test_frost_mana_from_spell_reaction(tmp_path):
    """正常路径：反应法术消耗法力也累计寒冰法力。"""
    e = _engine(tmp_path, "ice_spell")
    begin_battle(e)
    begin_round(e)
    p, m = e.state.player, e.state.enemies[0]
    e.state.relics.append(Relic("寒冰法力", "", tags=["血族"]))
    _give(p, "杀伐")
    p.spells.append(Spell("先发制人", ["杀伐"], "受到伤害前", "发动杀伐X"))
    p.current_mana = 20
    e.combat.note_mana_inflicted(p, m, 10)
    assert m.mana_inflicted_this_round == 10
    assert m.get_status_value("无力") == 1


def test_frost_mana_below_ten_no_stack(tmp_path):
    """边界：不足10点不叠无力。"""
    e = _engine(tmp_path, "ice_bound")
    begin_battle(e)
    p, m = e.state.player, e.state.enemies[0]
    e.state.relics.append(Relic("寒冰法力", "", tags=["血族"]))
    e.combat.note_mana_inflicted(p, m, 9)
    assert m.get_status_value("无力") == 0
    e.combat.note_mana_inflicted(p, m, 1)
    assert m.get_status_value("无力") == 1
