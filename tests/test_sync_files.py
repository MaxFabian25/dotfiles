"""Files writes a sync's changes and keeps, in its backup folder, each file as it was with a receipt naming it."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sync_files import ConcurrentEdit, Files, RehearsalFiles  # noqa: E402


class FilesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.backup = self.root / 'backup'

    def test_each_written_file_is_backed_up_and_the_receipt_names_each_copy(self):
        old, new = self.root / 'old.json', self.root / 'new/made.json'
        old.write_bytes(b'before\n')
        files = Files(self.backup)
        self.assertTrue(files.write(old, b'after\n', expected=b'before\n'))
        self.assertTrue(files.write(new, b'made\n', expected=None))
        self.assertFalse(files.write(old, b'after\n', expected=b'after\n'))
        self.assertEqual((old.read_bytes(), new.read_bytes()), (b'after\n', b'made\n'))
        self.assertEqual(files.changed, [old, new])
        receipt = json.loads((self.backup / 'files.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt, {str(old): {'existed': True, 'backup': '0001-old.json'},
                                   str(new): {'existed': False, 'backup': None}})
        self.assertEqual((self.backup / '0001-old.json').read_bytes(), b'before\n')
        self.assertEqual(sorted(p.name for p in self.backup.iterdir()), ['0001-old.json', 'files.json'])

    def test_a_file_that_changed_since_it_was_read_is_left_as_it_is(self):
        path = self.root / 'settings.json'
        path.write_bytes(b'edited elsewhere\n')
        with self.assertRaisesRegex(ConcurrentEdit, 'Changed during sync'):
            Files(self.backup).write(path, b'ours\n', expected=b'as read\n')
        self.assertEqual(path.read_bytes(), b'edited elsewhere\n')

    def test_a_rehearsal_says_which_writes_would_change_a_file_and_writes_none(self):
        path = self.root / 'settings.json'
        path.write_bytes(b'same\n')
        rehearsal = RehearsalFiles()
        self.assertEqual([rehearsal.write(path, data, expected=b'same\n') for data in (b'same\n', b'other\n')],
                         [False, True])
        self.assertEqual(path.read_bytes(), b'same\n')


if __name__ == '__main__':
    unittest.main()
