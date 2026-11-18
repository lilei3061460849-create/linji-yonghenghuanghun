#!/usr/bin/env python3
"""【献锋】1:1 / 1:4 真实引擎配对实验；只覆写进程内 effect_rate，不改正式事实源。"""
from __future__ import annotations
import argparse, json, os, sys, tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine import derivatives as dr
from engine.api import GameEngine
from engine.ai_tactics import TacticalAI
from engine.daowen import DaoWenEngine
from engine.models import DaoWen, DaoWenInstance, Entity
from sim.alt_path_test import resolve_monster_turn
from tests.setup_support import begin_battle, begin_round, finish_initial_daowen, grant_extra_actions, hand_priority_to_player, player_passes

def one(seed: int, effect_rate: int, extra_actions: int) -> dict:
    old = dr.BY_NAME["献锋"]
    if effect_rate:
        dr.BY_NAME["献锋"] = replace(old, effect_rate=effect_rate)
    DaoWenEngine.register_all()
    try:
        tmp = tempfile.mkdtemp(prefix=f"fengjian-{seed}-{effect_rate}-")
        e = GameEngine(db_path=os.path.join(tmp, "rulings.db"),
                       save_dir=os.path.join(tmp, "saves"), autosave=False, rng_seed=seed)
        for action, params in (
            ("setup_attributes", {"name":"献锋实验者","blood_points":5,"speed_points":8,"mana_points":12}),
        ):
            result=e.execute_action(action,params)
            if not result.get("success"): raise RuntimeError(f"{action}: {result}")
        finish_initial_daowen(e, prefer="杀伐")
        for action, params in (
            ("setup_choose_resonance", {"resonance_type":"反转"}),
            ("setup_choose_region", {"region":"龙心谷"}),
        ):
            result=e.execute_action(action,params)
            if not result.get("success"): raise RuntimeError(f"{action}: {result}")
        started=begin_battle(e)
        if not started.get("success"): raise RuntimeError(f"battle_start: {started}")
        p=e.state.player
        if p is None: raise RuntimeError("missing player")
        p.dao_wen.clear()
        p.dao_wen["献锋"]=DaoWenInstance(
            DaoWen(name="献锋",formula="",cost_type="攻击力",cost_formula="X",effect_formula="回复X生命"),
            x_value=0,x_free=True)
        p.blood_limit=80; p.current_hp=60
        p.mana_limit=12; p.current_mana=12
        p.speed_limit=8; p.current_speed=8
        e.state.relics.clear()
        foe=Entity("实验靶怪","怪物",blood_limit=500,current_hp=500,
                   mana_limit=1,current_mana=1,speed_limit=1,current_speed=1,
                   attack_count=1,attack_power=1)
        e.state.enemies[:]=[foe]
        rs=begin_round(e)
        if not rs.get("success"): raise RuntimeError(f"round_start: {rs}")
        hand_priority_to_player(e)
        e.combat._bind_existing_hp_hooks()
        grant_extra_actions(p,extra_actions)

        ai=TacticalAI(e,verbose=True); ai.new_round()
        hp0=p.current_hp; power0=p.effective_attack_power()
        hits=p.effective_attack_count(); foe0=foe.current_hp
        if effect_rate:
            cast=e.execute_action("use_daowen",{
                "daowen_name":"献锋","x":1,"target_ref":"player:0",
                "dodge":False,"blood_shadow":False,"trigger_spell_choices":{}})
            if not cast.get("success"): raise RuntimeError(f"献锋发动失败: {cast}")
        else:
            # 无献锋对照：保留同样的持有道纹，但本回合不施放，AI 仍可使用全部行动。
            cast={"success":True,"control":"no_cast"}
        ai.used["献锋"]=1
        hp1=p.current_hp; power1=p.effective_attack_power()
        # 交替出手：献锋施放后优先权交给敌方；敌方行动/跳过后玩家才能继续。
        all_player_actions=[]
        monster_results=[]
        turn_errors=[]
        for _ in range(12):
            if e.state.combat_subphase == "await_round_end":
                break
            if e.state.duel_turn == "opponent_side":
                mr=resolve_monster_turn(e,[])
                monster_results.append(mr)
                if not mr.get("success"):
                    turn_errors.append({"stage":"monster_phase","result":mr})
                    break
            elif e.state.duel_turn == "player_side":
                if p.actions_used_this_round >= p.action_count:
                    passed=player_passes(e)
                    if not passed.get("success"):
                        turn_errors.append({"stage":"player_skip","result":passed})
                        break
                else:
                    acts=ai.take_turn()
                    all_player_actions.extend(acts)
                    if not acts and e.state.duel_turn == "player_side":
                        passed=player_passes(e)
                        if not passed.get("success"):
                            turn_errors.append({"stage":"player_skip_after_no_action","result":passed})
                            break
            else:
                turn_errors.append({"stage":"unknown_priority","duel_turn":e.state.duel_turn})
                break
        foe1=foe.current_hp
        end=e.execute_action("round_end",{}) if e.state.combat_subphase == "await_round_end" else {
            "success":False,"error":f"循环结束时子阶段为{e.state.combat_subphase}","turn_errors":turn_errors}
        power2=p.effective_attack_power()
        actions=all_player_actions
        monster={"success":bool(monster_results) and all(x.get("success") for x in monster_results),
                 "phases":monster_results,"turn_errors":turn_errors}
        return {
            "seed":seed,"extra_actions":extra_actions,
            "action_budget":1+extra_actions,
            "rate":f"1:{effect_rate}" if effect_rate else "no-cast",
            "effect_rate":effect_rate,"healing_requested":(cast.get("calculation") or {}).get("target_heal"),
            "healing_actual":hp1-hp0,"attack_power_before":power0,
            "attack_power_after_cast":power1,"attack_power_cost":power0-power1,
            "attack_count":hits,"player_damage":foe0-foe1,
            "player_actions":actions,"monster_phase":monster,
            "round_end":end,"attack_power_after_round":power2,
            "attack_power_restored":power2==power0,
        }
    finally:
        dr.BY_NAME["献锋"]=old
        DaoWenEngine.register_all()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--runs",type=int,default=8)
    ap.add_argument("--output-dir",default="artifacts/fengjian-rate")
    args=ap.parse_args()
    if args.runs<1: ap.error("--runs must be >=1")
    seeds=[10,6,7,11,13,17,19,23,29,31,37,41][:args.runs]
    samples=[]; issues=[]
    for seed in seeds:
        for extra_actions in (0,2):
          for rate in (0,1,4):
            try:
                s=one(seed,rate,extra_actions); samples.append(s)
                label=f"1:{rate}" if rate else "no-cast"
                print(f"[OK] seed={seed} actions={s['action_budget']} {label} "
                      f"heal={s['healing_actual']} power={s['attack_power_before']}->"
                      f"{s['attack_power_after_cast']} damage={s['player_damage']} "
                      f"restored={s['attack_power_restored']}")
                if not s["attack_power_restored"]:
                    issues.append({"seed":seed,"rate":f"1:{rate}","problem":"round-end attack power did not restore"})
                if not s["round_end"].get("success"):
                    issues.append({"seed":seed,"rate":f"1:{rate}","problem":"round_end failed","detail":s["round_end"]})
                # 基础预算仅1次时，施放献锋会合法地消耗唯一行动；此时没有后续攻击是预期结果。
                if s["player_damage"]<=0 and (rate==0 or extra_actions>0):
                    issues.append({"seed":seed,"rate":f"1:{rate}","action_budget":1+extra_actions,
                                   "problem":"TacticalAI made no observable attack damage"})
            except Exception as exc:
                issue={"seed":seed,"rate":f"1:{rate}","problem":f"{type(exc).__name__}: {exc}"}
                issues.append(issue); print("[ERROR]",issue)
    paired={}
    for s in samples:
        paired.setdefault((s["seed"],s["extra_actions"]),{})[s["effect_rate"]]=s
    pairs=[]
    for (seed,extra_actions),arms in sorted(paired.items()):
        if all(rate in arms for rate in (0,1,4)):
            control,a,b=arms[0],arms[1],arms[4]
            pairs.append({"seed":seed,"action_budget":1+extra_actions,
                          "extra_healing_1_to_4":b["healing_actual"]-a["healing_actual"],
                          "attack_power_cost_1_to_1":a["attack_power_cost"],
                          "attack_power_cost_1_to_4":b["attack_power_cost"],
                          "damage_no_cast":control["player_damage"],
                          "damage_1_to_1":a["player_damage"],
                          "damage_1_to_4":b["player_damage"],
                          "damage_loss_1_to_1":control["player_damage"]-a["player_damage"],
                          "damage_loss_1_to_4":control["player_damage"]-b["player_damage"],
                          "damage_delta_1_to_4_vs_1_to_1":b["player_damage"]-a["player_damage"]})
    summary={"samples":len(samples),"planned":args.runs*3*2,"paired_seeds":len(pairs),
             "pairs":pairs,"issues":issues,
             "all_attack_power_restored":bool(samples) and all(s["attack_power_restored"] for s in samples),
             "all_round_ends_succeeded":bool(samples) and all(s["round_end"].get("success") for s in samples)}
    out=Path(args.output_dir); out.mkdir(parents=True,exist_ok=True)
    payload={"experiment":"献锋 1:1 vs 1:4","formal_source_unchanged":True,
             "override":"only BY_NAME['献锋'].effect_rate; cost rate stays 1",
             "engine":"GameEngine.execute_action + TacticalAI + resolve_monster_turn",
             "samples":samples,"summary":summary}
    (out/"results.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=repr),encoding="utf-8")
    (out/"issues.json").write_text(json.dumps(issues,ensure_ascii=False,indent=2),encoding="utf-8")
    lines=["# 献锋 1:1 / 1:4 / 不施放 对照实验报告","",
           f"- 样本：{len(samples)}/{args.runs*3*2}；行动预算分层配对数：{len(pairs)}；问题：{len(issues)}",
           f"- 所有样本回终恢复攻击力：{summary['all_attack_power_restored']}",
           f"- 所有样本回终成功：{summary['all_round_ends_succeeded']}","",
           "|种子|行动预算|不施放伤害|1:1伤害|1:4伤害|1:1伤害损失|1:4伤害损失|1:4额外治疗|",
           "|---:|---:|---:|---:|---:|---:|---:|---:|"]
    lines += [f"|{p['seed']}|{p['action_budget']}|{p['damage_no_cast']}|{p['damage_1_to_1']}|{p['damage_1_to_4']}|"
              f"{p['damage_loss_1_to_1']}|{p['damage_loss_1_to_4']}|{p['extra_healing_1_to_4']}|" for p in pairs]
    lines += ["","## 问题记录"]+[f"- {json.dumps(i,ensure_ascii=False)}" for i in issues]
    (out/"report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(f"Samples={len(samples)} issues={len(issues)} artifacts={out}")
    return 0 if samples else 1

if __name__=="__main__": raise SystemExit(main())
