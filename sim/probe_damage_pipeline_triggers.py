"""真实引擎冒烟：逐一实测【受到伤害后】【失去生命前】两个伤害管线内挂接点

是否真正接线并触发。

这两个时机复用了既有【受到伤害前】【失去生命后】反应型法术的同一套
prepare/validate/resolve_spell_reactions流水线（见 engine/combat.py
CombatEngine._REACTION_SPELL_SLOTS），只是新增了两个key："damage_after"
（受到伤害后）与"life_before"（失去生命前）。本探针验证：
1. 【学习】阶段 wired 字段如实标注为 True；
2. 真实进入战斗后，法术被正确列入 spell_options 候选（对应新key）；
3. 显式提交 use=True 后，资源变化符合预期；
4. 旧调用点（只提交 before/after 两个key、不知道新key存在）依然兼容不报错
   （由 test_shouyedeng_reaction_spells.py 等既有测试保证，这里额外冒烟
   一次以防回归）。

运行：python3 sim/probe_damage_pipeline_triggers.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance
from tests.setup_support import finish_initial_daowen
from sim.monster_phase_submit import single_actor_choices, monster_phase_choices  # noqa: E402


def _fresh_engine(tag: str, region: str = "罪孽都市") -> GameEngine:
    d = tempfile.mkdtemp(prefix=f"probe_dmgpipe_{tag}_")
    e = GameEngine(db_path=os.path.join(d, "g.db"), rng_seed=7,
                   sealed_candidate_path=os.path.join(d, "s.json"))
    e.execute_action("setup_attributes", {
        "name": "测试者", "blood_points": 10, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": region})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    return e


def _give_daowen(entity, name, x=0):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=x)




from sim.common import learn_spell_probe_shortcut as _learn  # 2026-11-03：4 份逐字相同 + 删掉一份自递归死代码


def _decline_all_spell_choices(target_option):
    return {k: {sp["spell_name"]: {"use": False} for sp in target_option.get("spell_options", {}).get(k, [])}
            for k in ("before", "after", "damage_after", "life_before")}


REPORT = []


def record(trigger_label, wired_flag, fired, detail):
    REPORT.append({"trigger": trigger_label, "learn_wired": wired_flag,
                   "actually_fired": fired, "detail": detail})


def probe_after_damage_taken():
    """受到伤害后：怪物普攻命中玩家（格挡吸收部分/全部伤害均视为"受到了伤害"），
    玩家的自创法术在伤害落地后立即反打攻击者。"""
    e = _fresh_engine("after_damage")
    _give_daowen(e.state.player, "杀伐")
    _give_daowen(e.state.player, "庇护")
    definition = {"name": "落地反击", "required_daowen": ["杀伐"],
                  "trigger_condition": "受到伤害后",
                  "effect_flow": "发动杀伐X于攻击者"}
    learned = _learn(e, definition)
    wired = learned["result"].get("wired")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    e.state.player.current_mana = 20
    e.state.player.current_hp = 200
    enemy = e.state.enemies[0]
    enemy.attack_power = 5
    enemy.attack_count = 1
    hp_before = enemy.current_hp

    res = e.execute_action("prepare_monster_phase", {})
    a = res["result"]["actors"][0]
    target_opt = a["attack_target_options"][0]
    spell_choices = _decline_all_spell_choices(target_opt)
    spell_choices["damage_after"]["落地反击"] = {"use": True, "steps": [{"x": 6, "target_ref": "enemy:0", "dodge": False}], "max_iterations": 1}
    choices = single_actor_choices(a, engine=e,
                                            spell_uses=spell_choices)
    r = e.execute_action("resolve_monster_phase", {"token": res["result"]["token"], "choices": choices})
    hp_after = e.state.enemies[0].current_hp
    fired = r["success"] and hp_after < hp_before
    record("受到伤害后", wired, fired,
           f"resolve success={r['success']}；敌方hp {hp_before}→{hp_after}"
           f"（应因【落地反击】的【杀伐6】掉12点，触发点在伤害落地之后）")


def probe_after_damage_taken_even_when_shielded():
    """受到伤害后（边界）：格挡把伤害全部吸收，仍应判定"受到了一次伤害"而触发。

    与"失去生命后"严格区分——后者要求actual_damage>0，前者只要求damage>0
    （这一击最终确定的伤害数值，不受格挡是否吸收影响）。
    """
    e = _fresh_engine("after_damage_shielded")
    _give_daowen(e.state.player, "杀伐")
    definition = {"name": "格挡侦知", "required_daowen": ["杀伐"],
                  "trigger_condition": "受到伤害后",
                  "effect_flow": "发动杀伐X于攻击者"}
    learned = _learn(e, definition)
    wired = learned["result"].get("wired")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    e.state.player.current_mana = 20
    e.state.player.current_hp = 200
    e.state.player.shield = 999  # 格挡全额吸收，实际伤害actual_damage=0
    enemy = e.state.enemies[0]
    enemy.attack_power = 5
    enemy.attack_count = 1
    hp_before = enemy.current_hp

    res = e.execute_action("prepare_monster_phase", {})
    a = res["result"]["actors"][0]
    target_opt = a["attack_target_options"][0]
    spell_choices = _decline_all_spell_choices(target_opt)
    spell_choices["damage_after"]["格挡侦知"] = {"use": True, "steps": [{"x": 3, "target_ref": "enemy:0", "dodge": False}], "max_iterations": 1}
    choices = single_actor_choices(a, engine=e,
                                            spell_uses=spell_choices)
    r = e.execute_action("resolve_monster_phase", {"token": res["result"]["token"], "choices": choices})
    hp_after = e.state.enemies[0].current_hp
    fired = r["success"] and hp_after < hp_before
    record("受到伤害后(格挡全吸收边界)", wired, fired,
           f"resolve success={r['success']}；敌方hp {hp_before}→{hp_after}"
           f"（玩家生命被格挡完全保护，actual_damage=0，但damage>0，"
           f"仍应触发【受到伤害后】法术，与要求actual_damage>0的"
           f"【失去生命后】严格区分）")


def probe_before_life_lost():
    """失去生命前：伤害数值已确定但生命尚未真正扣减，玩家的自创法术抢在
    扣血前对自己发动庇护，减少即将到来的生命损失。"""
    e = _fresh_engine("before_life_lost")
    _give_daowen(e.state.player, "庇护")
    definition = {"name": "临扣护体", "required_daowen": ["庇护"],
                  "trigger_condition": "失去生命前",
                  "effect_flow": "发动庇护X于自身"}
    learned = _learn(e, definition)
    wired = learned["result"].get("wired")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    e.state.player.current_mana = 20
    e.state.player.current_hp = 200
    e.state.player.shield = 0
    enemy = e.state.enemies[0]
    enemy.attack_power = 10
    enemy.attack_count = 1
    hp_before = e.state.player.current_hp

    res = e.execute_action("prepare_monster_phase", {})
    a = res["result"]["actors"][0]
    target_opt = a["attack_target_options"][0]
    spell_choices = _decline_all_spell_choices(target_opt)
    spell_choices["life_before"]["临扣护体"] = {"use": True, "steps": [{"x": 5, "target_ref": "player:0", "dodge": False}], "max_iterations": 1}
    choices = single_actor_choices(a, engine=e,
                                            spell_uses=spell_choices)
    r = e.execute_action("resolve_monster_phase", {"token": res["result"]["token"], "choices": choices})
    hp_after = e.state.player.current_hp
    hp_lost = hp_before - hp_after
    # 【庇护5】应产生10点格挡，抵消攻击力10的伤害全部或大部分，实际损失应<10
    fired = r["success"] and 0 <= hp_lost < 10
    record("失去生命前", wired, fired,
           f"resolve success={r['success']}；玩家hp {hp_before}→{hp_after}（损失{hp_lost}点）"
           f"（应因【临扣护体】的【庇护5】提前获得10点格挡挡下攻击力10的伤害，"
           f"实际损失应明显小于未接线时的10点）")


def probe_backward_compat_old_callers():
    """兼容性：只提交历史两个key（before/after），不知道damage_after/
    life_before新key存在的旧调用点，在没有对应候选法术时依然能正常结算，
    不因"未覆盖新key"而报错。"""
    e = _fresh_engine("backward_compat")
    _give_daowen(e.state.player, "杀伐")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    e.state.player.current_mana = 20
    e.state.player.current_hp = 200
    enemy = e.state.enemies[0]
    enemy.attack_power = 5
    enemy.attack_count = 1

    res = e.execute_action("prepare_monster_phase", {})
    a = res["result"]["actors"][0]
    target_opt = a["attack_target_options"][0]
    # 故意只提交旧的两个key，模拟历史调用点
    old_style_choices = {"before": {}, "after": {}}
    choices = single_actor_choices(a, engine=e,
                                            spell_uses=old_style_choices)
    r = e.execute_action("resolve_monster_phase", {"token": res["result"]["token"], "choices": choices})
    record("兼容性(旧调用点只提交before/after)", None, r["success"],
           f"resolve success={r['success']}"
           f"（玩家没有任何法术，damage_after/life_before均无候选，"
           f"旧式spell_choices={{before:{{}}, after:{{}}}}应能正常通过校验）")


def main():
    probe_after_damage_taken()
    probe_after_damage_taken_even_when_shielded()
    probe_before_life_lost()
    probe_backward_compat_old_callers()

    print(f"{'时机':<28}{'学得wired':<12}{'真实触发':<10}详情")
    all_ok = True
    for row in REPORT:
        ok = (row["learn_wired"] in (True, None)) and row["actually_fired"] is True
        all_ok = all_ok and ok
        print(f"{row['trigger']:<28}{str(row['learn_wired']):<12}{str(row['actually_fired']):<10}{row['detail']}")
    print()
    print("全部通过" if all_ok else "存在失败用例，见上表")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
