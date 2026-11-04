
# --- 2026-11-04：脚本放进子目录后，不再靠"往上数几层"猜仓库根 ---
# 向上找到同时含 engine/ 与 sim/ 的目录就是仓库根；再附带 sim/ 让老式裸模块名
# import（如 `import balance_sim`）继续可用。搬动本文件不需要再改这几行。
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.abspath(__file__))
while _ROOT != _os.path.dirname(_ROOT) and not _os.path.isdir(_os.path.join(_ROOT, "engine")):
    _ROOT = _os.path.dirname(_ROOT)
for _p in (_ROOT, _os.path.join(_ROOT, "sim"), _os.path.join(_ROOT, "tests")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import sys
import tests.test_spell_loop_mana_gain as M
from engine.models import DaoWen, DaoWenInstance

def run(name, req, flow, rounds, x=3, mana=30, hp=40, trig="失去生命后"):
    sp={"name":name,"required_daowen":req,"trigger_condition":trig,"effect_flow":flow}
    e=M._engine_with_loop_spell(f"bt_{name}",hp=hp,mana=mana,spells=(sp,))
    p=e.state.player
    for d in req:
        if d not in p.dao_wen:
            p.dao_wen[d]=DaoWenInstance(DaoWen(name=d,formula="",cost_type="",cost_formula="X",effect_formula=""))
    foe=e.state.enemies[0]; hp0=foe.current_hp
    combat=e.combat
    prep=combat.prepare_monster_phase(); actor=prep["actors"][0]
    tgt=actor["attack_target_options"][0]; opts=tgt["spell_options"]
    nstep=len(combat._predict_flat_steps(combat._parse_custom_spell([s for s in p.spells if s.name==name][0])["steps"],p,foe))
    steps=[]
    for st in combat._predict_flat_steps(combat._parse_custom_spell([s for s in p.spells if s.name==name][0])["steps"],p,foe):
        ref="player:0" if st.target=="self" else "enemy:0"
        d={"x":x,"target_ref":ref}
        if ref!="player:0": d["dodge"]=False
        steps.append(d)
    after={s["spell_name"]:{"use":False} for s in opts["after"]}
    after[name]={"use":True,"steps":steps,"max_iterations":rounds}
    sc={"before":{s["spell_name"]:{"use":False} for s in opts["before"]},"after":after,
        "damage_after":{s["spell_name"]:{"use":False} for s in opts["damage_after"]},
        "life_before":{s["spell_name"]:{"use":False} for s in opts["life_before"]}}
    ch=[{"actor_ref":actor["actor_ref"],"daowen":None,
      "attack_actions":[{"hits":[{"target_ref":tgt["ref"],"dodge":False,"blood_shadow":False,"spell_choices":sc}]}]}]
    try:
        combat.resolve_monster_phase(ch,prepared=prep)
        print(f"{name:10s} {rounds:2d}轮上限 -> 我方生命{p.current_hp:4d} 法力{p.current_mana:3d} 累计回复{p.total_healed:4d} | 对怪伤害{hp0-foe.current_hp:5d} | 存活{p.is_alive}")
    except Exception as ex:
        print(f"{name:10s} {rounds:2d}轮上限 -> 提交被拒(契约错误): {ex}")

run("反击循环",["再生","透支","杀伐"],"发动再生X于自身→发动透支X于自身→发动杀伐X于攻击者→循环",1)
run("反击循环",["再生","透支","杀伐"],"发动再生X于自身→发动透支X于自身→发动杀伐X于攻击者→循环",5)
run("反击循环",["再生","透支","杀伐"],"发动再生X于自身→发动透支X于自身→发动杀伐X于攻击者→循环",10)
run("连击周天",["再生","透支","连击"],"发动再生X于自身→发动透支X于自身→发动连击X于攻击者→循环",10)

print("--- 反击循环 上限探查（法力30起步）---")
for c in (11,12,20):
    run("反击循环",["再生","透支","杀伐"],"发动再生X于自身→发动透支X于自身→发动杀伐X于攻击者→循环",c)
print("--- 法力起步敏感度（10轮）---")
for m in (3,9,30,60):
    run("反击循环",["再生","透支","杀伐"],"发动再生X于自身→发动透支X于自身→发动杀伐X于攻击者→循环",10,mana=m)