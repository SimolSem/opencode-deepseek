"""Provider-neutral bridge settle metadata: tab whitelist, trace section, report."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chat_protocol import Reply
from providers import tab_bridge
from server import toolcall_logging
from server.trace_report import report

SECRET = 'SECRET-ANSWER-TEXT'


def settle_result(**extra):
    return {
        'settle': {'source': 'wire', 'tool': True, 'quiet_ms': 1200, 'body': SECRET, **extra},
        'late_rewrite': {'delay_ms': 3400, 'tool_before': True, 'tool_after': False, 'text': SECRET},
        'dom_check': {
            'found': True, 'match': False, 'exact': False, 'similarity': 12, 'coverage': 10,
            'changes': 1, 'stable_ms': 900, 'wire_len': 40, 'dom_len': 52,
            'tool_wire': True, 'tool_dom': False, 'ended': 'next_job', 'dom': SECRET,
        },
        'answer_text': SECRET,
    }


class SettleWhitelistTests(unittest.TestCase):
    def test_unlisted_provider_reports_nothing(self):
        with patch.dict(tab_bridge._SETTLE_EXTRA, {}, clear=True):
            self.assertIsNone(tab_bridge._sanitize_bridge_settle('demo', settle_result()))

    def test_common_keys_only_and_no_text(self):
        with patch.dict(tab_bridge._SETTLE_EXTRA, {'demo': None}, clear=True):
            out = tab_bridge._sanitize_bridge_settle('demo', settle_result(entries=2, held_path='/x'))
        self.assertEqual(out, {
            'source': 'wire', 'tool': True, 'quiet_ms': 1200,
            'late_rewrite': {'delay_ms': 3400, 'tool_before': True, 'tool_after': False},
            'dom_check': {
                'found': True, 'match': False, 'exact': False, 'similarity': 12, 'coverage': 10,
                'changes': 1, 'stable_ms': 900, 'wire_len': 40, 'dom_len': 52,
                'tool_wire': True, 'tool_dom': False, 'ended': 'next_job',
            },
        })
        self.assertNotIn(SECRET, json.dumps(out))

    def test_values_out_of_range_are_dropped(self):
        result = {'settle': {'source': 'other', 'quiet_ms': -1, 'tool': 'yes'},
                  'dom_check': {'similarity': True, 'changes': 10 ** 9, 'ended': 'later'}}
        with patch.dict(tab_bridge._SETTLE_EXTRA, {'demo': None}, clear=True):
            self.assertIsNone(tab_bridge._sanitize_bridge_settle('demo', result))

    @unittest.skipUnless(shutil.which('node'), 'Node required for the controller VM fixture')
    def test_controller_dom_check(self):
        fixture = Path(__file__).parent / 'dom_check_fixture.cjs'
        run = subprocess.run([shutil.which('node'), str(fixture)],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_reply_carries_settle(self):
        self.assertEqual(Reply('x', 'web:demo:x', bridge_settle={'source': 'dom'}).bridge_settle,
                         {'source': 'dom'})
        self.assertIsNone(Reply('x', 'web:demo:x').bridge_settle)


class SettleTraceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        patcher = patch.dict(os.environ, {'TOOLCALL_LOG': '1', 'DB_LOG': '1'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, name, *sections):
        path = self.root / name
        path.touch()
        toolcall_logging.record(path, 'from_provider', SECRET)
        for data in sections:
            toolcall_logging.record(path, 'bridge_settle', data)
        return path

    def test_section_is_written(self):
        path = self.record('1101000000-demo.log', {'source': 'wire', 'late_rewrite': {'delay_ms': 1500}})
        text = path.read_text(encoding='utf-8')
        self.assertIn('------------ bridge settle---------------\n', text)
        self.assertIn('"delay_ms": 1500', text)

    def test_report_groups_by_provider_without_text(self):
        self.record('1101000000-demo.log',
                    {'source': 'dom', 'tool': False},
                    {'source': 'wire', 'tool': True,
                     'late_rewrite': {'delay_ms': 4200},
                     'dom_check': {'match': False, 'similarity': 20, 'coverage': 30, 'changes': 2,
                                   'wire_len': 100, 'dom_len': 90,
                                   'tool_wire': True, 'tool_dom': False}},
                    {'source': 'wire',
                     'dom_check': {'match': True, 'similarity': 100, 'coverage': 100, 'changes': 0}})
        self.record('1101000001-other-2.log', {'source': 'wire'})
        result = report(self.root)
        demo = result['bridge_settle']['demo']
        self.assertEqual(demo['turns'], 3)
        self.assertEqual(demo['source_dom'], 1)
        self.assertEqual(demo['tool_envelope'], 1)
        self.assertEqual(demo['late_rewrite'], 1)
        self.assertEqual(demo['late_rewrite_delay_ms'], {'count': 1, 'min': 4200, 'max': 4200, 'avg': 4200})
        self.assertEqual(demo['dom_check'], 2)
        self.assertEqual(demo['dom_mismatch'], 1)
        self.assertEqual(demo['dom_suspect'], 1)
        self.assertEqual(demo['dom_tool_mismatch'], 1)
        self.assertEqual(demo['dom_changed_after_emit'], 1)
        self.assertEqual(demo['dom_similarity'], {'count': 2, 'min': 20, 'max': 100, 'avg': 60})
        self.assertNotIn('dom_render_extra', demo)
        self.assertEqual(result['bridge_settle']['other'], {'turns': 1})
        self.assertNotIn(SECRET, json.dumps(result))

    def test_rendered_widget_is_not_suspect(self):
        self.record('1101000000-demo.log', {
            'source': 'wire',
            'dom_check': {'match': False, 'similarity': 44, 'coverage': 100,
                          'wire_len': 1202, 'dom_len': 3824},
        })
        demo = report(self.root)['bridge_settle']['demo']
        self.assertEqual(demo['dom_mismatch'], 1)
        self.assertEqual(demo['dom_render_extra'], 1)
        self.assertNotIn('dom_suspect', demo)

    def test_logs_without_settle_add_no_block(self):
        self.record('1101000000-demo.log')
        self.assertNotIn('bridge_settle', report(self.root))


if __name__ == '__main__':
    unittest.main()
