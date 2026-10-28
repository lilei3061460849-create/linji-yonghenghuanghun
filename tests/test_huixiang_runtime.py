"""回响真实支付/状态变更回归；有限与∞哨兵，不声称穷举∞组合。"""
import pytest
from engine.daowen import DaoWenEngine
from engine.models import StatusEffect, Relic
from tests.test_daowen_wiring import _engine, _give, _monster


def setup(suffix):
    e = _engine('huixiang_' + suffix)
    _give(e.state.player, '回响')
    t = _monster(e, '回响靶')
    e.execute_action('round_start', {})
    e.state.player.mana_limit = 100
    e.state.player.current_mana = 100
    return e, e.state.player, t


def cast(e, t, x=2, **kw):
    return e.execute_action('use_daowen', {
        'daowen_name': '回响', 'x': x, 'target': t.name,
        'dodge': False, 'blood_shadow': False, **kw})


def test_api_pays_five_x_and_only_extends_selected_status():
    e, p, t = setup('selected')
    a = StatusEffect('甲', 3, value=7, source='original')
    b = StatusEffect('乙', 5, value=8, source='other')
    t.status_effects.extend([a, b])
    result = cast(e, t, status_index=1)
    assert result['success'], result
    assert p.current_mana == 90 and p.actions_used_this_round == 1
    assert a.remaining_rounds == 3 and b.remaining_rounds == 7
    assert (b.name, b.value, b.source) == ('乙', 8, 'other')
    assert not t.has_status('回响')  # 即时改已有状态，不挂到期再触发的新状态。


def test_unique_status_can_be_selected_without_index():
    e, _, t = setup('unique')
    t.add_status(StatusEffect('唯一', 4))
    result = cast(e, t, x=3)
    assert result['success'], result
    assert t.status_effects[0].remaining_rounds == 7


@pytest.mark.parametrize('index', [True, -1, 99, '0', 0.5])
def test_bad_index_is_rejected_without_payment_or_action(index):
    e, p, t = setup('invalid_' + str(index))
    t.add_status(StatusEffect('唯一', 4))
    before = p.current_mana, p.actions_used_this_round
    result = cast(e, t, status_index=index)
    assert not result['success'], result
    assert (p.current_mana, p.actions_used_this_round) == before
    assert t.status_effects[0].remaining_rounds == 4


@pytest.mark.parametrize('durations', [[], [2, 3], [0]])
def test_empty_ambiguous_or_expired_selection_does_not_pay(durations):
    e, p, t = setup('ambiguous_' + str(durations))
    t.status_effects = [StatusEffect(str(i), value) for i, value in enumerate(durations)]
    before = p.current_mana, p.actions_used_this_round
    assert not cast(e, t)['success']
    assert (p.current_mana, p.actions_used_this_round) == before


def test_infinity_stays_infinity_and_is_not_added_as_minus_one():
    e, p, t = setup('infinity')
    t.add_status(StatusEffect('永久', -1, value=6))
    result = cast(e, t, x=4)
    assert result['success'], result
    assert p.current_mana == 80
    assert t.status_effects[0].remaining_rounds == -1
    assert t.status_effects[0].value == 6


def test_insufficient_mana_does_not_change_status():
    e, p, t = setup('poor')
    t.add_status(StatusEffect('唯一', 4))
    p.current_mana = 4
    assert not cast(e, t, x=1)['success']
    assert p.current_mana == 4 and t.status_effects[0].remaining_rounds == 4


def test_removed_selection_does_not_retarget_replacement():
    e, p, t = setup('stale')
    t.add_status(StatusEffect('原状态', 3))
    calc = DaoWenEngine.resolve('回响', 2, target=t)
    e.combat.prepare_huixiang(calc, p, t)
    t.status_effects.clear()
    replacement = StatusEffect('新状态', 4)
    t.status_effects.append(replacement)
    assert p.spend_mana(calc['cost'])
    result = e.combat.apply_daowen_effect('回响', calc, p, t)
    assert replacement.remaining_rounds == 4
    assert result['effects'][-1]['type'] == 'duration_extension_missed'


def test_wave_divides_added_duration_without_multiplying_it():
    e, p, a = setup('wave')
    b = _monster(e, '另一个回响靶')
    a.status_effects.append(StatusEffect('甲', 2))
    b.status_effects.append(StatusEffect('乙', 5))
    e.combat._toggle_wave_mark(a, p)
    e.combat._toggle_wave_mark(b, p)
    refs = e.combat._combat_entity_refs()
    picks = {ref: 0 for ref, actor in refs.items() if actor is a or actor is b}
    result = cast(e, a, x=4, status_indices=picks)
    assert result['success'], result
    assert a.status_effects[0].remaining_rounds == 4
    assert b.status_effects[0].remaining_rounds == 7
    assert p.current_mana == 80


def test_wave_missing_secondary_selection_is_rejected_before_payment():
    e, p, a = setup('wave_bad')
    b = _monster(e, '另一个回响靶')
    a.status_effects.append(StatusEffect('甲', 2))
    b.status_effects.append(StatusEffect('乙', 5))
    e.combat._toggle_wave_mark(b, p)  # 乙与波及标记两种，不能擅选第一项。
    before = p.current_mana, p.actions_used_this_round
    assert not cast(e, a)['success']
    assert (p.current_mana, p.actions_used_this_round) == before


def test_decay_hourglass_reduces_effect_but_not_cost():
    e, p, t = setup('decay')
    e.state.relics.append(Relic('衰变沙漏', ''))
    t.status_effects.append(StatusEffect('甲', 2))
    result = cast(e, t, x=3)
    assert result['success'], result
    assert p.current_mana == 85
    assert t.status_effects[0].remaining_rounds == 4


def test_instant_spell_step_carries_explicit_status_selection():
    e, p, t = setup('instant')
    t.status_effects.extend([StatusEffect('甲', 2), StatusEffect('乙', 5)])
    execution = e.combat.build_instant_execution(
        p, '发动回响X于目标', t,
        [{'x': 2, 'dodge': False, 'status_index': 1}],
        e.combat._combat_entity_refs())
    execution.run_all()
    assert p.current_mana == 90
    assert [s.remaining_rounds for s in t.status_effects] == [2, 7]


def test_reaction_request_carries_explicit_status_selection():
    e, _, _ = setup('request')
    request = e.combat._decision_to_requests({'steps': [
        {'x': 2, 'status_index': 3, 'status_indices': {'enemy:0': 1}}]})[0]
    assert request.extra['status_index'] == 3
    assert request.extra['status_indices'] == {'enemy:0': 1}


def test_new_wave_recipient_is_not_silently_assigned_a_status():
    e, p, a = setup('new_wave')
    a.status_effects.append(StatusEffect('甲', 2))
    calc = DaoWenEngine.resolve('回响', 4, target=a)
    e.combat.prepare_huixiang(calc, p, a)
    b = _monster(e, '后加入波及')
    b.status_effects.append(StatusEffect('乙', 5))
    e.combat._toggle_wave_mark(b, p)
    assert p.spend_mana(calc['cost'])
    result = e.combat.apply_daowen_effect('回响', calc, p, a)
    assert b.status_effects[0].remaining_rounds == 5
    assert any(row['type'] == 'duration_extension_missed' and row['target'] == b.name
               for row in result['effects'])
