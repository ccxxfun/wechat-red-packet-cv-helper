"""Configuration/entrypoint integration tests without desktop/network access."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import enhanced
from vision import Match, ProcessResult, Region


class EnhancedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('card.png', 'open.png'):
            (self.root / name).write_bytes(b'fake')
        self.config = self.root / 'config.json'
        self.data = {'enhanced': {'enabled': True, 'allowed_groups': ['功德群'],
            'selected_group': '功德群', 'header_region': [0, 0, 100, 40],
            'chat_region': [0, 40, 300, 300], 'card_status_roi': [0, 0, 10, 10],
            'templates': {'cards': ['card.png'], 'open': ['open.png']},
            'reporting': {'header': '开始', 'footer': '结束', 'default_messages': ['恭喜发财']},
            'scan_interval': 0.2}}
        self.save()

    def save(self):
        self.config.write_text(json.dumps(self.data, ensure_ascii=False), encoding='utf-8')

    def test_opt_in_and_exact_allowlist(self):
        self.data['enhanced']['enabled'] = False
        self.save()
        with self.assertRaisesRegex(ValueError, 'enabled'):
            enhanced.load_settings(self.config)
        self.data['enhanced']['enabled'] = True
        self.data['enhanced']['selected_group'] = '其它群'
        self.save()
        with self.assertRaisesRegex(ValueError, 'selected_group'):
            enhanced.load_settings(self.config)

    def test_template_and_region_fail_closed(self):
        settings = enhanced.load_settings(self.config)
        self.assertEqual('功德群', settings.selected_group)
        self.assertEqual(self.root / 'records', settings.records_dir)
        self.data['enhanced']['templates']['cards'] = ['absent.png']
        self.save()
        with self.assertRaisesRegex(ValueError, 'template'):
            enhanced.load_settings(self.config)

    def test_extract_requires_unambiguous_sender_amount(self):
        self.assertEqual(('张三', '¥1.25', '恭喜发财'), enhanced.extract_packet(
            ('张三的红包', '红包金额¥1.25', '恭喜发财'), ('恭喜发财',)))
        self.assertIsNone(enhanced.extract_packet(('张三的红包', '¥1.25', '0.5元')))
        self.assertIsNone(enhanced.extract_packet(('红包金额¥1.25',)))

    def test_no_report_without_events(self):
        settings = enhanced.load_settings(self.config)
        with self.assertRaisesRegex(ValueError, 'no packets'):
            enhanced.send_report(settings, __import__('datetime').date.today())

    def test_process_records_only_confirmed_result(self):
        settings = enhanced.load_settings(self.config, scan_once=True)
        card = Match(Region(10, 50, 30, 20), .9)
        lines = [type('Line', (), {'text': word}) for word in
                 ('张三的红包', '金额¥1.25', '恭喜发财')]
        packet = ProcessResult('功德群', card, ('功德群', card.box, ()), (), True, True,
                               result_lines=tuple(lines), status='opened')
        class FakeVision:
            def __init__(self, config):
                self.seen = None
            def verify_group(self):
                return '功德群'
            def process_one(self, **kwargs):
                self.seen = kwargs['seen_cards']
                return packet
        with patch.object(enhanced, 'VisionHelper', FakeVision):
            self.assertEqual(0, enhanced.run(settings))
        report = (settings.records_dir / f'{__import__("datetime").date.today()}.txt').read_text(encoding='utf-8')
        self.assertIn('张三：1.25元', report)
        self.assertIn('今日合计功德善款1.25元', report)
        self.assertNotIn(' — 恭喜发财', report)


if __name__ == '__main__':
    unittest.main()
