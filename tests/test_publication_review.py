"""Regressions for launch boundaries, status input and private runtime state."""
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

import claude_status
import claudex_team
import codex_status
import grok_status
import limits
import pane_loop
import route
import status_session
import usage_pages
from test_claude_status import Session as CompanionSession, Clock as CompanionClock
from test_codex_status import Session as CodexSession, Clock as CodexClock


class LaunchBoundaryTests(unittest.TestCase):
    def test_cli_delivers_exact_arguments_to_a_fake_kilix_process(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cwd = root / 'work with spaces'
            cwd.mkdir()
            (cwd / 'space file.py').touch()
            fake = root / 'kilix'
            fake.write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
                            'with open(os.environ["REVIEW_ARGV"],"a") as out:\n'
                            ' out.write(json.dumps(sys.argv[1:])+"\\n")\n'
                            'print(json.dumps({"pane":{"pane_id":101}}))\n')
            fake.chmod(0o700)
            prompt = '--help fix "./space file.py"\r\n  café $HOME `literal`  '
            log = root / 'argv.jsonl'
            env = {'PATH': str(root) + ':/usr/bin:/bin', 'HOME': str(root),
                   'LANG': 'C.UTF-8', 'REVIEW_ARGV': str(log)}
            done = subprocess.run([sys.executable, '-B', pane_loop.__file__, '--config', str(root/'loop.json'),
                                   'route', '--json', '--cwd', str(cwd), 'worker', 'low', '--', prompt],
                                  env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len(calls), 2)
            for args in calls:
                self.assertEqual(args[:3], ['session', 'grid', 'add'])
                self.assertEqual(args[args.index('--cwd') + 1], str(cwd.resolve()))
                self.assertEqual(args[-2:], ['--', prompt])

    def test_prompt_is_one_literal_argument_after_option_separator(self):
        prompt = '--help fix "./file with spaces.txt"\r\n  keep $HOME and `literal`  '
        for agent, model in (("codex", route.ASTRA), ("claude", route.OPUS_55), ("grok", "example")):
            with self.subTest(agent=agent):
                session = route._session(agent, model, "high", title="example")
                forwarded = route._with_prompt([session], prompt)[0]["argv"]
                self.assertEqual(forwarded[-2:], ["--", prompt])

    def test_grid_preserves_working_directory_and_prompt(self):
        calls = []
        cwd = Path('/tmp/work with spaces')
        prompt = 'fix ./code.py\r\n  exact whitespace  '
        sessions = route._with_prompt(route.choose_worker("low")["sessions"], prompt)
        def launch(*args):
            calls.append(args)
            return {"pane": {"pane_id": 100 + len(calls)}}
        route.open_sessions(sessions, cwd=cwd, launch=launch)
        self.assertEqual(calls[0][:5], ("grid", "add", "--name", "claudex-route", "--new"))
        self.assertEqual(calls[1][calls[1].index("--into") + 1], "101")
        for args in calls:
            self.assertEqual(args[args.index("--cwd") + 1], str(cwd))
            self.assertEqual(args[-2:], ("--", prompt))

    def test_partial_launch_failure_names_confirmed_panes(self):
        sessions = route.choose_worker("low")["sessions"]
        with self.assertRaisesRegex(route.RouteError, r"\[101\].*inspect"):
            route.open_sessions(sessions, cwd=Path('/tmp'), launch=unittest.mock.Mock(
                side_effect=[{"pane": {"pane_id": 101}}, route.RouteError("failed")]))

    def test_receipt_rejects_boolean_id(self):
        with self.assertRaises(route.RouteError):
            route.open_sessions(route.choose_worker("low")["sessions"], cwd=Path('/tmp'),
                                launch=lambda *args: {"pane": {"pane_id": True}})

    def test_invalid_team_listing_cannot_bypass_duplicate_check(self):
        client = claudex_team.Kilix()
        with patch.object(client, "run", return_value='{"error":"unavailable"}'):
            with self.assertRaises(claudex_team.TeamError):
                client.ls()


class UsageStateTests(unittest.TestCase):
    def test_reset_and_future_cache_timestamps_are_not_fresh(self):
        self.assertFalse(route.fresh({"checked_at": 1001}, 1000))
        self.assertFalse(route.fresh({"checked_at": 999, "windows": [{"resets_at": 1000}]}, 1000))
        self.assertTrue(route.fresh({"checked_at": 999, "windows": [{"resets_at": 1001}]}, 1000))

    def test_monthly_grok_exhaustion_uses_fallback(self):
        entry = {"status": "ok", "windows": [{"name": "monthly", "used_percent": 100, "resets_at": 1100}]}
        decision = route.choose_security({"providers": {"grok": entry}}, now=1000)
        self.assertNotEqual(decision["sessions"][0]["agent"], "grok")

    def test_cache_recovers_from_non_object_and_keeps_newer_reading(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cache.json'
            path.write_text('[]')
            newer = {"status": "ok", "checked_at": 2000}
            usage_pages.merge_cache(path, {"providers": {"codex": newer}})
            merged = usage_pages.merge_cache(path, {"providers": {"codex": {"checked_at": 1000},
                                                                   "grok": {"checked_at": 2001}}})
            self.assertEqual(merged['providers']['codex'], newer)
            self.assertIn('grok', merged['providers'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_nonfinite_usage_is_unknown(self):
        for value in (float('nan'), float('inf'), True):
            self.assertIsNone(usage_pages.window('weekly', value)['used_percent'])
            self.assertIsNone(limits._number(value))

    def test_new_writes_in_old_rollout_directories_are_not_missed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index in range(24):
                path = root / 'sessions/2026/10/29' / f'rollout-{index}.jsonl'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                os.utime(path, (1000, 1000))
            old = root / 'sessions/2026/01/01/rollout-long-running.jsonl'
            old.parent.mkdir(parents=True)
            old.touch()
            os.utime(old, (2000, 2000))
            self.assertEqual(limits.recent_codex_rollouts(root)[0], old)

    def test_grok_never_follows_authenticated_redirects(self):
        request = grok_status.urllib.request.Request('https://example.invalid', headers={'Authorization': 'Bearer fixture'})
        redirect = grok_status._NoRedirect()
        self.assertIsNone(redirect.redirect_request(request, None, 302, 'redirect', {}, 'https://elsewhere.invalid'))

    def test_grok_error_does_not_echo_credential_details(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'auth.json').write_text(json.dumps({'account': {'key': 'fixture-secret'}}))
            result = grok_status.read(home=path, fetch=unittest.mock.Mock(side_effect=ValueError('fixture-secret')))
            self.assertEqual(result['status'], 'error')
            self.assertNotIn('fixture-secret', json.dumps(result))


class StatusInputTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.env = patch.dict(os.environ, {'XDG_CACHE_HOME': self.folder.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def read(self, module, session):
        clock = CodexClock()
        return module.read(run=session.run, commands_for=session.commands_for,
                           clock=clock, sleep=clock.sleep, now=1000)

    def test_wait_never_returns_a_rejected_last_screen(self):
        for module, clock in ((codex_status, CodexClock()), (claude_status, CompanionClock())):
            with self.subTest(module=module.__name__):
                with self.assertRaises(Exception):
                    module._wait(lambda: 'stale composer', lambda text: False, 1,
                                 clock=clock, sleep=clock.sleep)

    def test_existing_drafts_are_not_sent_or_submitted(self):
        cases = [(codex_status, CodexSession(command='codex'), '› my draft\n footer\n'),
                 (claude_status, CompanionSession(command='claude'), '────────────\n❯ my draft\n────────────\n')]
        for module, session, screen in cases:
            with self.subTest(module=module.__name__):
                session.phase = 'ready'
                session.screens['ready'] = screen
                result = self.read(module, session)
                self.assertEqual(result['status'], 'error')
                self.assertFalse(any(call[0] in {'send', 'key', 'type'} for call in session.calls))

    def test_malformed_tmux_metadata_returns_provider_error(self):
        for module in (codex_status, claude_status):
            with self.subTest(module=module.__name__):
                result = module.read(run=lambda args: codex_status.subprocess.CompletedProcess(args, 0, '[]', ''))
                self.assertEqual(result['status'], 'error')

    def test_reader_lock_refuses_overlap_without_touching_tmux(self):
        with status_session.reader_lock(codex_status.SESSION):
            run = unittest.mock.Mock()
            result = codex_status.read(run=run)
        self.assertEqual(result['status'], 'error')
        run.assert_not_called()

    def test_pane_replacement_blocks_the_next_write(self):
        session = CodexSession(command='codex')
        session.phase = 'ready'
        transport = status_session.Transport(session.run, codex_status.SESSION, 'codex', lambda p: True)
        transport(['show', '--json', codex_status.SESSION])
        transport.bound = ('%other', 9, '0', '0')
        with self.assertRaises(status_session.StatusSessionError):
            transport(['send', codex_status.SESSION, '/status'])
        self.assertFalse(any(call[0] == 'send' for call in session.calls))

    def test_empty_second_status_is_an_error(self):
        session = CodexSession(command='codex')
        session.phase = 'ready'
        session.screens['status2'] = '› \n footer\n'
        self.assertEqual(self.read(codex_status, session)['status'], 'error')

    def test_old_status_scrollback_does_not_turn_a_draft_into_a_dialog(self):
        self.assertFalse(claude_status.status_open(
            'Settings Status Usage\nEsc to cancel\n────────────\n❯ draft\n────────────\n'))


class LifecycleTests(unittest.TestCase):
    def test_interrupt_after_typing_keeps_uncertain_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            evidence = {'text_attempted': True, 'token': 'cx-fixture'}
            with self.assertRaises(pane_loop.DeliveryError):
                with pane_loop.record_attempt(Path(folder), evidence):
                    raise KeyboardInterrupt()
            saved = json.loads((Path(folder) / 'last-send.json').read_text())
            self.assertEqual(saved['status'], 'uncertain')

    def test_second_dashboard_cannot_replace_live_process_record(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            with pane_loop.exclusive(runtime / 'dashboard.lock'):
                with patch.object(pane_loop.dashboard, 'make_server') as make:
                    with self.assertRaises(pane_loop.Busy):
                        pane_loop.serve_dashboard(runtime, None, 0, [], 10)
                    make.assert_not_called()


if __name__ == '__main__':
    unittest.main()
