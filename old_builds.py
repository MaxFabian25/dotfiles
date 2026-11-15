"""Remove the aligned plugin builds that no running Claude Code session can read.

Each release of an aligned plugin gets a version folder of its own, in the catalog under
`marketplaces/codex-aligned/plugins` and in Claude Code's copy under `plugins/cache/codex-aligned`, and the folder
it replaces stays, because a session reads the build that was current when it started until it ends. Nothing
removed those folders: on 15 November 2026 one machine held 41 builds of one plugin in three weeks.

A pull cannot see which sessions run. It can see when this machine started, and every session running now started
after that. So a pull notes when it first finds a build that is no longer current, which is at or after the moment
the build was replaced, and a later pull removes the build once the machine has started since that note.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import time

CATALOG = 'marketplaces/codex-aligned'
TREES = (f'{CATALOG}/plugins', 'plugins/cache/codex-aligned')


def started() -> float | None:
    """When this machine started, in seconds since the epoch. None where that cannot be read, which removes nothing."""
    if os.name == 'nt':
        import ctypes
        ticks = ctypes.windll.kernel32.GetTickCount64
        ticks.restype = ctypes.c_uint64
        return time.time() - ticks() / 1000
    try:
        for line in Path('/proc/stat').read_text(encoding='ascii').splitlines():
            if line.startswith('btime '):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def builds(claude_home: Path) -> list[Path]:
    """Every version folder in the two trees. A name that starts with a dot is a work folder of a build in progress."""
    found = []
    for tree in TREES:
        for plugin in sorted((claude_home / tree).glob('*')):
            if plugin.is_dir() and not plugin.is_symlink():
                found += [build for build in sorted(plugin.iterdir())
                          if build.is_dir() and not build.is_symlink() and not build.name.startswith('.')]
    return found


def current(claude_home: Path) -> set[Path] | None:
    """The builds a new session can load: each one the catalog offers, in both trees' spelling of its version, and
    each one Claude Code's install record names at any scope. None when either file cannot be read as expected."""
    try:
        catalog = json.loads((claude_home / CATALOG / '.claude-plugin/marketplace.json').read_bytes())
        record = json.loads((claude_home / 'plugins/installed_plugins.json').read_bytes())
        kept = set()
        for plugin in catalog['plugins']:
            name, version = plugin['name'], plugin['version']
            kept.add(claude_home / TREES[0] / name / version)
            kept.add(claude_home / TREES[1] / name / version.replace('+', '-'))
        for entries in record['plugins'].values():
            kept.update(Path(entry['installPath']) for entry in entries)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return kept


def sweep(claude_home: Path, notes: Path, now: float, machine_started: float | None) -> list[Path]:
    """Note each build that is no longer current, and remove the ones noted before this machine started. Gives the
    builds removed. A build that is current again loses its note, so a later replacement notes it anew."""
    kept = current(claude_home)
    if kept is None:
        return []
    try:
        noted = json.loads(notes.read_bytes())
    except (OSError, ValueError):
        noted = {}
    still, removed = {}, []
    for build in builds(claude_home):
        if build in kept:
            continue
        at = noted.get(str(build)) if isinstance(noted, dict) else None
        if not isinstance(at, (int, float)) or isinstance(at, bool):
            at = now
        if machine_started is not None and at < machine_started:
            try:
                shutil.rmtree(build)
            except OSError as error:
                print(f'Old plugin build left in place: {build}: {error}', file=sys.stderr)
                still[str(build)] = at
                continue
            removed.append(build)
        else:
            still[str(build)] = at
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(json.dumps(still, indent=2) + '\n', encoding='utf-8')
    return removed
