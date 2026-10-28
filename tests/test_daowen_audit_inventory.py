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
        for name in ('血契', '波及', '坏死', '勾魂'):
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

    def test_long_night_draft_blocks_round_experience_not_just_triggers(self):
        dungeon = (ROOT / '副本/永夜庭.md').read_text(encoding='utf-8')
        self.assertIn('3.长夜X：支付：法力10X。使[目标]无法经历回合，持续X。', dungeon)
        self.assertNotIn('使[目标]由道纹、法术或遗物产生的[回始]与[回终]效果不再触发', dungeon)
        row = next(r for r in self.rows if r['name'] == '长夜')
        self.assertFalse(row['runtime'])
        self.assertIn('持续期时钟待明确', row['tags'])
        # 仅固定已裁定的草案原文，不伪造停止回合/期限递减的行为验证。

    def test_rule_boundary_and_examples_remain_explicit(self):
        rules = (ROOT / '规则正文.md').read_text(encoding='utf-8')
        for phrase in ('允许持续规则', '不允许内嵌触发', '按规则语义而非写法判定',
                       '疑义处理', '坏死禁止回复', '勾魂禁止获得法力',
                       '血契分担代价', '波及平分道纹效果',
                       '回终回复生命', '受击后获得法力', '状态结束时再生效'):
            self.assertIn(phrase, rules)
        self.assertNotIn('在事件发生时改写结算的效果，不符合道纹定义', rules)
        # 文本护栏只防止已裁定边界漂移，不是任意效果的自动语义判定器。


if __name__ == '__main__':
    unittest.main()
