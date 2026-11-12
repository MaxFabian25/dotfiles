"""Deliver the private checkout's home files: each file under config/agent-env/home goes to the same path under
the home folder, with the source's executable bit. A pull runs this on Linux after its sync.

A copy that someone edited on the machine is left as it is and named, and the rest is delivered: one edited helper
must not stop a release, as an edit to a file that sync manages does. The record of what was last delivered tells
an edit from a copy of an earlier release: a file is replaced only when it is absent or still holds the bytes the
record names. A machine's first delivery has no record, so there only a file that already matches is recorded."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from sync_files import ConcurrentEdit, Files, contents

SOURCE = 'config/agent-env/home'
RECORD = 'home-files.json'
EDITED = 'differs from the release and from the copy last delivered'


@dataclass(frozen=True)
class Delivery:
    written: tuple[str, ...]              # replaced or created, or with apply off, waiting for that
    left: tuple[tuple[str, str], ...]     # path and why it stays as it is

    def lines(self, applied: bool) -> list[str]:
        verb = 'delivered' if applied else 'to deliver'
        return [*(f'Home file {verb}: ~/{path}' for path in self.written),
                *(f'Home file left as it is: ~/{path} {reason}' for path, reason in self.left)]


def source_files(private: Path) -> dict[str, Path]:
    """Path under the home folder to source file. Bytecode that a test run leaves in the source is no home file."""
    root = private / SOURCE
    try:
        if not stat.S_ISDIR(root.stat().st_mode):
            return {}
    except FileNotFoundError:
        return {}
    except OSError as error:   # a folder above it that cannot be entered
        raise RuntimeError(f'{root}: cannot read the home files: {error.strerror}') from error

    def unlisted(error):
        # A folder that cannot be listed would otherwise count as empty, and its files as not part of the release.
        raise RuntimeError(f'{error.filename}: cannot list the home files: {error.strerror}')
    found = {}
    for folder, folders, names in os.walk(root, onerror=unlisted):
        folders[:] = sorted(name for name in folders if name != '__pycache__')
        for name in names:
            path = Path(folder) / name
            try:
                plain = stat.S_ISREG(path.stat().st_mode)
            except OSError as error:   # a folder that can be listed but not entered, or a link to nothing
                raise RuntimeError(f'{path}: cannot read the home file: {error.strerror}') from error
            if plain:
                found[path.relative_to(root).as_posix()] = path
    return dict(sorted(found.items()))


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _recorded(path: Path) -> dict[str, list[str]]:
    try:
        files = json.loads(path.read_text(encoding='utf-8'))['files']
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(f'{path}: cannot read the record of delivered home files: {error}. Without that file a '
                           'pull takes over each copy that matches the release and leaves the others') from error
    if not isinstance(files, dict) or not all(
            isinstance(known, list) and all(isinstance(digest, str) for digest in known) for known in files.values()):
        raise RuntimeError(f'{path}: the record of delivered home files is not a table of digest lists')
    return files


def _record(path: Path, files: dict[str, list[str]]):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix='.home-files-', dir=path.parent, delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write((json.dumps({'files': files}, indent=2, sort_keys=True) + '\n').encode())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def deliver(private: Path, home: Path, state: Path, *, apply: bool) -> Delivery:
    """The record lists, for each path, the digests a copy there may hold and still be the pull's own: what was
    last delivered and, from just before a replacement until the next delivery finds it done, what replaces it.
    So a pull that is killed between the two leaves a copy the next pull still knows. A path keeps its entry
    when a release drops the file, so that a release that brings it back finds its old copy known."""
    record = state / RECORD
    known = _recorded(record)
    saved = {path: list(digests) for path, digests in known.items()}
    written, left = [], []
    files = Files(state / 'home-file-backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    for relative, source in source_files(private).items():
        destination = home / relative
        try:
            data, executable = source.read_bytes(), os.access(source, os.X_OK)
            if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                left.append((relative, 'is not a plain file'))
                continue
            current = contents(destination)
            if current is not None and current != data and _digest(current) not in known.get(relative, ()):
                left.append((relative, EDITED))
                continue
            if current == data and os.access(destination, os.X_OK) == executable:
                known[relative] = [_digest(data)]
                continue
            if apply:
                if _digest(data) not in known.get(relative, ()):
                    ahead = {**known, relative: [*known.get(relative, ()), _digest(data)]}
                    _record(record, ahead)   # before the file, and a record that cannot be written leaves the file
                    known = ahead
                    saved = {path: list(digests) for path, digests in known.items()}
                files.write(destination, data, expected=current)
                if os.access(destination, os.X_OK) != executable:
                    destination.chmod(0o755 if executable else 0o644)
                known[relative] = [_digest(data)]
            written.append(relative)
        except ConcurrentEdit:
            left.append((relative, 'changed while the pull delivered it'))
        except OSError as error:
            left.append((relative, f'could not be delivered: {error}'))
    if apply and known != saved:
        try:
            _record(record, known)
        except OSError as error:
            raise RuntimeError(f'{record}: the files are delivered, but their record could not be written: {error}') from error
    return Delivery(tuple(written), tuple(left))
