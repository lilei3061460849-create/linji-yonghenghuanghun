"""不死：真实冷却/生命下限；不是回复或特殊事件免疫。"""
from engine.models import StatusEffect
from tests.test_daowen_wiring import _engine, _give, _monster


def setup(suffix):
    e = _engine('busi_' + suffix);p = e.state.player
    _give(p, '不死');t = _monster(e, '不死靶', hp=20)
    e.execute_action('round_start', {})
    return e, p, t


def cast(e, t, x=2):
    return e.execute_action('use_daowen', {'daowen_name': '不死', 'x': x,
        'target': t.name, 'dodge': False, 'blood_shadow': False})


def test_actual_cooldown_and_no_mana_cost():
    e,p,t=setup('cost');mana=p.current_mana
    r=cast(e,t)
    assert r['success'],r
    assert p.current_mana==mana
    assert p.dao_wen['不死'].cooldown_remaining==2
    assert t.get_status_effects('不死')[0].remaining_rounds==2
    assert not cast(e,t)['success']


def test_damage_stops_at_one_and_does_not_heal_or_mark_dead():
    e,p,t=setup('damage');assert cast(e,t)['success']
    healed=t.total_healed
    result=t.take_damage(100)
    assert t.current_hp==1 and t.is_alive
    assert result['actual_damage']==19 and result['actual_life_loss']==19
    assert t.total_healed==healed
    assert t.take_damage(20)['actual_damage']==0
    assert not e.combat._check_hp_zero_death(t)


def test_raw_loss_and_direct_writes_obey_same_floor():
    e,p,t=setup('raw');assert cast(e,t)['success']
    r=e.combat._raw_hp_loss(t,100)
    assert r['lost']==19 and not r['died'] and t.current_hp==1
    t.current_hp=-10
    assert t.current_hp==1 and t.is_alive


def test_bleed_payment_uses_actual_life_loss_not_nominal_damage():
    e,p,t=setup('bleed');assert cast(e,t)['success']
    r=e.combat.pay_numeric_cost(t,'流血',20)
    assert t.current_hp==1 and t.is_alive
    assert r['actual_paid']==19
    r=e.combat.pay_numeric_cost(t,'流血',1)
    assert r['actual_paid']==0 and t.current_hp==1


def test_expiry_removes_floor_without_restoring_or_healing():
    e,p,t=setup('expiry');assert cast(e,t)['success']
    t.take_damage(100)
    t.tick_status_effects();t.tick_status_effects()
    assert not t.has_status('不死') and t.current_hp==1
    t.take_damage(1)
    assert not t.is_alive and t.current_hp==0


def test_floor_does_not_create_life_or_resurrect_dead_entity():
    e,p,t=setup('dead')
    t.current_hp=0;t.is_alive=False
    t.add_status(StatusEffect('不死',3))
    t.current_hp=0
    assert t.current_hp==0 and not t.is_alive


def test_cancer_departure_is_not_prevented():
    e,p,t=setup('cancer');assert cast(e,t)['success']
    t.total_healed=2*t.blood_limit
    assert e.combat.check_cancer(t) is not None
    assert t.is_departed and not t.is_alive


def test_instant_spell_uses_the_same_floor_and_cooldown():
    e,p,t=setup('spell');before=p.current_mana
    execution=e.combat.build_instant_execution(p,'发动不死X于目标',t,
        [{'x':2,'dodge':False}],e.combat._combat_entity_refs())
    execution.run_all()
    assert p.current_mana==before and p.dao_wen['不死'].cooldown_remaining==2
    t.take_damage(100)
    assert t.current_hp==1


def test_decay_hourglass_zero_duration_does_not_create_permanent_immortality():
    from engine.models import Relic
    e,p,t=setup('zero_duration');e.state.relics.append(Relic('衰变沙漏',''))
    result=cast(e,t,x=1)
    assert result['success'],result
    assert p.dao_wen['不死'].cooldown_remaining==1  # 原声明代价照付。
    assert not t.has_status('不死')  # 1-1=0不是持续∞。
    t.take_damage(100)
    assert not t.is_alive


def test_spell_zero_duration_keeps_declared_cooldown_without_floor():
    from engine.models import Relic
    e,p,t=setup('spell_zero_duration');e.state.relics.append(Relic('衰变沙漏',''))
    execution=e.combat.build_instant_execution(p,'发动不死X于目标',t,
        [{'x':1,'dodge':False}],e.combat._combat_entity_refs())
    execution.run_all()
    assert p.dao_wen['不死'].cooldown_remaining==1 and not t.has_status('不死')
