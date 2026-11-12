"""Release reviewed changes as a signed public/private pair, push both, and pull them on this device.

Run it with the engine's own Python from the canonical public checkout. `start NAME` makes a worktree of
each repository at origin/main on branch NAME. Edit there. `finish NAME` locks and builds the named
entries, stops on a changed plugin whose Codex hooks are trusted until --hooks-reapproved, runs the public
test suite unless the release changes no code, runs the private folders' tests, takes this machine's release
lock once no other release, pull or sync runs here, stops on new files the .gitignore allowlist would leave out,
commits both worktrees, stops if the commits hold other files than the tests ran on, stops if
origin/main changed the same files meanwhile, rebases them onto it, pairs the private release.toml with the
public commit, checks that this device's pull could apply the pair, naming each Claude plugin cache that
running sessions hold, checks both pushes with --dry-run, pushes, and pulls, after any other pull here ends.
Before all of that it stops on changed code that `--reviewed FILE` does not show another agent reviewed, for which
`review NAME` prints the files, the command that shows their changes and the lines the reviewer writes, and checks
that again before the push.
It also stops first on a package whose selection changed without a new packaging number, and on an owned
source edited without `--entry NAME --refresh-owned`, which every pull would refuse as a stale lock.
Several finishes run their checks and tests at once on a machine. Only one at a time holds its release lock, from
the commit to the pull, and another waits for it up to --timeout seconds, then stops and names it. One finish
runs per release; a second stops and names the first. A finish notes
the worktrees whose tests passed in build/tests-passed, and a later finish of the same files does not run the
tests again. Each run of the tests moves the test logs of the run before aside, named for the time they were
written, and `finish --detach` does the same with the log of the attempt before.
`clean NAME` removes the worktrees and branches once origin/main has them. `status` lists what runs here and each open
release worktree with the files it changes and the T3 Code thread whose `start` made it, and `start` lists the others. `wait --timeout SECONDS` returns
once no release holds that lock and no pull or sync runs on this machine, so it does not wait for a finish that
is in its checks or tests; `status` names each such finish under its release. `finish NAME --detach` runs the finish as a systemd user unit,
which a restart of the agent's host leaves running. A finish that starts while a pull or sync runs here waits
for it and starts again, and so does one that finds, before it takes the lock, that a pull changed the engine
while it ran its checks and tests or waited, so it runs the engine as it is now.
`result NAME --timeout SECONDS` waits for that unit, prints the end of its log and exits with its status, or with
70 when the finish ended on an uncaught error or its unit is gone without a status.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import itertools
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

import candidate_result
import claims
from portable import GIT, codex_home, exclusive_lock, lock_holder, native_cli_environment
from pull import fetch, git

ENGINE = Path(__file__).resolve().parent
STATE = Path.home() / '.local/state/dotfiles'
WAIT_FOR_PULL = 1200  # seconds a finish waits for another pull on this machine before its own
PUBLIC_COMMIT = re.compile(r'^public_commit = ".*"$', re.MULTILINE)
COMMIT_ID = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?')
# Push over HTTPS with the GitHub CLI's login, which the Mac and WSL hold, whatever helper or SSH origin the
# checkout configures. The private checkout's SSH origin uses a read-only deploy key.
GITHUB_SSH = re.compile(r'^(?:git@github\.com:|ssh://git@github\.com/)')
CREDENTIALS = ('-c', 'credential.https://github.com.helper=',
               '-c', 'credential.https://github.com.helper=!gh auth git-credential')
ATTEMPTS = 3
RESULT_LINES = 25  # lines of a detached finish's log that result prints
CRASHED = 70  # status of a detached finish that ended on an uncaught error, or without writing a status
MOVED = re.compile(r'\((?:fetch first|non-fast-forward)\)')
# Files every release rewrites, so both sides changing them says nothing about clashing work.
PAIRING_FILES = frozenset({'release.toml', 'setup/lock.toml'})
LOCK = 'setup/lock.toml'
# What tested_state gives for a worktree without work of its own.
BASE = 'origin/main'
# The value of a tree key in a setup entry, which pins its source's tree. The rest of its line is the entry's own.
TREE_VALUE = re.compile(rb'^([ \t]*tree[ \t]*=[ \t]*)"[^"\r\n]*"', re.MULTILINE)
SETUP = 'setup/setup.toml'
# The public .gitignore is an allowlist: this pattern drops every path that a later ! line does not bring back.
ALLOWLIST_BASE = '/*'
# Strings the public tree must never hold, such as host and account names. The list is private, because a public
# list would publish them.
DENYLIST = 'config/privacy/public-denylist.txt'
# What finish wants reviewed: a changed file with one of these suffixes or names, one in these folders, or one
# that starts with #!.
CODE_SUFFIXES = ('.py', '.ps1', '.psm1', '.sh', '.bash', '.zsh', '.cmd', '.bat', '.js', '.mjs', '.cjs', '.ts',
                 '.service', '.timer', '.plist')
CODE_NAMES = ('Dockerfile', 'Makefile')
CODE_FOLDERS = ('.github/workflows/',)
# Stored copies of loose skills, each folder named by the hash of its bytes, so an edit there breaks the name.
# finish asks for no review of them; the standards file tells the reviewer to read a newly stored copy.
STORED_COPIES = ('setup/snapshots/',)
STANDARDS = 'docs/agents/coding-standards.md'


def machine_locks():
    """The locks a release finish, a pull and a sync hold on this machine."""
    return {'release': STATE / 'release.lock', 'pull': STATE / 'pull.lock', 'sync': codex_home() / 'dotfiles/sync.lock'}


def running(names=None):
    """The runs that hold this machine's locks now, each as a line naming its process and command."""
    return [f'{name} PID {holder["pid"]} since {str(holder.get("since", ""))[11:16]}: {str(holder.get("command", ""))[:160]}'
            for name, path in machine_locks().items() if (names is None or name in names) and (holder := lock_holder(path))]


def wait(names, timeout):
    """Block until no run holds the named locks: True once they are free, False after timeout seconds."""
    deadline, shown = time.monotonic() + timeout, None
    while busy := running(names):
        if busy != shown:
            print('Waiting for ' + '; '.join(busy), file=sys.stderr, flush=True)
            shown = busy
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)
    return True


class Restart(Exception):
    """A finish asks to be started again from the engine's files as they are now. Its text says why."""


def head(repo):
    """The commit a checkout stands on. A pull changes the engine only by moving the canonical public checkout to
    another commit, so this tells one engine from the next. A read that fails stops the finish with git's words,
    because two failed reads would compare equal and hide a changed engine."""
    return git(repo, 'rev-parse', 'HEAD')


def hold(path, timeout, stale):
    """This finish's hold on the machine's release lock, taken once no release, pull or sync runs here, or None
    when stale() then says that the engine is no longer the one this process loaded. A finish holds the lock only
    from its commit to its pull, so another one waits for it, up to timeout seconds, where it used to stop.
    running() reads the note a holder writes after it has the lock, so two waiting finishes can both see the lock
    free. The one that then finds it taken waits again."""
    began, stack = time.monotonic(), contextlib.ExitStack()
    deadline = began + timeout
    while True:
        if wait(None, deadline - time.monotonic()):
            if stale():
                # The process that starts again counts its own wait from zero, so this part is said here.
                print(f'Waited {time.monotonic() - began:.0f} seconds for the release lock before the engine changed',
                      flush=True)
                return None
            try:
                stack.enter_context(exclusive_lock(path, record=True))
            except BlockingIOError:
                pass
            else:
                taken = time.monotonic()
                # In the finish's log, so that the length of a hold need not be rebuilt from the journal.
                stack.callback(lambda: print(f'Held the release lock for {time.monotonic() - taken:.0f} seconds, after '
                                             f'waiting {taken - began:.0f} seconds for it', flush=True))
                return stack
        if time.monotonic() >= deadline:
            who = '; '.join(running()) or f'another run holds {path}'
            raise RuntimeError(f'another release, pull or sync still runs here after {timeout:g} seconds ({who}); '
                               'finish again, which does not run the tests that passed a second time; this run '
                               'committed nothing')
        time.sleep(1)


def finish_lock(base):
    """The lock a finish holds on its own release for as long as it runs."""
    return base / 'build/finish.lock'


def one_finish(args):
    """This finish's hold on its own release, or a stop that names the finish already running. The machine's lock
    kept two finishes of one release apart while a finish held it throughout. Now both would lock, build and test
    in the same worktree at once. A release that has no folder gets no lock: finish itself says what is missing."""
    base, stack = places(args)[0], contextlib.ExitStack()
    if not base.is_dir():
        return stack
    try:
        stack.enter_context(exclusive_lock(finish_lock(base), record=True))
    except BlockingIOError as error:
        holder = lock_holder(finish_lock(base))
        who = f'PID {holder["pid"]} since {str(holder.get("since", ""))[11:16]}' if holder else 'no process it names'
        raise RuntimeError(f'another finish of {args.name} runs ({who}); wait for it to end, with release.py result '
                           f'{args.name} --timeout 3600 when it is detached; this run did nothing') from error
    return stack


def changed_files(tree):
    """Without --no-renames a file renamed from a.py to a.txt would list only a.txt, and without -z git quotes a name
    that is not plain ASCII."""
    fork = git(tree, 'merge-base', 'HEAD', 'origin/main')
    listings = (git(tree, 'diff', '--name-only', '--no-renames', '-z', fork),
                git(tree, 'ls-files', '--others', '--exclude-standard', '-z'))
    return sorted({name for listing in listings for name in listing.split('\0') if name})


def summary(tree):
    """How a release worktree stands against origin/main: its own commits, its uncommitted files, and the files
    it changes."""
    commits = int(git(tree, 'rev-list', '--count', 'origin/main..HEAD'))
    uncommitted = len(git(tree, 'status', '--porcelain').splitlines())
    if not commits and not uncommitted:
        return 'on origin/main and clean'
    files = changed_files(tree)
    counts = [f'{commits} commit{"s" * (commits != 1)} not on origin/main' if commits else '',
              f'{uncommitted} uncommitted' if uncommitted else '']
    shown = ', '.join(files[:8]) + (f' and {len(files) - 8} more' if len(files) > 8 else '')
    return ', '.join(part for part in counts if part) + (f': {shown}' if shown else '')


def releases(args, public_repo, private_repo):
    """Each release worktree under --worktrees as (name, side, summary), from both repositories' worktree lists."""
    root = args.worktrees.resolve()
    found = []
    for repo in (public_repo, private_repo):
        for line in git(repo, 'worktree', 'list', '--porcelain').splitlines():
            path = Path(line.removeprefix('worktree ')) if line.startswith('worktree ') else None
            if path is None or path.parent.parent != root or path.name not in ('public', 'private'):
                continue
            try:
                found.append((path.parent.name, path.name, summary(path)))
            except RuntimeError as error:
                found.append((path.parent.name, path.name, f'unreadable: {error}'))
    return sorted(found)


def status(args, public_repo, private_repo):
    busy = running()
    print('Running on this machine: ' + ('; '.join(busy) if busy else 'no release, pull or sync'))
    for claim in claims.lines(STATE / 'claims'):
        print('Claimed: ' + claim)
    found = releases(args, public_repo, private_repo)
    print(f'Release worktrees under {args.worktrees}:' if found else f'No release worktrees under {args.worktrees}.')
    for line in release_lines(args.worktrees, found):
        print(line)
    return 0


def release_lines(worktrees, found):
    """The lines that list the release worktrees found, each release followed by the T3 Code thread its start noted,
    so that a session that meets another open release can find the thread that owns it."""
    lines = []
    for name, rows in itertools.groupby(found, key=lambda row: row[0]):
        lines += [f'  {name}/{side}: {text}' for _, side, text in rows]
        if thread := noted_thread(worktrees / name):
            lines.append(f'  {name}: {thread}')
        if holder := lock_holder(finish_lock(worktrees / name)):
            # A finish in its checks and tests holds no lock of the machine's, so the first line leaves it out.
            lines.append(f'  {name}: its finish runs, PID {holder["pid"]} since {str(holder.get("since", ""))[11:16]}')
    return lines


def thread_note(base):
    """Where start notes the T3 Code thread it ran in, from T3CODE_THREAD_ID, beside the release's worktrees."""
    return base / 't3-thread'


def noted_thread(base):
    """The T3 Code thread a release's start noted, as status names it, or None when start ran outside T3 Code."""
    try:
        thread = thread_note(base).read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        return f'T3 thread unreadable: {error}'
    return f'T3 thread {thread}' if thread else None


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
    """Whether the branch has a commit that origin/main lacks. A branch that is only behind, because another
    session pushed since it started, has nothing of its own to release or to pair."""
    return int(git(root, 'rev-list', '--count', 'origin/main..HEAD')) > 0


def dropped(root):
    """New paths the .gitignore allowlist leaves out, which a commit would silently miss. Cache rules ignore
    paths too, but on purpose."""
    ignored = git(root, 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory').splitlines()
    if not ignored:
        return []
    matches = (line.split('\t', 1) for line in git(root, 'check-ignore', '-v', '--', *ignored).splitlines())
    return [path for source, path in matches if source.split(':', 2)[2] == ALLOWLIST_BASE]


def dropped_stop(public, private):
    for tree in (public, private):
        if paths := dropped(tree):
            raise RuntimeError(f'{tree}: .gitignore leaves out {", ".join(paths)}; add a "!" line for each path the '
                               'release needs and delete the rest, then finish again')


def unnamed_commit(args, public, private):
    """Why finish could not commit a worktree that holds changes, or None: it was given no message for it."""
    for tree, message, option in ((public, args.message, '--message'), (private, args.private_message, '--private-message')):
        if dirty(tree) and not message:
            return f'{tree}: pass {option} with the {tree.name} commit message'
    return None


def leaks(public, private, message=None):
    """Where the public tree, as a commit would take it, or its commit message holds a string the private
    denylist names. One string per line, matched without regard to case; # starts a comment."""
    listed = private / DENYLIST
    if not listed.is_file():
        raise RuntimeError(f'{listed} is missing, so nothing checks the public side for the strings it names; '
                           'restore it, then finish again; this run committed nothing')
    patterns = [line.strip() for line in listed.read_text(encoding='utf-8').splitlines()
                if line.strip() and not line.strip().startswith('#')]
    if not patterns:
        raise RuntimeError(f'{listed} lists no strings, so nothing checks the public side; restore its strings, '
                           'then finish again; this run committed nothing')
    found = subprocess.run([*GIT, '-C', str(public), 'grep', '-n', '-I', '-i', '-F', '--untracked', '--full-name',
                            *(part for pattern in patterns for part in ('-e', pattern)), '--'],
                           capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
    if found.returncode not in (0, 1):
        raise RuntimeError(f'git grep failed in {public}: {found.stderr.strip()}')
    hits = [':'.join(line.split(':', 2)[:2]) for line in found.stdout.splitlines()]
    if message is not None:
        text = message.read_text(encoding='utf-8').casefold()
        if any(pattern.casefold() in text for pattern in patterns):
            hits.append('the public commit message')
    return hits


def message_stop(args):
    """Why a commit message file would give a commit the wrong subject or none, or stop the finish later than
    here, or None. Git takes every line up to the first blank one as the subject, and pair() copies the public
    subject into the private pair commit. On 12 November 2026 a file whose body began on line 2 gave both commits
    a 483-character subject."""
    for option, file in (('--message', args.message), ('--private-message', args.private_message),
                         ('--pair-message', args.pair_message)):
        if not file:
            continue
        try:
            raw = file.read_bytes()
        except OSError as error:
            return f'{option} {file} cannot be read: {error}; give the file that holds the message, then finish again'
        if b'\0' in raw:
            return (f'{option} {file} holds a NUL byte, which git refuses in a commit message, after the tests; a '
                    "file that Windows PowerShell's > wrote is UTF-16, which holds NUL bytes; save it as UTF-8, then "
                    'finish again')
        if raw.startswith(b'\xef\xbb\xbf'):
            return (f'{option} {file} starts with a byte-order mark, which git would keep at the start of the '
                    "commit's subject; Windows PowerShell's Out-File -Encoding utf8 writes one; save the file as "
                    'UTF-8 without it, then finish again')
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError as error:
            return (f'{option} {file} is not UTF-8 ({error.reason} at byte {error.start}), so git would read each '
                    'byte that is not UTF-8 as Latin-1, which gives wrong characters for any other encoding; save it '
                    'as UTF-8, then finish again')
        # Lines and blanks as git counts them: it splits at newlines only, and drops the blank lines above the subject.
        lines = [line.strip(' \t\r') for line in text.split('\n')]
        first = next((number for number, line in enumerate(lines) if line), None)
        if first is None:
            return (f'{option} {file} holds no text, so git would refuse the commit, after the tests; write the '
                    'subject on line 1, then finish again')
        if first + 1 < len(lines) and lines[first + 1]:
            return (f'{option} {file}: line {first + 2} is not blank, so git would take every line up to the first '
                    f'blank one as the commit\'s subject; put the subject by itself on line {first + 1} and leave '
                    f'line {first + 2} blank, with the body below it, then finish again')
    return None


def commit(root, message):
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-F', str(message.resolve()))


def rebase(root, regenerated=frozenset()):
    """Replay this branch onto origin/main, which another session may have moved since start. A conflict only in
    files the caller rebuilds afterwards, as finish relocks setup/lock.toml, takes origin's copy. Returns '' when
    the branch already holds origin/main, 'regenerate' when it took such a copy, and 'rebased' otherwise."""
    fetch(root)
    if subprocess.run([*GIT, '-C', str(root), 'merge-base', '--is-ancestor', 'origin/main', 'HEAD'],
                      capture_output=True, timeout=60).returncode == 0:
        return ''
    # --continue then commits each settled step with its own author and message, minus git's conflict comments.
    environment = {**native_cli_environment(), 'GIT_EDITOR': 'true'}
    result, step = 'rebased', ('rebase', '-q', 'origin/main')
    while subprocess.run([*GIT, '-C', str(root), *step], env=environment, capture_output=True, timeout=600).returncode:
        try:
            taken = take_origin(root, regenerated)
        except RuntimeError:
            taken = False
        if not taken:
            subprocess.run([*GIT, '-C', str(root), 'rebase', '--abort'], capture_output=True, timeout=60)
            raise RuntimeError(f'{root}: the branch conflicts with origin/main; rebase it by hand, then finish again')
        result, step = 'regenerate', ('rebase', '--continue')
    return result


def take_origin(root, regenerated):
    """Settle a stopped rebase step whose conflicts are all in regenerated files by taking origin's copy. False
    when another file conflicts, or when the step would then change nothing."""
    conflicted = sorted(set(git(root, 'diff', '--name-only', '--diff-filter=U').splitlines()))
    if not conflicted or not set(conflicted) <= regenerated:
        return False
    git(root, 'checkout', '--ours', '--', *conflicted)  # during a rebase, ours is origin/main's side
    git(root, 'add', '--', *conflicted)
    return subprocess.run([*GIT, '-C', str(root), 'diff', '--cached', '--quiet'], timeout=60).returncode != 0


def amend(root):
    """Fold the files a relock rewrote into the branch's own release commit."""
    if not dirty(root):
        return
    if not ahead(root):
        raise RuntimeError(f'{root}: relocking changed files on a branch without its own commit; commit them, then finish again')
    git(root, 'commit', '-q', '-a', '--amend', '--no-edit')


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
        wording = ['-F', str(message.resolve())] if message else ['-m', f'Pair "{subject}"']
        git(private, 'commit', '-q', '-a', *wording)


def push_url(root):
    return GITHUB_SSH.sub('https://github.com/', git(root, 'remote', 'get-url', '--push', 'origin'))


def overlap(root, regenerated=frozenset()):
    """Files origin/main changed since this branch left it that this branch changed too, committed or not. A
    clean rebase can still join two sessions' clashing designs for the same file. A file finish writes anew is
    no design of this branch's."""
    fetch(root)
    fork = git(root, 'merge-base', 'HEAD', 'origin/main')
    theirs = set(git(root, 'diff', '--name-only', '--no-renames', '-z', fork, 'origin/main').split('\0'))
    return sorted((theirs & set(changed_files(root))) - PAIRING_FILES - regenerated)


def pin_only(root, entries):
    """The setup files of the named entries in which this branch changed nothing beyond the tree its source pins:
    no other value, such as a tree key of another table, and no other byte, such as a comment, a line ending or
    the final newline, which origin's copy would lose. A file the branch left as it was counts too, and cannot conflict. Locking with
    --refresh-owned writes that pin anew, so origin's copy of such a file loses nothing."""
    def unpinned_text(description):
        return {**description, 'source': {key: value for key, value in description.get('source', {}).items()
                                          if key != 'tree'}}

    def unpinned_bytes(content):
        return TREE_VALUE.sub(rb'\1""', content)
    fork = git(root, 'merge-base', 'HEAD', 'origin/main')
    found = set()
    for path in (f'setup/{kind}/{entry}.toml' for kind in ('skills', 'packages') for entry in entries):
        before, after = started(root, path), current(root, path)
        if (before and after and unpinned_text(before) == unpinned_text(after)
                and unpinned_bytes(git_bytes(root, 'show', f'{fork}:{path}'))
                == unpinned_bytes((root / path).read_bytes())):
            found.add(path)
    return frozenset(found)


def content(root):
    """The tree a commit of the worktree would hold, as finish would commit it, made without moving the branch or
    its index."""
    if not dirty(root):
        return git(root, 'rev-parse', 'HEAD^{tree}')
    with tempfile.TemporaryDirectory() as folder:
        index = {'GIT_INDEX_FILE': str(Path(folder) / 'index')}
        git_bytes(root, 'read-tree', 'HEAD', environment=index)
        git_bytes(root, 'add', '-A', environment=index)
        return git_bytes(root, 'write-tree', environment=index).decode().strip()


def snapshot(root):
    """A commit of the worktree as finish would commit it, made without moving the branch or its index."""
    if not dirty(root):
        return git(root, 'rev-parse', 'HEAD')
    return git_bytes(root, 'commit-tree', content(root), '-p', 'HEAD', '-m', 'Trial merge').decode().strip()


def tested_state(public, private):
    """What a run of the tests vouches for: each worktree's content as a commit would take it. A worktree without
    work of its own, clean and with no commit origin/main lacks, counts as origin/main wherever that stands,
    because finish moves it there, as it rebases the other one, without testing again."""
    return {label: content(tree) if dirty(tree) or ahead(tree) else BASE
            for label, tree in (('public', public), ('private', private))}


def state_changes(public, private, before, after):
    """Where two states of the worktrees differ, for a stop to name."""
    found = []
    for label, tree in (('public', public), ('private', private)):
        old, new = before[label], after[label]
        if old == new:
            continue
        if BASE in (old, new):
            found.append(f'{label}: ' + ('its work is gone' if new == BASE else 'it holds work where it held none'))
        else:
            names = git(tree, 'diff', '--name-only', '--no-renames', old, new).splitlines()
            found.append(f'{label}: ' + ', '.join(names[:8]) + (f' and {len(names) - 8} more' if len(names) > 8 else ''))
    return '; '.join(found)


def tests_passed(base):
    """Where a finish notes the worktrees whose tests passed, as tested_state gives them."""
    return base / 'build/tests-passed'


def tested(public, private, base):
    """The state of the worktrees whose tests have passed: in this run, or in an earlier run of this release that
    noted the worktrees as they are now. A finish starts again when a pull changed the engine while it ran, and is
    run again after a stop behind the tests, and neither needs the same files tested twice."""
    note, state = tests_passed(base), tested_state(public, private)
    try:
        passed = json.loads(note.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        passed = None
    if passed == state:
        print(f'The tests did not run again: they passed on these files before, as {note} notes. Delete that note '
              'to run them again', flush=True)
    else:
        test_suite(public, private, base / 'build/tests.log')
        after = tested_state(public, private)
        if after != state:
            raise RuntimeError('the worktrees changed while their tests ran ('
                               f'{state_changes(public, private, state, after)}), so the tests vouch for neither '
                               'state; finish again once the edits are done; this run committed nothing')
    # Before the note, and before the wait for the lock. The state leaves out a file the allowlist ignores, while
    # the tests ran with it there: a note written first would let the finish after its deletion skip tests that
    # then fail.
    dropped_stop(public, private)
    note.write_text(json.dumps(state) + '\n', encoding='utf-8')
    return state


def conflicts(root, regenerated=frozenset()):
    """Files in which a merge of origin/main and this branch as it stands would conflict, outside those the
    rebase settles by itself. The rebase would stop on them too, but only after the tests have run."""
    fetch(root)
    merged = subprocess.run([*GIT, '-C', str(root), 'merge-tree', '--write-tree', '--name-only', '--no-messages', '-z',
                             'origin/main', snapshot(root)], capture_output=True, timeout=300,
                            env=native_cli_environment())
    # A merge git could try prints the merged tree's ID first. Without it exit 1 is no conflict: git also exits 1
    # with nothing on stdout when it cannot read a commit.
    tree, *names = merged.stdout.decode(errors='replace').split('\0')
    if merged.returncode not in (0, 1) or not tree.strip():
        raise RuntimeError(f'{root.name}: git merge-tree exited {merged.returncode}: '
                           f'{merged.stderr.decode(errors="replace").strip()}')
    return sorted({name for name in names if name and merged.returncode} - regenerated)


def origin_stop(public, private, regenerated, overlap_reviewed):
    """Why this release cannot go onto origin/main as it stands, or None. Asked before the tests, so that a stop
    costs seconds, and again after the commit, because origin can move while the tests run."""
    clashing = [f'{tree.name}: {", ".join(files)}' for tree in (public, private)
                if (files := conflicts(tree, regenerated[tree]))]
    if clashing:
        return ('the branch conflicts with origin/main (' + '; '.join(clashing) + '). Commit the worktree, rebase '
                'it by hand with git rebase origin/main, then finish again')
    if overlap_reviewed:
        return None
    shared = [f'{tree.name}: {", ".join(files)}' for tree in (public, private)
              if (files := overlap(tree, regenerated[tree]))]
    if shared:
        return ('another session changed the same files on origin/main since this branch started ('
                + '; '.join(shared) + '). Read its commits with git log -p HEAD..origin/main -- FILE, '
                'reconcile the two designs, then finish again with --overlap-reviewed')
    return None


def started(root, path):
    """A TOML file as this branch found it on origin/main, or {} where it was absent."""
    fork = git(root, 'merge-base', 'HEAD', 'origin/main')
    if not git(root, 'ls-tree', fork, path):
        return {}
    return toml(lambda: git(root, 'show', f'{fork}:{path}'), f'{path} as origin/main held it at {fork[:7]}',
                f'; no finish should have let it go there, and git log origin/main -- {path} names the commit that did')


def current(root, path):
    return toml(lambda: (root / path).read_text(encoding='utf-8'), root / path) if (root / path).is_file() else {}


def toml(read, name, then=''):
    """The parsed file, or a stop that names it, where the error itself would end a detached finish as a crash."""
    try:
        return tomllib.loads(read())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise RuntimeError(f'{name} is not TOML ({error}){then}') from error


def reused_release_names(public, private):
    """Packages whose selection changed at the same upstream commit and packaging number. A built release folder is
    named for those two alone, so a build with another selection differs from the folder of that name, and every
    machine's pull stops on it. The device check can't see this, because a candidate builds into its own folder."""
    reused = []
    for root in (public, private):
        for path in sorted((root / 'setup/packages').glob('*.toml')):
            relative = f'setup/packages/{path.name}'
            before, after = started(root, relative).get('source', {}), current(root, relative).get('source', {})
            if (before.get('commit') and before.get('commit') == after.get('commit')
                    and before.get('packaging') == after.get('packaging') and before.get('select') != after.get('select')):
                reused.append(path.stem)
    return reused


def edited_owned(public, private, private_repo):
    """Owned sources whose files differ from the tree their entry pins, as entry name to source path. The device
    check finds the same thing, but only after the suites have run. Empty for a tree without a setup description."""
    if not (public / SETUP).is_file():
        return {}
    from description import DescriptionError, Roots
    from description_toml import load
    from drift import _edited_owned
    from host_state import DeviceRoots
    private_setup = private / 'setup'
    try:
        setup = load(Roots(public / 'setup', private_setup if private_setup.is_dir() else None))
    except DescriptionError as error:
        raise RuntimeError(unloadable({public: ENGINE, private: private_repo}, error)) from error
    return _edited_owned(setup, DeviceRoots.current())


def holds(tree, commit):
    """Whether the tree's HEAD holds the commit, or a stop with git's own words when git cannot tell."""
    result = subprocess.run([*GIT, '-C', str(tree), 'merge-base', '--is-ancestor', commit, 'HEAD'],
                            capture_output=True, text=True, timeout=60)
    if result.returncode not in (0, 1):
        raise RuntimeError(f'{tree}: git merge-base exited {result.returncode}: {result.stderr.strip()}')
    return result.returncode == 0


def unloadable(checkouts, error):
    """Why finish stops on a setup that this engine cannot load, given each worktree's canonical checkout. The
    engine runs from those checkouts, which a pull moves on while a worktree stays where it started: on 12 November
    2026 a finish that had waited for another release ran an engine that had retired a build the worktree's older
    setup still named. finish does not rebase here, because a rebase before the overlap check would hide from it the
    files another session changed meanwhile."""
    problem = f'the setup does not load:\n{error}\n'
    try:
        older = [canonical for tree, canonical in checkouts.items()
                 if not holds(canonical, git(tree, 'merge-base', 'HEAD', 'origin/main'))]
        behind = [tree for tree, canonical in checkouts.items() if not holds(tree, git(canonical, 'rev-parse', 'HEAD'))]
        shared = [f'{tree.name}: {", ".join(files)}' for tree in behind if (files := overlap(tree))]
    except RuntimeError as failure:
        return (f'{problem}finish could not tell whether the worktrees are older than the engine: {failure}\n'
                'Fix the setup or bring the worktrees up to date, then finish again; this run committed nothing')
    if older:
        return (f'{problem}The engine is older than this branch: {", ".join(map(str, older))} lacks commits the '
                f'branch started from. Pull this machine with {sys.executable} -B {ENGINE / "pull.py"} --update '
                '--apply, then finish again; this run committed nothing')
    if not behind:
        return f'{problem}Fix it, then finish again; this run committed nothing'
    first = (('Another session changed the same files on origin/main since this branch started ('
             + '; '.join(shared) + '). Read its commits with git log -p HEAD..origin/main -- FILE and reconcile '
             'the two designs before you rebase, because the rebase hides them from the overlap check. ')
             if shared else '')
    rebases = ' and '.join(f'git -C {tree} rebase origin/main' for tree in behind)
    return (f'{problem}This branch is older than the engine: {", ".join(map(str, behind))} lacks commits the '
            f'engine holds, and the setup may load once the branch has them. {first}Commit the work in each such '
            f'worktree with its release message, unless it is committed, run {rebases}, settle any conflict it '
            'stops on and git rebase --continue, then finish again; this run committed nothing')


def unpinned(edited, entries, refresh_owned):
    """The edited owned sources this finish was not told to pin again."""
    accepted = set(entries) if refresh_owned else set()
    return {name: path for name, path in edited.items() if name not in accepted}


def git_bytes(tree, *args, passing=(0,), environment=None):
    """A git command's output as bytes, or a stop with git's own words when it exits outside passing."""
    done = subprocess.run([*GIT, '-C', str(tree), *args], capture_output=True, timeout=120,
                          env={**native_cli_environment(), **(environment or {})})
    if done.returncode not in passing:
        command = next(arg for arg in args if arg[:1] != '-' and '=' not in arg)
        raise RuntimeError(f'{tree.name}: git {command} exited {done.returncode}: '
                           f'{done.stderr.decode(errors="replace").strip()}')
    return done.stdout


def fork_bytes(tree, fork, name):
    """A file's bytes at the fork point, or None when it was no file there. The name is matched as written, not
    as a pattern, and a folder of that name is no file."""
    for entry in git_bytes(tree, '--literal-pathspecs', 'ls-tree', '-z', fork, '--', name).split(b'\0'):
        fields, _, path = entry.partition(b'\t')
        if path == name.encode() and fields.split()[1:2] == [b'blob']:
            return git_bytes(tree, 'cat-file', 'blob', fields.split()[2].decode())
    return None


def worktree_bytes(path):
    """A file's bytes in the worktree, None when it is no file. A link gives its own text, as git stores a link,
    so that the bytes of whatever it points to stay out of the fingerprint. That text need not be UTF-8."""
    if path.is_symlink():
        return os.fsencode(os.readlink(path))
    return path.read_bytes() if path.is_file() else None


def is_code(name, *contents):
    """Whether a changed file is code, by its name or by a #! at the start of either side. The side at the fork
    point counts, so that an edit which puts a line above a script's #! is still an edit to a script."""
    return (name.endswith(CODE_SUFFIXES) or name.rsplit('/', 1)[-1] in CODE_NAMES or name.startswith(CODE_FOLDERS)
            or any((content or b'')[:2] == b'#!' for content in contents))


def branch_lines(old, new):
    """The lines a branch adds to and removes from one file, given its bytes at the fork point and now, None for
    no file. They carry no position and none of the lines around them, so a rebase over another session's edit
    elsewhere in the file leaves them as they were, and so does committing the file. A change of mode alone
    gives no lines, so the file's name stands for it. Git's marker for a last line without its newline is not
    one of them, so adding or removing that newline on a last line the branch changes anyway leaves them as they
    were. The user's and the machine's git attributes files are left out, because their rules for every file
    would apply to the two temporary files and change the lines."""
    if old == new:
        return []
    with tempfile.TemporaryDirectory() as folder:
        sides = []
        for side, content in (('old', old), ('new', new)):
            if content is not None:
                (Path(folder) / side).write_bytes(content)
            sides.append(os.devnull if content is None else side)
        shown = git_bytes(Path(folder), '-c', f'core.attributesFile={os.devnull}', 'diff', '--no-index',
                          '--no-ext-diff', '--no-textconv', '--no-color', '-U0', '--', *sides, passing=(0, 1),
                          environment={'GIT_ATTR_NOSYSTEM': '1'})
    head, hunk, body = shown.partition(b'\n@@')
    if hunk:
        return [line for line in body.split(b'\n') if line[:1] in (b'+', b'-')]
    # No line diff, as for a file git reads as binary or as larger than core.bigFileThreshold: a hash of each side
    # stands in, so there another session's edit to the same file changes the lines too.
    return [sign + hashlib.sha256(content or b'').hexdigest().encode() for sign, content in ((b'-', old), (b'+', new))]


def code_changes(tree):
    """The code files this tree's branch changes against origin/main, and a fingerprint of the lines it adds to
    and removes from them. Hook commands inside JSON and TOML files are not counted."""
    fork = git(tree, 'merge-base', 'HEAD', 'origin/main')
    names, digest = [], hashlib.sha256()
    for name in changed_files(tree):
        if name.startswith(STORED_COPIES):
            continue
        old, new = fork_bytes(tree, fork, name), worktree_bytes(tree / name)
        if not is_code(name, old, new):
            continue
        names.append(name)
        digest.update(name.encode() + b'\0' + b'\n'.join(branch_lines(old, new)) + b'\0')
    return names, digest.hexdigest()[:16]


def review_lines(public, private):
    """For each tree whose branch changes code, its name with those files and the line a review record must hold.
    Each tree has its own line, so that a release whose public half is already on origin still matches the
    record that covered both."""
    wanted = {}
    for label, tree in (('public', public), ('private', private)):
        names, fingerprint = code_changes(tree)
        if names:
            wanted[label] = (names, f'Reviewed: {label} {fingerprint}')
    return wanted


def review_stop(public, private, record, name):
    """Why a release that changes code may not go out yet, or None once record holds a review of that code. On
    10 November 2026 a launcher change that passed every test went out unreviewed, and a later release had to
    repair what a reviewer would have asked about: what the caller does when its read of a vault fails."""
    wanted = review_lines(public, private)
    held = set()
    if record is not None and record.is_file():
        held = {line.strip() for line in record.read_text(encoding='utf-8-sig', errors='replace').splitlines()}
    missing = [line for _, line in wanted.values() if line not in held]
    if not missing:
        return None
    if record is None:
        problem = 'no --reviewed FILE names a review of it'
    elif not record.is_file():
        problem = f'{record} is not a file'
    else:
        problem = (f'{record} lacks ' + ' and '.join(f'"{line}"' for line in missing) + ' on a line of its own, '
                   'so it reviews other code or holds the line inside other text')
    return (f'this release changes code and {problem}. Run release.py review {name}, which prints the files, the '
            'command that shows their changes and the lines a reviewer writes. Have an agent that did not write the change review it, '
            'then finish again with --reviewed FILE')


def review(args, public_repo, private_repo):
    """Print what the reviewer of a release needs. It changes nothing, so it can run at any point."""
    base, public, private = places(args)
    wanted = review_lines(public, private)
    if not wanted:
        print(f'{args.name} changes no code, so finish asks for no review')
        return 0
    print(f'Review {args.name} against {ENGINE / STANDARDS}')
    for label, tree in (('public', public), ('private', private)):
        if label not in wanted:
            continue
        names, line = wanted[label]
        fork = git(tree, 'merge-base', 'HEAD', 'origin/main')
        print(f'{label}: {tree}\n'
              f'  code this branch changes: {", ".join(names)}\n'
              f'  its changes: git -C {tree} diff {fork[:12]}, and git -C {tree} status --short '
              '--untracked-files=all for the new files, which that diff leaves out\n'
              f'  the line its reviewer writes: {line}')
    return 0


def public_pushed(base):
    """Where a finish notes the public commit it has pushed, until the private half that names it is pushed too."""
    return base / 'build/public-pushed'


def half_published(base, public):
    """Whether an earlier finish of this release pushed its public commit and then failed to push the private
    half. The public branch then no longer differs from origin/main, so only this note says that the private
    release.toml still has to name a public commit that holds this release. Without it a rerun pushed the
    private half naming the public commit before, and pulls stopped on the mismatch. The public HEAD may have
    moved on since, to another session's commit on top of the noted one, and still holds it. A note the HEAD
    does not hold is a stop, because it cannot say whether the public half went out."""
    note = public_pushed(base)
    if not note.is_file():
        return False
    noted = note.read_text(encoding='utf-8', errors='replace').strip()
    if not COMMIT_ID.fullmatch(noted):
        problem = f'it holds {noted[:64]!r}, which is no full commit id'
    else:
        asked = subprocess.run([*GIT, '-C', str(public), 'merge-base', '--is-ancestor', noted, 'HEAD'],
                               capture_output=True, text=True, encoding='utf-8', errors='replace',
                               env=native_cli_environment(), timeout=60)
        if asked.returncode == 0:
            return True
        problem = (f'the branch in {public} does not hold {noted}' if asked.returncode == 1 else
                   f'git could not say whether the branch in {public} holds {noted}: {asked.stderr.strip()}')
    raise RuntimeError(f'{note} should name the public commit that an earlier finish of this release pushed, and '
                       f'{problem}. If that commit is on origin/main, write its full id into the note and bring the '
                       'public branch up to origin/main; if it never reached origin/main, delete the note; then '
                       'finish again. This run pushed nothing')


def push_state(base):
    """What a finish that stops says about origin. The note decides, not the attempt that stops: an earlier
    attempt, or an earlier finish, may have pushed the public commit."""
    if public_pushed(base).is_file():
        return 'the public commit is on origin without its pair; finish again to push the private half that names it'
    return 'nothing was pushed'


def reapprovals(public, private):
    """Plugins with trusted Codex hooks whose installed Codex content this branch may change: their lock entry
    changed, or they apply tool notes and the notes changed, which the lock leaves out. Codex ties a hook's
    approval to the plugin's content, so every machine's pull stops on those hooks until the user approves them
    again."""
    notes = started(public, SETUP).get('tool_note') != current(public, SETUP).get('tool_note')
    changed = []
    for root in (public, private):
        before = started(root, LOCK)
        for name, lock in current(root, LOCK).items():
            if not isinstance(lock, dict) or not lock.get('hooks'):
                continue
            old = before.get(name) or {}
            applies = current(root, f'setup/packages/{name}.toml').get('skills', {}).get('tool_notes') == 'apply'
            if (lock.get('intent'), lock.get('codex')) != (old.get('intent'), old.get('codex')) or (notes and applies):
                changed.append(name)
    return sorted(changed)


def can_change_test_results(name):
    return name.endswith('.py') or name.startswith(('tests/', 'uv.lock', 'pyproject.toml', 'config/mise/',
                                                    '.github/workflows/'))


def keep(log):
    """Move the log of an earlier attempt aside, under the time it was last written, and return where it went, or
    None when there was none. On 12 November 2026 nine detached finishes ended on status 1 within seconds of their
    wait, and each retry had emptied the log that said why."""
    if not log.is_file() or not log.stat().st_size:
        return None
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(log.stat().st_mtime))
    kept, count = log.with_name(f'{log.stem}.{stamp}{log.suffix}'), 1
    while kept.exists():
        count += 1
        kept = log.with_name(f'{log.stem}.{stamp}-{count}{log.suffix}')
    log.replace(kept)
    return kept


def public_suite(public, log):
    cache = public / '.sync'
    cached = cache.exists()
    keep(log)
    with log.open('w', encoding='utf-8') as output:
        result = subprocess.run([sys.executable, '-B', 'tests/run.py'], cwd=public, stdout=output,
                                stderr=subprocess.STDOUT, timeout=1800)
    if result.returncode:
        raise RuntimeError(f'the test suite failed; read {log}')
    ran = next((line for line in reversed(log.read_text(encoding='utf-8').splitlines()) if line.startswith('Ran ')), 'Ran')
    print(f'{ran}: OK; log {log}', flush=True)
    if cache.exists() and not cached:
        raise RuntimeError(f'the test suite wrote into {cache}, which in the canonical checkout is the cache real '
                           'pulls use; give the test that wrote it its own engine folder')


def test_suite(public, private, log):
    log.parent.mkdir(parents=True, exist_ok=True)
    if any(can_change_test_results(name) for tree in (public, private) for name in changed_files(tree)):
        public_suite(public, log)
    else:
        print('The public test suite did not run because the release changes no code', flush=True)
    folders = sorted({str(Path(name).parent) for name in git(private, 'ls-files', '-co', '--exclude-standard').splitlines()
                      if Path(name).name.startswith('test_') and name.endswith('.py')})
    if not folders:
        return
    private_log = log.with_name('private-tests.log')
    absent = log.parent / 'absent-config'
    environment = {**os.environ, 'CODEX_HOME': str(absent / 'codex'), 'CLAUDE_CONFIG_DIR': str(absent / 'claude'),
                   'PYTHONDONTWRITEBYTECODE': '1'}
    total = 0
    keep(private_log)
    with private_log.open('w', encoding='utf-8') as output:
        for folder in folders:
            result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', folder, '-t', folder],
                                    cwd=private, env=environment, capture_output=True, text=True, timeout=1800)
            output.write(f'== {folder}\n{result.stdout}{result.stderr}')
            if result.returncode:
                raise RuntimeError(f'the private tests in {folder} failed; read {private_log}')
            total += next((int(line.split()[1]) for line in reversed(result.stderr.splitlines())
                           if line.startswith('Ran ')), 0)
    print(f'Ran {total} private tests in {len(folders)} folders: OK; log {private_log}', flush=True)


def device_check(public, private, state):
    """Stop on a release that would stop the pull, which every device would then hit. Sync exits 1 when it
    only has changes to apply, as every release does, but so does a crash, which writes no result.
    Name each Claude plugin cache that running sessions hold: the pull swaps it at once when only its skills'
    Markdown text changes, and otherwise applies the rest of the release and replaces it once they exit."""
    result = subprocess.run([sys.executable, '-B', str(public / 'sync.py'), '--candidate', '--json', '--public',
                             str(public / 'setup'), '--private', str(private / 'setup')],
                            cwd=public, capture_output=True, text=True, encoding='utf-8', timeout=1800)
    stopped = f'sync exited {result.returncode}'
    try:
        candidate = candidate_result.read(result.stdout)
    except ValueError as error:
        candidate = None
        if result.returncode in (0, 1):
            stopped += f'; {error}' if result.stdout.strip() else ' and printed no result'
    if result.returncode not in (0, 1) or candidate is None or candidate.blocking:
        sys.stderr.write(('' if candidate else result.stdout) + result.stderr)
        raise RuntimeError(f'the pull on this device would stop on this release ({stopped}); {state}')
    for held in candidate.held:
        print(f'{held.lease}; the pull here swaps it at once if only skill text changes, and otherwise applies the '
              'rest of the release and replaces that cache once they exit',
              flush=True)


def push(root):
    git(root, *CREDENTIALS, 'push', '-q', push_url(root), 'HEAD:main')


def start(args, public_repo, private_repo):
    base, public, private = places(args)
    for repo, tree in ((public_repo, public), (private_repo, private)):
        fetch(repo)
        git(repo, 'worktree', 'add', '-q', '-b', args.name, str(tree), 'origin/main')
    # Both branches are new, so a note here is from an abandoned release of the same name, whose worktrees were
    # removed by hand. A start that stops above, on a branch that is still there, keeps the note for its finish.
    public_pushed(base).unlink(missing_ok=True)
    thread = os.environ.get('T3CODE_THREAD_ID', '').strip()
    if thread:
        thread_note(base).write_text(thread + '\n', encoding='utf-8')
    else:  # a note left by an abandoned release of the same name
        thread_note(base).unlink(missing_ok=True)
    print(f'Edit {public} and {private}, then run release.py finish {args.name} --message FILE')
    others = release_lines(args.worktrees, [row for row in releases(args, public_repo, private_repo)
                                            if row[0] != args.name and row[2] != 'on origin/main and clean'])
    if others:
        print('Other open releases; read them before you edit the same files:', *others, sep='\n')
    return 0


# What git keeps while a step that applies commits has stopped part way, the step and how to go on with it. A
# stopped git am also leaves rebase-apply, so its own marker comes first.
STOPPED_STEPS = (('rebase-merge', 'rebase', 'rebase --continue'), ('rebase-apply/applying', 'git am', 'am --continue'),
                 ('rebase-apply', 'rebase', 'rebase --continue'), ('MERGE_HEAD', 'merge', 'merge --continue'),
                 ('CHERRY_PICK_HEAD', 'cherry-pick', 'cherry-pick --continue'),
                 ('REVERT_HEAD', 'revert', 'revert --continue'),
                 ('sequencer', 'cherry-pick or revert', 'cherry-pick --continue or git revert --continue'))


def unfinished(tree, name):
    """Why a worktree is not ready to finish: a step that applies commits stopped part way, or the worktree is off
    its release branch. A finish there would release the commits applied so far and leave out the rest."""
    for marker, step, going_on in STOPPED_STEPS:
        if Path(git(tree, 'rev-parse', '--path-format=absolute', '--git-path', marker)).exists():
            return (f'a {step} stopped part way in {tree}; settle its conflicts, git add each file and run '
                    f'git -C {tree} {going_on}, then finish again')
    head = subprocess.run([*GIT, '-C', str(tree), 'symbolic-ref', '-q', 'HEAD'], capture_output=True, text=True,
                          timeout=60)
    if head.returncode not in (0, 1):  # 1 is a detached HEAD
        raise RuntimeError(f'{tree}: git symbolic-ref exited {head.returncode}: {head.stderr.strip()}')
    if head.stdout.strip() == f'refs/heads/{name}':  # --short would print heads/stash beside a stash
        return None
    if not head.stdout.strip() and holds(tree, f'refs/heads/{name}'):
        return (f'{tree} is on a detached HEAD that holds its release branch {name} and may hold commits of its '
                f'own; move the branch there with git -C {tree} switch -C {name}, then finish again')
    return (f'{tree} is off its release branch {name}; git -C {tree} log {name}..HEAD lists the commits only HEAD '
            f'holds. Bring those you want onto {name}, switch to it, then finish again')


def finish(args, public_repo, private_repo):
    base, public, private = places(args)
    loaded = head(public_repo)
    if running(('pull', 'sync')):
        # A pull that runs now may be changing the engine this process has just loaded. On 10 November 2026 a
        # finish that had waited for one then imported a new host_state.py beside the portable module it had
        # loaded before the pull, and crashed. So start again from the files as they are once it has ended.
        if not wait(('pull', 'sync'), args.timeout):
            raise RuntimeError(f'another pull or sync still runs here after {args.timeout:g} seconds; this run '
                               'committed nothing')
        raise Restart('after the wait')
    # The engine modules edited_owned needs, loaded now, while no pull runs, and beside the ones this process
    # holds. Another release's pull can move the engine at any moment from here on, since no finish holds the lock
    # through its checks and tests, and a module loaded after it would be a new one beside old ones.
    import description, description_toml, drift, host_state  # noqa: F401
    if stops := [stop for tree in (public, private) if (stop := unfinished(tree, args.name))]:
        raise RuntimeError('; '.join(stops) + '; this run committed nothing')
    unmerged = [f'{tree.name}: {", ".join(sorted(set(files)))}' for tree in (public, private)
                if (files := git(tree, 'diff', '--name-only', '--diff-filter=U').splitlines())]
    if unmerged:
        # git add -A would mark them settled and commit their conflict markers.
        raise RuntimeError(f'files in a conflict that is not settled ({"; ".join(unmerged)}); take out the conflict '
                           'markers and git add each file, then finish again; this run committed nothing')
    for tree in (public, private):
        # A worktree with no work of its own holds nothing a rebase could lose or the overlap check could name, so
        # it moves to where a pull brought origin/main; otherwise the engine that pull brought may not read it.
        if not dirty(tree) and not ahead(tree):
            # An ignored file is the worktree's own too: the public allowlist ignores a new file until its ! line.
            try:
                git(tree, 'merge', '-q', '--ff-only', '--no-overwrite-ignore', 'origin/main')
            except RuntimeError as error:
                raise RuntimeError(f'{tree} has no work of its own to rebase, but finish could not bring it up to '
                                   f'origin/main: {error}; this run committed nothing') from error
    if stop := review_stop(public, private, args.reviewed, args.name):
        raise RuntimeError(f'{stop}; this run committed nothing')
    if reused := reused_release_names(public, private):
        raise RuntimeError(f'{", ".join(reused)} changed its selection at the same upstream commit and packaging number, '
                           'so its release folder keeps its name and every machine\'s pull would stop with "Existing '
                           'release differs". Raise packaging by one in its setup/packages file, then finish again; '
                           'this run committed nothing')
    if stale := unpinned(edited_owned(public, private, private_repo), args.entry, args.refresh_owned):
        flags = ' '.join(f'--entry {name}' for name in sorted(stale))
        raise RuntimeError(f'{", ".join(sorted(stale.values()))} changed after its tree was pinned, so every '
                           f"machine's pull would stop on a stale lock. Finish again with {flags} --refresh-owned; "
                           'this run committed nothing')
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
    if not args.hooks_reapproved:
        if changed := reapprovals(public, private):
            raise RuntimeError(f'this release may change the Codex content of {", ".join(changed)}, whose Codex hooks '
                               'are trusted; Codex ties their approval to that content, so every machine\'s pull would '
                               'stop on those hooks. Ask the user to approve them again, queue the approval in each '
                               'machine\'s section of maintenance/pending-sync.md, then finish again with '
                               '--hooks-reapproved')
    if hits := leaks(public, private, args.message):
        raise RuntimeError(f'{", ".join(hits[:20])} in {public} hold a string that the private {DENYLIST} names; '
                           'take it out of the public side, then finish again; this run committed nothing')
    repinned = args.entry if args.refresh_owned else []
    regenerated = {tree: (frozenset({LOCK}) | pin_only(tree, repinned)) if args.entry else frozenset()
                   for tree in (public, private)}
    if stop := origin_stop(public, private, regenerated, args.overlap_reviewed):
        raise RuntimeError(f'{stop}; this run committed nothing')
    if stop := unnamed_commit(args, public, private):  # here, not after the tests and the wait for the lock
        raise RuntimeError(f'{stop}; this run committed nothing')
    passed = tested(public, private, base)
    # The lock is held from the commit to the pull and no longer through the tests: on 12 November 2026 two
    # releases held it for 305 and 360 seconds, of which the tests took 88 and 78 per cent, and other finishes
    # waited behind them for 670 minutes in one evening.
    held = hold(machine_locks()['release'], args.timeout, lambda: head(public_repo) != loaded)
    if held is None:
        raise Restart('because a pull changed the engine while this finish ran its checks or waited')
    with held:
        dropped_stop(public, private)  # again, for a file made during the wait
        if dirty(public):
            if not args.message:
                raise RuntimeError(f'{public}: pass --message with the public commit message')
            commit(public, args.message)
        if dirty(private):
            if not args.private_message:
                raise RuntimeError(f'{private}: pass --private-message with the private commit message')
            commit(private, args.private_message)
        committed = tested_state(public, private)
        if committed != passed:
            # The wait for the lock lies between the tests and the commit, and the commit takes whatever the
            # worktree holds by then.
            raise RuntimeError('the worktrees changed after their tests ran ('
                               f'{state_changes(public, private, passed, committed)}), and the commit took the '
                               f'change; finish again, which tests them as they are now; {push_state(base)}')
        if stop := origin_stop(public, private, regenerated, args.overlap_reviewed):
            raise RuntimeError(stop)
        paired = ahead(public) or half_published(base, public)
        for attempt in range(1, ATTEMPTS + 1):
            if rebase(public, regenerated[public]) and args.entry:
                run(lock, public)
                amend(public)
            if rebase(private, regenerated[private]) == 'regenerate':
                run(lock, public)
                amend(public)
                amend(private)
            before = git(private, 'rev-parse', 'HEAD')
            if paired:
                pair(public, private, args.pair_message)
            if not ahead(public) and not ahead(private):
                # Another session's pair already names a public commit that holds this release's, so the note of a
                # half-published release has nothing left to say.
                public_pushed(base).unlink(missing_ok=True)
                raise RuntimeError('nothing to release: both branches match origin/main')
            device_check(public, private, push_state(base))
            if not args.push:
                break
            # Again, because the commit took whatever the worktree held by then, minutes after the first check.
            if stop := review_stop(public, private, args.reviewed, args.name):
                raise RuntimeError(f'{stop}; the code changed after its review, and {push_state(base)}')
            try:
                for tree in (public, private):  # login and write access on both before either moves
                    git(tree, *CREDENTIALS, 'push', '--dry-run', '-q', push_url(tree), 'HEAD:main')
                if ahead(public):
                    push(public)
                    public_pushed(base).parent.mkdir(parents=True, exist_ok=True)
                    public_pushed(base).write_text(git(public, 'rev-parse', 'HEAD') + '\n', encoding='utf-8')
                push(private)
                public_pushed(base).unlink(missing_ok=True)
                break
            except RuntimeError as error:
                git(private, 'reset', '-q', '--hard', before)
                if not MOVED.search(str(error)):
                    raise RuntimeError(f'push failed and {push_state(base)}: {error}') from error
                if attempt == ATTEMPTS:
                    raise RuntimeError(f'push failed {ATTEMPTS} times because origin keeps moving, and '
                                       f'{push_state(base)}: {error}') from error
        print(f'public {git(public, "rev-parse", "--short", "HEAD")}  private {git(private, "rev-parse", "--short", "HEAD")}'
              + ('  pushed' if args.push else '  not pushed'), flush=True)
        if args.push:
            for line in other_machines(tailnet(), windows_side()):
                print(line, flush=True)
        if args.pull:
            if not wait(('pull', 'sync'), WAIT_FOR_PULL):
                raise RuntimeError(f'pushed, but another pull still runs here after {WAIT_FOR_PULL // 60} minutes; '
                                   'pull once it ends')
            # --wait 0: the wait above was the wait, and a second one would run inside this time limit.
            result = subprocess.run([*python, str(public_repo / 'pull.py'), '--root', str(public_repo), '--private',
                                     str(private_repo), '--update', '--apply', '--wait', '0'], cwd=public_repo, timeout=1800)
            if result.returncode == 0 and (args.entry or args.package):
                subprocess.run([*python, str(public_repo / 'skill_listing.py')], cwd=public_repo, timeout=900)
            return result.returncode
        return 0


def tailnet():
    """Tailscale's view of this tailnet as its status JSON, or None on a machine without a working Tailscale."""
    try:
        shown = subprocess.run(['tailscale', 'status', '--json'], capture_output=True, text=True, timeout=20)
        return json.loads(shown.stdout) if shown.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def windows_side():
    """The hostname WSL shares with the Windows it runs on, in lower case, or None on any other machine."""
    return platform.node().lower() if 'microsoft' in platform.release().lower() else None


def other_machines(status, same_pc=None):
    """A line per other computer of this user on the tailnet, saying whether it is online now. A report that a
    machine cannot pull then quotes this run's output. On 10 November 2026 a note that the Windows PC was cut off
    outlived the cut by a day, and two releases skipped that PC on its word. `same_pc` is windows_side(): from WSL
    the PC's own Windows can show as offline while it runs, as on 11 November 2026, when this line said to queue
    its steps and pull-machines then pulled it."""
    if not status:
        return ['Other machines: Tailscale gave no status here, so check each machine before you say it is unreachable.']
    owner = status['Self'].get('UserID')
    lines = []
    for peer in sorted(status.get('Peer', {}).values(), key=lambda peer: peer.get('HostName', '').lower()):
        if peer.get('UserID') != owner or peer.get('OS') not in ('linux', 'windows', 'macOS'):
            continue
        seen = peer.get('LastSeen', '')[:10]
        if peer.get('Online'):
            state = 'online now, so pull there'
        elif same_pc and peer['OS'] == 'windows' and peer.get('HostName', '').lower() == same_pc:
            state = ("shown as offline, but it is this PC's Windows side, which WSL reaches without the tailnet, "
                     'so pull it with pull-machines')
        else:
            state = f'offline, last seen {seen}, so queue its steps in maintenance/pending-sync.md'
        lines.append(f"Other machine {peer.get('HostName')} ({peer['OS']}): {state}")
    return lines or ['Other machines: the tailnet lists none of yours besides this one.']


def detached_paths(args):
    build = places(args)[0] / 'build'
    return build / 'finish.log', build / 'finish.exit'


def detach_command(args, argv, environ):
    """The systemd-run command that runs this finish as a user unit: the same arguments without --detach, its
    output appended to the log, and its exit status written to the status file. A unit starts without the
    session's environment, so it is given the PATH, HOME and SSH agent the finish needs."""
    log, status = detached_paths(args)
    passed = [part for name in ('PATH', 'HOME', 'SSH_AUTH_SOCK') if environ.get(name)
              for part in ('--setenv', f'{name}={environ[name]}')]
    inner = [sys.executable, '-B', str(ENGINE / 'release.py'), *(part for part in argv if part != '--detach'),
             '--report', str(status)]
    return ['systemd-run', '--user', '--collect', f'--unit=dotfiles-release-{args.name}', *passed,
            f'--working-directory={ENGINE}', f'--property=StandardOutput=append:{log}',
            f'--property=StandardError=append:{log}', *inner]


def detach(args, argv):
    for file in (args.message, args.private_message, args.pair_message, args.reviewed):
        if file and not file.is_absolute():
            raise RuntimeError(f'--detach needs an absolute path for {file}, because the unit runs in {ENGINE}')
    if stop := message_stop(args):  # here, like the next stop, not minutes later in the unit's log
        raise RuntimeError(f'{stop}; nothing was started')
    base, public, private = places(args)
    if stop := review_stop(public, private, args.reviewed, args.name):
        raise RuntimeError(f'{stop}; nothing was started')
    if not shutil.which('systemd-run'):
        raise RuntimeError('--detach needs systemd-run, which this machine lacks; run the finish in the foreground')
    if unit_state(args.name) in RUNNING_UNIT:
        # Its log would move aside while the unit still writes it, and result would lose the status file.
        raise RuntimeError(f'a detached finish of {args.name} still runs as unit dotfiles-release-{args.name}; wait '
                           f'for it with release.py result {args.name} --timeout 3600; nothing was started')
    log, status = detached_paths(args)
    log.parent.mkdir(parents=True, exist_ok=True)
    status.unlink(missing_ok=True)
    kept = keep(log)
    log.write_text('', encoding='utf-8')
    started = subprocess.run(detach_command(args, argv, os.environ), capture_output=True, text=True, timeout=60)
    if started.returncode:
        raise RuntimeError(f'systemd-run exited {started.returncode}: {started.stderr.strip()}')
    print(f'Detached as unit dotfiles-release-{args.name}; log {log}. Wait for it with: release.py result '
          f'{args.name} --timeout 3600' + (f'. The log of the attempt before is kept as {kept}' if kept else ''))
    return 0


RUNNING_UNIT = ('active', 'activating', 'reloading', 'deactivating')


def unit_state(name):
    """What systemd calls the state of a detached finish's unit, or None when systemd cannot be asked."""
    try:
        return subprocess.run(['systemctl', '--user', 'is-active', f'dotfiles-release-{name}'],
                              capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def unit_active(name):
    """Whether the unit of a detached finish still runs. True when systemd cannot be asked, so result keeps waiting."""
    state = unit_state(name)
    return state is None or state in RUNNING_UNIT


def result(args):
    """Block until a detached finish has ended, print the end of its log, and return its status: the one it
    wrote, or CRASHED when its unit is gone and it wrote none, as after a kill."""
    log, status = detached_paths(args)
    if not log.exists():
        raise RuntimeError(f'no detached finish of {args.name} was started: {log} is absent')

    def reported():
        return status.is_file() and status.read_text(encoding='utf-8').strip()

    deadline, ended = time.monotonic() + args.timeout, False
    while not reported():
        if ended:  # looked again after the unit was gone, since it writes its status as its last step
            break
        if time.monotonic() >= deadline:
            print(f'Still running after {args.timeout:g} seconds; log {log}', file=sys.stderr)
            return 4
        ended = not unit_active(args.name)
        time.sleep(2)
    lines = log.read_text(encoding='utf-8', errors='replace').splitlines()
    print('\n'.join(lines[-RESULT_LINES:]))
    if not reported():
        print(f'finish {args.name} ended without a status: its unit is gone, so it was killed or never started; '
              f'full log {log}', file=sys.stderr)
        return CRASHED
    code = int(reported())
    print(f'finish {args.name} exited {code}; full log {log}')
    return code


def clean(args, public_repo, private_repo):
    """Remove a release's worktrees, branches and build folder. Both branches and the note of a pushed public
    commit are checked before anything goes, because a release whose public half alone is on origin still needs
    its public worktree and that note for the finish that pairs it. The note alone tells such a release apart
    when it changed only the public side, where both branches are on origin/main."""
    base, public, private = places(args)
    if public_pushed(base).is_file() and not args.force:
        raise RuntimeError(f'the public commit of {args.name} is on origin without its pair, as {public_pushed(base)} '
                           f'notes; run its finish again, or pass --force to give the release up; nothing was removed')
    sides = []
    for repo, tree in ((public_repo, public), (private_repo, private)):
        listed = subprocess.run([*GIT, '-C', str(repo), 'rev-parse', '--verify', '-q', args.name],
                                capture_output=True, timeout=60).returncode == 0
        if listed and not args.force:
            fetch(repo)
            merged = subprocess.run([*GIT, '-C', str(repo), 'merge-base', '--is-ancestor', args.name, 'origin/main'],
                                    capture_output=True, timeout=60).returncode == 0
            if not merged:
                raise RuntimeError(f'{repo}: branch {args.name} is not on origin/main; push it, or pass --force; '
                                   'nothing was removed')
        sides.append((repo, tree, listed))
    for repo, tree, listed in sides:
        if tree.exists():
            git(repo, 'worktree', 'remove', *(['--force'] if args.force else []), str(tree))
        if listed:
            git(repo, 'branch', '-q', '-D', args.name)
    if base.exists():
        shutil.rmtree(base)
    return 0


def main(argv=None):
    report, code = [], CRASHED
    try:
        code = dispatch(argv, report)
    except SystemExit as stop:
        code = stop.code if isinstance(stop.code, int) else 1
        raise
    finally:
        for path in report:  # a detached finish tells result how it ended, an uncaught error included
            path.write_text(f'{code}\n', encoding='utf-8')
    return code


def dispatch(argv, report):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('start', 'finish', 'review', 'clean', 'status', 'wait', 'result'))
    parser.add_argument('name', nargs='?', help='branch name, also the folder under --worktrees')
    parser.add_argument('--private', type=Path, default=ENGINE.parent / 'dotfiles-private', help='canonical private checkout')
    parser.add_argument('--worktrees', type=Path, default=Path.home() / 'worktrees')
    parser.add_argument('--message', type=Path, help='file with the public commit message')
    parser.add_argument('--private-message', type=Path, help='file with the private commit message')
    parser.add_argument('--pair-message', type=Path, help='file with the message of the private commit that pairs the public one')
    parser.add_argument('--entry', action='append', default=[], help='lock this setup entry (repeat)')
    parser.add_argument('--refresh-owned', action='store_true', help='accept the edited owned trees of the entries')
    parser.add_argument('--package', action='append', default=[], help='build this package (repeat)')
    parser.add_argument('--push', action='store_true', help='push both branches to origin/main')
    parser.add_argument('--overlap-reviewed', action='store_true',
                        help='finish although origin changed files this branch changed, after reading those commits')
    parser.add_argument('--reviewed', type=Path,
                        help="finish a release that changes code: the file with another agent's review findings and "
                             'the Reviewed lines that review NAME prints')
    parser.add_argument('--hooks-reapproved', action='store_true',
                        help='finish although a plugin with trusted Codex hooks may change, once the user approved them again')
    parser.add_argument('--pull', action='store_true', help='then pull and apply the pair on this device')
    parser.add_argument('--force', action='store_true', help='clean: remove unpushed branches too')
    parser.add_argument('--timeout', type=float, default=1800,
                        help='wait and result: give up after this many seconds; finish: wait this long for '
                             'another release, pull or sync here')
    parser.add_argument('--detach', action='store_true',
                        help='finish: run as a systemd user unit and return at once, with absolute paths for the '
                             'message files and --reviewed; read it with result NAME')
    parser.add_argument('--report', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.action in ('start', 'finish', 'review', 'clean', 'result') and not args.name:
        parser.error(f'{args.action} needs a NAME')
    if args.report:
        report.append(args.report)
    try:
        if args.action == 'wait':
            if wait(None, args.timeout):
                print('No release holds this machine\'s lock and no pull or sync runs here.')
                return 0
            print(f'Still running after {args.timeout:g} seconds: ' + '; '.join(running()), file=sys.stderr)
            return 3
        if args.action == 'result':
            return result(args)
        if args.action == 'finish' and args.detach:
            return detach(args, sys.argv[1:] if argv is None else list(argv))
        if args.action == 'finish':
            if stop := message_stop(args):  # before any work
                raise RuntimeError(f'{stop}; this run committed nothing')
            try:
                with one_finish(args):
                    return finish(args, ENGINE, args.private.resolve())
            except Restart as why:
                print(f'Starting again {why}, to run the engine as it is now', flush=True)
                sys.stderr.flush()
                os.execv(sys.executable, [sys.executable, '-B', str(ENGINE / 'release.py'),
                                          *(sys.argv[1:] if argv is None else argv)])
        action = {'start': start, 'review': review, 'clean': clean, 'status': status}[args.action]
        return action(args, ENGINE, args.private.resolve())
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f'Release stopped: {error}', file=sys.stderr)
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
