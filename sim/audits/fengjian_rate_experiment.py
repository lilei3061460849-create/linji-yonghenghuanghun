#!/usr/bin/env python3
"""【献锋】1:1 / 1:4 真实引擎配对实验；只覆写进程内 effect_rate，不改正式事实源。"""
from __future__ import annotations
import argparse, json, os, tempfile
from dataclasses import replace
from pathlib import Path
from engine import derivatives as dr
from engine.api import GameEngine
from engine.ai_tactics import TacticalAI
from engine.daowen import DaoWenEngine
from engine.models import DaoWen, DaoWenInstance, Entity
from sim.alt_path_test import resolve_monster_turn
from tests.setup_support import begin_battle, begin_round, finish_initial_daowen, grant_extra_actions, hand_priority_to_player

def one(seed: int, effect_rate: int) -> dict:
    old = dr.BY_NAME["献锋"]
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
            if action=="setup_choose_region":
                choices=(result.get("result") or {}).get("relic_choices") or []
                if choices:
                    picked=e.execute_action("choose_discovered_relic",{"relic_name":choices[0]})
                    if not picked.get("success"): raise RuntimeError(f"relic: {picked}")
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
        grant_extra_actions(p,2)

        ai=TacticalAI(e,verbose=True); ai.new_round()
        hp0=p.current_hp; power0=p.effective_attack_power()
        hits=p.effective_attack_count(); foe0=foe.current_hp
        cast=e.execute_action("use_daowen",{
            "daowen_name":"献锋","x":1,"target_ref":"player:0",
            "dodge":False,"blood_shadow":False,"trigger_spell_choices":{}})
        if not cast.get("success"): raise RuntimeError(f"献锋发动失败: {cast}")
        ai.used["献锋"]=1
        hp1=p.current_hp; power1=p.effective_attack_power()
        actions=ai.take_turn()
        foe1=foe.current_hp
        monster=resolve_monster_turn(e,[])
        end=e.execute_action("round_end",{})
        power2=p.effective_attack_power()
        return {
            "seed":seed,"rate":f"1:{effect_rate}","effect_rate":effect_rate,
            "healing_requested":(cast.get("calculation") or {}).get("target_heal"),
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
        for rate in (1,4):
            try:
                s=one(seed,rate); samples.append(s)
                print(f"[OK] seed={seed} 1:{rate} heal={s['healing_actual']} "
                      f"power={s['attack_power_before']}->{s['attack_power_after_cast']} "
                      f"damage={s['player_damage']} restored={s['attack_power_restored']}")
                if not s["attack_power_restored"]:
                    issues.append({"seed":seed,"rate":f"1:{rate}","problem":"round-end attack power did not restore"})
                if not s["round_end"].get("success"):
                    issues.append({"seed":seed,"rate":f"1:{rate}","problem":"round_end failed","detail":s["round_end"]})
                if s["player_damage"]<=0:
                    issues.append({"seed":seed,"rate":f"1:{rate}","problem":"TacticalAI made no observable attack damage"})
            except Exception as exc:
                issue={"seed":seed,"rate":f"1:{rate}","problem":f"{type(exc).__name__}: {exc}"}
                issues.append(issue); print("[ERROR]",issue)
    paired={}
    for s in samples: paired.setdefault(s["seed"],{})[s["effect_rate"]]=s
    pairs=[]
    for seed,arms in sorted(paired.items()):
        if 1 in arms and 4 in arms:
            a,b=arms[1],arms[4]
            pairs.append({"seed":seed,"extra_healing":b["healing_actual"]-a["healing_actual"],
                          "extra_attack_power_cost":b["attack_power_cost"]-a["attack_power_cost"],
                          "actual_damage_delta":b["player_damage"]-a["player_damage"]})
    summary={"samples":len(samples),"planned":args.runs*2,"paired_seeds":len(pairs),
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
    lines=["# 献锋 1:1 vs 1:4 实验报告","",
           f"- 样本：{len(samples)}/{args.runs*2}；配对种子：{len(pairs)}；问题：{len(issues)}",
           f"- 所有样本回终恢复攻击力：{summary['all_attack_power_restored']}",
           f"- 所有样本回终成功：{summary['all_round_ends_succeeded']}","",
           "|种子|1:4额外治疗|额外攻击力损失|实际伤害差|","|---:|---:|---:|---:|"]
    lines += [f"|{p['seed']}|{p['extra_healing']}|{p['extra_attack_power_cost']}|{p['actual_damage_delta']}|" for p in pairs]
    lines += ["","## 问题记录"]+[f"- {json.dumps(i,ensure_ascii=False)}" for i in issues]
    (out/"report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(f"Samples={len(samples)} issues={len(issues)} artifacts={out}")
    return 0 if samples else 1

if __name__=="__main__": raise SystemExit(main())
