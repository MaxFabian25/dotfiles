"""Pull reviewed dotfiles releases and reconcile this device. Check-only unless --apply is supplied."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tomllib

from portable import GIT, exclusive_lock


def git(root, *args):
    result = subprocess.run([*GIT, '-C', str(root), *args], capture_output=True, text=True, encoding='utf-8', timeout=180)
    if result.returncode:
        raise RuntimeError(f'{root.name}: git {args[0]} failed: {result.stderr.strip()}')
    return result.stdout.strip()


def update(public, private):
    roots = [public, *([private] if private else [])]
    heads = {}
    for root in roots:
        if git(root, 'status', '--porcelain', '--untracked-files=normal'):
            raise RuntimeError(f'{root}: local changes; pull left all checkouts untouched')
        git(root, 'rev-parse', '--abbrev-ref', '@{upstream}')
        heads[root] = git(root, 'rev-parse', 'HEAD')
    for root in roots:
        git(root, 'fetch', '--prune', 'origin')
    selected = {root: git(root, 'rev-parse', '@{upstream}') for root in roots}
    if private:
        release = tomllib.loads(git(private, 'show', selected[private] + ':release.toml'))
        from host_state import parse_commit
        wanted = parse_commit(release.get('public_commit'))
        result = subprocess.run([*GIT, '-C', str(public), 'merge-base', '--is-ancestor', wanted, selected[public]],
                                capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError('Private release names a public commit outside the fetched public history')
        selected[public] = wanted
    for root in roots:
        result = subprocess.run([*GIT, '-C', str(root), 'merge-base', '--is-ancestor', heads[root], selected[root]],
                                capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError(f'{root}: selected release would discard local commits; no checkout was advanced')
    for root in roots:
        if git(root, 'rev-parse', 'HEAD') != heads[root] or git(root, 'status', '--porcelain', '--untracked-files=normal'):
            raise RuntimeError(f'{root}: changed during pull; no device files changed')
        git(root, 'merge', '--ff-only', selected[root])


def paired(public, private):
    """A private release names its reviewed public commit, preventing half-published fleet updates."""
    for root in (public, *([private] if private else [])):
        if git(root, 'status', '--porcelain', '--untracked-files=normal'):
            raise RuntimeError(f'{root}: local changes; a release pull requires clean checkouts')
    if private is None:
        return
    release = private / 'release.toml'
    if not release.is_file():
        raise RuntimeError(f'{release}: publish a paired release before enabling automatic pulls')
    expected = tomllib.loads(release.read_text(encoding='utf-8')).get('public_commit')
    actual = git(public, 'rev-parse', 'HEAD')
    if expected != actual:
        raise RuntimeError(f'Public/private release mismatch: expected public {expected}, found {actual}; no device changes')


def run(args):
    root = args.root.resolve()
    private = args.private.resolve() if args.private else None
    state = Path.home() / '.local/state/dotfiles'
    with exclusive_lock(state / 'pull.lock'):
        if args.update:
            update(root, private)
        paired(root, private)
        command = [sys.executable, str(root / 'sync.py'), '--public', str(root / 'setup'),
                   '--apply' if args.apply else '--check']
        if private:
            command += ['--private', str(private / 'setup')]
        result = subprocess.run(command, cwd=root, timeout=1200)
        if result.returncode == 0 and args.apply:
            from sync_files import Files, contents
            receipt = state / 'last-applied.json'
            commits = {'public': git(root, 'rev-parse', 'HEAD'),
                       'private': git(private, 'rev-parse', 'HEAD') if private else None}
            before = contents(receipt)
            previous = json.loads(before) if before else {}
            if any(previous.get(key) != value for key, value in commits.items()):
                data = {'at': datetime.now(timezone.utc).isoformat(), **commits}
                Files(state / 'receipt-backups').write(receipt, (json.dumps(data, indent=2) + '\n').encode(), expected=before)
        return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--private', type=Path)
    parser.add_argument('--update', action='store_true', help='fetch and fast-forward clean paired checkouts')
    parser.add_argument('--apply', action='store_true', help='apply the verified device plan')
    args = parser.parse_args()
    try:
        return run(args)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'Dotfiles pull stopped: {error}', file=sys.stderr)
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
