"""Run a command whose agent sessions read a draft SKILL.md in place of the installed one, so that skill_triggers.py
and codex_triggers.py measure a reworded description before its release instead of after it is live.

The command runs in a private user and mount namespace, with the draft bound read-only over each installed copy of
the skill. Nothing on disk changes, and every other process keeps reading the installed copy. The copies are
skills/NAME/SKILL.md under $CLAUDE_CONFIG_DIR (or ~/.claude), ~/.agents and $CODEX_HOME (or ~/.codex), where NAME is
the name in the draft's frontmatter. The run fails when a copy no longer reads as the draft at the end, such as after
a pull during the run. Needs Linux, WSL included, and Python 3.12 or newer. Usage:

    python3 -I skill_draft.py DRAFT_SKILL.md [--installed PATH ... | --new] -- COMMAND [ARG ...]
    python3 -I skill_draft.py DRAFT_SKILL.md -- python3 -I codex_triggers.py QUERIES.json OUT.json

--installed, which may repeat, names the copies to replace instead, such as a plugin's. --new measures a skill that
is not installed yet: the command sees it added as skills/NAME/SKILL.md under $CLAUDE_CONFIG_DIR (or ~/.claude) and
~/.agents, in each of the two skills folders that exists, through an overlay whose changes live only in memory.
"""

import argparse
import ctypes
import os
import signal
import subprocess
import sys
from pathlib import Path

MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC, MS_REMOUNT, MS_BIND = 1, 2, 4, 8, 32, 4096


def skill_name(text):
    lines = text.splitlines()
    if lines[:1] != ['---'] or '---' not in lines[1:]:
        return ''
    names = [line.partition(':')[2] for line in lines[1:lines.index('---', 1)] if line.startswith('name:')]
    return names[0].strip().strip('\'"') if len(names) == 1 else ''


def default_copies(name):
    claude = os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude'
    codex = os.environ.get('CODEX_HOME') or Path.home() / '.codex'
    return [Path(root, 'skills', name, 'SKILL.md') for root in (claude, Path.home() / '.agents', codex)]


def mount(libc, step, target, source=None, kind=None, flags=0, data=None):
    if libc.mount(source, os.fsencode(target), kind, flags, data):
        raise OSError(f'The kernel refused {step}: {os.strerror(ctypes.get_errno())}')


def bind_read_only(libc, draft, path):
    mount(libc, f'to bind {draft} over {path}', path, os.fsencode(draft), flags=MS_BIND)
    # A bind turns read-only only on a remount, which may not drop these flags of the draft's own mount. statvfs
    # reports them with the mount call's values. Without the remount, a session's write to the copy lands in the draft.
    kept = os.statvfs(path).f_flag & (MS_NOSUID | MS_NODEV | MS_NOEXEC)
    mount(libc, f'to make the bind over {path} read-only', path, flags=MS_REMOUNT | MS_BIND | MS_RDONLY | kept)


def add_empty_file(libc, path):
    folder = path.parent.parent
    lower = os.open(folder, os.O_PATH)
    mount(libc, f'to mount a tmpfs on {folder}', folder, b'tmpfs', b'tmpfs')
    (folder / 'upper').mkdir()
    (folder / 'work').mkdir()
    options = f'lowerdir=/proc/self/fd/{lower},upperdir={folder}/upper,workdir={folder}/work'
    mount(libc, f'to mount an overlay on {folder}', folder, b'overlay', b'overlay', data=os.fsencode(options))
    try:
        path.parent.mkdir()
    except FileExistsError:
        raise OSError(f'{path.parent} exists, though the checks did not find it, so --new cannot add the skill there. '
                      'Run the tool again, without --new if the skill is installed now.') from None
    path.touch()


def copies_and_searched(args, draft):
    if args.installed:
        if missing := [str(path) for path in args.installed if not path.is_file()]:
            sys.exit(f'--installed names no file at {", ".join(missing)}')
        return args.installed, []
    name = skill_name(draft.decode('utf-8', 'replace'))
    if not name or '/' in name or '\0' in name or name in ('.', '..'):
        sys.exit(f'The draft {args.draft} needs a name: line in its frontmatter that names a skill folder.')
    searched = default_copies(name)
    copies = [path for path in searched if path.is_file()]
    if args.new:
        if copies:
            sys.exit(f'The skill {name} is installed at {", ".join(map(str, copies))}. Drop --new to measure the '
                     'draft in place of the installed copies.')
        # Not <codex home>/skills, because Codex reads ~/.agents/skills too and would list the skill twice.
        copies = [path for path in searched[:2] if path.parent.parent.is_dir()]
        if not copies:
            sys.exit(f'--new adds the skill to {" and ".join(str(path.parent.parent) for path in searched[:2])}, '
                     'and neither folder exists.')
        if taken := [str(path.parent) for path in copies if os.path.lexists(path.parent)]:
            sys.exit(f'{", ".join(taken)} exists without a SKILL.md, so --new cannot add the skill {name} there.')
    elif not copies:
        sys.exit(f'The skill {name} is installed at none of {", ".join(map(str, searched))}. A plugin\'s copy '
                 'needs --installed PATH, and a skill that is not installed yet needs --new.')
    return copies, searched


def difference(path, draft):
    try:
        return '' if path.read_bytes() == draft else 'held other text'
    except OSError as error:
        return f'could not be read ({error})'


def main():
    argv = sys.argv[1:]
    split = argv.index('--') if '--' in argv else len(argv)
    parser = argparse.ArgumentParser(
        usage='%(prog)s DRAFT_SKILL.md [--installed PATH ... | --new] -- COMMAND [ARG ...]',
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('draft', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--installed', type=Path, action='append', help='an installed SKILL.md to replace')
    mode.add_argument('--new', action='store_true', help='add the skill, which is not installed yet')
    args = parser.parse_args(argv[:split])
    command = argv[split + 1:]
    if not command:
        parser.error('put -- and the command to run after the draft')
    try:
        draft = args.draft.read_bytes()
    except OSError as error:
        sys.exit(f'Cannot read the draft {args.draft}: {error}')
    copies, searched = copies_and_searched(args, draft)
    if not args.new and any(path.samefile(args.draft) for path in copies):
        sys.exit(f'The draft {args.draft} is an installed copy itself, so the run would measure the installed '
                 'description. Edit a copy of it and pass that as the draft.')
    if not hasattr(os, 'unshare'):
        sys.exit('skill_draft.py needs os.unshare, so Linux and Python 3.12 or newer. WSL counts.')
    uid, gid = os.getuid(), os.getgid()
    try:
        os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
        Path('/proc/self/setgroups').write_text('deny')
        Path('/proc/self/uid_map').write_text(f'{uid} {uid} 1')
        Path('/proc/self/gid_map').write_text(f'{gid} {gid} 1')
    except OSError as error:
        sys.exit(f'The kernel refused a private user and mount namespace: {error}')
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        overlaid = set()
        for path in copies:
            folder = path.parent.parent.resolve()
            # The two skills folders can be one folder through a link, and then the first pass added the file. When
            # the file exists for any other reason, the checks did not see it, and the command must not run without
            # the overlay.
            if args.new and folder not in overlaid:
                add_empty_file(libc, path)
                overlaid.add(folder)
            bind_read_only(libc, args.draft, path)
        if args.new:
            # A working folder inside a skills folder is still the folder under the overlay until it is entered again.
            os.chdir(os.getcwd())
    except OSError as error:
        sys.exit(str(error))
    if lost := [f'{path} {problem}' for path in copies if (problem := difference(path, draft))]:
        sys.exit(f'{", ".join(lost)} once the draft was placed there, so the command was not started.'
                 + ' One cause is a skills folder that lies inside the other.' * args.new)
    for path in copies:
        where = f'as {path}, a skill added for the command only' if args.new else f'in place of {path}'
        print(f"The command's sessions read {args.draft} {where}", file=sys.stderr)
    early = []
    for stop in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(stop, lambda number, frame: early.append(number))
    # A terminal's Ctrl-C reaches the command too, so the tool waits for the command's own exit status.
    signal.signal(signal.SIGINT, lambda number, frame: None)
    try:
        process = subprocess.Popen(command)
    except OSError as error:
        sys.exit(f'Cannot run {command[0]}: {error}')
    for stop in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(stop, lambda number, frame: process.send_signal(number))
    for number in early:
        process.send_signal(number)
    status = process.wait()
    status = 128 - status if status < 0 else status
    problems = {path: difference(path, draft) for path in (args.draft, *copies)}
    problems |= {path: 'was installed during the run' for path in searched if path not in copies and path.is_file()}
    problems = {path: problem for path, problem in problems.items() if problem}
    for path, problem in problems.items():
        print(f'{path} {problem} when the command ended, so some sessions may have read another description than the '
              'draft the run started with. Measure again.', file=sys.stderr)
    return (status or 1) if problems else status


if __name__ == '__main__':
    sys.exit(main())
