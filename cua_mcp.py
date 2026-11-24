"""Point the cua-driver MCP server of Claude Code and Codex at cua-driver-defaults, the private dotfiles' wrapper that
gives an accessibility-tree read without timeout_ms a longer budget, or back at the driver. A pull runs this on Linux
after it delivers the home files, which bring the wrapper to ~/.local/bin.

The private checkout's config/cua-driver/mcp.toml decides: `wrapper = true` points each server at the wrapper while
the wrapper is there, and `wrapper = false` points it back. Without that file nothing changes.

Each host registers this server by hand, so a pull owns only two of its fields: the command, and CUA_DRIVER_BIN in
its environment, which names the driver the wrapper runs. It moves a server only between two shapes: the driver
itself, a command whose file is named cua-driver with the one argument mcp, and the wrapper with that command in
CUA_DRIVER_BIN. Pointing back therefore restores the command exactly. A server in any other shape is left as it is and
named, and a host without the server gets none.

Claude Code keeps the server in ~/.claude.json, which every running Claude process rewrites. Claude Code 2.1.331
writes that file under a lock, the folder ~/.claude.json.lock, and reads the file again under the lock before it
applies its own change. So this takes the same lock around its read and write, and leaves the file to the next pull
while a Claude process holds it. Codex takes no lock on ~/.codex/config.toml, so there the write checks that the file
still holds what was read just before it replaces it, as the sync's own writes there do. Each file is saved under
~/.local/state/dotfiles/mcp-server-backups/ before its first change."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import time
import tomllib

import tomlkit

from configuration import CUA_WRAPPER
from portable import codex_home
from sync_files import ConcurrentEdit, Files, contents

SWITCH = 'config/cua-driver/mcp.toml'
SERVER = 'cua-driver'
DRIVER_BIN = 'CUA_DRIVER_BIN'
# Claude Code holds its lock for one read and one write, and counts a lock untouched for 10 seconds as abandoned.
LOCK_TRIES = 25
LOCK_PAUSE = 0.2


class Left(Exception):
    """A server this leaves as it is, with the reason."""


@dataclass(frozen=True)
class Outcome:
    to_wrapper: bool
    changed: tuple[str, ...]              # pointed, or with apply off, waiting for that
    left: tuple[tuple[str, str], ...]     # where, and why it stays as it is

    def lines(self, applied: bool) -> list[str]:
        verb = ('pointed' if applied else 'to point') + (' at cua-driver-defaults' if self.to_wrapper
                                                         else ' back at the driver')
        return [*(f'MCP server {verb}: {SERVER} in {where}' for where in self.changed),
                *(f'MCP server left as it is: {SERVER} in {where}: {reason}' for where, reason in self.left)]


def wanted(private: Path, home: Path) -> bool | None:
    """True for the wrapper, False for the driver, None when the private checkout leaves the servers alone."""
    data = contents(private / SWITCH)
    if data is None:
        return None
    table = tomllib.loads(data.decode('utf-8'))
    if set(table) != {'wrapper'} or not isinstance(table['wrapper'], bool):
        raise ValueError(f'{SWITCH} must hold only wrapper = true or wrapper = false')
    # A server pointed at a wrapper that is not there would not start.
    return table['wrapper'] and (home / CUA_WRAPPER).is_file()


def claude_config(home: Path) -> Path:
    """The file Claude Code keeps its user-scope MCP servers in."""
    directory = os.environ.get('CLAUDE_CONFIG_DIR')
    if directory and Path(home) == Path.home():
        return Path(directory).expanduser() / '.claude.json'
    return Path(home) / '.claude.json'


def _shape(server: object, wrapper: str) -> str | None:
    """'driver' or 'wrapper' for the two shapes this moves a server between, else None."""
    if not isinstance(server, dict) or server.get('args') != ['mcp'] or server.get('type', 'stdio') != 'stdio':
        return None
    command, environment = server.get('command'), server.get('env', {})
    if not isinstance(command, str) or not isinstance(environment, dict):
        return None
    if PurePosixPath(command).name == 'cua-driver' and DRIVER_BIN not in environment:
        return 'driver'
    driver = environment.get(DRIVER_BIN)
    if command == wrapper and isinstance(driver, str) and PurePosixPath(driver).name == 'cua-driver':
        return 'wrapper'
    return None


def rewritten(data: bytes, syntax: str, wrapper: str, to_wrapper: bool) -> bytes | None:
    """The file with its server pointed as wanted, or None when it needs no change. Raises Left for a server in a
    shape this does not move."""
    text = data.decode('utf-8')
    if syntax == 'json':
        document = json.loads(text) if text.strip() else {}
        servers = document.get('mcpServers') if isinstance(document, dict) else None
    else:
        document = tomlkit.parse(text)
        servers = document.get('mcp_servers')
    server = servers.get(SERVER) if isinstance(servers, dict) else None
    if server is None:
        return None
    plain = server.unwrap() if hasattr(server, 'unwrap') else server
    shape = _shape(plain, wrapper)
    if shape is None:
        raise Left('it is neither cua-driver run with the one argument mcp nor cua-driver-defaults with CUA_DRIVER_BIN')
    if (shape == 'wrapper') == to_wrapper:
        return None
    if to_wrapper:
        if 'env' not in server:
            server['env'] = {}
        server['env'][DRIVER_BIN] = plain['command']
        server['command'] = wrapper
    else:
        server['command'] = plain['env'][DRIVER_BIN]
        del server['env'][DRIVER_BIN]
    if syntax == 'json':
        # Claude Code writes the file as JSON.stringify(config, null, 2) does, so only the changed lines differ.
        return (json.dumps(document, indent=2, ensure_ascii=False) + ('\n' if text.endswith('\n') else '')).encode()
    return tomlkit.dumps(document).encode()


@contextmanager
def claude_lock(path: Path):
    """Claude Code's own lock on its config file: a folder beside the file, which Claude Code creates to take the
    lock and removes to give it back. Yields a check that the lock is still this one."""
    lock = path.with_name(path.name + '.lock')
    for _ in range(LOCK_TRIES):
        try:
            lock.mkdir()
            break
        except FileExistsError:
            time.sleep(LOCK_PAUSE)
    else:
        raise Left('a Claude Code process held its lock on the file, so the next pull tries again')
    taken = lock.stat().st_mtime_ns
    try:
        yield lambda: lock.stat().st_mtime_ns == taken
    finally:
        lock.rmdir()


def _reconcile(path: Path, syntax: str, wrapper: str, to_wrapper: bool, apply: bool, files: Files) -> bool:
    data = contents(path)
    if data is None or rewritten(data, syntax, wrapper, to_wrapper) is None:
        return False
    if not apply:
        return True
    with claude_lock(path) if syntax == 'json' else nullcontext(lambda: True) as held:
        before = contents(path)
        after = None if before is None else rewritten(before, syntax, wrapper, to_wrapper)
        if after is None:
            return False
        if not held():
            raise Left('Claude Code took its lock back before the write, so the next pull tries again')
        files.write(path, after, expected=before)
    return True


def reconcile(private: Path, home: Path, state: Path, *, apply: bool) -> Outcome | None:
    """None when the private checkout has no switch."""
    to_wrapper = wanted(private, home)
    if to_wrapper is None:
        return None
    wrapper = str(home / CUA_WRAPPER)
    files = Files(state / 'mcp-server-backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    changed, left = [], []
    for path, syntax in ((claude_config(home), 'json'), (codex_home(home) / 'config.toml', 'toml')):
        where = '~/' + path.relative_to(home).as_posix() if path.is_relative_to(home) else str(path)
        try:
            if _reconcile(path, syntax, wrapper, to_wrapper, apply, files):
                changed.append(where)
        except Left as reason:
            left.append((where, str(reason)))
        except ConcurrentEdit:
            left.append((where, 'it changed while the pull wrote it, so the next pull tries again'))
        except (OSError, ValueError) as error:   # ValueError covers JSON, TOML and UTF-8 errors
            left.append((where, f'it could not be read or written: {error}'))
    return Outcome(to_wrapper, tuple(changed), tuple(left))
