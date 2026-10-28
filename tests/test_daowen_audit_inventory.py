"""审查覆盖护栏，不是道纹设计全部合规的证明。"""
import json
import re
import unittest
from pathlib import Path

from engine.daowen import DaoWenEngine

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / 'docs/全道纹标准审查清单-2026-10-28.json'


class DaoWenAuditInventoryTests(unittest.TestCase):
    def setUp(self):
        self.rows = json.loads(AUDIT.read_text(encoding='utf-8'))['rows']

    def test_all_current_definitions_have_review(self):
        DaoWenEngine.register_all()
        runtime = set(DaoWenEngine._registry)
        dungeon = set()
        for path in (ROOT / '副本').glob('*.md'):
            dungeon.update(re.findall(r'^\d+\.([\u4e00-\u9fff]{2})X',
                                     path.read_text(encoding='utf-8'), re.M))
        examples = set()
        for path in (ROOT / 'data/custom_resources/examples').glob('*.json'):
            spec = json.loads(path.read_text(encoding='utf-8'))
            if spec['category'] == '道纹':
                examples.add(spec['name'])
        names = [row['name'] for row in self.rows]
        self.assertEqual(len(names), len(set(names)), '清单不得重复计数')
        self.assertEqual(set(names), runtime | dungeon | examples,
                         '定义有增删，须人工补审清单；不能自动标为合规')
        self.assertEqual({r['name'] for r in self.rows if r['runtime']}, runtime)
        self.assertEqual({r['name'] for r in self.rows if r.get('example')}, examples)

    def test_each_review_has_traceable_evidence_and_explicit_conclusion(self):
        allowed = {'暂未发现结构违规', '明确重审', '语义待定', '边界复核', '待按统一定义复核'}
        for row in self.rows:
            with self.subTest(name=row['name']):
                self.assertEqual(len(row['name']), 2)
                self.assertIn(row['review'], allowed)
                self.assertTrue(row['note'])
                self.assertTrue(row['sources'])
                for source in row['sources']:
                    path = ROOT / source['path']
                    self.assertTrue(path.is_file())
                    self.assertGreater(source['line'], 0)
                    self.assertTrue(source['text'])
                    # 源句变化须人工重审；行号仅供导航，不因上方增删行误报。
                    self.assertIn(source['text'], path.read_text(encoding='utf-8'))

    def test_persistent_relations_are_not_misclassified_as_triggers(self):
        by_name = {r['name']: r for r in self.rows}
        for name in ('共罪', '波及', '坏死', '勾魂'):
            self.assertEqual(by_name[name]['review'], '暂未发现结构违规')
            self.assertNotIn('后续事件触发/改写', by_name[name]['tags'])

    def test_retired_kuizeng_is_only_an_archived_design(self):
        self.assertNotIn('馈赠', {r['name'] for r in self.rows})
        dungeon = (ROOT / '副本/永夜庭.md').read_text(encoding='utf-8')
        self.assertNotRegex(dungeon, r'馈赠(?:X|\d+)')
        self.assertIn('⇄（转换）？？⇄（曲解）长夜X', dungeon)
        scraps = (ROOT / '废稿汇总.md').read_text(encoding='utf-8')
        self.assertIn('| A09 | 馈赠、受击次数供法力', scraps)
        self.assertIn('使目标受击时获得X点法力，持续∞', scraps)

    def test_round_blocking_moves_to_unnamed_sea_reversal(self):
        sea = (ROOT / '副本/沉沦海.md').read_text(encoding='utf-8')
        night = (ROOT / '副本/永夜庭.md').read_text(encoding='utf-8')
        self.assertIn('沉溺X ⇄（反转）【未命名道纹X】', sea)
        self.assertIn('使[目标]无法经历X个回合。', sea)
        self.assertIn('中断该次回合，并使剩余阻断次数-1', sea)
        self.assertIn('普通回合与沉溺产生的额外回合均适用', sea)
        self.assertIn('名称与支付待定', sea)
        self.assertIn('3.长夜X：效果待重做', night)
        self.assertNotIn('使[目标]无法经历回合，持续X', night)
        row = next(r for r in self.rows if r['name'] == '长夜')
        self.assertEqual(row['review'], '语义待定')
        design = json.loads(AUDIT.read_text(encoding='utf-8'))['pending_designs'][0]
        self.assertIsNone(design['name'])
        self.assertIsNone(design['cost'])
        self.assertFalse(design['runtime'])
        self.assertIn(design['effect'], sea)

    def test_undying_draft_uses_cooldown_and_life_floor(self):
        dungeon = (ROOT / '副本/永夜庭.md').read_text(encoding='utf-8')
        self.assertIn('4.不死X：支付：冷却X。使[目标]的生命无法降至1以下，持续X。', dungeon)
        self.assertNotIn('4.不死X：支付：法力10X', dungeon)
        self.assertNotIn('当前生命为0及以下时不触发[命零]', dungeon)
        row = next(r for r in self.rows if r['name'] == '不死')
        self.assertFalse(row['runtime'])
        self.assertEqual(row['review'], '暂未发现结构违规')

    def test_eternal_life_swap_has_no_healing_exemption(self):
        dungeon = (ROOT / '副本/永夜庭.md').read_text(encoding='utf-8')
        self.assertIn('5.永生X：支付：衰老10X。使[目标]的当前生命与已损生命互换，持续X。', dungeon)
        self.assertIn('生命增加视为回复，生命减少视为失血', dungeon)
        self.assertNotIn('［回终］［回复］你的所有已损生命', dungeon)
        self.assertIn('恢复至本次互换发生前记录的当前生命', dungeon)
        self.assertIn('而不是按届时数值再次互换', dungeon)
        self.assertIn('不回退期间的累计回复量、失血记录或已支付代价', dungeon)
        row = next(r for r in self.rows if r['name'] == '永生')
        self.assertFalse(row['runtime'])
        self.assertEqual(row['review'], '语义待定')

    def test_pact_names_do_not_reuse_the_wrong_mechanism(self):
        from engine.gamedata import UNIMPLEMENTED_DAOWEN
        DaoWenEngine.register_all()
        self.assertIn('共罪', DaoWenEngine._registry)
        self.assertNotIn('血契', DaoWenEngine._registry)
        self.assertIn('血契', UNIMPLEMENTED_DAOWEN)
        calc = DaoWenEngine.resolve('共罪', 2)
        self.assertEqual(calc['dao_wen'], '共罪')
        self.assertEqual(calc['mark_targets'], 2)
        self.assertEqual(calc['cost_type'], '流血')
        self.assertEqual(calc['cost_hp'], 10)
        self.assertNotIn('cost', calc)
        self.assertEqual(calc['duration'], -1)
        dungeon = (ROOT / '副本/永夜庭.md').read_text(encoding='utf-8')
        self.assertIn('6.血契X：支付：法力10X。使[目标]替你支付代价，持续X。', dungeon)
        self.assertNotIn('组内其他目标同步支付相同代价', dungeon)
        self.assertIn('获得道纹[共罪]', dungeon)
        by_name = {r['name']: r for r in self.rows}
        self.assertTrue(by_name['共罪']['runtime'])
        self.assertFalse(by_name['血契']['runtime'])

    def test_retired_life_loss_daowen_does_not_delete_life_loss_rules(self):
        self.assertNotIn('失血', {r['name'] for r in self.rows})
        dungeon = (ROOT / '副本/永夜庭.md').read_text(encoding='utf-8')
        self.assertNotRegex(dungeon, r'失血(?:X|\d)')
        self.assertIn('血契X⇄（曲解）？？⇄（反转）饥饿X', dungeon)
        self.assertIn('生命减少视为失血', dungeon)
        scraps = (ROOT / '废稿汇总.md').read_text(encoding='utf-8')
        self.assertIn('| A10 | 失血道纹、伤害来源自反噬', scraps)
        self.assertIn('法力3X，使目标每次造成伤害时失去等量生命，持续X', scraps)

    def test_hunger_draft_temporarily_lowers_blood_limit(self):
        dungeon = (ROOT / '副本/永夜庭.md').read_text(encoding='utf-8')
        self.assertIn('8.饥饿X：支付：法力10X。使[目标]的血限降至当前生命，持续X。', dungeon)
        self.assertNotIn('[目标]失去等同已损生命的生命，持续 X', dungeon)
        row = next(r for r in self.rows if r['name'] == '饥饿')
        self.assertFalse(row['runtime'])
        self.assertIn('相反效果统一后生效覆盖先生效', row['note'])
        # 文本护栏不代表已验证增殖覆盖或无限数值反制。

    def test_opposing_effects_follow_latest_effect_rule(self):
        rules = (ROOT / '规则正文.md').read_text(encoding='utf-8')
        for phrase in ('相反效果覆盖：', '后生效者覆盖先生效者',
                       '不按X大小、来源资源类型或是否为∞赋予额外优先级',
                       '先增殖后饥饿，饥饿覆盖增殖的无限血限',
                       '先饥饿后增殖，增殖覆盖饥饿的血限限制'):
            self.assertIn(phrase, rules)
        # 是规则文本护栏，不是相反状态覆盖运行时已实现的证明。

    def test_infection_replaces_daowen_and_library_restores_original(self):
        dungeon = (ROOT / '副本/荒疫古城.md').read_text(encoding='utf-8')
        self.assertIn('1.感染X（核心起点/终点）：支付待定。使[目标]的X种道纹变为感染。', dungeon)
        self.assertIn('后续感染仍须主动发动并支付代价，不会自动传播', dungeon)
        self.assertIn('2.辨认一龛：衰老10。选择自身一种被【感染】替换的道纹，使其恢复为被感染前的原版道纹。', dungeon)
        self.assertNotIn('选择自身一种【尘封】', dungeon)
        self.assertNotIn('支付：法力3X。使[目标]获得X层【异变】', dungeon)
        self.assertIn('5.尘封X：保留名称及闭环位置，效果与支付待定', dungeon)
        self.assertNotRegex(dungeon, r'，尘封[，）]')
        row = next(r for r in self.rows if r['name'] == '感染')
        self.assertFalse(row['runtime'])
        self.assertIn('新效果支付待定', row['tags'])
        self.assertIn('| A11 | 感染旧效、主动赋予异变',
                      (ROOT / '废稿汇总.md').read_text(encoding='utf-8'))

    def test_excavation_is_retained_as_transformation_rule(self):
        dungeon = (ROOT / '副本/荒疫古城.md').read_text(encoding='utf-8')
        self.assertIn('3.出土X：支付：流血10X。自身接下来X次道纹变化保留原道纹。', dungeon)
        row = next(r for r in self.rows if r['name'] == '出土')
        self.assertEqual(row['review'], '暂未发现结构违规')
        self.assertFalse(row['runtime'])

    def test_tampering_retains_target_selection_rule(self):
        dungeon = (ROOT / '副本/荒疫古城.md').read_text(encoding='utf-8')
        self.assertIn('4.篡改X：支付：流血3X。使[目标]接下来X次发动道纹的目标由你选择。', dungeon)
        row = next(r for r in self.rows if r['name'] == '篡改')
        self.assertEqual(row['review'], '暂未发现结构违规')
        self.assertFalse(row['runtime'])
        self.assertIn('合法目标范围', row['note'])

    def test_catalysis_is_retained_without_rewriting_its_effect(self):
        dungeon = (ROOT / '副本/荒疫古城.md').read_text(encoding='utf-8')
        self.assertIn('6.催化X：支付：冷却2X。使自身或[目标]X种处于【冷却】状态的道纹立即发动一次。', dungeon)
        row = next(r for r in self.rows if r['name'] == '催化')
        self.assertEqual(row['review'], '暂未发现结构违规')
        self.assertFalse(row['runtime'])

    def test_origin_has_one_random_target_borrowing_definition(self):
        definition = '原初X：支付：异变5X。借用[目标]身上自身未拥有的随机X种道纹。'
        sea = (ROOT / '副本/荒疫古城.md').read_text(encoding='utf-8')
        book = (ROOT / '死者之书.md').read_text(encoding='utf-8')
        self.assertIn('8.' + definition, sea)
        self.assertIn(definition, book)
        self.assertNotIn('原初X：支付：异变25X', sea)
        self.assertNotIn('其数值固定为本次X', book)
        row = next(r for r in self.rows if r['name'] == '原初')
        self.assertFalse(row['runtime'])
        self.assertIn('统一定义未接入', row['tags'])

    def test_rule_boundary_and_examples_remain_explicit(self):
        rules = (ROOT / '规则正文.md').read_text(encoding='utf-8')
        for phrase in ('允许持续规则', '不允许内嵌触发', '按规则语义而非写法判定',
                       '疑义处理', '坏死禁止回复', '勾魂禁止获得法力',
                       '共罪分担代价', '波及平分道纹效果',
                       '回终回复生命', '受击后获得法力', '状态结束时再生效'):
            self.assertIn(phrase, rules)
        self.assertNotIn('在事件发生时改写结算的效果，不符合道纹定义', rules)
        # 文本护栏只防止已裁定边界漂移，不是任意效果的自动语义判定器。


if __name__ == '__main__':
    unittest.main()
