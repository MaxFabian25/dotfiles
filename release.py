"""Release the exact signed public/private candidates whose complete trees passed the tests.

Run with the canonical engine's Python. `start NAME` makes two editable release worktrees. `finish NAME`
checks review, owned pins, packaging, hook approvals, leaks and overlap before committing and rebasing onto
captured origin tips. It derives a signed private pairing object without changing the editable branch.
Both complete suites run in sibling detached candidate worktrees outside the machine lock. Private test
files run separately, up to eight at a time. No generated file or documentation is excluded from evidence.

The machine lock covers preparation and publication, never tests. A per-release lock covers the entire
finish and cleanup. Before pushing explicit candidate commit IDs, finish rechecks source and candidate
files, device applicability and origin. Origin movement and engine reloads share three bounded rounds.
Tests can fail after local commits. Fix the files and finish again; unchanged exact trees reuse the atomic
build/tests-passed receipt. That receipt also recovers interrupted publication, including a public push
whose acknowledgement or half-publication note was lost. Unresolved candidate worktrees retain its objects.

`review NAME` prints the code and fingerprints an independent reviewer signs off in --reviewed FILE.
`finish --push --pull` publishes and then applies the pair using the canonical checkouts under the lock.
Without --push it retains the tested candidates and applies nothing. Logs record tested and pushed trees.
`clean NAME` removes published source branches and owned candidate registrations. `status` lists releases,
their owning threads and running finishes. `wait` waits for machine locks, not concurrent candidate tests.
`finish --detach` uses a systemd user unit; `result NAME --timeout SECONDS` waits for it and returns its status.
Uncaught errors and a disappeared unit without a status return 70. Windows allows only status and wait.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
from dataclasses import asdict, dataclass
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
import traceback

import candidate_result
import claims
from portable import GIT, codex_home, exclusive_lock, lock_holder, native_cli_environment, staging_folder
from pull import fetch, git

ENGINE = Path(__file__).resolve().parent
STATE = Path.home() / '.local/state/dotfiles'
WAIT_FOR_PULL = 1200  # seconds a finish waits for another pull on this machine before its own
PUBLIC_COMMIT = re.compile(rb'(?m)^([ \t]*public_commit[ \t]*=[ \t]*[\"\x27])([0-9a-f]{40}(?:[0-9a-f]{24})?)([\"\x27])')
COMMIT_ID = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?')
# Push over HTTPS with the GitHub CLI's login, which the Mac and WSL hold, whatever helper or SSH origin the
# checkout configures. The private checkout's SSH origin uses a read-only deploy key.
GITHUB_SSH = re.compile(r'^(?:git@github\.com:|ssh://git@github\.com/)')
CREDENTIALS = ('-c', 'credential.https://github.com.helper=',
               '-c', 'credential.https://github.com.helper=!gh auth git-credential')
ATTEMPTS = 3
ROUND_BUDGET = '_DOTFILES_FINISH_ROUNDS'
RESULT_LINES = 25  # lines of a detached finish's log that result prints
CRASHED = 70
MOVED = re.compile(r'\((?:fetch first|non-fast-forward)\)')
LOCK = 'setup/lock.toml'
# The value of a tree key in a setup entry, which pins its source's tree. The rest of its line is the entry's own.
TREE_VALUE = re.compile(rb'^([ \t]*tree[ \t]*=[ \t]*)"[^"\r\n]*"', re.MULTILINE)
SETUP = 'setup/setup.toml'
# The public .gitignore is an allowlist: this pattern drops every path that a later ! line does not bring back.
ALLOWLIST_BASE = '/*'
# Strings the public tree must never hold, such as host and account names. The list is private, because a public
# list would publish them.
DENYLIST = 'config/privacy/public-denylist.txt'
DENYLIST_COMMENT = re.compile(r'(?:^|\s)#')
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

ISOLATED_RUN = ('-I', '-B')
UNITTEST_END = re.compile(r'^Ran (\d+) tests? in [^\n]*\n\n(OK|FAILED)(?: \(([^)\n]*)\))?', re.MULTILINE)
UNITTEST_FAILURE = re.compile(r'^(?:FAIL|ERROR): (\S+)', re.MULTILINE)
UNITTEST_SKIPPED = re.compile(r'\bskipped=(\d+)')
PRIVATE_TESTS_AT_ONCE = 8
PRIVATE_TEST_TIMEOUT_SECONDS = 1800


@dataclass(frozen=True)
class Heads:
    public: str
    private: str


@dataclass(frozen=True)
class Trees:
    public: str
    private: str


@dataclass(frozen=True)
class Candidate:
    public: Path
    private: Path
    source: Heads
    commits: Heads
    trees: Trees
    paired: bool


@dataclass(frozen=True)
class TestedCandidate:
    candidate: Candidate


def machine_locks():
    """The locks a release finish, a pull and a sync hold on this machine."""
    return {'release': STATE / 'release.lock', 'pull': STATE / 'pull.lock', 'sync': codex_home() / 'dotfiles/sync.lock'}


def running(names=None):
    """The runs that hold this machine's locks now, each as a line naming its process and command."""
    return [f'{name} PID {holder["pid"]} since {str(holder.get("since", ""))[11:16]}: {str(holder.get("command", ""))[:160]}'
            for name, path in machine_locks().items() if (names is None or name in names) and (holder := lock_holder(path))]


def lock_pause(seconds):
    """A function of its own so that a test can patch this pause alone, because patching time.sleep would also
    catch subprocess, which sleeps while it waits for a child."""
    time.sleep(seconds)


def wait(names, timeout):
    """Block until no run holds the named locks: True once they are free, False after timeout seconds."""
    deadline, shown = time.monotonic() + timeout, None
    while busy := running(names):
        if busy != shown:
            print('Waiting for ' + '; '.join(busy), file=sys.stderr, flush=True)
            shown = busy
        if time.monotonic() >= deadline:
            return False
        lock_pause(2)
    return True


class Restart(Exception):
    def __init__(self, reason, remaining):
        super().__init__(reason)
        self.remaining = remaining


def head(repo):
    """The commit a checkout stands on. A pull changes the engine only by moving the canonical public checkout to
    another commit, so this tells one engine from the next. A read that fails stops the finish with git's words,
    because two failed reads would compare equal and hide a changed engine."""
    return git(repo, 'rev-parse', 'HEAD')


def hold(path, timeout, stale, *, stopped='this run committed nothing'):
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
                               f'finish again, which reuses unchanged exact-tree evidence; {stopped}')
        lock_pause(1)


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


def changed_files(tree, origin='origin/main'):
    """Without --no-renames a file renamed from a.py to a.txt would list only a.txt, and without -z git quotes a name
    that is not plain ASCII."""
    fork = git(tree, 'merge-base', 'HEAD', origin)
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


def ahead(root, origin='origin/main'):
    """Whether the branch has a commit that origin/main lacks. A branch that is only behind, because another
    session pushed since it started, has nothing of its own to release or to pair."""
    return int(git(root, 'rev-list', '--count', f'{origin}..HEAD')) > 0


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


def unnamed_commit(args, public, private, written):
    """Why finish could not commit a worktree that holds changes, or None: it was given no message for it, and
    they are not a relock that it amends into the branch's own commit."""
    for tree, message, option in ((public, args.message, '--message'), (private, args.private_message, '--private-message')):
        if dirty(tree) and not message and not relock_only(tree, written[tree]):
            return f'{tree}: pass {option} with the {tree.name} commit message'
    return None


def leaks(public, private, message=None):
    """Where the public tree, as a commit would take it, or its commit message holds a string the private
    denylist names. One string per line, matched without regard to case; # starts a comment, at the start of a line
    or after white space."""
    listed = private / DENYLIST
    if not listed.is_file():
        raise RuntimeError(f'{listed} is missing, so nothing checks the public side for the strings it names; '
                           'restore it, then finish again')
    try:
        text = listed.read_text(encoding='utf-8')
        if '\0' in text:
            raise ValueError('it holds a NUL byte, as a UTF-16 file does')
    except ValueError as error:
        raise RuntimeError(f'{listed} is not UTF-8 ({error}), so nothing checks the public side for the strings it '
                           'names; save it as UTF-8, then finish again') from error
    lines = text.replace('\ufeff', '').splitlines()
    patterns = [pattern for line in lines if (pattern := DENYLIST_COMMENT.split(line, maxsplit=1)[0].strip())]
    if not patterns:
        raise RuntimeError(f'{listed} lists no strings, so nothing checks the public side; restore its strings, '
                           'then finish again')
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
    folding = (ordinary_tip(root)
               and git_bytes(root, 'stripspace', data=message.read_bytes())
               == git_bytes(root, 'stripspace', data=git_bytes(root, 'log', '-1', '--format=%B')))
    git(root, 'add', '-A')
    git(root, 'commit', '-q', *(['--amend'] if folding else []), '-F', str(message.resolve()))


def rebase(root, regenerated=frozenset(), *, onto, paired=False):
    if holds(root, onto):
        return ''
    # --continue then commits each settled step with its own author and message, minus git's conflict comments.
    environment = {**native_cli_environment(), 'GIT_EDITOR': 'true'}
    result, step = 'rebased', ('rebase', '-q', onto)
    while True:
        done = subprocess.run([*GIT, '-C', str(root), *step], env=environment, capture_output=True, timeout=600)
        if not done.returncode:
            return result
        try:
            old_pair = paired and pair_only(root, git(root, 'rev-parse', 'REBASE_HEAD'), origin=onto)
            taken = take_origin(root, regenerated | ({'release.toml'} if old_pair else set()), empty=old_pair)
        except RuntimeError:
            taken = False
        if not taken:
            subprocess.run([*GIT, '-C', str(root), 'rebase', '--abort'], capture_output=True, timeout=60)
            raise RuntimeError(f'{root}: the branch conflicts with origin/main; rebase it by hand, then finish again: '
                               f'{done.stderr.decode(errors="replace").strip()}')
        empty = not git(root, 'diff', '--cached', '--name-only')
        result, step = 'regenerate', ('rebase', '--skip' if old_pair and empty else '--continue')


def take_origin(root, regenerated, *, empty=False):
    conflicted = sorted(set(git(root, 'diff', '--name-only', '--diff-filter=U').splitlines()))
    if not conflicted or not set(conflicted) <= regenerated:
        return False
    git(root, 'checkout', '--ours', '--', *conflicted)  # during a rebase, ours is origin/main's side
    git(root, 'add', '--', *conflicted)
    return bool(git(root, 'diff', '--cached', '--name-only')) or empty


def uncommitted(tree):
    """The files that differ from HEAD in the worktree or in the index, new ones included. The index counts on its
    own, because an amend commits all of it, and a change staged and then taken back is in it alone."""
    listings = (git(tree, 'diff', '--name-only', '--no-renames', '-z', 'HEAD'),
                git(tree, 'diff', '--cached', '--name-only', '--no-renames', '-z', 'HEAD'),
                git(tree, 'ls-files', '--others', '--exclude-standard', '-z'))
    return {name for listing in listings for name in listing.split('\0') if name}


def relock_writes(tree, regenerated, repinned):
    """The files a relock writes that an amend may take: those regenerated names, and a named entry's file whose
    change from HEAD is only the tree its source pins, which covers one the branch itself changed or added."""
    return regenerated | pin_only(tree, repinned, 'HEAD') if regenerated else frozenset()


def amend(root, written, then=''):
    """Fold the files a relock rewrote into the branch's own release commit. Only those: a file that appeared after
    the commit and the check that it holds what was tested is no part of the release."""
    if not (changed := uncommitted(root)):
        return
    if not ordinary_tip(root):
        raise RuntimeError(f'{root}: relocking needs a new source commit on this tip; supply its message, then finish again')
    if other := sorted(changed - written):
        raise RuntimeError(f'{root}: {", ".join(other)} changed after the commit, and an amend takes only what the '
                           f'relock writes; commit or remove them, then finish again{then}')
    git(root, 'add', '-A', '--', *sorted(changed))  # as commit() does, for a file the relock wrote for the first time
    git(root, 'commit', '-q', '--amend', '--no-edit')
    print(f'{root}: amended the relock into {git(root, "log", "-1", "--format=%h %s")}', flush=True)


def ordinary_tip(tree):
    return (ahead(tree)
            and len(git(tree, 'rev-list', '--parents', '-n', '1', 'HEAD').split()) == 2
            and git(tree, 'diff-tree', '--no-commit-id', '--name-only', '--no-renames', '-r', 'HEAD') != 'release.toml')


def relock_only(tree, written):
    """Whether every uncommitted file is one the relock writes, on a branch whose newest commit is its own and can
    take them by an amend. It looks at what is uncommitted rather than at when, because a finish that stops after
    its relock leaves those files for the next one. A merge is not such a commit, because rebase() drops it and what
    was amended into it, and nor is a pair, which changes release.toml alone."""
    return bool(written and dirty(tree) and ordinary_tip(tree) and uncommitted(tree) <= written)


def public_pointer(raw):
    try:
        value = tomllib.loads(raw.decode('utf-8'))['public_commit']
    except (ValueError, KeyError) as error:
        raise RuntimeError('release.toml needs one valid public_commit; fix it, then finish again') from error
    matches = list(PUBLIC_COMMIT.finditer(raw))
    if (not isinstance(value, str) or not COMMIT_ID.fullmatch(value) or len(matches) != 1
            or matches[0][2].decode() != value or matches[0][1][-1:] != matches[0][3]):
        raise RuntimeError('release.toml needs one unambiguous full public_commit; fix it, then finish again')
    return matches[0]


def pair(public, private, source, message):
    raw = git_bytes(private, 'show', f'{source.private}:release.toml')
    pointer = public_pointer(raw)
    if pointer[2].decode() == source.public:
        return source.private
    changed = raw[:pointer.start(2)] + source.public.encode() + raw[pointer.end(2):]
    mode = git(private, 'ls-tree', source.private, '--', 'release.toml').split()[0]
    if mode not in ('100644', '100755'):
        raise RuntimeError('release.toml must be a regular file; fix it, then finish again')
    with tempfile.TemporaryDirectory() as folder:
        index = {'GIT_INDEX_FILE': str(Path(folder) / 'index')}
        git_bytes(private, 'read-tree', source.private, environment=index)
        blob = git_bytes(private, 'hash-object', '-w', '--stdin', data=changed).decode().strip()
        git_bytes(private, 'update-index', '--cacheinfo', f'{mode},{blob},release.toml', environment=index)
        tree = git_bytes(private, 'write-tree', environment=index).decode().strip()
        subject = git(public, 'log', '-1', '--format=%s', source.public)
        wording = message.read_bytes() if message else f'Pair "{subject}"\n'.encode()
        return git_bytes(private, 'commit-tree', '-S', tree, '-p', source.private,
                         data=wording).decode().strip()


def pair_only(root, commit, *, origin='origin/main'):
    parents = git(root, 'rev-list', '--parents', '-n', '1', commit).split()
    if len(parents) != 2 or holds(root, commit, origin):
        return False
    if git(root, 'diff', '--name-only', '--no-renames', parents[1], commit) != 'release.toml':
        return False
    before = git_bytes(root, 'show', f'{parents[1]}:release.toml')
    after = git_bytes(root, 'show', f'{commit}:release.toml')
    try:
        old, new = public_pointer(before), public_pointer(after)
    except RuntimeError:
        return False
    modes = [git(root, 'ls-tree', at, '--', 'release.toml').split()[0] for at in (parents[1], commit)]
    return (modes[0] == modes[1] and before != after
            and before[:old.start(2)] + before[old.end(2):] == after[:new.start(2)] + after[new.end(2):])


def push_url(root):
    return GITHUB_SSH.sub('https://github.com/', git(root, 'remote', 'get-url', '--push', 'origin'))


def overlap(root, regenerated=frozenset(), *, origin=None):
    """Files origin/main changed since this branch left it that this branch changed too, committed or not. A
    clean rebase can still join two sessions' clashing designs for the same file. A file finish writes anew is
    no design of this branch's."""
    if origin is None:
        fetch(root)
        origin = git(root, 'rev-parse', 'origin/main')
    fork = git(root, 'merge-base', 'HEAD', origin)
    theirs = set(git(root, 'diff', '--name-only', '--no-renames', '-z', fork, origin).split('\0'))
    return sorted((theirs & set(changed_files(root, origin))) - {LOCK} - regenerated)


def pin_only(root, entries, at=None):
    """The setup files of the named entries in which this branch changed nothing beyond the tree its source pins:
    no other value, such as a tree key of another table, and no other byte, such as a comment, a line ending or
    the final newline, which origin's copy would lose. A file the branch left as it was counts too, and cannot conflict. Locking with
    --refresh-owned writes that pin anew, so origin's copy of such a file loses nothing. At a commit, such as HEAD,
    it compares with that commit instead of with where the branch started."""
    def unpinned_text(description):
        return {**description, 'source': {key: value for key, value in description.get('source', {}).items()
                                          if key != 'tree'}}

    def unpinned_bytes(content):
        return TREE_VALUE.sub(rb'\1""', content)
    fork = at or git(root, 'merge-base', 'HEAD', 'origin/main')
    found = set()
    for path in (f'setup/{kind}/{entry}.toml' for kind in ('skills', 'packages') for entry in entries):
        before, after = started(root, path, at), current(root, path)
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
    return Trees(content(public), content(private))


def state_changes(public, private, before, after):
    """Where two states of the worktrees differ, for a stop to name."""
    found = []
    for label, tree in (('public', public), ('private', private)):
        old, new = getattr(before, label), getattr(after, label)
        if old == new:
            continue
        names = git(tree, 'diff', '--name-only', '--no-renames', old, new).splitlines()
        found.append(f'{label}: ' + ', '.join(names[:8]) + (f' and {len(names) - 8} more' if len(names) > 8 else ''))
    return '; '.join(found)


def tests_passed(base):
    return base / 'build/tests-passed'


def read_receipt(public, private, base):
    try:
        saved = json.loads(tests_passed(base).read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        return None
    if (not isinstance(saved, dict) or saved.get('version') != 2 or type(saved.get('paired')) is not bool
            or not isinstance(saved.get('workspace'), str)
            or not re.fullmatch(r'round-[a-zA-Z0-9_-]+', saved['workspace'])):
        return None
    for field in ('source', 'commits', 'trees'):
        pair_ids = saved.get(field)
        if (not isinstance(pair_ids, dict) or set(pair_ids) != {'public', 'private'}
                or any(not isinstance(value, str) or not COMMIT_ID.fullmatch(value) for value in pair_ids.values())):
            return None
    for side, root in (('public', public), ('private', private)):
        ids = [saved[field][side] for field in ('source', 'commits', 'trees')]
        objects = git_bytes(root, 'cat-file', '--batch-check=%(objecttype)',
                            data=('\n'.join(ids) + '\n').encode()).decode().splitlines()
        if objects != ['commit', 'commit', 'tree']:
            return None
        if git(root, 'rev-parse', f'{ids[1]}^{{tree}}') != ids[2]:
            return None
    if saved['source']['public'] != saved['commits']['public']:
        return None
    if not holds(private, saved['source']['private'], saved['commits']['private']):
        return None
    workspace = base / 'build/candidates' / saved['workspace']
    return Candidate(workspace / 'public', workspace / 'private', Heads(**saved['source']),
                     Heads(**saved['commits']), Trees(**saved['trees']), saved['paired'])


def unchanged(public, private, commits):
    dropped_stop(public, private)
    for side, root in (('public', public), ('private', private)):
        expected = getattr(commits, side)
        paths = sorted(uncommitted(root))
        if head(root) != expected or paths or content(root) != git(root, 'rev-parse', f'{expected}^{{tree}}'):
            raise RuntimeError(f'{root}: files or HEAD changed after preparation ({", ".join(paths) or "HEAD/tree"}); '
                               'the edit is preserved; finish again once the edits are done')


def tested(draft, base):
    unchanged(draft.public, draft.private, draft.commits)
    note, passed = tests_passed(base), read_receipt(draft.public, draft.private, base)
    if passed and passed.trees == draft.trees:
        print(f'The tests did not run again: they passed on these exact trees before, as {note} notes', flush=True)
    else:
        reason = (state_changes(draft.public, draft.private, passed.trees, draft.trees) if passed
                  else 'no valid complete exact-tree receipt')
        print(f'Both complete suites run because {reason}', flush=True)
        test_suite(draft.public, draft.private, base / 'build/tests.log')
    unchanged(draft.public, draft.private, draft.commits)
    if tested_state(draft.public, draft.private) != draft.trees:
        raise RuntimeError('candidate trees differ from the prepared trees; finish again')
    saved = {field: asdict(getattr(draft, field)) for field in ('source', 'commits', 'trees')}
    saved.update(version=2, paired=draft.paired, workspace=draft.public.parent.name)
    note.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=note.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(saved, output)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(note)
    finally:
        temporary.unlink(missing_ok=True)
    print(f'Exact trees tested: public {draft.trees.public}; private {draft.trees.private}; no files excluded', flush=True)
    return TestedCandidate(draft)


def candidate_owner(public, private, base):
    return {'release': str(base.resolve()), **{side: git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')
                                            for side, root in (('public', public), ('private', private))}}


def retire_candidates(public, private, base, *, keep=None):
    parent = base / 'build/candidates'
    if not parent.exists():
        return
    owner = candidate_owner(public, private, base)
    names = {path.name.removesuffix('.owner.json') for path in parent.iterdir()}
    for name in sorted(names):
        folder = parent / name
        if folder == keep:
            continue
        retired = parent / f'{name}.owner.json'
        marker = retired if retired.exists() else folder / 'owner.json'
        try:
            known = (re.fullmatch(r'round-[a-zA-Z0-9_-]+', name) and not folder.is_symlink()
                     and not marker.is_symlink() and json.loads(marker.read_text(encoding='utf-8')) == owner)
        except (OSError, ValueError):
            known = False
        contents = list(folder.iterdir()) if folder.is_dir() else []
        if (not known or any(path.name not in ('public', 'private', 'owner.json') for path in contents)
                or (marker == retired and contents)):
            raise RuntimeError(f'{folder}: unrecognised candidate workspace; inspect it before cleanup')
        for side, repo in (('public', public), ('private', private)):
            root = folder / side
            listed = {line.removeprefix('worktree ') for line in
                      git(repo, 'worktree', 'list', '--porcelain', '-z').split('\0') if line.startswith('worktree ')}
            if str(root) in listed:
                if root.is_symlink() or (root.exists() and candidate_owner(root, repo, base)['public'] != owner[side]):
                    raise RuntimeError(f'{root}: candidate Git ownership changed; inspect it before cleanup')
                git(repo, 'worktree', 'remove', '--force', str(root))
            elif root.exists():
                raise RuntimeError(f'{root}: candidate is not registered to its repository; inspect it before cleanup')
        if marker != retired:
            marker.replace(retired)
        if folder.exists():
            folder.rmdir()
        retired.unlink()


@contextlib.contextmanager
def candidate(public, private, base, *, source, paired, message):
    unchanged(public, private, source)
    commits = Heads(source.public, pair(public, private, source, message) if paired else source.private)
    trees = Trees(*(git(root, 'rev-parse', f'{commit}^{{tree}}')
                    for root, commit in ((public, commits.public), (private, commits.private))))
    parent = base / 'build/candidates'
    parent.mkdir(parents=True, exist_ok=True)
    folder = Path(staging_folder(prefix='round-', dir=parent))
    (folder / 'owner.json').write_text(json.dumps(candidate_owner(public, private, base)) + '\n', encoding='utf-8')
    for side, repo in (('public', public), ('private', private)):
        git(repo, 'worktree', 'add', '--detach', '-q', str(folder / side), getattr(commits, side))
    draft = Candidate(folder / 'public', folder / 'private', source, commits, trees, paired)
    unchanged(draft.public, draft.private, commits)
    yield draft


def conflicts(root, regenerated=frozenset(), *, origin=None):
    if origin is None:
        fetch(root)
        origin = git(root, 'rev-parse', 'origin/main')
    merged = subprocess.run([*GIT, '-C', str(root), 'merge-tree', '--write-tree', '--name-only', '--no-messages', '-z',
                             origin, snapshot(root)], capture_output=True, timeout=300,
                            env=native_cli_environment())
    # A merge git could try prints the merged tree's ID first. Without it exit 1 is no conflict: git also exits 1
    # with nothing on stdout when it cannot read a commit.
    tree, *names = merged.stdout.decode(errors='replace').split('\0')
    if merged.returncode not in (0, 1) or not tree.strip():
        raise RuntimeError(f'{root.name}: git merge-tree exited {merged.returncode}: '
                           f'{merged.stderr.decode(errors="replace").strip()}')
    return sorted({name for name in names if name and merged.returncode} - regenerated)


def origin_stop(public, private, regenerated, overlap_reviewed, *, origins, paired=False):
    allowed = dict(regenerated)
    if paired and 'release.toml' not in uncommitted(private):
        changes = git(private, 'log', '--format=%H', f'{origins.private}..HEAD', '--', 'release.toml').splitlines()
        if changes and all(pair_only(private, commit, origin=origins.private) for commit in changes):
            allowed[private] |= {'release.toml'}
    clashing = [f'{side}: {", ".join(files)}' for side, tree in (('public', public), ('private', private))
                if (files := conflicts(tree, allowed[tree], origin=getattr(origins, side)))]
    if clashing:
        return ('the branch conflicts with origin/main (' + '; '.join(clashing) + '). Commit the worktree, rebase '
                'it by hand with git rebase origin/main, then finish again')
    if overlap_reviewed:
        return None
    shared = [f'{side}: {", ".join(files)}' for side, tree in (('public', public), ('private', private))
              if (files := overlap(tree, allowed[tree], origin=getattr(origins, side)))]
    if shared:
        return ('another session changed the same files on origin/main since this branch started ('
                + '; '.join(shared) + '). Read its commits with git log -p HEAD..origin/main -- FILE, '
                'reconcile the two designs, then finish again with --overlap-reviewed')
    return None


def started(root, path, at=None):
    """A TOML file as this branch found it on origin/main, or as the commit given holds it, or {} where it was
    absent. A copy at the commit given that is not TOML is None."""
    fork = at or git(root, 'merge-base', 'HEAD', 'origin/main')
    if not git(root, 'ls-tree', fork, path):
        return {}
    if at:
        try:
            return tomllib.loads(git(root, 'show', f'{fork}:{path}'))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            return None  # so pin_only() counts it as changed beyond the pin, and the commit takes it as before
    return toml(lambda: git(root, 'show', f'{fork}:{path}'), f'{path} as origin/main held it at {fork[:7]}',
                f'; no finish should have let it go there, and git log origin/main -- {path} names the commit that did')


def current(root, path):
    return toml(lambda: (root / path).read_text(encoding='utf-8'), root / path) if (root / path).is_file() else {}


def toml(read, name, then=''):
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
    import drift
    from host_state import DeviceRoots
    private_setup = private / 'setup'
    try:
        setup = load(Roots(public / 'setup', private_setup if private_setup.is_dir() else None))
    except DescriptionError as error:
        raise RuntimeError(unloadable({public: ENGINE, private: private_repo}, error)) from error
    return drift.edited_owned(setup, DeviceRoots.current())


def holds(tree, commit, target='HEAD'):
    """Whether the tree's HEAD holds the commit, or a stop with git's own words when git cannot tell."""
    result = subprocess.run([*GIT, '-C', str(tree), 'merge-base', '--is-ancestor', commit, target],
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
                'Fix the setup or bring the worktrees up to date, then finish again')
    if older:
        return (f'{problem}The engine is older than this branch: {", ".join(map(str, older))} lacks commits the '
                f'branch started from. Pull this machine with {sys.executable} -B {ENGINE / "pull.py"} --update '
                '--apply, then finish again')
    if not behind:
        return f'{problem}Fix it, then finish again'
    first = (('Another session changed the same files on origin/main since this branch started ('
             + '; '.join(shared) + '). Read its commits with git log -p HEAD..origin/main -- FILE and reconcile '
             'the two designs before you rebase, because the rebase hides them from the overlap check. ')
             if shared else '')
    rebases = ' and '.join(f'git -C {tree} rebase origin/main' for tree in behind)
    return (f'{problem}This branch is older than the engine: {", ".join(map(str, behind))} lacks commits the '
            f'engine holds, and the setup may load once the branch has them. {first}Commit the work in each such '
            f'worktree with its release message, unless it is committed, run {rebases}, settle any conflict it '
            'stops on and git rebase --continue, then finish again')


def unpinned(edited, entries, refresh_owned):
    """The edited owned sources this finish was not told to pin again."""
    accepted = set(entries) if refresh_owned else set()
    return {name: path for name, path in edited.items() if name not in accepted}


def git_bytes(tree, *args, passing=(0,), environment=None, data=None):
    """A git command's output as bytes, or a stop with git's own words when it exits outside passing."""
    done = subprocess.run([*GIT, '-C', str(tree), *args], capture_output=True, timeout=120, input=data,
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


def origin_heads(public, private):
    for root in (public, private):
        fetch(root)
    return Heads(git(public, 'rev-parse', 'origin/main'), git(private, 'rev-parse', 'origin/main'))


def recover_publication(public, private, base, origins):
    saved = read_receipt(public, private, base)
    if saved is None:
        return False
    public_out = holds(public, saved.commits.public, origins.public)
    private_out = holds(private, saved.commits.private, origins.private)
    if saved.paired and public_out and not private_out and holds(private, saved.source.private, origins.private):
        pointer = public_pointer(git_bytes(private, 'show', f'{origins.private}:release.toml'))[2].decode()
        # The pointed-to object may not be among the fetched public history after a force-push elsewhere.
        exists = git_bytes(public, 'cat-file', '--batch-check=%(objecttype)', data=(pointer + '\n').encode())
        private_out = exists.strip() == b'commit' and holds(public, saved.commits.public, pointer)
    complete = private_out and (public_out or not saved.paired)
    note = public_pushed(base)
    if saved.paired and public_out:
        if complete:
            if note.is_file() and note.read_text(encoding='utf-8').strip() == saved.commits.public:
                note.unlink()
        else:
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(saved.commits.public + '\n', encoding='utf-8')
    return complete


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
    # -z, because git otherwise quotes a name that is not ASCII, and such a file would not be run. A file the
    # release deletes is still listed until the commit.
    named = [name for name in git(private, 'ls-files', '-co', '--exclude-standard', '-z').split('\0')
             if Path(name).name.startswith('test_') and name.endswith('.py')]
    if broken := [name for name in named if (private / name).is_symlink() and not (private / name).exists()]:
        raise RuntimeError(f'these private test file links point at nothing, so their tests would not run: '
                           f'{", ".join(broken)}; point each at its file or delete it')
    files = sorted(name for name in named if (private / name).is_file())
    public_suite(public, log)
    if not files:
        return
    private_log = log.with_name('private-tests.log')
    absent = log.parent / 'absent-config'
    # Without colour, because with FORCE_COLOR set unittest colours the verdict that UNITTEST_END reads. The files
    # run with -I, but the Python runs they start read PYTHON_COLORS before NO_COLOR.
    environment = {name: value for name, value in os.environ.items() if name != 'PYTHON_COLORS'} | {
        'CODEX_HOME': str(absent / 'codex'), 'CLAUDE_CONFIG_DIR': str(absent / 'claude'),
        'PYTHONDONTWRITEBYTECODE': '1', 'NO_COLOR': '1'}

    def run_alone(name):
        try:
            return subprocess.run([sys.executable, *ISOLATED_RUN, name], cwd=private, env=environment,
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding='utf-8',
                                  errors='replace', timeout=PRIVATE_TEST_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as late:
            # On Linux and macOS subprocess hands back a timed-out run's output as bytes, even with text=True.
            stdout, stderr = (part.decode('utf-8', 'replace') if isinstance(part, bytes) else part or ''
                              for part in (late.stdout, late.stderr))
            return subprocess.CompletedProcess(late.cmd, None, stdout, stderr)

    problems, skips, total = [], [], 0
    keep(private_log)
    with private_log.open('w', encoding='utf-8') as output, \
            ThreadPoolExecutor(min(PRIVATE_TESTS_AT_ONCE, os.cpu_count() or 1)) as pool:
        for name, result in zip(files, pool.map(run_alone, files)):
            output.write(f'== {name}\n{result.stdout}{result.stderr}')
            ends = UNITTEST_END.findall(result.stderr)
            if result.returncode is None:
                problems.append(f'{name} ran past the {PRIVATE_TEST_TIMEOUT_SECONDS} s limit')
            # unittest.main(exit=False) exits 0 on a failed test.
            elif result.returncode or (ends and ends[-1][1] != 'OK'):
                tests = ', '.join(dict.fromkeys(UNITTEST_FAILURE.findall(result.stderr)))
                problems.append(f'the private tests in {name} failed' + (f' ({tests})' if tests else ''))
            elif not ends:
                problems.append(f'{name} ran no tests as {Path(sys.executable).name} {" ".join(ISOLATED_RUN)} {name}, '
                                'so end it with unittest.main() under if __name__ == "__main__"')
            else:
                total += int(ends[-1][0])
                if skip := UNITTEST_SKIPPED.search(ends[-1][2]):
                    skips.append(f'{name} {skip[1]}')
    if problems:
        raise RuntimeError(f'{"; ".join(problems)}; read {private_log}')
    folders = {str(Path(name).parent) for name in files}
    skipped = f'; skipped tests: {", ".join(skips)}' if skips else ''
    print(f'Ran {total} private tests in {len(folders)} folders: OK{skipped}; log {private_log}', flush=True)


def device_check(public, private, state):
    """Stop on a release that would stop the pull, which every device would then hit. Sync exits 1 when it
    only has changes to apply, as every release does, but so does a crash, which writes no result.
    Name each Claude plugin cache that running sessions hold: the pull swaps it at once when only its skills'
    Markdown text changes, and otherwise applies the rest of the release and replaces it once they exit."""
    result = subprocess.run([sys.executable, '-B', str(public / 'sync.py'), '--candidate', '--json', '--public',
                             str(public / 'setup'), '--private', str(private / 'setup')],
                            cwd=public, capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=1800)
    stopped = f'sync exited {result.returncode}'
    try:
        candidate = candidate_result.read(result.stdout)
    except ValueError as error:
        candidate = None
        if result.returncode in (0, 1):
            stopped += f'; {error}' if result.stdout.strip() else ' and printed no result'
    if result.returncode not in (0, 1) or candidate is None or candidate.blocking:
        sys.stderr.write((result.stdout if candidate is None else '') + result.stderr)
        raise RuntimeError(f'the pull on this device would stop on this release ({stopped}); {state}')
    for held in candidate.held:
        print(f'{held.lease}; the pull here swaps it at once if only skill text changes, and otherwise applies the '
              'rest of the release and replaces that cache once they exit',
              flush=True)


def push(root, commit):
    git(root, *CREDENTIALS, 'push', '-q', push_url(root), f'{commit}:main')


def start(args, public_repo, private_repo):
    base, public, private = places(args)
    for repo, tree in ((public_repo, public), (private_repo, private)):
        fetch(repo)
        git(repo, 'worktree', 'add', '-q', '-b', args.name, str(tree), 'origin/main')
    # Both branches are new, so a note here is from an abandoned release of the same name, whose worktrees were
    # removed by hand. A start that stops above, on a branch that is still there, keeps the note for its finish.
    tests_passed(base).unlink(missing_ok=True)
    public_pushed(base).unlink(missing_ok=True)
    retire_candidates(public_repo, private_repo, base)
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
        named = git(tree, 'rev-parse', '--path-format=absolute', '--git-path', 'HEAD')
        raise RuntimeError(f'{tree}: git symbolic-ref exited {head.returncode}: {head.stderr.strip()}; {named} names '
                           f'no branch git can read. Point it at the release branch with git -C {tree} symbolic-ref '
                           f'HEAD refs/heads/{name}, then finish again')
    if head.stdout.strip() == f'refs/heads/{name}':  # --short would print heads/stash beside a stash
        return None
    branch = subprocess.run([*GIT, '-C', str(tree), 'rev-parse', '-q', '--verify', f'refs/heads/{name}'],
                            capture_output=True, text=True, timeout=60)
    if branch.returncode not in (0, 1) or branch.stderr.strip():  # a broken ref file warns and exits 1 too
        raise RuntimeError(f'{tree}: git rev-parse --verify refs/heads/{name} exited {branch.returncode}: '
                           f'{branch.stderr.strip()}')
    if branch.returncode:
        return (f'{tree} has no release branch {name}; git -C {tree} log origin/main..HEAD lists the commits HEAD '
                f'holds that origin/main lacks. Where HEAD holds the release, make the branch there with git -C {tree} '
                f'switch -c {name}, then finish again')
    if not head.stdout.strip() and holds(tree, f'refs/heads/{name}'):
        return (f'{tree} is on a detached HEAD that holds its release branch {name} and may hold commits of its '
                f'own; move the branch there with git -C {tree} switch -C {name}, then finish again')
    return (f'{tree} is off its release branch {name}; git -C {tree} log {name}..HEAD lists the commits only HEAD '
            f'holds. Bring those you want onto {name}, switch to it, then finish again')


def release_checks(args, public, private, private_repo, *, final=False, hooks=True):
    if stop := review_stop(public, private, args.reviewed, args.name):
        raise RuntimeError(stop)
    if reused := reused_release_names(public, private):
        raise RuntimeError(f'{", ".join(reused)} changed its selection at the same upstream commit and packaging number, '
                           'so every machine\'s pull would stop with "Existing release differs". Raise packaging by '
                           'one in its setup/packages file, then finish again')
    if stale := unpinned(edited_owned(public, private, private_repo), [] if final else args.entry,
                        args.refresh_owned and not final):
        flags = ' '.join(f'--entry {name}' for name in sorted(stale))
        raise RuntimeError(f'{", ".join(sorted(stale.values()))} changed after its tree was pinned, so every '
                           f"machine's pull would stop on a stale lock. Finish again with {flags} --refresh-owned")
    if hooks and not args.hooks_reapproved and (changed := reapprovals(public, private)):
        raise RuntimeError(f'this release may change the Codex content of {", ".join(changed)}, whose Codex hooks '
                           'are trusted; ask the user to approve them again, queue each machine\'s approval in '
                           'maintenance/pending-sync.md, then finish again with --hooks-reapproved')
    if hits := leaks(public, private, args.message):
        raise RuntimeError(f'{", ".join(hits[:20])} in {public} hold a string that the private {DENYLIST} names; '
                           'take it out of the public side, then finish again')
    dropped_stop(public, private)


def source_commits(args, public, private, written, *, generated=False):
    if generated:
        for tree in (public, private):
            if other := sorted(uncommitted(tree) - written[tree]):
                raise RuntimeError(f'{tree}: {", ".join(other)} changed after the commit; only relock outputs may '
                                   'be committed here; preserve or remove the edit, then finish again')
    if stop := unnamed_commit(args, public, private, written):
        raise RuntimeError(stop)
    for tree, message in ((public, args.message), (private, args.private_message)):
        if relock_only(tree, written[tree]):
            amend(tree, written[tree])
        elif dirty(tree):
            commit(tree, message)


def finish_tail(args, public_repo, private_repo, draft, *, published, origins=None):
    superseded = published and origins is not None and not all(
        holds(root, getattr(draft.commits, side), getattr(origins, side))
        for side, root in (('public', public_repo), ('private', private_repo)))
    state = 'covered by a later pair' if superseded else 'pushed' if published else 'not pushed'
    print(f'public {draft.commits.public}  private {draft.commits.private}  {state}', flush=True)
    if not published:
        return 0
    if superseded:
        actual = Trees(*(git(root, 'rev-parse', f'{getattr(origins, side)}^{{tree}}')
                         for side, root in (('public', public_repo), ('private', private_repo))))
        print(f'Recorded tested trees: public {draft.trees.public}; private {draft.trees.private}', flush=True)
        print(f'Origin trees now: public {actual.public}; private {actual.private}', flush=True)
    else:
        print(f'Exact trees pushed: public {draft.trees.public}; private {draft.trees.private}', flush=True)
    if not args.push:
        return 0
    for line in other_machines(tailnet(), windows_side()):
        print(line, flush=True)
    if args.pull:
        if not wait(('pull', 'sync'), WAIT_FOR_PULL):
            raise RuntimeError(f'pushed, but another pull still runs here after {WAIT_FOR_PULL // 60} minutes; '
                               'pull once it ends')
        result = subprocess.run([sys.executable, '-B', str(public_repo / 'pull.py'), '--root', str(public_repo),
                                 '--private', str(private_repo), '--update', '--apply', '--wait', '0'],
                                cwd=public_repo, timeout=1800)
        if result.returncode == 0 and (args.entry or args.package):
            subprocess.run([sys.executable, '-B', str(public_repo / 'skill_listing.py')], cwd=public_repo, timeout=900)
        return result.returncode
    return 0


def finish(args, public_repo, private_repo, *, rounds=ATTEMPTS):
    base, public, private = places(args)
    loaded, began = head(public_repo), None
    published, uncertain, prepared = False, False, False

    def stopped():
        state = ('publication state could not be verified' if uncertain else
                 'the pair is already published' if published else push_state(base))
        if began is None:
            return f'this run committed nothing; {state}'
        local = Heads(head(public), head(private))
        if not prepared and local == began and rounds == ATTEMPTS:
            return f'this run committed nothing; {state}'
        return f'Local commits remain at public {local.public}, private {local.private}; {state}'

    def locked(remaining):
        held = hold(machine_locks()['release'], args.timeout, lambda: head(public_repo) != loaded, stopped=stopped())
        if held is None:
            raise Restart('because a pull changed the engine while this finish ran or waited', remaining)
        return held

    repinned = args.entry if args.refresh_owned else []
    setups = ['--public', str(public / 'setup'), '--private', str(private / 'setup')]
    entries = [part for entry in args.entry for part in ('--entry', entry)]
    lock = [sys.executable, '-B', 'lock_setup.py', *setups, '--output', str(base / 'build/lock'), *entries,
            *(['--refresh-owned'] if args.refresh_owned else []), '--write']
    try:
        for remaining in range(rounds - 1, -1, -1):
            if running(('pull', 'sync')):
                if not wait(('pull', 'sync'), args.timeout):
                    raise RuntimeError(f'another pull or sync still runs here after {args.timeout:g} seconds')
                raise Restart('after the wait', remaining)
            import description, description_toml, drift, host_state  # noqa: F401
            if stops := [stop for tree in (public, private) if (stop := unfinished(tree, args.name))]:
                raise RuntimeError('; '.join(stops))
            if began is None:
                began = Heads(head(public), head(private))
            unmerged = [f'{tree.name}: {", ".join(sorted(set(files)))}' for tree in (public, private)
                        if (files := git(tree, 'diff', '--name-only', '--diff-filter=U').splitlines())]
            if unmerged:
                raise RuntimeError(f'files in a conflict that is not settled ({"; ".join(unmerged)}); settle each one: take '
                                   'out its conflict markers and git add it, or, where one side deleted it, git add it to keep '
                                   'it or git rm it to delete it, then finish again')
            try:
                origins = origin_heads(public, private)
            except RuntimeError:
                edited_owned(public, private, private_repo)
                raise
            published = recover_publication(public, private, base, origins)
            if published and all(not dirty(root) and not ahead(root, getattr(origins, side))
                                 for side, root in (('public', public), ('private', private))):
                dropped_stop(public, private)
                saved = read_receipt(public, private, base)
                with locked(remaining):
                    retire_candidates(public, private, base)
                    return finish_tail(args, public_repo, private_repo, saved, published=True, origins=origins)
            paired = ahead(public, origins.public) or dirty(public) or half_published(base, public)
            for side, tree in (('public', public), ('private', private)):
                if not dirty(tree) and not ahead(tree, getattr(origins, side)):
                    try:
                        git(tree, 'merge', '-q', '--ff-only', '--no-overwrite-ignore', getattr(origins, side))
                    except RuntimeError as error:
                        raise RuntimeError(f'{tree} has no work of its own to rebase, but finish could not bring it up to '
                                           f'origin/main: {error}') from error
            release_checks(args, public, private, private_repo, hooks=False)
            if args.entry:
                run(lock, public)
            if args.package:
                packages = [part for package in args.package for part in ('--package', package)]
                run([sys.executable, '-B', 'build_setup.py', *setups, '--output', str(base / 'build/build'),
                     *packages], public)
            release_checks(args, public, private, private_repo)
            regenerated = {tree: frozenset({LOCK}) | pin_only(tree, repinned) if args.entry else frozenset()
                           for tree in (public, private)}
            written = {tree: relock_writes(tree, regenerated[tree], repinned) for tree in (public, private)}
            if stop := origin_stop(public, private, regenerated, args.overlap_reviewed, origins=origins,
                                   paired=paired or dirty(public)):
                raise RuntimeError(stop)
            if stop := unnamed_commit(args, public, private, written):
                raise RuntimeError(stop)
            before = {tree: (head(tree), content(tree), git(tree, 'write-tree')) for tree in (public, private)}
            with locked(remaining):
                dropped_stop(public, private)
                for tree in (public, private):
                    if (head(tree), content(tree), git(tree, 'write-tree')) != before[tree]:
                        raise RuntimeError(f'{tree}: files or index changed while waiting; the edit is preserved; '
                                           'finish again once the edits are done')
                if stop := origin_stop(public, private, regenerated, args.overlap_reviewed, origins=origins,
                                       paired=paired or dirty(public)):
                    raise RuntimeError(stop)
                prepared = True
                source_commits(args, public, private, written)
                paired = paired or ahead(public, origins.public)
                moved = [rebase(tree, regenerated[tree], onto=getattr(origins, side), paired=paired and side == 'private')
                         for side, tree in (('public', public), ('private', private))]
                if any(moved) and args.entry:
                    run(lock, public)
                    written = {tree: relock_writes(tree, frozenset({LOCK}), repinned) for tree in (public, private)}
                    source_commits(args, public, private, written, generated=True)
                    paired = paired or ahead(public, origins.public)
                if not paired and not ahead(private, origins.private):
                    raise RuntimeError('nothing to release: both branches match origin/main')
                source = Heads(head(public), head(private))
                unchanged(public, private, source)
            with candidate(public, private, base, source=source, paired=paired, message=args.pair_message) as draft:
                release_checks(args, draft.public, draft.private, private_repo, final=True)
                certified = tested(draft, base)
                retire_candidates(public, private, base, keep=draft.public.parent)
                with locked(remaining):
                    unchanged(public, private, source)
                    current_origins = origin_heads(public, private)
                    if any(not holds(root, getattr(current_origins, side), getattr(certified.candidate.commits, side))
                           for side, root in (('public', public), ('private', private))):
                        print('Origin moved; preparing and testing a new complete candidate', flush=True)
                        continue
                    device_check(draft.public, draft.private, stopped())
                    if stop := review_stop(draft.public, draft.private, args.reviewed, args.name):
                        raise RuntimeError(stop)
                    unchanged(draft.public, draft.private, draft.commits)
                    unchanged(public, private, source)
                    if not args.push:
                        return finish_tail(args, public_repo, private_repo, draft, published=False)
                    try:
                        for side, tree in (('public', public), ('private', private)):
                            git(tree, *CREDENTIALS, 'push', '--dry-run', '-q', push_url(tree),
                                f'{getattr(certified.candidate.commits, side)}:main')
                        unchanged(draft.public, draft.private, draft.commits)
                        unchanged(public, private, source)
                        if draft.commits.public != current_origins.public:
                            push(public, certified.candidate.commits.public)
                            if paired:
                                public_pushed(base).write_text(draft.commits.public + '\n', encoding='utf-8')
                        push(private, certified.candidate.commits.private)
                    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                        uncertain = True
                        refreshed = origin_heads(public, private)
                        published = recover_publication(public, private, base, refreshed)
                        uncertain = False
                        if not published:
                            if MOVED.search(str(error)):
                                continue
                            raise RuntimeError(f'push failed: {error}') from error
                    else:
                        uncertain = True
                        refreshed = origin_heads(public, private)
                        published = recover_publication(public, private, base, refreshed)
                        uncertain = False
                        if not published:
                            raise RuntimeError('push returned success but origin does not contain the tested pair; '
                                               'finish again to reconcile it')
                    retire_candidates(public, private, base)
                    return finish_tail(args, public_repo, private_repo, draft, published=True, origins=refreshed)
        raise RuntimeError(f'Origin or the engine changed through all {ATTEMPTS} rounds. '
                           'Finish again to prepare and test a new pair')
    except Restart as why:
        if not why.remaining:
            raise RuntimeError(f'Origin or the engine changed through all {ATTEMPTS} rounds. '
                               f'Finish again to prepare and test a new pair; {stopped()}') from why
        raise
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f'{error}; {stopped()}') from error


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
    if tests_passed(base).exists():
        recover_publication(public_repo, private_repo, base, origin_heads(public_repo, private_repo))
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
    retire_candidates(public_repo, private_repo, base)
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
    except Exception:
        traceback.print_exc()
        code = CRASHED
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
    # Releases are made on WSL or Omarchy. status and wait only read the machine they run on, such as its locks,
    # and agents on the PC read its own that way. This comes before the --report file and any read, lock, fetch or
    # write.
    if sys.platform == 'win32' and args.action not in ('status', 'wait'):
        print('Release stopped: run this from WSL or Omarchy, not from Windows itself; nothing was changed',
              file=sys.stderr)
        return 3
    if args.action in ('start', 'finish', 'review', 'clean', 'result') and not args.name:
        parser.error(f'{args.action} needs a NAME')
    if args.report:
        report.append(args.report)
    try:
        budget = os.environ.pop(ROUND_BUDGET, str(ATTEMPTS))
        if budget not in {str(value) for value in range(1, ATTEMPTS + 1)}:
            raise RuntimeError('invalid finish round budget; start a fresh finish without the private budget variable')
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
                    return finish(args, ENGINE, args.private.resolve(), rounds=int(budget))
            except Restart as why:
                print(f'Starting again {why}, to run the engine as it is now', flush=True)
                sys.stderr.flush()
                os.execve(sys.executable, [sys.executable, '-B', str(ENGINE / 'release.py'),
                                           *(sys.argv[1:] if argv is None else argv)],
                          {**os.environ, ROUND_BUDGET: str(why.remaining)})
        action = {'start': start, 'review': review, 'clean': clean, 'status': status}[args.action]
        if args.action in ('start', 'clean'):
            with one_finish(args):
                return action(args, ENGINE, args.private.resolve())
        return action(args, ENGINE, args.private.resolve())
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f'Release stopped: {error}', file=sys.stderr)
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
