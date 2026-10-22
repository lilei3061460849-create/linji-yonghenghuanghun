import os,sys,json,traceback
# 2026-10-14：【搏命】删除后该格留成空占位符（镇尸先例），【超频】的【反转】
# 通向占位符=失败，原 'boming'/'huaisi' combo 已无可测量的共振获取路径，整支移除。
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.api import GameEngine
from engine.ai_tactics import TacticalAI
from sim.build_learner import _resolve_monster_turn, _resolve_pending_choices
from sim.optional_actions import battle_start_relic_choices, round_start_relic_choices

ATTRS=(7,8,10)

def pre(e, todo, blood=False, spell=False):
    actions=[]
    stalls=0
    while e.state.energy>0:
        p=e.state.player
        before=e.state.energy
        if todo:
            n=todo[0]
            r=e.execute_action('pre_battle_action',{'sub_action':'学习','sub':'daowen','tier':1,'name':n})
            if r.get('success'):
                todo.pop(0); actions.append('learn:'+n); stalls=0; continue
            err=str(r.get('error',''))
            if '已经掌握' in err:
                todo.pop(0); continue
        elif blood and not spell and '再生' in p.dao_wen and '透支' in p.dao_wen:
            definition={'name':'血炼周天','required_daowen':['再生','透支'],
                        'trigger_condition':'失去生命后',
                        'effect_flow':'发动再生X于自身→发动透支X于自身→循环'}
            r=e.execute_action('pre_battle_action',{'sub_action':'学习','sub':'custom_spell','spell':definition})
            if r.get('success') and r.get('completed') is False:
                r=e.execute_action('pre_battle_action',{'sub_action':'学习','sub':'custom_spell','spell':definition,'dm_approved':True})
            if r.get('success') and r.get('result',{}).get('learned')=='custom_spell':
                spell=True; actions.append('spell:血炼周天'); stalls=0; continue
        # valid action that always spends energy and does not need a parity allocation
        r=e.execute_action('pre_battle_action',{'sub_action':'修行','tier':1})
        if r.get('success'):
            actions.append('train'); stalls=0
        else:
            stalls+=1
        if e.state.energy>=before: stalls+=1
        if stalls>=5: return actions,spell,{'reason':'prelock','error':r.get('error'),'todo':todo}
    return actions,spell,None

def setup(seed,relic,region,rtype,combo):
    e=GameEngine(db_path=f'/tmp/target_{os.getpid()}_{seed}.db',rng_seed=seed)
    s=e.execute_action('setup_attributes',{'name':'新角色探针','blood_points':ATTRS[0],'speed_points':ATTRS[1],'mana_points':ATTRS[2]})
    if not s.get('success'): return None,{'reason':'setup','error':s.get('error')}
    rc=list(e.state.pending_relic_choices)
    if relic not in rc:return None,{'reason':'relic_absent','relic_choices':rc}
    ch=e.execute_action('choose_discovered_relic',{'relic_name':relic})
    dc=list(e.state.pending_initial_daowen_choices)
    # choose defensive/damage starter; actual initial candidate from engine
    pref=(['封印','庇护','再生','杀伐','连击'] if combo in ('hyper',) else ['庇护','再生','杀伐','连击'])
    initial=next((x for x in pref if x in dc),dc[0])
    e.execute_action('setup_choose_initial_daowen',{'daowen_name':initial})
    e.execute_action('setup_choose_resonance',{'resonance_type':rtype})
    e.execute_action('setup_choose_region',{'region':region})
    return e,{'relic_choices':rc,'relic':relic,'initial_choices':dc,'initial':initial,'region':region,'rtype':rtype}

def acquire(e, target, source, rtype):
    if target in e.state.player.dao_wen:return {'success':True,'already':True}
    for i,m in enumerate(e.state.enemies):
        if m.is_alive and source in m.dao_wen:
            r=e.execute_action('use_resonance',{'source_daowen':source,'resonance_type':rtype,'target_ref':f'enemy:{i}'})
            if r.get('success'):
                return {'success':True,'action':r.get('action'),'target':target,'source':source,'monster':m.name,'granted':r.get('granted_daowen')}
            return {'success':False,'error':r.get('error'),'source':source,'monster':m.name}
    return {'success':False,'reason':'source_absent','source':source}

def run(seed,relic,region,combo,attrs=ATTRS):
    global ATTRS; old=ATTRS; ATTRS=attrs
    try:
      if combo=='blood': target=None; source=None;rtype='反转'; todo=['庇护','再生','透支']; blood=True
      elif combo=='hyper': target='超频';source='畸变';rtype='曲解';todo=['庇护','再生','超频'];blood=False
      e,rec=setup(seed,relic,region,rtype,combo)
      if e is None:return rec
      rec['battles']=[];spell=False;cleared=0;acq=None
      for b in range(1,8):
        acts,spell,err=pre(e,todo,blood,spell); rec.setdefault('pre',[]).append(acts)
        if err:return dict(rec,ok=False,cleared=cleared,died_at=b,reason=err['reason'],detail=err)
        _resolve_pending_choices(e)
        bs=e.execute_action('battle_start',{'relic_choices':battle_start_relic_choices(e)})
        if not bs.get('success'):return dict(rec,ok=False,cleared=cleared,died_at=b,reason='battle_start',detail=bs)
        enemies0=[{'name':m.name,'dw':sorted(m.dao_wen)} for m in e.state.enemies if m.is_alive]
        binfo={'battle':b,'enemies':enemies0,'rounds':0}
        ai=TacticalAI(e)
        for rnd in range(1,31):
            binfo['rounds']=rnd
            if not e.state.player or not e.state.player.is_alive:break
            alive=[m for m in e.state.enemies if m.is_alive]
            if not alive and not getattr(e.state,'monster_reinforcements',[]):break
            rs=e.execute_action('round_start',{'relic_choices':round_start_relic_choices(e)})
            if not rs.get('success'):return dict(rec,ok=False,cleared=cleared,died_at=b,reason='round_start',detail=rs)
            if combo!='blood' and acq is None:
                _a=acquire(e,target,source,rtype);rec['acquisition']=_a
                if _a.get('success'): acq=_a
            ai.new_round(); ai.take_turn()
            if not e.state.player or not e.state.player.is_alive:break
            if not any(m.is_alive for m in e.state.enemies) and not getattr(e.state,'monster_reinforcements',[]):break
            ap=e.execute_action('resolve_ally_phases',{})
            if not ap.get('success'):return dict(rec,ok=False,cleared=cleared,died_at=b,reason='ally',detail=ap)
            if not any(m.is_alive for m in e.state.enemies) and not getattr(e.state,'monster_reinforcements',[]):break
            mp=_resolve_monster_turn(e)
            if not mp.get('success'):return dict(rec,ok=False,cleared=cleared,died_at=b,reason='monster',detail=mp)
            if mp.get('result',{}).get('player_dead'):break
            re=e.execute_action('round_end',{})
            if not re.get('success'):
                _resolve_pending_choices(e);re=e.execute_action('round_end',{})
            if not re.get('success'):return dict(rec,ok=False,cleared=cleared,died_at=b,reason='round_end',detail=re)
        if not e.state.player or not e.state.player.is_alive:
            rec['battles'].append(dict(binfo,result='dead',hp=0));return dict(rec,ok=False,cleared=cleared,died_at=b,reason='dead',killer=[m.name for m in e.state.enemies if m.is_alive])
        if any(m.is_alive for m in e.state.enemies):
            rec['battles'].append(dict(binfo,result='stuck',hp=e.state.player.current_hp));return dict(rec,ok=False,cleared=cleared,died_at=b,reason='stuck')
        _resolve_pending_choices(e)
        be=e.execute_action('battle_end',{})
        if not be.get('success'):
            _resolve_pending_choices(e);be=e.execute_action('battle_end',{})
        if not be.get('success'):return dict(rec,ok=False,cleared=cleared,died_at=b,reason='battle_end',detail=be)
        cleared+=1;rec['battles'].append(dict(binfo,result='cleared',hp=e.state.player.current_hp, mana=e.state.player.current_mana,dw=sorted(e.state.player.dao_wen)))
      return dict(rec,ok=cleared==7,cleared=cleared,died_at=None if cleared==7 else cleared+1,reason=None if cleared==7 else 'incomplete')
    except Exception as ex:
      return {'ok':False,'reason':'exception','error':repr(ex),'traceback':traceback.format_exc(),'cleared':cleared if 'cleared' in locals() else 0}
    finally:ATTRS=old

if __name__=='__main__':
 import argparse
 ap=argparse.ArgumentParser();ap.add_argument('--seed',type=int,required=True);ap.add_argument('--relic',required=True);ap.add_argument('--region',default='扭曲都市');ap.add_argument('--combo',choices=['blood','hyper'],default='blood');ap.add_argument('--attrs',default='7,8,10');a=ap.parse_args();
 print(json.dumps(run(a.seed,a.relic,a.region,a.combo,tuple(map(int,a.attrs.split(',')))),ensure_ascii=False))
