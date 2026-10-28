"""增殖的生效血限必须真为∞；不能用有限基础账本/999伪装为其上限。"""
import math
from engine.daowen import DaoWenEngine
from engine.models import StatusEffect
from tests.test_daowen_wiring import _engine,_give,_monster


def setup(suffix):
    e=_engine('infinite_limit_'+suffix);p=e.state.player
    _monster(e,'靶',hp=100)
    e.execute_action('round_start',{})
    p.add_status(StatusEffect('无限血限',3,value=1,source='增殖'))
    return e,p


def test_real_infinite_constraint_is_not_finite_base_after_clamp():
    e,p=setup('clamp');base=p.blood_limit
    assert math.isinf(p.effective_blood_limit())
    e.state.apply_heal(p,5000)
    hp=p.current_hp
    assert hp>base
    e.combat.clamp_immortal_body(p)
    assert p.current_hp==hp
    assert p.to_dict()['blood_limit']=='∞'


def test_finite_aging_can_exceed_base_without_clamping_life_under_infinite_limit():
    e,p=setup('aging');hp=p.current_hp
    assert e.combat.validate_numeric_cost(p,'衰老',10000)
    e.combat.pay_numeric_cost(p,'衰老',10000)
    assert math.isinf(p.effective_blood_limit()) and p.current_hp==hp and p.is_alive
    assert p.blood_limit==0  # 保留既有基础账本语义，不把它冒充当前∞约束。


def test_generic_blood_limit_delta_does_not_turn_infinite_limit_into_finite_hp_cap():
    e,p=setup('delta');e.state.apply_heal(p,3000);hp=p.current_hp
    e.combat._apply_blood_limit_change(p,-20,'probe','debuff')
    assert p.current_hp==hp and math.isinf(p.effective_blood_limit())


def test_unknown_calculator_state_has_no_999_finite_substitute_for_infinity():
    calc=DaoWenEngine.resolve('透支',1000,cost_channel='衰老')
    assert calc['cost_blood_limit']==2000
    # 显式给出的有限上限依然校验，不把实际有限支付变成免费。
    import pytest
    with pytest.raises(ValueError):
        DaoWenEngine.resolve('透支',1000,cost_channel='衰老',_state={'blood_limit':10})


def test_real_spell_aging_supply_respects_current_infinite_limit():
    e,p=setup('spell');_give(p,'透支');hp=p.current_hp
    execution=e.combat.build_instant_execution(p,'发动透支X于自身',p,
        [{'x':1000,'cost_channel':'衰老'}],e.combat._combat_entity_refs())
    execution.run_all()
    assert execution.results[0].execution is not None
    assert p.current_hp==hp and p.is_alive and math.isinf(p.effective_blood_limit())
