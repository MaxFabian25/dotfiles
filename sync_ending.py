"""How `sync.py` tells the program that started it how it ended.

A sync that crashes exits 1, as one that leaves managed changes does, so its caller cannot tell the two apart by the
exit status. The caller passes FLAG with a path in a folder made for that one run under the caller's state folder.
sync.py writes its status there as its last act and then exits 0. The caller believes a status only when the sync
exited 0 and the file holds a whole number. Anything else, such as a traceback, a kill, a refused flag or a file that
cannot be read, is a stop. Without the flag, sync.py exits with its status.

pull.py imports this module when it starts, and passes the flag to the sync.py of the release it then fast-forwards
to. A release that renames the flag or the record's `status` key therefore makes that one pull stop with 3, and the
pull after it, which runs the new code, works."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time

from portable import WINDOWS, run_tree

FLAG = '--ending'
LEFTOVER_AGE = 86400  # seconds
_LOOK = 'its own output above, if any, says why'


def record(path: Path, status: int) -> None:
    path.write_text(json.dumps({'status': status}), encoding='utf-8')


def run_sync(root: Path, private: Path | None, *, state: Path, apply: bool, notes: bool = False,
             timeout: float | None = None) -> int:
    """Run root's sync.py with the caller's own stdout and stderr and return the status it recorded: 0 in step,
    1 managed changes remain, 2 blocked, 3 stopped or incomplete. RuntimeError when it did not end by recording one,
    subprocess.TimeoutExpired when it ran past timeout. The time limit, a Ctrl+C, and on POSIX a hang-up or a SIGTERM
    of the caller while the sync runs, stop the sync and the processes it started, as far as portable.run_tree reaches
    them, and the last two then end the caller with SystemExit(128 plus the signal's number), unless the caller
    ignores them. The record's folder is made under state, the folder that holds the pull's lock and receipt, and not
    in the temp folder, which programs other than this one clean: on 2026-11-13 a Windows pull stopped because its
    record folder, still empty, was removed from %TEMP% while the sync ran."""
    command = [sys.executable, str(root / 'sync.py'), '--public', str(root / 'setup'),
               '--apply' if apply else '--check']
    if private:
        command += ['--private', str(private / 'setup')]
    if notes:
        command.append('--notes')
    records = state / 'sync-endings'
    records.mkdir(parents=True, exist_ok=True)
    for old in records.glob('sync-*'):
        try:
            age = time.time() - old.stat().st_mtime
        except FileNotFoundError:
            continue
        if age > LEFTOVER_AGE:
            shutil.rmtree(old, ignore_errors=True)
    # A folder that Windows will not yet let go of must not turn a sync that ended well into a stop.
    with tempfile.TemporaryDirectory(prefix='sync-', dir=records, ignore_cleanup_errors=True) as folder:
        ending = Path(folder) / 'ending.json'
        with _stopped_with_the_caller():
            code = run_tree([*command, FLAG, str(ending)], cwd=root, timeout=timeout,
                            stopped_by=BaseException).returncode
        try:
            status = _status(ending.read_text(encoding='utf-8'))
        except FileNotFoundError:
            status = None
        except (OSError, ValueError) as error:
            raise RuntimeError(f"sync.py exited {code}, and its record of how it ended cannot be read: {error}; "
                               f'{_LOOK}') from error
    if status is None:
        crash = ', as after a crash or a kill' if code else ''
        raise RuntimeError(f'sync.py exited {code} without recording how it ended{crash}; {_LOOK}')
    if code:
        raise RuntimeError(f'sync.py recorded status {status} and then exited {code}; {_LOOK}')
    return status


@contextlib.contextmanager
def _stopped_with_the_caller():
    if WINDOWS:
        yield
        return
    previous = {number: signal.getsignal(number) for number in (signal.SIGHUP, signal.SIGTERM)}
    # signal.getsignal gives None for a handler set outside Python, which signal.signal cannot put back.
    ours = [number for number, handler in previous.items() if handler not in (signal.SIG_IGN, None)]
    for number in ours:
        signal.signal(number, _exit)
    try:
        yield
    finally:
        for number in ours:
            signal.signal(number, previous[number])


def _exit(number, frame):
    raise SystemExit(128 + number)


def _status(text):
    data = json.loads(text)
    status = data.get('status') if isinstance(data, dict) else None
    if type(status) is not int:  # also refuses a bool, which isinstance counts as an int
        raise ValueError(f'{text!r} holds no whole number as its status')
    return status
