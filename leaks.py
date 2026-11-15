"""Say where this public checkout holds a string that the private denylist names: in its working tree, in a commit
that no branch on a remote holds yet, or in such a commit's message. Run it before a branch of this repository is
pushed, because a pushed branch is public at once and GitHub offers no check that reads a private list.

    python -B leaks.py --private ../dotfiles-private [--message FILE]

Prints each hit as path:line, or as `commit <id>:` and the place, without the string, and exits 1 when there is
one. A hit in a commit stays until that commit is rewritten, since removing the string in a later commit still
pushes the earlier one."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys

from portable import GIT

DENYLIST = 'config/privacy/public-denylist.txt'
DENYLIST_COMMENT = re.compile(r'(?:^|\s)#')


def leaks(public, private, message=None):
    """Where the public tree, as a commit would take it, or its commit message holds a string the private
    denylist names. One string per line, matched without regard to case; # starts a comment, at the start of a line
    or after white space."""
    listed = private / DENYLIST
    if not listed.is_file():
        raise RuntimeError(f'{listed} is missing, so nothing checks the public side for the strings it names')
    try:
        text = listed.read_text(encoding='utf-8')
        if '\0' in text:
            raise ValueError('it holds a NUL byte, as a UTF-16 file does')
    except ValueError as error:
        raise RuntimeError(f'{listed} is not UTF-8 ({error}), so nothing checks the public side for the strings it '
                           'names; save it as UTF-8') from error
    lines = text.replace('﻿', '').splitlines()
    patterns = [pattern for line in lines if (pattern := DENYLIST_COMMENT.split(line, maxsplit=1)[0].strip())]
    if not patterns:
        raise RuntimeError(f'{listed} lists no strings, so nothing checks the public side; restore its strings')
    found = subprocess.run([*GIT, '-C', str(public), 'grep', '-n', '-I', '-i', '-F', '--untracked', '--full-name',
                            *(part for pattern in patterns for part in ('-e', pattern)), '--'],
                           capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
    if found.returncode not in (0, 1):
        raise RuntimeError(f'git grep failed in {public}: {found.stderr.strip()}')
    hits = [':'.join(line.split(':', 2)[:2]) for line in found.stdout.splitlines()]
    hits += unpublished(public, patterns)
    if message is not None:
        text = message.read_text(encoding='utf-8').casefold()
        if any(pattern.casefold() in text for pattern in patterns):
            hits.append('the public commit message')
    return hits


def unpublished(public, patterns):
    """Where a commit that a push of HEAD would publish holds a listed string, in its files or its message. A push
    sends every commit no branch on a remote holds yet, so a string that a later commit removed is still sent, and
    the working tree no longer shows it. A checkout with no remote has every commit read."""
    def run(*arguments, allowed=(0,)):
        done = subprocess.run([*GIT, '-C', str(public), *arguments], capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=300)
        if done.returncode not in allowed:
            raise RuntimeError(f'git {arguments[0]} failed in {public}: {done.stderr.strip()}')
        return done.stdout

    if not run('rev-parse', '--verify', '--quiet', 'HEAD', allowed=(0, 1)).strip():
        return []  # no commit yet, so the working tree is all a push could send
    commits = run('rev-list', 'HEAD', '--not', '--remotes').split()
    if not commits:
        return []
    found = run('grep', '-n', '-I', '-i', '-F', '--full-name',
                *(part for pattern in patterns for part in ('-e', pattern)), *commits, '--', allowed=(0, 1))
    hits = [f'commit {commit[:12]}: {path}:{line}'
            for commit, path, line in (row.split(':', 3)[:3] for row in found.splitlines())]
    for commit in commits:
        text = run('show', '-s', '--format=%B', commit).casefold()
        if any(pattern.casefold() in text for pattern in patterns):
            hits.append(f'commit {commit[:12]}: its message')
    return hits


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--public', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--private', type=Path, required=True)
    parser.add_argument('--message', type=Path, help='a file that holds the commit message or pull request text')
    args = parser.parse_args()
    try:
        hits = leaks(args.public.resolve(), args.private.resolve(), args.message)
    except (RuntimeError, OSError) as error:
        print(f'Leak check stopped: {error}', file=sys.stderr)
        return 3
    for hit in hits:
        print(hit)
    print(f'{len(hits)} places hold a string the private denylist names', file=sys.stderr)
    return 1 if hits else 0


if __name__ == '__main__':
    raise SystemExit(main())
