"""The description's TOML files in both directions: parse each file into a typed entry, and write entries back.

The writer is an emitter for this schema only: fixed key order, inline install tables, one array item per
line past three items, basic strings. tomllib keeps no comments, so render_root() keeps the bytes of any file
whose parsed content is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import re
import tomllib
from typing import Any, Mapping

from description import (AcademicRecipe, Adaptation, AlignedInstall, BUILDS, BuildSource, ClaudePlacement, CodexPlacement,
                         CodexRoles, CourseRecipe, DescriptionError, EngineInstall, Frozen, GeneratedRoles, GenericRecipe,
                         Hooks, HostMarketplace, Install, Lock, LockedTree, LocalSource, LooseSkill, MarketplaceInstall, MarketplaceMode,
                         NoteRule, Origin, Package, PluginRoles, PublicEmilRecipe, Recipe, Repo, RepoName, RepoRoles,
                         RepoSource, Roots, Rules, SCHEMA, Setup, SkillRoot, SnapshotRecipe, SnapshotSource, Source,
                         ToolNotes, Visibility, entry_path, via)
from description_checks import check_invariants, check_placement, check_privacy, check_references
from host_state import (ALIGNED_MARKETPLACE, ENGINE_MARKETPLACE, Commit, EntryName, FactKey, Host, Platform, SkillMode, Surface,
                        parse_commit, parse_digest)
import portable
import runtime_pin


_REQUIRED = object()


class _Table:
    def __init__(self, path: Path, data: Mapping, errors: list[str], prefix: str = ''):
        self.path, self.data, self.errors, self.prefix = path, data, errors, prefix
        self.used: set[str] = set()

    def error(self, key: str, message: str) -> None:
        self.errors.append(f'{self.path}:{self.prefix}{key}: {message}')

    def get(self, key: str, kind: type | tuple[type, ...], default: Any = _REQUIRED) -> Any:
        self.used.add(key)
        if key not in self.data:
            if default is _REQUIRED:
                self.error(key, 'missing')
            return None if default is _REQUIRED else default
        value = self.data[key]
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            self.error(key, f'expected {getattr(kind, "__name__", kind)}')
            return None if default is _REQUIRED else default
        return value

    def strings(self, key: str, default: Any = ()) -> tuple[str, ...]:
        value = self.get(key, list, _REQUIRED if default is _REQUIRED else list(default))
        if value is None:
            return ()
        if not all(isinstance(item, str) for item in value):
            self.error(key, 'expected a list of strings')
            return ()
        return tuple(value)

    def sub(self, key: str, required: bool = False) -> _Table | None:
        value = self.get(key, dict, _REQUIRED if required else None)
        return _Table(self.path, value, self.errors, f'{self.prefix}{key}.') if value is not None else None

    def parse(self, key: str, parser, text: Any) -> Any:
        try:
            return parser(text)
        except ValueError as error:
            self.error(key, str(error))
            return None

    def done(self) -> None:
        for key in self.data.keys() - self.used:
            self.error(key, 'unknown key')


def _commit(table: _Table, key: str) -> Commit | None:
    text = table.get(key, str)
    return table.parse(key, parse_commit, text) if text is not None else None


def _parse_recipe(table: _Table, build: str) -> Recipe | None:
    if build == 'snapshot':
        snapshot = table.parse('snapshot', parse_digest, table.get('snapshot', str))
        origins = {}
        raw = table.get('origins', dict, {})
        for skill, value in raw.items():
            if not isinstance(value, dict):
                table.error(f'origins.{skill}', 'expected a table')
                continue
            if not value:
                origins[skill] = None
                continue
            origin = _Table(table.path, value, table.errors, f'{table.prefix}origins.{skill}.')
            commit = origin.get('commit', str, None)
            origins[skill] = Origin(origin.get('repository', str), origin.get('path', str),
                                    origin.parse('commit', parse_commit, commit) if commit else None)
            origin.done()
        return SnapshotRecipe(snapshot, origins)
    common = dict(repository=table.get('repository', str), commit=_commit(table, 'commit'))
    if build == 'generic':
        return GenericRecipe(**common, subdirectory=table.get('subdirectory', str, None),
                             manifest=table.get('manifest', str), select=table.strings('select'))
    if build == 'course':
        return CourseRecipe(**common, select=table.strings('select', _REQUIRED), prefix=table.get('prefix', str, ''),
                            trim_descriptions=table.strings('trim_descriptions'))
    if build == 'public-emil':
        rename = table.get('rename', dict, {})
        if not all(isinstance(value, str) for value in rename.values()):
            table.error('rename', 'expected folder = "installed name" pairs')
            rename = {}
        return PublicEmilRecipe(**common, select=table.strings('select', _REQUIRED), rename=rename)
    if build == 'academic':
        return AcademicRecipe(**common, select=table.strings('select', _REQUIRED),
                              installer=table.parse('installer', parse_digest, table.get('installer', str)),
                              requirements=table.parse('requirements', parse_digest, table.get('requirements', str)))
    table.error('build', f'unknown build {build!r}; expected one of {sorted(BUILDS)}')
    return None


def _parse_source(table: _Table | None) -> Source:
    if table is None:
        return None
    if 'local' in table.data:
        variants = table.get('variants', dict, {})
        if any(k not in ('codex', 'agents', 'claude') or not isinstance(v, str) for k, v in variants.items()):
            table.error('variants', 'expected codex, agents or claude relative paths')
        return LocalSource(table.get('local', str), table.parse('tree', parse_digest, table.get('tree', str)), variants)
    if 'repo' in table.data:
        return RepoSource(RepoName(table.get('repo', str)), table.get('path', str, None))
    build = table.get('build', str)
    recipe = _parse_recipe(table, build) if build else None
    frozen_table = table.sub('frozen')
    frozen = None
    if frozen_table:
        frozen = Frozen(frozen_table.get('since', str), frozen_table.get('upstream', str), frozen_table.get('reason', str))
        frozen_table.done()
    return BuildSource(recipe, table.get('packaging', int), table.get('display_name', str), frozen,
                       table.get('metadata', dict, {}))


def _parse_install(table: _Table, index: int, raw: Any, host: Host) -> Install | None:
    key = f'install[{index}]'
    if not isinstance(raw, dict):
        table.error(key, 'expected an inline table')
        return None
    item = _Table(table.path, raw, table.errors, f'{table.prefix}{key}.')
    name = item.get('via', str)
    enabled = item.get('enabled', bool)
    if name == ENGINE_MARKETPLACE[host]:
        install = EngineInstall(enabled, item.get('name', str, None), item.strings('skills') or None)
    elif host == Host.CLAUDE and name == ALIGNED_MARKETPLACE:
        agents = item.strings('agents') if 'agents' in raw else None
        install = AlignedInstall(enabled, frozenset(agents) if agents is not None else None)
    else:
        install = MarketplaceInstall(name, enabled)
    item.done()
    return install


def _parse_installs(table: _Table, host: Host) -> tuple[Install, ...]:
    raw = table.get('install', list)
    return tuple(install for index, item in enumerate(raw or ())
                 if (install := _parse_install(table, index, item, host)) is not None)


def _parse_roles(table: _Table | None) -> CodexRoles | None:
    if table is None:
        return None
    kind = table.get('from', str)
    if kind == 'repo':
        rewrite = table.get('rewrite', list, [])
        if not all(isinstance(pair, list) and len(pair) == 2 and all(isinstance(s, str) for s in pair) for pair in rewrite):
            table.error('rewrite', 'expected pairs of strings')
            rewrite = []
        roles = RepoRoles(table.get('directory', str), table.get('pattern', str), table.get('skill', str),
                          tuple(tuple(pair) for pair in rewrite))
    elif kind == 'plugin':
        roles = PluginRoles(table.get('directory', str))
    elif kind == 'generated':
        roles = GeneratedRoles(table.get('directory', str, 'agents'))
    else:
        table.error('from', 'expected repo, plugin or generated')
        roles = None
    table.done()
    return roles


CLAUDE_MODES = (SkillMode.EXPLICIT, SkillMode.ON)


def _parse_claude_modes(table: _Table | None) -> dict[str, SkillMode]:
    modes: dict[str, SkillMode] = {}
    if table is None:
        return modes
    for mode in CLAUDE_MODES:
        for skill in table.strings(mode.value):
            if skill in modes:
                table.error(mode.value, f'{skill} already has mode {modes[skill].value}')
            modes[skill] = mode
    if SkillMode.OFF.value in table.data:
        table.used.add(SkillMode.OFF.value)
        table.error(SkillMode.OFF.value, 'a Claude mode is explicit or on')
    table.done()
    return modes


def _parse_package(path: Path, data: Mapping, visibility: Visibility, errors: list[str]) -> Package:
    table = _Table(path, data, errors)
    table.get('public', bool, False)
    note = table.get('note', str, '')
    platforms = frozenset(p for value in table.strings('platforms')
                          if (p := table.parse('platforms', Platform, value)) is not None)
    source_table = table.sub('source')
    source = _parse_source(source_table)
    if source_table:
        source_table.done()
    modes: dict[str, SkillMode] = {}
    tool_notes = ToolNotes.AUDIT
    skills = table.sub('skills')
    if skills:
        for mode in SkillMode:
            for skill in skills.strings(mode.value):
                if skill in modes:
                    skills.error(mode.value, f'{skill} already has mode {modes[skill].value}')
                modes[skill] = mode
        tool_notes = skills.parse('tool_notes', ToolNotes, skills.get('tool_notes', str, ToolNotes.AUDIT.value))
        skills.done()
    codex = claude = None
    codex_table = table.sub('codex')
    if codex_table:
        adaptation_table = codex_table.sub('adaptation')
        adaptation = None
        if adaptation_table:
            adaptation = Adaptation(adaptation_table.get('repository', str), _commit(adaptation_table, 'base'),
                                    _commit(adaptation_table, 'adapted'), adaptation_table.get('harness', str, None))
            adaptation_table.done()
        codex = CodexPlacement(_parse_installs(codex_table, Host.CODEX),
                               codex_table.parse('hooks', Hooks, codex_table.get('hooks', str, Hooks.OFF.value)),
                               _parse_roles(codex_table.sub('roles')), adaptation)
        codex_table.done()
    claude_table = table.sub('claude')
    if claude_table:
        claude = ClaudePlacement(_parse_installs(claude_table, Host.CLAUDE), _parse_claude_modes(claude_table.sub('skills')))
        claude_table.done()
    table.done()
    return Package(EntryName(path.stem), visibility, note, source, modes, tool_notes, codex, claude, platforms)


def _parse_marketplace(table: _Table | None) -> HostMarketplace | None:
    if table is None:
        return None
    marketplace = HostMarketplace(table.get('marketplace', str),
                                  table.parse('mode', MarketplaceMode, table.get('mode', str, 'git')),
                                  table.strings('sparse'))
    table.done()
    return marketplace


def _parse_repo(path: Path, data: Mapping, visibility: Visibility, errors: list[str]) -> Repo:
    table = _Table(path, data, errors)
    table.get('public', bool, False)
    repo = Repo(RepoName(path.stem), visibility, table.get('note', str, ''), table.get('url', str), _commit(table, 'commit'),
                _parse_marketplace(table.sub('codex')), _parse_marketplace(table.sub('claude')))
    table.done()
    return repo


def _parse_loose(path: Path, data: Mapping, visibility: Visibility, errors: list[str]) -> LooseSkill:
    table = _Table(path, data, errors)
    table.get('public', bool, False)
    note = table.get('note', str, '')
    source_table = table.sub('source', required=True)
    source = None
    if source_table:
        if 'snapshot' in source_table.data:
            snapshots = {}
            for root, digest in source_table.get('snapshot', dict, {}).items():
                key = f'snapshot.{root}'
                if (place := source_table.parse(key, SkillRoot, root)) and (value := source_table.parse(key, parse_digest, digest)):
                    snapshots[place] = value
            source = SnapshotSource(snapshots)
        elif 'local' in source_table.data:
            source = _parse_source(source_table)
        else:
            source = RepoSource(RepoName(source_table.get('repo', str)), source_table.get('path', str, None))
        source_table.done()
    place = {}
    place_table = table.sub('place', required=True)
    if place_table:
        for root in SkillRoot:
            value = place_table.get(root.value, bool, None)
            if value is not None:
                place[root] = value
        place_table.done()
    table.done()
    return LooseSkill(EntryName(path.stem), visibility, note, source, place)


def _parse_host_skills(path: Path, data: Mapping, errors: list[str]) -> dict[str, SkillMode]:
    table = _Table(path, data, errors)
    skills = table.sub('skills')
    modes = {}
    if skills:
        for key in skills.data:
            modes[key] = skills.parse(key, SkillMode, skills.get(key, str))
        skills.done()
    table.done()
    return modes


def _parse_device(path: Path, data: Mapping, errors: list[str]) -> frozenset[FactKey]:
    """A [host] table per host, listing subjects by surface as sync reports them:
    loose_skill = ["{claude_home}/skills/orca-cli"]."""
    table = _Table(path, data, errors)
    keys = set()
    for host in Host:
        if (surfaces := table.sub(host.value)) is not None:
            for surface in Surface:
                keys.update(FactKey(host, surface, subject) for subject in surfaces.strings(surface.name.lower()))
            surfaces.done()
    table.done()
    return frozenset(keys)


def _parse_lock(path: Path, data: Mapping, errors: list[str]) -> dict[EntryName, Lock]:
    locks = {}
    for name, raw in data.items():
        if not isinstance(raw, dict):
            errors.append(f'{path}:{name}: expected a table')
            continue
        table = _Table(path, raw, errors, f'{name}.')
        trees = {}
        for host in Host:
            tree = table.sub(host.value)
            if tree:
                trees[host] = LockedTree(tree.get('version', str), tree.parse('tree', parse_digest, tree.get('tree', str)))
                tree.done()
        roles = table.get('roles', dict, {})
        placements = table.get('placements', dict, {})
        locks[EntryName(name)] = Lock(
            table.parse('intent', parse_digest, table.get('intent', str)), trees,
            {role: table.parse(f'roles.{role}', parse_digest, digest) for role, digest in roles.items()},
            {table.parse(f'placements.{root}', SkillRoot, root): table.parse(f'placements.{root}', parse_digest, digest)
             for root, digest in placements.items()},
            table.get('hooks', int, 0))
        table.done()
    return locks


@dataclass
class _Root:
    packages: dict[EntryName, Package] = field(default_factory=dict)
    repos: dict[RepoName, Repo] = field(default_factory=dict)
    loose: dict[EntryName, LooseSkill] = field(default_factory=dict)
    host_skills: dict[Host, dict[str, SkillMode]] = field(default_factory=dict)
    rules: Rules | None = None
    note_rules: tuple[NoteRule, ...] = ()
    public_repositories: frozenset[str] = frozenset()
    lock: dict[EntryName, Lock] = field(default_factory=dict)
    lock_path: Path | None = None
    expected_unmanaged: dict[Platform, frozenset[FactKey]] = field(default_factory=dict)
    runtime: runtime_pin.RuntimePin | None = None


def _toml(path: Path, errors: list[str]) -> dict | None:
    try:
        return tomllib.loads(portable.read_utf8(path))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        errors.append(f'{path}:: {error}')
        return None


def _read_root(root: Path, visibility: Visibility, errors: list[str]) -> _Root:
    found = _Root()
    if not root.is_dir():
        errors.append(f'{root}:: the {visibility.value} root does not exist')
        return found
    setup_path = root / 'setup.toml'
    if visibility == Visibility.PRIVATE:
        if setup_path.is_file():
            errors.append(f'{setup_path}:: setup.toml belongs to the public root')
    elif setup_path.is_file() and (data := _toml(setup_path, errors)) is not None:
        table = _Table(setup_path, data, errors)
        schema = table.get('schema', int)
        if schema is not None and schema != SCHEMA:
            table.error('schema', f'expected {SCHEMA}')
        _check_flag(table, visibility)
        found.public_repositories = frozenset(table.strings('public_repositories'))
        rules = []
        for index, raw in enumerate(table.get('tool_note', list, [])):
            rule = _Table(setup_path, raw if isinstance(raw, dict) else {}, errors, f'tool_note[{index}].')
            rules.append(NoteRule(rule.get('match', str), rule.get('note', str)))
            rule.done()
        found.note_rules = tuple(rules)
        table.done()
    else:
        errors.append(f'{setup_path}:: missing')
    pin = root / runtime_pin.PIN
    if visibility == Visibility.PRIVATE:
        if pin.exists():
            errors.append(f'{pin}:: the runtime pin belongs to the public root')
    else:
        found.runtime = runtime_pin.RuntimePin.read(pin, errors)
    for kind, parser, target in (('packages', _parse_package, found.packages), ('repos', _parse_repo, found.repos),
                                 ('skills', _parse_loose, found.loose)):
        for path in sorted((root / kind).glob('*.toml')):
            data = _toml(path, errors)
            if data is None:
                continue
            _check_flag(_Table(path, data, errors), visibility)
            entry = parser(path, data, visibility, errors)
            target[entry.name] = entry
    for host in Host:
        path = root / 'hosts' / f'{host.value}.toml'
        if path.is_file() and (data := _toml(path, errors)) is not None:
            found.host_skills[host] = _parse_host_skills(path, data, errors)
    for platform in Platform:
        path = root / 'devices' / f'{platform.value}.toml'
        if path.is_file() and (data := _toml(path, errors)) is not None:
            found.expected_unmanaged[platform] = _parse_device(path, data, errors)
    rules_path = root / 'rules/AGENTS.md'
    if rules_path.is_file():
        found.rules = Rules(rules_path.read_bytes())
    found.lock_path = root / 'lock.toml'
    if found.lock_path.is_file() and (data := _toml(found.lock_path, errors)) is not None:
        found.lock = _parse_lock(found.lock_path, data, errors)
    return found


def _check_flag(table: _Table, visibility: Visibility) -> None:
    table.used.add('public')
    flag = table.data.get('public')
    if visibility == Visibility.PUBLIC and flag is not True:
        table.error('public', 'a public file must say public = true')
    if visibility == Visibility.PRIVATE and 'public' in table.data:
        table.error('public', 'a private file must not carry the public key; move it to the public root to publish it')


def load(roots: Roots) -> Setup:
    """Parse, validate and merge both roots. The public root is validated alone first."""
    errors: list[str] = []
    public = _read_root(roots.public, Visibility.PUBLIC, errors)
    if errors:
        raise DescriptionError(errors)
    alone = _merge(Roots(roots.public, None), public, _Root())
    check_privacy(alone, errors)
    check_references(alone, Visibility.PUBLIC, errors)
    private = _Root()
    if roots.private is not None:
        placement = check_placement(roots)
        if placement:
            raise DescriptionError(errors + placement)
        private = _read_root(roots.private, Visibility.PRIVATE, errors)
        if errors:
            raise DescriptionError(errors)
        for kind, a, b in (('packages', public.packages, private.packages), ('repos', public.repos, private.repos),
                           ('skills', public.loose, private.loose)):
            for name in sorted(a.keys() & b.keys()):
                errors.append(f'{entry_path(roots.private, kind, name)}:: {name} is already defined in the public root')
    setup = _merge(roots, public, private)
    if roots.private is not None:
        check_references(setup, Visibility.PRIVATE, errors)
    for found in (public, private):
        for name in found.lock:
            if name not in found.packages and name not in found.loose:
                errors.append(f'{found.lock_path}:{name}: locks only entries of its own root')
    check_invariants(setup, errors)
    if errors:
        raise DescriptionError(errors)
    return setup


def _merge(roots: Roots, public: _Root, private: _Root) -> Setup:
    return Setup(
        roots=roots,
        packages={**public.packages, **private.packages}, repos={**public.repos, **private.repos},
        loose={**public.loose, **private.loose}, host_skills={**public.host_skills, **private.host_skills},
        rules=private.rules or public.rules, note_rules=public.note_rules,
        public_repositories=public.public_repositories, lock={**public.lock, **private.lock},
        expected_unmanaged={**public.expected_unmanaged, **private.expected_unmanaged}, runtime=public.runtime)


@dataclass(frozen=True)
class _Inline:
    table: Mapping[str, Any]


def _string(text: str) -> str:
    escaped = {'"': '\\"', '\\': '\\\\', '\n': '\\n', '\t': '\\t', '\r': '\\r', '\b': '\\b', '\f': '\\f'}
    return '"' + ''.join(escaped.get(ch) or (f'\\u{ord(ch):04x}' if ord(ch) < 0x20 or ord(ch) == 0x7f else ch)
                         for ch in text) + '"'


def _key(key: str) -> str:
    return key if re.fullmatch(r'[A-Za-z0-9_-]+', key) else _string(key)


def _value(value: Any, inline: bool = False) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, _Inline):
        return '{ ' + ', '.join(f'{_key(k)} = {_value(v, True)}' for k, v in value.table.items()) + ' }' if value.table else '{}'
    if isinstance(value, Mapping):
        return _value(_Inline(value), True)
    if isinstance(value, (list, tuple)):
        items = [_value(item, inline) for item in value]
        if len(items) > 3 and not inline:
            return '[\n' + ''.join(f'  {item},\n' for item in items) + ']'
        return '[' + ', '.join(items) + ']'
    raise TypeError(f'cannot write {value!r}')


def _emit(table: Mapping[str, Any], prefix: str = '') -> str:
    scalars = [(k, v) for k, v in table.items() if not isinstance(v, dict)]
    tables = [(k, v) for k, v in table.items() if isinstance(v, dict)]
    lines = [f'{_key(k)} = {_value(v)}\n' for k, v in scalars]
    text = ''.join(lines)
    for key, sub in tables:
        name = f'{prefix}{_key(key)}'
        body = _emit(sub, name + '.')
        header = f'[{name}]\n' if any(not isinstance(v, dict) for v in sub.values()) or not sub else ''
        text += ('\n' if text else '') + header + body
    return text


def _dump_recipe(recipe: Recipe) -> dict:
    build = next(name for name, kind in BUILDS.items() if isinstance(recipe, kind))
    out: dict[str, Any] = {'build': build}
    if isinstance(recipe, SnapshotRecipe):
        out['snapshot'] = recipe.snapshot
        out['origins'] = {skill: _Inline({k: v for k, v in (('repository', o.repository), ('path', o.path),
                                                            ('commit', o.commit)) if v is not None} if o else {})
                          for skill, o in recipe.origins.items()}
        return out
    out.update(repository=recipe.repository, commit=recipe.commit)
    if isinstance(recipe, GenericRecipe):
        if recipe.subdirectory is not None:
            out['subdirectory'] = recipe.subdirectory
        out['manifest'] = recipe.manifest
    if isinstance(recipe, CourseRecipe):
        out['prefix'] = recipe.prefix
        if recipe.trim_descriptions:
            out['trim_descriptions'] = list(recipe.trim_descriptions)
    if isinstance(recipe, AcademicRecipe):
        out.update(installer=recipe.installer, requirements=recipe.requirements)
    if isinstance(recipe, PublicEmilRecipe) and recipe.rename:
        out['rename'] = _Inline(dict(sorted(recipe.rename.items())))
    if recipe.select or not isinstance(recipe, GenericRecipe):
        out['select'] = list(recipe.select)
    return out


_SOURCE_ORDER = ('build', 'snapshot', 'repository', 'commit', 'subdirectory', 'manifest', 'packaging', 'display_name',
                 'prefix', 'trim_descriptions', 'installer', 'requirements', 'select', 'rename', 'frozen', 'origins', 'metadata')


def _dump_install(install: Install, host: Host) -> _Inline:
    out: dict[str, Any] = {'via': via(install, host)}
    if isinstance(install, EngineInstall):
        if install.name:
            out['name'] = install.name
        if install.skills:
            out['skills'] = list(install.skills)
    out['enabled'] = install.enabled
    if isinstance(install, AlignedInstall) and install.agents is not None:
        out['agents'] = sorted(install.agents)
    return _Inline(out)


def _dump_package(package: Package) -> dict:
    out: dict[str, Any] = {}
    if package.visibility == Visibility.PUBLIC:
        out['public'] = True
    if package.note:
        out['note'] = package.note
    if package.platforms:
        out['platforms'] = sorted(p.value for p in package.platforms)
    source = package.source
    if isinstance(source, LocalSource):
        out['source'] = _dump_local(source)
    elif isinstance(source, RepoSource):
        out['source'] = {'repo': source.repo, **({'path': source.path} if source.path is not None else {})}
    elif isinstance(source, BuildSource):
        recipe = _dump_recipe(source.recipe)
        recipe.update(packaging=source.packaging, display_name=source.display_name)
        if source.metadata:
            recipe['metadata'] = dict(source.metadata)
        if source.frozen:
            recipe['frozen'] = _Inline({'since': source.frozen.since, 'upstream': source.frozen.upstream,
                                        'reason': source.frozen.reason})
        out['source'] = {key: recipe[key] for key in _SOURCE_ORDER if key in recipe}
    skills: dict[str, Any] = {}
    for mode in SkillMode:
        names = sorted(skill for skill, value in package.modes.items() if value == mode)
        if names:
            skills[mode.value] = names
    if package.tool_notes != ToolNotes.AUDIT:
        skills['tool_notes'] = package.tool_notes.value
    if skills:
        out['skills'] = skills
    if package.codex:
        codex: dict[str, Any] = {'install': [_dump_install(i, Host.CODEX) for i in package.codex.installs]}
        if package.codex.hooks != Hooks.OFF:
            codex['hooks'] = package.codex.hooks.value
        adaptation = package.codex.adaptation
        if adaptation:
            codex['adaptation'] = _Inline({'repository': adaptation.repository, 'base': adaptation.base,
                                           'adapted': adaptation.adapted,
                                           **({'harness': adaptation.harness} if adaptation.harness else {})})
        roles = package.codex.roles
        if isinstance(roles, RepoRoles):
            codex['roles'] = {'from': 'repo', 'directory': roles.directory, 'pattern': roles.pattern,
                              'skill': roles.skill, 'rewrite': [list(pair) for pair in roles.rewrite]}
        elif isinstance(roles, PluginRoles):
            codex['roles'] = {'from': 'plugin', 'directory': roles.directory}
        elif isinstance(roles, GeneratedRoles):
            codex['roles'] = {'from': 'generated', 'directory': roles.directory}
        out['codex'] = codex
    if package.claude:
        out['claude'] = {'install': [_dump_install(i, Host.CLAUDE) for i in package.claude.installs]}
        claude_skills = {mode.value: names for mode in CLAUDE_MODES
                         if (names := sorted(s for s, m in package.claude.modes.items() if m == mode))}
        if claude_skills:
            out['claude']['skills'] = claude_skills
    return out


def _dump_marketplace(marketplace: HostMarketplace) -> dict:
    out: dict[str, Any] = {'marketplace': marketplace.name}
    if marketplace.mode != MarketplaceMode.GIT:
        out['mode'] = marketplace.mode.value
    if marketplace.sparse:
        out['sparse'] = list(marketplace.sparse)
    return out


def _dump_repo(repo: Repo) -> dict:
    out: dict[str, Any] = {'public': True} if repo.visibility == Visibility.PUBLIC else {}
    if repo.note:
        out['note'] = repo.note
    out.update(url=repo.url, commit=repo.commit)
    for host in Host:
        marketplace = repo.marketplace(host)
        if marketplace:
            out[host.value] = _dump_marketplace(marketplace)
    return out


def _dump_loose(skill: LooseSkill) -> dict:
    out: dict[str, Any] = {'public': True} if skill.visibility == Visibility.PUBLIC else {}
    if skill.note:
        out['note'] = skill.note
    source = skill.source
    out['source'] = (_dump_local(source) if isinstance(source, LocalSource) else
                    {'snapshot': _Inline({root.value: digest for root, digest in sorted(source.snapshots.items())})}
                     if isinstance(source, SnapshotSource)
                     else {'repo': source.repo, **({'path': source.path} if source.path is not None else {})})
    out['place'] = {root.value: on for root, on in sorted(skill.place.items())}
    return out


def _dump_local(source: LocalSource) -> dict:
    result = {'local': source.path, 'tree': source.digest}
    if source.variants:
        result['variants'] = _Inline(dict(source.variants))
    return result


def _dump_lock(lock: Lock) -> dict:
    out: dict[str, Any] = {'intent': lock.intent}
    for host, tree in lock.trees.items():
        out[host.value] = _Inline({'version': tree.version, 'tree': tree.digest})
    if lock.roles:
        out['roles'] = _Inline(dict(sorted(lock.roles.items())))
    if lock.placements:
        out['placements'] = _Inline({root.value: digest for root, digest in sorted(lock.placements.items())})
    if lock.hooks:
        out['hooks'] = lock.hooks
    return out


def render_entry(entry: Package | Repo | LooseSkill) -> str:
    dump = _dump_package if isinstance(entry, Package) else _dump_repo if isinstance(entry, Repo) else _dump_loose
    return _emit(dump(entry))


def _render_setup_toml(setup: Setup) -> str:
    head = {'schema': SCHEMA, 'public': True, 'public_repositories': sorted(setup.public_repositories)}
    text = _emit(head)
    for rule in setup.note_rules:
        text += f'\n[[tool_note]]\nmatch = {_string(rule.match)}\nnote = {_string(rule.note)}\n'
    return text


def _render_lock(locks: Mapping[EntryName, Lock]) -> str:
    header = '# Generated from the setup description and verified package builds. Do not edit.\n'
    return header + ''.join('\n' + _emit({name: _dump_lock(lock)}) for name, lock in sorted(locks.items()))


def managed(relative: str) -> bool:
    """Whether a path in a root, relative to it, is a file the description owns; anything else is left alone."""
    parts = PurePosixPath(relative).parts
    return (relative in ('setup.toml', 'lock.toml', 'rules/AGENTS.md')
            or (len(parts) == 2 and parts[0] in ('packages', 'repos', 'skills', 'hosts') and parts[1].endswith('.toml')))


def render_root(setup: Setup, visibility: Visibility, current: Path | None) -> dict[str, bytes]:
    """Every file the description owns in one root, by path relative to it. A file in `current` that already
    parses to the same content keeps its bytes, which is how hand-written comments survive."""
    files: dict[str, bytes] = {}

    def put(relative: str, text: str | bytes, same) -> None:
        existing = current / relative if current is not None else None
        if existing is not None and existing.is_file() and same(existing):
            files[relative] = existing.read_bytes()
        else:
            files[relative] = text if isinstance(text, bytes) else text.encode('utf-8')

    def unchanged(parser, entry):
        def same(path: Path) -> bool:
            errors: list[str] = []
            data = _toml(path, errors)
            return data is not None and parser(path, data, entry.visibility, errors) == entry and not errors
        return same

    for kind, entries, parser in (('packages', setup.packages, _parse_package), ('repos', setup.repos, _parse_repo),
                                  ('skills', setup.loose, _parse_loose)):
        for name, entry in sorted(entries.items()):
            if entry.visibility == visibility:
                put(f'{kind}/{name}.toml', render_entry(entry), unchanged(parser, entry))
    if visibility == Visibility.PUBLIC:
        put('setup.toml', _render_setup_toml(setup), lambda path: _same_setup_toml(path, setup))
    else:
        for host, modes in sorted(setup.host_skills.items()):
            if modes:
                text = _emit({'skills': {key: mode.value for key, mode in sorted(modes.items())}})
                put(f'hosts/{host.value}.toml', text, lambda path, m=modes: _parse_host_skills(path, _toml(path, []) or {}, []) == m)
        if setup.rules is not None:
            files['rules/AGENTS.md'] = setup.rules.text
    own = {name: lock for name, lock in setup.lock.items() if setup.visibility(name) == visibility}
    files['lock.toml'] = _render_lock(own).encode('utf-8')
    return files


def _same_setup_toml(path: Path, setup: Setup) -> bool:
    errors: list[str] = []
    data = _toml(path, errors)
    if data is None:
        return False
    rules = tuple(NoteRule(rule.get('match'), rule.get('note')) for rule in data.get('tool_note', []))
    return (data.get('schema') == SCHEMA and data.get('public') is True and rules == setup.note_rules
            and frozenset(data.get('public_repositories', [])) == setup.public_repositories
            and set(data) <= {'schema', 'public', 'public_repositories', 'tool_note'})
