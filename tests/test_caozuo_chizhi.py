"""【操纵】【迟滞】口径：指令 / 未给指令作废不退 / 出手加价对每一次行动同口径。

【操作】改名【操纵】，作用范围由「下一次」改为「下X次」。
"""
from engine.daowen import DaoWenEngine, normalize_caozuo_order
from tests.test_daowen_wiring import _engine, _give, _monster


def setup(suffix):
    e = _engine('caozuo_chizhi_' + suffix)
    p = e.state.player
    _give(p, '操纵')
    _give(p, '迟滞')
    a = _monster(e, '受术者', hp=100)
    e.execute_action('round_start', {})
    return e, p, a


def apply_caozuo(e, caster, target, x=1, order=None):
    calc = DaoWenEngine.calculate_caozuo(x, target, normalize_caozuo_order(order))
    return e.combat.apply_daowen_effect('操纵', calc, caster, target)


def apply_chizhi(e, caster, target, x=1):
    calc = DaoWenEngine.calculate_chizhi(x, target)
    return e.combat.apply_daowen_effect('迟滞', calc, caster, target)


def test_caozuo_predeclared_action_must_be_followed():
    """给出【跳过】指令后，目标接下来X次非施法行动只能是跳过。"""
    e, p, a = setup('order')
    apply_caozuo(e, p, a, order={'action': '跳过'})
    assert e.combat.caozuo_status_of(a) is not None

    refused = e.combat.caozuo_check(a, '攻击', [{'target_ref': 'player:0', 'damage': 1}])
    assert refused and '操纵' in refused['error'], refused

    assert e.combat.caozuo_check(a, '跳过') is None
    assert e.combat.caozuo_status_of(a) is None, 'X=1：执行一次即用完名额'


def test_caozuo_uses_scale_with_x_and_each_action_consumes_one():
    """操纵X：接下来X次非施法行动都由施法者控制，每执行一次扣1次名额。"""
    e, p, a = setup('uses')
    apply_caozuo(e, p, a, x=3, order={'action': '跳过'})
    for remaining in (2, 1):
        assert e.combat.caozuo_check(a, '跳过') is None
        assert e.combat.caozuo_status_of(a).effect_data['remaining_uses'] == remaining
    assert e.combat.caozuo_check(a, '跳过') is None
    assert e.combat.caozuo_status_of(a) is None, '第X次执行后名额归零、状态移除'


def test_caozuo_without_order_voids_and_never_refunds():
    """未预指定的：到该行动时作废，付出代价不退。"""
    e, p, a = setup('void')
    cap_before = p.mana_limit
    calc = DaoWenEngine.calculate_caozuo(1, a, None)
    assert calc['caozuo_order'] is None
    # apply 是唯一公开入口：支付与效果同在这一次结算里发生。
    e.combat.apply_daowen_effect('操纵', calc, p, a)
    assert p.mana_limit == cap_before - 1, '操纵要以法限支付'
    assert e.combat.caozuo_status_of(a) is not None

    # 目标照常行动：效果作废（不拒行动），但已支付的法限没有回退路径。
    assert e.combat.caozuo_check(a, '攻击', [{'target_ref': 'player:0', 'damage': 1}]) is None
    assert e.combat.caozuo_status_of(a) is None
    assert p.mana_limit == cap_before - 1, '作废不退代价'


def test_chizhi_charges_every_action_extra_action():
    """迟滞：每一次行动所需出手+1，回合外闪避同样记出手。"""
    e, p, a = setup('penalty')
    apply_chizhi(e, p, a, x=2)
    assert a.action_cost_penalty == 1
    calc = DaoWenEngine.calculate_chizhi(2, a)
    assert calc['action_cost_penalty'] == 1 and calc['duration'] == 2

    # 预算只有1次时，迟滞让这一次行动也做不成（要2次出手）。
    assert e._action_budget_of(a) == 1
    err = e._consume_action_or_error(a, action_type='攻击')
    assert err and '需2次' in err['error'], err

    # 拿到第2次出手后，同一次行动扣2次。
    a.add_status(e.combat._daowen_status(
        daowen_order=None, name='疯狂', value=1, remaining_rounds=1,
        source='测试', effect_data={}))
    assert e._action_budget_of(a) == 2
    a.actions_used_this_round = 0
    assert e._consume_action_or_error(a, action_type='攻击') is None
    assert a.actions_used_this_round == 2, '迟滞下每次行动要花2次出手'

    # 回合外防御：闪避本不占出手，迟滞下同样要付（怪物在非死斗中不记出手，
    # 故用轮回者验这一条）。
    apply_chizhi(e, a, p, x=1)
    assert p.action_cost_penalty == 1
    budget = e._action_budget_of(p)
    p.actions_used_this_round = budget
    assert e.combat.charge_reaction_penalty(p, '闪避') is not None
    p.actions_used_this_round = budget - 1
    assert e.combat.charge_reaction_penalty(p, '闪避') is None
    assert p.actions_used_this_round == budget
