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
        allowed = {'暂未发现结构违规', '明确重审', '语义待定', '边界复核'}
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

    def test_known_event_rewriters_are_not_silently_marked_clean(self):
        by_name = {r['name']: r for r in self.rows}
        # 是债务记录，不是批准这些设计继续扩张；修复时须同步更新审查证据。
        for name in ('血契', '波及', '永生', '回响', '出土', '篡改'):
            self.assertEqual(by_name[name]['review'], '明确重审')
            self.assertIn('后续事件触发/改写', by_name[name]['tags'])


if __name__ == '__main__':
    unittest.main()
