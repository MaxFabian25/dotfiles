"""Release reviewed changes as a signed public/private pair, push both, and pull them on this device.

Run it with the engine's own Python from the canonical public checkout. `start NAME` makes a worktree of
each repository at origin/main on branch NAME. Edit there. `finish NAME` locks and builds the named
entries, stops on a changed plugin whose Codex hooks are trusted until --hooks-reapproved, runs the public
test suite unless the release changes no code, runs the private folders' tests, stops on new files the .gitignore allowlist would leave out, commits both worktrees, stops if
origin/main changed the same files meanwhile, rebases them onto it, pairs the private release.toml with the
public commit, checks that this device's pull could apply the pair, naming each Claude plugin cache that
running sessions hold, checks both pushes with --dry-run, pushes, and pulls, after any other pull here ends.
Before all of that it stops on a package whose selection changed without a new packaging number.
One finish runs at a time on a machine; another stops and names it. `clean NAME` removes the worktrees and branches once origin/main has them. `status` lists what runs here and each open
release worktree with the files it changes, and `start` lists the others. `wait --timeout SECONDS` returns
once no release, pull or sync runs on this machine.
"""
from __future__ import annotations

import argparse
import contextlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tomllib

from portable import GIT, codex_home, exclusive_lock, lock_holder, native_cli_environment
from pull import git

ENGINE = Path(__file__).resolve().parent
STATE = Path.home() / '.local/state/dotfiles'
WAIT_FOR_PULL = 1200  # seconds a finish waits for another pull on this machine before its own
PUBLIC_COMMIT = re.compile(r'^public_commit = ".*"$', re.MULTILINE)
# Push over HTTPS with the GitHub CLI's login, which the Mac and WSL hold, whatever helper or SSH origin the
# checkout configures. The private checkout's SSH origin uses a read-only deploy key.
GITHUB_SSH = re.compile(r'^(?:git@github\.com:|ssh://git@github\.com/)')
CREDENTIALS = ('-c', 'credential.https://github.com.helper=',
               '-c', 'credential.https://github.com.helper=!gh auth git-credential')
ATTEMPTS = 3
MOVED = re.compile(r'\((?:fetch first|non-fast-forward)\)')
# Files every release rewrites, so both sides changing them says nothing about clashing work.
PAIRING_FILES = frozenset({'release.toml', 'setup/lock.toml'})
LOCK = 'setup/lock.toml'
SETUP = 'setup/setup.toml'
LEASE = 'Claude lease: '
# The public .gitignore is an allowlist: this pattern drops every path that a later ! line does not bring back.
ALLOWLIST_BASE = '/*'
# Strings the public tree must never hold, such as host and account names. The list is private, because a public
# list would publish them.
DENYLIST = 'config/privacy/public-denylist.txt'


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


def hold(path):
    """This finish's hold on the machine's release lock, or a stop that names the release holding it."""
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(exclusive_lock(path, record=True))
    except BlockingIOError as error:
        who = '; '.join(running(('release',))) or f'it holds {path}'
        raise RuntimeError(f'another release is finishing on this machine ({who}); run release.py wait --timeout '
                           'SECONDS, then finish again') from error
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
    found = releases(args, public_repo, private_repo)
    print(f'Release worktrees under {args.worktrees}:' if found else f'No release worktrees under {args.worktrees}.')
    for name, side, text in found:
        print(f'  {name}/{side}: {text}')
    return 0


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


def dropped(root):
    """New paths the .gitignore allowlist leaves out, which a commit would silently miss. Cache rules ignore
    paths too, but on purpose."""
    ignored = git(root, 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory').splitlines()
    if not ignored:
        return []
    matches = (line.split('\t', 1) for line in git(root, 'check-ignore', '-v', '--', *ignored).splitlines())
    return [path for source, path in matches if source.split(':', 2)[2] == ALLOWLIST_BASE]


def leaks(public, private, message=None):
    """Where the public tree, as a commit would take it, or its commit message holds a string the private
    denylist names. One string per line, matched without regard to case; # starts a comment."""
    listed = private / DENYLIST
    if not listed.is_file():
        return []
    patterns = [line.strip() for line in listed.read_text(encoding='utf-8').splitlines()
                if line.strip() and not line.strip().startswith('#')]
    if not patterns:
        return []
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


def commit(root, message):
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-F', str(message.resolve()))


def rebase(root, regenerated=frozenset()):
    """Replay this branch onto origin/main, which another session may have moved since start. A conflict only in
    files the caller rebuilds afterwards, as finish relocks setup/lock.toml, takes origin's copy. Returns '' when
    the branch already holds origin/main, 'regenerate' when it took such a copy, and 'rebased' otherwise."""
    git(root, 'fetch', '--prune', 'origin')
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


def overlap(root):
    """Files origin/main changed since this branch left it that this branch changed too. A clean rebase can
    still join two sessions' clashing designs for the same file."""
    git(root, 'fetch', '--prune', 'origin')
    fork = git(root, 'merge-base', 'HEAD', 'origin/main')
    theirs = set(git(root, 'diff', '--name-only', fork, 'origin/main').splitlines())
    ours = set(git(root, 'diff', '--name-only', fork, 'HEAD').splitlines())
    return sorted((theirs & ours) - PAIRING_FILES)


def started(root, path):
    """A TOML file as this branch found it on origin/main, or {} where it was absent."""
    fork = git(root, 'merge-base', 'HEAD', 'origin/main')
    return tomllib.loads(git(root, 'show', f'{fork}:{path}')) if git(root, 'ls-tree', fork, path) else {}


def current(root, path):
    return tomllib.loads((root / path).read_text(encoding='utf-8')) if (root / path).is_file() else {}


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


def public_suite(public, log):
    cache = public / '.sync'
    cached = cache.exists()
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


def device_check(public, private):
    """Stop on a release that would stop the pull, which every device would then hit. Sync exits 1 when it
    only has changes to apply, as every release does, but so does a crash, which skips the closing summary.
    Name each Claude plugin cache that running sessions hold: the pull swaps it at once when only its skills'
    Markdown text changes, and otherwise applies the rest of the release and replaces it once they exit."""
    result = subprocess.run([sys.executable, '-B', str(public / 'sync.py'), '--candidate', '--public',
                             str(public / 'setup'), '--private', str(private / 'setup')],
                            cwd=public, capture_output=True, text=True, encoding='utf-8', timeout=1800)
    if result.returncode not in (0, 1) or 'Managed changes:' not in result.stdout:
        sys.stderr.write(result.stdout + result.stderr)
        raise RuntimeError(f'the pull on this device would stop on this release (sync exited {result.returncode}); nothing was pushed')
    for line in result.stdout.splitlines():
        if line.startswith(LEASE):
            print(f'{line}; the pull here swaps it at once if only skill text changes, and otherwise applies the '
                  'rest of the release and replaces that cache once they exit',
                  flush=True)


def push(root):
    git(root, *CREDENTIALS, 'push', '-q', push_url(root), 'HEAD:main')


def start(args, public_repo, private_repo):
    _, public, private = places(args)
    for repo, tree in ((public_repo, public), (private_repo, private)):
        git(repo, 'fetch', '--prune', 'origin')
        git(repo, 'worktree', 'add', '-q', '-b', args.name, str(tree), 'origin/main')
    print(f'Edit {public} and {private}, then run release.py finish {args.name} --message FILE')
    others = [f'  {name}/{side}: {text}' for name, side, text in releases(args, public_repo, private_repo)
              if name != args.name and text != 'on origin/main and clean']
    if others:
        print('Other open releases; read them before you edit the same files:', *others, sep='\n')
    return 0


def finish(args, public_repo, private_repo):
    base, public, private = places(args)
    if reused := reused_release_names(public, private):
        raise RuntimeError(f'{", ".join(reused)} changed its selection at the same upstream commit and packaging number, '
                           'so its release folder keeps its name and every machine\'s pull would stop with "Existing '
                           'release differs". Raise packaging by one in its setup/packages file, then finish again; '
                           'nothing was committed')
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
                           'take it out of the public side, then finish again; nothing was committed')
    test_suite(public, private, base / 'build/tests.log')
    for tree in (public, private):
        if paths := dropped(tree):
            raise RuntimeError(f'{tree}: .gitignore leaves out {", ".join(paths)}; add a "!" line for each path the '
                               'release needs and delete the rest, then finish again')
    if dirty(public):
        if not args.message:
            raise RuntimeError(f'{public}: pass --message with the public commit message')
        commit(public, args.message)
    if dirty(private):
        if not args.private_message:
            raise RuntimeError(f'{private}: pass --private-message with the private commit message')
        commit(private, args.private_message)
    if not args.overlap_reviewed:
        shared = [f'{tree.name}: {", ".join(files)}' for tree in (public, private) if (files := overlap(tree))]
        if shared:
            raise RuntimeError('another session changed the same files on origin/main since this branch started ('
                               + '; '.join(shared) + '). Read its commits with git log -p HEAD..origin/main -- FILE, '
                               'reconcile the two designs, then finish again with --overlap-reviewed')
    paired = ahead(public)
    regenerated = frozenset({LOCK}) if args.entry else frozenset()
    for attempt in range(1, ATTEMPTS + 1):
        if rebase(public, regenerated) and args.entry:
            run(lock, public)
            amend(public)
        if rebase(private, regenerated) == 'regenerate':
            run(lock, public)
            amend(public)
            amend(private)
        before = git(private, 'rev-parse', 'HEAD')
        if paired:
            pair(public, private, args.pair_message)
        if not ahead(public) and not ahead(private):
            raise RuntimeError('nothing to release: both branches match origin/main')
        device_check(public, private)
        if not args.push:
            break
        published = False
        try:
            for tree in (public, private):  # login and write access on both before either moves
                git(tree, *CREDENTIALS, 'push', '--dry-run', '-q', push_url(tree), 'HEAD:main')
            if ahead(public):
                push(public)
                published = True
            push(private)
            break
        except RuntimeError as error:
            git(private, 'reset', '-q', '--hard', before)
            if not MOVED.search(str(error)):
                state = 'the public commit is on origin without its pair' if published else 'nothing was pushed'
                raise RuntimeError(f'push failed and {state}: {error}') from error
            if attempt == ATTEMPTS:
                raise RuntimeError(f'push failed {ATTEMPTS} times; origin keeps moving: {error}') from error
    print(f'public {git(public, "rev-parse", "--short", "HEAD")}  private {git(private, "rev-parse", "--short", "HEAD")}'
          + ('  pushed' if args.push else '  not pushed'), flush=True)
    if args.pull:
        if not wait(('pull', 'sync'), WAIT_FOR_PULL):
            raise RuntimeError(f'pushed, but another pull still runs here after {WAIT_FOR_PULL // 60} minutes; '
                               'pull once it ends')
        result = subprocess.run([*python, str(public_repo / 'pull.py'), '--root', str(public_repo),
                                 '--private', str(private_repo), '--update', '--apply'], cwd=public_repo, timeout=1800)
        if result.returncode == 0 and (args.entry or args.package):
            subprocess.run([*python, str(public_repo / 'skill_listing.py')], cwd=public_repo, timeout=900)
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
    parser.add_argument('action', choices=('start', 'finish', 'clean', 'status', 'wait'))
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
    parser.add_argument('--hooks-reapproved', action='store_true',
                        help='finish although a plugin with trusted Codex hooks may change, once the user approved them again')
    parser.add_argument('--pull', action='store_true', help='then pull and apply the pair on this device')
    parser.add_argument('--force', action='store_true', help='clean: remove unpushed branches too')
    parser.add_argument('--timeout', type=float, default=1800, help='wait: give up after this many seconds')
    args = parser.parse_args(argv)
    if args.action in ('start', 'finish', 'clean') and not args.name:
        parser.error(f'{args.action} needs a NAME')
    try:
        if args.action == 'wait':
            if wait(None, args.timeout):
                print('No release, pull or sync runs on this machine.')
                return 0
            print(f'Still running after {args.timeout:g} seconds: ' + '; '.join(running()), file=sys.stderr)
            return 3
        if args.action == 'finish':
            with hold(machine_locks()['release']):
                return finish(args, ENGINE, args.private.resolve())
        action = {'start': start, 'clean': clean, 'status': status}[args.action]
        return action(args, ENGINE, args.private.resolve())
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f'Release stopped: {error}', file=sys.stderr)
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
