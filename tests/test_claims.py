"""A claim names the task that holds something no lock covers, and ends by itself."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import claims  # noqa: E402


class ClaimsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name) / 'state/claims'   # a fresh machine has no state folder yet

    def run_claims(self, *arguments):
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            status = claims.main(list(arguments), self.folder)
        return status, output.getvalue(), errors.getvalue()

    def test_a_second_task_is_refused_and_told_who_holds_the_claim(self):
        self.assertEqual(self.run_claims('take', 'windows-desktop', '--who', 'thread A', '--note', 'restart the app')[0], 0)
        status, _, errors = self.run_claims('take', 'windows-desktop', '--who', 'thread B')
        self.assertEqual(status, 3)
        self.assertIn('windows-desktop is claimed by thread A', errors)
        self.assertIn('restart the app', errors)
        self.assertEqual(self.run_claims('release', 'windows-desktop', '--who', 'thread B')[0], 3)
        self.assertEqual(claims.running(self.folder)['windows-desktop']['who'], 'thread A')

    def test_a_command_that_finds_another_one_running_stops_with_status_3(self):
        from portable import exclusive_lock
        with exclusive_lock(self.folder / '.lock'):
            self.assertEqual(self.run_claims('take', 'windows-desktop', '--who', 'thread A'),
                             (3, '', 'Another claims command is running; run this one again\n'))
        self.assertEqual(claims.running(self.folder), {})

    def test_the_holder_renews_and_releases_its_own_claim(self):
        self.run_claims('take', 'windows-config', '--who', 'thread A', '--note', 'two settings', '--hours', '1')
        first = claims.running(self.folder)['windows-config']
        self.assertEqual(self.run_claims('take', 'windows-config', '--who', 'thread A', '--hours', '3')[0], 0)
        renewed = claims.running(self.folder)['windows-config']
        self.assertEqual((renewed['since'], renewed['note']), (first['since'], 'two settings'))
        self.assertGreater(renewed['until'], first['until'])
        self.assertEqual(self.run_claims('release', 'windows-config', '--who', 'thread A')[0], 0)
        self.assertEqual(self.run_claims('status')[1], 'No claims.\n')
        self.assertEqual(self.run_claims('take', 'windows-config', '--who', 'thread B')[0], 0)

    def test_a_claim_past_its_end_blocks_nobody(self):
        self.run_claims('take', 'windows-desktop', '--who', 'thread A', '--hours', '1')
        later = claims.now() + timedelta(hours=1, minutes=1)
        with patch.object(claims, 'now', lambda: later):
            self.assertEqual(claims.lines(self.folder), [])
            self.assertEqual(self.run_claims('take', 'windows-desktop', '--who', 'thread B')[0], 0)
            self.assertEqual(claims.running(self.folder)['windows-desktop']['who'], 'thread B')

    def test_status_lists_each_running_claim_and_skips_a_damaged_file(self):
        self.run_claims('take', 'windows-desktop', '--who', 'thread A', '--note', 'checking the terminal')
        self.run_claims('take', 'windows-pull', '--who', 'thread B')
        (self.folder / 'broken.json').write_text('{not json')
        (self.folder / 'no-end.json').write_text(json.dumps({'who': 'thread C'}))
        lines = self.run_claims('status')[1].splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r'^windows-desktop by thread A until \d\d:\d\d: checking the terminal$')
        self.assertRegex(lines[1], r'^windows-pull by thread B until \d\d:\d\d$')

    def test_names_and_hours_are_checked(self):
        for arguments in (('take', 'Windows Desktop', '--who', 'a'), ('take', 'windows-desktop'),
                          ('take', 'windows-desktop', '--who', 'a', '--hours', '13')):
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                claims.main(list(arguments), self.folder)


if __name__ == '__main__':
    unittest.main()
