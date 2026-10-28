"""运行中断点与资源不变量：有限行为探针不冒充无限运行穷举证明。"""
from engine.daowen import DaoWenEngine
from engine.models import Spell,Relic,StatusEffect
from engine.spell_execution import StepStatus,InterruptReason
from engine.spell_dsl import ActionStep
from tests.test_daowen_wiring import _engine,_give,_monster


def setup(suffix):
    e=_engine('running_counter_'+suffix);p=e.state.player
    _give(p,'杀伐');_give(p,'透支');_give(p,'不死')
    a=_monster(e,'反制者',hp=10000)
    e.execute_action('round_start',{})
    e.state.relics=[Relic('不朽之躯','')]
    p.current_mana=5
    return e,p,a


def paid_step(e,p,name,target,extra=None):
    refs=e.combat._combat_entity_refs()
    ref=next((r for r,t in refs.items() if t is target),'player:0' if target is p else 'enemy:0')
    return e.combat._execute_single_daowen_step(definition=None,
        step=ActionStep(daowen=name,target='any'),
        entry={'x':1,'target_ref':ref,'dodge':False,**(extra or {})},
        caster=p,attacker=None,refs=refs,policy=e.combat.CAST_STEP_POLICY)


def seal_reaction(e,enemy):
    _give(enemy,'封印')
    enemy.spells.append(Spell(name='运行中封印',required_daowen=['封印'],
        trigger_condition='目标发动道纹前',effect_flow='发动封印X于目标'))
    expected=e.combat.prepare_daowen_trigger_spells(e.state.player)
    assert 'enemy:0' in expected,expected
    return {'enemy:0':{'运行中封印':{'use':True,'steps':[{'x':1,'target_ref':'player:0','dodge':False}]}}}


def test_already_producing_loop_can_be_sealed_at_a_later_step_before_cost_output():
    e,p,a=setup('reaction')
    first=paid_step(e,p,'杀伐',a)
    assert first.status==StepStatus.COMPLETED and a.current_hp==9999
    choices=seal_reaction(e,a)
    mana=p.current_mana
    second=paid_step(e,p,'杀伐',a,{'trigger_spell_choices':choices})
    assert second.reason==InterruptReason.CASTER_UNAVAILABLE,second.to_log()
    assert p.is_alive and e.state.is_sealed_away(p)
    assert p.current_mana==mana and a.current_hp==9999
    # 用旧ref也不能令暂离者偷偷继续下一步。
    third=paid_step(e,p,'杀伐',a)
    assert third.reason==InterruptReason.CASTER_UNAVAILABLE
    assert a.current_hp==9999


def test_undying_floor_does_not_make_seal_reaction_fail():
    e,p,a=setup('undying')
    e.combat.apply_daowen_effect('不死',DaoWenEngine.resolve('不死',1,target=p),p,p)
    choices=seal_reaction(e,a)
    result=paid_step(e,p,'杀伐',a,{'trigger_spell_choices':choices})
    assert result.reason==InterruptReason.CASTER_UNAVAILABLE and p.has_status('不死')


def test_relic_supply_can_be_removed_after_prefix_and_before_later_cost():
    e,p,a=setup('relic');_give(a,'豪夺');a.current_mana=a.mana_limit=100
    assert paid_step(e,p,'透支',p,{'cost_channel':'衰老'}).status==StepStatus.COMPLETED
    assert paid_step(e,p,'杀伐',a).status==StepStatus.COMPLETED
    blood=p.blood_limit
    # 豪夺是实际遗物转移执行器，不是测试直接把供能遗物pop掉。
    result=e.combat.apply_daowen_effect('豪夺',DaoWenEngine.resolve('豪夺',1,target=p),a,p)
    assert not e.state.side_has(p,'不朽之躯'),result
    assert paid_step(e,p,'透支',p,{'cost_channel':'衰老'}).status==StepStatus.COMPLETED
    assert p.blood_limit==blood-2


def test_bizhong_can_close_reaction_window_not_a_proof_of_universal_no_counter():
    e,p,a=setup('locked');seal_reaction(e,a)
    # 由真实必中效果建立回合外封锁，不靠猜旧状态名。
    e.combat.apply_daowen_effect('必中',DaoWenEngine.resolve('必中',1,target=a),p,a)
    assert e.combat.prepare_daowen_trigger_spells(p)=={}


def test_instant_steps_do_not_silently_discard_selected_supply_channel():
    e,p,a=setup('channel');hp=p.current_hp;blood=p.blood_limit
    execution=e.combat.build_instant_execution(p,'发动透支X于自身→发动杀伐X于目标',a,
        [{'x':1,'cost_channel':'衰老'},{'x':1,'dodge':False}],e.combat._combat_entity_refs())
    execution.run_all()
    assert len(execution.results)==2 and all(r.status==StepStatus.COMPLETED for r in execution.results)
    assert p.current_hp==hp and p.blood_limit==blood and a.current_hp==9999
