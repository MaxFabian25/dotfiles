import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homes import ENGINE, child_stderr_dropped, keeping_arguments, recording_sync
from sync_ending import run_sync

PUT_A_FOLDER_IN_THE_RECORDS_PLACE = 'ending.unlink()\nending.mkdir()\n'
OF_ANOTHER_RELEASE = ('import pathlib, sys\n'
                      "pathlib.Path(sys.argv[sys.argv.index('--ending') + 1]).write_bytes(b'{\"status\": 2}')\n")


class RunSyncTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def sync(self, text, **options):
        (self.root / 'sync.py').write_text(text, encoding='utf-8')
        return run_sync(self.root, None, apply=False, **options)

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

        # The folder that is left goes with this test's own folder.
        with mock.patch('os.unlink', held_as_on_windows), mock.patch.object(tempfile, 'tempdir', str(self.root)):
            self.assertEqual(self.sync(recording_sync(1)), 1)

    def test_the_sync_writes_to_the_callers_own_output_while_it_runs(self):
        (self.root / 'sync.py').write_text(
            recording_sync(0, then="print('to stdout')\nprint('to stderr', file=sys.stderr)\n"), encoding='utf-8')
        caller = (f'import pathlib, sys\nsys.path.insert(0, {str(ENGINE)!r})\nfrom sync_ending import run_sync\n'
                  f'print(run_sync(pathlib.Path({str(self.root)!r}), None, apply=False))\n')
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

    def test_a_sync_past_its_time_limit_is_stopped_there(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.sync('import time\ntime.sleep(60)\n', timeout=1)
        self.assertLess(time.monotonic() - started, 30)

    def test_the_sync_gets_the_callers_mode_checkouts_and_notes(self):
        (self.root / 'sync.py').write_text(recording_sync(0, then=keeping_arguments()), encoding='utf-8')
        private = self.root / 'private'
        run_sync(self.root, private, apply=True, notes=True)
        arguments = json.loads((self.root / 'arguments.json').read_bytes())
        self.assertEqual(arguments[arguments.index('--public') + 1], str(self.root / 'setup'))
        self.assertEqual(arguments[arguments.index('--private') + 1], str(private / 'setup'))
        self.assertEqual([flag in arguments for flag in ('--apply', '--notes', '--check')], [True, True, False])
        run_sync(self.root, None, apply=False)
        arguments = json.loads((self.root / 'arguments.json').read_bytes())
        self.assertEqual([flag in arguments for flag in ('--check', '--apply', '--private', '--notes')],
                         [True, False, False, False])

    def test_the_real_sync_records_the_3_it_stops_with_where_no_setup_folder_exists(self):
        (self.root / 'sync.py').write_text(f'import runpy, sys\nsys.path.insert(0, {str(ENGINE)!r})\n'
                                           f'runpy.run_path({str(ENGINE / "sync.py")!r}, run_name="__main__")\n',
                                           encoding='utf-8')
        with child_stderr_dropped():
            self.assertEqual(run_sync(self.root, self.root / 'private', apply=False), 3)


if __name__ == '__main__':
    unittest.main()
