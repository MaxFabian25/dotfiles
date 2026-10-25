"""read_page.py drives the user's own Chrome, never one that automation started."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/browser-and-computer-use/scripts'))
import read_page  # noqa: E402

AGENT_BROWSER = ('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=0 '
                 '--no-first-run --user-data-dir=/var/folders/x/agent-browser-chrome')
OWN = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'


class FakeDriver:
    command = 'cua-driver'

    def __init__(self, pids):
        self.pids = pids

    def call(self, tool, **args):
        assert tool == 'list_apps'
        return {'apps': [{'bundle_id': 'com.google.Chrome', 'name': 'Google Chrome', 'pid': pid, 'running': True}
                         for pid in self.pids]}


class ChromeTest(unittest.TestCase):
    def pick(self, lines):
        with mock.patch.object(read_page, 'command_line', side_effect=lambda pid, cua: lines[pid]):
            return read_page.chrome(FakeDriver(list(lines)))

    def test_skips_a_chrome_that_automation_started(self):
        self.assertEqual(self.pick({31372: AGENT_BROWSER, 85197: OWN}), 85197)

    def test_stops_when_no_chrome_is_the_users_own(self):
        with self.assertRaisesRegex(RuntimeError, "can't tell which Chrome"):
            self.pick({31372: AGENT_BROWSER})

    def test_stops_when_two_could_be_the_users_own(self):
        with self.assertRaisesRegex(RuntimeError, "can't tell which Chrome"):
            self.pick({1: OWN, 2: OWN})

    def test_uses_the_only_chrome_when_its_command_line_is_unreadable(self):
        self.assertEqual(self.pick({85197: ''}), 85197)


if __name__ == '__main__':
    unittest.main()
