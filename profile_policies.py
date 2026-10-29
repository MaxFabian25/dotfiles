"""Bounded invocation-policy reconciliation for already installed native profile plugins."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib

from codex_exports import explicit_skills
from description import MarketplaceInstall, RepoSource, plugin_id
from drift import Difference, EntryRef, Kind
from host_state import FileValue, Host, bytes_digest
from invocation_overrides import skill_paths, transform_policy
from sync_files import ConcurrentEdit, contents
from sync_sources import BuildError, child, plugin_manifest


@dataclass(frozen=True)
class Repair:
    path: Path
    before: bytes | None
    after: bytes
    guards: tuple[tuple[Path, bytes | None], ...]


def profiles(home):
    parents = (home / '.local/share/orca/codex-accounts', home / 'AppData/Roaming/orca/codex-accounts',
               home / 'Library/Application Support/Orca/codex-accounts',
               home / 'Library/Application Support/orca/codex-accounts')
    return tuple(sorted({path for parent in parents for path in parent.glob('*/home') if path.is_dir()}))


def inspect(setup, roots):
    repairs, differences = [], []
    for profile in profiles(roots.home):
        config_path = profile / 'config.toml'
        config = contents(config_path)
        if config is None:
            continue
        plugins = tomllib.loads(config.decode('utf-8')).get('plugins', {})
        for package in setup.packages.values():
            if not package.codex or not isinstance(package.source, RepoSource) or not explicit_skills(package):
                continue
            locked = setup.lock.get(package.name)
            tree = locked.trees.get(Host.CODEX) if locked else None
            if tree is None:
                continue
            for install in package.codex.installs:
                if not isinstance(install, MarketplaceInstall) or not install.enabled:
                    continue
                identity = plugin_id(package.name, install, Host.CODEX)
                if plugins.get(identity, {}).get('enabled') is not True:
                    continue
                entry = EntryRef(package.name, package.visibility)
                try:
                    relative = f'plugins/cache/{install.marketplace}/{package.name}/{tree.version}'
                    if (profile / relative).resolve() == (roots.codex_home / relative).resolve():
                        continue
                    target = child(profile, relative)
                    if not target.is_dir():
                        continue
                    repo = setup.repos[package.source.repo]
                    source = roots.engine / 'native' / package.name / repo.commit
                    if not source.is_dir() and (installed := roots.installed_engine()) is not None:
                        source = installed / 'native' / package.name / repo.commit
                    if not source.is_dir() or roots.tree_digest(source) != tree.digest:
                        raise ValueError(f'verified native source is unavailable: {source}')
                    if plugin_manifest(target).get('name') != package.name:
                        raise ValueError(f'profile plugin identity differs: {target}')
                    available = skill_paths(source)
                    for name in sorted(explicit_skills(package)):
                        skill = available[name]
                        relative = skill.parent.relative_to(source)
                        sidecar = child(target, (relative / 'agents/openai.yaml').as_posix())
                        if sidecar.absolute() != sidecar.resolve():
                            raise ValueError(f'profile policy path is linked: {sidecar}')
                        original = contents(skill.parent / 'agents/openai.yaml')
                        desired = transform_policy(original.decode('utf-8') if original is not None else None, False).encode()
                        actual = contents(sidecar)
                        if actual == desired:
                            continue
                        if actual != original:
                            raise ValueError(f'profile policy differs from both reviewed copies: {sidecar}')
                        profile_skill = target / skill.relative_to(source)
                        if contents(profile_skill) != skill.read_bytes():
                            raise ValueError(f'profile skill has local edits: {profile_skill}')
                        guards = ((config_path, config), (profile_skill, skill.read_bytes()))
                        repairs.append(Repair(sidecar, actual, desired, guards))
                        differences.append(Difference(Kind.OVERLAY, None, FileValue(bytes_digest(desired)),
                                                      FileValue(bytes_digest(actual)) if actual is not None else None,
                                                      entry, host=Host.CODEX, detail=f'profile policy: {sidecar}'))
                except (OSError, ValueError, KeyError, BuildError) as error:
                    differences.append(Difference(Kind.LOCAL_EDIT, None, None, None, entry,
                                                  host=Host.CODEX, detail=str(error)))
    return tuple(repairs), tuple(differences)


def preflight(repairs):
    for repair in repairs:
        if repair.path.absolute() != repair.path.resolve():
            raise ConcurrentEdit(f'Profile policy path was linked: {repair.path}')
        for path, expected in ((repair.path, repair.before), *repair.guards):
            if contents(path) != expected:
                raise ConcurrentEdit(f'Profile changed during sync: {path}')


def apply(repairs, files):
    preflight(repairs)
    for repair in repairs:
        files.write(repair.path, repair.after, expected=repair.before)
