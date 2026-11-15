import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import home_files
import pull
import windows_hooks


class WindowsHooksTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.private, self.home, self.state = (self.root / name for name in ('private', 'home', 'state'))
        self.source = self.private / 'config/agent-env/home/.claude/bin/example.py'
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b'print("release 1")\n')
        (self.source.parent / 'linux-only.py').write_bytes(b'raise SystemExit(2)\n')
        self.manifest = self.private / windows_hooks.SOURCE
        self.document = {
            'files': {'.claude/bin/example.py': 'config/agent-env/home/.claude/bin/example.py'},
            'registrations': {
                'claude': {'hooks': {'PreToolUse': [{
                    'matcher': 'Bash', 'hooks': [{'type': 'command',
                    'command': 'python3 -I "{home}/.claude/bin/example.py"', 'timeout': 10}]}]}},
                'codex': {'hooks': {'PreToolUse': [{
                    'matcher': '^Bash$', 'hooks': [{'type': 'command',
                    'command': 'python3 -I "{home}/.claude/bin/example.py"', 'timeout': 10}]}]}},
            },
        }
        self.save_manifest()

    def save_manifest(self):
        self.manifest.write_text(json.dumps(self.document), encoding='utf-8')

    def write(self, relative, data):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def deliver(self, apply=True):
        return windows_hooks.deliver(self.private, self.home, self.state, apply=apply)

    def test_pull_delivers_windows_selection_without_linux_files(self):
        with patch('pull.Path.home', return_value=self.home), patch.object(pull.sys, 'platform', 'win32'), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(pull.home_files(self.private, self.state, False))
            self.assertFalse(self.home.exists())
            self.assertFalse(pull.home_files(self.private, self.state, True))
            self.assertFalse(pull.home_files(self.private, self.state, True))
        self.assertEqual((self.home / '.claude/bin/example.py').read_bytes(), self.source.read_bytes())
        self.assertFalse((self.home / '.claude/bin/linux-only.py').exists())

    def test_merge_keeps_unrelated_settings_hooks_and_approvals_and_is_repeatable(self):
        other = {'type': 'command', 'command': 'app-notification', 'timeout': 42}
        old = {'type': 'command', 'command': 'python3 -I C:/old/.claude/bin/example.py'}
        settings = self.write('.claude/settings.json', {
            'permissions': {'deny': ['SecretRead']},
            'hooks': {'PreToolUse': [{'matcher': '*', 'hooks': [old, other]}, {'matcher': 'Read', 'hooks': []}],
                      'SessionStart': [{'hooks': [other]}]},
        })
        config = self.home / '.codex/config.toml'
        config.parent.mkdir(parents=True)
        approvals = b'[hooks.state.existing]\ntrusted_hash="keep-this"\n'
        config.write_bytes(approvals)
        self.deliver()
        installed = json.loads(settings.read_bytes())
        self.assertEqual(installed['permissions'], {'deny': ['SecretRead']})
        self.assertEqual(installed['hooks']['SessionStart'], [{'hooks': [other]}])
        self.assertEqual(installed['hooks']['PreToolUse'][0], {'matcher': '*', 'hooks': [other]})
        self.assertEqual(installed['hooks']['PreToolUse'][1], {'matcher': 'Read', 'hooks': []})
        self.assertEqual(len(installed['hooks']['PreToolUse']), 3)
        before = settings.read_bytes()
        self.assertEqual(self.deliver().written, ())
        self.assertEqual(settings.read_bytes(), before)
        self.assertEqual(config.read_bytes(), approvals)
        self.assertTrue(any(path.name.endswith('settings.json')
                            for path in (self.state / 'home-file-backups').rglob('*')))

    def test_unknown_manual_copy_is_not_overwritten_or_registered(self):
        installed = self.home / '.claude/bin/example.py'
        installed.parent.mkdir(parents=True)
        installed.write_bytes(b'local experiment\n')
        result = self.deliver()
        self.assertEqual(result.left[0], ('.claude/bin/example.py', home_files.EDITED))
        self.assertFalse((self.home / '.claude/settings.json').exists())
        self.assertEqual(installed.read_bytes(), b'local experiment\n')
        installed.write_bytes(self.source.read_bytes())
        self.assertEqual(self.deliver().left, ())
        self.source.write_bytes(b'print("release 2")\n')
        self.assertEqual(self.deliver().written, ('.claude/bin/example.py',))
        self.assertEqual(installed.read_bytes(), b'print("release 2")\n')

    def test_current_codex_group_needs_no_write_or_new_approval(self):
        wanted = windows_hooks.render(self.document['registrations'], self.home)['codex']
        hooks = self.write('.codex/hooks.json', wanted)
        before = hooks.read_bytes()
        result = self.deliver()
        self.assertNotIn('.codex/hooks.json', result.written)
        self.assertEqual(hooks.read_bytes(), before)

    def test_invalid_settings_are_left_and_pull_reports_failure(self):
        settings = self.home / '.claude/settings.json'
        settings.parent.mkdir(parents=True)
        settings.write_bytes(b'{"hooks": []}')
        with patch('pull.Path.home', return_value=self.home), patch.object(pull.sys, 'platform', 'win32'), \
                contextlib.redirect_stderr(io.StringIO()) as errors, contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(pull.home_files(self.private, self.state, True))
        self.assertIn('invalid Windows hook settings', errors.getvalue())
        self.assertEqual(settings.read_bytes(), b'{"hooks": []}')

    def test_missing_or_escaping_sources_stop_before_any_delivery(self):
        for source in ('config/missing.py', '../outside.py', 'C:/outside.py'):
            with self.subTest(source=source):
                self.document['files']['.claude/bin/example.py'] = source
                self.save_manifest()
                with self.assertRaises(ValueError):
                    self.deliver()
                self.assertFalse(self.home.exists())

    def test_failed_registration_write_leaves_settings_and_reports_failure(self):
        self.deliver()
        settings = self.home / '.claude/settings.json'
        before = settings.read_bytes()
        self.document['registrations']['claude']['hooks']['PreToolUse'][0]['hooks'][0]['timeout'] = 20
        self.save_manifest()
        for error in (PermissionError('write refused'), home_files.ConcurrentEdit('Changed during sync')):
            with self.subTest(error=type(error).__name__), patch.object(windows_hooks.Files, 'write', side_effect=error), \
                    patch('pull.Path.home', return_value=self.home), patch.object(pull.sys, 'platform', 'win32'), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertTrue(pull.home_files(self.private, self.state, True))
            self.assertIn(str(error), errors.getvalue())
            self.assertEqual(settings.read_bytes(), before)

    def test_render_quotes_windows_paths_and_keeps_the_wsl_dispatch_route(self):
        source = {'command': 'python3 -I "{home}/.claude/bin/example.py"', 'wsl': '{wsl_home}'}
        class Home:
            def as_posix(self):
                return 'C:/Users/Test User'
        result = windows_hooks.render(source, Home())
        self.assertEqual(result, {'command': 'python3 -I "C:/Users/Test User/.claude/bin/example.py"',
                                  'wsl': '/mnt/c/Users/Test User'})
        class Unsafe:
            def as_posix(self):
                return 'C:/Users/unsafe$home'
        with self.assertRaises(ValueError):
            windows_hooks.render(source, Unsafe())

    def test_old_private_release_without_a_selection_is_unchanged(self):
        self.manifest.unlink()
        self.assertEqual(self.deliver(), home_files.Delivery((), ()))
        self.assertFalse(self.home.exists())


if __name__ == '__main__':
    unittest.main()
