"""Pull reviewed dotfiles releases and reconcile this device. Check-only unless --apply is supplied."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tomllib
import traceback

from portable import GIT, WINDOWS, exclusive_lock, lock_holder, native_cli_environment, run_tree
from sync_ending import run_sync

GIT_TIMEOUT = 180
# Seconds a pull waits for another pull on this machine, whether a person, an agent or the schedule started it.
# A pull that outlasts the wait is named in the stop. A release's own pull passes 0, because finish has waited.
LOCK_WAIT = 300
LOCK_POLL = 2


def git(root, *args):
    try:
        result = run_tree([*GIT, '-C', str(root), *args], timeout=GIT_TIMEOUT, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding='utf-8', env=native_cli_environment())
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f'{root.name}: git {args[0]} did not finish within {GIT_TIMEOUT} seconds') from error
    if result.returncode:
        raise RuntimeError(f'{root.name}: git {args[0]} failed: {result.stderr.strip()}')
    return result.stdout.strip()


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


def signers():
    """Release signers are pinned per device, so a pushed commit cannot vouch for itself."""
    return Path.home() / '.config/dotfiles/allowed_signers'


def unsigned(root, old, new, allowed):
    """Return the first commit in old..new without an SSH signature from an allowed signer."""
    verifier = []
    if WINDOWS:
        # Windows OpenSSH can hang on Git's MSYS stdin pipe; verification needs no signing agent.
        executable = Path(git(root, '--exec-path')).parents[2] / 'usr/bin/ssh-keygen.exe'
        if not executable.is_file():
            raise RuntimeError(f'{executable}: Git bundled SSH signature verifier is missing')
        verifier = ['-c', f'gpg.ssh.program={executable.as_posix()}']
    for commit in git(root, 'rev-list', f'{old}..{new}').split():
        header = git(root, 'cat-file', 'commit', commit).split('\n\n', 1)[0]
        status = git(root, *verifier, '-c', f'gpg.ssh.allowedSignersFile={allowed}', 'show', '-s', '--format=%G?', commit)
        if '\ngpgsig -----BEGIN SSH SIGNATURE-----' not in header or status != 'G':
            return commit
    return None


def update(public, private, allowed):
    if not allowed.is_file():
        raise RuntimeError(f'{allowed}: pin trusted release signers before pulling; no checkout was advanced')
    roots = [public, *([private] if private else [])]
    heads = {}
    for root in roots:
        if git(root, 'status', '--porcelain', '--untracked-files=normal'):
            raise RuntimeError(f'{root}: local changes; pull left all checkouts untouched')
        git(root, 'rev-parse', '--abbrev-ref', '@{upstream}')
        heads[root] = git(root, 'rev-parse', 'HEAD')
    for root in roots:
        fetch(root)
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
        if commit := unsigned(root, heads[root], selected[root], allowed):
            raise RuntimeError(f'{root}: commit {commit} is not signed by a key in {allowed}; no checkout was advanced')
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


def install_tools(home, state):
    """A release writes tool versions and the npm PATH entry into the mise config, and mise stops on a pinned
    version that isn't installed. Install once per config contents, and again on the next pull after a failed
    install."""
    config = home / '.config/mise/config.toml'
    if not config.is_file():
        return
    record = state / 'mise-installed.sha256'
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    if record.is_file() and record.read_text(encoding='utf-8').strip() == digest:
        return
    mise = shutil.which('mise')
    if mise is None:
        raise RuntimeError(f'{config} pins tools, but mise is not on PATH to install them')
    # mise also reads version files in the folder it runs in, so it runs in the home folder, not the pull's checkout.
    result = subprocess.run([mise, '--cd', str(home), 'install', '--yes'], timeout=900)
    if result.returncode:
        raise RuntimeError(f'mise install exited {result.returncode}; device files were applied, and the next pull retries it')
    record.write_text(digest + '\n', encoding='utf-8')


def home_files(private, state, apply):
    """Deliver the private checkout's home files on Linux, the platform of the machines that use them, and name
    each one written and each one left as it is. Whether any still differs from the release: one left as it is,
    or without --apply one waiting to be delivered."""
    if private is None or not sys.platform.startswith('linux'):
        return False
    from home_files import deliver
    try:
        delivery = deliver(private, Path.home(), state, apply=apply)
    except (RuntimeError, OSError) as error:
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
    state = Path.home() / '.local/state/dotfiles'
    with pull_lock(state / 'pull.lock', args.wait):
        if args.update:
            update(root, private, signers())
        paired(root, private)
        status = run_sync(root, private, apply=args.apply, notes=args.notes, timeout=1200)
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
        # 1 from a pair that is applied and recorded says that a home file still differs from it.
        return status or int(left)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--private', type=Path)
    parser.add_argument('--update', action='store_true', help='fetch and fast-forward clean paired checkouts')
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
