#!/usr/bin/env python3
"""Record which agent task holds something no file lock covers, such as an app's window or a machine's agent config.

    python -B claims.py take windows-desktop --who "THREAD TITLE OR ID" --note "restart the app and check it"
    python -B claims.py release windows-desktop --who "THREAD TITLE OR ID"
    python -B claims.py status

A pull and a sync each hold a file lock for as long as their process runs, a release finish holds one from its
commit to its pull, and `release.py status` names them. Work that spans many commands has no one process to hold a lock: driving a desktop app, a run of
writes to a machine's agent config, or pulls on another machine started from this one. A task records a claim on a
name for that work instead, and other tasks read it here rather than asking each thread.

`take` exits 3 and names the holder while another task's claim runs, and renews a claim the same --who holds.
A claim ends when it is released or after --hours, two by default, so a task that died blocks nobody for long.
A claim is advice between tasks: it stops no pull and no program. Claims are files under
~/.local/state/dotfiles/claims on the machine where the tasks run, so a claim on another machine's resource is
kept where its agents' threads run. `release.py status` lists the claims that are running.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys

from portable import exclusive_lock

CLAIMS = Path.home() / '.local/state/dotfiles/claims'
NAME = re.compile(r'[a-z0-9][a-z0-9-]{1,48}')
MAX_HOURS = 12


def now():
    return datetime.now(timezone.utc)


def read(path):
    """A claim file's record while its claim runs, or None."""
    try:
        claim = json.loads(path.read_text(encoding='utf-8'))
        return claim if datetime.fromisoformat(claim['until']) > now() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def running(folder=None):
    """The claims that run now, by name."""
    folder = Path(folder or CLAIMS)
    return {path.stem: claim for path in sorted(folder.glob('*.json')) if (claim := read(path))}


def lines(folder=None):
    return [f'{name} by {claim["who"]} until {datetime.fromisoformat(claim["until"]).astimezone():%H:%M}'
            + (f': {claim["note"]}' if claim.get('note') else '') for name, claim in running(folder).items()]


def take(folder, name, who, note, hours):
    folder.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(folder / '.lock'):
        path = folder / f'{name}.json'
        held = read(path)
        if held and held['who'] != who:
            return held
        claim = {'who': who, 'note': note or (held or {}).get('note', ''),
                 'since': (held or {}).get('since') or now().isoformat(timespec='seconds'),
                 'until': (now() + timedelta(hours=hours)).isoformat(timespec='seconds')}
        temporary = path.with_name(f'.{path.name}.{os.getpid()}')
        temporary.write_text(json.dumps(claim, indent=1) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    return None


def release(folder, name, who):
    """None once the claim is gone, or the record of another task's running claim."""
    if not folder.is_dir():
        return None
    with exclusive_lock(folder / '.lock'):
        path = folder / f'{name}.json'
        held = read(path)
        if held and held['who'] != who:
            return held
        path.unlink(missing_ok=True)
    return None


def main(argv=None, folder=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('take', 'release', 'status'))
    parser.add_argument('name', nargs='?', help='what is claimed, such as windows-desktop or windows-config')
    parser.add_argument('--who', help='the task that claims it: a thread title or ID another agent can look up')
    parser.add_argument('--note', default='', help='what the task is doing, for whoever reads the claim')
    parser.add_argument('--hours', type=float, default=2, help=f'take: how long the claim runs, up to {MAX_HOURS}')
    args = parser.parse_args(argv)
    folder = Path(folder or CLAIMS)
    if args.action == 'status':
        held = lines(folder)
        print('\n'.join(held) if held else 'No claims.')
        return 0
    if not args.name or not NAME.fullmatch(args.name):
        parser.error('NAME takes lower-case letters, digits and hyphens')
    if not args.who:
        parser.error(f'{args.action} needs --who')
    if not 0 < args.hours <= MAX_HOURS:
        parser.error(f'--hours takes a number up to {MAX_HOURS}')
    try:
        other = (take(folder, args.name, args.who, args.note, args.hours) if args.action == 'take'
                 else release(folder, args.name, args.who))
    except BlockingIOError:
        print('Another claims command is running; run this one again', file=sys.stderr)
        return 3
    if other:
        print(f'{args.name} is claimed by {other["who"]} until '
              f'{datetime.fromisoformat(other["until"]).astimezone():%H:%M}'
              + (f': {other["note"]}' if other.get('note') else '') + '. Agree with that task, or wait.', file=sys.stderr)
        return 3
    print(f'{args.name}: ' + ('claimed' if args.action == 'take' else 'released'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
