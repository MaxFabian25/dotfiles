"""Build folders take their parent's access rules on Windows, and an unreadable release is reported as such."""
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import package  # noqa: E402
import portable  # noqa: E402


class StagingTest(unittest.TestCase):
    def test_staging_directory_is_made_under_its_parent_and_removed(self):
        with tempfile.TemporaryDirectory() as parent:
            with portable.staging_directory(prefix='.build-', dir=parent) as folder:
                self.assertEqual(Path(folder).parent, Path(parent))
                self.assertTrue(Path(folder).name.startswith('.build-'))
                (Path(folder) / 'package').mkdir()
            self.assertEqual(list(Path(parent).iterdir()), [])

    def test_windows_staging_folder_is_made_without_the_owner_only_mode(self):
        # Python on Windows turns mode 0o700, which tempfile passes, into an owner-only access rule.
        modes = []
        real = os.mkdir

        def record(path, mode=0o777, **arguments):
            modes.append(mode)
            return real(path, mode, **arguments)

        with tempfile.TemporaryDirectory() as parent, patch.object(portable, 'WINDOWS', True), \
                patch.object(os, 'mkdir', record):
            with portable.staging_directory(prefix='.build-', dir=parent) as folder:
                tree = Path(folder) / 'repo'
                tree.mkdir()
                locked = tree / 'object'
                locked.write_bytes(b'x')
                locked.chmod(stat.S_IREAD)
            self.assertFalse(Path(folder).exists())
        self.assertNotIn(0o700, modes)

    @unittest.skipIf(os.name == 'nt' or os.geteuid() == 0, 'needs a folder this user cannot list')
    def test_files_reports_an_unreadable_folder_instead_of_an_empty_tree(self):
        with tempfile.TemporaryDirectory() as root:
            closed = Path(root) / 'release' / 'skills'
            closed.mkdir(parents=True)
            (closed / 'SKILL.md').write_text('x')
            closed.chmod(0)
            try:
                with self.assertRaisesRegex(PermissionError, 'Cannot read .*skills'):
                    package.files(Path(root) / 'release')
            finally:
                closed.chmod(0o700)


if __name__ == '__main__':
    unittest.main()
