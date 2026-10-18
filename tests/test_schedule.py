from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

import schedule


NS = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}


class WindowsScheduleTest(unittest.TestCase):
    def test_current_session_has_one_indefinite_half_hour_clock_and_immediate_logon(self):
        with patch('schedule.datetime') as clock, patch('schedule.getpass.getuser', return_value='fixture'):
            clock.now.return_value = datetime(2026, 10, 15, 23, 59, 30, 123456, tzinfo=timezone.utc)
            files = schedule.render('win32', Path('/home/fixture'), ['python.exe', 'pull.py'])
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

    def test_install_anchors_the_timer_after_the_drift_check(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.config/dotfiles').mkdir(parents=True)
            (home / '.config/dotfiles/allowed_signers').write_text('')
            argv = ['schedule.py', '--root', str(home / 'repo'), '--private', str(home / 'private'), '--install']
            with patch('schedule.datetime') as clock:
                clock.now.return_value = datetime(2026, 10, 15, 19, 0, tzinfo=timezone.utc)

                def finish(command, **kwargs):
                    clock.now.return_value = datetime(2026, 10, 15, 19, 5, tzinfo=timezone.utc)
                    return SimpleNamespace(returncode=0)

                with patch.object(sys, 'argv', argv), patch.object(sys, 'platform', 'win32'), \
                        patch('schedule.Path.home', return_value=home), \
                        patch('schedule.command', return_value=['python.exe', 'pull.py']), \
                        patch('schedule.paired'), patch('schedule.subprocess.run', side_effect=finish), \
                        patch('schedule.DeviceRoots.current'), patch('schedule.backup_directory'), \
                        patch('schedule.Files') as writer:
                    self.assertEqual(schedule.main(), 0)
                task = ElementTree.fromstring(writer.return_value.write.call_args.args[1])
            self.assertEqual(task.findtext('t:Triggers/t:TimeTrigger/t:StartBoundary', namespaces=NS),
                             '2026-10-15T19:06:00+00:00')


if __name__ == '__main__':
    unittest.main()
