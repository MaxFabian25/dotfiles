"""read_page.py drives the user's own Chrome, never one that automation started."""

import contextlib
import io
import json
import re
import signal
import sys
import tempfile
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


class FakeClock:
    """read_page's time module, where sleep() moves monotonic() on at once."""

    def __init__(self):
        self.now, self.slept = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def __enter__(self):
        self.patch = mock.patch.object(read_page, 'time', self)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


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


# Linux's AT-SPI tree, as cua-driver prints Chrome's on the Omarchy laptop (Hyprland), where containers' names hold
# U+FFFC placeholders.
P = read_page.PLACEHOLDER
LINUX_TREE = f'''- [0] frame "Plans - Google Chrome" [actions=[doDefault,showContextMenu]]
  - [1] panel "{P}{P}" [actions=[doDefault,showContextMenu]]
    - [3] button "Close" [actions=[press,showContextMenu]]
    - [4] panel "{P}{P}{P}" [actions=[doDefault,showContextMenu]]
      - [6] tool bar "{P}{P}" [actions=[doDefault,showContextMenu]]
        - button = "Back" (disabled)
        - [11] entry "Address and search bar" [actions=[activate,showContextMenu]]
      - [20] tool bar "Bookmarks" [actions=[doDefault,showContextMenu]]
        - [21] button "Private bookmark" (description "https://private.example") [actions=[press,showContextMenu]]
    - [31] panel "{P}{P}{P}" [actions=[doDefault,showContextMenu]]
      - [34] document web "Plans" [actions=[doDefault,showContextMenu]]
        - [35] section "{P}" [actions=[click,showContextMenu]]
          - [36] heading "Plans" [actions=[clickAncestor,showContextMenu]]
            - static = "Plans"
          - [37] paragraph "{P}" [actions=[clickAncestor,showContextMenu]]
            - [38] link "Compare the plans" [actions=[jump,showContextMenu]]
              - [39] section "Compare the plans" [actions=[clickAncestor,showContextMenu]]
                - static = "Compare the plans"
          - static = "4 HOURS AGO"
          - [40] section "{P}" [actions=[clickAncestor,showContextMenu]]
          - [41] table "{P}" [actions=[showContextMenu]]
            - [42] table row "{P}" [actions=[showContextMenu]]
              - [43] table column header "Plan" [actions=[showContextMenu]]
                - static = "Plan"
              - [44] table column header "Price" [actions=[showContextMenu]]
                - static = "Price"
            - [45] table row "{P}" [actions=[showContextMenu]]
              - [46] table cell "Team" [actions=[showContextMenu]]
                - static = "Team"
              - [47] table cell "25" [actions=[showContextMenu]]
                - static = "25"
          - [48] push button "Is there a free trial?" [actions=[press,showContextMenu]]
            - static = "Is there a free trial?"
          - [49] push button "Can I pay yearly?" [actions=[press,showContextMenu]]
            - static = "Can I pay yearly?"
          - static = "Yes, with a discount."
          - image = "Team photo"
          - [50] link "{P}" [actions=[jump,showContextMenu]]
            - static = "Read more"
    - [50] panel "{P}{P}" [actions=[doDefault,showContextMenu]]
      - [52] page tab list "{P}{P}" [actions=[doDefault,showContextMenu]]
        - [53] page tab "Another private tab - Memory usage - 203 MB" [actions=[doDefault,showContextMenu]]
'''


class LinuxTreeTest(unittest.TestCase):
    def test_reads_only_the_page_once(self):
        self.assertEqual(read_page.page_text(LINUX_TREE).splitlines(),
                         ['# Plans', '[link] Compare the plans', '4 HOURS AGO', 'Plan | Price', 'Team | 25',
                          'Is there a free trial?', 'Can I pay yearly?', 'Yes, with a discount.', '[image] Team photo',
                          'Read more'])

    def test_finds_only_collapsed_questions(self):
        self.assertEqual(read_page.collapsed_questions(LINUX_TREE), ['Is there a free trial?'])

    def test_reads_the_page_once_its_text_stops_growing(self):
        half = LINUX_TREE.split('          - [41] table')[0]
        empty = LINUX_TREE.split('        - [35] section')[0]
        reads = [{'tree_markdown': t} for t in (empty, half, LINUX_TREE, LINUX_TREE)]
        driver = mock.Mock(tree=mock.Mock(side_effect=reads))
        with FakeClock() as clock:
            self.assertEqual(read_page.settled_tree(driver, 7, 1, 30), reads[2])
        self.assertEqual(driver.tree.call_count, 4)
        self.assertEqual(clock.slept, [2, 2, 2])
        driver = mock.Mock(tree=mock.Mock(return_value={'tree_markdown': empty}))
        with FakeClock():
            read_page.settled_tree(driver, 7, 1, 6)
        self.assertEqual(driver.tree.call_count, 4)  # a page that never shows text stops at the deadline

    def test_presses_a_collapsed_question(self):
        driver = mock.Mock(tree=mock.Mock(return_value={'tree_markdown': LINUX_TREE, 'snapshot_id': 's1'}))
        with mock.patch.object(read_page.time, 'sleep'):
            read_page.expand(driver, 7, 1, ['Is there a free trial?'])
        driver.call.assert_called_once_with('click', pid=7, window_id=1, element_token='s1:48')


def hypr_window(address, pid, cls='google-chrome'):
    return {'address': address, 'pid': pid, 'class': cls, 'workspace': {'name': '1'}, 'title': 't'}


class FakeHyprctl:
    """Answers hyprctl calls in order of their first argument and records every call."""

    def __init__(self, clients, answers=None):
        self.clients, self.calls, self.answers = list(clients), [], answers or {}

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[:2] == ['hyprctl', 'clients']:
            return mock.Mock(returncode=0, stdout=json.dumps(self.clients.pop(0) if len(self.clients) > 1
                                                             else self.clients[0]), stderr='')
        said = next((answer for part, answer in self.answers.items() if part in argv[-1]), 'ok')
        return mock.Mock(returncode=0, stdout=said, stderr='')


RUNTIME = {'XDG_RUNTIME_DIR': '/run/user/1000'}  # so that no test reaches os.getuid, which Windows lacks


def hyprland():
    with mock.patch.dict(read_page.os.environ, RUNTIME):
        return read_page.Hyprland({'instance': 'abc_1', 'wl_socket': 'wayland-1'})


def on_linux(test):
    """Run a test as on the Omarchy laptop, which the suite's Windows and macOS runners are not."""
    return mock.patch.object(read_page.sys, 'platform', 'linux')(mock.patch.dict(read_page.os.environ, RUNTIME)(test))


class HyprlandTest(unittest.TestCase):
    @on_linux
    def test_finds_the_one_running_session(self):
        found = json.dumps([{'instance': 'abc_1', 'wl_socket': 'wayland-1'}])
        with mock.patch.object(read_page.shutil, 'which', return_value='/usr/bin/hyprctl'), \
                mock.patch.object(read_page.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=found, stderr='')):
            desktop = read_page.Hyprland.find()
        self.assertEqual(desktop.env['HYPRLAND_INSTANCE_SIGNATURE'], 'abc_1')
        self.assertEqual(desktop.env['WAYLAND_DISPLAY'], 'wayland-1')
        self.assertEqual(desktop.env['CUA_DRIVER_RS_ENABLE_WAYLAND'], '1')

    @on_linux
    def test_needs_the_signature_when_several_sessions_run(self):
        found = json.dumps([{'instance': 'a', 'wl_socket': 'wayland-1'}, {'instance': 'b', 'wl_socket': 'wayland-2'}])
        with mock.patch.object(read_page.shutil, 'which', return_value='/usr/bin/hyprctl'), \
                mock.patch.dict(read_page.os.environ, {'HYPRLAND_INSTANCE_SIGNATURE': 'b'}), \
                mock.patch.object(read_page.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=found, stderr='')):
            self.assertEqual(read_page.Hyprland.find().env['WAYLAND_DISPLAY'], 'wayland-2')
            read_page.os.environ['HYPRLAND_INSTANCE_SIGNATURE'] = 'gone'
            with self.assertRaisesRegex(RuntimeError, 'several Hyprland sessions'):
                read_page.Hyprland.find()

    @on_linux
    def test_says_why_hyprctl_gave_no_sessions(self):
        for broken, said in ((mock.Mock(returncode=0, stdout='not json', stderr=''), 'not json'),
                             (mock.Mock(returncode=127, stdout='', stderr='error while loading shared libraries'),
                              'error while loading')):
            with self.subTest(said=said), mock.patch.object(read_page.shutil, 'which', return_value='/usr/bin/hyprctl'), \
                    mock.patch.object(read_page.subprocess, 'run', return_value=broken):
                with self.assertRaisesRegex(RuntimeError, f'hyprctl instances: {said}'):
                    read_page.Hyprland.find()

    def test_says_why_hyprctl_listed_no_windows(self):
        with mock.patch.object(read_page.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='[{"a": 1', stderr='')):
            with self.assertRaisesRegex(RuntimeError, 'hyprctl clients'):
                hyprland().windows()

    def test_is_absent_without_hyprland(self):
        found = json.dumps([{'instance': 'abc_1', 'wl_socket': 'wayland-1'}])
        for platform, hyprctl in (('linux', None), ('darwin', '/usr/bin/hyprctl'), ('win32', '/usr/bin/hyprctl')):
            with self.subTest(platform=platform), mock.patch.object(read_page.sys, 'platform', platform), \
                    mock.patch.object(read_page.shutil, 'which', return_value=hyprctl), \
                    mock.patch.object(read_page.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=found, stderr='')):
                self.assertIsNone(read_page.Hyprland.find())

    def test_opens_a_hidden_window_and_turns_its_rule_off(self):
        user = hypr_window('0x10', 7)
        run = FakeHyprctl([[user], [user, hypr_window('0x20', 7)]])
        with mock.patch.object(read_page.subprocess, 'run', side_effect=run), \
                mock.patch.object(read_page.shutil, 'which', return_value='/usr/bin/google-chrome-stable'):
            self.assertEqual(hyprland().open('Default', 'https://example.com/a?b=c&d'), (0x20, 7))
        evals = [argv[2] for argv in run.calls if argv[1] == 'eval']
        self.assertEqual(len(evals), 2)
        for setting in (f'workspace = "{read_page.HIDDEN} silent"', 'no_initial_focus = true',
                        'render_unfocused = true', 'timeout = 60000 }',
                        "env ACCESSIBILITY_ENABLED=1 /usr/bin/google-chrome-stable --profile-directory=Default "
                        "--force-renderer-accessibility --new-window 'https://example.com/a?b=c&d'"):
            self.assertIn(setting, evals[0])
        self.assert_turns_off_only_its_own_rule(evals)
        # The new window alone keeps rendering once the rule is off.
        dispatches = [argv[2] for argv in run.calls if argv[1] == 'dispatch']
        self.assertEqual(dispatches, ['hl.dsp.window.set_prop({ window = "address:0x20", prop = "render_unfocused", '
                                      'value = "1" })'])

    def test_closes_its_window_when_it_cannot_keep_it_rendering(self):
        """A Hyprland that renames the property would otherwise leave each run's window hidden."""
        run = FakeHyprctl([[], [hypr_window('0x20', 7)]], answers={'set_prop': 'error: Invalid prop name'})
        with mock.patch.object(read_page.subprocess, 'run', side_effect=run), \
                mock.patch.object(read_page.shutil, 'which', return_value='/usr/bin/google-chrome-stable'):
            with self.assertRaisesRegex(RuntimeError, 'hyprctl dispatch: error: Invalid prop name'):
                hyprland().open('Default', 'https://example.com/')
        self.assertEqual([argv[2] for argv in run.calls if argv[1] == 'dispatch'][-1],
                         'hl.dsp.window.close({ window = "address:0x20" })')
        self.assert_turns_off_only_its_own_rule([argv[2] for argv in run.calls if argv[1] == 'eval'])

    def test_keeps_both_errors_when_its_window_will_not_close_either(self):
        run = FakeHyprctl([[], [hypr_window('0x20', 7)]],
                          answers={'set_prop': 'error: Invalid prop name', 'window.close': 'error: no such window'})
        with mock.patch.object(read_page.subprocess, 'run', side_effect=run), \
                mock.patch.object(read_page.shutil, 'which', return_value='/usr/bin/google-chrome-stable'):
            with self.assertRaisesRegex(RuntimeError, 'Invalid prop name; closing window address:0x20 also failed: '
                                                      'hyprctl dispatch: error: no such window; ask the user'):
                hyprland().open('Default', 'https://example.com/')
        self.assert_turns_off_only_its_own_rule([argv[2] for argv in run.calls if argv[1] == 'eval'])

    def assert_turns_off_only_its_own_rule(self, evals):
        """Both the minute's timer and the cleanup turn the rule off only while it is still this run's."""
        generation = re.search(r'READ_PAGE_GENERATION = "(\w+)"', evals[0])[1]
        off = f'if READ_PAGE_GENERATION == "{generation}" then READ_PAGE_RULE:set_enabled(false) end'
        self.assertIn(f'hl.timer(function() {off} end', evals[0])
        self.assertEqual(evals[-1], off)

    def test_gives_each_run_its_own_generation(self):
        generations = set()
        for _ in range(2):
            run = FakeHyprctl([[], [hypr_window('0x20', 7)]])
            with mock.patch.object(read_page.subprocess, 'run', side_effect=run), \
                    mock.patch.object(read_page.shutil, 'which', return_value='/usr/bin/google-chrome-stable'):
                hyprland().open('Default', 'https://example.com/')
            generations.add(re.search(r'READ_PAGE_GENERATION = "(\w+)"', run.calls[1][2])[1])
        self.assertEqual(len(generations), 2)

    def test_turns_the_rule_off_when_no_window_appears(self):
        run = FakeHyprctl([[hypr_window('0x10', 7)]])
        with mock.patch.object(read_page.subprocess, 'run', side_effect=run), \
                mock.patch.object(read_page.shutil, 'which', return_value='/usr/bin/google-chrome-stable'), \
                mock.patch.object(read_page.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'no new Chrome window'):
                hyprland().open('Default', 'https://example.com/', wait=1)
        self.assert_turns_off_only_its_own_rule([argv[2] for argv in run.calls if argv[1] == 'eval'])

    def test_stops_at_once_when_several_windows_open(self):
        run = FakeHyprctl([[], [hypr_window('0x20', 7), hypr_window('0x30', 7)]])
        with mock.patch.object(read_page.subprocess, 'run', side_effect=run), \
                mock.patch.object(read_page.shutil, 'which', return_value='/usr/bin/google-chrome-stable'), \
                mock.patch.object(read_page.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, '2 Chrome windows opened at once'):
                hyprland().open('Default', 'https://example.com/')
        sleep.assert_not_called()
        self.assert_turns_off_only_its_own_rule([argv[2] for argv in run.calls if argv[1] == 'eval'])

    def test_closes_only_a_window_of_that_chrome(self):
        run = FakeHyprctl([[hypr_window('0x20', 7), hypr_window('0x30', 8)]])
        with mock.patch.object(read_page.subprocess, 'run', side_effect=run):
            hyprland().close(7, 0x20)
            with self.assertRaisesRegex(RuntimeError, 'ask the user to close it'):
                hyprland().close(7, 0x30)
            hyprland().close(7, 0x40)  # already closed
        closes = [argv[2] for argv in run.calls if argv[1] == 'dispatch']
        self.assertEqual(closes, ['hl.dsp.window.close({ window = "address:0x20" })'])

    def test_refuses_an_address_hyprland_cannot_take(self):
        with mock.patch.object(read_page.subprocess, 'run') as run, \
                mock.patch.object(read_page.shutil, 'which', return_value='/usr/bin/google-chrome-stable'):
            with self.assertRaisesRegex(RuntimeError, 'cannot pass this address'):
                hyprland().open('Default', 'https://example.com/]==]')
        run.assert_not_called()

    def test_stops_on_a_hyprctl_error(self):
        for result in (mock.Mock(returncode=0, stdout='error: hl.dispatch: expected a dispatcher', stderr=''),
                       mock.Mock(returncode=7, stdout='', stderr='')):
            with self.subTest(result=result), mock.patch.object(read_page.subprocess, 'run', return_value=result):
                with self.assertRaisesRegex(RuntimeError, 'hyprctl eval'):
                    hyprland().hyprctl('eval', 'x')

    def test_tells_whether_chrome_is_in_the_accessibility_tree(self):
        for result, expected in ((mock.Mock(returncode=0, stdout='', stderr=''), True),
                                 (mock.Mock(returncode=1, stdout='', stderr='no accessible application for pid 7'), False)):
            with self.subTest(result=result), \
                    mock.patch.object(read_page.Hyprland, 'atspi', return_value=result) as atspi:
                self.assertIs(hyprland().accessible(7), expected)
            # The application object is the walk's first node, so the walk stops there.
            atspi.assert_called_once_with(7, '--role', '^application$', '--name', '', '--limit', '1')

    def test_says_why_the_accessibility_check_failed(self):
        """A failed check names its cause, rather than blaming the user's Chrome."""
        broken = mock.Mock(returncode=1, stdout='', stderr='ModuleNotFoundError: jeepney')
        with mock.patch.object(read_page.Hyprland, 'atspi', return_value=broken):
            with self.assertRaisesRegex(RuntimeError, 'atspi_do.py: ModuleNotFoundError: jeepney'):
                hyprland().accessible(7)
        with mock.patch.object(read_page.subprocess, 'run', side_effect=FileNotFoundError('uv')):
            with self.assertRaisesRegex(RuntimeError, 'atspi_do.py could not run'):
                hyprland().accessible(7)

    def test_reads_the_address_from_the_page_document(self):
        result = mock.Mock(returncode=0, stdout='uri=https://example.com/a\n', stderr='')
        with mock.patch.object(read_page.subprocess, 'run', return_value=result) as run:
            self.assertEqual(hyprland().address(7, 'A page - Google Chrome'), 'https://example.com/a')
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index('--frame') + 1], 'A page - Google Chrome')
        self.assertIn('--uri', argv)
        same = mock.Mock(returncode=0, stdout='uri=https://www.ft.com/\nuri=https://www.ft.com/world\n', stderr='')
        with mock.patch.object(read_page.subprocess, 'run', return_value=same):
            # The user's own window with the same title, on the same site, as on a weekday with the FT left open
            self.assertEqual(hyprland().address(7, 'Home - Financial Times'), 'https://www.ft.com/')
        for returncode, stdout, stderr, why in (
                (1, '', "expected one frame named 'A page', found 0", "no window titled 'A page' in the accessibility tree"),
                (0, '', '', 'its page has no address yet'),  # a frame whose page hasn't loaded
                (0, 'uri=https://a.example/\nuri=https://b.example/\n', '',
                 "2 windows titled 'A page' show different sites")):
            desktop = hyprland()
            with self.subTest(stdout=stdout), mock.patch.object(
                    read_page.subprocess, 'run', return_value=mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)):
                self.assertEqual(desktop.address(7, 'A page'), '')
            self.assertEqual(desktop.why, why)

    def test_waits_for_a_loading_page_to_show_its_address(self):
        driver = mock.Mock(windows=mock.Mock(return_value={1: 'The Economist'}))
        desktop = hyprland()
        with mock.patch.object(read_page.Hyprland, 'address', side_effect=['', '', 'https://www.economist.com/']) as address, \
                FakeClock() as clock:
            self.assertEqual(desktop.wait_for_address(driver, 7, 1, 30), 'https://www.economist.com/')
        self.assertEqual(address.call_count, 3)
        address.assert_called_with(7, 'The Economist')
        self.assertEqual(clock.slept, [2, 2])
        with mock.patch.object(read_page.Hyprland, 'address', return_value='') as address, FakeClock() as clock:
            self.assertEqual(desktop.wait_for_address(driver, 7, 1, 14), '')
        self.assertEqual((address.call_count, clock.now), (8, 14))
        # The title is read on each try, because a loading page changes it, and an empty title stops the frame match.
        driver.windows.side_effect = [{1: ''}, {1: 'Loading'}, {1: 'The Economist'}]
        with mock.patch.object(read_page.Hyprland, 'address', side_effect=['', '', 'https://www.economist.com/']) as address, \
                FakeClock():
            desktop.wait_for_address(driver, 7, 1, 30)
        self.assertEqual([c.args for c in address.call_args_list], [(7, ''), (7, 'Loading'), (7, 'The Economist')])
        for stderr in ('jeepney.wrappers.DBusErrorResponse: no bus', 'no accessible application for pid 7'):
            with self.subTest(stderr=stderr), mock.patch.object(
                    read_page.subprocess, 'run', return_value=mock.Mock(returncode=1, stdout='', stderr=stderr)):
                with self.assertRaisesRegex(RuntimeError, stderr):
                    hyprland().address(7, 'A page')

    @unittest.skipIf(sys.platform == 'win32', 'fcntl exists only on Linux and macOS')
    def test_lets_one_run_at_a_time(self):
        with tempfile.TemporaryDirectory() as runtime:
            desktop = hyprland()
            desktop.env['XDG_RUNTIME_DIR'] = runtime
            with contextlib.redirect_stderr(io.StringIO()) as said:
                desktop.lock()
            self.assertEqual(said.getvalue(), '')  # a run that didn't wait says nothing
            with open(Path(runtime) / 'read-page.lock') as other:
                import fcntl
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)  # even a shared lock waits
            desktop.held.close()

    @unittest.skipIf(sys.platform == 'win32', 'fcntl exists only on Linux and macOS')
    def test_waits_for_the_run_that_holds_the_lock_and_says_so(self):
        import fcntl
        with tempfile.TemporaryDirectory() as runtime:
            desktop = hyprland()
            desktop.env['XDG_RUNTIME_DIR'] = runtime
            other = open(Path(runtime) / 'read-page.lock', 'w')
            fcntl.flock(other, fcntl.LOCK_EX)

            class Said(io.StringIO):
                """The other run ends once this one says it waits, so lock() has to wait and then succeed."""
                def write(self, text):
                    if 'waiting for another run to end' in text:
                        other.close()
                    return super().write(text)
            with contextlib.redirect_stderr(Said()) as said:
                desktop.lock()
            self.assertTrue(other.closed, 'lock() returned without waiting for the other run')
            self.assertIn('waiting for another run to end', said.getvalue())
            with open(Path(runtime) / 'read-page.lock') as third, self.assertRaises(BlockingIOError):
                fcntl.flock(third, fcntl.LOCK_SH | fcntl.LOCK_NB)
            desktop.held.close()


class DaemonTest(unittest.TestCase):
    def test_starts_its_own_daemon_in_the_desktops_environment(self):
        desktop = hyprland()

        def serve(argv, **kwargs):
            Path(argv[argv.index('--socket') + 1]).touch()
            return mock.Mock(poll=mock.Mock(return_value=None))
        with mock.patch.object(read_page.subprocess, 'Popen', side_effect=serve) as popen:
            process, socket, folder = read_page.daemon('cua-driver', desktop)
        self.addCleanup(read_page.shutil.rmtree, folder, True)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:4], ['cua-driver', 'serve', '--socket', socket])
        self.assertIn('--no-overlay', argv)
        self.assertEqual(argv[argv.index('--grant') + 1], 'existing-profile')
        self.assertIs(popen.call_args.kwargs['env'], desktop.env)
        self.assertLess(len(socket), 100)

    def test_stops_and_cleans_up_a_daemon_that_never_listens(self):
        process = mock.Mock(poll=mock.Mock(return_value=1))

        def serve(argv, stdout, **kwargs):
            stdout.write('error: permission mode refused')
            return process
        with tempfile.TemporaryDirectory() as parent:
            folder = Path(parent) / 'd'
            folder.mkdir()
            with mock.patch.object(read_page.subprocess, 'Popen', side_effect=serve), \
                    mock.patch.object(read_page.tempfile, 'mkdtemp', return_value=str(folder)):
                with self.assertRaisesRegex(RuntimeError, 'did not start: error: permission mode refused'):
                    read_page.daemon('cua-driver', hyprland())
            self.assertFalse(folder.exists())
        process.terminate.assert_called_once()

    def test_stops_its_daemon_when_stopped_while_it_starts(self):
        process = mock.Mock(poll=mock.Mock(return_value=None))
        with mock.patch.object(read_page.subprocess, 'Popen', return_value=process), \
                mock.patch.object(read_page, 'stop_daemon') as stop, \
                mock.patch.object(read_page.time, 'sleep', side_effect=SystemExit(143)):
            with self.assertRaises(SystemExit):
                read_page.daemon('cua-driver', hyprland())
        stop.assert_called_once()
        self.assertIs(stop.call_args.args[0], process)
        read_page.shutil.rmtree(stop.call_args.args[1], ignore_errors=True)

    def test_kills_a_daemon_that_ignores_terminate(self):
        process = mock.Mock(wait=mock.Mock(side_effect=[read_page.subprocess.TimeoutExpired('serve', 10), 0]))
        with tempfile.TemporaryDirectory() as parent:
            folder = Path(parent) / 'd'
            folder.mkdir()
            read_page.stop_daemon(process, str(folder))
            self.assertFalse(folder.exists())
        process.kill.assert_called_once()


class HyprlandChromeTest(unittest.TestCase):
    def pick(self, lines, accessible=True):
        desktop = mock.Mock(accessible=mock.Mock(return_value=accessible))
        with mock.patch.object(read_page, 'command_line', side_effect=lambda pid, cua: lines[pid]):
            return read_page.chrome(FakeDriver(list(lines)), desktop)

    def test_starts_chrome_when_only_automation_runs_one(self):
        self.assertIsNone(self.pick({31372: AGENT_BROWSER}))
        self.assertIsNone(self.pick({}))

    def test_uses_the_users_chrome_when_it_is_accessible(self):
        self.assertEqual(self.pick({31372: AGENT_BROWSER, 85197: OWN}), 85197)

    def test_skips_helpers_and_exited_children(self):
        """Linux lists each of Chrome's helper processes, and an exited one until it is reaped (seen on Omarchy)."""
        self.assertEqual(self.pick({85197: OWN, 85210: f'{OWN} --type=renderer', 85299: '[chrome] <defunct>'}), 85197)

    def test_names_only_the_candidates_when_it_cannot_tell(self):
        with self.assertRaisesRegex(RuntimeError, "can't tell") as stop:
            self.pick({1: OWN, 2: OWN, 3: f'{OWN} --type=renderer'})
        self.assertNotIn('pid 3', str(stop.exception))

    def test_stops_at_a_users_chrome_hidden_from_accessibility(self):
        for lines in ({85197: OWN}, {85197: ''}):  # '': the only Chrome, whose command line can't be read
            with self.subTest(lines=lines), self.assertRaisesRegex(RuntimeError, 'ACCESSIBILITY_ENABLED=1'):
                self.pick(lines, accessible=False)


@on_linux
class HyprlandMainTest(unittest.TestCase):
    def setUp(self):
        for name in ('SIGTERM', 'SIGHUP'):  # main() installs handlers of its own
            if hasattr(signal, name):
                self.addCleanup(signal.signal, getattr(signal, name), signal.getsignal(getattr(signal, name)))
        keyring = mock.patch.object(read_page, 'keyring_refusal', return_value='')
        self.keyring = keyring.start()
        self.addCleanup(keyring.stop)

    def test_does_not_start_chrome_while_the_keyring_is_locked(self):
        """On 18 November 2026 a Chrome started on the user's profile with the keyring locked lost their sign-ins."""
        self.keyring.return_value = 'chrome_keyring: the default keyring is locked. Ask the user to unlock it'
        desktop = mock.Mock()
        with mock.patch.object(read_page.sys, 'argv', ['read_page', 'https://example.com/a']), \
                mock.patch.object(read_page.Hyprland, 'find', return_value=desktop), \
                mock.patch.object(read_page, 'daemon', return_value=(mock.Mock(), '/tmp/s', '/tmp')), \
                mock.patch.object(read_page, 'stop_daemon'), \
                mock.patch.object(read_page, 'chrome', return_value=None), \
                mock.patch.object(read_page.Driver, 'call', return_value={}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as said:
            self.assertEqual(read_page.main(), 3)
        desktop.open.assert_not_called()
        self.keyring.assert_called_once_with(desktop.env)
        self.assertIn('the default keyring is locked. Ask the user to unlock it', said.getvalue())

    def test_uses_a_running_chrome_whatever_the_keyring(self):
        self.keyring.return_value = 'chrome_keyring: the default keyring is locked'
        desktop = mock.Mock(note='')
        desktop.open.return_value = (0x20, 7)
        desktop.wait_for_address.return_value = 'https://example.com/a'
        with mock.patch.object(read_page.sys, 'argv', ['read_page', 'https://example.com/a']), \
                mock.patch.object(read_page.Hyprland, 'find', return_value=desktop), \
                mock.patch.object(read_page, 'daemon', return_value=(mock.Mock(), '/tmp/s', '/tmp')), \
                mock.patch.object(read_page, 'stop_daemon'), \
                mock.patch.object(read_page, 'chrome', return_value=7), \
                mock.patch.object(read_page, 'command_line', return_value=OWN), \
                mock.patch.object(read_page.Driver, 'call', return_value={'tree_markdown': LINUX_TREE}), \
                mock.patch.object(read_page.Driver, 'windows', return_value={0x20: 'A'}), \
                mock.patch.object(read_page.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(read_page.main(), 0)
        self.keyring.assert_not_called()

    def test_opens_each_page_before_closing_the_last_and_stops_its_daemon(self):
        """Closing a Chrome's only window quits it, so the next window opens first."""
        desktop = mock.Mock(note=' (one of 2 windows with this title)')
        desktop.open.side_effect = [(0x20, 7), (0x30, 7)]
        desktop.wait_for_address.side_effect = ["https://example.com/a"] * 2 + ["https://example.com/b"] * 2
        process = mock.Mock(poll=mock.Mock(return_value=None))

        def start(command, found):
            self.assertTrue(found.lock.called, 'the daemon started before the lock was held')
            return process, '/tmp/s', '/tmp'
        with mock.patch.object(read_page.sys, 'argv', ['read_page', 'https://example.com/a', 'https://example.com/b']), \
                mock.patch.object(read_page.Hyprland, 'find', return_value=desktop), \
                mock.patch.object(read_page, 'daemon', side_effect=start) as daemon, \
                mock.patch.object(read_page, 'stop_daemon') as stop, \
                mock.patch.object(read_page, 'chrome', return_value=None), \
                mock.patch.object(read_page, 'command_line', return_value=OWN), \
                mock.patch.object(read_page.Driver, 'call', autospec=True,
                                  return_value={'tree_markdown': LINUX_TREE}) as call, \
                mock.patch.object(read_page.Driver, 'windows', return_value={0x20: 'A', 0x30: 'B'}), \
                mock.patch.object(read_page, 'settled_tree', wraps=read_page.settled_tree) as settled, \
                mock.patch.object(read_page.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(read_page.main(), 0)
        self.assertIn('ADDRESS: https://example.com/b (one of 2 windows with this title)\n', out.getvalue())
        self.assertEqual([c.args[1:3] for c in settled.call_args_list], [(7, 0x20), (7, 0x30)])
        self.assertEqual({c.args[0].socket for c in call.call_args_list}, {'/tmp/s'})
        steps = [call[0] for call in desktop.method_calls if call[0] in ('open', 'close')]
        self.assertEqual(steps, ['open', 'open', 'close', 'close'])
        self.assertEqual([call.args for call in desktop.close.call_args_list], [(7, 0x20), (7, 0x30)])
        daemon.assert_called_once()
        stop.assert_called_once_with(process, '/tmp')
        desktop.lock.assert_called_once_with()

    def test_closes_a_window_that_opened_in_another_chrome(self):
        desktop = mock.Mock()
        desktop.open.side_effect = [(0x20, 7), (0x30, 9)]
        desktop.wait_for_address.return_value = "https://example.com/a"
        with mock.patch.object(read_page.sys, 'argv', ['read_page', 'https://example.com/a', 'https://example.com/b']), \
                mock.patch.object(read_page.Hyprland, 'find', return_value=desktop), \
                mock.patch.object(read_page, 'daemon', return_value=(mock.Mock(), '/tmp/s', '/tmp')), \
                mock.patch.object(read_page, 'stop_daemon'), \
                mock.patch.object(read_page, 'chrome', return_value=None), \
                mock.patch.object(read_page, 'command_line', return_value=OWN), \
                mock.patch.object(read_page.Driver, 'call', return_value={'tree_markdown': LINUX_TREE}), \
                mock.patch.object(read_page.Driver, 'windows', return_value={0x20: 'A'}), \
                mock.patch.object(read_page.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(read_page.main(), 3)
        self.assertEqual([call.args for call in desktop.close.call_args_list], [(9, 0x30), (7, 0x20)])

    def test_skips_a_page_sent_to_another_site_while_it_settled(self):
        desktop = mock.Mock(note='')
        desktop.open.return_value = (0x20, 7)
        desktop.wait_for_address.side_effect = ['https://example.com/a', 'https://login.other.example/']
        reads = []
        with mock.patch.object(read_page.sys, 'argv', ['read_page', 'https://example.com/a']), \
                mock.patch.object(read_page.Hyprland, 'find', return_value=desktop), \
                mock.patch.object(read_page, 'daemon', return_value=(mock.Mock(), '/tmp/s', '/tmp')), \
                mock.patch.object(read_page, 'stop_daemon'), \
                mock.patch.object(read_page, 'chrome', return_value=None), \
                mock.patch.object(read_page, 'command_line', return_value=OWN), \
                mock.patch.object(read_page, 'settled_tree', side_effect=lambda *a: reads.append(
                    desktop.wait_for_address.call_count) or {'tree_markdown': LINUX_TREE}), \
                mock.patch.object(read_page.Driver, 'call', return_value={}), \
                mock.patch.object(read_page.Driver, 'windows', return_value={0x20: 'A'}), \
                mock.patch.object(read_page.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as said:
            self.assertEqual(read_page.main(), 5)
        self.assertEqual(reads, [1])  # the tree is read between the two address reads
        self.assertIn('Skipped https://example.com/a: the window shows https://login.other.example/', said.getvalue())
        self.assertEqual(out.getvalue(), '')

    def test_says_why_a_page_had_no_address(self):
        desktop = mock.Mock(why='its page has no address yet')
        desktop.open.return_value = (0x20, 7)
        desktop.wait_for_address.return_value = ''
        with mock.patch.object(read_page.sys, 'argv', ['read_page', 'https://example.com/a']), \
                mock.patch.object(read_page.Hyprland, 'find', return_value=desktop), \
                mock.patch.object(read_page, 'daemon', return_value=(mock.Mock(), '/tmp/s', '/tmp')), \
                mock.patch.object(read_page, 'stop_daemon'), \
                mock.patch.object(read_page, 'chrome', return_value=None), \
                mock.patch.object(read_page, 'command_line', return_value=OWN), \
                mock.patch.object(read_page.Driver, 'call', return_value={'tree_markdown': LINUX_TREE}), \
                mock.patch.object(read_page.Driver, 'windows', return_value={0x20: 'A'}), \
                mock.patch.object(read_page.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as said:
            self.assertEqual(read_page.main(), 5)
        self.assertIn('shows no address (its page has no address yet)', said.getvalue())
        desktop.wait_for_address.assert_called_once()  # a page with no address isn't settled and waited for again

    def test_stops_when_the_chrome_it_started_is_automations(self):
        desktop = mock.Mock()
        desktop.open.return_value = (0x20, 7)
        with mock.patch.object(read_page.sys, 'argv', ['read_page', 'https://example.com/a']), \
                mock.patch.object(read_page.Hyprland, 'find', return_value=desktop), \
                mock.patch.object(read_page, 'daemon', return_value=(mock.Mock(), '/tmp/s', '/tmp')), \
                mock.patch.object(read_page, 'stop_daemon'), \
                mock.patch.object(read_page, 'chrome', return_value=None), \
                mock.patch.object(read_page, 'command_line', return_value=AGENT_BROWSER), \
                mock.patch.object(read_page.Driver, 'call', return_value={}), \
                mock.patch.object(read_page.time, 'sleep'), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(read_page.main(), 3)
        self.assertIn("not in the user's own Chrome", err.getvalue())
        desktop.close.assert_called_once_with(7, 0x20)
        desktop.wait_for_address.assert_not_called()

    def test_keeps_cua_driver_exe_on_the_windows_desktop(self):
        with mock.patch.object(read_page.sys, 'argv', ['read_page', '--cua', 'cua-driver.exe', 'https://example.com/a']), \
                mock.patch.object(read_page.Hyprland, 'find') as find, \
                mock.patch.object(read_page, 'chrome', side_effect=RuntimeError('stop here')), \
                mock.patch.object(read_page.Driver, 'call', return_value={}), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(read_page.main(), 3)
        find.assert_not_called()

    @unittest.skipUnless(hasattr(signal, 'SIGHUP'), 'Windows has no SIGHUP')
    def test_keeps_a_hangup_the_caller_ignores(self):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)  # as nohup does; setUp restores the old handler
        with mock.patch.object(read_page.sys, 'argv', ['read_page', 'https://example.com/a']), \
                mock.patch.object(read_page.Hyprland, 'find', return_value=None), \
                mock.patch.object(read_page, 'chrome', side_effect=RuntimeError('stop here')), \
                mock.patch.object(read_page.Driver, 'call', return_value={}), \
                contextlib.redirect_stderr(io.StringIO()):
            read_page.main()
        self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
        self.assertIsNot(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)

    @unittest.skipUnless(hasattr(signal, 'SIGHUP'), 'Windows has no SIGHUP')
    def test_cleans_up_when_a_job_is_stopped(self):
        """A stopped scheduled job ends through `finally`, which closes its window and daemon."""
        desktop = mock.Mock()
        desktop.open.return_value = (0x20, 7)
        process = mock.Mock(poll=mock.Mock(return_value=None))
        stopped = []

        def tree(*args, **kwargs):
            if not stopped:
                stopped.append(True)
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return {}
        with mock.patch.object(read_page.sys, 'argv', ['read_page', 'https://example.com/a']), \
                mock.patch.object(read_page.Hyprland, 'find', return_value=desktop), \
                mock.patch.object(read_page, 'daemon', return_value=(process, '/tmp/s', '/tmp')), \
                mock.patch.object(read_page, 'stop_daemon') as stop, \
                mock.patch.object(read_page, 'chrome', return_value=None), \
                mock.patch.object(read_page, 'command_line', return_value=OWN), \
                mock.patch.object(read_page.Driver, 'call', return_value={}), \
                mock.patch.object(read_page.Driver, 'tree', side_effect=tree), \
                mock.patch.object(read_page.time, 'sleep'):
            with self.assertRaises(SystemExit) as stop_code:
                read_page.main()
        self.assertEqual(stop_code.exception.code, 128 + signal.SIGTERM)
        with self.assertRaises(SystemExit) as hangup:
            signal.getsignal(signal.SIGHUP)(signal.SIGHUP, None)
        self.assertEqual(hangup.exception.code, 128 + signal.SIGHUP)
        desktop.close.assert_called_once_with(7, 0x20)
        stop.assert_called_once_with(process, '/tmp')


FAKE_JEEPNEY = '''"""Answers atspi_do.py's D-Bus calls from the JSON tree in $FAKE_ATSPI_TREE."""
import json, os
from collections import namedtuple

DBusAddress = namedtuple('DBusAddress', 'path name interface')


def new_method_call(address, method, signature=None, body=()):
    return address, method, tuple(body)


class Reply:
    def __init__(self, *body):
        self.body = body


class Connection:
    def __init__(self):
        with open(os.environ['FAKE_ATSPI_TREE']) as f:
            self.tree = json.load(f)

    def send_and_get_reply(self, message):
        (path, name, interface), method, body = message
        if method == 'GetAddress':
            return Reply('unix:path=/fake')
        if method == 'GetConnectionUnixProcessID':
            return Reply(7)
        node = self.tree[path]
        if method == 'GetChildren':
            return Reply([(':1.5', child) for child in node.get('children', [])])
        if method == 'GetRoleName':
            return Reply(node['role'])
        if method == 'Get' and body == ('org.a11y.atspi.Accessible', 'Name'):
            return Reply(('s', node.get('name', '')))
        if method == 'GetAttributeValue':
            return Reply(node['uri'])
        raise RuntimeError(f'no answer for {method} on {path}')
'''


class AtspiDoTest(unittest.TestCase):
    """Runs atspi_do.py itself, on a stand-in for jeepney that serves one Chrome's frames, and address() on what it
    prints."""

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.mkdtemp()
        (Path(cls.folder) / 'jeepney' / 'io').mkdir(parents=True)
        (Path(cls.folder) / 'jeepney' / '__init__.py').write_text(FAKE_JEEPNEY)
        (Path(cls.folder) / 'jeepney' / 'io' / '__init__.py').write_text('')
        (Path(cls.folder) / 'jeepney' / 'io' / 'blocking.py').write_text(
            'from jeepney import Connection\n\n\ndef open_dbus_connection(bus="SESSION"):\n    return Connection()\n')

    @classmethod
    def tearDownClass(cls):
        read_page.shutil.rmtree(cls.folder, ignore_errors=True)

    def address(self, frames, title):
        """frames: (frame name, page address, or None for a frame whose page document hasn't appeared)."""
        nodes = {'/org/a11y/atspi/accessible/root': {'role': 'desktop frame', 'children': ['/app']},
                 '/app': {'role': 'application', 'name': 'Google Chrome', 'children': []}}
        for i, (name, uri) in enumerate(frames):
            nodes['/app']['children'].append(f'/f{i}')
            nodes[f'/f{i}'] = {'role': 'frame', 'name': name, 'children': [f'/f{i}/bar'] + ([f'/f{i}/doc'] if uri else [])}
            nodes[f'/f{i}/bar'] = {'role': 'tool bar', 'name': ''}
            if uri:
                nodes[f'/f{i}/doc'] = {'role': 'document web', 'name': name, 'uri': uri}
        tree = Path(self.folder) / 'tree.json'
        tree.write_text(json.dumps(nodes))
        script = Path(read_page.__file__).resolve().parent / 'atspi_do.py'
        env = {**read_page.os.environ, 'PYTHONPATH': self.folder, 'FAKE_ATSPI_TREE': str(tree)}
        run = mock.Mock(side_effect=lambda pid, *args: read_page.subprocess.run(
            [sys.executable, str(script), '--pid', str(pid), *args], capture_output=True, text=True, env=env, timeout=60))
        desktop = hyprland()
        with mock.patch.object(read_page.Hyprland, 'atspi', side_effect=lambda pid, *args: run(pid, *args)):
            shown = desktop.address(7, title)
        return shown, desktop.why, desktop.note, run.call_count

    def test_reads_each_case_as_the_real_script_prints_it(self):
        ft, world, google = 'https://www.ft.com/', 'https://www.ft.com/world', 'https://accounts.google.com/'
        for frames, title, expected in (
                ([('Home - FT', ft)], 'Home - FT', (ft, '', '', 1)),
                ([('Home - FT', None)], 'Home - FT', ('', 'its page has no address yet', '', 1)),  # still loading
                ([('Home - FT', ft), ('Home - FT', world)], 'Home - FT',
                 (ft, '', ' (one of 2 windows with this title)', 1)),  # the user's own copy of the page
                ([('Home - FT', ft), ('Home - FT', google)], 'Home - FT',
                 ('', "2 windows titled 'Home - FT' show different sites", '', 1)),
                ([('Other', ft)], 'Home - FT', ('', "no window titled 'Home - FT' in the accessibility tree", '', 1)),
                ([('Home - FT', ft), ('World - FT', world)], '', ('', 'the window has no title', '', 0))):
            with self.subTest(frames=frames, title=title):
                self.assertEqual(self.address(frames, title), expected)

    def test_matches_an_empty_frame_name_exactly(self):
        shown, why, _, _ = self.address([('Home - FT', 'https://www.ft.com/')], 'x')
        self.assertEqual(shown, '')
        tree = Path(self.folder) / 'tree.json'
        script = Path(read_page.__file__).resolve().parent / 'atspi_do.py'
        result = read_page.subprocess.run(
            [sys.executable, str(script), '--pid', '7', '--frame', '', '--role', '^document web$', '--name', '', '--uri'],
            capture_output=True, text=True, timeout=60,
            env={**read_page.os.environ, 'PYTHONPATH': self.folder, 'FAKE_ATSPI_TREE': str(tree)})
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertIn("expected one frame named '', found 0", result.stderr)


class DriverTest(unittest.TestCase):
    def test_calls_its_own_daemon(self):
        with mock.patch.object(read_page.subprocess, 'run', return_value=mock.Mock(stdout='{}', stderr='')) as run:
            read_page.Driver('cua-driver', 's', '/tmp/x/s').call('list_apps')
        self.assertEqual(run.call_args.args[0], ['cua-driver', 'call', '--socket', '/tmp/x/s', 'list_apps'])


class SaveTest(unittest.TestCase):
    def test_saves_each_page_on_every_platform(self):
        for platform in ('darwin', 'linux', 'win32'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as folder, \
                    mock.patch.object(read_page.sys, 'platform', platform), \
                    mock.patch.object(read_page.sys, 'argv', ['read_page', '--out', folder, 'https://example.com/a']), \
                    mock.patch.object(read_page.Hyprland, 'find', return_value=None), \
                    mock.patch.object(read_page, 'Driver') as driver, \
                    mock.patch.object(read_page, 'chrome', return_value=1), \
                    mock.patch.object(read_page, 'open_window', return_value=2), \
                    mock.patch.object(read_page, 'close'), \
                    mock.patch.object(read_page, 'address_bar', return_value=(None, 'https://example.com/a')), \
                    mock.patch.object(read_page.time, 'sleep'), \
                    contextlib.redirect_stdout(io.TextIOWrapper(io.BytesIO(), encoding='utf-8')):
                driver.return_value.uia = False
                driver.return_value.tree.return_value = {'tree_markdown': WINDOWS_TREE}
                driver.return_value.windows.return_value = {2: 'A page'}
                self.assertEqual(read_page.main(), 0)
                saved = list(Path(folder).iterdir())
                self.assertEqual(len(saved), 1)
                self.assertIn('Compare the plans.', saved[0].read_text(encoding='utf-8'))


class PollTest(unittest.TestCase):
    """main() reads each page again until its text stops growing, or until --wait seconds pass."""
    # Chrome on Windows shows a page's tree only after a tool first asks for it: on 18 November 2026 a loaded page
    # in a Chrome just started read like this at 1, 7 and 20 seconds, and in full a second later.
    EMPTY = WINDOWS_TREE.split('              - [24] Document')[0]
    HALF = WINDOWS_TREE.split('                - Group "Primary navigation"')[0]
    OTHER = WINDOWS_TREE.replace('Compare the plans.', 'Another page.')

    def read(self, trees, *argv, urls=('https://example.com/a',), uia=True):
        """Run main() on reads that follow `trees`, the last repeating, and return (exit status, stdout, stderr,
        seconds slept, reads)."""
        reads = iter(trees)
        with FakeClock() as clock, \
                mock.patch.object(read_page.sys, 'argv', ['read_page', *argv, *urls]), \
                mock.patch.object(read_page.Hyprland, 'find', return_value=None), \
                mock.patch.object(read_page, 'Driver') as driver, \
                mock.patch.object(read_page, 'chrome', return_value=1), \
                mock.patch.object(read_page, 'open_window', return_value=2), \
                mock.patch.object(read_page, 'load'), \
                mock.patch.object(read_page, 'close'), \
                mock.patch.object(read_page, 'address_bar', return_value=(None, 'https://example.com/a')), \
                contextlib.redirect_stdout(io.TextIOWrapper(io.BytesIO(), encoding='utf-8')) as out, \
                contextlib.redirect_stderr(io.StringIO()) as said:  # main() reconfigures stdout on Windows
            driver.return_value.uia = uia
            driver.return_value.tree.side_effect = lambda *a, **k: {'tree_markdown': next(reads, trees[-1])}
            driver.return_value.windows.return_value = {2: 'Plans'}
            status = read_page.main()
            out.flush()
            return (status, out.buffer.getvalue().decode('utf-8'), said.getvalue(), clock.now,
                    driver.return_value.tree.call_count)

    def test_returns_soon_when_the_text_shows_at_once(self):
        status, out, _, slept, _ = self.read([WINDOWS_TREE])
        self.assertEqual(status, 0)
        self.assertIn('Compare the plans.', out)
        self.assertLessEqual(slept, 2)

    def test_reads_again_until_the_text_arrives(self):
        status, out, said, slept, _ = self.read([self.EMPTY, self.EMPTY, WINDOWS_TREE])
        self.assertEqual((status, said), (0, ''))
        self.assertIn('Compare the plans.', out)
        self.assertLessEqual(slept, 6)

    def test_fails_at_the_limit_when_no_text_arrives(self):
        """On 18 November 2026 a Windows read printed no page text at the default wait and still exited 0."""
        status, _, said, slept, reads = self.read([self.EMPTY], '--wait', '30')
        self.assertEqual(status, 6)
        self.assertGreater(reads, 1)
        self.assertTrue(30 <= slept <= 32, slept)
        self.assertIn('https://example.com/a: no page text within --wait 30 seconds', said)
        self.assertIn('--wait 60', said)

    def test_waits_for_text_that_is_still_growing(self):
        status, out, _, _, _ = self.read([self.HALF, WINDOWS_TREE])
        self.assertEqual(status, 0)
        self.assertIn('[link] Pricing', out)
        # At the limit it keeps the last read, even if the page is still adding text.
        status, out, _, slept, _ = self.read([self.HALF, WINDOWS_TREE], '--wait', '2')
        self.assertEqual((status, slept), (0, 2))
        self.assertIn('[link] Pricing', out)

    def test_does_not_take_the_last_page_for_the_next(self):
        """On macOS the window loads each page in turn, and shows the last one until the next replaces it."""
        trees = [WINDOWS_TREE] * 4 + [self.OTHER]
        status, out, _, _, _ = self.read(trees, urls=('https://example.com/a', 'https://example.com/b'), uia=False)
        self.assertEqual(status, 0)
        first, second = out.split('URL: https://example.com/b')
        self.assertIn('Compare the plans.', first)
        self.assertIn('Another page.', second)
        self.assertNotIn('Compare the plans.', second)

    @on_linux
    def test_hyprland_never_closes_window_showing_different_site(self):
        """F6: a user Chrome window captured during open() is moved to the active workspace, never closed."""
        desktop = mock.MagicMock()
        desktop.env = RUNTIME
        desktop.open.return_value = (0x20, 7)
        desktop.wait_for_address.return_value = 'https://mail.example.org/inbox'
        desktop.why = ''
        desktop.note = ''
        with FakeClock(), \
                mock.patch.object(read_page.sys, 'argv', ['read_page', 'https://ft.com/content/1']), \
                mock.patch.object(read_page.Hyprland, 'find', return_value=desktop), \
                mock.patch.object(read_page, 'daemon', return_value=(mock.Mock(poll=lambda: None), '/tmp/s', '/tmp/d')), \
                mock.patch.object(read_page, 'stop_daemon'), \
                mock.patch.object(read_page, 'Driver') as driver, \
                mock.patch.object(read_page, 'chrome', return_value=7), \
                mock.patch.object(read_page, 'command_line', return_value='/usr/bin/google-chrome'), \
                contextlib.redirect_stderr(io.StringIO()) as said:
            driver.return_value.uia = False
            driver.return_value.tree.return_value = {'tree_markdown': LINUX_TREE}
            driver.return_value.windows.return_value = {0x20: 'Inbox - Google Chrome'}
            status = read_page.main()
        self.assertEqual(status, 3)
        desktop.return_to_active.assert_called_once_with(7, 0x20)
        desktop.close.assert_not_called()
        self.assertIn('0x20', said.getvalue())
        self.assertIn('moved it to the active workspace instead of closing it', said.getvalue())

    def test_hyprland_return_to_active_moves_window_silently(self):
        run = FakeHyprctl([[hypr_window('0x20', 7)]], answers={'-j': json.dumps({'id': 3})})
        with mock.patch.object(read_page.subprocess, 'run', side_effect=run):
            hyprland().return_to_active(7, 0x20)
        dispatches = [argv for argv in run.calls if argv[1] == 'dispatch']
        self.assertEqual(dispatches, [['hyprctl', 'dispatch', 'movetoworkspacesilent', '3,address:0x20']])


if __name__ == '__main__':
    unittest.main()
