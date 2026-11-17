import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('invocation_overrides', Path(__file__).resolve().parents[1] / 'invocation_overrides.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

IDENTITY = 'impeccable@personal'
REGISTRY = {'targets': {'Codex': {IDENTITY: []}}, 'explicitTargets': {'Codex': {IDENTITY: []}}}


class WindowsReplaceTest(unittest.TestCase):
    def test_windows_replace_patches_one_file_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launcher = root / 'skills/x/impeccable.cmd'
            launcher.parent.mkdir(parents=True)
            launcher.write_text('rem head\n"%~1" engine-probe >"%tmp%" 2>nul\nrem tail\n', encoding='utf-8',
                                newline='\n')
            replace = {'plugins': {}, 'windows': {'impeccable': [
                ('skills/x/impeccable.cmd', '"%~1" engine-probe >"%tmp%" 2>nul', 'call "%~1" engine-probe >"%tmp%" 2>nul')]}}
            writes = module.plan(REGISTRY, [('Codex', IDENTITY, root)], replace)
            self.assertEqual(writes[0][2], 'rem head\ncall "%~1" engine-probe >"%tmp%" 2>nul\nrem tail\n')
            launcher.write_text('rem an upstream rewrite with no such line\n', encoding='utf-8', newline='\n')
            with self.assertRaisesRegex(ValueError, 'Review upstream change to an adapted file'):
                module.plan(REGISTRY, [('Codex', IDENTITY, root)], replace)
            launcher.write_text('call "%~1" engine-probe >"%tmp%" 2>nul\n', encoding='utf-8', newline='\n')
            writes = module.plan(REGISTRY, [('Codex', IDENTITY, root)], replace)
            self.assertEqual(writes[0][1], writes[0][2])

    def test_two_windows_replaces_on_one_file_both_apply(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launcher = root / 'skills/x/impeccable.cmd'
            launcher.parent.mkdir(parents=True)
            launcher.write_text('"%~1" engine-probe\nwhere curl.exe\ncurl.exe url\n', encoding='utf-8', newline='\n')
            replace = {'plugins': {}, 'windows': {'impeccable': [
                ('skills/x/impeccable.cmd', '"%~1" engine-probe', 'call "%~1" engine-probe'),
                ('skills/x/impeccable.cmd', 'where curl.exe', 'exit /b 1')]}}
            writes = module.plan(REGISTRY, [('Codex', IDENTITY, root)], replace)
            self.assertEqual(writes[0][2], 'call "%~1" engine-probe\nexit /b 1\ncurl.exe url\n')

    def test_multiline_windows_replace_preserves_native_crlf_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launcher = root / 'skills/x/impeccable.cmd'
            launcher.parent.mkdir(parents=True)
            original = 'rem keep\r\nwhere impeccable\r\ncall :probe impeccable\r\nrem tail\r\n'
            launcher.write_bytes(original.encode())
            replacement = {'plugins': {}, 'windows': {'impeccable': [
                ('skills/x/impeccable.cmd', 'where impeccable\ncall :probe impeccable',
                 'set "path_bin="\ncall :probe "%path_bin%"')]}}
            writes = module.plan(REGISTRY, [('Codex', IDENTITY, root)], replacement)
            expected = 'rem keep\r\nset "path_bin="\r\ncall :probe "%path_bin%"\r\nrem tail\r\n'
            self.assertEqual(writes[0][1:3], (original, expected))
            launcher.write_bytes(expected.encode())
            replay = module.plan(REGISTRY, [('Codex', IDENTITY, root)], replacement)
            self.assertEqual(replay[0][1:3], (expected, expected))


if __name__ == '__main__':
    unittest.main()
