"""Say where this public checkout holds a string that the private denylist names: in its working tree, in a commit
that no branch on a remote holds yet, or in such a commit's message. Run it before a branch of this repository is
pushed, because a pushed branch is public at once and GitHub offers no check that reads a private list.

    python -B leaks.py --private ../dotfiles-private [--message FILE]

The native pre-push hook passes --pre-push and Git's ref updates on stdin. This mode checks the commits and tag
messages the push sends, without treating another remote's refs as already public.

Prints each hit as path:line, or as `commit <id>:` and the place, without the string, and exits 1 when there is
one. A hit in a commit stays until that commit is rewritten, since removing the string in a later commit still
pushes the earlier one."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys

from portable import GIT

DENYLIST = 'config/privacy/public-denylist.txt'
DENYLIST_COMMENT = re.compile(r'(?:^|\s)#')
OBJECT_ID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')


@dataclass
class Push:
    tips: list[str]
    published: list[str]


def leaks(public, private, message=None, pushed=None):
    """Where the public tree, as a commit would take it, or its commit message holds a string the private
    denylist names. One string per line, matched without regard to case; # starts a comment, at the start of a line
    or after white space."""
    if pushed is not None and not pushed.tips:
        return []
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
    hits = []
    if pushed is None:
        found = subprocess.run([*GIT, '-C', str(public), 'grep', '-n', '-I', '-i', '-F', '--untracked', '--full-name',
                                *(part for pattern in patterns for part in ('-e', pattern)), '--'],
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
        if found.returncode not in (0, 1):
            raise RuntimeError(f'git grep failed in {public}: {found.stderr.strip()}')
        hits = [':'.join(line.split(':', 2)[:2]) for line in found.stdout.splitlines()]
    hits += unpublished(public, patterns, pushed)
    if message is not None:
        text = message.read_text(encoding='utf-8').casefold()
        if any(pattern.casefold() in text for pattern in patterns):
            hits.append('the public commit message')
    return hits


def unpublished(public, patterns, pushed=None):
    """Check unpublished HEAD history, or the tips and known destination history from pre-push input. Removed
    strings remain in an earlier commit. Other remotes exempt no commit in pre-push mode."""
    def run(*arguments, allowed=(0,)):
        done = subprocess.run([*GIT, '-C', str(public), *arguments], capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=300)
        if done.returncode not in allowed:
            raise RuntimeError(f'git {arguments[0]} failed in {public}: {done.stderr.strip()}')
        return done.stdout

    hits = []
    if pushed is None:
        if not run('rev-parse', '--verify', '--quiet', 'HEAD', allowed=(0, 1)).strip():
            return []  # no commit yet, so the working tree is all a push could send
        commits = run('rev-list', 'HEAD', '--not', '--remotes').split()
    else:
        tips = pushed.tips
        excluded = [remote for remote in pushed.published if
                    run('rev-parse', '--verify', '--quiet', f'{remote}^{{object}}', allowed=(0, 1, 128)).strip()]
        for tip in tips:
            while run('cat-file', '-t', tip).strip() == 'tag':
                text = run('cat-file', 'tag', tip)
                if any(pattern.casefold() in text.casefold() for pattern in patterns):
                    hits.append(f'object {tip[:12]}: its tag message')
                tip = text.splitlines()[0].removeprefix('object ')
            kind = run('cat-file', '-t', tip).strip()
            if kind == 'blob':
                text = run('cat-file', 'blob', tip).casefold()
                if any(pattern.casefold() in text for pattern in patterns):
                    hits.append(f'object {tip[:12]}: its content')
            elif kind == 'tree':
                found = run('grep', '-n', '-I', '-i', '-F', '--full-name',
                            *(part for pattern in patterns for part in ('-e', pattern)), tip, '--', allowed=(0, 1))
                hits += [f'object {tip[:12]}: {path}:{line}'
                         for object_id, path, line in (row.split(':', 3)[:3] for row in found.splitlines())]
        commits = run('rev-list', *tips, '--not', *excluded).split()
    if not commits:
        return hits
    found = run('grep', '-n', '-I', '-i', '-F', '--full-name',
                *(part for pattern in patterns for part in ('-e', pattern)), *commits, '--', allowed=(0, 1))
    hits += [f'commit {commit[:12]}: {path}:{line}'
             for commit, path, line in (row.split(':', 3)[:3] for row in found.splitlines())]
    for commit in commits:
        text = run('show', '-s', '--format=%B', commit).casefold()
        if any(pattern.casefold() in text for pattern in patterns):
            hits.append(f'commit {commit[:12]}: its message')
    return hits


def push_updates(public, stream, destination):
    tips, published = [], []
    for number, line in enumerate(stream, 1):
        fields = line.split()
        if len(fields) != 4 or any(not OBJECT_ID.fullmatch(fields[at]) for at in (1, 3)):
            raise RuntimeError(f'Malformed pre-push input at line {number}')
        if int(fields[1], 16):
            tips.append(fields[1])
            if int(fields[3], 16):
                published.append(fields[3])
    if tips:
        found = subprocess.run([*GIT, '-C', str(public), 'ls-remote', '--refs', '--', destination],
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
        if found.returncode:
            raise RuntimeError('Cannot read the push destination refs, so its published history is unknown')
        for line in found.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2 or not OBJECT_ID.fullmatch(fields[0]):
                raise RuntimeError('Malformed push destination refs')
            published.append(fields[0])
    return Push(list(dict.fromkeys(tips)), list(dict.fromkeys(published)))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--public', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--private', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--message', type=Path, help='a file that holds the commit message or pull request text')
    mode.add_argument('--pre-push', metavar='DESTINATION', help="read Git's pre-push ref updates from stdin")
    args = parser.parse_args()
    try:
        pushed = push_updates(args.public, sys.stdin, args.pre_push) if args.pre_push else None
        hits = leaks(args.public.resolve(), args.private.resolve(), args.message, pushed)
    except subprocess.TimeoutExpired:
        print('Leak check stopped: Git exceeded the scan time limit', file=sys.stderr)
        return 3
    except (RuntimeError, OSError) as error:
        print(f'Leak check stopped: {error}', file=sys.stderr)
        return 3
    for hit in hits:
        print(hit)
    print(f'{len(hits)} places hold a string the private denylist names', file=sys.stderr)
    return 1 if hits else 0


if __name__ == '__main__':
    raise SystemExit(main())
