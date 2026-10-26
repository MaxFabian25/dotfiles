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


# Windows' UI Automation tree, as cua-driver.exe prints it on Windows and from WSL.
WINDOWS_TREE = '''- Window "Plans - Google Chrome"
  - Pane "Plans - Google Chrome"
          - [0] Button "Minimize" [id=view_1 actions=[invoke]]
          - [2] Button "Close" [id=view_4 actions=[invoke]]
                - [6] Edit "Address and search bar" [value="example.com/plans" id=view_1012 help="Ask Google or type a URL" actions=[set_value,text]]
            - ToolBar "Bookmarks"
              - [13] Button "Private bookmark" [id=view_1028 actions=[invoke]]
              - [24] Document "Plans" [value="https://example.com/plans" id=RootWebArea actions=[set_value,text,scroll]]
                - [25] Text "Compare the plans." [actions=[text]]
                - Text "FINAL WEEK"
                - Group "Primary navigation"
                    - ListItem "Pricing"
                      - [26] Hyperlink "Pricing" [value="https://example.com/pricing" actions=[invoke,set_value]]
                - Table "Plans"
                    - DataItem "Plan"
                    - DataItem "Price"
                - [27] Button "Is there a free trial?" [actions=[invoke]]
              - [40] Button "Collapse tabs (Ctrl+Shift+L)" [actions=[invoke]]
                      - [41] TabItem "Another private tab" [actions=[select]]
                        - [42] Button "Close" [actions=[invoke]]
'''
CHROME_EXE = r'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'


class FakeWindows(read_page.Driver):
    def __init__(self, **responses):
        super().__init__('cua-driver.exe', 'test')
        self.calls, self.responses = [], responses

    def call(self, tool, **args):
        self.calls.append((tool, args))
        response = self.responses[tool]
        return response.pop(0) if isinstance(response, list) else response


def windows(*ids):
    return {'windows': [{'window_id': i, 'title': f'window {i}', 'bounds': {'height': 900}} for i in ids]}


class WindowsTest(unittest.TestCase):
    def test_reads_only_the_page(self):
        lines = read_page.page_text(WINDOWS_TREE).splitlines()
        self.assertEqual(lines, ['Compare the plans.', 'FINAL WEEK', '[link] Pricing', 'Plan', 'Price',
                                 'Is there a free trial?'])

    def test_finds_only_collapsed_questions(self):
        self.assertEqual(read_page.collapsed_questions(WINDOWS_TREE), ['Is there a free trial?'])

    def test_reads_the_address_bar(self):
        driver = FakeWindows(get_window_state={'tree_markdown': WINDOWS_TREE, 'snapshot_id': 's1'})
        self.assertEqual(read_page.address_bar(driver, 7, 1), ('s1:6', 'example.com/plans'))

    def test_finds_the_users_chrome(self):
        driver = FakeWindows(list_apps={'apps': [{'name': 'chrome.exe', 'bundle_id': None, 'pid': 7, 'running': True},
                                                 {'name': 'Google Chrome', 'bundle_id': CHROME_EXE, 'pid': 0}]})
        with mock.patch.object(read_page, 'command_line', return_value=f'"{CHROME_EXE}" --profile-directory=Default'):
            self.assertEqual(read_page.chrome(driver), 7)

    def test_launches_chrome_by_its_path(self):
        driver = FakeWindows(list_windows=[windows(1), windows(1, 2)], launch_app={},
                             list_apps={'apps': [{'name': 'Google Chrome', 'bundle_id': CHROME_EXE, 'pid': 0}]})
        self.assertEqual(read_page.open_window(driver, 7, 'Default', 'https://example.com/'), 2)
        launch = next(args for tool, args in driver.calls if tool == 'launch_app')
        self.assertEqual(launch['path'], CHROME_EXE)
        self.assertEqual(launch['additional_arguments'], ['--profile-directory=Default', '--new-window',
                                                          'https://example.com/'])

    def test_finds_a_window_chrome_opened_minimized(self):
        minimized = {'window_id': 2, 'title': 'window 2', 'bounds': {'height': 20}, 'minimized': True}
        driver = FakeWindows(list_windows=[windows(1), {'windows': windows(1)['windows'] + [minimized]}], launch_app={},
                             list_apps={'apps': [{'name': 'Google Chrome', 'bundle_id': CHROME_EXE, 'pid': 0}]})
        self.assertEqual(read_page.open_window(driver, 7, 'Default', 'https://example.com/'), 2)

    def test_closes_the_window_through_ui_automation(self):
        driver = FakeWindows()
        with mock.patch.object(read_page.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
            read_page.close(driver, 7, 6622428)
        script = run.call_args.args[0][-1]
        self.assertIn('FromHandle([IntPtr]6622428)', script)
        self.assertIn('ProcessId -ne 7', script)
        self.assertIn('WindowPattern', script)
        self.assertEqual(driver.calls, [])

    def test_reports_a_window_it_could_not_close(self):
        with mock.patch.object(read_page.subprocess, 'run', return_value=mock.Mock(returncode=3)):
            with self.assertRaisesRegex(RuntimeError, 'ask the user to close it'):
                read_page.close(FakeWindows(), 7, 1)


if __name__ == '__main__':
    unittest.main()
