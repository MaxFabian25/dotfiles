import importlib.util
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('portable', Path(__file__).resolve().parents[1] / 'portable.py')
portable = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portable)


def archive(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as bundle:
        for name, value in entries:
            info = tarfile.TarInfo(name)
            if isinstance(value, tuple):
                info.type, info.linkname = tarfile.SYMTYPE, value[1]
                bundle.addfile(info)
            elif value is None:
                info.type = tarfile.DIRTYPE
                bundle.addfile(info)
            else:
                info.size = len(value)
                bundle.addfile(info, io.BytesIO(value))
    return buffer.getvalue()


class PortableTest(unittest.TestCase):
    def test_windows_cli_environment_recovers_missing_executable_extensions(self):
        for inherited in ({}, {'PATHEXT': ''}):
            with self.subTest(inherited=inherited), patch.object(portable, 'WINDOWS', True):
                before = dict(inherited)
                environment = portable.native_cli_environment(inherited)
                self.assertIn('.EXE', environment['PATHEXT'].split(';'))
                self.assertEqual(inherited, before)

    def test_cli_environment_preserves_custom_extensions_and_posix_environment(self):
        with patch.object(portable, 'WINDOWS', True):
            self.assertEqual(portable.native_cli_environment({'PATHEXT': '.EXE;.PY'})['PATHEXT'], '.EXE;.PY')
        with patch.object(portable, 'WINDOWS', False):
            self.assertNotIn('PATHEXT', portable.native_cli_environment({}))

    def test_homes_honour_the_apps_variables_only_for_the_real_home(self):
        with patch.dict(os.environ, {'CODEX_HOME': '/elsewhere/codex', 'CLAUDE_CONFIG_DIR': '/elsewhere/claude'}):
            self.assertEqual(portable.codex_home(), Path('/elsewhere/codex'))
            self.assertEqual(portable.claude_home(Path.home()), Path('/elsewhere/claude'))
            self.assertEqual(portable.codex_home(Path('/sandbox')), Path('/sandbox/.codex'))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(portable.claude_home(Path('/sandbox')), Path('/sandbox/.claude'))

    def test_cli_override_must_name_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory) / 'codex'
            cli.write_text('')
            with patch.dict(os.environ, {'CODEX_CLI': str(cli)}):
                self.assertEqual(portable.codex_cli(), str(cli))
            with patch.dict(os.environ, {'CODEX_CLI': str(cli) + '-missing'}):
                with self.assertRaises(FileNotFoundError):
                    portable.codex_cli()

    def test_text_round_trips_bytes_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'note.md'
            text = 'line one\r\nline two — “quoted”\n'
            portable.write_utf8(path, text)
            self.assertEqual(path.read_bytes(), text.encode('utf-8'))
            self.assertEqual(portable.read_utf8(path), text)

    def test_posix_keys_use_forward_slashes(self):
        self.assertEqual(portable.posix(Path('skills') / 'a' / 'SKILL.md'), 'skills/a/SKILL.md')
        self.assertEqual(portable.posix(portable.PurePath('skills/a')), 'skills/a')

    def test_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'state/lock'
            with portable.exclusive_lock(lock):
                with self.assertRaises(BlockingIOError):
                    with portable.exclusive_lock(lock):
                        pass
            with portable.exclusive_lock(lock):
                pass

    def test_pointer_swaps_between_sibling_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '1.0').mkdir()
            (root / '2.0').mkdir()
            link = root / 'current'
            self.assertIsNone(portable.read_pointer(link))
            portable.set_pointer(link, '1.0')
            self.assertEqual(portable.read_pointer(link), '1.0')
            portable.set_pointer(link, '2.0')
            self.assertEqual(portable.read_pointer(link), '2.0')
            self.assertEqual(link.resolve(), (root / '2.0').resolve())
            portable.remove_pointer(link)
            self.assertFalse(os.path.lexists(link))
            with self.assertRaises(ValueError):
                portable.remove_pointer(root / '1.0')

    def test_extract_materialises_links_inside_the_archive(self):
        data = archive([('pkg', None), ('pkg/CLAUDE.md', b'rules\n'), ('pkg/AGENTS.md', ('link', 'CLAUDE.md')),
                        ('pkg/shared', None), ('pkg/shared/a.md', b'a\n'), ('pkg/shared/b.md', ('link', 'a.md')),
                        ('pkg/copy', ('link', 'shared'))])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            portable.extract_tar(data, root)
            self.assertFalse((root / 'pkg/AGENTS.md').is_symlink())
            self.assertEqual((root / 'pkg/AGENTS.md').read_bytes(), b'rules\n')
            self.assertEqual((root / 'pkg/copy/b.md').read_bytes(), b'a\n')
            self.assertFalse(any(path.is_symlink() for path in root.rglob('*')))

    def test_extract_rejects_links_that_leave_the_archive_or_dangle(self):
        for link in ['../../outside', 'missing.md']:
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    portable.extract_tar(archive([('pkg', None), ('pkg/x', ('link', link))]), directory)


if __name__ == '__main__':
    unittest.main()
