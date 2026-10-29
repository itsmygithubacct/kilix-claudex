from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import route
import pane_loop
import usage_input
from usage_pages import window


class ManualUsageTests(unittest.TestCase):
    def test_malformed_or_out_of_range_readings_need_manual_input(self):
        for windows in (None, 7, {}, [], [{'used_percent': float('nan')}],
                        [{'used_percent': True}], [{'used_percent': -1}], [{'used_percent': 101}]):
            with self.subTest(windows=windows):
                self.assertTrue(usage_input.needs_input({'status': 'ok', 'windows': windows}))

    def test_percentages_accept_the_website_label(self):
        for text, basis, used in [('20', 'left', 80), ('20%', 'used', 20),
                                  ('20% used', 'left', 20), ('20% remaining', 'used', 80),
                                  ('100 left', 'used', 0), ('0 used', 'left', 0),
                                  ('unknown', 'used', None)]:
            with self.subTest(text=text):
                self.assertEqual(usage_input.percentage(text, basis), used)
        for text in ('-1', '101', 'nan', 'infinity', ''):
            with self.assertRaises(ValueError):
                usage_input.percentage(text, 'used')

    def test_each_missing_window_is_asked_and_invalid_input_is_retried(self):
        ask = Mock(side_effect=['bad', '25% left', '60'])
        entry = usage_input.read_manual('codex', now=1000, ask=ask)
        self.assertEqual(ask.call_count, 3)
        self.assertIn('5-hour', ask.call_args_list[0].args[0])
        self.assertIn('weekly', ask.call_args_list[-1].args[0])
        self.assertEqual([w['remaining_percent'] for w in entry['windows']], [25, 60])
        self.assertEqual(entry['source'], 'manual website reading')

    def test_used_percentages_are_converted_and_grok_period_is_explicit(self):
        entry = usage_input.read_manual('claude', ask=Mock(side_effect=['70', '40']))
        self.assertEqual([w['remaining_percent'] for w in entry['windows']], [30, 60])
        entry = usage_input.read_manual('grok', ask=Mock(side_effect=['monthly', '90% used']))
        self.assertEqual((entry['windows'][0]['name'], entry['windows'][0]['remaining_percent']), ('monthly', 10))

    def test_real_prompt_path_uses_stderr_and_does_not_touch_a_browser(self):
        class Terminal(StringIO):
            def isatty(self):
                return True
        errors = StringIO()
        with patch('sys.stdin', Terminal('40\n50\n')), redirect_stderr(errors):
            entry = usage_input.read_manual('codex')
        self.assertEqual(len(entry['windows']), 2)
        self.assertIn('https://', errors.getvalue())
        self.assertIn('percent LEFT', errors.getvalue())

    def test_noninteractive_failure_gives_a_manual_entry_command(self):
        with patch('sys.stdin', StringIO('')):
            with self.assertRaisesRegex(usage_input.ManualUsageRequired, 'claudex usage manual codex'):
                usage_input.read_manual('codex')

    def test_usage_command_recovers_from_failed_read_and_preserves_json_stdout(self):
        class Terminal(StringIO):
            def isatty(self):
                return True
        with tempfile.TemporaryDirectory() as folder:
            out, err = StringIO(), StringIO()
            with patch.object(pane_loop.codex_status, 'read', side_effect=OSError('unavailable')), \
                    patch.object(pane_loop.usage_pages, 'read_usage') as browser, \
                    patch('sys.stdin', Terminal('40% left\n60% left\n')), \
                    redirect_stdout(out), redirect_stderr(err):
                code = pane_loop.main(['--config', str(Path(folder)/'loop.json'),
                                       'usage', 'codex', '--json'])
            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(json.loads(out.getvalue()), {'codex': {'5h': 40, 'weekly': 60}})
            self.assertIn('percent LEFT', err.getvalue())
            browser.assert_not_called()
            cached = json.loads((Path(folder)/'.loop/usage-pages.json').read_text())
            self.assertEqual(cached['providers']['codex']['source'], 'manual website reading')

    def test_manual_command_skips_automatic_readers_and_noninteractive_error_explains_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            out, err = StringIO(), StringIO()
            with patch.object(pane_loop.codex_status, 'read') as automatic, \
                    patch.object(pane_loop.usage_pages, 'read_usage') as browser, \
                    patch('sys.stdin', StringIO('')), redirect_stdout(out), redirect_stderr(err):
                code = pane_loop.main(['--config', str(Path(folder)/'loop.json'),
                                       'usage', 'manual', 'codex', '--full'])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out.getvalue())['providers']['codex']['status'], 'input_required')
            self.assertIn('claudex usage manual codex', err.getvalue())
            automatic.assert_not_called()
            browser.assert_not_called()

    def test_cancel_or_no_values_cannot_invent_usage(self):
        with self.assertRaises(usage_input.ManualUsageRequired):
            usage_input.read_manual('codex', ask=Mock(side_effect=['unknown', 'unknown']))

    def test_worker_usage_error_asks_only_for_the_failed_provider_and_caches_answers(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'usage.json'
            good = {'status': 'ok', 'checked_at': 1000, 'windows': [window('weekly', 20)]}
            manual = Mock(return_value={'status': 'ok', 'checked_at': 1000,
                                        'source': 'manual website reading',
                                        'windows': [window('weekly', 80), window('five_hour', 80)]})
            readers = {'codex': Mock(return_value=good), 'claude': Mock(side_effect=OSError('unavailable'))}
            result = route.run('worker', 'low', cache=cache, cwd=Path(folder), now=1000,
                               readers=readers, manual=manual, dry_run=True)
            manual.assert_called_once()
            self.assertEqual(manual.call_args.args[0], 'claude')
            self.assertEqual(len(result['sessions']), 1)
            self.assertEqual(result['sessions'][0]['agent'], 'codex')
            cached = json.loads(cache.read_text())
            self.assertEqual(cached['providers']['claude']['source'], 'manual website reading')
            route.run('worker', 'low', cache=cache, cwd=Path(folder), now=1001,
                      readers=readers, manual=manual, dry_run=True)
            self.assertEqual(manual.call_count, 1)

    def test_worker_setting_and_cli_override_have_one_provider(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'loop.json'
            path.write_text(json.dumps({'routing': {'worker_provider': 'claude'}}))
            self.assertEqual(route.worker_setting(path), 'claude')
            result = route.run('worker', 'mid', cache=path.parent/'usage.json', cwd=path.parent,
                               provider=route.worker_setting(path), dry_run=True, readers={})
            self.assertEqual([s['agent'] for s in result['sessions']], ['claude'])

    def test_missing_manual_input_stops_before_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            launch = Mock()
            with self.assertRaises(route.RouteError):
                route.run('worker', 'low', cache=Path(folder)/'usage.json', cwd=Path(folder),
                          readers={'codex': lambda: {'status': 'error'}}, launch=launch,
                          manual=Mock(side_effect=usage_input.ManualUsageRequired('cancelled')))
            launch.assert_not_called()
