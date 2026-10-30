"""守夜灯法力与反应法术。

现行规则：守夜灯在持有者每次经历回合的【回增】相位，获得等同于当前世界回合数的法力。
本文件仍用同一套反应法术夹具，验证：授予量＝世界回合数、法力留存不清空、
以及"法力不足"在结算期表现为带原因的 interrupted（不是提交错误、不吞成异常）。
（世界回合数由引擎在[回始]递增，测试里直接设定 `state.current_round` 取值。）
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.mechanisms import Phase
from engine.models import Entity, DaoWen, DaoWenInstance, Relic, Spell
from tests.setup_support import finish_initial_daowen


def _grant_round_increase(e, player):
    """通过现行声明机制结算一次目标经历回合的回增相位。"""
    before = player.current_mana
    logs = [entry for entry in e.combat._dispatch_phase(Phase.ROUND_INCREASE, target=player)
            if entry]
    return {"gained": player.current_mana - before, "logs": logs}


def _engine(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "t.db"), rng_seed=7,
                   sealed_candidate_path=str(tmp_path / "s.json"))
    e.execute_action("setup_attributes", {
        "name": "守夜者", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)  # 开局：杀伐
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    # 开局会随机发现一件遗物；本文件只测守夜灯与反应法术，清掉随机遗物，
    # 避免【万钧印】等无关效果改写受击路径。
    e.state.relics.clear()
    p = e.state.player
    p.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=0)
    p.spells.append(Spell(name="先发制人", required_daowen=["杀伐"],
                          trigger_condition="受到伤害前",
                          effect_flow="受到伤害前→发动杀伐 X"))
    return e


def _monster_phase_submit(e, spell_x=None, trigger_spell=True):
    """构造怪物攻击玩家的提交：怪物普攻1次，玩家可提交先发制人反应。

    返回 (prepared, choice) 供 resolve 使用。
    """
    m = Entity(name="靶怪", entity_type="怪物", blood_limit=200, current_hp=200,
               attack_count=1, attack_power=3)
    e.state.enemies.append(m)
    e.combat.reset_monster_activation()
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.current_round = 2
    prepared = e.combat.prepare_monster_phase()
    actor = next(a for a in prepared["actors"] if a["actor_ref"] == "enemy:0")
    target_option = next(t for t in actor["attack_target_options"] if t["ref"] == "player:0")
    spell_choices = {timing: {} for timing in ("before", "after")}
    for sp in target_option.get("spell_options", {}).get("before", []) or []:
        if sp["spell_name"] == "先发制人" and trigger_spell:
            if spell_x is None:
                spell_x = 2
            spell_choices["before"]["先发制人"] = {
                "use": True,
                "steps": [{"x": spell_x, "target_ref": sp["steps"][0]["target_ref"],
                           "dodge": False}],
            }
        else:
            spell_choices["before"][sp["spell_name"]] = {"use": False}
    for sp in target_option.get("spell_options", {}).get("after", []) or []:
        spell_choices["after"][sp["spell_name"]] = {"use": False}
    hits = [{"target_ref": "player:0", "dodge": False, "blood_shadow": False,
             "spell_choices": spell_choices} for _ in range(actor["base_hits_per_attack"])]
    choice = {"actor_ref": actor["actor_ref"], "daowen": None,
              "attack_actions": [{"hits": hits} for _ in range(actor["base_attack_actions"])]}
    return prepared, choice


def test_shouyedeng_mana_enables_reaction_spell(tmp_path):
    """守夜灯授予的法力可用于随后的怪物阶段反应法术。"""
    e = _engine(tmp_path)
    e.state.relics.append(Relic(name="守夜灯", effect="", tags=[]))
    p = e.state.player
    p.current_mana = 0
    p.mana_limit = 20
    e.state.current_round = 10  # 世界第10回合，每次经历回合授予10点法力
    assert _grant_round_increase(e, p)["gained"] == 10
    assert p.current_mana == 10
    prepared, choice = _monster_phase_submit(e, spell_x=2)  # 消耗2 ≤ 2
    res = e.combat.resolve_monster_phase([choice], prepared)
    # 先发制人应已触发（怪物受到反打伤害）
    fired = any(
        lg.get("spell") == "先发制人" and lg.get("execution")
        for hit in res for lg in (hit.get("spell_logs") or []))
    assert fired, "先发制人应使用守夜灯法力触发"
    # 执行后法力扣除正确：0 +10(守夜灯) -2(先发制人) = 8；不再有[敌回终]清空
    assert p.current_mana == 8, f"法力应只被法术消耗扣光，实{p.current_mana}"
    # 靶怪是[怪物]（读面板 1击×3伤），反打不抵消来袭：玩家应恰好掉 3 点。
    # 旧写法 "<= 57" 是把当时血限60硬编进断言（60-3=57）；血限随加点定价变成66后失效。
    assert p.current_hp == p.blood_limit - 3, \
        f"守夜灯法力被用于反打，玩家仍应受靶怪那 3 点伤害，实{p.blood_limit - p.current_hp}"


def test_shouyedeng_mana_persists_after_monster_phase(tmp_path):
    """守夜灯法力不再被清空：怪物阶段结束后没花掉的部分留在池里。"""
    e = _engine(tmp_path)
    e.state.relics.append(Relic(name="守夜灯", effect="", tags=[]))
    p = e.state.player
    p.current_mana = 0
    p.mana_limit = 20
    e.state.current_round = 10
    _grant_round_increase(e, p)  # 第10回合＝10点法力
    assert p.current_mana == 10
    prepared, choice = _monster_phase_submit(e, spell_x=1)  # 只花1法力
    res = e.combat.resolve_monster_phase([choice], prepared)
    assert not [x for x in res if x.get("type") == "shouyedeng_grant"], \
        "授予发生在经历回合的[回增]，怪物阶段不应再授予"
    assert not [x for x in res if x.get("type") == "shouyedeng_clear"], \
        "守夜灯法力不再清空"
    assert p.current_mana == 9, f"10授予-1消耗=9应留存，实{p.current_mana}"


def test_shouyedeng_grants_for_each_experienced_round(tmp_path):
    """同一世界回合内每额外经历一个回合均再次授予；实际法力仍受法限约束。"""
    e = _engine(tmp_path)
    e.state.relics.append(Relic(name="守夜灯", effect="", tags=[]))
    p = e.state.player
    p.current_mana = 0
    p.mana_limit = 20
    e.state.current_round = 10
    first = _grant_round_increase(e, p)
    assert first["gained"] == 10
    assert p.current_mana == 10
    p.current_mana = 0
    second = _grant_round_increase(e, p)
    assert second["gained"] == 10
    assert p.current_mana == 10


def test_insufficient_real_mana_interrupts_at_runtime(tmp_path):
    """守夜灯授予量不足以覆盖法术消耗 → 执行期中断（资源问题是中断，不是提交错误）。"""
    e = _engine(tmp_path)
    e.state.relics.append(Relic(name="守夜灯", effect="", tags=[]))
    p = e.state.player
    p.current_mana = 0
    p.mana_limit = 4
    e.state.current_round = 2
    _grant_round_increase(e, p)  # 第2回合授予2；先发制人 X=3 需3 > 2
    prepared, choice = _monster_phase_submit(e, spell_x=3)
    res = e.combat.resolve_monster_phase([choice], prepared)
    logs = [lg for hit in res for lg in (hit.get("spell_logs") or [])
            if lg.get("spell") == "先发制人"]
    assert logs and logs[0].get("interrupted") == "mana_insufficient", logs
    assert not p.has_status("勾魂"), "中断后无副作用"
    assert p.current_mana == 2, "法力不足时不得扣除未能支付的法力"


def test_no_shouyedeng_mana_zero_interrupts_at_runtime(tmp_path):
    """无守夜灯且当前法力=0 → 反应法术执行期中断，怪物阶段照常完成。"""
    e = _engine(tmp_path)
    p = e.state.player
    p.current_mana = 0
    prepared, choice = _monster_phase_submit(e, spell_x=2)
    res = e.combat.resolve_monster_phase([choice], prepared)
    logs = [lg for hit in res for lg in (hit.get("spell_logs") or [])
            if lg.get("spell") == "先发制人"]
    assert logs and logs[0].get("interrupted") == "mana_insufficient", logs


def test_normal_mana_source_works_without_shouyedeng(tmp_path):
    """不影响普通法力来源：无守夜灯、法力充足时反应法术照常触发。"""
    e = _engine(tmp_path)
    p = e.state.player
    p.current_mana = 20  # 普通法力（如回始补满）
    prepared, choice = _monster_phase_submit(e, spell_x=3)
    res = e.combat.resolve_monster_phase([choice], prepared)
    fired = any(
        lg.get("spell") == "先发制人" and lg.get("execution")
        for hit in res for lg in (hit.get("spell_logs") or []))
    assert fired, "普通法力下先发制人应触发"
