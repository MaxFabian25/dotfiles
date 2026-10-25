"""Release reviewed changes as a signed public/private pair, push both, and pull them on this device.

Run it with the engine's own Python from the canonical public checkout. `start NAME` makes a worktree of
each repository at origin/main on branch NAME. Edit there. `finish NAME` locks and builds the named
entries, commits both worktrees, rebases them onto whatever origin/main now holds, pairs the private
release.toml with the public commit, pushes, and pulls. `clean NAME` removes the worktrees and branches once origin/main has them.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil
import subprocess
import sys

from portable import GIT
from pull import git

ENGINE = Path(__file__).resolve().parent
PUBLIC_COMMIT = re.compile(r'^public_commit = ".*"$', re.MULTILINE)
# Push with the GitHub CLI's login, which the Mac and WSL hold, whatever helper the checkout configures.
CREDENTIALS = ('-c', 'credential.https://github.com.helper=',
               '-c', 'credential.https://github.com.helper=!gh auth git-credential')
ATTEMPTS = 3


def places(args):
    base = args.worktrees / args.name
    return base, base / 'public', base / 'private'


def run(command, cwd):
    result = subprocess.run(command, cwd=cwd, timeout=1800)
    if result.returncode:
        raise RuntimeError(f'{" ".join(map(str, command[2:4]))} exited {result.returncode}')


def dirty(root):
    return bool(git(root, 'status', '--porcelain', '--untracked-files=normal'))


def ahead(root):
    return git(root, 'rev-parse', 'HEAD') != git(root, 'rev-parse', 'origin/main')


def commit(root, message):
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-F', str(message.resolve()))


def rebase(root):
    """Replay this branch onto origin/main, which another session may have moved since start."""
    git(root, 'fetch', '--prune', 'origin')
    if subprocess.run([*GIT, '-C', str(root), 'merge-base', '--is-ancestor', 'origin/main', 'HEAD'],
                      capture_output=True, timeout=60).returncode == 0:
        return False
    try:
        git(root, 'rebase', '-q', 'origin/main')
    except RuntimeError as error:
        subprocess.run([*GIT, '-C', str(root), 'rebase', '--abort'], capture_output=True, timeout=60)
        raise RuntimeError(f'{root}: the branch conflicts with origin/main; rebase it by hand, then finish again') from error
    return True


def rewrite(path, pattern, replacement):
    """Substitute in place, keeping the file's own line endings."""
    text = path.read_bytes().decode('utf-8')
    changed = pattern.sub(replacement, text)
    if changed != text:
        path.write_bytes(changed.encode('utf-8'))
    return changed != text


def pair(public, private, message):
    """Name the public commit in the private release.toml."""
    head = git(public, 'rev-parse', 'HEAD')
    if rewrite(private / 'release.toml', PUBLIC_COMMIT, f'public_commit = "{head}"'):
        subject = git(public, 'log', '-1', '--format=%s')
        git(private, 'commit', '-q', '-am', message or f'Pair "{subject}"')


def push(root):
    git(root, *CREDENTIALS, 'push', '-q', git(root, 'remote', 'get-url', 'origin'), 'HEAD:main')


def start(args, public_repo, private_repo):
    _, public, private = places(args)
    for repo, tree in ((public_repo, public), (private_repo, private)):
        git(repo, 'fetch', '--prune', 'origin')
        git(repo, 'worktree', 'add', '-q', '-b', args.name, str(tree), 'origin/main')
    print(f'Edit {public} and {private}, then run release.py finish {args.name} --message FILE')
    return 0


def finish(args, public_repo, private_repo):
    base, public, private = places(args)
    python = [sys.executable, '-B']
    setups = ['--public', str(public / 'setup'), '--private', str(private / 'setup')]
    entries = [part for entry in args.entry for part in ('--entry', entry)]
    owned = ['--refresh-owned'] if args.refresh_owned else []
    lock = [*python, 'lock_setup.py', *setups, '--output', str(base / 'build/lock'), *entries, *owned, '--write']
    if args.entry:
        run(lock, public)
    if args.package:
        packages = [part for package in args.package for part in ('--package', package)]
        run([*python, 'build_setup.py', *setups, '--output', str(base / 'build/build'), *packages], public)
    if args.test:
        run([*python, '-m', 'unittest', *args.test], public)
    if dirty(public):
        if not args.message:
            raise RuntimeError(f'{public}: pass --message with the public commit message')
        commit(public, args.message)
    if dirty(private):
        if not args.private_message:
            raise RuntimeError(f'{private}: pass --private-message with the private commit message')
        commit(private, args.private_message)
    paired = ahead(public)
    for attempt in range(1, ATTEMPTS + 1):
        if rebase(public) and args.entry:
            run(lock, public)
            if dirty(public):
                git(public, 'commit', '-q', '-a', '--amend', '--no-edit')
        rebase(private)
        before = git(private, 'rev-parse', 'HEAD')
        if paired:
            pair(public, private, args.pair_message)
        if not ahead(public) and not ahead(private):
            raise RuntimeError('nothing to release: both branches match origin/main')
        if not args.push:
            break
        try:
            if ahead(public):
                push(public)
            push(private)
            break
        except RuntimeError as error:
            git(private, 'reset', '-q', '--hard', before)
            if attempt == ATTEMPTS:
                raise RuntimeError(f'push failed {ATTEMPTS} times; origin keeps moving: {error}') from error
    print(f'public {git(public, "rev-parse", "--short", "HEAD")}  private {git(private, "rev-parse", "--short", "HEAD")}'
          + ('  pushed' if args.push else '  not pushed'))
    if args.pull:
        result = subprocess.run([*python, str(public_repo / 'pull.py'), '--root', str(public_repo),
                                 '--private', str(private_repo), '--update', '--apply'], cwd=public_repo, timeout=1800)
        return result.returncode
    return 0


def clean(args, public_repo, private_repo):
    base, public, private = places(args)
    for repo, tree in ((public_repo, public), (private_repo, private)):
        listed = subprocess.run([*GIT, '-C', str(repo), 'rev-parse', '--verify', '-q', args.name],
                                capture_output=True, timeout=60).returncode == 0
        if listed and not args.force:
            git(repo, 'fetch', '--prune', 'origin')
            merged = subprocess.run([*GIT, '-C', str(repo), 'merge-base', '--is-ancestor', args.name, 'origin/main'],
                                    capture_output=True, timeout=60).returncode == 0
            if not merged:
                raise RuntimeError(f'{repo}: branch {args.name} is not on origin/main; push it, or pass --force')
        if tree.exists():
            git(repo, 'worktree', 'remove', *(['--force'] if args.force else []), str(tree))
        if listed:
            git(repo, 'branch', '-q', '-D', args.name)
    if base.exists():
        shutil.rmtree(base)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('start', 'finish', 'clean'))
    parser.add_argument('name', help='branch name, also the folder under --worktrees')
    parser.add_argument('--private', type=Path, default=ENGINE.parent / 'dotfiles-private', help='canonical private checkout')
    parser.add_argument('--worktrees', type=Path, default=Path.home() / 'worktrees')
    parser.add_argument('--message', type=Path, help='file with the public commit message')
    parser.add_argument('--private-message', type=Path, help='file with the private commit message')
    parser.add_argument('--pair-message', help='message of the private commit that pairs the public one')
    parser.add_argument('--entry', action='append', default=[], help='lock this setup entry (repeat)')
    parser.add_argument('--refresh-owned', action='store_true', help='accept the edited owned trees of the entries')
    parser.add_argument('--package', action='append', default=[], help='build this package (repeat)')
    parser.add_argument('--test', action='append', default=[], help='unittest module to run first (repeat)')
    parser.add_argument('--push', action='store_true', help='push both branches to origin/main')
    parser.add_argument('--pull', action='store_true', help='then pull and apply the pair on this device')
    parser.add_argument('--force', action='store_true', help='clean: remove unpushed branches too')
    args = parser.parse_args(argv)
    try:
        action = {'start': start, 'finish': finish, 'clean': clean}[args.action]
        return action(args, ENGINE, args.private.resolve())
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f'Release stopped: {error}', file=sys.stderr)
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
