from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homes import child_stderr_dropped, recording_sync
import schedule


NS = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}


class WindowsScheduleTest(unittest.TestCase):
    def test_current_session_has_one_indefinite_half_hour_clock_and_immediate_logon(self):
        with patch('schedule.datetime') as clock, patch('schedule.getpass.getuser', return_value='fixture'):
            clock.now.return_value = datetime(2026, 10, 15, 23, 59, 30, 123456, tzinfo=timezone.utc)
            files = schedule.render('win32', Path('/home/fixture'), ['python.exe', 'pull.py', '--private', r'C:\my repo'])
        task = ElementTree.fromstring(next(iter(files.values())))
        timer = task.find('t:Triggers/t:TimeTrigger', NS)
        self.assertIsNotNone(timer, 'Installing during a session requires a time trigger')
        self.assertEqual(timer.findtext('t:StartBoundary', namespaces=NS), '2026-10-16T00:00:30+00:00')
        self.assertEqual(timer.findtext('t:Enabled', namespaces=NS), 'true')
        self.assertEqual(timer.findtext('t:Repetition/t:Interval', namespaces=NS), 'PT30M')
        self.assertEqual(timer.findtext('t:Repetition/t:StopAtDurationEnd', namespaces=NS), 'false')
        self.assertIsNone(timer.find('t:Repetition/t:Duration', NS))
        self.assertIsNone(timer.find('t:EndBoundary', NS))
        logon = task.find('t:Triggers/t:LogonTrigger', NS)
        self.assertEqual(logon.findtext('t:Enabled', namespaces=NS), 'true')
        self.assertEqual(logon.findtext('t:UserId', namespaces=NS), 'fixture')
        self.assertIsNone(logon.find('t:Delay', NS))
        self.assertIsNone(logon.find('t:Repetition', NS))
        self.assertEqual(task.findtext('t:Principals/t:Principal/t:LogonType', namespaces=NS), 'InteractiveToken')
        self.assertEqual((task.findtext('t:Actions/t:Exec/t:Command', namespaces=NS),
                          task.findtext('t:Actions/t:Exec/t:Arguments', namespaces=NS)),
                         ('python.exe', 'pull.py --private "C:\\my repo"'))

    def test_install_refuses_without_pinned_release_signers(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            argv = ['schedule.py', '--root', str(home / 'repo'), '--private', str(home / 'private'), '--install']
            with patch.object(sys, 'argv', argv), patch('schedule.Path.home', return_value=home), \
                    patch('schedule.command', return_value=['python.exe', 'pull.py']), \
                    patch('schedule.subprocess.run') as run, patch('schedule.Files') as writer:
                with self.assertRaisesRegex(RuntimeError, 'pin trusted release signers'):
                    schedule.main()
            run.assert_not_called()
            writer.assert_not_called()

    def test_install_stops_when_the_task_cannot_be_registered(self):
        def refuse_the_task(command, check=False, **kwargs):
            code = int(command[0] == 'schtasks.exe')
            if check and code:
                raise subprocess.CalledProcessError(code, command)
            return SimpleNamespace(returncode=code)

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.config/dotfiles').mkdir(parents=True)
            (home / '.config/dotfiles/allowed_signers').write_text('')
            argv = ['schedule.py', '--root', str(home / 'repo'), '--private', str(home / 'private'), '--install']
            with patch.object(sys, 'argv', argv), patch.object(sys, 'platform', 'win32'), \
                    patch('schedule.Path.home', return_value=home), \
                    patch('schedule.command', return_value=['python.exe', 'pull.py']), patch('schedule.paired'), \
                    patch('schedule.run_sync', return_value=0), \
                    patch('schedule.subprocess.run', side_effect=refuse_the_task), \
                    patch('schedule.DeviceRoots.current'), patch('schedule.backup_directory'), patch('schedule.Files'), \
                    redirect_stdout(io.StringIO()) as output, self.assertRaises(subprocess.CalledProcessError):
                schedule.main()
        self.assertEqual(output.getvalue(), '')

    def test_install_anchors_the_timer_after_the_drift_check(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()  # as the install resolves its roots; macOS keeps its temp folder behind a link
            (home / '.config/dotfiles').mkdir(parents=True)
            (home / '.config/dotfiles/allowed_signers').write_text('')
            argv = ['schedule.py', '--root', str(home / 'repo'), '--private', str(home / 'private'), '--install']
            with patch('schedule.datetime') as clock:
                clock.now.return_value = datetime(2026, 10, 15, 19, 0, tzinfo=timezone.utc)

                def finish(*args, **kwargs):
                    clock.now.return_value = datetime(2026, 10, 15, 19, 5, tzinfo=timezone.utc)
                    return 0

                with patch.object(sys, 'argv', argv), patch.object(sys, 'platform', 'win32'), \
                        patch('schedule.Path.home', return_value=home), \
                        patch('schedule.command', return_value=['python.exe', 'pull.py']), \
                        patch('schedule.paired') as paired, patch('schedule.run_sync', side_effect=finish), \
                        patch('schedule.subprocess.run'), \
                        patch('schedule.DeviceRoots.current'), patch('schedule.backup_directory'), \
                        patch('schedule.Files') as writer, redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(schedule.main(), 0)
                self.assertEqual(output.getvalue(), 'Local pull schedule enabled; daily source review remains on the '
                                                    'maintainer machine.\n')
                task = ElementTree.fromstring(writer.return_value.write.call_args.args[1])
            paired.assert_called_once_with(home / 'repo', home / 'private')
            self.assertEqual(task.findtext('t:Triggers/t:TimeTrigger/t:StartBoundary', namespaces=NS),
                             '2026-10-15T19:06:00+00:00')

    def test_install_goes_on_only_after_a_sync_check_that_recorded_no_drift(self):
        real_run = subprocess.run

        def install(sync):
            registered = []

            def register(command, **kwargs):
                if command[0] != 'schtasks.exe':
                    return real_run(command, **kwargs)
                registered.append(command)
                return subprocess.CompletedProcess(command, 0)

            with tempfile.TemporaryDirectory() as directory:
                home = Path(directory).resolve()
                (home / '.config/dotfiles').mkdir(parents=True)
                (home / '.config/dotfiles/allowed_signers').write_text('')
                (home / 'repo').mkdir()
                (home / 'repo/sync.py').write_text(sync, encoding='utf-8')
                argv = ['schedule.py', '--root', str(home / 'repo'), '--private', str(home / 'private'), '--install']
                with patch.object(sys, 'argv', argv), patch.object(sys, 'platform', 'win32'), \
                        patch('schedule.Path.home', return_value=home), \
                        patch('schedule.command', return_value=['python.exe', 'pull.py']), patch('schedule.paired'), \
                        patch('schedule.DeviceRoots.current'), \
                        patch('schedule.backup_directory', return_value=home / 'backups'), \
                        patch('schedule.subprocess.run', register), redirect_stdout(io.StringIO()), \
                        child_stderr_dropped():
                    try:
                        outcome = schedule.main()
                    except RuntimeError as error:
                        outcome = str(error)
                written = (home / '.local/state/dotfiles/pull-task.xml').exists()
                arguments = home / 'repo/arguments.json'
                return (outcome, written, len(registered),
                        json.loads(arguments.read_bytes()) if arguments.exists() else [])

        outcome, written, registered, arguments = install("raise KeyError('a stand-in sync that crashes')\n")
        self.assertTrue(outcome.startswith('sync.py exited 1 without recording how it ended, as after a crash'),
                        outcome)
        self.assertEqual((written, registered), (False, 0))
        for status in (1, 2, 3):
            with self.subTest(status=status):
                outcome, written, registered, arguments = install(recording_sync(status))
                self.assertEqual(outcome, 'Resolve device drift before enabling its automatic pull schedule')
                self.assertEqual((written, registered), (False, 0))
        keep_arguments = ("import json\n"
                          "pathlib.Path(__file__).with_name('arguments.json').write_text(json.dumps(sys.argv[1:]))\n")
        outcome, written, registered, arguments = install(recording_sync(0, then=keep_arguments))
        self.assertEqual((outcome, written, registered), (0, True, 1))
        self.assertIn('--check', arguments)
        self.assertNotIn('--apply', arguments)
        self.assertEqual(Path(arguments[arguments.index('--private') + 1]).parts[-2:], ('private', 'setup'))


class CommandTest(unittest.TestCase):
    def test_the_pull_runs_through_uv_on_the_locked_project_and_needs_uv(self):
        root, private = Path('/repo'), Path('/private')
        with patch('schedule.shutil.which', return_value='/bin/uv'):
            self.assertEqual(schedule.command(root, private), [
                '/bin/uv', 'run', '--locked', '--project', str(root), 'python', str(root / 'pull.py'), '--root', str(root),
                '--private', str(private), '--update', '--apply'])
        with patch('schedule.shutil.which', return_value=None), self.assertRaisesRegex(RuntimeError, 'Install uv'):
            schedule.command(root, private)


class LinuxScheduleTest(unittest.TestCase):
    def test_the_service_runs_the_quoted_command_and_the_timer_starts_it_every_half_hour(self):
        home = Path('/home/fixture')
        files = schedule.render('linux', home, ['/usr/bin/python3', '/repo/pull.py', '--private', '/my "100%" repo'])
        folder = home / '.config/systemd/user'
        self.assertEqual(sorted(files), [folder / 'dotfiles-pull.service', folder / 'dotfiles-pull.timer'])
        service = files[folder / 'dotfiles-pull.service'].decode().splitlines()
        self.assertIn('ExecStart="/usr/bin/python3" "/repo/pull.py" "--private" "/my \\"100%%\\" repo"', service)
        self.assertIn('OnUnitActiveSec=30min', files[folder / 'dotfiles-pull.timer'].decode().splitlines())


if __name__ == '__main__':
    unittest.main()
