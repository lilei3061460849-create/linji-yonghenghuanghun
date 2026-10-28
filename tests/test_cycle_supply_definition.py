"""供能分类用语及大梦/守夜灯的现行供能边界；不改游戏规则。"""
import unittest
from pathlib import Path
from engine.models import Entity, GameState, Relic
from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine
from engine.dice import DiceEngine


class CycleSupplyDefinitionTests(unittest.TestCase):
    def test_definition_is_in_spell_rules(self):
        text = Path('法术索引.md').read_text()
        for sentence in ('**输出端**：致胜方式', '输出端必须的资源',
                         '供能端有限的循环称为**循环**', '供能端无限的循环称为**无限**'):
            self.assertIn(sentence, text)

    def test_extra_rounds_are_dameng_not_new_chengni(self):
        DaoWenEngine.register_all()
        self.assertIn('大梦', DaoWenEngine._registry)
        self.assertNotIn('沉溺', DaoWenEngine._registry)
        from engine.gamedata import UNIMPLEMENTED_DAOWEN
        self.assertIn('沉溺', UNIMPLEMENTED_DAOWEN)

    def probe(self, world_round):
        state = GameState(phase='in_combat', current_round=world_round)
        player = Entity('持灯者', '轮回者', blood_limit=100, current_hp=100,
                        mana_limit=100, current_mana=20)
        state.player = player
        state.enemies = [Entity('靶', '怪物', blood_limit=100, current_hp=100)]
        state.relics = [Relic('守夜灯', '[回增]获得当前世界回合数的法力')]
        combat = CombatEngine(state, DiceEngine(seed=1))
        rows = []
        # Isolate payment and actual extra-round settlement, not a full spell submission.
        cost = DaoWenEngine.calculate_dameng(1, target=player)['cost']
        while player.is_alive and len(rows) < 6:
            self.assertTrue(player.spend_mana(cost))
            combat.advance_target_rounds(player, 1)
            rows.append(player.current_mana)
        self.assertEqual(state.current_round, world_round)
        self.assertEqual(len(rows), 5)
        self.assertFalse(player.is_alive)
        self.assertEqual(state.last_death_cause, 'mediocrity')
        return rows

    def test_round_four_loses_mana(self):
        self.assertEqual(self.probe(4), [19, 18, 17, 16, 15])

    def test_round_five_sustains_mana_but_mediocrity_stops_it(self):
        self.assertEqual(self.probe(5), [20] * 5)

    def test_round_ten_gains_mana_but_mediocrity_stops_it(self):
        self.assertEqual(self.probe(10), [25, 30, 35, 40, 45])
