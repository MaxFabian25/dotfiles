import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from host_observation import run
from host_state import DeviceRoots, Host
import portable
from sync_files import Files
from sync_hosts import Apply


NATIVE_CLI = '''
import json
import os
import subprocess
import sys

source, pin, destination = sys.argv[1:]
subprocess.run(['git', 'clone', '--quiet', '--no-checkout', source, destination], check=True)
subprocess.run(['git', '-C', destination, 'checkout', '--quiet', '--detach', pin], check=True)
def config(key):
    return subprocess.run(['git', '-C', destination, 'config', '--default', '', '--get', key],
                          check=True, capture_output=True, text=True).stdout.strip()
print(json.dumps({'autocrlf': config('core.autocrlf'), 'eol': config('core.eol'),
                  'marker': config('fixture.marker'),
                  'codex_home': os.environ.get('CODEX_HOME'),
                  'claude_home': os.environ.get('CLAUDE_CONFIG_DIR')}))
'''


class NativeGitEnvironmentTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.system = self.root / 'git-system'
        self.global_config = self.root / 'git-global'
        self.system.write_bytes(b'[core]\n\tautocrlf = true\n\teol = crlf\n')
        self.global_config.write_bytes(b'[core]\n\tautocrlf = true\n\teol = crlf\n')
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        self.environment.update(GIT_CONFIG_SYSTEM=str(self.system), GIT_CONFIG_GLOBAL=str(self.global_config),
                                GIT_CONFIG_COUNT='3', GIT_CONFIG_KEY_0='fixture.marker',
                                GIT_CONFIG_VALUE_0='keep existing configuration',
                                GIT_CONFIG_KEY_1='core.autocrlf', GIT_CONFIG_VALUE_1='true',
                                GIT_CONFIG_KEY_2='core.eol', GIT_CONFIG_VALUE_2='crlf')
        self.source = self.root / 'upstream'
        self.source.mkdir()
        self.git('init', '--quiet', '-b', 'main')
        self.git('config', 'user.name', 'Native Git Test')
        self.git('config', 'user.email', 'native@example.invalid')
        self.content = b'---\nname: example\ndescription: Native Git fixture.\n---\nPinned instructions.\n'
        (self.source / 'SKILL.md').write_bytes(self.content)
        self.git('-c', 'core.autocrlf=false', '-c', 'core.eol=lf', 'add', '.')
        self.git('commit', '--quiet', '-m', 'Pinned LF fixture')
        self.pin = self.git('rev-parse', 'HEAD')
        (self.source / 'SKILL.md').write_bytes(b'Moving branch content.\n')
        self.git('-c', 'core.autocrlf=false', '-c', 'core.eol=lf', 'add', '.')
        self.git('commit', '--quiet', '-m', 'Move branch after pin')

    def git(self, *arguments, env=None):
        return subprocess.run(['git', '-C', str(self.source), *arguments], check=True, capture_output=True,
                              text=True, timeout=30, env=self.environment if env is None else env).stdout.strip()

    def cli(self, destination):
        return [sys.executable, '-c', NATIVE_CLI, str(self.source), self.pin, str(destination)]

    def assert_checkout(self, destination, output):
        self.assertEqual((destination / 'SKILL.md').read_bytes(), self.content)
        self.assertEqual(output['autocrlf'], 'false')
        self.assertEqual(output['eol'], 'lf')
        self.assertEqual(output['marker'], 'keep existing configuration')
        self.assertEqual(self.git('-C', str(destination), 'rev-parse', 'HEAD'), self.pin)

    def test_inherited_git_configuration_reproduces_crlf_checkout(self):
        for runtime_config in (False, True):
            with self.subTest(runtime_config=runtime_config):
                environment = {key: value for key, value in self.environment.items()
                               if runtime_config or not key.startswith(('GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_',
                                                                        'GIT_CONFIG_VALUE_'))}
                destination = self.root / f'unprotected-{runtime_config}'
                output = subprocess.run(self.cli(destination), env=environment, check=True, capture_output=True,
                                        text=True, timeout=30)
                self.assertEqual((destination / 'SKILL.md').read_bytes(), self.content.replace(b'\n', b'\r\n'))
                self.assertEqual(json.loads(output.stdout)['autocrlf'], 'true')
                self.assertEqual(self.git('-C', str(destination), 'rev-parse', 'HEAD'), self.pin)

    def test_child_environment_overrides_checkout_settings_and_preserves_parent(self):
        destination = self.root / 'protected'
        before = {path: path.read_bytes() for path in (self.system, self.global_config, self.source / '.git/config')}
        with patch.dict(os.environ, self.environment, clear=True):
            parent = dict(os.environ)
            environment = portable.native_cli_environment()
            output = subprocess.run(self.cli(destination), env=environment, check=True, capture_output=True,
                                    text=True, timeout=30)
            self.assert_checkout(destination, json.loads(output.stdout))
            self.assertEqual(dict(os.environ), parent)
            self.assertEqual({key: environment[key] for key in parent if key != 'GIT_CONFIG_COUNT'},
                             {key: value for key, value in parent.items() if key != 'GIT_CONFIG_COUNT'})
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_child_environment_works_without_existing_runtime_configuration(self):
        environment = {key: value for key, value in self.environment.items()
                       if not key.startswith(('GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_'))}
        for count in (None, ''):
            with self.subTest(count=count):
                if count is not None:
                    environment['GIT_CONFIG_COUNT'] = count
                destination = self.root / f'no-runtime-config-{count}'
                with patch.dict(os.environ, environment, clear=True):
                    output = subprocess.run(self.cli(destination), env=portable.native_cli_environment(),
                                            check=True, capture_output=True, text=True, timeout=30)
                self.assertEqual((destination / 'SKILL.md').read_bytes(), self.content)
                self.assertEqual(json.loads(output.stdout)['eol'], 'lf')
                self.assertEqual(self.git('-C', str(destination), 'rev-parse', 'HEAD'), self.pin)

    def test_explicit_child_environment_does_not_inherit_parent(self):
        command = [sys.executable, '-c',
                   "import json, os; print(json.dumps([os.environ.get('PARENT_SETTING'), os.environ.get('CHILD_SETTING')]))"]
        with patch.dict(os.environ, {'PARENT_SETTING': 'not inherited'}):
            for environment, expected in (({}, [None, None]), ({'CHILD_SETTING': 'kept'}, [None, 'kept'])):
                with self.subTest(environment=environment):
                    self.assertEqual(json.loads(run(command, env=environment)), expected)
                    self.assertEqual(environment, {} if expected[1] is None else {'CHILD_SETTING': 'kept'})

    def test_negative_runtime_count_is_rejected_without_changing_parent(self):
        with patch.dict(os.environ, self.environment, clear=True):
            os.environ['GIT_CONFIG_COUNT'] = '-1'
            parent = dict(os.environ)
            with self.assertRaisesRegex(ValueError, 'GIT_CONFIG_COUNT'):
                portable.native_cli_environment()
            self.assertEqual(dict(os.environ), parent)

    def test_observation_cli_passes_byte_preserving_configuration_to_nested_git(self):
        destination = self.root / 'observed'
        with patch.dict(os.environ, self.environment, clear=True):
            parent = dict(os.environ)
            output = json.loads(run(self.cli(destination)))
            self.assert_checkout(destination, output)
            self.assertEqual(dict(os.environ), parent)

    def test_apply_cli_passes_byte_preserving_configuration_and_scoped_homes(self):
        for host in Host:
            with self.subTest(host=host):
                home = self.root / host.value
                home.mkdir()
                destination = home / 'installed'
                operation = Apply.__new__(Apply)
                operation.roots = DeviceRoots.current(home)
                operation.files = Files(home / 'backups')
                operation.device = SimpleNamespace(host=lambda requested: SimpleNamespace(cli=self.cli(destination)))
                with patch.dict(os.environ, self.environment, clear=True):
                    parent = dict(os.environ)
                    output = json.loads(operation.command(host))
                    self.assert_checkout(destination, output)
                    self.assertEqual(output['codex_home'], str(operation.roots.codex_home))
                    self.assertEqual(output['claude_home'], str(operation.roots.claude_home))
                    self.assertEqual(dict(os.environ), parent)


if __name__ == '__main__':
    unittest.main()
