"""Import verified snapshot skills into a separate immutable repository-plugin export."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from standalone_package import content_hashes
from sync_sources import BuildError, Built, child


def compose(package, built, private, directory, roots):
    if private is None:
        raise BuildError(f'{package.name}: skill imports require the private snapshot store')
    imports = []
    for name, pinned in sorted(package.source.imports.items()):
        digest = str(pinned).removeprefix('sha256:')
        snapshot = child(private, f'snapshots/{digest}')
        record = json.loads((snapshot / 'source.json').read_bytes())
        skills = snapshot / 'skills'
        for path in skills.rglob('*'):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise BuildError(f'{package.name}: unsupported snapshot member: {path}')
        hashes = content_hashes(skills)
        original = {k: v for k, v in record.items() if k not in ('snapshotSha256', 'fileHashes')}
        actual = hashlib.sha256(json.dumps({'files': hashes, 'source': original}, sort_keys=True).encode()).hexdigest()
        if actual != digest or record.get('snapshotSha256') != digest or hashes != record.get('fileHashes'):
            raise BuildError(f'{package.name}: imported snapshot differs from its pin: {name}')
        source = child(skills, name)
        if not (source / 'SKILL.md').is_file():
            raise BuildError(f'{package.name}: imported skill is missing: {name}')
        if (built.root / 'skills' / name).exists():
            raise BuildError(f'{package.name}: imported skill would replace existing content: {name}')
        imports.append((name, pinned, source, original, content_hashes(source)))
    parent = directory / 'composed' / package.name
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.compose-', dir=parent) as temporary:
        staged = Path(temporary) / 'tree'
        shutil.copytree(built.root, staged)
        receipt = {'schema': 1, 'base_version': built.version, 'base_digest': built.digest, 'imports': {}}
        for name, pinned, source, origin, hashes in imports:
            shutil.copytree(source, staged / 'skills' / name)
            receipt['imports'][name] = {'snapshot': pinned, 'origin': origin, 'files': hashes}
        receipt_path = staged / '.dotfiles-composition.json'
        if receipt_path.exists():
            raise BuildError(f'{package.name}: source already contains a composition receipt')
        receipt_path.write_bytes((json.dumps(receipt, indent=2) + '\n').encode())
        digest = str(roots.tree_digest(staged)).removeprefix('sha256:')
        version = built.version + ('.' if '+' in built.version else '+') + 'composed.' + digest[:16]
        for relative in ('plugin.json', '.codex-plugin/plugin.json', '.claude-plugin/plugin.json', '.cursor-plugin/plugin.json'):
            manifest = staged / relative
            if not manifest.is_file():
                continue
            data = json.loads(manifest.read_bytes())
            data['version'] = version
            selected = data.get('skills')
            if isinstance(selected, list) and not any(s.rstrip('/') in ('skills', './skills') for s in selected):
                data['skills'] = [*selected, *(f'./skills/{name}' for name, *_ in imports)]
            manifest.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode())
        expected = roots.tree_digest(staged)
        destination = parent / version
        if destination.exists():
            if roots.tree_digest(destination) != expected:
                raise BuildError(f'{package.name}: immutable composed export was edited: {destination}')
        else:
            staged.rename(destination)
    return Built(package.name, destination, version, expected)
