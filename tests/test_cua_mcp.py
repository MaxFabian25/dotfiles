import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homes import recording_sync
import cua_mcp
import home_files
import pull

# The servers as the user registered them by hand: WSL's through the vendor installer's link, Omarchy's from its
# package, each with the environment config/cua-driver/README.md in the private dotfiles gives it.
HOSTS = {
    'wsl': ('/home/u/.local/bin/cua-driver', {'DISPLAY': ':99', 'CUA_DRIVER_PERMISSION_MODE': 'unrestricted',
                                              'CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS': '1'}),
    'omarchy': ('/usr/lib/cua-driver/cua-driver', {'CUA_DRIVER_PERMISSION_MODE': 'unrestricted',
                                                   'CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS': '1',
                                                   'CUA_DRIVER_RS_ENABLE_WAYLAND': '1'}),
}


def claude_json(command, environment):
    document = {'numStartups': 3, 'projects': {'/home/u': {'hasTrustDialogAccepted': True}},
                'mcpServers': {'cua-driver': {'type': 'stdio', 'command': command, 'args': ['mcp'], 'env': environment}},
                'pluginUsage': {'pstack': 2}}
    return json.dumps(document, indent=2) + '\n'


def codex_toml(command, environment):
    variables = ''.join(f'{name} = "{value}"\n' for name, value in environment.items())
    return ('model = "gpt"\n\n[mcp_servers.cua-driver]\n'
            f'command = "{command}"\nargs = ["mcp"]\ndisabled_tools = ["zoom"]\n\n'
            f'[mcp_servers.cua-driver.env]\n{variables}\n'
            '[mcp_servers.rea]\nenabled = false\ncommand = "npx"\n')


@unittest.skipIf(sys.platform == 'win32', 'a pull points these servers only on Linux')
class PullPointsMcpServersTest(unittest.TestCase):
    """pull.run with a stand-in sync.py, on a home that holds Claude Code's and Codex's cua-driver servers."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.public, self.private, self.home = (self.root / name for name in ('public', 'private', 'home'))
        self.public.mkdir()
        wrapper = self.private / home_files.SOURCE / '.local/bin/cua-driver-defaults'
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text('#!/usr/bin/env python3\n')
        wrapper.chmod(0o755)
        self.switch(True)
        self.claude, self.codex = self.home / '.claude.json', self.home / '.codex/config.toml'
        self.wrapper = str(self.home / '.local/bin/cua-driver-defaults')
        self.machine('wsl')
        # Without these the files would be the ones in the environment's Claude and Codex folders.
        environment = {name: value for name, value in os.environ.items() if name not in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME')}
        patcher = patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def switch(self, wrapper):
        path = self.private / cua_mcp.SWITCH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'wrapper = {str(wrapper).lower()}\n')

    def machine(self, host):
        self.driver, self.environment = HOSTS[host]
        self.codex.parent.mkdir(parents=True, exist_ok=True)
        self.claude.write_text(claude_json(self.driver, self.environment))
        self.codex.write_text(codex_toml(self.driver, self.environment))
        self.originals = self.claude.read_bytes(), self.codex.read_bytes()

    def pull(self, status=0, apply=True, platform='linux'):
        (self.public / 'sync.py').write_text(recording_sync(status), encoding='utf-8')
        args = SimpleNamespace(root=self.public, private=self.private, update=False, apply=apply, notes=False, wait=0)
        out, err = io.StringIO(), io.StringIO()
        with patch('pull.Path.home', return_value=self.home), patch('pull.clean'), patch('pull.install_tools'), \
                patch.object(pull.sys, 'platform', platform), patch('pull.git', return_value='0' * 40), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            return pull.run(args), out.getvalue(), err.getvalue()

    def servers(self):
        return (json.loads(self.claude.read_text())['mcpServers']['cua-driver'],
                tomllib.loads(self.codex.read_text())['mcp_servers']['cua-driver'])

    def snapshot(self):
        return [(path.read_bytes(), path.stat().st_mtime_ns) for path in (self.claude, self.codex)]

    def test_a_pull_points_both_servers_at_the_wrapper_and_a_second_pull_changes_nothing(self):
        for host in HOSTS:
            with self.subTest(host=host):
                self.machine(host)
                status, out, err = self.pull()
                self.assertEqual((status, err), (0, ''))
                self.assertIn('MCP server pointed at cua-driver-defaults: cua-driver in ~/.claude.json\n'
                              'MCP server pointed at cua-driver-defaults: cua-driver in ~/.codex/config.toml\n', out)
                claude, codex = self.servers()
                self.assertEqual(claude, {'type': 'stdio', 'command': self.wrapper, 'args': ['mcp'],
                                          'env': {**self.environment, 'CUA_DRIVER_BIN': self.driver}})
                self.assertEqual(codex, {'command': self.wrapper, 'args': ['mcp'], 'disabled_tools': ['zoom'],
                                         'env': {**self.environment, 'CUA_DRIVER_BIN': self.driver}})
                # Every other key, and in config.toml every other line, stays as it was.
                document = json.loads(self.claude.read_text())
                self.assertEqual({key: value for key, value in document.items() if key != 'mcpServers'},
                                 {key: value for key, value in json.loads(self.originals[0]).items() if key != 'mcpServers'})
                self.assertEqual(self.codex.read_text(), self.originals[1].decode().replace(
                    f'command = "{self.driver}"', f'command = "{self.wrapper}"').replace(
                    '\n\n[mcp_servers.rea]', f'\nCUA_DRIVER_BIN = "{self.driver}"\n\n[mcp_servers.rea]'))
                self.assertFalse(Path(f'{self.claude}.lock').exists())
                before = self.snapshot()
                self.assertEqual(self.pull(), (0, '', ''))
                self.assertEqual(self.snapshot(), before)

    def test_each_file_is_saved_before_its_first_change(self):
        self.pull()
        saved = sorted((self.home / '.local/state/dotfiles/mcp-server-backups').glob('*/0*'))
        self.assertEqual([path.read_bytes() for path in saved], list(self.originals))

    def test_wrapper_false_points_each_server_back_to_the_command_it_had(self):
        self.pull()
        self.switch(False)
        status, out, err = self.pull()
        self.assertEqual((status, err), (0, ''))
        self.assertEqual(out, 'MCP server pointed back at the driver: cua-driver in ~/.claude.json\n'
                              'MCP server pointed back at the driver: cua-driver in ~/.codex/config.toml\n')
        self.assertEqual((self.claude.read_bytes(), self.codex.read_bytes()), self.originals)
        self.assertEqual(self.pull(), (0, '', ''))

    def test_a_server_pointed_at_a_wrapper_that_is_gone_goes_back_to_the_driver(self):
        self.pull()
        os.unlink(self.wrapper)
        with patch('pull.home_files', return_value=False):
            status, out, _ = self.pull()
        self.assertEqual(status, 0)
        self.assertIn('MCP server pointed back at the driver: cua-driver in ~/.codex/config.toml\n', out)
        self.assertEqual((self.claude.read_bytes(), self.codex.read_bytes()), self.originals)

    def delivered(self):
        """The wrapper as an earlier pull delivered it, so that only the servers differ from the release."""
        (self.home / '.local/bin').mkdir(parents=True)
        source = self.private / home_files.SOURCE / '.local/bin/cua-driver-defaults'
        Path(self.wrapper).write_bytes(source.read_bytes())
        os.chmod(self.wrapper, 0o755)

    def test_a_check_names_each_server_it_would_point_and_changes_nothing(self):
        self.delivered()
        self.assertEqual(self.pull(apply=False), (1, 'MCP server to point at cua-driver-defaults: cua-driver in '
                                                     '~/.claude.json\nMCP server to point at cua-driver-defaults: '
                                                     'cua-driver in ~/.codex/config.toml\n', ''))
        self.assertEqual((self.claude.read_bytes(), self.codex.read_bytes()), self.originals)

    def test_nothing_changes_without_the_switch_off_linux_or_after_a_blocked_sync(self):
        self.delivered()
        for platform in ('win32', 'darwin'):
            self.assertEqual(self.pull(platform=platform), (0, '', ''))
        for status in (2, 3):
            self.assertEqual(self.pull(status)[0], status)
        (self.private / cua_mcp.SWITCH).unlink()
        status, out, _ = self.pull()
        self.assertEqual(status, 0)
        self.assertNotIn('MCP server', out)
        self.assertEqual((self.claude.read_bytes(), self.codex.read_bytes()), self.originals)

    def test_a_host_without_the_server_gets_none(self):
        self.claude.write_text('{"numStartups": 3}\n')
        self.codex.unlink()
        self.assertEqual(self.pull()[0], 0)
        self.assertEqual(self.claude.read_text(), '{"numStartups": 3}\n')
        self.assertFalse(self.codex.exists())

    def test_a_server_in_another_shape_is_left_as_it_is_and_named(self):
        shapes = {'another command': {'command': '/opt/driver/run', 'args': ['mcp']},
                  'more arguments': {'command': self.driver, 'args': ['mcp', '--verbose']},
                  'a URL': {'type': 'http', 'url': 'http://127.0.0.1:9/mcp'},
                  'a driver of its own': {'command': self.driver, 'args': ['mcp'],
                                          'env': {'CUA_DRIVER_BIN': '/opt/cua-driver'}}}
        for name, server in shapes.items():
            with self.subTest(shape=name):
                text = json.dumps({'mcpServers': {'cua-driver': server}}, indent=2) + '\n'
                self.claude.write_text(text)
                status, out, err = self.pull()
                self.assertEqual(status, 1)
                self.assertRegex(err, r'^MCP server left as it is: cua-driver in ~/\.claude\.json: ')
                self.assertEqual(self.claude.read_text(), text)
                self.assertEqual(self.servers()[1]['command'], self.wrapper)

    def test_the_pull_waits_for_claude_codes_lock_and_leaves_the_file_to_the_next_pull(self):
        lock = Path(f'{self.claude}.lock')
        lock.mkdir()
        with patch.object(cua_mcp, 'LOCK_PAUSE', 0):
            status, out, err = self.pull()
        self.assertEqual(status, 1)
        self.assertEqual(err, 'MCP server left as it is: cua-driver in ~/.claude.json: a Claude Code process held its '
                              'lock on the file, so the next pull tries again\n')
        self.assertTrue(lock.is_dir())
        self.assertEqual(self.claude.read_bytes(), self.originals[0])
        self.assertEqual(self.servers()[1]['command'], self.wrapper)
        lock.rmdir()
        status, out, _ = self.pull()
        self.assertEqual(status, 0)
        self.assertEqual(out, 'MCP server pointed at cua-driver-defaults: cua-driver in ~/.claude.json\n')

    def test_a_claude_write_just_before_the_lock_is_kept(self):
        taken = cua_mcp.claude_lock

        @contextlib.contextmanager
        def after_claude_wrote(path):
            document = json.loads(path.read_text())
            document['numStartups'] = 4
            path.write_text(json.dumps(document, indent=2) + '\n')
            with taken(path) as held:
                yield held

        with patch.object(cua_mcp, 'claude_lock', after_claude_wrote):
            self.assertEqual(self.pull()[0], 0)
        document = json.loads(self.claude.read_text())
        self.assertEqual((document['numStartups'], document['mcpServers']['cua-driver']['command']), (4, self.wrapper))

    def test_a_switch_that_is_not_one_boolean_stops_nothing_else(self):
        for text in ('wrapper = "yes"\n', 'wrapper = true\nalso = 1\n', 'wrapper = \n'):
            with self.subTest(text=text):
                (self.private / cua_mcp.SWITCH).write_text(text)
                status, out, err = self.pull()
                self.assertEqual(status, 1)
                self.assertTrue(err.startswith('MCP servers not pointed: '), err)
                self.assertTrue((self.home / '.local/state/dotfiles/last-applied.json').exists())
        self.assertEqual((self.claude.read_bytes(), self.codex.read_bytes()), self.originals)


if __name__ == '__main__':
    unittest.main()
