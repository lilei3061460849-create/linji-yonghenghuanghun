import pytest
from engine.daowen import DaoWenEngine
from tests.test_daowen_wiring import _engine,_give,_monster


def setup(suffix):
    e=_engine('cuihua_'+suffix);p=e.state.player
    e.state.relics=[]  # 隔离随机遗物；本套件不测试其被动效果。
    _give(p,'催化');_give(p,'不死');_give(p,'全速')
    a=_monster(e,'目标',hp=100)
    e.execute_action('round_start',{})
    p.dao_wen['不死'].cooldown_remaining=5
    p.dao_wen['全速'].cooldown_remaining=7
    return e,p,a


def call(e,steps,x=1,target_ref='player:0'):
    return e.execute_action('use_daowen',{'daowen_name':'催化','x':x,
        'target_ref':target_ref,'catalysis_steps':steps,'dodge':False,'blood_shadow':False})


def step(name='不死',x=2,ref='player:0',**kw):
    return {'name':name,'x':x,'target_ref':ref,**kw}


def test_call_cooling_instance_once_without_resetting_its_original_cooldown():
    e,p,a=setup('once');before=p.current_mana,p.current_hp,p.actions_used_this_round
    result=call(e,[step()])
    assert result['success'],result
    assert p.get_status_effects('不死')[0].remaining_rounds==2
    assert p.dao_wen['不死'].cooldown_remaining==5
    assert p.dao_wen['催化'].cooldown_remaining==2
    assert (p.current_mana,p.current_hp)==before[:2]
    assert p.actions_used_this_round==before[2]+1
    assert result['execution']['effects'][0]['daowen']=='不死'


def test_selected_source_cannot_then_be_used_normally_while_still_cooling():
    e,p,a=setup('normal');assert call(e,[step()])['success']
    result=e.execute_action('use_daowen',{'daowen_name':'不死','x':2})
    assert not result['success'] and p.dao_wen['不死'].cooldown_remaining==5


def test_two_distinct_types_each_fire_once_with_their_own_x():
    e,p,a=setup('two');speed=p.current_speed
    result=call(e,[step(x=3),step('全速',x=1)],x=2)
    assert result['success'],result
    assert p.get_status_effects('不死')[0].remaining_rounds==3
    assert p.has_status('全速') and p.dao_wen['催化'].cooldown_remaining==4
    assert [part['daowen'] for part in result['execution']['effects']]==['不死','全速']


@pytest.mark.parametrize('mode',['missing','duplicate','not_cooling','frozen','wrong_target','x_zero','wrong_count'])
def test_invalid_selection_does_not_pay_catalyst_cooldown_or_action(mode):
    e,p,a=setup('invalid');entries=[step()]
    if mode=='missing':entries=None
    if mode=='duplicate':entries=[step(),step()]
    if mode=='not_cooling':p.dao_wen['不死'].cooldown_remaining=0
    if mode=='frozen':p.dao_wen['不死'].is_frozen=True
    if mode=='wrong_target':entries=[step(ref='bad')]
    if mode=='x_zero':entries=[step(x=0)]
    if mode=='wrong_count':entries=[]
    before=p.actions_used_this_round
    result=call(e,entries)
    assert not result['success'],result
    assert p.actions_used_this_round==before and p.dao_wen['催化'].cooldown_remaining==0
    assert not p.has_status('不死')


def test_catalyst_in_spell_uses_identical_executor():
    e,p,a=setup('spell')
    execution=e.combat.build_instant_execution(p,'发动催化X于目标',p,
        [{'x':1,'catalysis_steps':[step()]}],e.combat._combat_entity_refs())
    execution.run_all()
    assert p.has_status('不死') and p.dao_wen['不死'].cooldown_remaining==5
    assert p.dao_wen['催化'].cooldown_remaining==2


def test_catalysed_owner_not_the_catalyst_caster_executes():
    e,p,a=setup('owner');_give(a,'不死');a.dao_wen['不死'].cooldown_remaining=4
    result=call(e,[step(x=1,ref='enemy:0')],target_ref='enemy:0')
    assert result['success'],result
    assert a.has_status('不死') and not p.has_status('不死')
    assert a.dao_wen['不死'].cooldown_remaining==4


def test_wave_splits_number_of_activations_instead_of_duplicating():
    e,p,a=setup('wave');b=_monster(e,'波及靶',hp=100)
    for m in (a,b):
        _give(m,'不死');m.dao_wen['不死'].cooldown_remaining=3
    _give(p,'波及')
    e.combat.apply_daowen_effect('波及',{'x':1,'mark_targets':1},p,b)
    result=call(e,{'enemy:0':[step(x=1,ref='enemy:0')],
        'enemy:1':[step(x=1,ref='enemy:1')]},target_ref='enemy:0')
    assert result['success'],result
    assert sum(m.has_status('不死') for m in (a,b))==1


def test_replaced_source_does_not_get_old_instances_permission():
    e,p,a=setup('identity');calc=DaoWenEngine.resolve('催化',1,target=p)
    e.combat.prepare_catalysis(calc,p,p,{'catalysis_steps':[step()]},e.combat._combat_entity_refs())
    old=p.dao_wen.pop('不死');_give(p,'不死');p.dao_wen['不死'].cooldown_remaining=9
    result=e.combat.apply_daowen_effect('催化',calc,p,p)
    assert not p.has_status('不死') and p.dao_wen['不死'].cooldown_remaining==9
    assert result['effects'][0]['type']=='catalysis_interrupted'


def test_decay_hourglass_reduces_number_of_called_types_not_only_label():
    from engine.models import Relic
    e,p,a=setup('decay');e.state.relics.append(Relic(name='衰变沙漏',effect=''))
    result=call(e,[step()])
    assert result['success'],result
    assert not p.has_status('不死') and p.dao_wen['催化'].cooldown_remaining==2


def test_truth_preserves_dm_statement_path_and_original_cooldown():
    e,p,a=setup('truth');_give(p,'真理');p.dao_wen['真理'].cooldown_remaining=6
    result=call(e,[step('真理',x=1,ref='enemy:0',statement='你是目标。')])
    assert result['success'],result
    assert len(e._pending_interrupts)==1
    interrupt=e._pending_interrupts[0]
    assert interrupt.context['ability']=='真理' and interrupt.context['sentences']==['你是目标']
    assert p.dao_wen['真理'].cooldown_remaining==6
    assert not a.has_status('真理')


def test_truth_bad_statement_rejected_before_catalyst_is_paid():
    e,p,a=setup('truth_bad');_give(p,'真理');p.dao_wen['真理'].cooldown_remaining=6
    result=call(e,[step('真理',x=2,ref='enemy:0',statement='只有一句。')])
    assert not result['success'] and not e._pending_interrupts
    assert p.dao_wen['催化'].cooldown_remaining==0


def test_truth_catalysis_preview_does_not_pollute_interrupt_queue_or_source_instance():
    from engine.ai_preview import ActionPreview
    from engine.pollution_guard import snapshot_runtime_state,assert_runtime_unchanged
    e,p,a=setup('truth_preview');_give(p,'真理');p.dao_wen['真理'].cooldown_remaining=6
    before=snapshot_runtime_state(e)
    ActionPreview(e).preview('use_daowen',{'daowen_name':'催化','x':1,'target_ref':'player:0',
        'dodge':False,'catalysis_steps':[step('真理',x=1,ref='enemy:0',statement='一句陈述。')]})
    assert_runtime_unchanged(before,snapshot_runtime_state(e),context='催化真理预演')
    assert not e._pending_interrupts and not e.state.pending_daowen_interrupts


def test_public_action_schema_lists_required_catalysis_candidates_without_mutating_instances():
    e,p,a=setup('schema')
    before={name:dict(inst.__dict__) for name,inst in p.dao_wen.items()}
    actions=e.get_available_actions()['actions']
    action=next(row for row in actions if row['action_type']=='use_daowen'
        and row['params_schema'].get('daowen_name')=='催化')
    schema=action['params_schema']['catalysis_steps']
    assert schema['required'] and {row['name'] for row in schema['holders']['player:0']}=={'不死','全速'}
    assert {name:dict(inst.__dict__) for name,inst in p.dao_wen.items()}==before


def test_monster_prepare_exposes_same_catalysis_and_control_submission_contract():
    from tests.test_cuangai_runtime import apply_rule
    e,p,a=setup('monster_schema');_give(a,'催化');a.dao_wen['催化'].x_value=1
    _give(a,'不死');a.dao_wen['不死'].cooldown_remaining=4
    apply_rule(e,p,a,1)
    prepared=e.combat.prepare_monster_phase()
    option=next(row for row in prepared['actors'][0]['daowen_options'] if row['name']=='催化')
    schema=option['extra_choice_schema']
    assert schema['tamper']['controller_id']==p.runtime_id
    assert schema['catalysis_steps']['required']


def test_nested_extra_choices_are_preserved_for_catalysed_step():
    e,p,a=setup('nested_extra');_give(p,'透支');p.dao_wen['透支'].cooldown_remaining=3
    from engine.models import Relic
    e.state.relics.append(Relic('不朽之躯',''))
    hp=p.current_hp
    result=call(e,[step('透支',x=1,extra={'cost_channel':'血限'})])
    assert result['success'],result
    assert p.current_hp==hp  # 若丢弃extra而退回生命，会扣4生命。
