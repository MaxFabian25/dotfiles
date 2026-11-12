"""Device.current describes the machine it runs on: its config homes, its platform, and which host CLIs it lacks."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from device import Device  # noqa: E402
from host_state import Host, Platform  # noqa: E402


class CurrentDeviceTest(unittest.TestCase):
    def test_a_machine_without_either_cli_has_both_hosts_absent_and_its_own_homes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {'CODEX_HOME': str(root / 'codex'), 'CLAUDE_CONFIG_DIR': str(root / 'claude'),
                           'CODEX_CLI': str(root / 'no-codex'), 'CLAUDE_CLI': str(root / 'no-claude')}
            with mock.patch.dict(os.environ, environment):
                device = Device.current()
        self.assertEqual((device.hosts, device.absent), ((), (Host.CODEX, Host.CLAUDE)))
        self.assertEqual((device.roots.codex_home, device.roots.claude_home), (root / 'codex', root / 'claude'))
        self.assertEqual(device.platform, Platform.WINDOWS if os.name == 'nt' else
                         Platform.MACOS if sys.platform == 'darwin' else Platform.LINUX)

    def test_a_cli_that_exists_gives_its_host_with_the_device_homes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {'CODEX_HOME': str(root / 'codex'), 'CLAUDE_CONFIG_DIR': str(root / 'claude'),
                           'CODEX_CLI': str(root / 'no-codex'), 'CLAUDE_CLI': sys.executable}
            with mock.patch.dict(os.environ, environment):
                device = Device.current()
        self.assertEqual(device.absent, (Host.CODEX,))
        [claude] = device.hosts
        self.assertEqual((claude.host, claude.roots, claude.cli), (Host.CLAUDE, device.roots, [sys.executable]))


if __name__ == '__main__':
    unittest.main()
