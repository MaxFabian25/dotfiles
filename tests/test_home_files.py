import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import home_files
import pull
from home_files import EDITED, deliver


class HomeFilesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.private, self.home, self.state = (self.root / name for name in ('private', 'home', 'state'))
        self.home.mkdir()
        self.state.mkdir()
        self.release({'.local/bin/tool': ('tool 1\n', True), '.config/tool/rc': ('rc 1\n', False)})

    def release(self, files):
        """Write a release's home files into the private checkout, as path to text and whether it is executable."""
        for relative, (text, executable) in files.items():
            path = self.private / home_files.SOURCE / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
            path.chmod(0o755 if executable else 0o644)

    def deliver(self, apply=True):
        return deliver(self.private, self.home, self.state, apply=apply)

    def installed(self):
        return {path.relative_to(self.home).as_posix(): (path.read_text(encoding='utf-8'), os.access(path, os.X_OK))
                for path in sorted(self.home.rglob('*')) if path.is_file()}

    def test_a_first_delivery_creates_each_file_with_the_sources_executable_bit(self):
        cache = self.private / home_files.SOURCE / '.local/bin/__pycache__'
        cache.mkdir()
        (cache / 'tool.cpython-314.pyc').write_bytes(b'\0')
        delivery = self.deliver()
        self.assertEqual((delivery.written, delivery.left), (('.config/tool/rc', '.local/bin/tool'), ()))
        self.assertEqual(self.installed(), {'.local/bin/tool': ('tool 1\n', True), '.config/tool/rc': ('rc 1\n', False)})
        self.assertEqual(delivery.lines(True), ['Home file delivered: ~/.config/tool/rc',
                                                'Home file delivered: ~/.local/bin/tool'])
        again = self.deliver()
        self.assertEqual((again.written, again.left), ((), ()))

    def test_a_copy_of_the_last_delivery_is_replaced_with_a_backup_and_keeps_its_bit(self):
        self.deliver()
        self.release({'.local/bin/tool': ('tool 2\n', True), '.config/tool/rc': ('rc 2\n', True)})
        delivery = self.deliver()
        self.assertEqual(delivery.written, ('.config/tool/rc', '.local/bin/tool'))
        self.assertEqual(self.installed(), {'.local/bin/tool': ('tool 2\n', True), '.config/tool/rc': ('rc 2\n', True)})
        backups = sorted(path.read_text() for path in (self.state / 'home-file-backups').rglob('*-*')
                         if path.is_file() and path.name != 'files.json')
        self.assertEqual(backups, ['rc 1\n', 'tool 1\n'])

    def test_an_edited_copy_is_left_and_named_while_the_rest_is_delivered(self):
        self.deliver()
        (self.home / '.local/bin/tool').write_text('a fix under test\n')
        self.release({'.local/bin/tool': ('tool 2\n', True), '.config/tool/rc': ('rc 2\n', False)})
        for _ in range(2):   # the next pull leaves it too
            delivery = self.deliver()
            self.assertEqual(delivery.left, (('.local/bin/tool', EDITED),))
            self.assertEqual(self.installed(), {'.local/bin/tool': ('a fix under test\n', True),
                                                '.config/tool/rc': ('rc 2\n', False)})
        self.assertEqual(delivery.lines(True), [f'Home file left as it is: ~/.local/bin/tool {EDITED}'])
        # Reverting the edit to the delivered copy lets the next pull replace it.
        (self.home / '.local/bin/tool').write_text('tool 1\n')
        self.assertEqual(self.deliver().written, ('.local/bin/tool',))
        self.assertEqual(self.installed()['.local/bin/tool'], ('tool 2\n', True))

    def test_without_a_record_only_a_matching_or_absent_file_is_taken_over(self):
        for relative, text in (('.local/bin/tool', 'tool 1\n'), ('.config/tool/rc', 'made by hand\n')):
            (self.home / relative).parent.mkdir(parents=True)
            (self.home / relative).write_text(text)
        delivery = self.deliver()
        # The matching copy had lost its executable bit, which is delivered; the other has no known origin.
        self.assertEqual((delivery.written, delivery.left), (('.local/bin/tool',), (('.config/tool/rc', EDITED),)))
        self.assertEqual(self.installed(), {'.local/bin/tool': ('tool 1\n', True), '.config/tool/rc': ('made by hand\n', False)})
        # Copying the release's file by hand settles it, and the next release then replaces it.
        (self.home / '.config/tool/rc').write_text('rc 1\n')
        self.assertEqual(self.deliver().left, ())
        self.release({'.config/tool/rc': ('rc 2\n', False)})
        self.assertEqual(self.deliver().written, ('.config/tool/rc',))

    def test_a_check_names_what_a_pull_would_deliver_and_writes_nothing(self):
        self.deliver()
        (self.state / home_files.RECORD).unlink()
        self.assertEqual(self.deliver(apply=False).lines(False), [])
        self.assertFalse((self.state / home_files.RECORD).exists())   # not even the record of copies that match
        self.deliver()
        self.release({'.local/bin/tool': ('tool 2\n', True)})
        record = (self.state / home_files.RECORD).read_bytes()
        delivery = self.deliver(apply=False)
        self.assertEqual(delivery.lines(False), ['Home file to deliver: ~/.local/bin/tool'])
        self.assertEqual(self.installed()['.local/bin/tool'], ('tool 1\n', True))
        self.assertEqual((self.state / home_files.RECORD).read_bytes(), record)
        self.assertEqual(self.deliver().written, ('.local/bin/tool',))

    def test_a_link_or_folder_in_a_files_place_and_a_failed_write_are_left_with_the_reason(self):
        self.deliver()
        (self.home / '.local/bin/tool').unlink()
        (self.home / '.local/bin/tool').symlink_to(self.home / '.config/tool/rc')
        (self.home / '.config/tool').chmod(0o500)
        self.addCleanup((self.home / '.config/tool').chmod, 0o700)
        self.release({'.local/bin/tool': ('tool 2\n', True), '.config/tool/rc': ('rc 2\n', False)})
        delivery = self.deliver()
        self.assertEqual(delivery.written, ())
        self.assertEqual(delivery.left[0][0], '.config/tool/rc')
        self.assertRegex(delivery.left[0][1], r'^could not be delivered: .*Permission denied')
        self.assertEqual(delivery.left[1], ('.local/bin/tool', 'is not a plain file'))
        self.assertEqual((self.home / '.config/tool/rc').read_text(), 'rc 1\n')
        # The record still names the copy it delivered, so the file is replaced once it can be written.
        (self.home / '.config/tool').chmod(0o700)
        self.assertEqual(self.deliver().written, ('.config/tool/rc',))

    def test_a_pull_killed_after_replacing_a_file_leaves_a_copy_the_next_release_still_replaces(self):
        self.deliver()
        self.release({'.local/bin/tool': ('tool 2\n', True)})
        real = home_files._record

        def killed_at_the_last_write(path, files):
            if files['.local/bin/tool'] == [home_files._digest(b'tool 2\n')]:
                raise KeyboardInterrupt
            real(path, files)
        with patch.object(home_files, '_record', killed_at_the_last_write), self.assertRaises(KeyboardInterrupt):
            self.deliver()
        self.assertEqual(self.installed()['.local/bin/tool'], ('tool 2\n', True))
        self.release({'.local/bin/tool': ('tool 3\n', True)})
        delivery = self.deliver()
        self.assertEqual((delivery.written, delivery.left), (('.local/bin/tool',), ()))
        # A pull killed before it replaced the file leaves the old copy, which is known too.
        self.release({'.local/bin/tool': ('tool 4\n', True)})
        with patch.object(home_files.Files, 'write', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.deliver()
        self.assertEqual(self.deliver().written, ('.local/bin/tool',))
        self.assertEqual(self.installed()['.local/bin/tool'], ('tool 4\n', True))

    def test_a_record_that_cannot_be_written_leaves_the_file_and_says_why(self):
        self.deliver()
        self.release({'.local/bin/tool': ('tool 2\n', True)})
        with patch.object(home_files, '_record', side_effect=PermissionError('no write')):
            delivery = self.deliver()
        self.assertEqual((delivery.written, delivery.left),
                         ((), (('.local/bin/tool', 'could not be delivered: no write'),)))
        self.assertEqual(self.installed()['.local/bin/tool'], ('tool 1\n', True))
        # The record of a file that only needed recording fails after the files, and says so.
        (self.state / home_files.RECORD).unlink()
        with patch.object(home_files, '_record', side_effect=PermissionError('no write')), \
                self.assertRaisesRegex(RuntimeError, 'the files are delivered, but their record could not be written: no write'):
            self.deliver()

    def test_a_record_write_that_fails_leaves_no_temporary_file(self):
        for failing, error in (('replace', PermissionError('no replace')), ('fspath', None)):
            # The second writes a value json cannot encode, so the write into the temporary file fails.
            files = {} if failing == 'replace' else {'a': [object()]}
            with self.subTest(failing=failing), \
                    patch.object(home_files.os, 'replace', side_effect=error or home_files.os.replace), \
                    self.assertRaises((PermissionError, TypeError)):
                home_files._record(self.state / home_files.RECORD, files)
            self.assertEqual(list(self.state.iterdir()), [])

    def test_a_file_a_release_dropped_and_a_later_one_brings_back_is_still_known(self):
        self.deliver()
        (self.private / home_files.SOURCE / '.config/tool/rc').unlink()
        self.release({'.local/bin/tool': ('tool 2\n', True)})
        self.deliver()
        self.assertEqual(self.installed()['.config/tool/rc'], ('rc 1\n', False))   # a pull removes nothing
        self.release({'.config/tool/rc': ('rc 3\n', False)})
        delivery = self.deliver()
        self.assertEqual((delivery.written, delivery.left), (('.config/tool/rc',), ()))

    def test_a_source_that_loses_its_executable_bit_takes_it_from_the_copy(self):
        self.deliver()
        (self.private / home_files.SOURCE / '.local/bin/tool').chmod(0o644)
        self.assertEqual(self.deliver().written, ('.local/bin/tool',))
        self.assertEqual(self.installed()['.local/bin/tool'], ('tool 1\n', False))

    def test_a_copy_that_changes_during_the_write_and_a_source_that_cannot_be_read_are_left(self):
        self.deliver()
        self.release({'.local/bin/tool': ('tool 2\n', True), '.config/tool/rc': ('rc 2\n', False)})
        (self.private / home_files.SOURCE / '.config/tool/rc').chmod(0)
        with patch.object(home_files.Files, 'write', side_effect=home_files.ConcurrentEdit('Changed during sync')):
            delivery = self.deliver()
        self.assertEqual(delivery.written, ())
        self.assertRegex(delivery.left[0][1], r'^could not be delivered: .*Permission denied')
        self.assertEqual(delivery.left[1], ('.local/bin/tool', 'changed while the pull delivered it'))
        (self.private / home_files.SOURCE / '.config/tool/rc').chmod(0o644)
        self.assertEqual(self.deliver().written, ('.config/tool/rc', '.local/bin/tool'))

    def test_a_source_folder_that_cannot_be_listed_stops_the_delivery_and_names_the_folder(self):
        self.deliver()
        self.release({'.local/bin/tool': ('tool 2\n', True)})
        folder = self.private / home_files.SOURCE / '.local/bin'
        folder.chmod(0)
        self.addCleanup(folder.chmod, 0o755)
        with self.assertRaisesRegex(RuntimeError, rf'^{folder}: cannot list the home files: Permission denied$'):
            self.deliver()
        folder.chmod(0o400)   # listed, but not entered
        with self.assertRaisesRegex(RuntimeError, rf'^{folder}/tool: cannot read the home file: Permission denied$'):
            self.deliver()
        folder.chmod(0o755)
        # A link to nothing in the source is a broken release, which stops the delivery too.
        (folder / 'gone').symlink_to(folder / 'nothing')
        with self.assertRaisesRegex(RuntimeError, rf'^{folder}/gone: cannot read the home file: No such file or directory$'):
            self.deliver()
        (folder / 'gone').unlink()
        above = self.private / 'config'
        above.chmod(0)
        self.addCleanup(above.chmod, 0o755)
        with self.assertRaisesRegex(RuntimeError, 'cannot read the home files: Permission denied$'):
            self.deliver()
        above.chmod(0o755)
        self.assertEqual(self.installed()['.local/bin/tool'], ('tool 1\n', True))
        # A private checkout without the folder has no home files, which is not an error.
        self.assertEqual(home_files.source_files(self.root / 'elsewhere'), {})

    def test_a_record_that_cannot_be_read_stops_the_delivery_before_any_file(self):
        self.deliver()
        self.release({'.local/bin/tool': ('tool 2\n', True)})
        for text in ('{', '{"files": {"a": 1}}', '{"files": {"a": "0"}}', '[]'):
            (self.state / home_files.RECORD).write_text(text)
            with self.subTest(text=text), self.assertRaisesRegex(RuntimeError, 'record of delivered home files'):
                self.deliver()
        self.assertEqual(self.installed()['.local/bin/tool'], ('tool 1\n', True))


class PullDeliversHomeFilesTest(unittest.TestCase):
    """pull.run with a stand-in sync.py that exits with the status the test gives."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.public, self.private, self.home = (self.root / name for name in ('public', 'private', 'home'))
        for path in (self.public, self.home, self.private / home_files.SOURCE / '.local/bin'):
            path.mkdir(parents=True)
        (self.private / home_files.SOURCE / '.local/bin/tool').write_text('tool 1\n')
        self.receipt = self.home / '.local/state/dotfiles/last-applied.json'

    def pull(self, status=0, apply=True, platform='linux'):
        (self.public / 'sync.py').write_text(f'raise SystemExit({status})\n')
        args = SimpleNamespace(root=self.public, private=self.private, update=False, apply=apply, notes=False, wait=0)
        out, err = io.StringIO(), io.StringIO()
        with patch('pull.Path.home', return_value=self.home), patch('home_files.Path.home', return_value=self.home), \
                patch('pull.paired'), patch('pull.install_tools'), patch.object(pull.sys, 'platform', platform), \
                patch('pull.git', return_value='0' * 40), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            return pull.run(args), out.getvalue(), err.getvalue()

    def test_a_pull_delivers_after_a_sync_that_nothing_blocked_and_only_on_linux(self):
        tool = self.home / '.local/bin/tool'
        for status in (2, 3):
            self.assertEqual(self.pull(status)[0], status)
            self.assertFalse(tool.exists())
        self.assertEqual(self.pull(platform='win32'), (0, '', ''))
        self.assertEqual(self.pull(platform='darwin'), (0, '', ''))
        self.assertFalse(tool.exists())
        self.assertEqual(self.pull(apply=False), (1, 'Home file to deliver: ~/.local/bin/tool\n', ''))
        self.assertFalse(tool.exists())
        status, out, err = self.pull(1)
        self.assertEqual((status, out), (1, 'Home file delivered: ~/.local/bin/tool\n'))
        self.assertIn('managed changes remain after applying', err)
        self.assertEqual(tool.read_text(), 'tool 1\n')
        self.assertEqual(self.pull(), (0, '', ''))

    def test_an_edited_home_file_makes_the_pull_exit_1_with_the_pair_recorded(self):
        self.pull()
        self.receipt.unlink()
        (self.home / '.local/bin/tool').write_text('a fix under test\n')
        status, out, err = self.pull()
        self.assertEqual((status, out), (1, ''))
        self.assertEqual(err, f'Home file left as it is: ~/.local/bin/tool {EDITED}\n')
        self.assertEqual(json.loads(self.receipt.read_text())['public'], '0' * 40)
        self.assertEqual((self.home / '.local/bin/tool').read_text(), 'a fix under test\n')

    def test_a_record_that_cannot_be_read_costs_neither_the_receipt_nor_the_tools(self):
        self.pull()
        self.receipt.unlink()
        record = self.home / '.local/state/dotfiles' / home_files.RECORD
        record.write_text('{')
        (self.private / home_files.SOURCE / '.local/bin/tool').write_text('tool 2\n')
        status, out, err = self.pull()
        self.assertEqual((status, out), (1, ''))
        self.assertRegex(err, rf'^Home files not delivered: {record}: cannot read the record of delivered home files: ')
        self.assertIn('Without that file a pull takes over each copy that matches the release', err)
        self.assertTrue(self.receipt.exists())
        self.assertEqual((self.home / '.local/bin/tool').read_text(), 'tool 1\n')

    def test_a_failed_tool_install_holds_back_no_home_file(self):
        (self.public / 'sync.py').write_text('raise SystemExit(0)\n')
        args = SimpleNamespace(root=self.public, private=self.private, update=False, apply=True, notes=False, wait=0)
        with patch('pull.Path.home', return_value=self.home), patch('pull.paired'), patch('pull.git', return_value='0' * 40), \
                patch('pull.install_tools', side_effect=RuntimeError('mise install exited 1')), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'mise install'):
            pull.run(args)
        self.assertEqual((self.home / '.local/bin/tool').read_text(), 'tool 1\n')

    def test_a_pull_without_a_private_checkout_delivers_nothing(self):
        (self.public / 'sync.py').write_text('raise SystemExit(0)\n')
        args = SimpleNamespace(root=self.public, private=None, update=False, apply=True, notes=False, wait=0)
        with patch('pull.Path.home', return_value=self.home), patch('pull.paired'), patch('pull.install_tools'), \
                patch('pull.git', return_value='0' * 40):
            self.assertEqual(pull.run(args), 0)
        self.assertFalse((self.home / '.local/bin').exists())


if __name__ == '__main__':
    unittest.main()
