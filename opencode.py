from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import platform as operating_system
import re
import shutil
import sys
import tomllib

import yaml

from configuration import BlockedConfiguration, Configuration, FileConfiguration, JsonConfiguration, read_fields
from description import LooseSkill, Package, Setup, SkillRoot, LocalSource, _canonical
from host_state import DeviceRoots, Digest, EntryName, Host, Platform, SkillMode, bytes_digest
from invocation_overrides import collect_skills, frontmatter, skill_paths
from pstack_runtime import model_entries, model_target, opencode_model
from sync_sources import Builder, BuildError, child

EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
# Pstack's agent definitions that OpenCode subagents run, as the projection holds them, without their frontmatter.
PSTACK_AGENTS = ('poteto-agent', 'comment-sicko')


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
class OpenCodePlan:
    packages: tuple[Package, ...]
    loose: tuple[LooseExport, ...]
    destination: Path
    intent: Digest
    skills: tuple[SkillExport, ...]
    configurations: tuple[Configuration, ...]
    unsupported: tuple[str, ...]
    reserved: tuple[str, ...]
    agents: bool = False


def _json(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + '\n').encode()


def _id(name: str) -> str:
    encoded = re.sub('[^a-z0-9]+', '-', name.casefold()).strip('-')
    if not encoded or len(encoded) > 64:
        raise ValueError(f'OpenCode skill ID is not portable: {name!r}')
    return encoded


def _loose_mode(setup: Setup, skill: LooseSkill, root: SkillRoot) -> SkillMode | None:
    token = {SkillRoot.CODEX: 'codex_home', SkillRoot.AGENTS: 'agents_home'}[root]
    subject = '{' + token + '}/skills/' + skill.name + '/SKILL.md'
    return setup.host_skills.get(Host.CODEX, {}).get(subject,
                                                  None if skill.place[root] else SkillMode.OFF)


def plan(setup: Setup, roots: DeviceRoots, platform: Platform, *, adopted: bool = False) -> OpenCodePlan | None:
    source = setup.roots.private.parent / 'config/opencode/setup.toml' if setup.roots.private else None
    if source is None or not source.is_file():
        return None
    settings = tomllib.loads(source.read_text(encoding='utf-8'))
    if (set(settings) - {'enabled', 'baseline', 'mcp', 'hooks', 'retain'}
            or type(settings.get('enabled')) is not bool or settings.get('baseline') != 'codex'
            or settings.get('mcp', []) not in ([], ['cua-driver'])
            or type(settings.get('hooks', False)) is not bool):
        raise ValueError('OpenCode setup needs enabled, baseline = "codex", optional mcp = ["cua-driver"] '
                         'and optional boolean hooks')
    if not settings['enabled']:
        return None
    retained = settings.get('retain', {})
    if (not isinstance(retained, dict) or set(retained) - {'skills', 'permissions', 'policies'}
            or any(not isinstance(value, list) for value in retained.values())
            or any(not isinstance(value, str) for value in retained.get('skills', []))
            or any(not isinstance(value, dict) for key in ('permissions', 'policies') for value in retained.get(key, []))):
        raise ValueError('OpenCode retain must contain reviewed skills, permissions or policies arrays')
    selected = setup.for_platform(platform)
    wanted = {name for name, package in selected.packages.items() if package.source is not None and package.codex
              and any(install.enabled for install in package.codex.installs)}
    wanted.update(name for name, skill in selected.loose.items() if {SkillRoot.AGENTS, SkillRoot.CODEX} & skill.place.keys())
    if stale := setup.stale() & wanted:
        raise ValueError('OpenCode requires current setup locks: ' + ', '.join(sorted(stale)))
    packages, exports, unsupported, denied = [], [], [], set()
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
            identity = _id(name + '-' + skill)
            exports.append(SkillExport(name, skill, mode))
            if mode == SkillMode.OFF:
                denied.add(identity)
    loose = []
    for name, skill in sorted(selected.loose.items()):
        root = next((root for root in (SkillRoot.AGENTS, SkillRoot.CODEX) if root in skill.place), None)
        if root is None:
            continue
        mode = _loose_mode(selected, skill, root)
        loose.append(LooseExport(skill, root, mode, selected.lock[name].placements[root]))
        if mode == SkillMode.OFF:
            denied.add(name)
    for subject, mode in overrides.items():
        if mode == SkillMode.OFF and ':' not in subject and '/' not in subject:
            denied.add(subject)
        elif mode == SkillMode.OFF and subject.endswith('/SKILL.md'):
            denied.add(subject.rsplit('/', 2)[1])
    destination = roots.home / '.config/opencode/dotfiles'
    fields = {('update',): 'disable', ('skills',): [*retained.get('skills', []), str(destination / 'skills')]}
    permissions = [*retained.get('permissions', []),
                   *({'action': 'skill', 'resource': name, 'effect': 'deny'} for name in sorted(denied))]
    if settings.get('mcp'):
        fields[('mcp', 'servers', 'cua-driver')] = _cua(roots, platform)
        filters = json.loads((source.parent.parent / 'mcp-tool-filters/filters.json').read_bytes())
        excluded = filters['cua_browser_tools']
        if not isinstance(excluded, list) or any(not isinstance(name, str) for name in excluded):
            raise ValueError('OpenCode cua-driver exclusions must be a list of tool names')
        permissions.extend({'action': 'cua-driver_' + re.sub('[^a-zA-Z0-9_-]', '_', name),
                            'resource': '*', 'effect': 'deny'} for name in excluded)
    fields[('permissions',)] = permissions
    configuration = JsonConfiguration('opencode', destination.parent / 'opencode.json', fields)
    configurations: tuple[Configuration, ...] = (configuration,)
    if selected.rules is not None:
        configurations += (FileConfiguration('opencode-rules', destination.parent / 'AGENTS.md', selected.rules.text),)
    runtime = source.parent / 'runtime.toml'
    if runtime.is_file():
        runtime_settings = tomllib.loads(runtime.read_text(encoding='utf-8'))
        model = runtime_settings.get('model', '')
        if (set(runtime_settings) != {'vault_reference', 'model'}
                or not isinstance(runtime_settings['vault_reference'], str)
                or not re.fullmatch(r'op://Agents/[^/\r\n]+/[^/\r\n]+', runtime_settings['vault_reference'])
                or not isinstance(model, str)
                or not re.fullmatch(r'anthropic-api/claude-opus-[a-z0-9.-]+(?:#(?:low|medium|high|xhigh|max))?', model)):
            raise ValueError('OpenCode runtime needs an Agents vault reference and a native anthropic-api Opus model')
        fields[('model',)] = model
        fields[('policies',)] = [*retained.get('policies', []),
                                  {'action': 'provider.use', 'resource': '*', 'effect': 'deny'},
                                  {'action': 'provider.use', 'resource': 'anthropic-api', 'effect': 'allow'},
                                  {'action': 'provider.use', 'resource': 'openai-api', 'effect': 'allow'}]
        slug = model.split('/', 1)[1].split('#', 1)[0]
        helper = json.dumps([sys.executable, '-I', str(destination.parent / 'runtime.py'), 'credential'])
        plugin = (source.parent / 'credentials.js.in').read_text(encoding='utf-8')
        if plugin.count('__DOTFILES_CREDENTIAL_HELPER__') != 1 or plugin.count('__DOTFILES_MODEL_ID__') > 1:
            raise ValueError('OpenCode credential plugin must contain one helper command and at most one model slot')
        configurations += (
            FileConfiguration('opencode-runtime', destination.parent / 'runtime.py', (source.parent / 'runtime.py').read_bytes()),
            FileConfiguration('opencode-credential-reference', destination.parent / 'runtime.toml', runtime.read_bytes()),
            FileConfiguration('opencode-credentials-plugin', destination.parent / 'plugins/credentials.js',
                              plugin.replace('__DOTFILES_CREDENTIAL_HELPER__', helper)
                                    .replace('__DOTFILES_MODEL_ID__', json.dumps(slug)).encode()),
        )
        if platform == Platform.WINDOWS:
            configurations += (FileConfiguration('opencode-launcher', roots.home / '.local/bin/opencode-anthropic.cmd',
                                                 (source.parent / 'opencode-anthropic.cmd').read_bytes()),)
    if settings.get('hooks'):
        configurations += _guards(source.parent, destination.parent)
    mentions = source.parent / 'skill-mentions.js'
    if mentions.is_file():
        configurations += (FileConfiguration('opencode-skill-mentions-plugin',
                                             destination.parent / 'plugins/skill-mentions.js', mentions.read_bytes()),)
    agents = _agents(source.parent.parent / 'pstack/models.md', destination)
    if agents and 'pstack' not in {package.name for package in packages}:
        raise ValueError('OpenCode pstack seats need the pstack package exported to OpenCode')
    fields.update(agents)
    if (destination.parent / 'opencode.jsonc').exists():
        configurations += (BlockedConfiguration('opencode-jsonc', destination.parent / 'opencode.jsonc',
                           'OpenCode opencode.jsonc exists; review its migration before adopting JSON fields'),)
    elif not adopted and configuration.destination.exists():
        current = read_fields(configuration.destination.read_bytes(), fields, 'json')
        conflicts = [path[0] for path in fields if len(path) == 1 and path[0] in {'skills', 'permissions', 'policies'}
                     and current[path].get('value') not in (None, [], fields[path], retained.get(path[0], []))]
        if conflicts:
            configurations += (BlockedConfiguration('opencode-adoption', configuration.destination,
                               'OpenCode arrays need reviewed retain entries before adoption: ' + ', '.join(conflicts)),)
    fingerprint = {'renderer': 2, 'settings': settings, 'packages': packages, 'loose': loose, 'skills': exports,
                   'locks': {item.name: selected.lock[item.name] for item in packages},
                   'rules': selected.rules.digest if selected.rules else None, 'fields': list(fields.items())}
    reserved = set(selected.loose)
    for directory in (roots.claude_home / 'skills', roots.agents_home / 'skills', destination.parent / 'skills'):
        reserved.update(path.parent.name for path in collect_skills(directory))
        reserved.update(path.stem for path in directory.glob('*.md') if path.is_file())
    fingerprint['reserved'] = sorted(reserved)
    return OpenCodePlan(tuple(packages), tuple(loose), destination, bytes_digest(_json(_canonical(fingerprint))),
                        tuple(exports), configurations, tuple(unsupported), tuple(sorted(reserved)), bool(agents))


def _guards(source: Path, config: Path) -> tuple[Configuration, ...]:
    """The plugin that runs Antigravity's PreToolUse hooks before each OpenCode tool call, and the adapter it runs."""
    hooks = json.loads((source.parent / 'antigravity/hooks.json').read_bytes())
    pre_tool = []
    for spec in hooks.values():
        for entry in spec.get('PreToolUse', []):
            for hook in entry['hooks']:
                mode = re.fullmatch(r'\{python\} -I "\{adapter\}" ([a-z-]+)', hook['command'])
                if mode is None or not isinstance(entry['matcher'], str):
                    raise ValueError(f'OpenCode cannot run this Antigravity PreToolUse hook: {hook["command"]!r}')
                pre_tool.append({'mode': mode[1], 'matcher': entry['matcher']})
    if not pre_tool:
        raise ValueError('OpenCode hooks need PreToolUse hooks in config/antigravity/hooks.json')
    adapter = config / 'hook-adapter.py'
    plugin = (source / 'guards.js.in').read_text(encoding='utf-8')
    if plugin.count('__DOTFILES_HOOK_ADAPTER__') != 1 or plugin.count('__DOTFILES_PRE_TOOL_HOOKS__') != 1:
        raise ValueError('OpenCode guards plugin must contain one adapter command and one hook list')
    plugin = (plugin.replace('__DOTFILES_HOOK_ADAPTER__', json.dumps([sys.executable, '-I', str(adapter)]))
                    .replace('__DOTFILES_PRE_TOOL_HOOKS__', json.dumps(pre_tool)))
    return (FileConfiguration('opencode-hook-adapter', adapter,
                              (source.parent / 'antigravity/hook_adapter.py').read_bytes()),
            FileConfiguration('opencode-guards-plugin', config / 'plugins/guards.js', plugin.encode()))


def _agents(models: Path, destination: Path) -> dict[tuple[str, ...], object]:
    """opencode.json agent fields for the ## opencode section of pstack's models.md: one subagent per seat, which runs
    pstack's poteto-agent, and comment-sicko and the built-in general and explore agents on Opus at the lowest effort
    the section lists for it, as Claude Code's subagent hook runs a spawn that names no model."""
    if not models.is_file():
        return {}
    seats: dict[str, tuple[str, list[str]]] = {}
    opus = []
    for section, role, model, effort in model_entries(models.read_bytes()):
        if section != 'opencode' or model in {'auto', 'inherit-parent'}:
            continue
        target = model_target(section, model)
        seats.setdefault('pstack-' + _id(target[1] + '-' + effort), (opencode_model(model, effort), []))[1].append(role)
        if target == ('claude-code', 'opus'):
            opus.append(effort)
    system = {name: '{file:' + (destination / 'agents' / (name + '.md')).as_posix() + '}' for name in PSTACK_AGENTS}
    fields: dict[tuple[str, ...], object] = {}
    for name, (reference, roles) in seats.items():
        fields[('agents', name)] = {
            'description': (f'pstack poteto-agent on {reference}. Spawn it for the pstack roles '
                            + ', '.join(f'`{role}`' for role in roles)
                            + ', as ~/.config/pstack/models.md routes them under ## opencode.'),
            'mode': 'subagent', 'model': reference, 'system': system['poteto-agent']}
    if opus:
        default = opencode_model('opus', min(opus, key=EFFORTS.index))
        fields[('agents', 'comment-sicko')] = {
            'description': 'pstack comment-sicko, which the no-comments skill spawns to find comments and '
                           'workaround code to delete.',
            'mode': 'subagent', 'model': default, 'system': system['comment-sicko']}
        fields[('agents', 'general', 'model')] = default
        fields[('agents', 'explore', 'model')] = default
    return fields


def _agent_text(source: Path, payload: Path) -> bytes:
    """An agent definition's body, with its links resolved to the payload that holds the files they name."""
    text = source.read_text(encoding='utf-8')
    body = text[frontmatter(text)[0].end():].lstrip('\n')
    return re.sub(r'\]\((\.\./[^)\s]+)\)', lambda link: '](' + (payload / link[1][3:]).as_posix() + ')', body).encode()


def _cua(roots: DeviceRoots, platform: Platform) -> dict:
    command = shutil.which('cua-driver')
    if command is None and platform == Platform.WINDOWS:
        candidate = roots.home / 'AppData/Local/Programs/Cua/cua-driver/bin/cua-driver.exe'
        command = str(candidate) if candidate.is_file() else None
    if command is None:
        raise ValueError('OpenCode needs an installed cua-driver executable')
    environment = {'CUA_DRIVER_PERMISSION_MODE': 'unrestricted',
                   'CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS': '1', 'CUA_TELEMETRY_ENABLED': 'false'}
    if platform == Platform.LINUX:
        environment.update({'DISPLAY': ':99'} if 'microsoft' in operating_system.release().lower()
                           else {'CUA_DRIVER_RS_ENABLE_WAYLAND': '1'})
    return {'type': 'local', 'command': [command, 'mcp'], 'environment': environment}


def _metadata(path: Path, mode: SkillMode | None, *, sidecars: bool = True) -> tuple[str, bool]:
    _, header = frontmatter(path.read_text(encoding='utf-8'))
    description = header.get('description', '')
    if not isinstance(description, str):
        raise ValueError(f'OpenCode skill description must be text: {path}')
    if mode is not None:
        return description, mode == SkillMode.ON
    metadata = header.get('metadata', {})
    if not isinstance(metadata, dict):
        raise ValueError(f'OpenCode skill metadata must be a mapping: {path}')
    disabled = header.get('disable-model-invocation', False)
    if type(disabled) is not bool:
        raise ValueError(f'OpenCode skill invocation policy must be boolean: {path}')
    implicit = metadata.get('opencode/autoinvoke', not disabled)
    sidecar = path.parent / 'agents/openai.yaml'
    if sidecars and sidecar.is_file():
        policy = yaml.safe_load(sidecar.read_text(encoding='utf-8'))
        if not isinstance(policy, dict) or not isinstance(policy.get('policy', {}), dict):
            raise ValueError(f'OpenCode skill invocation sidecar must be a mapping: {sidecar}')
        implicit = policy.get('policy', {}).get('allow_implicit_invocation', implicit)
    if type(implicit) is not bool:
        raise ValueError(f'OpenCode skill invocation policy must be boolean: {path}')
    return description, implicit


def _copy(source: Path, destination: Path):
    for path in source.rglob('*'):
        if path.is_symlink() and (path.is_dir() or not path.resolve().is_relative_to(source.resolve())):
            raise BuildError(f'OpenCode payload link is not a contained file: {path}')
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns('.git', 'node_modules', '__pycache__'))


def materialise(plan: OpenCodePlan, builder: Builder, destination: Path) -> Path:
    if any(isinstance(item, BlockedConfiguration) for item in plan.configurations):
        raise BuildError('OpenCode configuration adoption is blocked')
    if destination.exists() or destination.is_symlink():
        raise BuildError(f'OpenCode staging destination already exists: {destination}')
    used = {}
    for name in plan.reserved:
        if name.casefold() in used:
            raise BuildError(f'OpenCode native skill ID collision: {name}')
        used[name.casefold()] = ('loose', name)
    records, wrappers, payloads, agents = [], [], [], {}

    def wrapper(identity, package, skill, source, mode, digest, relative):
        owner = (package, skill)
        if identity.casefold() in used and used[identity.casefold()] != owner:
            raise BuildError(f'OpenCode skill ID collision: {identity}')
        used[identity.casefold()] = owner
        if mode == SkillMode.OFF:
            return
        description, implicit = _metadata(source, mode)
        target = plan.destination / relative
        header = {'name': identity, 'description': description, 'disable-model-invocation': not implicit,
                  'metadata': {'opencode/autoinvoke': implicit}}
        body = (f'Read the complete skill at {target.as_posix()} before acting. '
                'Resolve its relative references from that file\'s directory. '
                f'This is the reviewed {package}:{skill} skill, not a native OpenCode plugin.\n')
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
            raise BuildError(f'{package.name}: missing selected OpenCode skills: {sorted(missing)}')
        relative_root = Path('payload') / _id(package.name)
        if any(relative_root == previous for _, previous, _ in payloads):
            raise BuildError(f'OpenCode package payload ID collision: {package.name}')
        payloads.append((effective, relative_root, builder.roots.tree_digest(effective)))
        if plan.agents and package.name == 'pstack':
            for name in PSTACK_AGENTS:
                definition = effective / 'agents' / (name + '.md')
                if not definition.is_file():
                    raise BuildError(f'pstack: missing the agent definition OpenCode runs: agents/{name}.md')
                agents[name] = _agent_text(definition, plan.destination / relative_root)
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
            raise BuildError(f'{skill.name}: OpenCode loose skill source differs from its lock')
        _, implicit = _metadata(tree / 'SKILL.md', export.mode)
        native = _metadata(tree / 'SKILL.md', None, sidecars=False)[1]
        if root == SkillRoot.AGENTS and implicit == native:
            records.append({'package': 'loose', 'skill': skill.name, 'id': skill.name,
                            'mode': export.mode, 'implicit': implicit, 'lock': export.digest,
                            'native': str(builder.roots.agents_home / 'skills' / skill.name / 'SKILL.md')})
            continue
        relative = Path('payload/loose') / _id(skill.name)
        payloads.append((tree, relative, export.digest))
        identity = skill.name
        used.pop(identity.casefold())
        wrapper(identity, 'loose', skill.name, tree / 'SKILL.md', export.mode, export.digest, relative / 'SKILL.md')
    destination.mkdir(parents=True)
    (destination / 'skills').mkdir()
    for tree, relative, digest in payloads:
        _copy(tree, destination / relative)
        if builder.roots.tree_digest(destination / relative) != digest:
            raise BuildError(f'OpenCode payload changed while copying: {relative}')
    for identity, data in wrappers:
        path = destination / 'skills' / identity / 'SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
    for name, data in agents.items():
        (destination / 'agents').mkdir(exist_ok=True)
        (destination / 'agents' / (name + '.md')).write_bytes(data)
    (destination / 'projection.json').write_bytes(_json({'schema': 1, 'intent': plan.intent,
                      'skills': records, 'payloads': [{'path': path.as_posix(), 'digest': digest}
                                                     for _, path, digest in payloads], 'unsupported': plan.unsupported}))
    return destination
