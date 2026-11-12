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
from read_account_skill import Client, main, select_source, windows_handoff

STAND_IN = """import json, os, sys
assert sys.argv[1:] == ['app-server', '--stdio'], sys.argv
with open(sys.argv[0] + '.seen', 'w') as seen:
    json.dump({key: os.environ.get(key) for key in ('CODEX_HOME', 'CODEX_SQLITE_HOME')}, seen)
for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message:
        continue
    print(json.dumps({'method': 'notice'}), flush=True)
    result = {'data': [{'errors': [], 'skills': [{'name': 'example:task'}]}]} if message['method'] == 'skills/list' else {}
    print(json.dumps({'id': message['id'], 'result': result}), flush=True)
"""


class SourceSelectionTest(unittest.TestCase):
    def test_reads_the_publisher_even_when_its_discovery_entry_is_disabled(self):
        source = {'name': 'example:task', 'pluginId': 'example@openai-curated-remote',
                  'path': str(Path('/publisher/skills/task/SKILL.md').absolute()), 'enabled': False}
        wrapper = {**source, 'pluginId': None, 'path': '/managed/SKILL.md', 'enabled': True}
        self.assertEqual(select_source([wrapper, source], 'example:task'), Path(source['path']))

    def test_refuses_missing_ambiguous_or_foreign_sources(self):
        source = {'name': 'example:task', 'pluginId': 'example@openai-curated-remote',
                  'path': str(Path('/publisher/skills/task/SKILL.md').absolute())}
        for skills in ([], [source, source], [{**source, 'pluginId': 'example@personal'}]):
            with self.subTest(skills=skills), self.assertRaises(RuntimeError):
                select_source(skills, 'example:task')

    def test_rejects_unqualified_names_and_non_skill_paths(self):
        with self.assertRaises(ValueError):
            select_source([], 'task')
        with self.assertRaises(RuntimeError):
            select_source([{'name': 'example:task', 'pluginId': 'example@openai-curated-remote',
                            'path': '/publisher/settings.json'}], 'example:task')

    @unittest.skipIf(sys.platform == 'win32', 'WSL path translation runs on POSIX')
    def test_wsl_uses_windows_python_and_preserves_the_native_profile(self):
        for profile in ('C:/Users/test person/profile/home', '/mnt/c/Users/test person/profile/home'):
            with self.subTest(profile=profile), patch('portable.WINDOWS', False), patch.object(Path, 'is_file', return_value=True):
                self.assertEqual(windows_handoff(profile, 'read_account_skill.py'), [
                    '/mnt/c/Users/test person/dotfiles/.venv/Scripts/python.exe',
                    'C:/Users/test person/dotfiles/read_account_skill.py', '--profile',
                    'C:/Users/test person/profile/home'])

    @unittest.skipIf(sys.platform == 'win32', 'WSL path translation runs on POSIX')
    def test_without_a_profile_option_a_wsl_read_hands_off_to_the_windows_profile_codex_home_names(self):
        profile = '/mnt/c/Users/test person/profile/home'
        with patch('portable.WINDOWS', False), patch.object(Path, 'is_file', return_value=True), \
                patch.dict(os.environ, {'CODEX_HOME': profile}), \
                patch('read_account_skill.subprocess.run', return_value=SimpleNamespace(returncode=5)) as run:
            self.assertEqual(main(['example:task']), 5)
        self.assertEqual(run.call_args.args[0][-3:], ['--profile', 'C:/Users/test person/profile/home', 'example:task'])

    def test_wsl_refuses_a_missing_windows_runtime(self):
        with patch('portable.WINDOWS', False), patch.object(Path, 'is_file', return_value=False):
            with self.assertRaises(RuntimeError):
                windows_handoff('C:/Users/test/.codex', 'read_account_skill.py')

    def test_native_profiles_need_no_handoff(self):
        with patch('portable.WINDOWS', False):
            self.assertIsNone(windows_handoff('/home/test/.codex', 'read_account_skill.py'))
        with patch('portable.WINDOWS', True):
            self.assertIsNone(windows_handoff('C:/Users/test/.codex', 'read_account_skill.py'))

    def test_publisher_unicode_is_utf8_even_under_a_legacy_console_encoding(self):
        with tempfile.TemporaryDirectory() as temporary:
            skill = Path(temporary) / 'SKILL.md'
            skill.write_bytes('Publisher café 🚀\n'.encode('utf-8'))
            buffer = io.BytesIO()
            output = io.TextIOWrapper(buffer, encoding='ascii')
            errors = io.TextIOWrapper(io.BytesIO(), encoding='ascii')
            with patch('read_account_skill.Client') as client, patch('read_account_skill.windows_handoff', return_value=None), \
                    patch('sys.stdout', output), patch('sys.stderr', errors):
                client.return_value.skills.return_value = [{'name': 'example:task',
                    'pluginId': 'example@openai-curated-remote', 'path': str(skill)}]
                self.assertEqual(main(['example:task']), 0)
                output.flush()
                self.assertIn('Publisher café 🚀', buffer.getvalue().decode('utf-8').splitlines())
            output.close()
            errors.close()



@unittest.skipIf(os.name == 'nt', 'the stand-in app-server is a script with a shebang')
class ClientTest(unittest.TestCase):
    def test_the_app_server_runs_on_the_profile_without_an_inherited_sqlite_home_and_lists_its_skills(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = root / 'codex'
            server.write_text(f'#!{sys.executable}\n' + STAND_IN)
            server.chmod(0o755)
            with patch('read_account_skill.portable.codex_cli', return_value=str(server)), \
                    patch.dict(os.environ, {'CODEX_SQLITE_HOME': str(root / 'sqlite')}):
                client = Client(root / 'profile')
                try:
                    skills = client.skills()
                finally:
                    client.close()
            self.assertEqual(skills, [{'name': 'example:task'}])
            self.assertEqual(json.loads(Path(f'{server}.seen').read_text()), {'CODEX_HOME': str(root / 'profile'), 'CODEX_SQLITE_HOME': None})

    def test_an_app_server_that_exits_before_replying_stops_the_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            server = Path(temporary) / 'codex'
            server.write_text(f'#!{sys.executable}\n')
            server.chmod(0o755)
            with patch('read_account_skill.portable.codex_cli', return_value=str(server)), \
                    self.assertRaisesRegex(RuntimeError, 'Native app-server exited before replying'):
                Client()


if __name__ == '__main__':
    unittest.main()
