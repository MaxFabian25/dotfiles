"""Read an installed account skill through its native inventory, preserving its resource paths."""
from __future__ import annotations

import argparse
from collections import deque
import errno
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
import tempfile
from threading import Thread
import time

import portable

EXITED = 'Native app-server exited before replying'
EXIT_WAIT_SECONDS = 5
# A fresh app-server lists an account's remote plugins only once the account has loaded: on the Omarchy laptop on
# 2026-11-14 their skills appeared 3.6 and 9.8 s after the start in two runs, so a listing taken at once lacked them.
SOURCE_WAIT_SECONDS = 20
SOURCE_POLL_SECONDS = 0.5
# Ten, because when Codex's npm wrapper fails, Node's report puts the error's line seventh from the end.
STDERR_LINES = 10
STDERR_WIDTH = 400
SEQUENCE = re.compile(r'\x1b[\]P_^X][^\x07\x1b\x9c]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[ -/]+[0-~]')
CONTROL = re.compile(r'[\x00-\x08\x0a-\x1f\x7f-\x9f]')
KEY_MARKER = re.compile(r'-----(BEGIN|END) [A-Z ]*-----')
SECRET_NAME = (r'(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|authorization'
               r'|cookie)')
CREDENTIALS = (
    re.compile(SECRET_NAME + r'''[\w.-]*["']?\s*[:=]\s*\S''', re.I),
    re.compile(r'(?<![\w-])(?=[\w-]*[_-])[\w-]*' + SECRET_NAME + r'[\w-]*\s+\S', re.I),
    re.compile(r'(?<![\w-])[A-Z0-9_]*' + SECRET_NAME.upper() + r'[A-Z0-9_]*\s+\S'),
    re.compile(r'[a-z][\w+.-]*://[^\s/:@]*:[^\s/@]+@', re.I),
    re.compile(r'bearer\s+\S', re.I),
    re.compile(r'(?i:basic)\s+(?=[A-Za-z0-9+/]*[A-Z0-9+/])[A-Za-z0-9+/]{8,}'),
    re.compile(r'gh[pousr]_[A-Za-z0-9]{8,}|github_pat_\w{8,}'),
    re.compile(r'(?:AKIA|ASIA)[0-9A-Z]{16}'),
    re.compile(r'xox[abposr]-[A-Za-z0-9-]+'),
    re.compile(r'sk-[\w-]{16,}'),
    re.compile(r'eyJ[\w-]{8,}\.[\w-]{8,}'),
)
WITHHELD = '[withheld: the line may hold a credential]'


def stderr_lines(file):
    """The server shares the file's offset, so only a server that has exited is read."""
    file.seek(0)
    kept = deque(maxlen=STDERR_LINES)
    key = False
    for raw in file:
        text = CONTROL.sub(' ', SEQUENCE.sub('', raw.decode('utf-8', 'replace'))).strip()
        if not text:
            continue
        markers = KEY_MARKER.findall(text)
        kept.append((text, key or bool(markers)))
        if markers:
            key = markers[-1] == 'BEGIN'
    return [WITHHELD if in_key or any(pattern.search(text) for pattern in CREDENTIALS)
            else text[:STDERR_WIDTH] + '...' if len(text) > STDERR_WIDTH else text for text, in_key in kept]


def stopped_reading(error):
    """A write to an app-server that has exited fails with a broken pipe, which Windows reports as EINVAL."""
    return isinstance(error, BrokenPipeError) or (portable.WINDOWS and error.errno == errno.EINVAL)


class Client:
    def __init__(self, profile=None):
        env = portable.native_cli_environment()
        if profile is not None:
            env['CODEX_HOME'] = str(profile)
        env.pop('CODEX_SQLITE_HOME', None)
        # A file, because a pipe that nobody reads fills and stalls the server.
        self.stderr = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen([portable.codex_cli(), 'app-server', '--stdio'],
                                            cwd=Path.home(), env=env, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=self.stderr,
                                            text=True, encoding='utf-8')
        except BaseException:
            self.stderr.close()
            raise
        self.queue = Queue()
        self.identifier = 0
        self.reader = Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.call('initialize', {'clientInfo': {'name': 'dotfiles-account-skills', 'version': '1.0'},
                                     'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized'})
        except Exception:
            self.close()
            raise

    def _read(self):
        for line in self.process.stdout:
            self.queue.put(line)
        self.queue.put(None)

    def send(self, value):
        try:
            self.process.stdin.write(json.dumps(value) + '\n')
            self.process.stdin.flush()
        except OSError as error:
            if not stopped_reading(error):
                raise
            raise self.exited() from error

    def exited(self):
        try:
            self.process.wait(timeout=EXIT_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            return RuntimeError(EXITED)
        try:
            lines = stderr_lines(self.stderr)
        except OSError as error:
            lines = [f'could not be read: {error}']
        return RuntimeError('\n'.join([EXITED, *(f'app-server stderr: {line}' for line in lines)]))

    def call(self, method, params):
        self.identifier += 1
        self.send({'id': self.identifier, 'method': method, 'params': params})
        deadline = time.monotonic() + 60
        while True:
            try:
                line = self.queue.get(timeout=max(0.01, deadline - time.monotonic()))
            except Empty as error:
                raise RuntimeError(f'Native app-server timed out: {method}') from error
            if line is None:
                raise self.exited()
            message = json.loads(line)
            if message.get('id') != self.identifier:
                continue
            if 'error' in message:
                raise RuntimeError(f'{method}: {message["error"]}')
            return message['result']

    def skills(self, wanted=()):
        """The native inventory, listed again until it holds a publisher skill for each wanted name, or until
        SOURCE_WAIT_SECONDS have passed, when it is returned as it is for select_source to refuse."""
        deadline = time.monotonic() + SOURCE_WAIT_SECONDS
        while True:
            listing = self.call('skills/list', {'cwds': [str(Path.home())], 'forceReload': True})
            if any(group.get('errors') for group in listing['data']):
                raise RuntimeError('Native skill discovery reported errors')
            skills = [skill for group in listing['data'] for skill in group['skills']]
            if all(publisher_skills(skills, name) for name in wanted) or time.monotonic() >= deadline:
                return skills
            time.sleep(SOURCE_POLL_SECONDS)

    def close(self):
        self.stderr.close()
        if self.process.stdin:
            try:
                self.process.stdin.close()
            except OSError as error:
                # Closing flushes what a failed send left behind, and closes the pipe even when that fails.
                if not stopped_reading(error):
                    raise
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.reader.join(timeout=5)
        self.process.stdout.close()


def publisher_skills(skills, name):
    """The skills named name that the account's remote plugin of that name supplies."""
    plugin = name.split(':', 1)[0] + '@openai-curated-remote'
    return [s for s in skills if s['name'] == name and s.get('pluginId') == plugin]


def check_name(name):
    if not re.fullmatch(r'[a-z0-9-]+:[a-z0-9-]+', name):
        raise ValueError('Expected a qualified account skill name: plugin:skill')


def select_source(skills, name):
    check_name(name)
    matches = publisher_skills(skills, name)
    if len(matches) != 1:
        raise RuntimeError(f'Expected one installed publisher skill for {name}; found {len(matches)}')
    path = Path(matches[0]['path'])
    if not path.is_absolute() or path.name != 'SKILL.md':
        raise RuntimeError(f'Native inventory returned an invalid skill path for {name}')
    return path


def windows_handoff(profile, script):
    """A Windows desktop skill may be invoked by a tool running in WSL."""
    if portable.WINDOWS or not profile:
        return None
    value = str(profile).replace('\\', '/')
    if re.match(r'^[A-Za-z]:/', value):
        value = '/mnt/' + value[0].lower() + value[2:]
    match = re.match(r'^/mnt/([a-z])/Users/([^/]+)(?:/|$)', value)
    if not match:
        return None
    home = Path('/mnt') / match[1] / 'Users' / match[2]
    python = home / 'dotfiles/.venv/Scripts/python.exe'
    windows_script = home / 'dotfiles' / script
    if not python.is_file() or not windows_script.is_file():
        raise RuntimeError('The Windows dotfiles reader and Python must be installed before invoking this Windows skill')
    native = lambda p: match[1].upper() + ':/' + str(p).removeprefix('/mnt/' + match[1] + '/')
    return [str(python), native(windows_script), '--profile', native(Path(value))]


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name')
    parser.add_argument('--profile', type=Path)
    args = parser.parse_args(argv)
    profile = args.profile or os.environ.get('CODEX_HOME')
    try:
        # Before the app-server starts, because a name no publisher skill can have would wait out SOURCE_WAIT_SECONDS.
        check_name(args.name)
        if command := windows_handoff(profile, 'read_account_skill.py'):
            return subprocess.run([*command, args.name], timeout=120).returncode
        client = Client(profile)
        try:
            path = select_source(client.skills((args.name,)), args.name)
            content = portable.read_utf8(path)
        finally:
            client.close()
        print(f'Publisher skill: {args.name}\nSource: {path}\n'
              'Resolve all relative references and resources against this source path.\n\n' + content)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'Account skill unavailable: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
