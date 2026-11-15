from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import os
from pathlib import Path
import platform
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402
from release_case import ReleaseCase, git, release_main  # noqa: E402
from release_case import skip_on_windows as setUpModule  # noqa: E402, F401

MAIN_CI = release.main_ci  # ReleaseCase.setUp replaces it with a green run
RUN = 43499226815
STEP = 'Run uv run --locked python -B tests/run.py'
RED = release.RedRun(RUN, '6e813e03e7d484f2b046e94fe1f9a81fdf8f841e',
                     f'https://github.com/MaxFabian25/dotfiles/actions/runs/{RUN}', (f'test (macos-latest), step {STEP}',))


def listed(*runs):
    """Runs as gh run list prints them, newest first, each at its first attempt unless it names another; an
    unfinished attempt's conclusion is empty."""
    return [{'databaseId': number, 'attempt': attempt, 'conclusion': conclusion, 'headSha': f'{number:040d}',
             'url': f'https://github.com/MaxFabian25/dotfiles/actions/runs/{number}'}
            for number, conclusion, attempt in (run if len(run) == 3 else (*run, 1) for run in runs)]


def canned(runs, attempts=None):
    """gh_json as GitHub answers it: the run list, and each attempt the test names, by run and attempt number, as
    its conclusion and jobs."""
    def answer(public_repo, *args):
        if args[:2] == ('run', 'list'):
            return runs
        if args[:2] == ('run', 'view') and args[3] == '--attempt':
            conclusion, jobs = attempts[int(args[2]), int(args[4])]
            return {'conclusion': conclusion, 'jobs': jobs}
        raise AssertionError(f'gh asked for {args}')
    return answer


class CiGateTest(ReleaseCase):
    def start(self, name, *extra):
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
            status = release_main(['start', name, *extra, *self.options()], self.public)
        return status, output.getvalue(), error.getvalue()

    def assertNothingStarted(self, name):
        self.assertFalse((self.worktrees / name).exists())
        self.assertEqual(git(self.public, 'branch', '--list', name), '')
        self.assertEqual(git(self.private, 'branch', '--list', name), '')

    def test_start_on_a_red_main_stops_with_the_job_the_run_and_both_ways_on_and_creates_nothing(self):
        with patch.object(release, 'main_ci', return_value=RED):
            status, _, error = self.start('blocked')

        self.assertEqual(status, 3, error)
        for text in (f"main's tests failed in run {RUN} at commit 6e813e0: test (macos-latest), step {STEP}",
                     f'ci_log.py MaxFabian25/dotfiles {RUN}',
                     f'gh run rerun {RUN} --failed --repo MaxFabian25/dotfiles',
                     f'gh run watch {RUN} --repo MaxFabian25/dotfiles --exit-status',
                     f'release.py start NAME --repairs {RUN}'):
            self.assertIn(text, error)
        self.assertTrue(error.rstrip().endswith('; nothing was started'), error)
        self.assertNothingStarted('blocked')

    def test_start_on_a_green_main_names_the_run_that_passed(self):
        status, output, error = self.start('green')

        self.assertEqual(status, 0, error)
        self.assertIn("main's tests passed in run 1 at commit 0000000", output)
        self.assertTrue((self.worktrees / 'green/public').is_dir())

    def test_repairs_naming_the_red_run_starts_and_notes_the_run(self):
        with patch.object(release, 'main_ci', return_value=RED):
            status, _, error = self.start('mend', '--repairs', str(RUN))

        self.assertEqual(status, 0, error)
        self.assertTrue((self.worktrees / 'mend/public').is_dir())
        self.assertEqual(release.repairs_note(self.worktrees / 'mend').read_text(encoding='utf-8'), f'{RUN}\n')

    def test_start_without_repairs_drops_the_note_of_an_abandoned_release_of_the_same_name(self):
        release.repairs_note(self.worktrees / 'reused').parent.mkdir(parents=True)
        release.repairs_note(self.worktrees / 'reused').write_text(f'{RUN}\n', encoding='utf-8')

        status, _, error = self.start('reused')

        self.assertEqual(status, 0, error)
        self.assertFalse(release.repairs_note(self.worktrees / 'reused').exists())

    def test_repairs_naming_another_run_is_refused_with_the_newest_red_run(self):
        with patch.object(release, 'main_ci', return_value=RED):
            status, _, error = self.start('stale', '--repairs', '111')

        self.assertEqual(status, 3, error)
        self.assertIn(f'--repairs names run 111, but the newest red run on main is {RUN}', error)
        self.assertNothingStarted('stale')

    def test_repairs_on_a_green_main_is_refused(self):
        status, _, error = self.start('needless', '--repairs', '-1')

        self.assertEqual(status, 3, error)
        self.assertIn("--repairs names run -1, but main's tests passed in run 1; start without --repairs", error)
        self.assertNothingStarted('needless')

    def test_the_refusal_names_each_open_release_that_repairs_the_run_with_its_thread(self):
        os.environ['T3CODE_THREAD_ID'] = 'thread-fixer'
        with patch.object(release, 'main_ci', return_value=RED):
            self.assertEqual(self.start('fixer', '--repairs', str(RUN))[0], 0)
            os.environ['T3CODE_THREAD_ID'] = 'thread-waiter'
            status, _, error = self.start('waiter')

        self.assertEqual(status, 3, error)
        self.assertIn(f'Open release fixer already repairs run {RUN} (T3 thread thread-fixer); wait for its finish',
                      error)
        self.assertNothingStarted('waiter')

    def test_main_ci_takes_the_newest_run_that_passed_past_cancelled_and_unfinished_ones(self):
        runs = listed((5, ''), (4, 'cancelled'), (3, 'success'), (2, 'failure'))

        with patch.object(release, 'gh_json', canned(runs)):
            self.assertEqual(MAIN_CI(self.public), release.PassedRun(3, f'{3:040d}'))

    def test_a_failed_run_being_rerun_stays_red_with_the_jobs_of_the_attempt_before(self):
        runs = listed((200, '', 2), (199, 'success'))
        failed = [{'name': 'test (macos-latest)', 'conclusion': 'failure', 'steps': [{'name': STEP, 'conclusion': 'failure'}]}]

        with patch.object(release, 'gh_json', canned(runs, {(200, 1): ('failure', failed)})):
            latest = MAIN_CI(self.public)

        self.assertEqual(latest, release.RedRun(200, f'{200:040d}', 'https://github.com/MaxFabian25/dotfiles/actions/runs/200',
                                                (f'test (macos-latest), step {STEP}',)))

    def test_a_passed_run_being_rerun_stays_green(self):
        runs = listed((200, '', 3), (199, 'failure'))

        with patch.object(release, 'gh_json', canned(runs, {(200, 2): ('success', [])})):
            self.assertEqual(MAIN_CI(self.public), release.PassedRun(200, f'{200:040d}'))

    def test_a_rerun_is_judged_by_its_newest_attempt_that_passed_or_failed_past_a_cancelled_one(self):
        for first, older, judged in (('failure', 'success', 'red'), ('success', 'failure', 'green')):
            with self.subTest(first_attempt=first):
                runs = listed((200, '', 3), (199, older))
                attempts = {(200, 2): ('cancelled', []), (200, 1): (first, [
                    {'name': 'test (macos-latest)', 'conclusion': first, 'steps': [{'name': STEP, 'conclusion': first}]}])}

                with patch.object(release, 'gh_json', canned(runs, attempts)):
                    latest = MAIN_CI(self.public)

                self.assertEqual(latest.run, 200)
                self.assertIsInstance(latest, release.RedRun if judged == 'red' else release.PassedRun)

    def test_a_rerun_that_failed_is_red_whatever_the_attempt_before_it_did(self):
        runs = listed((200, 'failure', 2), (199, 'success'))
        failed = [{'name': 'test (macos-latest)', 'conclusion': 'failure', 'steps': [{'name': STEP, 'conclusion': 'failure'}]}]

        with patch.object(release, 'gh_json', canned(runs, {(200, 2): ('failure', failed), (200, 1): ('success', [])})):
            self.assertEqual(MAIN_CI(self.public).jobs, (f'test (macos-latest), step {STEP}',))

    def test_main_ci_reads_a_run_that_timed_out_as_red(self):
        runs = listed((3, 'timed_out'), (2, 'success'))
        jobs = [{'name': 'test (macos-latest)', 'conclusion': 'timed_out', 'steps': []}]

        with patch.object(release, 'gh_json', canned(runs, {(3, 1): ('timed_out', jobs)})):
            self.assertEqual(MAIN_CI(self.public).jobs, ('test (macos-latest), timed_out',))

    def test_main_ci_names_the_failed_jobs_and_their_failed_steps_of_a_red_run(self):
        runs = listed((7, ''), (6, 'cancelled'), (5, 'failure'), (4, 'success'))
        jobs = [{'name': 'test (macos-latest)', 'conclusion': 'failure',
                     'steps': [{'name': 'Set up job', 'conclusion': 'success'}, {'name': STEP, 'conclusion': 'failure'}]},
                    {'name': 'test (windows-latest)', 'conclusion': 'success',
                     'steps': [{'name': STEP, 'conclusion': 'success'}]},
                    {'name': 'test (ubuntu-latest)', 'conclusion': 'timed_out',
                     'steps': [{'name': STEP, 'conclusion': 'cancelled'}]}]

        with patch.object(release, 'gh_json', canned(runs, {(5, 1): ('failure', jobs)})):
            self.assertEqual(MAIN_CI(self.public), release.RedRun(
                5, f'{5:040d}', 'https://github.com/MaxFabian25/dotfiles/actions/runs/5',
                (f'test (macos-latest), step {STEP}', 'test (ubuntu-latest), timed_out')))

    def test_a_gh_that_fails_or_prints_no_json_stops_start_before_anything_is_made(self):
        programs = self.root / 'programs'
        programs.mkdir()
        gh = programs / 'gh'
        os.environ['PATH'] = f'{programs}{os.pathsep}{os.environ["PATH"]}'
        for name, script, told in (('unauthorised', 'echo "HTTP 401: Bad credentials" >&2\nexit 1', 'Bad credentials'),
                                   ('garbled', 'echo "<html>"', 'printed no JSON')):
            with self.subTest(gh=name):
                gh.write_text(f'#!/bin/sh\n{script}\n', encoding='utf-8')
                gh.chmod(0o755)
                with patch.object(release, 'main_ci', MAIN_CI):
                    status, _, error = self.start(name)

                self.assertEqual(status, 3, error)
                self.assertIn(told, error)
                self.assertIn('check gh auth status and the network; nothing was started', error)
                self.assertNothingStarted(name)

    def test_a_pushed_finish_adds_one_row_and_a_finish_after_publication_adds_none(self):
        os.environ['T3CODE_THREAD_ID'] = 'thread-mend'
        with patch.object(release, 'main_ci', return_value=RED):
            self.assertEqual(self.start('mend', '--repairs', str(RUN))[0], 0)
        (self.worktrees / 'mend/public/content').write_text('second\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        argv = ['finish', 'mend', '--message', str(message), '--push', *self.options()]
        log = self.root / 'state/releases.tsv'

        self.assertEqual(release_main(argv, self.public), 0)
        self.assertEqual(release_main(argv, self.public), 0)

        header, *rows = log.read_text(encoding='utf-8').splitlines()
        self.assertEqual(header.split('\t'), ['finished', 'machine', 'name', 'public', 'private', 'repairs', 'thread'])
        self.assertEqual(len(rows), 1, rows)
        finished, *rest = rows[0].split('\t')
        self.assertIsNotNone(datetime.fromisoformat(finished).utcoffset())
        self.assertEqual(rest, [platform.node(), 'mend', *self.origin_heads(), str(RUN), 'thread-mend'])

    def test_later_releases_add_rows_under_one_header_and_a_private_only_one_leaves_public_empty(self):
        args = SimpleNamespace(worktrees=self.worktrees, name='later')
        with patch.object(release, 'STATE', self.root / 'state'):
            for public, private, paired in (('a' * 40, 'b' * 40, True), ('a' * 40, 'c' * 40, False)):
                release.log_release(args, SimpleNamespace(commits=release.Heads(public, private), paired=paired))

        header, *rows = (self.root / 'state/releases.tsv').read_text(encoding='utf-8').splitlines()
        self.assertEqual(header, '\t'.join(release.RELEASES_COLUMNS))
        self.assertEqual([row.split('\t')[2:5] for row in rows], [['later', 'a' * 40, 'b' * 40], ['later', '', 'c' * 40]])

    def test_the_rows_already_there_are_kept_byte_for_byte_whatever_they_hold(self):
        log = self.root / 'state/releases.tsv'
        log.parent.mkdir(parents=True)
        before = '\t'.join(release.RELEASES_COLUMNS).encode() + b'\n' + b'\t'.join(
            (b't', b'pc', b'earlier', b'a' * 40, b'b' * 40, b'', b'thread-\xff')) + b'\n'
        log.write_bytes(before)
        with patch.object(release, 'STATE', log.parent):
            release.log_release(SimpleNamespace(worktrees=self.worktrees, name='later'),
                                SimpleNamespace(commits=release.Heads('c' * 40, 'd' * 40), paired=True))

        after = log.read_bytes()
        self.assertTrue(after.startswith(before), after)
        self.assertEqual(after[len(before):].split(b'\t')[2:5], [b'later', b'c' * 40, b'd' * 40])

    def test_a_write_stopped_part_way_leaves_the_rows_as_they_were_and_no_file_beside_them(self):
        log = self.root / 'state/releases.tsv'
        log.parent.mkdir(parents=True)
        before = '\t'.join(release.RELEASES_COLUMNS) + '\n' + '\t'.join(('t', 'pc', 'earlier', 'a' * 40, 'b' * 40, '', '')) + '\n'
        log.write_text(before, encoding='utf-8')
        for step in ('fsync', 'replace'):
            with self.subTest(stopped_at=step), patch.object(release, 'STATE', log.parent), \
                    patch.object(release.os if step == 'fsync' else Path, step, side_effect=OSError('No space left')):
                with self.assertRaises(OSError):
                    release.log_release(SimpleNamespace(worktrees=self.worktrees, name='later'),
                                        SimpleNamespace(commits=release.Heads('c' * 40, 'd' * 40), paired=True))

                self.assertEqual(log.read_text(encoding='utf-8'), before)
                self.assertEqual(sorted(path.name for path in log.parent.iterdir()), ['releases.tsv'])

    def test_a_row_that_cannot_be_written_leaves_the_pushed_finish_successful_and_says_so(self):
        self.assertEqual(self.start('unlogged')[0], 0)
        (self.worktrees / 'unlogged/public/content').write_text('second\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        (self.root / 'state/releases.tsv').mkdir(parents=True)

        with redirect_stderr(io.StringIO()) as error:
            status = release_main(['finish', 'unlogged', '--message', str(message), '--push', *self.options()],
                                  self.public)

        self.assertEqual(status, 0, error.getvalue())
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'second')
        self.assertIn('The release is pushed, but its row in', error.getvalue())


if __name__ == '__main__':
    unittest.main()
