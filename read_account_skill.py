"""Read an installed account skill through its native inventory, preserving its resource paths."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
from threading import Thread
import time

import portable


class Client:
    def __init__(self, profile=None):
        env = portable.native_cli_environment()
        if profile is not None:
            env['CODEX_HOME'] = str(profile)
        env.pop('CODEX_SQLITE_HOME', None)
        self.process = subprocess.Popen([portable.codex_cli(), 'app-server', '--stdio'],
                                        cwd=Path.home(), env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, encoding='utf-8')
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
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()

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
                raise RuntimeError('Native app-server exited before replying')
            message = json.loads(line)
            if message.get('id') != self.identifier:
                continue
            if 'error' in message:
                raise RuntimeError(f'{method}: {message["error"]}')
            return message['result']

    def skills(self):
        listing = self.call('skills/list', {'cwds': [str(Path.home())], 'forceReload': True})
        if any(group.get('errors') for group in listing['data']):
            raise RuntimeError('Native skill discovery reported errors')
        return [skill for group in listing['data'] for skill in group['skills']]

    def close(self):
        if self.process.stdin:
            self.process.stdin.close()
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


def select_source(skills, name):
    if not re.fullmatch(r'[a-z0-9-]+:[a-z0-9-]+', name):
        raise ValueError('Expected a qualified account skill name: plugin:skill')
    plugin = name.split(':', 1)[0] + '@openai-curated-remote'
    matches = [s for s in skills if s['name'] == name and s.get('pluginId') == plugin]
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name')
    parser.add_argument('--profile', type=Path)
    args = parser.parse_args(argv)
    profile = args.profile or os.environ.get('CODEX_HOME')
    try:
        if command := windows_handoff(profile, 'read_account_skill.py'):
            return subprocess.run([*command, args.name], timeout=120).returncode
        client = Client(profile)
        try:
            path = select_source(client.skills(), args.name)
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
