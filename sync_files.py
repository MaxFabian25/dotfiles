from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

import tomlkit

from portable import replace


class ConcurrentEdit(RuntimeError):
    pass


def contents(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


class Files:
    def __init__(self, backup: Path):
        self.backup = backup
        self.saved: dict[Path, bytes | None] = {}
        self.changed: list[Path] = []

    def remember(self, path: Path) -> bytes | None:
        path = path.resolve() if path.is_symlink() else path
        before = contents(path)
        if path not in self.saved:
            self.saved[path] = before
            self.backup.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination = self.backup / f'{len(self.saved):04d}-{path.name}'
            if before is not None:
                destination.write_bytes(before)
                destination.chmod(0o600)
            self._receipt()
        return before

    def _receipt(self):
        receipt = {str(path): {'existed': data is not None, 'backup': f'{n:04d}-{path.name}' if data is not None else None}
                   for n, (path, data) in enumerate(self.saved.items(), 1)}
        path = self.backup / 'files.json'
        path.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
        path.chmod(0o600)

    def write(self, path: Path, data: bytes, *, expected: bytes | None) -> bool:
        path = path.resolve() if path.is_symlink() else path
        before = contents(path)
        if before != expected:
            raise ConcurrentEdit(f'Changed during sync: {path}')
        if before == data:
            return False
        self.remember(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix='.sync-', dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        try:
            temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
            if contents(path) != before:
                raise ConcurrentEdit(f'Changed during sync: {path}')
            replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        self.changed.append(path)
        return True

    def json(self, path: Path, change):
        before = contents(path)
        data = json.loads(before) if before is not None else {}
        original = json.dumps(data, sort_keys=True)
        change(data)
        if json.dumps(data, sort_keys=True) != original:
            return self.write(path, (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode(), expected=before)
        return False

    def toml(self, path: Path, change):
        before = contents(path)
        data = tomlkit.parse(before.decode('utf-8')) if before is not None else tomlkit.document()
        change(data)
        after = tomlkit.dumps(data).encode('utf-8')
        self.write(path, after, expected=before)

def backup_directory(roots) -> Path:
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    return roots.codex_home / 'sync-backups/dotfiles' / stamp
