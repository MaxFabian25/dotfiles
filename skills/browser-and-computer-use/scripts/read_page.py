#!/usr/bin/env python3
"""Read web pages in the user's own Chrome through cua-driver's accessibility tree, never DevTools.

Opens one Chrome window of its own in the given profile, loads each URL through that window's address
bar, and prints each page's text, or saves it under --out. It stops at a bot check, closes only its own
window, and ends its cua-driver session. Requires the user's own Chrome running, and cua-driver on PATH. A
Chrome that automation started, such as agent-browser's, is never used. On WSL, pass --cua cua-driver.exe to
drive Chrome on the Windows desktop.
"""

import argparse
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

BOT_CHECK = re.compile(r'Just a moment|Security Verification|DataDome|captcha|Are you a robot', re.IGNORECASE)
NYT_LIMIT = 5  # The NYT shows a bot check after a longer run of pages.
NOT_OWN = re.compile(r'--(?:remote-debugging-(?:port|pipe)|headless|enable-automation|user-data-dir|type)\b')
ROW = re.compile(r'- \[\d+\] AX(\w+)(?: "((?:[^"\\]|\\.)*)")?(?: = "((?:[^"\\]|\\.)*)")?', re.DOTALL)


class Driver:
    def __init__(self, command, session):
        self.command, self.session = command, session

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
        return {w['window_id']: w.get('title', '') for w in self.call('list_windows', pid=pid).get('windows', [])
                if w.get('bounds', {}).get('height', 0) > 200}

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
            and (app.get('bundle_id') == 'com.google.Chrome' or app.get('name') in ('Google Chrome', 'chrome'))]
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
    target = {'bundle_id': 'com.google.Chrome'} if sys.platform == 'darwin' else {'name': 'Google Chrome'}
    driver.call('launch_app', **target, creates_new_application_instance=True,
                additional_arguments=[f'--profile-directory={profile}', '--new-window', url])
    for _ in range(30):
        new = set(driver.windows(pid)) - before
        if len(new) == 1:
            return new.pop()
        time.sleep(1)
    raise RuntimeError('no new Chrome window appeared')


def load(driver, pid, window, url):
    data = driver.tree(pid, window, query='Address and search bar', max_elements=60)
    found = re.search(r'\[(\d+)\] AXTextField "Address and search bar"', data.get('tree_markdown', ''))
    if not found:
        raise RuntimeError('the window has no address bar in its accessibility tree')
    token = f'{data["snapshot_id"]}:{found[1]}'
    driver.call('set_value', pid=pid, window_id=window, element_token=token, value=url)
    driver.call('press_key', pid=pid, window_id=window, element_token=token, key='return')


def page_text(tree):
    start = tree.find('AXWebArea')
    lines = []
    for role, label, value in ROW.findall(tree[start if start >= 0 else 0:]):
        text = (value or label).strip()
        if role == 'Image' and '\n' in text:
            text = text.splitlines()[0]
        if text:
            lines.append({'Heading': '# ', 'Image': '[image] ', 'Link': '[link] '}.get(role, '') + text)
    return '\n'.join(lines)


def close(driver, pid, window):
    """Press the window's own close button, which works while Chrome is behind the user's app. Only macOS
    leaves it unnamed, as the leftmost title-bar button; elsewhere press only a button named Close."""
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
    args = parser.parse_args()
    if args.out and sys.platform != 'darwin':
        parser.error('--out works only on macOS; the employer manages Windows and WSL, so keep article text out of files there')
    if sum('nytimes.com' in url for url in args.urls) > NYT_LIMIT:
        parser.error(f'read at most {NYT_LIMIT} NYT pages per session')
    driver = Driver(args.cua, f'read-page-{secrets.token_hex(3)}')
    pid = window = None
    try:
        pid = chrome(driver)
        for index, url in enumerate(args.urls):
            if window is None:
                window = open_window(driver, pid, args.profile, url)
            else:
                load(driver, pid, window, url)
            time.sleep(args.wait)
            data = driver.tree(pid, window)
            text = page_text(data.get('tree_markdown', ''))
            title = driver.windows(pid).get(window, '')
            if BOT_CHECK.search(title + '\n' + text[:2000]):
                print(f'Stopped at a bot check on {url}; tell the user.', file=sys.stderr)
                return 4
            page = f'URL: {url}\nTITLE: {title}\n\n{text}\n'
            if args.out:
                args.out.mkdir(parents=True, exist_ok=True)
                slug = re.sub(r'[^a-z0-9]+', '-', url.lower().split('//', 1)[-1]).strip('-')[-90:]
                (args.out / f'{index + 1:02d}-{slug}.txt').write_text(page, encoding='utf-8')
                print(f'{len(text):7d} characters  {url}')
            else:
                print(page)
        return 0
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
