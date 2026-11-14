from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402
from release_case import ProcessReplaced, ReleaseCase, git, release_main  # noqa: E402
from release_case import skip_on_windows as setUpModule  # noqa: E402, F401


class DetachTest(ReleaseCase):
    def test_a_detached_finish_runs_the_same_arguments_as_a_unit_and_result_reads_how_it_ended(self):
        release_main(['start', 'away', *self.options()], self.public)
        args = release.argparse.Namespace(name='away', worktrees=self.worktrees, timeout=0.1)
        argv = ['finish', 'away', '--detach', '--message', '/tmp/m', '--push']
        command = release.detach_command(args, argv, {'PATH': '/bin', 'HOME': '/home/x'})
        log, status = self.worktrees / 'away/build/finish.log', self.worktrees / 'away/build/finish.exit'
        self.assertEqual(command[:4], ['systemd-run', '--user', '--collect', '--unit=dotfiles-release-away'])
        self.assertEqual(command[4:8], ['--setenv', 'PATH=/bin', '--setenv', 'HOME=/home/x'])  # no SSH agent to pass
        self.assertIn(f'--property=StandardOutput=append:{log}', command)
        self.assertEqual(command[-7:], ['finish', 'away', '--message', '/tmp/m', '--push', '--report', str(status)])
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['result', 'away', *self.options()], self.public), 3)
        self.assertIn('no detached finish of away was started', error.getvalue())
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'away', '--detach', '--message', 'm.txt', *self.options()],
                                          self.public), 3)
        self.assertIn('--detach needs an absolute path for m.txt', error.getvalue())
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'away', '--detach', '--message', '/tmp/m', '--pair-message', 'pair.txt',
                                           *self.options()], self.public), 3)
        self.assertIn('--detach needs an absolute path for pair.txt', error.getvalue())
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('Ran 3 tests: OK\nRelease stopped: nothing to release\n', encoding='utf-8')
        with redirect_stderr(io.StringIO()) as error, patch.object(release, 'unit_state', return_value=('active', '')):
            self.assertEqual(release_main(['result', 'away', '--timeout', '0.1', *self.options()], self.public), 4)
        self.assertIn('Still running', error.getvalue())
        # A unit that was killed is gone and wrote no status. On 10 November 2026 result waited an hour on one.
        with redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()) as output, \
                patch.object(release, 'unit_state', return_value=('inactive', '')):
            self.assertEqual(release_main(['result', 'away', '--timeout', '600', *self.options()], self.public),
                             release.CRASHED)
        self.assertIn('ended without a status', error.getvalue())
        self.assertIn('Release stopped: nothing to release', output.getvalue())
        status.write_text('3\n', encoding='utf-8')
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(['result', 'away', *self.options()], self.public), 3)
        self.assertIn('Release stopped: nothing to release', output.getvalue())
        self.assertIn('finish away exited 3', output.getvalue())

    def test_result_waits_through_a_systemctl_call_that_gives_no_state_and_names_the_state_it_ends_on(self):
        # On 14 November 2026 one such call, about twelve minutes into a running finish, made result exit 70 and
        # report the unit gone, while the finish ran another six minutes and wrote its own status.
        release_main(['start', 'asked', *self.options()], self.public)
        log = self.worktrees / 'asked/build/finish.log'
        log.parent.mkdir(parents=True)
        log.write_text('Both complete suites run\n', encoding='utf-8')

        @contextmanager
        def systemctl(*answers, then=(0, 'active\n', '')):
            calls, clock = iter(answers), [0.0]  # result's two-second polls pass on this clock

            def run(command, **options):
                self.assertEqual(command, ['systemctl', '--user', 'is-active', 'dotfiles-release-asked'])
                return subprocess.CompletedProcess(command, *next(calls, then))
            with patch.object(release.subprocess, 'run', run), patch.object(release.time, 'monotonic', lambda: clock[0]), \
                    patch.object(release.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
                yield
        no_answer = (1, '', 'Failed to get unit: Connection timed out\n')
        with systemctl(no_answer, (3, 'refreshing\n', '')), redirect_stderr(io.StringIO()) as error, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(release_main(['result', 'asked', '--timeout', '3', *self.options()], self.public), 4)
        self.assertIn('Still running after 3 seconds; log', error.getvalue())
        # With no state at the last check, the line says so in systemctl's own words.
        with systemctl(then=no_answer), redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()):
            self.assertEqual(release_main(['result', 'asked', '--timeout', '3', *self.options()], self.public), 4)
        self.assertIn('Still running after 3 seconds, though systemd gave no state at the last check (systemctl '
                      'exited 1: Failed to get unit: Connection timed out); log', error.getvalue())
        for state in ('inactive', 'failed'):
            with self.subTest(state=state), systemctl(no_answer, (3, f'{state}\n', '')), \
                    redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(release_main(['result', 'asked', '--timeout', '20', *self.options()], self.public),
                                 release.CRASHED)
                self.assertIn(f'ended without a status: systemd calls its unit {state}', error.getvalue())
                self.assertIn('Both complete suites run', output.getvalue())

    def test_a_finish_started_by_a_unit_reports_its_status_to_the_file_result_reads(self):
        release_main(['start', 'unit', *self.options()], self.public)
        status = self.root / 'finish.exit'
        with redirect_stderr(io.StringIO()):
            self.assertEqual(release_main(['finish', 'unit', '--report', str(status), *self.options()], self.public), 3)
        self.assertEqual(status.read_text(encoding='utf-8'), '3\n')  # nothing to release

    def test_a_finish_that_ends_on_an_uncaught_error_still_reports_a_status(self):
        status = self.root / 'finish.exit'

        def crash(argv, report):
            report.append(status)
            raise AttributeError("module 'portable' has no attribute 'require_readable'")
        with patch.object(release, 'dispatch', crash), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release.main(['finish', 'unit']), release.CRASHED)
        self.assertEqual(status.read_text(encoding='utf-8'), f'{release.CRASHED}\n')
        self.assertIn("AttributeError: module 'portable' has no attribute 'require_readable'", error.getvalue())

    def test_a_finish_that_crashes_after_its_push_exits_with_its_own_status_not_the_pulls(self):
        release_main(['start', 'late', *self.options()], self.public)
        (self.worktrees / 'late/public/content').write_text('second\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        with patch.object(release, 'other_machines', side_effect=KeyError('Peer')), \
                redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
            code = release_main(['finish', 'late', '--message', str(message), '--push', *self.options()], self.public)
        self.assertEqual(code, release.CRASHED)
        self.assertIn('  pushed\n', output.getvalue())
        self.assertIn("KeyError: 'Peer'", error.getvalue())
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'second')

    def test_a_finish_that_waited_for_another_run_starts_again_from_the_engine_as_it_is_now(self):
        release_main(['start', 'late', *self.options()], self.public)
        status = self.root / 'finish.exit'
        argv = ['finish', 'late', '--report', str(status), *self.options()]
        busy = iter([['pull PID 1 since 23:54: pull.py --update --apply'], []])

        def restart(program, arguments, environment):
            raise ProcessReplaced(arguments)
        with patch.object(release, 'running', lambda names=None: next(busy, [])), \
                patch.object(release.os, 'execve', restart), redirect_stderr(io.StringIO()), \
                redirect_stdout(io.StringIO()), self.assertRaises(ProcessReplaced) as restarted:
            release_main(argv, self.public)
        self.assertEqual(restarted.exception.args[0][1:], ['-B', str(self.public / 'release.py'), *argv])
        with patch.object(release.os, 'execve', restart), redirect_stderr(io.StringIO()):
            self.assertEqual(release_main(argv, self.public), 3)

    def test_a_finish_starts_again_before_the_lock_when_a_pull_changed_the_engine_meanwhile(self):
        release_main(['start', 'tested', *self.options()], self.public)
        (self.worktrees / 'tested/public/content').write_text('tested\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        argv = ['finish', 'tested', '--message', str(message), '--push', *self.options()]
        suite, runs = release.test_suite, []

        def restart(program, arguments, environment):
            raise ProcessReplaced(arguments)

        def pull_during_the_suite(*arguments):
            runs.append(arguments)
            suite(*arguments)
            if len(runs) == 1:  # another release goes out and its pull moves the engine's checkout
                self.push_from_another_session(self.public_origin, 'other', {'doc': 'theirs\n'})
                git(self.public, 'pull', '-q', '--ff-only', 'origin', 'main')

        with patch.object(release, 'test_suite', pull_during_the_suite), patch.object(release.os, 'execve', restart), \
                redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(ProcessReplaced) as restarted:
                release_main(argv, self.public)
            self.assertEqual(restarted.exception.args[0][3:5], ['finish', 'tested'])
            self.assertIn('Starting again because a pull changed the engine while this finish ran or '
                          'waited, to run the engine as it is now', output.getvalue())
            self.assertRegex(output.getvalue(), r'Waited \d+ seconds for the release lock before the engine changed\n')
            self.assertIn('Held the release lock', output.getvalue())
            self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'first')
            self.assertEqual(git(self.worktrees / 'tested/public', 'log', '-1', '--format=%s'), 'Change the content')
            self.assertFalse((self.root / 'state/release.lock.holder').exists())
            self.assertEqual(release_main(argv, self.public), 0)  # as the engine starts it again
        self.assertEqual(len(runs), 2)
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'tested')

    def test_a_detached_finish_keeps_the_log_of_the_attempt_before_and_starts_no_second_unit(self):
        release_main(['start', 'twice', *self.options()], self.public)
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        argv = ['finish', 'twice', '--detach', '--message', str(message), *self.options()]
        log, status = self.worktrees / 'twice/build/finish.log', self.worktrees / 'twice/build/finish.exit'
        log.parent.mkdir(parents=True)
        log.write_text('Release stopped: the first attempt\n', encoding='utf-8')
        status.write_text('3\n', encoding='utf-8')
        started = []

        def unit(args, argv, environ):
            started.append(argv)
            return [sys.executable, '-c', 'pass']

        with patch.object(release, 'detach_command', unit), patch.object(release.shutil, 'which', return_value='/bin/x'):
            with patch.object(release, 'unit_state', return_value=('active', '')), redirect_stderr(io.StringIO()) as error:
                self.assertEqual(release_main(argv, self.public), 3)
            self.assertIn('a detached finish of twice still runs as unit dotfiles-release-twice', error.getvalue())
            self.assertEqual(started, [])
            self.assertEqual(log.read_text(encoding='utf-8'), 'Release stopped: the first attempt\n')
            self.assertEqual(status.read_text(encoding='utf-8'), '3\n')
            no_state = (None, 'systemctl exited 1: Failed to connect to bus: No medium found')
            with patch.object(release, 'unit_state', return_value=no_state), redirect_stderr(io.StringIO()) as error:
                self.assertEqual(release_main(argv, self.public), 3)
            self.assertIn('systemd gave no state for unit dotfiles-release-twice (systemctl exited 1: Failed to connect '
                          'to bus: No medium found), so an earlier finish of twice may still run', error.getvalue())
            self.assertEqual((started, log.read_text(encoding='utf-8')), ([], 'Release stopped: the first attempt\n'))
            for state in ('inactive', 'failed'):
                log.write_text(f'Release stopped: an attempt, unit {state}\n', encoding='utf-8')
                with self.subTest(state=state), patch.object(release, 'unit_state', return_value=(state, '')), \
                        redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(release_main(argv, self.public), 0)
                    kept = sorted(log.parent.glob('finish.*.log'), key=lambda path: path.stat().st_mtime_ns)[-1]
                    self.assertIn(f'The log of the attempt before is kept as {kept}', output.getvalue())
                    self.assertEqual(kept.read_text(encoding='utf-8'), f'Release stopped: an attempt, unit {state}\n')
                    self.assertEqual(log.read_text(encoding='utf-8'), '')
                    self.assertFalse(status.exists())
        self.assertEqual(len(started), 2)
        self.assertEqual(len(list(log.parent.glob('finish.*.log'))), 2)
        # systemd gives no state without systemctl, or when its bus call fails and it prints only to stderr, as
        # systemctl --user did on Omarchy with no bus to reach; result keeps waiting then, and detach above stopped.
        failed_call = subprocess.CompletedProcess(
            [], 1, '', 'Failed to connect to user scope bus via local transport: No such file or directory\n')
        for answer, problem in (
                ({'side_effect': OSError('no systemctl')}, 'systemctl: no systemctl'),
                ({'return_value': failed_call}, 'systemctl exited 1: Failed to connect to user scope bus via local '
                                                'transport: No such file or directory'),
                ({'return_value': subprocess.CompletedProcess([], 3, 'unknown\n', '')}, "systemctl exited 3: 'unknown'")):
            with patch.object(release.subprocess, 'run', **answer):
                self.assertEqual(release.unit_state('twice'), (None, problem))
        for state in ('active', 'deactivating', 'refreshing', 'inactive', 'failed', 'maintenance'):
            with patch.object(release.subprocess, 'run', return_value=subprocess.CompletedProcess([], 3, f'{state}\n')):
                self.assertEqual(release.unit_state('twice'), (state, ''))


if __name__ == '__main__':
    unittest.main()
