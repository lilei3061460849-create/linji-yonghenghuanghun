import math
import pytest
from engine.models import Entity,StatusEffect
from tests.test_daowen_wiring import _engine,_give,_monster


def actor():
    return Entity(name='目标',entity_type='轮回者',mana_limit=10,current_mana=3,
        speed_limit=8,current_speed=3,blood_limit=30,current_hp=30)


def st(name,rounds=5,value=1,**kw):
    return StatusEffect(name=name,remaining_rounds=rounds,value=value,**kw)


@pytest.mark.parametrize('big',[1,100,math.inf])
def test_later_force_power_wins_without_x_or_infinity_priority(big):
    p=actor();p.add_status(st('全力',value=big));p.add_status(st('龙族利爪'))
    assert p.effective_attack_power()==6
    p.add_status(st('全力'))
    assert p.effective_attack_power()==10


def test_later_speed_force_overrides_old_reduction_but_new_reduction_applies():
    p=actor();p.add_status(st('手雷减攻',value=2))
    assert p.effective_attack_count()==1
    p.add_status(st('全速'))
    assert p.effective_attack_count()==8
    p.add_status(st('手雷减攻',value=1))
    assert p.effective_attack_count()==7  # 旧2点不能被跨过全速后合并复活。
    p.add_status(st('全速'))
    assert p.effective_attack_count()==8


def test_expiry_removes_only_its_own_override_and_earlier_live_rule_remains():
    p=actor();p.add_status(st('全力',rounds=4));p.add_status(st('龙族利爪',rounds=1))
    assert p.effective_attack_power()==6
    p.tick_status_effects()
    assert p.effective_attack_power()==10


def test_daowen_sequence_does_not_decide_opposing_output():
    p=actor();p.add_status(st('全力',daowen_order=99));p.add_status(st('龙族利爪',daowen_order=0))
    assert p.effective_attack_power()==6
    assert [s.name for s in p.ordered_statuses()]==['龙族利爪','全力']


def test_dingxing_snapshots_outputs_without_freezing_mana_or_speed_payment():
    p=actor();p.add_status(st('定型'))
    p.current_mana=0;p.current_speed=1
    assert p.effective_attack_power()==3 and p.effective_attack_count()==3
    p.add_status(st('全力'))
    assert p.effective_attack_power()==10 and p.effective_attack_count()==3
    p.add_status(st('全速'))
    assert p.effective_attack_count()==8
    p.add_status(st('定型'))
    p.mana_limit=20;p.speed_limit=20
    assert p.effective_attack_power()==10 and p.effective_attack_count()==8


def test_direct_legacy_statuses_use_list_order_until_new_application_is_stamped():
    p=actor();p.status_effects.extend([st('全力'),st('龙族利爪')])
    assert p.effective_attack_power()==6
    p.add_status(st('全力'))
    assert p.effective_attack_power()==10


def test_snapshot_preserves_conflict_order_and_lock_values():
    e=_engine('override_save');p=e.state.player
    p.add_status(st('全力'));p.add_status(st('定型'))
    before=p.effective_attack_power()
    restored=e._deserialize_entity_full(e._serialize_entity_full(p))
    restored.current_mana=0;restored.mana_limit+=5
    assert restored.effective_attack_power()==before
    restored.add_status(st('龙族利爪'))
    assert restored.effective_attack_power()==0


def test_real_dingxing_cast_locks_post_payment_panel_then_later_force_can_override():
    e=_engine('override_api');p=e.state.player;_give(p,'定型');_give(p,'全速')
    _monster(e);e.execute_action('round_start',{})
    result=e.execute_action('use_daowen',{'daowen_name':'定型','x':2,'target_ref':'player:0'})
    assert result['success'],result
    power=p.current_mana;hits=p.current_speed
    p.current_mana=0;p.current_speed=1
    assert p.effective_attack_power()==power and p.effective_attack_count()==hits
    result=e.execute_action('use_daowen',{'daowen_name':'全速','x':1,'target_ref':'player:0'})
    assert result['success'],result
    assert p.effective_attack_count()==p.speed_limit


def test_pickle_legacy_status_adds_missing_override_metadata():
    import pickle
    p=actor();effect=st('全力');effect.__dict__.pop('effect_data');effect.__dict__.pop('effect_sequence')
    p.status_effects.append(effect)
    restored=pickle.loads(pickle.dumps(p))
    assert restored.effective_attack_power()==10
    restored.add_status(st('龙族利爪'))
    assert restored.effective_attack_power()==6


def test_legacy_positional_status_daowen_order_keeps_its_original_slot():
    effect=StatusEffect('全力',2,1,'旧来源','battle','buff',None,0,7)
    assert effect.daowen_order==7 and effect.effect_sequence==0
