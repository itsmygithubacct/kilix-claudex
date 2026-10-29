import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import route
import team


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.spec = self.root / 'team.json'
        for index in range(12):
            (self.root / f'doc {index}.txt').write_text('Synthetic assignment\n')

    def write(self, rows, defaults=None):
        self.spec.write_text(json.dumps({'name': 'review-grid', 'defaults': defaults or {}, 'sessions': rows}))
        return self.spec

    def plan(self, rows, defaults=None, **kwargs):
        return team.make_plan(self.write(rows, defaults), cache=self.root / 'usage.json', **kwargs)

    def test_twelve_sessions_use_one_grid_and_each_receives_its_document(self):
        rows = [{'title': f'worker-{i}', 'document': f'doc {i}.txt'} for i in range(12)]
        plan = self.plan(rows, {'provider': 'codex', 'model': 'gpt-6.1-terra', 'effort': 'high',
                                'prompt': 'Shared instructions'})
        calls = []
        def launch(*args):
            calls.append(args)
            return {'pane': {'pane_id': 100 + len(calls)}}
        result = team.launch(plan, launcher=launch, titles=[])
        self.assertEqual(len(result['window_ids']), 12)
        self.assertEqual(calls[0][4], '--new')
        for i, args in enumerate(calls):
            self.assertEqual(args[:4], ('grid', 'add', '--name', 'review-grid'))
            self.assertEqual(args[args.index('--cwd')+1], str(self.root))
            self.assertIn(f'doc {i}.txt', args[-1])
            self.assertTrue(args[-1].startswith('Shared instructions\n\n'))
            if i:
                self.assertEqual(args[4:6], ('--into', '101'))
            self.assertNotIn('--no-hold', args)

    def test_session_overrides_preserve_prompt_and_addon(self):
        base = '  shared\r\n'
        unique = '  unique\r\nsecond line  '
        addon = '\tadditional instruction  '
        plan = self.plan([{'document': 'doc 0.txt'},
                          {'provider': 'claude', 'model': 'claude-sonnet-5-5', 'effort': 'medium',
                           'document': 'doc 1.txt', 'prompt': unique, 'prompt_addon': addon}],
                         {'provider': 'codex', 'model': 'gpt-6.1-terra', 'effort': 'high', 'prompt': base})
        first, second = plan['sessions']
        self.assertEqual(first['prompt'], base)
        self.assertEqual((second['agent'], second['model'], second['effort']),
                         ('claude', 'claude-sonnet-5-5', 'medium'))
        self.assertEqual(second['prompt'], unique)
        self.assertEqual(second['prompt_addon'], addon)
        self.assertTrue(second['argv'][-1].startswith(unique + '\n\n'))
        self.assertTrue(second['argv'][-1].endswith('\n\n' + addon))
        self.assertNotIn(base, second['argv'][-1])
        self.assertEqual(second['argv'][-2], '--')

    def test_relative_paths_use_plan_directory_and_each_session_cwd(self):
        other = self.root / 'other project'
        other.mkdir()
        (other / 'assignment.txt').touch()
        plan = self.plan([{'cwd': 'other project', 'document': 'assignment.txt'}], {'provider': 'codex'})
        self.assertEqual(plan['sessions'][0]['cwd'], str(other))
        self.assertEqual(plan['sessions'][0]['document'], str(other / 'assignment.txt'))

    def test_all_sessions_validate_before_account_reads_or_launches(self):
        resolver = Mock()
        with self.assertRaisesRegex(team.TeamError, 'existing file'):
            self.plan([{'document': 'doc 0.txt'}, {'document': 'missing.txt'}], resolver=resolver)
        resolver.assert_not_called()

    def test_later_title_exceeding_kilix_limits_is_rejected_before_account_reads(self):
        for title in ('x' * 201, '🙂' * 51, 'bad\x85title'):
            resolver = Mock()
            with self.subTest(title=title), self.assertRaisesRegex(team.TeamError, '200 UTF-8 bytes'):
                self.plan([{}, {'title': title}], resolver=resolver)
            resolver.assert_not_called()

    def test_auto_selection_uses_settings_once_for_each_tier(self):
        resolver = Mock(return_value=route.choose_worker('low', 'claude'))
        plan = self.plan([{}, {}], worker_provider='claude', resolver=resolver)
        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(resolver.call_args.kwargs['provider'], 'claude')
        self.assertEqual([s['agent'] for s in plan['sessions']], ['claude', 'claude'])

    def test_explicit_models_do_not_read_usage(self):
        resolver = Mock()
        self.plan([{}], {'provider': 'codex', 'model': 'gpt-6-astra', 'effort': 'max'}, resolver=resolver)
        resolver.assert_not_called()

    def test_zero_or_thirteen_sessions_are_rejected(self):
        for count in (0, 13):
            with self.subTest(count=count), self.assertRaisesRegex(team.TeamError, '1–12'):
                self.plan([{} for _ in range(count)], {'provider': 'codex'})

    def test_duplicate_titles_and_documents_are_rejected(self):
        (self.root / 'alias.txt').symlink_to(self.root / 'doc 0.txt')
        for rows in ([{'title': 'same'}, {'title': 'same'}],
                     [{'document': 'doc 0.txt'}, {'document': 'alias.txt'}]):
            with self.assertRaisesRegex(team.TeamError, 'unique'):
                self.plan(rows, {'provider': 'codex'})

    def test_unknown_fields_and_ambiguous_custom_models_are_rejected(self):
        for row in ({'effrot': 'high'}, {'model': 'custom'}, {'provider': 'missing'}, {'effort': '--help'}):
            with self.subTest(row=row), self.assertRaises(team.TeamError):
                self.plan([row])

    def test_existing_titles_block_launch_before_any_pane_is_opened(self):
        plan = self.plan([{'title': 'taken'}], {'provider': 'codex'})
        launch = Mock()
        with self.assertRaisesRegex(team.TeamError, 'already exist'):
            team.launch(plan, launcher=launch, titles=['taken'])
        launch.assert_not_called()

    def test_partial_failure_reports_open_panes_and_stops(self):
        plan = self.plan([{}, {}, {}], {'provider': 'codex'})
        launch = Mock(side_effect=[{'pane': {'pane_id': 101}}, route.RouteError('failure')])
        with self.assertRaisesRegex(route.RouteError, r'\[101\].*inspect'):
            team.launch(plan, launcher=launch, titles=[])
        self.assertEqual(launch.call_count, 2)


if __name__ == '__main__':
    unittest.main()
