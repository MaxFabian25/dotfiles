#!/usr/bin/env python3
"""Read web pages in the user's own Chrome through cua-driver's accessibility tree, never DevTools.

Opens one Chrome window of its own in the given profile, loads each URL through that window's address
bar, and prints each page's text, or saves it under --out. It reads only the page, never the tab strip or
the rest of the browser, prints each table row on one line, and with --expand first opens collapsed FAQ
answers. It skips a page whose address bar shows another site, such as a sign-in redirect. It stops at a
bot check, closes only its own window, and ends its cua-driver session. Requires the user's own Chrome
running, except on a Hyprland desktop as below, and cua-driver on PATH. A Chrome that automation started, such
as agent-browser's, is never used. On WSL, pass --cua cua-driver.exe to drive Chrome on the Windows desktop.
It reads each page again every 2 seconds until its text stops growing or --wait seconds pass. It exits 3 when it
stops, 4 at a bot check, 5 when it skipped a page, and 6 when a page showed no text within --wait.

On a Hyprland desktop, such as the Omarchy laptop, it runs unattended: it starts a cua-driver daemon of its own
with the desktop's environment, opens each page in a window on a hidden workspace without taking focus, starts the
user's Chrome there when it isn't running and the keyring holding its cookie key is unlocked (see chrome_keyring.py),
which then quits when its last window closes, and reads each page's
address through AT-SPI with uv and jeepney. A Chrome the user started shows its pages there only when, as it
started, ACCESSIBILITY_ENABLED=1 was in its environment or the desktop's AT-SPI switch was on, and the script stops
at one that is not. Runs there go one at a time.
"""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

BOT_CHECK = re.compile(r'Just a moment|Security Verification|DataDome|captcha|Are you a robot', re.IGNORECASE)
NYT_LIMIT = 5  # The NYT shows a bot check after a longer run of pages.
# A Chrome child that has exited but not yet been reaped shows `ps` the command line "[chrome] <defunct>".
NOT_OWN = re.compile(r'--(?:remote-debugging-(?:port|pipe)|headless|enable-automation|user-data-dir|type)\b|<defunct>$')
ROW = re.compile(r'^( *)- \[(\d+)\] AX(\w+)(?: "((?:[^"\\]|\\.)*)")?(?: = "((?:[^"\\]|\\.)*)")?',
                 re.MULTILINE | re.DOTALL)
# Windows' UI Automation tree names roles without AX, leaves some text rows unnumbered, and puts values in
# brackets, where a link's value is its URL. It hides table rows, so a table's cells print one per line.
UIA_ROW = re.compile(r'^( *)- (?:\[(\d+)\] )?(\w+)(?: "((?:[^"\\]|\\.)*)")?([^\n]*)', re.MULTILINE | re.DOTALL)
UIA_VALUE = re.compile(r'value="((?:[^"\\]|\\.)*)"')
UIA_ROLES = {'Hyperlink': 'Link', 'Edit': 'TextField', 'Table': 'Group'}
TAB_STRIP = re.compile(r'(?:Expand Tabs|Tab groups|Search tabs|New Tab)\b')  # Chrome's strip follows the page
CELLS = ('Cell', 'ColumnHeader', 'RowHeader')
EXPAND_LIMIT = 40
# Linux's AT-SPI tree names roles in lowercase words and puts a text row's text after '='. A heading's, link's,
# button's or image's name repeats the text inside it, and other containers, such as sections, carry U+FFFC
# placeholders or repeat their contents, so only the named roles and the text rows keep text.
ATSPI_ROW = re.compile(r'^( *)- (?:\[(\d+)\] )?([a-z]+(?: [a-z]+)*)(?: "((?:[^"\\]|\\.)*)")?(?: = "((?:[^"\\]|\\.)*)")?',
                       re.MULTILINE | re.DOTALL)
ATSPI_ROLES = {'document web': 'WebArea', 'heading': 'Heading', 'link': 'Link', 'static': 'StaticText',
               'image': 'Image', 'button': 'Button', 'push button': 'Button', 'table': 'Table', 'table row': 'Row',
               'table cell': 'Cell', 'table column header': 'ColumnHeader', 'column header': 'ColumnHeader',
               'table row header': 'RowHeader', 'row header': 'RowHeader'}
ATSPI_NAMED = ('Heading', 'Link', 'Button', 'Image')
PLACEHOLDER = chr(0xFFFC)  # the object replacement character
A11Y_FLAG = '--force-renderer-accessibility'
HIDDEN = 'special:read-page'  # a Hyprland workspace no key shows


class Driver:
    def __init__(self, command, session, socket=None):
        self.command, self.session, self.socket = command, session, socket
        self.uia = sys.platform == 'win32' or command.endswith('.exe')  # cua-driver.exe on WSL drives Windows

    def call(self, tool, **args):
        socket = ['--socket', self.socket] if self.socket else []
        result = subprocess.run([self.command, 'call', *socket, tool], input=json.dumps({'session': self.session, **args}),
                                capture_output=True, text=True, encoding='utf-8', timeout=180)
        try:
            data = json.loads(result.stdout, strict=False)
        except ValueError:
            raise RuntimeError(f'{tool}: {(result.stdout + result.stderr)[-400:]}') from None
        if isinstance(data, dict) and (data.get('refusal') or data.get('error')):
            raise RuntimeError(f'{tool}: {data.get("refusal") or data.get("error")}')
        return data

    def windows(self, pid):
        """Chrome's windows without its popups. Windows reports a minimized window 20 high; it still reads."""
        return {w['window_id']: w.get('title', '') for w in self.call('list_windows', pid=pid).get('windows', [])
                if w.get('bounds', {}).get('height', 0) > 200 or w.get('minimized')}

    def tree(self, pid, window, **args):
        return self.call('get_window_state', pid=pid, window_id=window, include_screenshot=False,
                         **{'max_elements': 6000, 'timeout_ms': 15000, **args})


def command_line(pid, cua):
    """A process's command line, or '' when it can't be read. cua-driver.exe on WSL reports Windows processes."""
    if sys.platform == 'win32' or cua.endswith('.exe'):
        query = f'(Get-CimInstance Win32_Process -Filter "ProcessId={int(pid)}").CommandLine'
        argv = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', query]
    else:
        argv = ['ps', '-o', 'command=', '-p', str(int(pid))]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def chrome(driver, desktop=None):
    """The user's own Chrome. Automation, such as agent-browser, starts Chrome with DevTools, headless or with a
    profile folder of its own, and Chrome's helper processes carry --type; skip all of those. On a Hyprland desktop,
    None when it isn't running, so that the first window starts it."""
    apps = [app for app in driver.call('list_apps').get('apps', [])
            if app.get('pid') and app.get('running', True)
            and (app.get('bundle_id') == 'com.google.Chrome' or app.get('name') in ('Google Chrome', 'chrome', 'chrome.exe'))]
    lines = {app['pid']: command_line(app['pid'], driver.command) for app in apps}
    own = [app for app in apps if lines[app['pid']] and not NOT_OWN.search(lines[app['pid']])]
    if not own and len(apps) == 1 and not lines[apps[0]['pid']]:
        own = apps  # the only Chrome, and its command line can't be read
    if desktop is not None and not own:
        return None
    if not apps:
        raise RuntimeError('Google Chrome is not running; start it, then run this again')
    if len(own) != 1:
        found = '; '.join(f"pid {app['pid']}: {lines[app['pid']][:80] or 'command line unreadable'}" for app in apps
                          if '--type=' not in lines[app['pid']])  # Chrome's helpers, one per tab and service
        raise RuntimeError(f"can't tell which Chrome is the user's own ({found})")
    if desktop is not None and not desktop.accessible(own[0]['pid']):
        raise RuntimeError("the user's Chrome started without accessibility, so its pages can't be read; ask the user "
                           'to quit it, or to start it with ACCESSIBILITY_ENABLED=1 and ' + A11Y_FLAG)
    return own[0]['pid']


def keyring_refusal(env):
    """Why this run must not start the user's Chrome, or '' when it may: Chrome started while the keyring is locked
    deletes the cookies it can't decrypt."""
    script = Path(__file__).resolve().parent / 'chrome_keyring.py'
    try:
        check = subprocess.run([sys.executable, '-I', str(script), 'check'], capture_output=True, text=True, env=env,
                               timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        return f'{script.name} check could not run ({error}), so Chrome was not started'
    return '' if check.returncode == 0 else check.stderr.strip() or f'{script.name} check exited {check.returncode}'


class Hyprland:
    """The Hyprland desktop, as the Omarchy laptop runs it, reached from a shell without the desktop's environment,
    such as a scheduled job's, through `hyprctl`, which finds the session without it."""

    def __init__(self, instance):
        runtime = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
        self.env = {**os.environ, 'XDG_RUNTIME_DIR': runtime, 'HYPRLAND_INSTANCE_SIGNATURE': instance['instance'],
                    'WAYLAND_DISPLAY': instance['wl_socket'], 'CUA_DRIVER_RS_ENABLE_WAYLAND': '1'}
        self.env.setdefault('DBUS_SESSION_BUS_ADDRESS', f'unix:path={runtime}/bus')
        self.env.setdefault('DISPLAY', ':0')
        self.why = self.note = ''

    @classmethod
    def find(cls):
        """The running Hyprland session, or None off Linux, without Hyprland, or with cua-driver.exe on WSL."""
        if not sys.platform.startswith('linux') or not shutil.which('hyprctl'):
            return None
        result = subprocess.run(['hyprctl', 'instances', '-j'], capture_output=True, text=True, timeout=30)
        try:
            if result.returncode:
                raise ValueError
            found = json.loads(result.stdout or '[]')
        except ValueError:
            raise RuntimeError(f'hyprctl instances: {(result.stdout + result.stderr).strip()[-300:]}') from None
        if len(found) > 1:
            found = [i for i in found if i['instance'] == os.environ.get('HYPRLAND_INSTANCE_SIGNATURE')]
            if len(found) != 1:
                raise RuntimeError('several Hyprland sessions run; set HYPRLAND_INSTANCE_SIGNATURE to the desktop one')
        return cls(found[0]) if found else None

    def lock(self):
        """Wait for any other run to end, because two runs share the one window rule and each takes the first new
        Chrome window it sees. The lock lasts until this process exits."""
        import fcntl  # Linux only
        self.held = open(os.path.join(self.env['XDG_RUNTIME_DIR'], 'read-page.lock'), 'w')
        try:
            fcntl.flock(self.held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('read_page: waiting for another run to end', file=sys.stderr, flush=True)
            fcntl.flock(self.held, fcntl.LOCK_EX)

    def hyprctl(self, *args):
        result = subprocess.run(['hyprctl', *args], capture_output=True, text=True, env=self.env, timeout=30)
        if result.returncode or result.stdout.startswith('error'):
            raise RuntimeError(f'hyprctl {args[0]}: {(result.stdout + result.stderr).strip()[-300:]}')
        return result.stdout

    def windows(self):
        output = self.hyprctl('clients', '-j')
        try:
            return {int(c['address'], 16): c for c in json.loads(output)}
        except (ValueError, TypeError, KeyError):
            raise RuntimeError(f'hyprctl clients: {output.strip()[-300:]}') from None

    def open(self, profile, url, wait=45):
        """Open url in a new Chrome window on the hidden workspace and return (window, Chrome's pid). A window rule
        for Chrome's class, on only until the window appears and at most a minute, keeps it off the user's screen
        and focus and lets it render there; Chrome on a hidden workspace without it shows the page no tree. The
        window keeps rendering through a property set on it alone."""
        chrome = shutil.which('google-chrome-stable') or shutil.which('google-chrome')
        if not chrome:
            raise RuntimeError('google-chrome-stable is not installed')
        command = shlex.join(['env', 'ACCESSIBILITY_ENABLED=1', chrome, f'--profile-directory={profile}', A11Y_FLAG,
                              '--new-window', url])
        if ']==]' in command:
            raise RuntimeError(f'cannot pass this address to Hyprland: {url}')
        before = set(self.windows())
        # One rule, by name, serves every run; the generation stops an earlier run's timer turning off a later one.
        generation = secrets.token_hex(4)
        off = f'if READ_PAGE_GENERATION == "{generation}" then READ_PAGE_RULE:set_enabled(false) end'
        self.hyprctl('eval', f'''
            READ_PAGE_GENERATION = "{generation}"
            READ_PAGE_RULE = hl.window_rule({{ name = "read-page", match = {{ class = "^google-chrome$" }},
                workspace = "{HIDDEN} silent", no_initial_focus = true, render_unfocused = true }})
            hl.timer(function() {off} end, {{ type = "oneshot", timeout = 60000 }})
            hl.exec_cmd([==[{command}]==])''')
        try:
            for _ in range(wait * 2):
                new = {address: c for address, c in self.windows().items()
                       if address not in before and c.get('class') == 'google-chrome'}
                if len(new) == 1:
                    window, client = new.popitem()
                    # Without this, a run's later windows stopped loading once the rule went off and read 0
                    # characters or a header only, in 4 of 4 runs on the Omarchy laptop on 14 November 2026; with
                    # it they read in full in 4 of 4.
                    address = f'address:{client["address"]}'
                    try:
                        self.hyprctl('dispatch', f'hl.dsp.window.set_prop({{ window = "{address}", '
                                                 'prop = "render_unfocused", value = "1" })')
                    except RuntimeError as error:
                        try:
                            self.hyprctl('dispatch', f'hl.dsp.window.close({{ window = "{address}" }})')
                        except RuntimeError as closing:
                            raise RuntimeError(f'{error}; closing window {address} also failed: {closing}; ask the '
                                               'user to close it') from None
                        raise
                    return window, client['pid']
                if new:
                    # A Chrome restoring its last session opens several, and its own can't be told apart.
                    raise RuntimeError(f'{len(new)} Chrome windows opened at once and wait on the hidden workspace '
                                       f'{HIDDEN}; tell the user, who can show it with Hyprland\'s toggle_special')
                time.sleep(0.5)
        finally:
            self.hyprctl('eval', off)
        raise RuntimeError('no new Chrome window appeared')

    def atspi(self, pid, *args):
        """Run atspi_do.py on Chrome's AT-SPI tree."""
        script = Path(__file__).resolve().parent / 'atspi_do.py'
        try:
            return subprocess.run(['uv', 'run', '-q', '--with', 'jeepney', 'python', '-I', str(script), '--pid', str(pid),
                                   *args], capture_output=True, text=True, env=self.env, timeout=120)
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f'atspi_do.py could not run: {error}') from None

    @staticmethod
    def failed(result, expected):
        """Raise with the tool's own error, unless it failed in the expected way."""
        if result.returncode and expected not in result.stderr:
            raise RuntimeError(f'atspi_do.py: {(result.stdout + result.stderr).strip()[-300:]}')

    def accessible(self, pid):
        """Whether this Chrome shows its windows to accessibility tools. Chrome joins only when, as it starts,
        ACCESSIBILITY_ENABLED=1 is in its environment or the desktop's AT-SPI IsEnabled is on, and every cua-driver
        exit turns IsEnabled off (tested on the Omarchy laptop on 14 November 2026)."""
        result = self.atspi(pid, '--role', '^application$', '--name', '', '--limit', '1')
        self.failed(result, 'no accessible application')
        return result.returncode == 0

    def address(self, pid, title):
        """The address of the page in the window with this title, from its AT-SPI document, because the address bar
        there has no value. Windows with the same title, such as the user's own copy of a front page, give one
        address when all show one site, and self.note says so. Otherwise '', with the reason in self.why."""
        self.note = ''
        if not title:
            self.why = 'the window has no title'
            return ''
        result = self.atspi(pid, '--frame', title, '--role', '^document web$', '--name', '', '--skip-documents', '--uri')
        self.failed(result, 'expected one frame')
        found = [line[4:] for line in result.stdout.splitlines() if line.startswith('uri=')]
        if found and all(same_site(found[0], other) for other in found):
            self.note = f' (one of {len(found)} windows with this title)' if len(found) > 1 else ''
            return found[0]
        self.why = (f'{len(found)} windows titled {title!r} show different sites' if len(found) > 1
                    else f'no window titled {title!r} in the accessibility tree' if result.returncode
                    else 'its page has no address yet')
        return ''

    def wait_for_address(self, driver, pid, window, deadline):
        """The page's address, read again every 2 seconds while the page has none until the deadline, because a page
        in a Chrome that the script has just started takes a while: The Economist's had none at 7 seconds and had
        one at 10 on the Omarchy laptop on 14 November 2026."""
        while True:
            shown = self.address(pid, driver.windows(pid).get(window, ''))
            if shown or time.monotonic() >= deadline:
                return shown
            time.sleep(2)

    def close(self, pid, window):
        client = self.windows().get(window)
        if client is None:
            return
        if client['pid'] != pid:
            raise RuntimeError(f'window {window} is no longer Chrome {pid}\'s; ask the user to close it')
        self.hyprctl('dispatch', f'hl.dsp.window.close({{ window = "address:{client["address"]}" }})')


def daemon(command, desktop):
    """Start a cua-driver daemon of its own with the desktop's environment and return (process, socket, folder).
    The agents' own driver servers may have started without it, and a scheduled job's shell has none."""
    folder = tempfile.mkdtemp(prefix='read-page.')  # short, because a Unix socket path must stay short
    socket = os.path.join(folder, 's')
    log = open(os.path.join(folder, 'serve.log'), 'w')
    process = subprocess.Popen([command, 'serve', '--socket', socket, '--pid-file', os.path.join(folder, 'pid'),
                                '--no-overlay', '--grant', 'existing-profile'], env=desktop.env,
                               stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    log.close()
    try:
        for _ in range(60):
            if os.path.exists(socket) or process.poll() is not None:
                break
            time.sleep(0.25)
    except BaseException:  # such as a stop signal's SystemExit, before main() holds the daemon
        stop_daemon(process, folder)
        raise
    if not os.path.exists(socket):
        with open(os.path.join(folder, 'serve.log'), errors='replace') as log:
            said = log.read().strip()[-300:]
        stop_daemon(process, folder)
        raise RuntimeError(f'cua-driver serve did not start: {said or "it printed nothing"}')
    return process, socket, folder


def stop_daemon(process, folder):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    shutil.rmtree(folder, ignore_errors=True)


def open_window(driver, pid, profile, url):
    before = set(driver.windows(pid))
    if driver.uia:
        # Windows drops the arguments of a launch by name, so launch the installed chrome.exe by its path.
        found = [app['bundle_id'] for app in driver.call('list_apps').get('apps', [])
                 if app.get('name') == 'Google Chrome' and str(app.get('bundle_id')).lower().endswith('chrome.exe')]
        if not found:
            raise RuntimeError('cua-driver lists no installed Google Chrome')
        target = {'path': found[0]}
    elif sys.platform == 'darwin':
        target = {'bundle_id': 'com.google.Chrome'}
    else:
        target = {'name': 'Google Chrome'}
    driver.call('launch_app', **target, creates_new_application_instance=True,
                additional_arguments=[f'--profile-directory={profile}', '--new-window', url])
    for _ in range(30):
        new = set(driver.windows(pid)) - before
        if len(new) == 1:
            return new.pop()
        time.sleep(1)
    raise RuntimeError('no new Chrome window appeared')


def settled_tree(driver, pid, window, deadline, before=''):
    """The window's tree once its page shows text and that text has stopped growing, read again every 2 seconds
    until the deadline, when it returns the last read. Chrome on Windows builds a page's tree only once a tool asks
    for it: on 18 November 2026 a loaded page in a Chrome just started read empty at 1, 7 and 20 seconds and in
    full a second later. A page's scripts can still be adding text: on the Omarchy laptop on 14 November 2026 the
    FT's page had half its text when its address first showed. Text equal to `before`, the page the window showed
    before this one was asked for, counts as none."""
    data, size = None, 0
    while True:
        again = driver.tree(pid, window)
        text = page_text(again.get('tree_markdown', ''))
        grown = 0 if text == before else len(text)
        if size and grown <= size:
            return data
        data, size = again, grown
        if time.monotonic() >= deadline:
            return data
        time.sleep(2)


def address_bar(driver, pid, window):
    data = driver.tree(pid, window, query='Address and search bar', max_elements=60)
    found = re.search(r'\[(\d+)\] (?:AXTextField|Edit) "Address and search bar"'
                      r'(?: = "((?:[^"\\]|\\.)*)"|[^\n]*?value="((?:[^"\\]|\\.)*)")?', data.get('tree_markdown', ''))
    if not found:
        raise RuntimeError('the window has no address bar in its accessibility tree')
    return f'{data["snapshot_id"]}:{found[1]}', found[2] or found[3] or ''


def load(driver, pid, window, url):
    token, _ = address_bar(driver, pid, window)
    driver.call('set_value', pid=pid, window_id=window, element_token=token, value=url)
    driver.call('press_key', pid=pid, window_id=window, element_token=token, key='return')


def host(url):
    return (urlsplit(url if '//' in url else '//' + url).hostname or '').removeprefix('www.')


def same_site(wanted, shown):
    a, b = host(wanted), host(shown)
    return bool(a and b) and (a == b or a.endswith('.' + b) or b.endswith('.' + a))


def rows(tree):
    """(depth, index, role, text) for each numbered row of the page's web area; none when the tree has no web area,
    because the rest of the window is the user's browser, not the page."""
    if re.search(r'^ *- \[\d+\] AX', tree, re.MULTILINE):
        found = [(len(m[1]), m[2], m[3], ((m[5] or m[4] or '').strip())) for m in ROW.finditer(tree)]
    elif re.search(r'^ *- (?:\[\d+\] )?document web\b', tree, re.MULTILINE):
        found = atspi_rows(tree)
    else:
        found = uia_rows(tree)
    start = next((i for i, row in enumerate(found) if row[2] == 'WebArea'), None)
    if start is None:
        return []
    area = found[start][0]
    page = []
    for row in found[start + 1:]:
        if row[0] <= area or TAB_STRIP.match(row[3]):
            break
        page.append(row)
    return page


def uia_rows(tree):
    """rows() for a Windows tree, in its macOS role names. A link's text is its name, and list items and groups,
    which repeat their contents' names, keep no text of their own."""
    found = []
    for m in UIA_ROW.finditer(tree):
        role, name, value = m[3], (m[4] or '').strip(), UIA_VALUE.search(m[5])
        role = 'WebArea' if role == 'Document' and 'id=RootWebArea' in m[5] else UIA_ROLES.get(role, role)
        text = '' if role in ('ListItem', 'Group') else name if role in ('Link', 'WebArea') else (
            value[1].strip() if value else '') or name
        found.append((len(m[1]), m[2], role, text))
    return found


def atspi_rows(tree):
    """rows() for a Linux AT-SPI tree, in macOS role names, leaving out the rows inside a named heading, link,
    button or image, whose name already holds their text."""
    found, named = [], None
    for m in ATSPI_ROW.finditer(tree):
        depth = len(m[1])
        if named is not None and depth > named:
            continue
        named = None
        role = ATSPI_ROLES.get(m[3], m[3])
        name, value = ((group or '').replace(PLACEHOLDER, '').strip() for group in (m[4], m[5]))
        if role in ATSPI_NAMED:
            text = name or value
            named = depth if text else None
        else:
            text = name if role == 'WebArea' else value
        found.append((depth, m[2], role, text))
    return found


def page_text(tree):
    lines, table, cells = [], None, None

    def end_row():
        nonlocal cells
        if cells is not None and any(cells):
            lines.append(' | '.join(' '.join(' '.join(text.split()) for text in cell) for cell in cells))
        cells = None

    for depth, _, role, text in rows(tree):
        if role == 'Image' and '\n' in text:
            text = text.splitlines()[0]
        if table is not None and depth <= table:
            end_row()
            table = None
        if role == 'Table':
            table = depth
            continue
        if table is not None:
            if role == 'Row':
                end_row()
                cells = []
            elif role in CELLS:
                cells = [] if cells is None else cells
                cells.append([text] if text else [])
            elif text and cells:
                cells[-1].append(text)
            continue
        if text:
            lines.append({'Heading': '# ', 'Image': '[image] ', 'Link': '[link] '}.get(role, '') + text)
    end_row()
    return '\n'.join(lines)


def collapsed_questions(tree):
    """Button labels ending in a question mark with no answer below them, as an FAQ accordion shows them."""
    page = rows(tree)
    questions = []
    for i, (depth, _, role, text) in enumerate(page):
        if role != 'Button' or not text.endswith('?'):
            continue
        following = page[i + 1] if i + 1 < len(page) else None
        if following is None or following[0] < depth or following[2] in ('Button', 'Heading', 'Link'):
            questions.append(text)
    return questions[:EXPAND_LIMIT]


def expand(driver, pid, window, questions):
    for question in questions:
        data = driver.tree(pid, window, query=question, max_elements=40)
        found = re.search(rf'\[(\d+)\] (?:AXButton|Button|button|push button) "{re.escape(question)}"',
                          data.get('tree_markdown', ''))
        if found:
            driver.call('click', pid=pid, window_id=window, element_token=f'{data["snapshot_id"]}:{found[1]}')
            time.sleep(0.8)


def close(driver, pid, window):
    """Close only this window, while Chrome is behind the user's app. Chrome on Windows can open it minimized,
    where cua-driver refuses every click and key, so Windows asks the window itself to close through UI
    Automation, after checking that its handle still belongs to that Chrome. Elsewhere press the window's own
    close button: macOS leaves it unnamed, as the leftmost title-bar button; others name it Close."""
    if driver.uia:
        script = ('Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes; '
                  f'$e = [Windows.Automation.AutomationElement]::FromHandle([IntPtr]{int(window)}); '
                  f'if ($e.Current.ProcessId -ne {int(pid)}) {{ exit 3 }}; '
                  '$e.GetCurrentPattern([Windows.Automation.WindowPattern]::Pattern).Close()')
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(f'window {window} did not close; ask the user to close it')
        return
    data = driver.tree(pid, window, tree_format='elements')
    buttons = [e for e in data.get('elements', []) if e.get('depth') == 1 and e.get('role') in ('AXButton', 'Button')]
    named = [e for e in buttons if (e.get('title') or e.get('name') or '').lower() == 'close']
    unnamed = sorted((e for e in buttons if not (e.get('title') or e.get('name'))), key=lambda e: e['frame']['x'])
    button = (named or (unnamed if sys.platform == 'darwin' else []) or [None])[0]
    if button is None:
        raise RuntimeError(f'window {window} has no button named Close; ask the user to close it')
    driver.call('click', pid=pid, window_id=window, element_token=button['element_token'])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('urls', nargs='+')
    parser.add_argument('--profile', default='Default', help="the Chrome profile directory holding the user's subscriptions")
    parser.add_argument('--out', type=Path, help='save each page here instead of printing it')
    parser.add_argument('--cua', default='cua-driver', help='the cua-driver command; cua-driver.exe on WSL')
    parser.add_argument('--wait', type=float, default=30, help='the most seconds to wait for each page to show its text')
    parser.add_argument('--expand', action='store_true', help='open collapsed FAQ answers before reading')
    args = parser.parse_args()
    if sum('nytimes.com' in url for url in args.urls) > NYT_LIMIT:
        parser.error(f'read at most {NYT_LIMIT} NYT pages per session')
    if sys.platform == 'win32':
        sys.stdout.reconfigure(encoding='utf-8')  # a pipe's ANSI code page can't print every page
    for name in ('SIGTERM', 'SIGHUP'):  # a stopped job still closes its window and daemon in `finally`
        if hasattr(signal, name) and signal.getsignal(getattr(signal, name)) is not signal.SIG_IGN:  # as nohup sets
            signal.signal(getattr(signal, name), lambda number, frame: sys.exit(128 + number))
    driver = Driver(args.cua, f'read-page-{secrets.token_hex(3)}')
    desktop = serve = pid = window = None
    skipped = empty = False
    try:
        if not driver.uia:
            desktop = Hyprland.find()
        if desktop is not None:
            desktop.lock()
            serve = daemon(args.cua, desktop)
            driver.socket = serve[1]
        pid = chrome(driver, desktop)
        if pid is None and (refusal := keyring_refusal(desktop.env)):
            raise RuntimeError(refusal)
        for index, url in enumerate(args.urls):
            before = ''
            if window is not None and driver.uia:
                # Windows refuses the address bar's Return key in the background, so each page gets a window.
                close(driver, pid, window)
                for _ in range(10):
                    if window not in driver.windows(pid):
                        break
                    time.sleep(1)
                window = None
            if desktop is not None:
                # Linux ignores the Return key in the background too. The next window opens before the last one
                # closes, so that a Chrome this run started stays running until the end.
                previous, (window, opened) = window, desktop.open(args.profile, url)
                if pid is None:
                    pid = opened  # this run started Chrome
                if opened != pid or NOT_OWN.search(command_line(opened, args.cua)):
                    desktop.close(opened, window)
                    window = previous
                    raise RuntimeError(f"the window opened in Chrome {opened}, not in the user's own Chrome")
                if previous is not None:
                    desktop.close(pid, previous)
            elif window is None:
                window = open_window(driver, pid, args.profile, url)
            else:
                # The window shows the last page until the new one replaces it.
                before = page_text(driver.tree(pid, window).get('tree_markdown', ''))
                load(driver, pid, window, url)
            deadline = time.monotonic() + args.wait
            if desktop is not None:
                # Once the page has an address it has loaded, so read its tree, then its address once more, in
                # case the page's own script sent it to another site meanwhile. A page that never showed an
                # address is skipped without that second wait.
                shown = desktop.wait_for_address(driver, pid, window, deadline)
                data = {}
                if shown:
                    data = settled_tree(driver, pid, window, deadline)
                    shown = desktop.wait_for_address(driver, pid, window, deadline)
            else:
                data = settled_tree(driver, pid, window, deadline, before)
                shown = address_bar(driver, pid, window)[1]
            title = driver.windows(pid).get(window, '')
            if BOT_CHECK.search(title + '\n' + page_text(data.get('tree_markdown', ''))[:2000]):
                print(f'Stopped at a bot check on {url}; tell the user.', file=sys.stderr)
                return 4
            if not same_site(url, shown):
                why = f' ({desktop.why})' if desktop is not None and not shown else ''
                print(f'Skipped {url}: the window shows {shown or "no address"}{why}.', file=sys.stderr)
                skipped = True
                continue
            if args.expand:
                questions = collapsed_questions(data.get('tree_markdown', ''))
                if questions:
                    expand(driver, pid, window, questions)
                    data = driver.tree(pid, window)
            text = page_text(data.get('tree_markdown', ''))
            if not text:
                print(f'{url}: no page text within --wait {args.wait:g} seconds; run it again with a longer wait, '
                      f'such as --wait {args.wait * 2:g}.', file=sys.stderr)
                empty = True
            note = desktop.note if desktop is not None else ''
            page = f'URL: {url}\nADDRESS: {shown}{note}\nTITLE: {title}\n\n{text}\n'
            if args.out:
                args.out.mkdir(parents=True, exist_ok=True)
                slug = re.sub(r'[^a-z0-9]+', '-', url.lower().split('//', 1)[-1]).strip('-')[-90:]
                (args.out / f'{index + 1:02d}-{slug}.txt').write_text(page, encoding='utf-8')
                print(f'{len(text):7d} characters  {url}')
            else:
                print(page)
        return 5 if skipped else 6 if empty else 0
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f'read_page stopped: {error}', file=sys.stderr)
        return 3
    finally:
        try:
            if window is not None and desktop is not None:
                desktop.close(pid, window)
            elif window is not None:
                close(driver, pid, window)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            print(f'read_page could not close window {window}: {error}', file=sys.stderr)
        try:
            if serve is None or serve[0].poll() is None:
                driver.call('end_session')
        except (RuntimeError, OSError, subprocess.SubprocessError):
            pass
        if serve is not None:
            stop_daemon(serve[0], serve[2])


if __name__ == '__main__':
    raise SystemExit(main())
