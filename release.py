"""Release reviewed changes as a signed public/private pair, push both, and pull them on this device.

Run it with the engine's own Python from the canonical public checkout. `start NAME` makes a worktree of
each repository at origin/main on branch NAME. Edit there. `finish NAME` locks and builds the named
entries, runs the public test suite, stops on new files the .gitignore allowlist would leave out, commits
both worktrees, stops if origin/main changed the same files meanwhile, rebases them onto it, pairs the
private release.toml with the public commit, checks that this device's pull could apply the pair, checks
both pushes with --dry-run, pushes, and pulls. `clean NAME` removes the worktrees and branches once
origin/main has them.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from portable import GIT
from pull import git

ENGINE = Path(__file__).resolve().parent
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
# The public .gitignore is an allowlist: this pattern drops every path that a later ! line does not bring back.
ALLOWLIST_BASE = '/*'


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


def test_suite(public, log):
    """Run every public test, not just the ones the change looks like it touches. The config roots point at a
    missing folder, as in CI, because agent sessions export CODEX_HOME at this device's real config."""
    log.parent.mkdir(parents=True, exist_ok=True)
    absent = log.parent / 'absent-config'
    environment = {**os.environ, 'CODEX_HOME': str(absent / 'codex'), 'CLAUDE_CONFIG_DIR': str(absent / 'claude')}
    with log.open('w', encoding='utf-8') as output:
        result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests'], cwd=public,
                                env=environment, stdout=output, stderr=subprocess.STDOUT, timeout=1800)
    if result.returncode:
        raise RuntimeError(f'the test suite failed; read {log}')


def device_check(public, private):
    """Stop on a release that would stop the pull, which every device would then hit. Sync exits 1 when it
    only has changes to apply, as every release does, but so does a crash, which skips the closing summary."""
    result = subprocess.run([sys.executable, '-B', str(public / 'sync.py'), '--candidate', '--public',
                             str(public / 'setup'), '--private', str(private / 'setup')],
                            cwd=public, capture_output=True, text=True, encoding='utf-8', timeout=1800)
    if result.returncode not in (0, 1) or 'Managed changes:' not in result.stdout:
        sys.stderr.write(result.stdout + result.stderr)
        raise RuntimeError(f'the pull on this device would stop on this release (sync exited {result.returncode}); nothing was pushed')


def push(root):
    git(root, *CREDENTIALS, 'push', '-q', push_url(root), 'HEAD:main')


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
    test_suite(public, base / 'build/tests.log')
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
    parser.add_argument('--push', action='store_true', help='push both branches to origin/main')
    parser.add_argument('--overlap-reviewed', action='store_true',
                        help='finish although origin changed files this branch changed, after reading those commits')
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
