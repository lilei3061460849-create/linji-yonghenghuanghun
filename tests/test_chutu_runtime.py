"""出土的实际流血支付与原变化保留；不冒称未实现的感染路径已接入。"""
import pytest
from engine.models import StatusEffect, Relic
from tests.test_daowen_wiring import _engine, _give, _monster


def setup(suffix):
    e = _engine('chutu_' + suffix)
    p = e.state.player
    _give(p, '出土')
    e.execute_action('round_start', {})
    p.blood_limit = p.current_hp = 100
    return e, p


def cast(e, x=2, **kw):
    return e.execute_action('use_daowen', {'daowen_name': '出土', 'x': x, **kw})


def resonance(e, source='杀伐', dest='再生', rtype='反转', **kw):
    return e.execute_action('use_resonance', {
        'source_daowen': source, 'target_daowen': dest, 'resonance_type': rtype,
        'target_ref': 'player:0', **kw})


def test_pays_bleed_once_not_mana_and_does_not_tick_by_round():
    e, p = setup('cost')
    before = p.current_mana
    result = cast(e)
    assert result['success'], result
    assert p.current_hp == 80 and p.current_mana == before
    assert p.get_status_value('出土') == 2
    p.tick_status_effects()
    assert p.get_status_value('出土') == 2
    assert p.get_status_effects('出土')[0].remaining_rounds == -1


def test_next_x_conversions_keep_original_then_normal_replacement_resumes():
    e, p = setup('count')
    _give(p, '杀伐')
    original = p.dao_wen['杀伐']
    original.cooldown_remaining = 3
    e.state.resonance['反转'] = 3
    e.state.resonance['曲解'] = 1
    assert cast(e)['success']
    r = resonance(e)
    assert r['success'] and r['original_preserved'], r
    assert p.dao_wen['杀伐'] is original and original.cooldown_remaining == 3
    assert '再生' in p.dao_wen and p.get_status_value('出土') == 1
    r = resonance(e, source='再生', dest='庇护', rtype='曲解')
    assert r['success'] and r['original_preserved'], r
    assert {'杀伐', '再生', '庇护'} <= set(p.dao_wen)
    assert not p.has_status('出土')
    r = resonance(e)
    assert r['success'] and not r['original_preserved'], r
    assert '杀伐' not in p.dao_wen


def test_failed_conversion_consumes_neither_resonance_nor_preservation():
    e, p = setup('invalid')
    _give(p, '杀伐')
    e.state.resonance['反转'] = 1
    assert cast(e, 1)['success']
    result = resonance(e, dest='超频')
    assert not result['success']
    assert e.state.resonance['反转'] == 1
    assert p.get_status_value('出土') == 1 and '杀伐' in p.dao_wen


def test_capacity_failure_keeps_original_and_does_not_spend_charges():
    e, p = setup('capacity')
    _give(p, '杀伐')
    p.dao_wen.pop('再生', None)
    e.state.resonance['反转'] = 1
    assert cast(e, 1)['success']
    p.death_page_count = p.pages_used
    result = resonance(e)
    assert not result['success'], result
    assert '死者之页' in result['error']
    assert e.state.resonance['反转'] == 1 and p.get_status_value('出土') == 1
    assert '杀伐' in p.dao_wen and '再生' not in p.dao_wen


def test_existing_destination_does_not_duplicate_kind_or_need_extra_page():
    e, p = setup('duplicate')
    _give(p, '杀伐');_give(p, '再生')
    dest = p.dao_wen['再生']
    e.state.resonance['反转'] = 1
    assert cast(e, 1)['success']
    p.death_page_count = p.pages_used
    result = resonance(e)
    assert result['success'], result
    assert p.dao_wen['再生'] is dest and '杀伐' in p.dao_wen
    assert not p.has_status('出土')


def test_rule_belongs_to_changed_holder_not_resonance_actor():
    e, p = setup('ownership')
    m = _monster(e, '变化者')
    _give(m, '杀伐')
    e.state.resonance['反转'] = 1
    assert cast(e, 1)['success']
    result = resonance(e, target_ref='enemy:0')
    assert result['success'], result
    assert '杀伐' not in m.dao_wen and '再生' in m.dao_wen
    assert p.get_status_value('出土') == 1


def test_non_actor_holder_can_preserve_its_own_original():
    e, p = setup('holder')
    m = _monster(e, '变化者')
    _give(m, '杀伐')
    m.add_status(StatusEffect('出土', -1, value=1, source=m.name))
    e.state.resonance['反转'] = 1
    result = resonance(e, target_ref='enemy:0')
    assert result['success'] and result['original_preserved'], result
    assert {'杀伐', '再生'} <= set(m.dao_wen)
    assert not m.has_status('出土')


def test_not_enough_life_does_not_create_preservation():
    e, p = setup('poor')
    p.current_hp = 9
    assert not cast(e, 1)['success']
    assert p.current_hp == 9 and not p.has_status('出土')


def test_same_source_or_missing_source_does_not_consume_preservation():
    e, p = setup('no_change')
    _give(p, '杀伐')
    assert cast(e, 1)['success']
    assert e._permanently_convert_daowen(p, '杀伐', '杀伐')
    assert not e._permanently_convert_daowen(p, '不存在', '再生')
    assert p.get_status_value('出土') == 1


def test_same_soul_pen_uses_second_holders_own_preservation():
    e, p = setup('pen')
    _give(p, '杀伐')
    m = _monster(e, '同魂变化者')
    _give(m, '杀伐')
    m.add_status(StatusEffect('出土', -1, value=1))
    e.state.relics.append(Relic('同魂笔', ''))
    e.state.resonance['反转'] = 1
    assert cast(e, 1)['success']
    result = resonance(e, second_target_ref='enemy:0', second_source_daowen='杀伐',
                       second_target_daowen='再生')
    assert result['success'], result
    assert {'杀伐', '再生'} <= set(p.dao_wen)
    assert {'杀伐', '再生'} <= set(m.dao_wen)
    assert not p.has_status('出土') and not m.has_status('出土')


def test_same_soul_pen_capacity_failure_does_not_change_secondary_holder():
    e, p = setup('pen_full')
    _give(p, '杀伐')
    m = _monster(e, '同魂满页者')
    _give(m, '杀伐')
    m.add_status(StatusEffect('出土', -1, value=1))
    m.death_page_count = m.pages_used
    e.state.relics.append(Relic('同魂笔', ''))
    e.state.resonance['反转'] = 1
    result = resonance(e, second_target_ref='enemy:0', second_source_daowen='杀伐',
                       second_target_daowen='再生')
    assert result['success'], result
    assert '死者之页' in result['second_target_log']
    assert '杀伐' in m.dao_wen and '再生' not in m.dao_wen
    assert m.get_status_value('出土') == 1


def test_repeated_casts_use_existing_status_charge_merge_rule():
    e, p = setup('merge')
    assert cast(e, 1)['success']
    assert cast(e, 2)['success']
    assert p.current_hp == 70
    assert p.get_status_value('出土') == 3
