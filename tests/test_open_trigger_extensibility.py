"""开放可扩展触发时点：新增时点=注册一条事件挂钩，注册后即被 DSL 接受并真实触发。

用户约定：**凡是语法能接受的触发时必须要么真能触发、要么如实标注未接线**，
不许出现“被识别却永不触发、还静默装作可用”的死时点。EXTRA_TRIGGERS 里只登记
**确实在引擎接了线的时点**；“敌回始”属例外——DSL 接受，但生产 define_spell 以
wired=False + “暂未接入战斗结算管线”警告如实标注（见本文件末尾回归测试）。

验证点：
  1. 既有 11 个时点全部仍被 DSL 接受（防止后续把它改成不可识别/死时点）。
  2. 新注册时点「闪避时」被 DSL 接受（含同义写法「闪避后」「被躲避后」）。
  3. 「闪避时」在持有者成功闪避一次攻击后**真实触发**（走 _fire_auto_reaction）。
  4. 未注册的写法（如「对方施法前」）仍被拒绝，保持词汇表封闭可预期。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.spell_dsl import parse_spell_definition, SpellDslError
from engine.daowen import DaoWenEngine
from engine.models import Entity, GameState, DaoWen, DaoWenInstance, Spell


def _known() -> set[str]:
    DaoWenEngine.register_all()
    return set(DaoWenEngine.list_all())


# 各时点一条能通过校验的效果流：伤害类用“攻击者”，全局/开放类用“自身”。
_VALID_FLOW = {
    "受到伤害前": "发动杀伐 X于攻击者", "受到伤害后": "发动杀伐 X于攻击者",
    "失去生命前": "发动杀伐 X于攻击者", "失去生命后": "发动杀伐 X于攻击者",
    "目标发动道纹前": "发动杀伐 X于攻击者",
    "战始": "发动庇护 X于自身", "战终": "发动庇护 X于自身", "回始": "发动庇护 X于自身",
    "回终": "发动庇护 X于自身", "敌回始": "发动庇护 X于自身", "敌回终": "发动庇护 X于自身",
    "闪避时": "发动庇护 X于自身",
}


def test_all_existing_triggers_still_parse():
    """既有 11 个时点 + 新注册「闪避时」全部仍可被 DSL 接受。"""
    for trig, flow in _VALID_FLOW.items():
        assert parse_spell_definition(trig, flow, _known()) is not None, trig


def test_new_trigger_synonyms_parse():
    """新注册「闪避时」的合法写法（含同义）可被接受。"""
    assert parse_spell_definition("闪避时", "发动庇护 X于自身", _known()) is not None
    assert parse_spell_definition("闪避后", "发动庇护 X于自身", _known()) is not None
    assert parse_spell_definition("被躲避后", "发动庇护 X于自身", _known()) is not None


def test_unregistered_trigger_rejected():
    """未注册写法仍被拒绝（词汇表封闭，不会随意放行新时点）。"""
    with pytest.raises(SpellDslError):
        parse_spell_definition("对方施法前", "发动庇护 X于自身", _known())


def test_unwired_trigger_is_honestly_labelled_by_production_define_spell(tmp_path):
    """诚实契约：DSL 接受、但引擎尚未接线的时点，生产 define_spell 必须

    wired=False + 明确“暂未接入战斗结算管线”警告——不许静默标注成会触发。
    本测试走生产 define_spell（4 个 sim 探针都走各自的 _learn 捷径、复制了这条判定，
    因此生产包装层此前没有任何覆盖），并用已接线时点做反向对照，防止退化成恒 False。
    """
    from engine.api import GameEngine
    from tests.setup_support import begin_battle, begin_round, finish_initial_daowen

    e = GameEngine(db_path=str(tmp_path / "g.db"), save_dir=str(tmp_path / "saves"),
                   sealed_candidate_path=str(tmp_path / "cand.json"), rng_seed=5)
    assert e.execute_action("setup_attributes", {
        "name": "测试者", "blood_points": 11, "speed_points": 8, "mana_points": 6})["success"]
    assert finish_initial_daowen(e)["success"]
    assert e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})["success"]
    assert e.execute_action("setup_choose_region", {"region": "龙心谷"})["success"]
    # 本用例验证触发时点接线，清空随机遗物以隔离无关被动效果。
    e.state.relics = []
    player = e.state.player
    player.dao_wen["庇护"] = DaoWenInstance(
        DaoWen(name="庇护", formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=0, x_free=True)
    assert begin_battle(e)["success"]
    assert begin_round(e)["success"]

    r = e.execute_action("define_spell", {"spell": {
        "name": "死时点探针", "trigger_condition": "敌回始", "required_daowen": ["庇护"],
        "effect_flow": "发动庇护X于自身"}})
    assert r["success"], r
    assert r["result"]["wired"] is False
    assert "暂未接入" in (r["result"].get("warning") or ""), r["result"]

    # 第一手后轮到敌方；让敌方跳过后才轮到玩家做反向对照。
    player.actions_used_this_round = 0
    assert e.execute_action("skip", {"actor_ref": "enemy:0"})["success"]
    r2 = e.execute_action("define_spell", {"spell": {
        "name": "回始探针", "trigger_condition": "回始", "required_daowen": ["庇护"],
        "effect_flow": "发动庇护X于自身"}})
    assert r2["success"], r2
    assert r2["result"]["wired"] is True
    assert not r2["result"].get("warning"), r2["result"]


def test_dodge_trigger_fires_on_successful_dodge():
    """持有者成功闪避一次攻击 → 「闪避时」反应法术真实触发（护佑·庇护加盾）。"""
    state = GameState(phase="in_combat", combat_subphase="player_actions", current_round=1)
    player = Entity("玄夜", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=20, current_mana=20, speed_limit=3, current_speed=3)
    enemy = Entity("靶怪", "怪物", blood_limit=100, current_hp=100, attack_power=3)
    state.player = player
    state.enemies = [enemy]
    player.dao_wen["庇护"] = DaoWenInstance(
        DaoWen(name="庇护", formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=0)
    player.spells.append(Spell(
        name="护佑", required_daowen=["庇护"], trigger_condition="闪避时",
        effect_flow="发动庇护 X于自身"))
    combat = CombatEngine(state, DiceEngine())

    refs = combat._combat_entity_refs()
    # 【庇护】给的是免疫次数（status value），不再加[格挡]——格挡只来自遗物。
    before_shelter = player.get_status_value("庇护")
    combat.resolve_attack(enemy, player, dodge=True, entity_refs=refs)
    assert player.get_status_value("庇护") > before_shelter, \
        "成功闪避后「闪避时」反应应触发（庇护免疫次数+X，时长X回合）"
