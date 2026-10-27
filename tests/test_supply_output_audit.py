"""供能收支探针：隔离真实支付/效果；不是完整法术手操或无限运行证明。"""
import unittest
from engine.models import Entity, GameState, Relic, StatusEffect
from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine
from engine.dice import DiceEngine


def arena(*relics):
    state = GameState(phase='in_combat', current_round=10)
    state.player = Entity('P', '轮回者', blood_limit=100, current_hp=90,
                          mana_limit=100, current_mana=20, speed_limit=10, current_speed=2)
    state.enemies = [Entity('T', '轮回者', blood_limit=100000, current_hp=100000)]
    state.relics = [Relic(name, '') for name in relics]
    return CombatEngine(state, DiceEngine(seed=1))


def cast_paid(combat, name, x, target=None, **kwargs):
    """仅拼合真实支付与效果入口，不绕称为execute_action或完整施法。"""
    p = combat.state.player
    target = target if target is not None else p
    calc = DaoWenEngine.resolve(name, x, caster=p, target=target, **kwargs)
    kind = calc['cost_type']
    # API pays mana before this effect entry; numeric costs are paid inside it.
    if kind in ('法力', '消耗') and not p.spend_mana(calc['cost']):
        raise ValueError('法力不足')
    return combat.apply_daowen_effect(name, calc, p, target)


class SupplyOutputAuditTests(unittest.TestCase):
    def test_immortal_aging_overdraft_feeds_damage_without_depletion(self):
        c = arena('不朽之躯');p = c.state.player;t = c.state.enemies[0]
        before = p.current_hp, p.blood_limit, p.current_mana
        for _ in range(12):
            cast_paid(c, '透支', 2, cost_channel='衰老')
            cast_paid(c, '杀伐', 2, t)
        self.assertEqual((p.current_hp, p.blood_limit, p.current_mana), before)
        self.assertEqual(t.current_hp, 100000 - 12 * 4)

    def test_gear_overclock_fatigue_cycle_feeds_huifeng(self):
        c = arena('倍速齿轮', '回锋刀');p = c.state.player;t = c.state.enemies[0]
        c._remember_huifeng_target(p, 'enemy:0')
        before = p.current_speed, p.current_mana, p.speed_limit, p.current_hp
        for _ in range(12):
            cast_paid(c, '超频', 1)
            cast_paid(c, '透支', 2, cost_channel='疲惫')
        self.assertEqual((p.current_speed, p.current_mana, p.speed_limit, p.current_hp), before)
        self.assertEqual(t.current_hp, 100000 - 12 * 6)

    def test_unbounded_hp_regen_overdraft_cup_can_feed_shaifa(self):
        c = arena('承露盏');p = c.state.player;t = c.state.enemies[0]
        p.add_status(StatusEffect('无限血限', 1, value=1, source='增殖'))
        before = p.current_hp, p.current_mana
        for _ in range(12):
            cast_paid(c, '再生', 5)
            cast_paid(c, '透支', 5, cost_channel='流血')
            cast_paid(c, '杀伐', 2, t)
            self.assertIsNone(c.check_cancer(p))
        self.assertEqual((p.current_hp, p.current_mana), before)
        self.assertEqual(t.current_hp, 100000 - 12 * 4)

    def test_self_lianji_potion_produces_health_with_infinite_hp(self):
        c = arena('增生药剂');p = c.state.player
        p.add_status(StatusEffect('无限血限', 1, value=1, source='增殖'))
        start = p.current_hp
        cast_paid(c, '连击', 1, p, cost_channel='流血')
        self.assertEqual(p.current_hp - start, 40)
        self.assertIsNone(c.check_cancer(p))
