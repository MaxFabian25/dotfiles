"""Immutable personal-catalog exports with the invocation policy carried in their bytes."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile

from host_state import SkillMode
from invocation_overrides import read_text, skill_paths, transform_policy, transform_skill


def explicit_skills(package):
    names = {name for name, mode in package.modes.items() if mode == SkillMode.EXPLICIT}
    if package.claude:
        names.update(name for name, mode in package.claude.modes.items() if mode == SkillMode.EXPLICIT)
    return names


def policy_writes(package, root):
    paths = skill_paths(root)
    explicit = explicit_skills(package)
    implicit = {name for name, mode in package.modes.items() if mode == SkillMode.ON}
    names = explicit | implicit
    if missing := names - paths.keys():
        raise ValueError(f'{package.name}: missing invocation-policy skills: {sorted(missing)}')
    changes = []
    for name in sorted(names):
        sidecar = paths[name].parent / 'agents/openai.yaml'
        before = read_text(sidecar)
        after = transform_policy(before, name in implicit)
        if before != after:
            changes.append((sidecar, before, after, name))
        if name in implicit:
            skill = paths[name]
            before = read_text(skill)
            after = transform_skill(before, f'{package.name}:{name}', {})
            if before != after:
                changes.append((skill, before, after, name))
    return tuple(changes)


def export(package, built, directory, roots):
    from sync_sources import BuildError, Built

    changes = policy_writes(package, built.root)
    if not changes:
        return built
    parent = directory / 'codex-exports' / package.name
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.policy-', dir=parent) as temporary:
        staged = Path(temporary) / 'tree'
        shutil.copytree(built.root, staged)
        for path, _, after, _ in changes:
            target = staged / path.relative_to(built.root.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(after.encode('utf-8'))
        receipt = {'schema': 2, 'upstream_version': built.version, 'upstream_digest': built.digest,
                   'explicit': [name for _, _, _, name in changes if package.modes.get(name) != SkillMode.ON]}
        implicit = sorted({name for _, _, _, name in changes if package.modes.get(name) == SkillMode.ON})
        if implicit:
            receipt['implicit'] = implicit
        receipt_path = staged / '.dotfiles-policy.json'
        if receipt_path.exists():
            raise BuildError(f'{package.name}: source already contains a policy export receipt')
        receipt_path.write_bytes((json.dumps(receipt, indent=2) + '\n').encode())
        digest = str(roots.tree_digest(staged)).removeprefix('sha256:')
        version = built.version + ('.' if '+' in built.version else '+') + 'explicit.' + digest[:16]
        for name in ('plugin.json', '.codex-plugin/plugin.json', '.claude-plugin/plugin.json', '.cursor-plugin/plugin.json'):
            manifest = staged / name
            if manifest.is_file():
                data = json.loads(manifest.read_bytes())
                data['version'] = version
                manifest.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode())
        expected = roots.tree_digest(staged)
        destination = parent / version
        if destination.exists():
            if roots.tree_digest(destination) != expected:
                raise BuildError(f'{package.name}: immutable policy export was edited: {destination}')
        else:
            staged.rename(destination)
    return Built(package.name, destination, version, expected, pristine=built.root)
