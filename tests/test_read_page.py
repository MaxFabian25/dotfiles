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


TREE = '''- [0] AXWindow "Plans - Google Chrome" [actions=[raise]]
    - [4] AXTextField "Address and search bar" = "example.com/plans" [actions=[press]]
  - [11] AXToolbar "Bookmarks"
    - [12] AXButton "Private bookmark" [actions=[press]]
  - [23] AXWebArea "Plans"
    - [24] AXHeading "Plans"
    - [25] AXStaticText = "Compare the plans."
    - [26] AXTable
      - [27] AXRow
        - [28] AXCell
          - [29] AXStaticText = "Plan"
        - [30] AXCell
          - [31] AXStaticText = "Price"
      - [32] AXRow
        - [33] AXCell
          - [34] AXStaticText = "Team"
        - [35] AXCell
          - [36] AXStaticText = "25"
    - [37] AXButton "Is there a free trial?" [actions=[press]]
    - [38] AXButton "Can I pay yearly?" [actions=[press]]
      - [39] AXStaticText = "Yes, with a discount."
    - [40] AXButton "Expand Tabs" [actions=[press]]
    - [41] AXPopUpButton "Tab groups" [actions=[press]]
    - [42] AXRadioButton "Another private tab" [actions=[press]]
  - [43] AXButton [actions=[press]]
- [44] AXMenuBar [actions=[cancel]]
  - [45] AXMenuBarItem "History" [actions=[press]]
'''


class PageTextTest(unittest.TestCase):
    def test_reads_only_the_page(self):
        text = read_page.page_text(TREE)
        self.assertIn('# Plans', text)
        for outside in ('Private bookmark', 'Another private tab', 'Tab groups', 'History'):
            self.assertNotIn(outside, text)

    def test_prints_each_table_row_on_one_line(self):
        lines = read_page.page_text(TREE).splitlines()
        self.assertIn('Plan | Price', lines)
        self.assertIn('Team | 25', lines)

    def test_reads_nothing_without_a_web_area(self):
        self.assertEqual(read_page.page_text(TREE.split('  - [23] AXWebArea')[0]), '')

    def test_finds_only_collapsed_questions(self):
        self.assertEqual(read_page.collapsed_questions(TREE), ['Is there a free trial?'])

    def test_compares_the_address_bar_by_site(self):
        self.assertTrue(read_page.same_site('https://www.example.com/a', 'example.com/a'))
        self.assertTrue(read_page.same_site('https://help.example.com/a', 'help.example.com/b'))
        self.assertFalse(read_page.same_site('https://example.com/a', 'login.other.com/sso'))
        self.assertFalse(read_page.same_site('https://example.com/a', ''))


if __name__ == '__main__':
    unittest.main()
