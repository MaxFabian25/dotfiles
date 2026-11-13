"""Build folders take their parent's access rules on Windows, and an unreadable release is reported as such."""
import ast
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


def folders_made_under_a_parent(source):
    """Line numbers of tempfile.TemporaryDirectory and tempfile.mkdtemp calls in source that name a parent folder,
    by the dir keyword or as the third positional argument."""
    return sorted(node.lineno for node in ast.walk(ast.parse(source))
                  if isinstance(node, ast.Call)
                  and getattr(node.func, 'attr', getattr(node.func, 'id', None)) in ('TemporaryDirectory', 'mkdtemp')
                  and (len(node.args) >= 3 or any(keyword.arg == 'dir' for keyword in node.keywords)))


class StagingGuardTest(unittest.TestCase):
    def test_a_tempfile_folder_under_a_named_parent_is_found(self):
        self.assertEqual(folders_made_under_a_parent(
            "import tempfile\n"
            "with tempfile.TemporaryDirectory(prefix='.build-', dir=root) as a: pass\n"
            "b = tempfile.mkdtemp('', '.native-', parent)\n"
            "with tempfile.TemporaryDirectory(prefix='check-') as c: pass\n"), [2, 3])

    def test_engine_modules_stage_through_portable(self):
        # A folder tempfile makes under a release root is owner-only on Windows, and the tree renamed out of it
        # stays closed to a non-elevated run. sync_ending keeps a private record, never a tree renamed into place.
        root = Path(__file__).resolve().parents[1]
        direct = [f'{module.name}:{line}' for module in sorted(root.glob('*.py'))
                  if module.name not in {'portable.py', 'sync_ending.py'}
                  for line in folders_made_under_a_parent(module.read_text(encoding='utf-8'))]
        self.assertEqual(direct, [], 'make these folders with portable.staging_directory or portable.staging_folder')


if __name__ == '__main__':
    unittest.main()
