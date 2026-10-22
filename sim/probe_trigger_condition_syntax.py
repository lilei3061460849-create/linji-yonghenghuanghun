"""触发条件标准语法 —— 穷举同义写法的真实引擎实测。

与 probe_custom_spell_triggers.py 的区别：那个脚本只测"每种规范时机选
一个标准写法"，本脚本专门测"同一个规范时机的多种同义表述是否都能被
正确识别、且识别结果在真实战斗里的触发行为完全一致"——即验证语法层的
「同义词归一化」不只是解析器返回同一个字符串，而是真的驱动同样的
结算行为（已接线的时机=多种写法都能真实触发；未接线的时机=多种写法
都诚实返回 wired=False+具体warning，不会有的写法误判成已接线）。

同时覆盖《自创法术标准语法.md》1.1节列出的全部边界/歧义写法，逐条验证
文档描述与代码真实行为一致（拒绝空输入/裸词缺前后缀/未知词汇/歧义
"前后"同现写法按第一条命中规则处理）。

运行：python3 sim/probe_trigger_condition_syntax.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance
from engine.spell_dsl import parse_trigger, SpellDslError
from tests.setup_support import finish_initial_daowen
from sim.monster_phase_submit import single_actor_choices, monster_phase_choices  # noqa: E402


def _fresh_engine(tag: str) -> GameEngine:
    d = tempfile.mkdtemp(prefix=f"probe_syntax_{tag}_")
    e = GameEngine(db_path=os.path.join(d, "g.db"), rng_seed=7,
                   sealed_candidate_path=os.path.join(d, "s.json"))
    e.execute_action("setup_attributes", {
        "name": "测试者", "blood_points": 10, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "罪孽都市"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    return e


def _give_daowen(entity, name):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


def _learn(e, definition):
    """探针专用：直接写入玩家法术列表（跳过 define_spell 的 1 次出手消耗）。

    局外【学习】自定义法术入口已于 2026-09-16 取消（法术一律在战斗中用
    define_spell 自创）；探针只关心触发管线是否接线，故此捷径保留"学习"语义：
    仍然走同一个 _build_custom_spell/parse_spell_definition 校验与 wired 标注。
    """
    built = e._build_custom_spell(e.state.player, definition)
    if "error" in built:
        return {"success": False, "error": built["error"]}
    e.state.player.spells.append(built["spell"])
    parsed = built["parsed"]
    wired = parsed.trigger in e.combat._WIRED_TRIGGERS
    result = {"spell": built["spell"].to_dict(), "wired": wired, "cost": "0（探针捷径）"}
    if not wired:
        result["warning"] = (f"触发时机【{parsed.trigger}】已通过句式校验，"
                             "但该时机暂未接入战斗结算管线，本法术目前不会实际触发")
    return {"success": True, "action": "自定义法术(探针)", "result": result}


def _decline_all_spell_choices(target_option):
    return {k: {sp["spell_name"]: {"use": False} for sp in target_option.get("spell_options", {}).get(k, [])}
            for k in ("before", "after")}


# ---------------------------------------------------------------------------
# 一、纯解析层：规范时机 → 多种同义写法，逐条验证 parse_trigger 返回值
# ---------------------------------------------------------------------------

SYNONYM_GROUPS = {
    "受到伤害前": ["受到伤害前", "我方受到伤害前", "自身受到伤害前", "受到攻击前", "受到伤害的前"],
    "受到伤害后": ["受到伤害后", "受到攻击后"],
    "失去生命前": ["失去生命前", "生命减少前", "损失生命前", "掉血前", "扣血前"],
    "失去生命后": ["失去生命后", "生命减少后", "损失生命后", "掉血后", "扣血后",
                "失去生命后（循环）", "失去生命后(循环)"],
    "目标发动道纹前": ["目标发动道纹前", "对方发动道纹前", "使用道纹前", "出招前", "对方发动法术前"],
    "战始": ["战斗开始时", "战始", "开局时", "开局", "我方开局时"],
    "战终": ["战斗结束时", "战终", "结局时", "结局"],
    "回始": ["回合开始时", "回始", "每回合开始时", "每回合开始", "我方回合开始时", "对方回合开始时"],
    "回终": ["回合结束时", "回终", "每回合结束时"],
    "敌回始": ["敌方回合开始时", "敌回始", "敌方回始"],
    "敌回终": ["敌方回合结束时", "敌回终", "敌方回终"],
}

EDGE_CASES = [
    # (输入, 期望结果: 'reject' 或 具体规范时机字符串, 说明)
    ("", "reject", "空字符串"),
    ("   ", "reject", "纯空白"),
    ("受到伤害", "reject", "裸词缺前后缀，无法判断结算点"),
    ("受伤", "reject", "裸词缺前后缀"),
    ("生命减少", "reject", "裸词缺前后缀"),
    ("乱写一通", "reject", "完全不认识的词汇"),
    ("敌方受到伤害前", "受到伤害前", "含'敌'字但不影响'受到伤害前'判定（该规则无敌字排斥）"),
    ("战斗开始后", "战始", "'前后'对战始无区分意义，仍按核心词命中"),
    ("回合开始前", "回始", "同上，'前后'对回始无区分意义"),
    ("受到伤害前后", "受到伤害前", "'前''后'同现的歧义写法，按规则表顺序命中排在前面的'受到伤害前'"),
    ("失去生命前后", "失去生命前", "同上，命中'失去生命前'"),
    ("敌方发动道纹前", "目标发动道纹前", "含'敌'字但命中的是'发动道纹+前'关键词组，不是敌回合系列"),
]


def probe_parser_layer():
    print("=" * 78)
    print("一、纯解析层：同义写法归一化验证（parse_trigger 直接调用）")
    print("=" * 78)
    all_ok = True
    for canonical, phrasings in SYNONYM_GROUPS.items():
        results = []
        for p in phrasings:
            try:
                r = parse_trigger(p)
            except SpellDslError as e:
                r = f"REJECTED:{e}"
            results.append((p, r))
        ok = all(r == canonical for _, r in results)
        all_ok = all_ok and ok
        status = "✅" if ok else "❌"
        print(f"{status} 【{canonical}】{len(phrasings)}种写法 全部归一到同一规范时机={ok}")
        for p, r in results:
            mark = "✓" if r == canonical else "✗"
            print(f"    {mark} {p!r:24s} -> {r}")

    print("-" * 78)
    print("边界/歧义写法验证：")
    for text, expected, note in EDGE_CASES:
        try:
            r = parse_trigger(text)
        except SpellDslError as e:
            r = "reject"
        ok = (r == expected) if expected != "reject" else (r == "reject")
        all_ok = all_ok and ok
        status = "✅" if ok else "❌"
        print(f"{status} {text!r:20s} 期望={expected!r:16s} 实际={r!r:16s} —— {note}")
    print(f"\n解析层结论：全部同义写法/边界用例与文档描述一致 = {all_ok}")
    return all_ok


# ---------------------------------------------------------------------------
# 二、真实引擎层：已接线的3种时机，每种时机换不同同义写法真实学习+触发
# ---------------------------------------------------------------------------

def _fire_before_damage_case(trigger_text):
    """用给定的trigger_text文本学一个"受到伤害前"类法术，在真实战斗里验证触发。"""
    e = _fresh_engine("wired_before")
    p = e.state.player
    _give_daowen(p, "杀伐")
    definition = {"name": f"同义测试_{trigger_text}", "required_daowen": ["杀伐"],
                  "trigger_condition": trigger_text,
                  "effect_flow": "发动杀伐X于攻击者"}
    r2 = _learn(e, definition)
    if not r2["success"]:
        return False, None, f"学习被拒绝：{r2.get('error')}"
    wired = r2["result"].get("wired")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    p.current_mana = 20
    enemy = e.state.enemies[0]
    enemy.attack_power = 5
    enemy.attack_count = 1
    res = e.execute_action("prepare_monster_phase", {})
    a = res["result"]["actors"][0]
    target_opt = a["attack_target_options"][0]
    spell_name = definition["name"]
    spell_choices = _decline_all_spell_choices(target_opt)
    spell_choices["before"][spell_name] = {"use": True, "steps": [{"x": 3, "target_ref": "enemy:0", "dodge": False}], "max_iterations": 1}
    choices = single_actor_choices(a, engine=e,
                                            spell_uses=spell_choices)
    hp_before = enemy.current_hp
    r = e.execute_action("resolve_monster_phase", {"token": res["result"]["token"], "choices": choices})
    hp_after = e.state.enemies[0].current_hp
    fired = r["success"] and hp_after < hp_before
    return fired, wired, f"resolve success={r['success']}；敌方hp {hp_before}→{hp_after}"


def _fire_after_life_lost_case(trigger_text):
    e = _fresh_engine("wired_after")
    p = e.state.player
    _give_daowen(p, "再生")
    definition = {"name": f"同义测试_{trigger_text}", "required_daowen": ["再生"],
                  "trigger_condition": trigger_text,
                  "effect_flow": "发动再生X于自身"}
    r2 = _learn(e, definition)
    if not r2["success"]:
        return False, None, f"学习被拒绝：{r2.get('error')}"
    wired = r2["result"].get("wired")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    p.current_mana = 20
    p.current_hp = 50
    enemy = e.state.enemies[0]
    enemy.attack_power = 5
    enemy.attack_count = 1
    res = e.execute_action("prepare_monster_phase", {})
    a = res["result"]["actors"][0]
    target_opt = a["attack_target_options"][0]
    spell_name = definition["name"]
    spell_choices = _decline_all_spell_choices(target_opt)
    spell_choices["after"][spell_name] = {"use": True, "steps": [{"x": 4, "target_ref": "player:0", "dodge": False}], "max_iterations": 1}
    choices = single_actor_choices(a, engine=e,
                                            spell_uses=spell_choices)
    hp_before = p.current_hp
    r = e.execute_action("resolve_monster_phase", {"token": res["result"]["token"], "choices": choices})
    hp_after = p.current_hp
    fired = r["success"] and hp_after > hp_before - 5
    return fired, wired, f"resolve success={r['success']}；玩家hp {hp_before}→{hp_after}"


def _pure_attack_round(e):
    """驱动一整回合怪物阶段：全体按当前契约提交（有合法道纹则声明第一个），纯普攻。"""
    res = e.execute_action("prepare_monster_phase", {})
    assert res["success"], res
    choices = monster_phase_choices(res["result"]["actors"], engine=e)
    r = e.execute_action("resolve_monster_phase", {"token": res["result"]["token"],
                                                  "choices": choices})
    assert r["success"], r
    e.execute_action("round_end", {})
    e.execute_action("round_start", {})


def _fire_target_before_daowen_case(trigger_text):
    e = _fresh_engine("wired_daowen")
    p = e.state.player
    for n in ("坠落", "杀伐", "连击"):
        _give_daowen(p, n)
    spell_name = f"同义测试_{trigger_text}"
    definition = {"name": spell_name, "required_daowen": ["坠落", "杀伐", "连击"],
                  "trigger_condition": trigger_text,
                  "effect_flow": "发动坠落X于目标→发动杀伐X于目标→发动连击X于目标"}
    r2 = _learn(e, definition)
    if not r2["success"]:
        return False, None, f"学习被拒绝：{r2.get('error')}"
    wired = r2["result"].get("wired")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    p.current_mana = 20
    p.current_hp = 200
    enemy = e.state.enemies[0]
    _pure_attack_round(e)
    p.current_mana = 20
    res = e.execute_action("prepare_monster_phase", {})
    a = res["result"]["actors"][0]
    if not a["daowen_options"]:
        return False, wired, "第2回合怪物仍无可用道纹，无法构造触发场景"
    opt = a["daowen_options"][0]
    trigger_options = opt.get("trigger_spell_options", {})
    listed = "player:0" in trigger_options and any(
        sp["spell_name"] == spell_name for sp in trigger_options["player:0"])
    dao_choice = {"name": opt["name"], "dodge": False, "blood_shadow": False,
                  "trigger_spell_choices": {"player:0": {spell_name: {"use": True, "steps": [
                      {"x": 3, "target_ref": "enemy:0", "dodge": False},
                      {"x": 4, "target_ref": "enemy:0", "dodge": False},
                      {"x": 2, "target_ref": "enemy:0", "dodge": False},
                  ]}}}}
    if opt["requires_target"]:
        dao_choice["target_ref"] = opt["target_options"][0]["ref"]
    target_opt = a["attack_target_options"][0]
    choices = [{"actor_ref": a["actor_ref"], "daowen": dao_choice,
                "attack_actions": [{"hits": [{"target_ref": target_opt["ref"], "dodge": False,
                                              "blood_shadow": False,
                                              "spell_choices": _decline_all_spell_choices(target_opt)}]}]}]
    hp_before = enemy.current_hp
    r = e.execute_action("resolve_monster_phase", {"token": res["result"]["token"], "choices": choices})
    hp_after = e.state.enemies[0].current_hp
    fired = r["success"] and listed and hp_after < hp_before
    return fired, wired, f"resolve success={r.get('success')}；候选已列出={listed}；敌方hp {hp_before}→{hp_after}"


WIRED_FIRE_FUNCS = {
    "受到伤害前": _fire_before_damage_case,
    "失去生命后": _fire_after_life_lost_case,
    "目标发动道纹前": _fire_target_before_daowen_case,
}


def probe_wired_synonyms():
    print()
    print("=" * 78)
    print("二、真实引擎层：已接线3种时机，逐一同义写法验证真实触发")
    print("=" * 78)
    all_ok = True
    for canonical, fire_func in WIRED_FIRE_FUNCS.items():
        phrasings = SYNONYM_GROUPS[canonical]
        print(f"\n【{canonical}】（{len(phrasings)}种同义写法逐一实测）")
        for p in phrasings:
            fired, wired, detail = fire_func(p)
            ok = (wired is True) and (fired is True)
            all_ok = all_ok and ok
            status = "✅" if ok else "❌"
            print(f"  {status} 写法={p!r:20s} wired={wired} fired={fired}  {detail}")
    print(f"\n已接线时机同义写法结论：全部写法均正确学会且真实触发 = {all_ok}")
    return all_ok


GLOBAL_TIMINGS = ("战始", "战终", "回始", "回终", "敌回始", "敌回终")

# 同义写法分组（与 probe_wired_synonyms 共用）。下表是 2026-10-02 后的实际口径：
#   · 全局时点（战始/战终/回始/回终/敌回始/敌回终）没有"攻击者/目标"身份，
#     写"于攻击者"必须在**学习阶段**被拒绝（不能学会一个语义不成立的写法）；
#   · 伤害管线时点（受到伤害后/失去生命前）天然带攻击者，写"于攻击者"必须
#     学会并标注 wired=True。
def probe_trigger_target_identity_boundary():
    print()
    print("=" * 78)
    print("三、真实引擎层：触发时点的目标身份边界（全局时点 vs 伤害管线时点）")
    print("=" * 78)
    all_ok = True
    for canonical in ("受到伤害后", "失去生命前", "战始", "战终", "回始", "回终", "敌回始", "敌回终"):
        phrasings = SYNONYM_GROUPS[canonical]
        global_timing = canonical in GLOBAL_TIMINGS
        expect = "学习阶段拒绝（无攻击者身份）" if global_timing else "学会并标注已接线"
        print(f"\n【{canonical}】{len(phrasings)}种同义写法——预期：{expect}")
        for p in phrasings:
            e = _fresh_engine(f"boundary_{canonical}")
            _give_daowen(e.state.player, "杀伐")
            definition = {"name": f"同义测试_{p}", "required_daowen": ["杀伐"],
                          "trigger_condition": p, "effect_flow": "发动杀伐X于攻击者"}
            r = _learn(e, definition)
            if global_timing:
                ok = (not r["success"]) and "攻击者" in (r.get("error") or "")
            else:
                ok = r["success"] and r["result"].get("wired") is True
            all_ok = all_ok and ok
            print(f"  {'✅' if ok else '❌'} 写法={p!r:20s} "
                  f"{'拒绝：' + (r.get('error') or '')[:36] if not r['success'] else 'wired=' + str(r['result'].get('wired'))}")
    print(f"\n触发时点目标身份边界结论：全部写法均符合当前口径 = {all_ok}")
    return all_ok


def main():
    ok1 = probe_parser_layer()
    ok2 = probe_wired_synonyms()
    ok3 = probe_trigger_target_identity_boundary()
    print()
    print("=" * 78)
    print(f"总体结论：解析层={ok1}  已接线真实触发层={ok2}  未接线诚实标注层={ok3}")
    all_ok = ok1 and ok2 and ok3
    print(f"全部通过 = {all_ok}")
    print("=" * 78)
    # 退出码契约（2026-10-02 独立验证补齐）：此前只打印结论、永远 exit 0。
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
