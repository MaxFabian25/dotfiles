import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('invocation_overrides', Path(__file__).resolve().parents[1] / 'invocation_overrides.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

COMMAND = 'cmd /c if exist "${CLAUDE_PLUGIN_ROOT}\\run.cmd" "${CLAUDE_PLUGIN_ROOT}\\run.cmd" hook'
HANDLER = {'type': 'command', 'command': '[ ! -f "${CLAUDE_PLUGIN_ROOT}/run" ] || sh "${CLAUDE_PLUGIN_ROOT}/run" hook',
           'windows': 'cmd /c ${CLAUDE_PLUGIN_ROOT}\\run.cmd hook', 'env': {'HARNESS': 'vscode'}, 'timeout': 5,
           'statusMessage': 'Checking'}
MANIFEST = {'hooks': {'SessionStart': [{'hooks': [dict(HANDLER)]}],
                      'PostToolUse': [{'matcher': 'Edit|Write', 'hooks': [dict(HANDLER)]}],
                      'Stop': [{'hooks': [dict(HANDLER, timeout=30)]}]}}
IDENTITY = 'impeccable@personal'
REGISTRY = {'targets': {'Codex': {IDENTITY: []}}, 'explicitTargets': {'Codex': {IDENTITY: []}}}


def render(manifest):
    return json.dumps(manifest, indent=2, ensure_ascii=False) + '\n'


class WindowsHookCommandTest(unittest.TestCase):
    def plan(self, manifest, adaptations):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'hooks').mkdir()
            (root / 'hooks/hooks.json').write_text(render(manifest), encoding='utf-8')
            writes, _ = module.plan(REGISTRY, [('Codex', IDENTITY, root)], None, adaptations)
        return [(Path(path).name, old, new) for path, old, new, _, _ in writes]

    def test_each_command_hook_gains_commandWindows_and_nothing_else_changes(self):
        writes = self.plan(MANIFEST, {'plugins': {}, 'windows': {'impeccable': {'hookCommand': COMMAND}}})
        self.assertEqual([w[0] for w in writes], ['hooks.json'])
        _, old, new = writes[0]
        stripped = json.loads(new)
        for groups in stripped['hooks'].values():
            for group in groups:
                for handler in group['hooks']:
                    self.assertEqual(handler.pop('commandWindows'), COMMAND)
        self.assertEqual(render(stripped), old)

    def test_windows_replace_patches_one_file_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launcher = root / 'skills/x/impeccable.cmd'
            launcher.parent.mkdir(parents=True)
            launcher.write_text('rem head\n"%~1" engine-probe >"%tmp%" 2>nul\nrem tail\n', encoding='utf-8')
            replace = {'plugins': {}, 'windows': {'impeccable': {'replace': [
                ('skills/x/impeccable.cmd', '"%~1" engine-probe >"%tmp%" 2>nul', 'call "%~1" engine-probe >"%tmp%" 2>nul')]}}}
            writes, _ = module.plan(REGISTRY, [('Codex', IDENTITY, root)], None, replace)
            self.assertEqual(writes[0][2], 'rem head\ncall "%~1" engine-probe >"%tmp%" 2>nul\nrem tail\n')
            launcher.write_text('rem an upstream rewrite with no such line\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Review upstream change to an adapted file'):
                module.plan(REGISTRY, [('Codex', IDENTITY, root)], None, replace)
            launcher.write_text('call "%~1" engine-probe >"%tmp%" 2>nul\n', encoding='utf-8')
            writes, _ = module.plan(REGISTRY, [('Codex', IDENTITY, root)], None, replace)
            self.assertEqual(writes[0][1], writes[0][2])

    def test_two_windows_replaces_on_one_file_both_apply(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launcher = root / 'skills/x/impeccable.cmd'
            launcher.parent.mkdir(parents=True)
            launcher.write_text('"%~1" engine-probe\nwhere curl.exe\ncurl.exe url\n', encoding='utf-8')
            replace = {'plugins': {}, 'windows': {'impeccable': {'replace': [
                ('skills/x/impeccable.cmd', '"%~1" engine-probe', 'call "%~1" engine-probe'),
                ('skills/x/impeccable.cmd', 'where curl.exe', 'exit /b 1')]}}}
            writes, _ = module.plan(REGISTRY, [('Codex', IDENTITY, root)], None, replace)
            self.assertEqual(writes[0][2], 'call "%~1" engine-probe\nexit /b 1\ncurl.exe url\n')

    def test_no_declaration_leaves_the_hook_file_alone(self):
        self.assertEqual(self.plan(MANIFEST, {'plugins': {}}), [])

    def test_multiline_windows_replace_preserves_native_crlf_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launcher = root / 'skills/x/impeccable.cmd'
            launcher.parent.mkdir(parents=True)
            original = 'rem keep\r\nwhere impeccable\r\ncall :probe impeccable\r\nrem tail\r\n'
            launcher.write_bytes(original.encode())
            replacement = {'plugins': {}, 'windows': {'impeccable': {'replace': [
                ('skills/x/impeccable.cmd', 'where impeccable\ncall :probe impeccable',
                 'set "path_bin="\ncall :probe "%path_bin%"')]}}}
            writes, _ = module.plan(REGISTRY, [('Codex', IDENTITY, root)], None, replacement)
            expected = 'rem keep\r\nset "path_bin="\r\ncall :probe "%path_bin%"\r\nrem tail\r\n'
            self.assertEqual(writes[0][1:3], (original, expected))
            launcher.write_bytes(expected.encode())
            replay, _ = module.plan(REGISTRY, [('Codex', IDENTITY, root)], None, replacement)
            self.assertEqual(replay[0][1:3], (expected, expected))

    def test_a_manifest_that_already_sets_commandWindows_keeps_it(self):
        manifest = json.loads(render(MANIFEST))
        manifest['hooks']['Stop'][0]['hooks'][0]['commandWindows'] = 'upstream form'
        writes = self.plan(manifest, {'plugins': {}, 'windows': {'impeccable': {'hookCommand': COMMAND}}})
        new = json.loads(writes[0][2])
        self.assertEqual(new['hooks']['Stop'][0]['hooks'][0]['commandWindows'], 'upstream form')
        self.assertEqual(new['hooks']['SessionStart'][0]['hooks'][0]['commandWindows'], COMMAND)


if __name__ == '__main__':
    unittest.main()
