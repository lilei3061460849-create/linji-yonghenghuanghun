"""篡改只接管目标选择，不接管X/费用；所有执行入口共同计数。"""
import pytest
from engine.daowen import DaoWenEngine
from engine.models import StatusEffect
from tests.test_daowen_wiring import _engine, _give, _monster


def setup(suffix):
    e=_engine('cuangai_'+suffix);p=e.state.player
    _give(p,'篡改');_give(p,'杀伐')
    a=_monster(e,'原靶',hp=100);b=_monster(e,'改靶',hp=100)
    e.execute_action('round_start',{})
    p.current_hp=p.blood_limit=100
    return e,p,a,b


def apply_rule(e,owner,target,x=2):
    return e.combat.apply_daowen_effect('篡改',DaoWenEngine.resolve('篡改',x,target=target),owner,target)


def command(owner,ref):
    return {'controller_id':owner.runtime_id,'target_ref':ref}


def use(e,a,**kw):
    return e.execute_action('use_daowen',{'daowen_name':'杀伐','x':2,'target':a.name,
        'dodge':False,'blood_shadow':False,**kw})


def test_cast_pays_three_x_bleed_and_creates_count_not_duration():
    e,p,a,b=setup('cost');before=p.current_mana
    result=e.execute_action('use_daowen',{'daowen_name':'篡改','x':2,'target':a.name,
        'dodge':False,'blood_shadow':False})
    assert result['success'],result
    assert p.current_hp==94 and p.current_mana==before
    rule=a.get_status_effects('篡改')[0]
    assert rule.controller_runtime_id==p.runtime_id and rule.value==2
    a.tick_status_effects()
    assert rule.value==2 and rule.remaining_rounds==-1


def test_player_changes_only_target_keeps_x_and_own_cost_then_rule_ends():
    e,p,a,b=setup('player');apply_rule(e,a,p,1)
    before=p.current_mana
    result=use(e,a,tamper=command(a,'enemy:1'))
    assert result['success'],result
    assert a.current_hp==97  # a支付篡改3，不是被杀伐打中。
    assert b.current_hp==96 and p.current_mana==before-2
    assert not p.has_status('篡改')
    result=use(e,a)
    assert result['success'] and a.current_hp==93


@pytest.mark.parametrize('decision',[None,{}, {'controller_id':'wrong','target_ref':'enemy:1'},
    {'target_ref':'enemy:1'}, {'controller_id':'correct','target_ref':'bad'}])
def test_missing_wrong_or_illegal_decision_does_not_pay_or_use_charge(decision):
    e,p,a,b=setup('invalid');apply_rule(e,a,p,2)
    if decision and decision.get('controller_id')=='correct':decision=dict(decision,controller_id=a.runtime_id)
    before=p.current_mana,p.actions_used_this_round
    result=use(e,a,tamper=decision)
    assert not result['success'],result
    assert (p.current_mana,p.actions_used_this_round)==before
    assert p.get_status_value('篡改')==2 and b.current_hp==100


def test_zero_x_and_unaffordable_cast_do_not_consume_rule():
    e,p,a,b=setup('no_cast');apply_rule(e,a,p,2)
    assert use(e,a,x=0)['success']
    assert p.get_status_value('篡改')==2
    p.current_mana=0
    assert not use(e,a,tamper=command(a,'enemy:1'))['success']
    assert p.get_status_value('篡改')==2


def test_later_controller_overrides_larger_earlier_count():
    e,p,a,b=setup('overwrite')
    apply_rule(e,p,b,5);apply_rule(e,a,b,1)
    rules=b.get_status_effects('篡改')
    assert len(rules)==1 and rules[0].controller_runtime_id==a.runtime_id
    assert rules[0].value==1


def test_same_controller_recasts_follow_normal_additive_charge_rule():
    e,p,a,b=setup('merge');apply_rule(e,p,b,2);apply_rule(e,p,b,1)
    assert len(b.get_status_effects('篡改'))==1 and b.get_status_value('篡改')==3


def test_spell_step_uses_same_target_control():
    e,p,a,b=setup('spell');apply_rule(e,a,p,1)
    execution=e.combat.build_instant_execution(p,'发动杀伐X于目标',a,
        [{'x':2,'dodge':False,'tamper':command(a,'enemy:1')}], e.combat._combat_entity_refs())
    execution.run_all()
    assert b.current_hp==96 and not p.has_status('篡改')


def test_monster_executes_controller_target_and_pays_its_own_cost():
    e,p,a,b=setup('monster');_give(a,'杀伐');a.dao_wen['杀伐'].x_value=2
    apply_rule(e,p,a,1);mana=a.current_mana
    result=e.combat._resolve_monster_daowen_choice(a,
        {'name':'杀伐','target_ref':'player:0','dodge':False,'tamper':command(p,'enemy:1')},
        e.combat._combat_entity_refs(),set(),{})
    assert result['target']==b.name and b.current_hp==96
    assert a.current_mana==mana-2 and not a.has_status('篡改')


def test_self_only_daowen_cannot_be_redirected_outside_its_scope():
    e,p,a,b=setup('self');_give(p,'出土');apply_rule(e,a,p,1)
    result=e.execute_action('use_daowen',{'daowen_name':'出土','x':1,
        'tamper':command(a,'enemy:1')})
    assert not result['success'] and p.get_status_value('篡改')==1


def test_control_identity_survives_full_snapshot_without_name_matching():
    e,p,a,b=setup('save');apply_rule(e,a,p,2)
    restored=e._deserialize_entity_full(e._serialize_entity_full(p))
    owner=e._deserialize_entity_full(e._serialize_entity_full(a))
    assert restored.runtime_id==p.runtime_id and owner.runtime_id==a.runtime_id
    assert restored.get_status_effects('篡改')[0].controller_runtime_id==owner.runtime_id


def test_truth_special_api_also_obeys_target_control_and_consumes_one():
    e,p,a,b=setup('truth');_give(p,'真理');apply_rule(e,a,p,1)
    bad=e.execute_action('use_daowen',{'daowen_name':'真理','x':1,'target_ref':'enemy:0','statement':'一句陈述。'})
    assert not bad['success'] and p.get_status_value('篡改')==1
    result=e.execute_action('use_daowen',{'daowen_name':'真理','x':1,'target_ref':'enemy:0',
        'statement':'一句陈述。','tamper':command(a,'enemy:1')})
    assert result['success'],result
    assert e._pending_interrupts[0].context['target']==b.name
    assert not p.has_status('篡改')
