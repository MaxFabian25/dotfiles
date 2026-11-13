from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import portable  # noqa: E402
import release  # noqa: E402
from release_case import CandidateCase, FAILING_TEST, PASSING_TEST, ProcessReplaced, RUN_ALONE, git, release_main  # noqa: E402
from release_case import skip_on_windows as setUpModule  # noqa: E402, F401


class RoundsTest(CandidateCase):
    def test_pairing_intent_survives_rebase_that_drops_equivalent_public_work(self):
        self.prepare()
        git(self.mine, 'commit', '-qam', 'This release owns the public change')
        self.push_from_another_session(self.public_origin, 'equivalent', {'content': 'second\n'})
        self.assertEqual(release_main([*self.argv, '--push', '--overlap-reviewed'], self.public), 0)
        self.assertIn(self.origin_heads()[0], git(self.private_origin, 'show', 'main:release.toml'))

    def test_tests_run_without_machine_lock_after_source_commits(self):
        self.prepare()
        lock = self.root / 'state/release.lock'
        suite, commit, push = release.test_suite, release.commit, release.push
        observed = []

        def under_lock(operation):
            def run(*args):
                with self.assertRaises(BlockingIOError):
                    with portable.exclusive_lock(lock):
                        pass
                return operation(*args)
            return run

        def outside(public, private, log):
            with portable.exclusive_lock(lock):
                self.assertEqual(git(self.mine, 'show', 'HEAD:content'), 'second')
                self.assertEqual(git(public, 'rev-parse', 'HEAD'), git(self.mine, 'rev-parse', 'HEAD'))
                self.assertEqual(public.parent, private.parent)
                self.assertNotEqual(public, self.mine)
                self.assertEqual(git(public, 'rev-parse', '--abbrev-ref', 'HEAD'), 'HEAD')
                observed.append('outside')
            suite(public, private, log)

        with patch.object(release, 'commit', under_lock(commit)), patch.object(release, 'push', under_lock(push)), \
                patch.object(release, 'test_suite', outside):
            self.assertEqual(release_main([*self.argv, '--push'], self.public), 0)
        self.assertEqual(observed, ['outside'])

    def test_remote_advances_during_tests_wait_or_dry_run_are_retested(self):
        for boundary in ('suite', 'wait', 'dry-run'):
            with self.subTest(boundary=boundary):
                self.prepare(boundary)
                (self.mine / 'content').write_text(f'{boundary}\n', encoding='utf-8')
                suite, hold, run_git = release.test_suite, release.hold, release.git
                observed, moved = [], []

                def advance():
                    if not moved:
                        self.push_from_another_session(self.private_origin, f'other-{boundary}',
                                                       {f'{boundary}.md': 'other work\n'})
                        moved.append(True)

                def record(public, private, log):
                    suite(public, private, log)
                    observed.append((git(public, 'rev-parse', 'HEAD^{tree}'), git(private, 'rev-parse', 'HEAD^{tree}')))
                    if boundary == 'suite':
                        advance()

                def during_wait(*args, **kwargs):
                    if observed and boundary == 'wait':
                        advance()
                    return hold(*args, **kwargs)

                def during_dry_run(root, *args):
                    if 'push' in args and '--dry-run' in args and boundary == 'dry-run':
                        advance()
                    return run_git(root, *args)

                with patch.object(release, 'test_suite', record), patch.object(release, 'hold', during_wait), \
                        patch.object(release, 'git', during_dry_run):
                    self.assertEqual(release_main([*self.argv, '--push'], self.public), 0)
                self.assertEqual(len(observed), 2)
                self.assertNotEqual(observed[0], observed[1])
                self.assertEqual(observed[-1], tuple(git(root, 'rev-parse', 'main^{tree}')
                                                    for root in (self.public_origin, self.private_origin)))

    def test_origin_checked_for_overlap_is_the_origin_rebased_onto(self):
        self.prepare()
        self.push_from_another_session(self.public_origin, 'first', {'tests/first.md': 'before check\n'})
        checked, rebased, advanced = [], [], []
        origin_stop, rebase = release.origin_stop, release.rebase

        def check(*args, **kwargs):
            result = origin_stop(*args, **kwargs)
            checked.append(kwargs['origins'].public)
            if not advanced:
                self.push_from_another_session(self.public_origin, 'next', {'tests/next.md': 'after check\n'})
                git(self.mine, 'fetch', '-q', 'origin')
                advanced.append(True)
            return result

        def replay(root, regenerated, *, onto, paired=False):
            if root == self.mine:
                rebased.append(onto)
            return rebase(root, regenerated, onto=onto, paired=paired)

        with patch.object(release, 'origin_stop', check), patch.object(release, 'rebase', replay):
            self.assertEqual(release_main([*self.argv, '--push'], self.public), 0)
        self.assertEqual(rebased[0], checked[0])
        self.assertNotEqual(rebased[0], rebased[-1])
        self.assertEqual(git(self.public_origin, 'show', 'main:tests/next.md'), 'after check')

    def test_continuous_origin_movement_exhausts_three_rounds_without_pushing(self):
        self.prepare()
        suite, runs = release.test_suite, []
        before = self.origin_heads()[0]

        def move(public, private, log):
            suite(public, private, log)
            self.push_from_another_session(self.private_origin, f'other-{len(runs)}', {f'round-{len(runs)}': 'moved\n'})
            runs.append(True)

        with patch.object(release, 'test_suite', move), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main([*self.argv, '--push'], self.public), 3)
        self.assertEqual(len(runs), 3)
        self.assertIn('changed through all 3 rounds', error.getvalue())
        self.assertIn('Local commits remain at public', error.getvalue())
        self.assertIn('Finish again', error.getvalue())
        self.assertEqual(self.origin_heads()[0], before)
        for lock in (self.root / 'state/release.lock', release.finish_lock(self.base)):
            with portable.exclusive_lock(lock):
                pass

    def test_remote_retries_and_engine_reload_share_one_budget_hidden_from_tests(self):
        self.prepare()
        suite, seen, reloads = release.test_suite, [], []

        def move(public, private, log):
            self.assertNotIn(release.ROUND_BUDGET, os.environ)
            suite(public, private, log)
            seen.append(True)
            if len(seen) == 1:
                self.push_from_another_session(self.private_origin, 'private-next', {'next': 'moved\n'})
            else:
                self.push_from_another_session(self.public_origin, 'public-next', {'doc': 'moved\n'})
                git(self.public, 'pull', '-q', '--ff-only')

        def restart(program, argv, environment):
            reloads.append(environment[release.ROUND_BUDGET])
            raise ProcessReplaced()

        with patch.object(release, 'test_suite', move), patch.object(release.os, 'execve', restart):
            with self.assertRaises(ProcessReplaced):
                release_main([*self.argv, '--push'], self.public)
        self.assertEqual(reloads, ['1'])
        with patch.dict(os.environ, {release.ROUND_BUDGET: reloads[0]}), \
                patch.object(release, 'running', return_value=['pull running']), \
                patch.object(release, 'wait', return_value=True), \
                patch.object(release.os, 'execve', side_effect=AssertionError('fourth round')), \
                redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(self.argv, self.public), 3)
            self.assertNotIn(release.ROUND_BUDGET, os.environ)
        self.assertIn('changed through all 3 rounds', error.getvalue())

    def test_second_hold_timeout_keeps_commits_receipt_and_reuses_unchanged_evidence(self):
        self.prepare()
        tested = release.tested
        lock = portable.exclusive_lock(self.root / 'state/release.lock', record=True)

        def busy(*args):
            result = tested(*args)
            lock.__enter__()
            return result

        try:
            with patch.object(release, 'tested', busy), redirect_stderr(io.StringIO()) as error:
                self.assertEqual(release_main([*self.argv, '--push', '--timeout', '0'], self.public), 3)
        finally:
            lock.__exit__(None, None, None)
        self.assertIn('Local commits remain', error.getvalue())
        self.assertNotIn('committed nothing', error.getvalue())
        self.assertTrue(release.tests_passed(self.base).is_file())
        with patch.object(release, 'test_suite', side_effect=AssertionError('receipt not reused')):
            self.assertEqual(release_main([*self.argv, '--push'], self.public), 0)

    def test_failed_source_commit_fix_folds_only_same_message_unpublished_ordinary_tip(self):
        self.prepare()
        module = self.mine / 'tests/test_added.py'
        module.write_text(FAILING_TEST, encoding='utf-8')
        before = git(self.mine, 'rev-parse', 'HEAD')
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(self.argv, self.public), 3)
        self.assertIn('test suite failed', error.getvalue())
        self.assertNotIn('committed nothing', error.getvalue())
        failed = git(self.mine, 'rev-parse', 'HEAD')
        module.write_text(PASSING_TEST, encoding='utf-8')
        self.assertEqual(release_main([*self.argv, '--push'], self.public), 0)
        self.assertEqual(git(self.public_origin, 'rev-list', '--count', f'{before}..main'), '1')
        self.assertNotEqual(git(self.public_origin, 'rev-parse', 'main'), failed)
        (self.mine / 'content').write_text('third\n', encoding='utf-8')
        self.assertEqual(release_main([*self.argv, '--push'], self.public), 0)
        self.assertEqual(git(self.public_origin, 'rev-list', '--count', f'{before}..main'), '2')

    def test_upstream_markdown_dependency_is_tested_and_can_stop_a_retry(self):
        self.push_from_another_session(self.private_origin, 'seed-guide', {
            'guide.md': 'good\n', 'test_guide.py':
            'from pathlib import Path\nimport unittest\nclass Guide(unittest.TestCase):\n'
            '    def test_guide(self):\n'
            '        self.assertEqual(Path(__file__).with_name("guide.md").read_text(), "good\\n")\n' + RUN_ALONE})
        self.prepare()
        suite, seen = release.test_suite, []
        original = self.origin_heads()[0]

        def move(*args):
            suite(*args)
            seen.append(True)
            self.push_from_another_session(self.private_origin, 'bad-guide', {'guide.md': 'bad\n'})

        with patch.object(release, 'test_suite', move), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main([*self.argv, '--push'], self.public), 3)
        self.assertEqual(seen, [True])
        self.assertIn('test_guide.py failed (test_guide)', error.getvalue())
        self.assertEqual(self.origin_heads()[0], original)
        self.assertIn('bad', (self.base / 'build/private-tests.log').read_text())


if __name__ == '__main__':
    unittest.main()
