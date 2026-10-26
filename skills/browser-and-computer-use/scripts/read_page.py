#!/usr/bin/env python3
"""Read web pages in the user's own Chrome through cua-driver's accessibility tree, never DevTools.

Opens one Chrome window of its own in the given profile, loads each URL through that window's address
bar, and prints each page's text, or saves it under --out. It reads only the page, never the tab strip or
the rest of the browser, prints each table row on one line, and with --expand first opens collapsed FAQ
answers. It skips a page whose address bar shows another site, such as a sign-in redirect. It stops at a
bot check, closes only its own window, and ends its cua-driver session. Requires the user's own Chrome
running, and cua-driver on PATH. A Chrome that automation started, such as agent-browser's, is never used.
On WSL, pass --cua cua-driver.exe to drive Chrome on the Windows desktop.
"""

import argparse
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from urllib.parse import urlsplit

BOT_CHECK = re.compile(r'Just a moment|Security Verification|DataDome|captcha|Are you a robot', re.IGNORECASE)
NYT_LIMIT = 5  # The NYT shows a bot check after a longer run of pages.
NOT_OWN = re.compile(r'--(?:remote-debugging-(?:port|pipe)|headless|enable-automation|user-data-dir|type)\b')
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


class Driver:
    def __init__(self, command, session):
        self.command, self.session = command, session
        self.uia = sys.platform == 'win32' or command.endswith('.exe')  # cua-driver.exe on WSL drives Windows

    def call(self, tool, **args):
        result = subprocess.run([self.command, 'call', tool], input=json.dumps({'session': self.session, **args}),
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


def chrome(driver):
    """The user's own Chrome. Automation, such as agent-browser, starts Chrome with DevTools, headless or with a
    profile folder of its own, and Chrome's helper processes carry --type; skip all of those."""
    apps = [app for app in driver.call('list_apps').get('apps', [])
            if app.get('pid') and app.get('running', True)
            and (app.get('bundle_id') == 'com.google.Chrome' or app.get('name') in ('Google Chrome', 'chrome', 'chrome.exe'))]
    if not apps:
        raise RuntimeError('Google Chrome is not running; start it, then run this again')
    lines = {app['pid']: command_line(app['pid'], driver.command) for app in apps}
    own = [app for app in apps if lines[app['pid']] and not NOT_OWN.search(lines[app['pid']])]
    if not own and len(apps) == 1 and not lines[apps[0]['pid']]:
        own = apps  # the only Chrome, and its command line can't be read
    if len(own) != 1:
        found = '; '.join(f"pid {app['pid']}: {lines[app['pid']][:80] or 'command line unreadable'}" for app in apps)
        raise RuntimeError(f"can't tell which Chrome is the user's own ({found})")
    return own[0]['pid']


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
        found = re.search(rf'\[(\d+)\] (?:AX)?Button "{re.escape(question)}"', data.get('tree_markdown', ''))
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
    parser.add_argument('--out', type=Path, help='save each page here instead of printing it; macOS only')
    parser.add_argument('--cua', default='cua-driver', help='the cua-driver command; cua-driver.exe on WSL')
    parser.add_argument('--wait', type=float, default=7, help='seconds to let each page load')
    parser.add_argument('--expand', action='store_true', help='open collapsed FAQ answers before reading')
    args = parser.parse_args()
    if args.out and sys.platform != 'darwin':
        parser.error('--out works only on macOS; the employer manages Windows and WSL, so keep article text out of files there')
    if sum('nytimes.com' in url for url in args.urls) > NYT_LIMIT:
        parser.error(f'read at most {NYT_LIMIT} NYT pages per session')
    if sys.platform == 'win32':
        sys.stdout.reconfigure(encoding='utf-8')  # a pipe's ANSI code page can't print every page
    driver = Driver(args.cua, f'read-page-{secrets.token_hex(3)}')
    pid = window = None
    skipped = False
    try:
        pid = chrome(driver)
        for index, url in enumerate(args.urls):
            if window is not None and driver.uia:
                # Windows refuses the address bar's Return key in the background, so each page gets a window.
                close(driver, pid, window)
                for _ in range(10):
                    if window not in driver.windows(pid):
                        break
                    time.sleep(1)
                window = None
            if window is None:
                window = open_window(driver, pid, args.profile, url)
            else:
                load(driver, pid, window, url)
            time.sleep(args.wait)
            data = driver.tree(pid, window)
            title = driver.windows(pid).get(window, '')
            _, shown = address_bar(driver, pid, window)
            if BOT_CHECK.search(title + '\n' + page_text(data.get('tree_markdown', ''))[:2000]):
                print(f'Stopped at a bot check on {url}; tell the user.', file=sys.stderr)
                return 4
            if not same_site(url, shown):
                print(f'Skipped {url}: the window shows {shown or "no address"}.', file=sys.stderr)
                skipped = True
                continue
            if args.expand:
                questions = collapsed_questions(data.get('tree_markdown', ''))
                if questions:
                    expand(driver, pid, window, questions)
                    data = driver.tree(pid, window)
            text = page_text(data.get('tree_markdown', ''))
            if not text:
                print(f'{url}: the window shows no page content yet; try a longer --wait.', file=sys.stderr)
            page = f'URL: {url}\nADDRESS: {shown}\nTITLE: {title}\n\n{text}\n'
            if args.out:
                args.out.mkdir(parents=True, exist_ok=True)
                slug = re.sub(r'[^a-z0-9]+', '-', url.lower().split('//', 1)[-1]).strip('-')[-90:]
                (args.out / f'{index + 1:02d}-{slug}.txt').write_text(page, encoding='utf-8')
                print(f'{len(text):7d} characters  {url}')
            else:
                print(page)
        return 5 if skipped else 0
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f'read_page stopped: {error}', file=sys.stderr)
        return 3
    finally:
        try:
            if window is not None:
                close(driver, pid, window)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            print(f'read_page could not close window {window}: {error}', file=sys.stderr)
        try:
            driver.call('end_session')
        except (RuntimeError, OSError, subprocess.SubprocessError):
            pass


if __name__ == '__main__':
    raise SystemExit(main())
