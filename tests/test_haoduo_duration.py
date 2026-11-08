"""【豪夺】"持续X回合"的计时口径回归（2026-11-01）。

历史洞：calculate_haoduo 同时给出 duration=X，通用状态块会再挂一份【豪夺】，
与 _steal_one_relic 挂的镜像状态重叠——两份各按各的递减，回响还只认其中一份。
现行口径：
- 唯一计时事实源＝stolen_relics[name]["remaining"]；
- 玩家可见的镜像状态只有一份，带 relic_name，与账本同步增减；
- 回终 / 大梦额外回合都走 _tick_stolen_relics，到期把遗物还给原持有者。
"""
import os

from engine.daowen import DaoWenEngine
from engine.models import DaoWen, DaoWenInstance, Relic
from tests.test_daowen_wiring import _engine, _give, _monster


def setup(suffix, shards=100):
    e = _engine('haoduo_' + suffix)
    p = e.state.player
    _give(p, '豪夺')
    _give(p, '回响')
    m = _monster(e, '靶怪' + suffix, hp=200)
    m.relics = [Relic('防弹插板', '伤害减半')]
    e.state.shards = shards
    e.execute_action('round_start', {})
    return e, p, m


def steal(e, p, m, x=2):
    result = e.execute_action('use_daowen', {
        'daowen_name': '豪夺', 'x': x, 'target': m.name, 'dodge': False})
    assert result['success'], result
    return result


def haoduo_statuses(holder):
    return [s for s in holder.status_effects if s.name == '豪夺']


def test_only_one_haoduo_status_per_stolen_relic():
    """通用状态块不再重复挂【豪夺】——一件被夺遗物只有一个镜像状态。"""
    e, p, m = setup('single')
    steal(e, p, m)
    statuses = haoduo_statuses(p)
    assert len(statuses) == 1, [s.to_dict() for s in statuses]
    assert statuses[0].remaining_rounds == 2
    assert statuses[0].effect_data.get('relic_name') == '防弹插板'
    assert e.state.stolen_relics['防弹插板']['remaining'] == 2


def test_round_end_ticks_ledger_and_mirror_together_and_returns_relic():
    e, p, m = setup('expire')
    steal(e, p, m, x=2)
    e.combat._tick_sealed_and_stolen_relics()
    assert e.state.stolen_relics['防弹插板']['remaining'] == 1
    assert haoduo_statuses(p)[0].remaining_rounds == 1
    ticked = e.combat._tick_sealed_and_stolen_relics()
    assert ticked['returned_steals'] == ['防弹插板']
    assert '防弹插板' not in e.state.stolen_relics
    assert haoduo_statuses(p) == [], '到期后镜像状态必须一并移除，不留幽灵回合数'
    assert not any(r.name == '防弹插板' for r in e.state.relics), '到期应失去该遗物'
    assert any(r.name == '防弹插板' for r in m.relics), '到期应还给原持有者'


def test_huixiang_extends_ledger_and_mirror_together():
    """回响延长【豪夺】：状态与账本同步 +N，遗物不会"镜子变长、实物先还"。"""
    e, p, m = setup('echo')
    steal(e, p, m, x=2)
    calc = DaoWenEngine.resolve('回响', 2, target=p)
    e.combat.prepare_huixiang(calc, p, p)
    assert p.spend_mana(calc['cost'])
    result = e.combat.apply_daowen_effect('回响', calc, p, p)
    assert result['effects'], result
    assert haoduo_statuses(p)[0].remaining_rounds == 4
    assert e.state.stolen_relics['防弹插板']['remaining'] == 4
    # 仍按延长后的回合数结算：两次回终只是递减，不归还。
    e.combat._tick_sealed_and_stolen_relics()
    e.combat._tick_sealed_and_stolen_relics()
    assert e.state.stolen_relics['防弹插板']['remaining'] == 2
    assert any(r.name == '防弹插板' for r in e.state.relics)


def test_monster_holder_also_returns_relic_to_player_on_expiry():
    """反向：怪物夺走玩家遗物，到期走同一条账本路径还给玩家。"""
    e, p, m = setup('monster', shards=0)
    p_relic = Relic('血誓戒', '')
    e.state.relics = [p_relic]
    m.shards = 50
    calc = DaoWenEngine.resolve('豪夺', 1, target=p)
    stolen = e.combat._steal_one_relic(m, p, int(calc['duration']), '血誓戒')
    assert stolen == '血誓戒'
    assert any(r.name == '血誓戒' for r in m.relics)
    assert haoduo_statuses(m)[0].remaining_rounds == 1
    ticked = e.combat._tick_sealed_and_stolen_relics()
    assert '血誓戒' in ticked['returned_steals']
    assert any(r.name == '血誓戒' for r in e.state.relics), '到期应回到玩家身上'
    assert not any(r.name == '血誓戒' for r in m.relics)
    assert haoduo_statuses(m) == []


def test_steal_consumable_moves_and_returns_with_remaining_uses():
    """2026-11-07 加强：豪夺可夺消耗品；到期按归还时剩余耐久还回。"""
    from engine.models import Consumable
    e, p, m = setup('consumable')
    pill = Consumable(name='定命丸', effect='', current_uses=2, max_uses=2)
    m.consumables = [pill]
    # 目标同时有遗物与消耗品，玩家必须显式指定；夺走消耗品。
    r = e.execute_action('use_daowen', {
        'daowen_name': '豪夺', 'x': 2, 'target': m.name, 'dodge': False,
        'relic_name': '定命丸'})
    assert r['success'], r
    assert pill in e.state.consumables and pill not in m.consumables
    assert e.state.stolen_relics['定命丸']['kind'] == 'consumable'
    statuses = haoduo_statuses(p)
    assert len(statuses) == 1 and statuses[0].effect_data.get('item_kind') == 'consumable'
    # 夺取期间用掉1点耐久；到期归还时按剩余1点还回原主。
    pill.current_uses = 1
    e.combat._tick_sealed_and_stolen_relics()
    ticked = e.combat._tick_sealed_and_stolen_relics()
    assert ticked['returned_steals'] == ['定命丸']
    assert '定命丸' not in e.state.stolen_relics
    assert pill in m.consumables and pill.current_uses == 1
    assert haoduo_statuses(p) == []


def test_steal_nothing_reports_relic_or_consumable():
    """目标无任何可夺对象：扣费前拒绝，文案覆盖遗物或消耗品。"""
    e, p, m = setup('empty')
    m.relics = []
    r = e.execute_action('use_daowen', {
        'daowen_name': '豪夺', 'x': 1, 'target': m.name, 'dodge': False})
    assert r['success'] is False and '没有可夺取的遗物或消耗品' in r['error'], r


def test_dream_advancing_target_rounds_also_returns_stolen_relic():
    """大梦让目标额外经历回合时，目标身上的豪夺计时同样递减到期。"""
    e, p, m = setup('dream')
    p_relic = Relic('血誓戒', '')
    m.relics = [p_relic, Relic('防弹插板', '伤害减半')]
    # 玩家夺走血誓戒
    e.state.shards = 100
    real = DaoWenEngine.resolve('豪夺', 1, target=m)
    assert e.combat._steal_one_relic(p, m, int(real['duration']), '血誓戒') == '血誓戒'
    m.stolen_relics['防弹插板'] = {'remaining': 1, 'from_entity_ref': id(p),
                                   'from_entity_name': p.name, 'target_is_player': True}
    m.relics.append(p_relic)
    effects = e.combat.advance_target_rounds(m, 1, source='大梦')
    assert any(eff.get('type') == 'haoduo_returned' for eff in effects), effects
    assert '防弹插板' not in m.stolen_relics
