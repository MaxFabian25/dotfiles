import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homes import ENGINE, child_stderr_dropped, keeping_arguments, recording_sync
from portable import process_running
from sync_ending import run_sync

PUT_A_FOLDER_IN_THE_RECORDS_PLACE = 'ending.unlink()\nending.mkdir()\n'
KILL = getattr(signal, 'SIGKILL', signal.SIGTERM)  # os.kill ends a process on Windows with any signal


def ended(pids):
    for pid in pids:
        if process_running(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, KILL)


OF_ANOTHER_RELEASE = ('import pathlib, sys\n'
                      "pathlib.Path(sys.argv[sys.argv.index('--ending') + 1]).write_bytes(b'{\"status\": 2}')\n")


class RunSyncTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / 'home/.local/state/dotfiles'

    def sync(self, text, **options):
        (self.root / 'sync.py').write_text(text, encoding='utf-8')
        return run_sync(self.root, None, state=self.state, apply=False, **options)

    def stop(self, text):
        with self.assertRaises(RuntimeError) as raised, child_stderr_dropped():
            self.sync(text)
        return str(raised.exception)

    def test_the_status_a_sync_recorded_before_it_exited_0_is_returned(self):
        for status in (0, 1, 2, 3):
            with self.subTest(status=status):
                self.assertEqual(self.sync(recording_sync(status)), status)

    def test_a_sync_that_did_not_end_by_recording_its_status_is_a_stop_that_names_its_exit(self):
        crashed = 'sync.py exited 1 without recording how it ended, as after a crash or a kill; '
        for text, head in (("raise KeyError('a stand-in sync that crashes')\n", crashed),
                           ('raise SystemExit(1)\n', crashed),
                           ('raise SystemExit(0)\n', 'sync.py exited 0 without recording how it ended; '),
                           (recording_sync(1, then='raise SystemExit(1)\n'),
                            'sync.py recorded status 1 and then exited 1; '),
                           (recording_sync(0, then='raise SystemExit(2)\n'),
                            'sync.py recorded status 0 and then exited 2; ')):
            with self.subTest(head=head):
                stop = self.stop(text)
                self.assertTrue(stop.startswith(head), stop)
                self.assertTrue(stop.endswith('; its own output above, if any, says why'), stop)

    def test_a_record_that_cannot_be_read_is_a_stop_in_the_readers_own_words(self):
        head = 'sync.py exited 0, and its record of how it ended cannot be read: '
        with self.assertRaises(ValueError) as parser:
            json.loads('{')
        self.assertIn(f'{head}{parser.exception}; ', self.stop(recording_sync(0, then="ending.write_text('{')\n")))
        self.assertRegex(self.stop(recording_sync(0, then=PUT_A_FOLDER_IN_THE_RECORDS_PLACE)),
                         rf'^{re.escape(head)}\[Errno \d+\] ')
        for status in (True, '0'):
            with self.subTest(status=status):
                self.assertTrue(self.stop(recording_sync(status)).startswith(head))
        for record in ('null', '[0]'):
            with self.subTest(record=record):
                self.assertTrue(self.stop(recording_sync(0, then=f'ending.write_text({record!r})\n')).startswith(head))

    def test_a_sync_of_another_release_is_understood_by_the_flag_and_the_key_they_share(self):
        self.assertEqual(self.sync(OF_ANOTHER_RELEASE), 2)

    def test_a_record_that_cannot_be_removed_costs_a_sync_that_ended_well_nothing(self):
        unlink = os.unlink

        def held_as_on_windows(path, *args, **kwargs):
            if str(path).endswith('ending.json'):
                raise PermissionError(13, 'Access is denied', str(path))
            return unlink(path, *args, **kwargs)

        with mock.patch('os.unlink', held_as_on_windows):
            self.assertEqual(self.sync(recording_sync(1)), 1)

    def test_a_cleaner_that_empties_the_temp_folder_while_the_sync_runs_costs_the_record_nothing(self):
        # On 2026-11-13 a Windows pull stopped because its empty record folder left %TEMP% while the sync ran.
        temp = self.root / 'temp'
        temp.mkdir()
        cleaner = (f'import os\nfor folder, children, files in os.walk({str(temp)!r}, topdown=False):\n'
                   f'    if folder != {str(temp)!r} and not os.listdir(folder):\n        os.rmdir(folder)\n')
        with mock.patch.object(tempfile, 'tempdir', str(temp)):
            self.assertEqual(self.sync(cleaner + recording_sync(1)), 1)

    def left_behind(self, **ages):
        records = self.state / 'sync-endings'
        for name, age in ages.items():
            (records / name).mkdir(parents=True)
            (records / name / 'ending.json').write_text('{"status": 0}', encoding='utf-8')
            then = time.time() - age
            os.utime(records / name, (then, then))
        return records

    def test_a_record_folder_left_by_a_run_over_a_day_ago_goes_and_a_younger_one_stays(self):
        records = self.left_behind(**{'sync-old': 86400 + 60, 'sync-young': 86400 - 3600})
        self.assertEqual(self.sync(recording_sync(0)), 0)
        self.assertEqual(sorted(path.name for path in records.iterdir()), ['sync-young'])

    def test_a_left_folder_that_windows_still_holds_costs_the_sync_nothing(self):
        records = self.left_behind(**{'sync-held': 86400 + 60})
        rmdir = os.rmdir

        def held_as_on_windows(path, *args, **kwargs):
            if 'sync-held' in str(path):
                raise PermissionError(13, 'Access is denied', str(path))
            return rmdir(path, *args, **kwargs)

        with mock.patch('os.rmdir', held_as_on_windows):
            self.assertEqual(self.sync(recording_sync(1)), 1)
        self.assertTrue((records / 'sync-held').exists())

    def test_a_left_folder_another_run_removes_after_the_listing_costs_the_sync_nothing(self):
        records = self.left_behind(**{'sync-other-run': 86400 + 60})
        glob = Path.glob

        def removed_once_listed(path, pattern, *args, **kwargs):
            found = list(glob(path, pattern, *args, **kwargs))
            shutil.rmtree(records / 'sync-other-run')
            return iter(found)

        with mock.patch.object(Path, 'glob', removed_once_listed):
            self.assertEqual(self.sync(recording_sync(1)), 1)

    def test_the_sync_writes_to_the_callers_own_output_while_it_runs(self):
        (self.root / 'sync.py').write_text(
            recording_sync(0, then="print('to stdout')\nprint('to stderr', file=sys.stderr)\n"), encoding='utf-8')
        caller = (f'import pathlib, sys\nsys.path.insert(0, {str(ENGINE)!r})\nfrom sync_ending import run_sync\n'
                  f'print(run_sync(pathlib.Path({str(self.root)!r}), None, state=pathlib.Path({str(self.state)!r}), '
                  'apply=False))\n')
        result = subprocess.run([sys.executable, '-c', caller], capture_output=True, text=True, encoding='utf-8',
                                timeout=60)
        self.assertEqual((result.stdout, result.stderr), ('to stdout\n0\n', 'to stderr\n'))

    def test_the_folder_that_held_the_record_is_gone_whether_the_sync_ended_well_or_not(self):
        note = "pathlib.Path(__file__).with_name('folder').write_text(str(ending.parent), encoding='utf-8')\n"
        self.assertEqual(self.sync(recording_sync(2, then=note)), 2)
        returned = Path((self.root / 'folder').read_text(encoding='utf-8'))
        self.stop(recording_sync(2, then=note + 'raise SystemExit(1)\n'))
        raised = Path((self.root / 'folder').read_text(encoding='utf-8'))
        self.assertEqual((returned.exists(), raised.exists()), (False, False))
        self.assertEqual(list((self.state / 'sync-endings').iterdir()), [])

    def test_a_sync_past_its_time_limit_is_stopped_there_with_the_commands_it_started(self):
        marker = self.root / 'started'
        starts_a_command = ('import os, pathlib, subprocess, sys, time\n'
                            'command = subprocess.Popen([sys.executable, "-c", "import signal, time; '
                            'signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])\n'
                            f"pathlib.Path({str(marker)!r} + '.part').write_text(f'{{os.getpid()}} {{command.pid}}')\n"
                            f'pathlib.Path({str(marker)!r} + ".part").replace({str(marker)!r})\n'
                            'time.sleep(60)\n')
        for limit in (3, 10, 30):
            began = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                self.sync(starts_a_command, timeout=limit)
            self.assertLess(time.monotonic() - began, limit + 20)
            if marker.exists():
                pids = [int(pid) for pid in marker.read_text().split()]
                self.addCleanup(ended, pids)
                ending = time.monotonic() + 10
                while any(map(process_running, pids)):
                    self.assertLess(time.monotonic(), ending, 'The sync or its command outlived its time limit')
                    time.sleep(0.05)
                return
        self.fail('The sync never started its command within the longest limit')

    @unittest.skipIf(os.name == 'nt', 'Windows has no SIGHUP')
    def test_the_callers_own_handlers_for_a_hang_up_and_a_sigterm_are_back_once_the_sync_ends(self):
        def own(number, frame):
            pass

        for number in (signal.SIGHUP, signal.SIGTERM):
            self.addCleanup(signal.signal, number, signal.signal(number, own))
        self.assertEqual(self.sync(recording_sync(0)), 0)
        self.stop('raise SystemExit(1)\n')
        with self.assertRaises(subprocess.TimeoutExpired):
            self.sync('import time\ntime.sleep(60)\n', timeout=1)
        self.assertEqual([signal.getsignal(number) for number in (signal.SIGHUP, signal.SIGTERM)], [own, own])

    @unittest.skipIf(os.name == 'nt', 'Windows has no SIGHUP')
    def test_a_signal_the_caller_ignores_stays_ignored_and_the_other_one_still_ends_the_caller(self):
        def own(number, frame):
            pass

        signals_its_caller = ('import os, signal\nos.kill(os.getppid(), signal.SIGHUP)\n'
                              'os.kill(os.getppid(), signal.SIGTERM)\n')
        # nohup has a pull ignore SIGHUP only, so a kill must still stop its sync.
        for ignored, sent in ((signal.SIGHUP, signal.SIGTERM), (signal.SIGTERM, signal.SIGHUP)):
            with self.subTest(ignored=ignored.name):
                self.addCleanup(signal.signal, ignored, signal.signal(ignored, signal.SIG_IGN))
                self.addCleanup(signal.signal, sent, signal.signal(sent, own))
                with self.assertRaises(SystemExit) as raised:
                    self.sync(recording_sync(2, then=signals_its_caller))
                self.assertEqual(raised.exception.code, 128 + sent)
                self.assertEqual([signal.getsignal(ignored), signal.getsignal(sent)], [signal.SIG_IGN, own])

    @unittest.skipIf(os.name == 'nt', 'Windows has no SIGHUP')
    def test_a_hang_up_handler_set_outside_python_is_left_to_it(self):
        # Python reports such a handler as None, as in a Python that a program embeds, and cannot set None back.
        # The test's own handler stands in for it.
        got = []
        for number in (signal.SIGHUP, signal.SIGTERM):
            self.addCleanup(signal.signal, number, signal.getsignal(number))
        signal.signal(signal.SIGHUP, lambda number, frame: got.append(number))
        getsignal = signal.getsignal
        hangs_up_its_caller = 'import os, signal\nos.kill(os.getppid(), signal.SIGHUP)\n'
        with mock.patch.object(signal, 'getsignal', lambda number: None if number == signal.SIGHUP else
                               getsignal(number)):
            self.assertEqual(self.sync(recording_sync(2, then=hangs_up_its_caller)), 2)
        self.assertEqual(got, [signal.SIGHUP])

    def test_the_sync_gets_the_callers_mode_checkouts_and_notes(self):
        (self.root / 'sync.py').write_text(recording_sync(0, then=keeping_arguments()), encoding='utf-8')
        private = self.root / 'private'
        run_sync(self.root, private, state=self.state, apply=True, notes=True)
        arguments = json.loads((self.root / 'arguments.json').read_bytes())
        self.assertEqual(arguments[arguments.index('--public') + 1], str(self.root / 'setup'))
        self.assertEqual(arguments[arguments.index('--private') + 1], str(private / 'setup'))
        self.assertEqual([flag in arguments for flag in ('--apply', '--notes', '--check')], [True, True, False])
        run_sync(self.root, None, state=self.state, apply=False)
        arguments = json.loads((self.root / 'arguments.json').read_bytes())
        self.assertEqual([flag in arguments for flag in ('--check', '--apply', '--private', '--notes')],
                         [True, False, False, False])

    def test_the_real_sync_records_the_3_it_stops_with_where_no_setup_folder_exists(self):
        (self.root / 'sync.py').write_text(f'import runpy, sys\nsys.path.insert(0, {str(ENGINE)!r})\n'
                                           f'runpy.run_path({str(ENGINE / "sync.py")!r}, run_name="__main__")\n',
                                           encoding='utf-8')
        with child_stderr_dropped():
            self.assertEqual(run_sync(self.root, self.root / 'private', state=self.state, apply=False), 3)


if __name__ == '__main__':
    unittest.main()
