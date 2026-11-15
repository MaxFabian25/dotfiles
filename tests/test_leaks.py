from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homes import isolate_git
from leaks import DENYLIST, leaks

ROOT = Path(__file__).resolve().parents[1]


def setUpModule():
    isolate_git()


class LeaksTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.public, self.private = Path(temp.name, 'public'), Path(temp.name, 'private')
        self.public.mkdir()
        subprocess.run(['git', '-C', str(self.public), 'init', '-q', '-b', 'main'], check=True)
        self.denylist = self.private / DENYLIST
        self.denylist.parent.mkdir(parents=True)
        self.denylist.write_text('# hosts\nsecret-host\n', encoding='utf-8')

    def write(self, name, text):
        (self.public / name).write_text(text, encoding='utf-8')

    def test_a_listed_string_is_found_in_any_case_in_files_git_would_commit(self):
        self.write('notes.md', 'fine\nssh SECRET-Host\n')
        self.write('clean.md', 'nothing here\n')
        self.assertEqual(leaks(self.public, self.private), ['notes.md:2'])

    def test_a_clean_tree_and_message_have_no_hits(self):
        self.write('notes.md', 'fine\n')
        message = self.public.parent / 'message'
        message.write_text('Change the notes\n', encoding='utf-8')
        self.assertEqual(leaks(self.public, self.private, message), [])

    def test_a_listed_string_in_the_message_is_a_hit(self):
        message = self.public.parent / 'message'
        message.write_text('Reach Secret-Host over SSH\n', encoding='utf-8')
        self.assertEqual(leaks(self.public, self.private, message), ['the public commit message'])

    def test_a_string_is_read_behind_a_byte_order_mark_before_a_comment_and_with_a_number_sign_inside(self):
        self.write('notes.md', 'one seeded-host\ntwo team#seeded\n')
        self.denylist.write_text('﻿seeded-host  # the laptop\nteam#seeded\n', encoding='utf-8')
        self.assertEqual(leaks(self.public, self.private), ['notes.md:1', 'notes.md:2'])

    def test_a_missing_empty_or_non_utf_8_denylist_stops_the_check(self):
        for prepare, expected in ((self.denylist.unlink, 'is missing'),
                                  (lambda: self.denylist.write_text('# only a comment\n', encoding='utf-8'), 'lists no strings'),
                                  (lambda: self.denylist.write_text('secret-host\n', encoding='utf-16'), 'is not UTF-8')):
            self.denylist.write_text('secret-host\n', encoding='utf-8')
            prepare()
            with self.subTest(expected=expected), self.assertRaisesRegex(RuntimeError, expected):
                leaks(self.public, self.private)

    def test_the_command_exits_1_and_prints_the_place_without_the_string(self):
        self.write('notes.md', 'ssh secret-host\n')
        done = subprocess.run([sys.executable, '-B', str(ROOT / 'leaks.py'), '--public', str(self.public),
                               '--private', str(self.private)], capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout), (1, 'notes.md:1\n'))
        self.assertNotIn('secret-host', done.stdout + done.stderr)
        self.write('notes.md', 'fine\n')
        done = subprocess.run([sys.executable, '-B', str(ROOT / 'leaks.py'), '--public', str(self.public),
                               '--private', str(self.private)], capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout), (0, ''))


if __name__ == '__main__':
    unittest.main()
