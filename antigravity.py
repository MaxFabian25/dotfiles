from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import platform as operating_system
import re
import shutil
import tomllib

import yaml

from configuration import BlockedConfiguration, Configuration, FileConfiguration, JsonConfiguration, read_fields
from description import LocalSource, LooseSkill, Package, Setup, SkillRoot, _canonical
from host_state import DeviceRoots, Digest, EntryName, Host, Platform, SkillMode, bytes_digest
from invocation_overrides import collect_skills, frontmatter, skill_paths
from sync_sources import BuildError, Builder, child


@dataclass(frozen=True)
class SkillExport:
    package: EntryName
    source_skill: str
    mode: SkillMode | None


@dataclass(frozen=True)
class LooseExport:
    skill: LooseSkill
    root: SkillRoot
    mode: SkillMode | None
    digest: Digest


@dataclass(frozen=True)
class AntigravityPlan:
    packages: tuple[Package, ...]
    loose: tuple[LooseExport, ...]
    destination: Path
    intent: Digest
    skills: tuple[SkillExport, ...]
    configurations: tuple[Configuration, ...]
    unsupported: tuple[str, ...]
    reserved: tuple[str, ...]
    native_reserved: tuple[str, ...] = ()


def _json(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + '\n').encode()


def _id(name: str) -> str:
    encoded = re.sub('[^a-z0-9]+', '-', name.casefold()).strip('-')
    if not encoded or len(encoded) > 64:
        raise ValueError(f'Antigravity skill ID is not portable: {name!r}')
    return encoded


def _loose_mode(setup: Setup, skill: LooseSkill, root: SkillRoot) -> SkillMode | None:
    token = {SkillRoot.CODEX: 'codex_home', SkillRoot.AGENTS: 'agents_home'}[root]
    subject = '{' + token + '}/skills/' + skill.name + '/SKILL.md'
    return setup.host_skills.get(Host.CODEX, {}).get(subject,
                                                  None if skill.place[root] else SkillMode.OFF)


def _leaf_fields(table: dict, prefix: tuple[str, ...]) -> dict[tuple[str, ...], object]:
    result = {}
    for key, value in table.items():
        if isinstance(value, dict):
            result.update(_leaf_fields(value, (*prefix, key)))
        else:
            result[(*prefix, key)] = value
    return result


def _render_hooks(value, replacements: dict[str, str]):
    if isinstance(value, str):
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        return value
    if isinstance(value, list):
        return [_render_hooks(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _render_hooks(item, replacements) for key, item in value.items()}
    return value


def plan(setup: Setup, roots: DeviceRoots, platform: Platform, *, adopted: bool = False) -> AntigravityPlan | None:
    source = setup.roots.private.parent / 'config/antigravity/setup.toml' if setup.roots.private else None
    if source is None or not source.is_file():
        return None
    settings = tomllib.loads(source.read_text(encoding='utf-8'))
    if (set(settings) - {'enabled', 'baseline', 'mcp', 'hooks', 'cli_settings', 'retain'}
            or type(settings.get('enabled')) is not bool or settings.get('baseline') != 'codex'
            or settings.get('mcp', []) not in ([], ['cua-driver'])
            or type(settings.get('hooks', False)) is not bool
            or not isinstance(settings.get('cli_settings', {}), dict)):
        raise ValueError(
            'Antigravity setup needs enabled, baseline = "codex", optional mcp = ["cua-driver"], '
            'boolean hooks, and optional cli_settings table'
        )
    if not settings['enabled']:
        return None
    retained = settings.get('retain', {})
    if (not isinstance(retained, dict) or set(retained) - {'entries', 'inherits'}
            or any(not isinstance(value, list) for value in retained.values())
            or any(not isinstance(item, dict) for value in retained.values() for item in value)):
        raise ValueError('Antigravity retain must contain reviewed entries or inherits arrays of objects')
    selected = setup.for_platform(platform)
    wanted = {name for name, package in selected.packages.items() if package.source is not None and package.codex
              and any(install.enabled for install in package.codex.installs)}
    wanted.update(name for name, skill in selected.loose.items() if {SkillRoot.AGENTS, SkillRoot.CODEX} & skill.place.keys())
    if stale := setup.stale() & wanted:
        raise ValueError('Antigravity requires current setup locks: ' + ', '.join(sorted(stale)))
    packages, exports, unsupported = [], [], []
    overrides = selected.host_skills.get(Host.CODEX, {})
    for name, package in sorted(selected.packages.items()):
        placement = package.codex
        if placement is None or not any(install.enabled for install in placement.installs):
            continue
        if package.source is None:
            unsupported.append(f'{name}: application-owned package is not exported')
            continue
        modes = dict(package.modes)
        modes.update({subject.split(':', 1)[1]: mode for subject, mode in overrides.items()
                      if subject.startswith(name + ':')})
        packages.append(package)
        for skill, mode in sorted(modes.items()):
            _id(name + '-' + skill)
            exports.append(SkillExport(name, skill, mode))
    loose = []
    for name, skill in sorted(selected.loose.items()):
        root = next((root for root in (SkillRoot.AGENTS, SkillRoot.CODEX) if root in skill.place), None)
        if root is None:
            continue
        mode = _loose_mode(selected, skill, root)
        loose.append(LooseExport(skill, root, mode, selected.lock[name].placements[root]))
    gemini_home = roots.home / '.gemini'
    config_dir = gemini_home / 'config'
    destination = config_dir / 'dotfiles'
    skills_fields: dict[tuple[str, ...], object] = {
        ('entries',): [*retained.get('entries', []), {'path': str(destination / 'skills')}],
    }
    if 'inherits' in retained:
        skills_fields[('inherits',)] = list(retained['inherits'])
    skills_config = JsonConfiguration('antigravity-skills', config_dir / 'skills.json', skills_fields)
    configurations: list[Configuration] = [skills_config]
    if not adopted and skills_config.destination.exists():
        current = read_fields(skills_config.destination.read_bytes(),
                              {('entries',): None, ('inherits',): None}, 'json')
        conflicts = [
            key for key in ('entries', 'inherits')
            if current[(key,)].get('value') not in (None, [], skills_fields.get((key,)), retained.get(key, []))
        ]
        if conflicts:
            configurations.append(BlockedConfiguration(
                'antigravity-skills-adoption', skills_config.destination,
                'Antigravity skills.json arrays need reviewed retain entries before adoption: ' + ', '.join(conflicts),
            ))
    if selected.rules is not None:
        configurations.append(FileConfiguration('antigravity-rules', gemini_home / 'AGENTS.md', selected.rules.text))
    # T3 Code runs agy with each of its profiles as the Antigravity home, so a profile's config folder gets the same
    # MCP servers and hooks as ~/.gemini/config. The hooks keep naming the adapter there.
    profiles = sorted(path for path in (roots.home / '.t3/userdata/providers/antigravity').glob('*/config')
                      if path.is_dir())
    profile_configurations: list[Configuration] = []
    if settings.get('mcp'):
        cua = _cua(roots, platform)
        filters = json.loads((source.parent.parent / 'mcp-tool-filters/filters.json').read_bytes())
        excluded = filters['cua_browser_tools']
        if not isinstance(excluded, list) or any(not isinstance(name, str) for name in excluded):
            raise ValueError('Antigravity cua-driver exclusions must be a list of tool names')
        cua['disabledTools'] = list(excluded)
        configurations.append(JsonConfiguration('antigravity-mcp', config_dir / 'mcp_config.json',
                                                {('mcpServers', 'cua-driver'): cua}))
        profile_configurations.extend(
            JsonConfiguration(f'antigravity-t3-mcp-{profile.parent.name}', profile / 'mcp_config.json',
                              {('mcpServers', 'cua-driver'): cua})
            for profile in profiles)
    if cli_settings := settings.get('cli_settings'):
        cli_fields = _leaf_fields(cli_settings, ())
        if not cli_fields:
            raise ValueError('Antigravity cli_settings must name at least one key when present')
        configurations.append(JsonConfiguration('antigravity-cli-settings',
                                                gemini_home / 'antigravity-cli/settings.json', cli_fields))
    if settings.get('hooks'):
        hooks_path = source.parent / 'hooks.json'
        adapter_path = source.parent / 'hook_adapter.py'
        if not hooks_path.is_file() or not adapter_path.is_file():
            raise ValueError('Antigravity hooks require config/antigravity/hooks.json and hook_adapter.py')
        raw_hooks = json.loads(hooks_path.read_text(encoding='utf-8'))
        if (not isinstance(raw_hooks, dict) or not raw_hooks
                or any(not isinstance(name, str) or not name or not isinstance(spec, dict)
                       for name, spec in raw_hooks.items())):
            raise ValueError('Antigravity hooks.json must be a non-empty object mapping hook names to objects')
        adapter_dest = config_dir / 'hook-adapter.py'
        replacements = {'{adapter}': adapter_dest.as_posix(), '{python}': 'python3'}
        rendered_hooks = {
            (name,): _render_hooks(spec, replacements)
            for name, spec in sorted(raw_hooks.items())
        }
        configurations.append(FileConfiguration('antigravity-hook-adapter', adapter_dest, adapter_path.read_bytes()))
        configurations.append(JsonConfiguration('antigravity-hooks', config_dir / 'hooks.json', rendered_hooks))
        profile_configurations.extend(
            JsonConfiguration(f'antigravity-t3-hooks-{profile.parent.name}', profile / 'hooks.json', rendered_hooks)
            for profile in profiles)
    native_reserved: set[str] = set()
    for directory in (config_dir / 'skills', gemini_home / 'antigravity-cli/skills'):
        native_reserved.update(path.parent.name for path in collect_skills(directory))
        native_reserved.update(path.stem for path in directory.glob('*.md') if path.is_file())
    reserved = set(selected.loose) | native_reserved
    fingerprint = {
        'renderer': 1,
        'settings': settings,
        'packages': packages,
        'loose': loose,
        'skills': exports,
        'locks': {item.name: selected.lock[item.name] for item in packages},
        'rules': selected.rules.digest if selected.rules else None,
        'configurations': [
            (item.name, str(item.destination),
             list(item.fields.items()) if isinstance(item, JsonConfiguration)
             else bytes_digest(item.data) if isinstance(item, FileConfiguration)
             else item.reason)
            for item in configurations
        ],
        'reserved': sorted(reserved),
        'native_reserved': sorted(native_reserved),
    }
    return AntigravityPlan(
        tuple(packages), tuple(loose), destination, bytes_digest(_json(_canonical(fingerprint))),
        tuple(exports), (*configurations, *profile_configurations), tuple(unsupported), tuple(sorted(reserved)),
        tuple(sorted(native_reserved)),
    )


def _cua(roots: DeviceRoots, platform: Platform) -> dict:
    command = shutil.which('cua-driver')
    if command is None and platform == Platform.WINDOWS:
        candidate = roots.home / 'AppData/Local/Programs/Cua/cua-driver/bin/cua-driver.exe'
        command = str(candidate) if candidate.is_file() else None
    elif command is None and platform == Platform.LINUX:
        candidate = roots.home / '.local/bin/cua-driver'
        command = str(candidate) if candidate.is_file() else None
    if command is None:
        raise ValueError('Antigravity needs an installed cua-driver executable')
    environment = {
        'CUA_DRIVER_PERMISSION_MODE': 'unrestricted',
        'CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS': '1',
        'CUA_TELEMETRY_ENABLED': 'false',
    }
    if platform == Platform.LINUX:
        environment.update({'DISPLAY': ':99'} if 'microsoft' in operating_system.release().lower()
                           else {'CUA_DRIVER_RS_ENABLE_WAYLAND': '1'})
    return {'command': command, 'args': ['mcp'], 'env': environment}


def _metadata(path: Path, mode: SkillMode | None, *, sidecars: bool = True) -> tuple[str, bool]:
    _, header = frontmatter(path.read_text(encoding='utf-8'))
    description = header.get('description', '')
    if not isinstance(description, str):
        raise ValueError(f'Antigravity skill description must be text: {path}')
    if mode is not None:
        return description, mode == SkillMode.ON
    disabled = header.get('disable-model-invocation', False)
    if type(disabled) is not bool:
        raise ValueError(f'Antigravity skill invocation policy must be boolean: {path}')
    implicit = not disabled
    sidecar = path.parent / 'agents/openai.yaml'
    if sidecars and sidecar.is_file():
        policy = yaml.safe_load(sidecar.read_text(encoding='utf-8'))
        if not isinstance(policy, dict) or not isinstance(policy.get('policy', {}), dict):
            raise ValueError(f'Antigravity skill invocation sidecar must be a mapping: {sidecar}')
        implicit = policy.get('policy', {}).get('allow_implicit_invocation', implicit)
    if type(implicit) is not bool:
        raise ValueError(f'Antigravity skill invocation policy must be boolean: {path}')
    return description, implicit


def _copy(source: Path, destination: Path):
    for path in source.rglob('*'):
        if path.is_symlink() and (path.is_dir() or not path.resolve().is_relative_to(source.resolve())):
            raise BuildError(f'Antigravity payload link is not a contained file: {path}')
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns('.git', 'node_modules', '__pycache__'))


def materialise(plan: AntigravityPlan, builder: Builder, destination: Path) -> Path:
    if any(isinstance(item, BlockedConfiguration) for item in plan.configurations):
        raise BuildError('Antigravity configuration adoption is blocked')
    if destination.exists() or destination.is_symlink():
        raise BuildError(f'Antigravity staging destination already exists: {destination}')
    used = {}
    native_fold = {name.casefold() for name in plan.native_reserved}
    for name in plan.reserved:
        if name.casefold() in used:
            raise BuildError(f'Antigravity native skill ID collision: {name}')
        used[name.casefold()] = ('native' if name.casefold() in native_fold else 'loose', name)
    records, wrappers, payloads = [], [], []

    def wrapper(identity, package, skill, source, mode, digest, relative):
        owner = (package, skill)
        if identity.casefold() in used and used[identity.casefold()] != owner:
            raise BuildError(f'Antigravity skill ID collision: {identity}')
        used[identity.casefold()] = owner
        if mode == SkillMode.OFF:
            return
        description, implicit = _metadata(source, mode)
        target = plan.destination / relative
        header: dict[str, object] = {'name': identity, 'description': description}
        if not implicit:
            header['disable-model-invocation'] = True
        body = (f'Read the complete skill at {target.as_posix()} before acting. '
                'Resolve its relative references from that file\'s directory. '
                f'This is the reviewed {package}:{skill} skill.\n')
        data = ('---\n' + yaml.safe_dump(header, sort_keys=False) + '---\n' + body).encode()
        wrappers.append((identity, data))
        records.append({'package': package, 'skill': skill, 'id': identity, 'mode': mode,
                        'implicit': implicit, 'source': relative.as_posix(), 'lock': digest,
                        'wrapper_digest': bytes_digest(data)})

    for package in plan.packages:
        built = builder.codex(package)
        effective = builder.effective(package, built)
        paths = skill_paths(effective)
        required = {item.source_skill for item in plan.skills if item.package == package.name and item.mode != SkillMode.OFF}
        if missing := required - paths.keys():
            raise BuildError(f'{package.name}: missing selected Antigravity skills: {sorted(missing)}')
        relative_root = Path('payload') / _id(package.name)
        if any(relative_root == previous for _, previous, _ in payloads):
            raise BuildError(f'Antigravity package payload ID collision: {package.name}')
        payloads.append((effective, relative_root, builder.roots.tree_digest(effective)))
        modes = {item.source_skill: item.mode for item in plan.skills if item.package == package.name}
        for name, path in sorted(paths.items()):
            relative = relative_root / path.resolve().relative_to(effective.resolve())
            wrapper(_id(package.name + '-' + name), package.name, name, path, modes.get(name), built.digest, relative)
    for export in plan.loose:
        skill, root = export.skill, export.root
        if export.mode == SkillMode.OFF:
            continue
        source = skill.source
        if isinstance(source, LocalSource):
            tree = builder.local(skill.visibility, source, root)
        else:
            tree = child(builder.setup.roots.private / 'snapshots', str(source.snapshots[root]).removeprefix('sha256:'))
        if builder.roots.tree_digest(tree) != export.digest:
            raise BuildError(f'{skill.name}: Antigravity loose skill source differs from its lock')
        relative = Path('payload/loose') / _id(skill.name)
        payloads.append((tree, relative, export.digest))
        identity = skill.name
        if identity.casefold() in native_fold:
            raise BuildError(f'Antigravity native skill ID collision: {identity}')
        used.pop(identity.casefold(), None)
        wrapper(identity, 'loose', skill.name, tree / 'SKILL.md', export.mode, export.digest, relative / 'SKILL.md')
    destination.mkdir(parents=True)
    (destination / 'skills').mkdir()
    for tree, relative, digest in payloads:
        _copy(tree, destination / relative)
        if builder.roots.tree_digest(destination / relative) != digest:
            raise BuildError(f'Antigravity payload changed while copying: {relative}')
    for identity, data in wrappers:
        path = destination / 'skills' / identity / 'SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
    (destination / 'projection.json').write_bytes(_json({
        'schema': 1,
        'intent': plan.intent,
        'skills': records,
        'payloads': [{'path': path.as_posix(), 'digest': digest} for _, path, digest in payloads],
        'unsupported': plan.unsupported,
    }))
    return destination
