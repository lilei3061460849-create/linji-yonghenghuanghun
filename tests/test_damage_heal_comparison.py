"""杀伐＋再生的真实瞬发流程；不把未定稿生命转移伪装成已实现。"""
import unittest

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import DaoWen, DaoWenInstance, Entity, GameState, StatusEffect
from engine.spell_execution import ExecutionStatus


class DamageHealComparisonTests(unittest.TestCase):
    def cast_pair(self, attack_x, heal_x, shield=0, block_heal=False):
        state = GameState(phase='in_combat', combat_subphase='player_actions', current_round=1)
        p = Entity('轮回者', '轮回者', blood_limit=100, current_hp=50,
                   mana_limit=100, current_mana=100, speed_limit=5, current_speed=5)
        enemy = Entity('靶怪', '怪物', blood_limit=100, current_hp=100,
                       attack_count=1, attack_power=3)
        enemy.shield = shield
        state.player, state.enemies = p, [enemy]
        for name in ('杀伐', '再生'):
            p.dao_wen[name] = DaoWenInstance(DaoWen(name=name, formula='', cost_type='消耗',
                                                  cost_formula='X', effect_formula=''), x_value=0)
        if block_heal:
            p.add_status(StatusEffect('坏死', 2, value=1))
        combat = CombatEngine(state, DiceEngine(seed=1))
        execution = combat.build_instant_execution(
            p, '发动杀伐X于目标→发动再生X于自身', enemy,
            [{'x': attack_x, 'dodge': False}, {'x': heal_x}], combat._combat_entity_refs())
        execution.run_all()
        self.assertEqual(execution.status, ExecutionStatus.COMPLETED)
        return p, enemy

    def test_equal_one_life_change_costs_two_mana_but_heals_four(self):
        p, enemy = self.cast_pair(1, 1)
        self.assertEqual((p.current_mana, enemy.current_hp, p.current_hp), (98, 99, 54))

    def test_four_loss_and_four_heal_cost_three_mana(self):
        p, enemy = self.cast_pair(2, 1)
        self.assertEqual((p.current_mana, enemy.current_hp, p.current_hp), (97, 96, 54))

    def test_same_five_mana_budget(self):
        p, enemy = self.cast_pair(4, 1)
        self.assertEqual((p.current_mana, enemy.current_hp, p.current_hp), (95, 84, 54))

    def test_healing_is_not_limited_by_actual_enemy_hp_loss(self):
        p, enemy = self.cast_pair(2, 1, shield=100)
        self.assertEqual((enemy.current_hp, enemy.shield, p.current_hp), (100, 96, 54))

    def test_bad_death_blocks_heal_not_damage(self):
        p, enemy = self.cast_pair(2, 1, block_heal=True)
        self.assertEqual((enemy.current_hp, p.current_hp), (96, 50))


if __name__ == '__main__':
    unittest.main()
