from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import uuid

from configuration import (BlockedConfiguration, FileConfiguration, JsonConfiguration, NpmrcConfiguration,
                           desired, patch_fields, read_fields, validate_runtime)
from description import AlignedInstall, LocalSource, RepoSource, SkillRoot, SnapshotSource, _canonical, plugin_id
from drift import Difference, EntryRef, Kind
from host_state import FactKey, Host, InstallDetail, Surface, Unpinned, bytes_digest
from portable import replace as replace_path
from sync_files import ConcurrentEdit, contents
from sync_sources import BuildError


_ABSENT = {'kind': 'absent'}
_RENDERER = 1


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def _same(left, right):
    return _encoded(left) == _encoded(right)


def _linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def image(path: Path, fields=None, syntax='toml'):
    """Raw path, entry-kind and byte evidence. Portable lock normalization is not ownership evidence."""
    if _linked(path):
        raise ConcurrentEdit(f'Refusing a linked working copy: {path}')
    if fields is not None:
        if path.exists() and not path.is_file():
            raise ConcurrentEdit(f'Managed {syntax} path is not a file: {path}')
        return _fields_image(read_fields(contents(path), fields, syntax), syntax)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return dict(_ABSENT)
    if stat.S_ISREG(mode):
        return {'kind': 'file', 'sha256': str(bytes_digest(path.read_bytes()))}
    if not stat.S_ISDIR(mode):
        raise ConcurrentEdit(f'Unsupported installed entry: {path}')
    entries = {}
    for entry in sorted(path.rglob('*')):
        relative = entry.relative_to(path).as_posix()
        mode = entry.lstat().st_mode
        if _linked(entry):
            raise ConcurrentEdit(f'Refusing a linked tree entry: {entry}')
        if stat.S_ISDIR(mode):
            entries[relative] = {'kind': 'directory'}
        elif stat.S_ISREG(mode):
            entries[relative] = {'kind': 'file', 'sha256': str(bytes_digest(entry.read_bytes()))}
        else:
            raise ConcurrentEdit(f'Unsupported installed entry: {entry}')
    return {'kind': 'tree', 'entries': entries}


def _claude_runtime(path):
    try:
        info = path.lstat()
        if _linked(path):
            raise ConcurrentEdit(f'Refusing linked Claude runtime metadata: {path}')
        if path.name == '.orphaned_at':
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16:
                raise ConcurrentEdit(f'Invalid Claude orphan marker: {path}')
            data = path.read_bytes()
            if not re.fullmatch(rb'[1-9][0-9]{0,15}', data) or int(data) > 8640000000000000:
                raise ConcurrentEdit(f'Invalid Claude orphan marker: {path}')
            return False
        if not stat.S_ISDIR(info.st_mode):
            raise ConcurrentEdit(f'Claude lease path is not a directory: {path}')
        leases = tuple(path.iterdir())
    except FileNotFoundError:
        if path.exists() or _linked(path):
            raise ConcurrentEdit(f'Claude runtime metadata changed during read: {path}')
        return False
    present = False
    for lease in leases:
        try:
            info = lease.lstat()
            if _linked(lease) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ConcurrentEdit(f'Invalid Claude lease entry: {lease}')
            name = re.fullmatch(r'([1-9][0-9]{0,9})(?:\.tmp\.[0-9a-f]{8})?', lease.name)
            if name is None or info.st_size > 4096:
                raise ConcurrentEdit(f'Invalid Claude lease entry: {lease}')
            data = lease.read_bytes()
        except FileNotFoundError:
            if lease.exists() or _linked(lease):
                raise ConcurrentEdit(f'Claude lease changed during read: {lease}')
            continue
        try:
            pairs = json.loads(data.decode('utf-8'), object_pairs_hook=tuple)
            marker = dict(pairs) if isinstance(pairs, tuple) else {}
        except (ValueError, UnicodeError) as error:
            raise ConcurrentEdit(f'Invalid Claude lease marker: {lease}') from error
        if (len(data) > 4096 or not isinstance(pairs, tuple) or len(marker) != len(pairs)
                or set(marker) - {'pid', 'procStart', 'procStartFt'}
                or type(marker.get('pid')) is not int or not 0 < marker['pid'] < 2 ** 31
                or marker['pid'] != int(name[1])
                or any(not isinstance(value, str) for key, value in marker.items() if key != 'pid')):
            raise ConcurrentEdit(f'Invalid Claude lease marker: {lease}')
        present = True
    return present


def _claude_cache_image(path, *, replacing=False):
    if _linked(path) or not path.is_dir():
        return image(path)
    entries = {}
    for entry in sorted(path.iterdir()):
        if entry.name in {'.in_use', '.orphaned_at'}:
            if _claude_runtime(entry) and replacing:
                raise ConcurrentEdit(f'Claude cache has lease markers; let Claude release them before replacement: {path}')
            continue
        child = image(entry)
        if child['kind'] == 'tree':
            entries[entry.name] = {'kind': 'directory'}
            entries.update({f'{entry.name}/{name}': value for name, value in child['entries'].items()})
        elif child != _ABSENT:
            entries[entry.name] = child
    return {'kind': 'tree', 'entries': entries}


def _fields_image(values, syntax):
    return {'kind': f'{syntax}-fields', 'fields': [{'path': list(key), **value} for key, value in sorted(values.items())]}


def _missing(value):
    return value == _ABSENT or (value['kind'].endswith('-fields') and
                                all(not field['present'] for field in value['fields']))


def _bindings(paths):
    return [{'path': str(path), 'physical': str(path.resolve()),
             'links': [{'path': str(part), 'target': str(part.readlink()) if part.is_symlink() else str(part.resolve())}
                       for part in (*reversed(path.parents), path) if _linked(part)]} for path in paths]


@dataclass(frozen=True)
class _Artifact:
    identity: str
    key: FactKey | None
    entry: EntryRef | None
    destination: Path
    current: Path
    aliases: tuple[Path, ...]
    intent: str
    kind: str
    expected: object = None
    data: bytes | None = None
    fields: object = None
    syntax: str = 'toml'
    native: bool = False
    provenance: object = None

    @property
    def claude_cache(self):
        return self.kind == 'plugin' and self.key.host == Host.CLAUDE


@dataclass(frozen=True)
class _PreparedArtifact:
    artifact: _Artifact
    target: dict
    before: dict
    before_path: Path
    bindings: list
    destination_before: dict
    pristine: dict | None = None
    source: Path | None = None
    data: bytes | None = None
    pending: dict | None = None


@dataclass(frozen=True)
class Prepared:
    artifacts: tuple[object, ...]
    built: dict
    claude: dict


class AcceptedInstallations:
    def __init__(self, setup, device, *, configurations=True):
        self.setup, self.device, self.roots = setup, device, device.roots
        self.directory = self.roots.codex_home / 'dotfiles/accepted'
        self.configurations = desired(setup.roots.public.parent if setup.roots.public else None,
                                      setup.roots.private.parent if setup.roots.private else None,
                                      self.roots, device.platform) if configurations else ()
        self._prepared = {}

    def _path(self, artifact):
        return self.directory / (hashlib.sha256(artifact.identity.encode()).hexdigest() + '.json')

    def _receipt(self, artifact):
        path = self._path(artifact)
        if not path.exists():
            if artifact.identity.startswith('rules:'):
                aliases = {str(alias) for alias in artifact.aliases}
                for existing in self.directory.glob('*.json'):
                    record = json.loads(existing.read_bytes())
                    if not record.get('identity', '').startswith('rules:'):
                        continue
                    accepted = record.get('accepted') or (record.get('pending') or {}).get('target') or {}
                    for binding in accepted.get('bindings', []):
                        if binding['path'] in aliases and not _same(binding, _bindings((Path(binding['path']),))[0]):
                            raise ConcurrentEdit(f'Shared rules link was retargeted: {binding["path"]}')
            return None
        value = json.loads(path.read_bytes())
        if value.get('schema') != 1 or value.get('identity') != artifact.identity:
            raise ValueError(f'Unsupported accepted installation receipt: {path}')
        if not isinstance(value.get('accepted'), (dict, type(None))) or not isinstance(value.get('pending'), (dict, type(None))):
            raise ValueError(f'Malformed accepted installation receipt: {path}')
        if not value.get('accepted') and not value.get('pending'):
            raise ValueError(f'Empty accepted installation receipt: {path}')
        for record in (value.get('accepted'), (value.get('pending') or {}).get('target')):
            if record is not None:
                if not all(key in record for key in ('path', 'image', 'intent', 'bindings')):
                    raise ValueError(f'Malformed accepted image: {path}')
                if not _same(record['bindings'], _bindings(tuple(Path(b['path']) for b in record['bindings']))):
                    raise ConcurrentEdit(f'Accepted path binding changed: {artifact.identity}')
        return value

    def artifacts(self, observations):
        units = []
        rules = {}
        device_paths = {name: str(getattr(self.roots, name)) for name in
                        ('home', 'codex_home', 'claude_home', 'agents_home', 'engine')}
        for state in self.device.hosts:
            facts = state.project(self.setup)
            for key, fact in facts.items():
                entry = EntryRef(fact.entry, self.setup.visibility(fact.entry)) if fact.entry in {
                    *self.setup.packages, *self.setup.loose} else None
                if key.surface == Surface.TREE:
                    detail = observations[state.host].details.get(FactKey(key.host, Surface.PLUGIN, key.subject))
                    version = self.setup.lock[fact.entry].trees[key.host].version
                    name, marketplace = key.subject.split('@', 1)
                    home = self.roots.codex_home if key.host == Host.CODEX else self.roots.claude_home
                    cache_version = version.replace('+', '-') if key.host == Host.CLAUDE else version
                    destination = home / 'plugins/cache' / marketplace / name / cache_version
                    current = detail.root if isinstance(detail, InstallDetail) else destination
                    package = self.setup.packages[fact.entry]
                    plugin_key = FactKey(key.host, Surface.PLUGIN, key.subject)
                    installed = observations[state.host].facts.get(plugin_key)
                    desired_plugin = facts[plugin_key].value
                    needs_native = installed is None or (not isinstance(desired_plugin.pin, Unpinned) and
                                                         installed.value.pin != desired_plugin.pin)
                    fingerprint = {'lock': _canonical(self.setup.lock[fact.entry]),
                                   'package': _canonical(package), 'notes': _canonical(self.setup.note_rules),
                                   'renderer': _RENDERER, 'host': key.host.value, 'paths': device_paths}
                    if isinstance(package.source, RepoSource):
                        repository = self.setup.repos[package.source.repo]
                        selection = ({'path': package.source.path} if package.source.path is not None
                                     else {'marketplace_plugin': package.name})
                        fingerprint['repository'] = {'url': repository.url, 'commit': repository.commit,
                                                     'selection': selection}
                    units.append(_Artifact(f'plugin:{key.host}:{key.subject}', key, entry, destination, current,
                                           (destination,), str(bytes_digest(_encoded(fingerprint))), 'plugin', fact.value,
                                           native=needs_native, provenance=fingerprint))
                elif key.surface in (Surface.AGENT, Surface.LOOSE_SKILL):
                    path = self.roots.codex_home / 'agents' / key.subject if key.surface == Surface.AGENT else self.expand(key.subject)
                    fingerprint = (str(bytes_digest(_encoded({'digest': fact.value.digest, 'paths': device_paths,
                                                             'renderer': _RENDERER})))
                                   if key.surface == Surface.AGENT else str(fact.value.digest))
                    units.append(_Artifact(f'{key.host}:{key.surface.name}:{key.subject}', key, entry, path, path,
                                           (path,), fingerprint, 'role' if key.surface == Surface.AGENT else 'loose', fact.value))
                elif key.surface == Surface.RULES:
                    alias = self.roots.codex_home / 'AGENTS.md' if key.host == Host.CODEX else self.roots.claude_home / 'CLAUDE.md'
                    path = alias.resolve()
                    if path in rules:
                        previous = rules[path]
                        rules[path] = replace(previous, aliases=(*previous.aliases, alias))
                    else:
                        rules[path] = _Artifact('rules:' + str(path), key, entry, path, path, (alias,),
                                                str(self.setup.rules.digest), 'file', fact.value, self.setup.rules.text)
        units.extend(rules.values())
        for config in self.configurations:
            if isinstance(config, BlockedConfiguration):
                continue
            data = config.data if isinstance(config, FileConfiguration) else None
            fields = None if isinstance(config, FileConfiguration) else config.fields
            syntax = {NpmrcConfiguration: 'npmrc', JsonConfiguration: 'json'}.get(type(config), 'toml')
            fingerprint = data if data is not None else _encoded([[list(k), v] for k, v in sorted(fields.items())])
            units.append(_Artifact('configuration:' + config.name, None, None, config.destination, config.destination,
                                   (config.destination,), str(bytes_digest(fingerprint)),
                                   'fields' if fields is not None else 'file', data=data, fields=fields, syntax=syntax))
        return tuple(units)

    def _read(self, artifact, path=None, *, replacing=False):
        if artifact.claude_cache:
            return _claude_cache_image(path or artifact.current, replacing=replacing)
        return image(path or artifact.current, artifact.fields, artifact.syntax)

    def _unchanged(self, artifact, actual, accepted):
        if _same(actual, accepted):
            return True
        if artifact.fields is None or not accepted['kind'].endswith('-fields'):
            return False
        # A release that adds or drops a managed key changes the field set, so read every key either side names:
        # the receipt's keys must still hold their accepted values and a newly managed key must still be absent.
        recorded = {tuple(field['path']): field for field in accepted['fields']}
        current = image(artifact.current, dict.fromkeys([*recorded, *artifact.fields]), artifact.syntax)
        return current['kind'] == accepted['kind'] and all(
            _same(field, recorded.get(tuple(field['path']), {'path': field['path'], 'present': False}))
            for field in current['fields'])

    def _known_pending(self, artifact, pending):
        target = pending['target']
        path = Path(target['path'])
        actual = self._read(artifact, path)
        known = [target['image'], pending.get('pristine')]
        if str(path) == pending['before_path']:
            known.append(pending['before'])
        else:
            known.append(pending['destination_before'])
        displaced_known = False
        if actual == _ABSENT and pending.get('displaced'):
            displaced = Path(pending['displaced'])
            if displaced.exists() and _same(self._read(artifact, displaced), pending['swap_before']):
                displaced_known = True
        if not displaced_known and not any(candidate is not None and _same(actual, candidate) for candidate in known):
            raise ConcurrentEdit(f'Unknown partial installation preserved: {artifact.identity}')
        old = Path(pending['before_path'])
        if old != path and not _same(self._read(artifact, old), pending['before']):
            raise ConcurrentEdit(f'Previous installation changed during replacement: {artifact.identity}')
        return actual

    def _assessment(self, artifact):
        receipt = self._receipt(artifact)
        if receipt and receipt.get('pending'):
            self._known_pending(artifact, receipt['pending'])
            return Kind.CHANGED, 'Finish a verified pending replacement'
        actual = self._read(artifact)
        accepted = receipt and receipt.get('accepted')
        if accepted:
            if Path(accepted['path']) != artifact.current:
                return Kind.CHANGED, 'Verify the installation at its new location'
            if not self._unchanged(artifact, actual, accepted['image']):
                if _missing(actual):
                    return Kind.MISSING, 'Recreate an absent managed artifact'
                if accepted['intent'] != artifact.intent:
                    return Kind.CHANGED, 'Verify exact desired content before replacing a stale receipt'
                return Kind.LOCAL_EDIT, 'Installed bytes differ from the accepted image'
            if accepted['intent'] == artifact.intent and _same(accepted['bindings'], _bindings(artifact.aliases)):
                return None, ''
            return Kind.CHANGED, 'Prepare the reviewed desired image'
        if _missing(actual):
            return Kind.MISSING, 'Create an absent managed artifact'
        return Kind.CHANGED, 'Verify pinned desired bytes before receipt-only adoption; mismatches are refused'

    def inspect(self, observations):
        differences = []
        artifacts = self.artifacts(observations)
        for artifact in artifacts:
            try:
                kind, detail = self._assessment(artifact)
            except ConcurrentEdit as error:
                kind, detail = Kind.LOCAL_EDIT, str(error)
            except (OSError, ValueError, KeyError, TypeError) as error:
                kind, detail = Kind.INCOMPLETE, str(error)
            if kind is not None:
                differences.append(Difference(kind, artifact.key, artifact.expected, None, artifact.entry,
                                              detail=f'{artifact.identity}: {detail}'))
        differences.extend(Difference(Kind.INCOMPLETE, None, None, None, None,
                                      detail=f'configuration:{config.name}: {config.reason}')
                           for config in self.configurations if isinstance(config, BlockedConfiguration))
        if self.configurations and not any(difference.key is None for difference in differences):
            try:
                validate_runtime(self.configurations, self.roots, proposed=False)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
                differences.append(Difference(Kind.INCOMPLETE, None, None, None, None,
                                              detail=f'Runtime configuration validation failed: {error}'))
        return artifacts, tuple(differences)

    def recoverable_missing_caches(self, artifacts, owned, observations):
        changed = {difference.key for difference in owned if difference.kind == Kind.CHANGED}
        recoverable = {}
        for artifact in artifacts:
            if artifact.kind != 'plugin' or artifact.key.host != Host.CLAUDE or artifact.key not in changed:
                continue
            plugin = FactKey(Host.CLAUDE, Surface.PLUGIN, artifact.key.subject)
            detail = observations[Host.CLAUDE].details.get(plugin)
            if not isinstance(detail, InstallDetail) or detail.root != artifact.destination:
                continue
            try:
                receipt = self._receipt(artifact)
                pending = receipt and receipt.get('pending')
                if not pending:
                    continue
                target = pending['target']
                destination = artifact.destination
                before_path = Path(pending['before_path'])
                staged, displaced = Path(pending['staged']), Path(pending['displaced'])
                work = staged.parent
                prefix = '.sync-' + destination.name + '-'
                suffix = work.name.removeprefix(prefix)
                if (Path(target['path']) != destination or target['intent'] != artifact.intent
                        or target['image']['kind'] != 'tree'
                        or not _same(target['bindings'], _bindings(artifact.aliases))
                        or not _same(pending['bindings'], _bindings((*artifact.aliases, before_path)))
                        or work.parent != destination.parent or not work.name.startswith(prefix)
                        or len(suffix) != 32 or uuid.UUID(suffix).hex != suffix
                        or staged != work / 'staged' or displaced != work / 'displaced'
                        or _linked(work) or image(destination) != _ABSENT
                        or not _same(image(staged), target['image'])
                        or pending['swap_before']['kind'] != 'tree'
                        or not _same(self._read(artifact, displaced), pending['swap_before'])
                        or not any(_same(pending['swap_before'], known) for known in
                                   (pending['before'], pending['destination_before'], pending.get('pristine')) if known)
                        or (before_path != destination and not _same(self._read(artifact, before_path), pending['before']))):
                    continue
                self._known_pending(artifact, pending)
            except (ConcurrentEdit, OSError, ValueError, KeyError, TypeError):
                continue
            recoverable[artifact.key] = self.roots.tokens(destination)
        return recoverable

    def prepare(self, observations, builder, *, packages=()):
        artifacts = self.artifacts(observations)
        needed = []
        for artifact in artifacts:
            kind, detail = self._assessment(artifact)
            if kind == Kind.LOCAL_EDIT:
                raise ConcurrentEdit(f'{artifact.identity}: {detail}')
            if kind is not None:
                needed.append(artifact)
        names = set(packages) | {a.entry.name for a in needed if a.kind in ('plugin', 'role')}
        built, claude, roles = {}, {}, {}
        for name in sorted(names):
            package = self.setup.packages[name]
            if package.source is None or not any(p and any(i.enabled for i in p.installs) for p in (package.codex, package.claude)):
                continue
            built[name] = builder.codex(package)
            claude.update(builder.claude(package, built[name]))
            if package.codex and any(i.enabled for i in package.codex.installs):
                roles.update(builder.roles(package, built[name]))
        prepared = []
        for artifact in needed:
            source, data, pristine = None, artifact.data, None
            if artifact.kind == 'plugin':
                package = self.setup.packages[artifact.entry.name]
                release = built[package.name] if artifact.key.host == Host.CODEX else claude[artifact.key.subject]
                if self.roots.tree_digest(release.root) != self.setup.lock[package.name].trees[artifact.key.host].digest:
                    raise BuildError(f'Prepared plugin differs from its pinned tree: {artifact.identity}')
                pristine = image(release.root)
                scratch = self.roots.engine / '.sync/accepted-images'
                scratch.mkdir(parents=True, exist_ok=True)
                source = Path(tempfile.mkdtemp(prefix=package.name + '-', dir=scratch)) / 'tree'
                shutil.copytree(release.root, source)
                install = next(i for i in package.placement(artifact.key.host).installs
                               if plugin_id(package.name, i, artifact.key.host) == artifact.key.subject)
                if not isinstance(install, AlignedInstall):
                    for path, before, after, _, _ in builder.overlay(package, artifact.key.host, artifact.key.subject, source):
                        if before != after:
                            if after is None:
                                path.unlink(missing_ok=True)
                            else:
                                path.parent.mkdir(parents=True, exist_ok=True)
                                path.write_bytes(after.encode('utf-8'))
            elif artifact.kind == 'role':
                data = roles[artifact.key.subject]
                if bytes_digest(self.roots.neutral(data)) != artifact.expected.digest:
                    raise BuildError(f'Prepared role differs from its lock: {artifact.identity}')
            elif artifact.kind == 'loose':
                skill = self.setup.loose[artifact.entry.name]
                root_name = next(root for root in SkillRoot if self._skill_root(root) / skill.name == artifact.destination)
                if isinstance(skill.source, LocalSource):
                    source = builder.local(skill.visibility, skill.source, root_name)
                elif isinstance(skill.source, SnapshotSource):
                    if self.setup.roots.private is None:
                        raise BuildError(f'{skill.name}: its snapshot requires the private repository')
                    source = self.setup.roots.private / 'snapshots' / str(skill.source.snapshots[root_name]).removeprefix('sha256:')
                else:
                    repo = self.setup.repos[skill.source.repo]
                    source = self.roots.engine / '.sync/loose' / skill.name / repo.commit
                    if not source.exists():
                        builder.sources.tree(repo.url, repo.commit, source, skill.source.path)
                if self.roots.tree_digest(source) != artifact.expected.digest:
                    raise BuildError(f'{skill.name}: source bytes do not match the locked loose skill')
            if artifact.fields is not None:
                target = _fields_image({k: {'present': True, 'value': v} for k, v in artifact.fields.items()},
                                       artifact.syntax)
            elif source is not None:
                target = image(source)
                if artifact.claude_cache and {'.in_use', '.orphaned_at'} & target.get('entries', {}).keys():
                    raise BuildError(f'Claude runtime metadata is not source content: {artifact.identity}')
                if any(part in {'.git', 'node_modules'} for path in target.get('entries', {}) for part in Path(path).parts):
                    raise BuildError(f'Excluded source content is not ownership evidence: {artifact.identity}')
            else:
                target = {'kind': 'file', 'sha256': str(bytes_digest(data))}
            prepared.append(self._prepare_artifact(artifact, target, source, data, pristine))
        validate_runtime(self.configurations, self.roots, proposed=True)
        tokens = tuple(object() for _ in prepared)
        self._prepared.update(zip(tokens, prepared))
        result = Prepared(tokens, built, claude)
        self.preflight(result)
        return result

    def _prepare_artifact(self, artifact, target, source, data, pristine):
        receipt = self._receipt(artifact)
        pending = receipt and receipt.get('pending')
        if pending:
            if pending['target']['intent'] != artifact.intent or not _same(pending['target']['image'], target):
                raise ConcurrentEdit(f'Finish the pending release before changing desired inputs: {artifact.identity}')
            self._known_pending(artifact, pending)
            return _PreparedArtifact(artifact, target, pending['before'], Path(pending['before_path']),
                                     pending['bindings'], pending['destination_before'], pristine, source, data, pending)
        actual = self._read(artifact)
        at_target = self._read(artifact, artifact.destination)
        accepted = receipt and receipt.get('accepted')
        if _same(at_target, target):
            before = actual
        elif _missing(actual):
            before = actual
        elif accepted and Path(accepted['path']) == artifact.current and self._unchanged(artifact, actual, accepted['image']):
            before = actual
        else:
            raise ConcurrentEdit(f'Unverified existing content preserved: {artifact.identity}; an independently pinned baseline is required')
        # A native host can cache the pinned release before its installer registers it, as Claude Code's
        # plugin listing does for a marketplace that moved to the new version.
        native_copy = artifact.native and pristine is not None and _same(at_target, pristine)
        if artifact.current != artifact.destination and not (_missing(at_target) or _same(at_target, target) or native_copy):
            raise ConcurrentEdit(f'Unverified destination preserved: {artifact.destination}')
        if target['kind'] == 'tree' and any(part in {'.git', 'node_modules'} for path in at_target.get('entries', {})
                                          for part in Path(path).parts) and not _same(at_target, target):
            raise ConcurrentEdit(f'Unowned tree content preserved: {artifact.destination}')
        return _PreparedArtifact(artifact, target, before, artifact.current,
                                 _bindings((*artifact.aliases, artifact.current)), at_target, pristine, source, data)

    def _check(self, prepared):
        artifact = prepared.artifact
        if not _same(prepared.bindings, _bindings(tuple(Path(b['path']) for b in prepared.bindings))):
            raise ConcurrentEdit(f'Path binding changed after preparation: {artifact.identity}')
        if prepared.pending:
            self._known_pending(artifact, prepared.pending)
        elif not (_same(self._read(artifact, prepared.before_path), prepared.before) and
                  _same(self._read(artifact, artifact.destination), prepared.destination_before)):
            raise ConcurrentEdit(f'Changed after preparation: {artifact.identity}')
        if prepared.source is not None and not _same(image(prepared.source), prepared.target):
            raise ConcurrentEdit(f'Prepared source changed: {artifact.identity}')

    def preflight(self, prepared):
        for token in prepared.artifacts:
            self._check(self._resolve(token))

    def _resolve(self, token):
        if token not in self._prepared:
            raise TypeError('Only this owner can apply its verified preparation')
        return self._prepared[token]

    def _save(self, artifact, accepted, pending=None):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(artifact)
        with tempfile.NamedTemporaryFile(prefix='.receipt-', dir=self.directory, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(_encoded({'schema': 1, 'identity': artifact.identity, 'accepted': accepted, 'pending': pending}) + b'\n')
        try:
            temporary.chmod(0o600)
            replace_path(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def apply(self, token, files, *, native=None):
        prepared = self._resolve(token)
        self._check(prepared)
        artifact = prepared.artifact
        destination = artifact.destination
        record = {'path': str(destination), 'bindings': _bindings(artifact.aliases),
                  'intent': artifact.intent, 'image': prepared.target, 'provenance': artifact.provenance}
        if _same(self._read(artifact, destination), prepared.target):
            self._save(artifact, record)
            return
        receipt = self._receipt(artifact)
        accepted = receipt and receipt.get('accepted')
        work = destination.parent / ('.sync-' + destination.name + '-' + uuid.uuid4().hex)
        pending = prepared.pending or {'before': prepared.before, 'before_path': str(prepared.before_path),
                                       'bindings': prepared.bindings, 'destination_before': prepared.destination_before,
                                       'target': record, 'pristine': prepared.pristine,
                                       'staged': str(work / 'staged'), 'displaced': str(work / 'displaced'),
                                       'swap_before': prepared.destination_before}
        self._save(artifact, accepted, pending)
        self._check(prepared)
        actual = self._known_pending(artifact, pending)
        displaced = actual == _ABSENT and Path(pending['displaced']).exists()
        if (artifact.kind == 'plugin' and not displaced
                and (artifact.native or (_missing(prepared.before) and not _same(actual, prepared.pristine)))):
            if native is None:
                raise RuntimeError(f'Native installer required: {artifact.identity}')
            native(artifact.key.host, artifact.key.subject)
            actual = self._read(artifact, destination)
            if not (_same(actual, prepared.pristine) or _same(actual, prepared.target)):
                raise ConcurrentEdit(f'Unknown native output preserved: {artifact.identity}')
        if not _same(actual, prepared.target):
            if prepared.source is not None:
                self._tree(prepared, pending, accepted, actual)
            else:
                if not _same(self._read(artifact, destination), actual):
                    raise ConcurrentEdit(f'Changed before write: {artifact.identity}')
                before = contents(destination)
                data = (patch_fields(before, artifact.fields, artifact.syntax) if artifact.fields is not None
                        else prepared.data)
                read = (_fields_image(read_fields(before, artifact.fields, artifact.syntax), artifact.syntax)
                        if artifact.fields is not None else _ABSENT if before is None else
                        {'kind': 'file', 'sha256': str(bytes_digest(before))})
                if not _same(read, actual):
                    raise ConcurrentEdit(f'Content changed before write: {artifact.identity}')
                files.write(destination, data, expected=before)
        if not _same(self._read(artifact, destination), prepared.target):
            raise ConcurrentEdit(f'Effective output failed verification: {artifact.identity}')
        if Path(pending['displaced']).exists():
            record['backup'] = pending['displaced']
        self._save(artifact, record)

    def _tree(self, prepared, pending, accepted, actual):
        artifact = prepared.artifact
        destination, staged, displaced = artifact.destination, Path(pending['staged']), Path(pending['displaced'])
        if any(part in {'.git', 'node_modules'} for path in actual.get('entries', {}) for part in Path(path).parts):
            raise ConcurrentEdit(f'Unowned tree content preserved: {destination}')
        staged.parent.mkdir(parents=True, exist_ok=True)
        if staged.exists():
            if not _same(image(staged), prepared.target):
                raise ConcurrentEdit(f'Prepared stage was edited: {staged}')
        else:
            shutil.copytree(prepared.source, staged)
        if not _same(image(staged), prepared.target):
            raise ConcurrentEdit(f'Prepared stage failed verification: {staged}')
        if not _same(self._read(artifact, destination, replacing=True), actual):
            raise ConcurrentEdit(f'Tree changed before replacement: {destination}')
        pending['swap_before'] = actual if actual != _ABSENT else pending['swap_before']
        self._save(artifact, accepted, pending)
        if not _same(self._read(artifact, destination, replacing=True), actual):
            raise ConcurrentEdit(f'Tree changed before replacement: {destination}')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if displaced.exists():
                raise ConcurrentEdit(f'Displaced tree already exists: {displaced}')
            destination.rename(displaced)
        staged.rename(destination)

    def expand(self, value):
        for token in ('home', 'codex_home', 'claude_home', 'agents_home', 'engine'):
            prefix = '{' + token + '}'
            if value == prefix or value.startswith(prefix + '/'):
                relative = value[len(prefix):].lstrip('/')
                if '..' in PurePosixPath(relative).parts or '\\' in relative or ':' in relative:
                    raise ValueError(f'Not a managed device-relative path: {value}')
                return getattr(self.roots, token).joinpath(*PurePosixPath(relative).parts)
        raise ValueError(f'Not a managed device-relative path: {value}')

    def _skill_root(self, root):
        return {SkillRoot.CODEX: self.roots.codex_home, SkillRoot.CLAUDE: self.roots.claude_home,
                SkillRoot.AGENTS: self.roots.agents_home}[root] / 'skills'
