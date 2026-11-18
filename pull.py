"""Pull reviewed dotfiles releases and reconcile this device. Check-only unless --apply is supplied."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

from mise_tools import install as install_tools, state_folder
from portable import GIT, exclusive_lock, lock_holder, native_cli_environment, run_tree
from sync_ending import run_sync

GIT_TIMEOUT = 180
# Seconds a pull waits for another pull on this machine, whether a person, an agent or the schedule started it.
# A pull that outlasts the wait is named in the stop. A release's own pull passes 0, because finish has waited.
LOCK_WAIT = 300
LOCK_POLL = 2
MAIN = 'refs/remotes/origin/main'


def git(root, *args):
    try:
        result = run_tree([*GIT, '-C', str(root), *args], timeout=GIT_TIMEOUT, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=native_cli_environment())
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f'{root.name}: git {args[0]} did not finish within {GIT_TIMEOUT} seconds') from error
    if result.returncode:
        stderr = result.stderr.decode('utf-8', 'replace').replace('\r\n', '\n').replace('\r', '\n').strip()
        raise RuntimeError(f'{root.name}: git {args[0]} failed: {stderr}')
    try:
        stdout = result.stdout.decode('utf-8')
    except UnicodeDecodeError as error:
        raise RuntimeError(f'{root.name}: git {args[0]} output is not valid UTF-8') from error
    return stdout.replace('\r\n', '\n').replace('\r', '\n').strip()


def fetch(root):
    """Fetch origin, a second time when the first try fails. Fetches in two worktrees of one repository that both
    find origin/main moved collide on that ref, and the loser exits 1 with "cannot lock ref": on 12 November 2026
    each of 40 rounds of three such fetches lost one or two. The ref then stands where the winner put it, so the
    second try has nothing left to change. A release finish fetches while another's pull does, since it runs its
    checks and tests without the machine's release lock. A fetch that ran out of time is not tried again."""
    try:
        git(root, 'fetch', '--prune', 'origin')
    except RuntimeError as error:
        if 'did not finish within' in str(error):
            raise
        git(root, 'fetch', '--prune', 'origin')


def update(public, private):
    """Fast-forward both checkouts to origin/main. A pull trusts what GitHub holds on main, a pull request merged
    there included, which the user chose on 15 November 2026 over a signature pinned on each device.

    The private checkout is fetched first. A change's public half is merged before its private half, so the public
    main fetched afterwards holds what the private main fetched before it needs. In the other order, a change
    merged between the two fetches gave a pull the old public half with the new private one.

    Each checkout must be on main, and what is installed is origin/main itself, not the branch's upstream, which
    can be a branch whose pull request is not merged."""
    roots = [public, *([private] if private else [])]
    heads = {}
    for root in roots:
        if git(root, 'status', '--porcelain', '--untracked-files=normal'):
            raise RuntimeError(f'{root}: local changes; pull left all checkouts untouched')
        branch = git(root, 'branch', '--show-current')
        if branch != 'main':
            raise RuntimeError(f'{root}: is on {branch or "no branch"}, and a pull installs main; run `git switch main` '
                               'there; pull left all checkouts untouched')
        heads[root] = git(root, 'rev-parse', 'HEAD')
    for root in reversed(roots):
        fetch(root)
    selected = {root: git(root, 'rev-parse', MAIN) for root in roots}
    for root in roots:
        result = subprocess.run([*GIT, '-C', str(root), 'merge-base', '--is-ancestor', heads[root], selected[root]],
                                capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError(f'{root}: selected release would discard local commits; no checkout was advanced')
    for root in roots:
        if git(root, 'rev-parse', 'HEAD') != heads[root] or git(root, 'status', '--porcelain', '--untracked-files=normal'):
            raise RuntimeError(f'{root}: changed during pull; no device files changed')
        git(root, 'merge', '--ff-only', selected[root])


def clean(public, private):
    for root in (public, *([private] if private else [])):
        if git(root, 'status', '--porcelain', '--untracked-files=normal'):
            raise RuntimeError(f'{root}: local changes; a release pull requires clean checkouts')


def home_files(private, state, apply):
    if private is None or not (sys.platform.startswith('linux') or sys.platform == 'win32'):
        return False
    if sys.platform == 'win32':
        from windows_hooks import deliver
    else:
        from home_files import deliver
    try:
        delivery = deliver(private, Path.home(), state, apply=apply)
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        print(f'Home files not delivered: {error}', file=sys.stderr, flush=True)
        return True
    for line in delivery.lines(apply):
        print(line, file=sys.stderr if line.startswith('Home file left') else sys.stdout, flush=True)
    return bool(delivery.left) or (not apply and bool(delivery.written))


def seconds(text):
    value = float(text)
    if not 0 <= value < float('inf'):
        raise argparse.ArgumentTypeError('give a number of seconds, zero or more')
    return value


def pull_lock(path, patience):
    """This pull's hold on the machine's pull lock, after waiting up to patience seconds for the pull that holds it,
    or a stop that names that pull. Without the wait, a pull started while another runs stopped with the operating
    system's "Resource temporarily unavailable", which names neither the lock nor its holder."""
    stack = contextlib.ExitStack()
    deadline, shown = time.monotonic() + patience, None
    while True:
        try:
            stack.enter_context(exclusive_lock(path, record=True))
            return stack
        except BlockingIOError as error:
            holder = lock_holder(path)
            who = (f'PID {holder["pid"]} since {str(holder.get("since", ""))[11:16]}: {str(holder.get("command", ""))[:160]}'
                   if holder else f'the run that holds {path}')
            if time.monotonic() >= deadline:
                raise RuntimeError(f'another pull still runs on this machine after {patience:g} seconds ({who}); '
                                   'pull again once it has ended') from error
            if who != shown:
                print(f'Waiting for another pull: {who}', file=sys.stderr, flush=True)
                shown = who
            time.sleep(LOCK_POLL)


def run(args):
    root = args.root.resolve()
    private = args.private.resolve() if args.private else None
    state = state_folder(Path.home())
    with pull_lock(state / 'pull.lock', args.wait):
        if args.update:
            update(root, private)
        clean(root, private)
        if args.apply:
            # A sync that wrote the mise config and stopped before its tools installed leaves Codex's launcher asking
            # mise for a Node that is not there, and every later sync then stops while reading Codex.
            try:
                install_tools(Path.home(), state)
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                print(f'Dotfiles pull: the tools the mise config pins did not install before the sync: {error}',
                      file=sys.stderr, flush=True)
        status = run_sync(root, private, state=state, apply=args.apply, notes=args.notes, timeout=1200)
        # 1 after --apply: sync wrote what it could and nothing blocked it, but some managed changes did not take.
        # The pair is not applied, so it gets no receipt, but the mise config was written, so its tools still install.
        # Before the tools, so that a failed install holds back no home file. It stops nothing itself, so the
        # receipt and the tools follow whatever it finds.
        left = home_files(private, state, args.apply) if status in (0, 1) else False
        if status == 1 and args.apply:
            print('Dotfiles pull: managed changes remain after applying, listed above under drift; the pair is not '
                  'recorded as applied, and the next pull retries them', file=sys.stderr)
            install_tools(Path.home(), state)
        if status == 0 and args.apply:
            from sync_files import Files, contents
            receipt = state / 'last-applied.json'
            commits = {'public': git(root, 'rev-parse', 'HEAD'),
                       'private': git(private, 'rev-parse', 'HEAD') if private else None}
            before = contents(receipt)
            previous = json.loads(before) if before else {}
            if any(previous.get(key) != value for key, value in commits.items()):
                data = {'at': datetime.now(timezone.utc).isoformat(), **commits}
                Files(state / 'receipt-backups').write(receipt, (json.dumps(data, indent=2) + '\n').encode(), expected=before)
            install_tools(Path.home(), state)
            import old_builds
            from portable import claude_home
            for build in old_builds.sweep(claude_home(Path.home()), state / 'builds-out-of-use.json', time.time(),
                                          old_builds.started()):
                print(f'Dotfiles pull: removed the old plugin build {build}', file=sys.stderr)
        # 1 from a pair that is applied and recorded says that a home file still differs from it.
        return status or int(left)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--private', type=Path)
    parser.add_argument('--update', action='store_true', help='fetch and fast-forward clean checkouts')
    parser.add_argument('--apply', action='store_true', help='apply the verified device plan')
    parser.add_argument('--notes', action='store_true', help="list each of sync's notes in full instead of counting them")
    parser.add_argument('--wait', type=seconds, default=LOCK_WAIT,
                        help='seconds to wait for another pull on this machine before stopping')
    args = parser.parse_args()
    try:
        return run(args)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'Dotfiles pull stopped: {error}', file=sys.stderr)
        return 3
    except Exception as error:
        traceback.print_exc()
        print(f'Dotfiles pull stopped: pull.py crashed with {type(error).__name__}: {error}; the traceback above says '
              'where', file=sys.stderr)
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
