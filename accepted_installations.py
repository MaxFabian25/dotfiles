from __future__ import annotations

import contextlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from functools import partial
import hashlib
import json
import opencode
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

from configuration import (BlockedConfiguration, FileConfiguration, JsonConfiguration, NpmrcConfiguration,
                           desired, patch_fields, read_fields, validate_runtime)
from description import AlignedInstall, LocalSource, RepoSource, SkillRoot, _canonical, plugin_id
from claude_host import _loaded
from drift import Difference, EntryRef, Kind
from host_state import (ENGINE_MARKETPLACE, FactKey, Host, InstallDetail, Released, Surface, Unpinned, bytes_digest,
                        named_for, version_folder)
from portable import StillHeld, staging_directory, staging_folder, replace as replace_path, while_held
from sync_files import ConcurrentEdit, contents
from sync_sources import BuildError


_ABSENT = {'kind': 'absent'}
_RENDERER = 1
_PYTHON_CACHE = '__pycache__'
_WORK = re.compile(r'\.sync-(.+)-[0-9a-f]{32}')
# The manifests Codex 0.171.0-alpha.12 reads a plugin's version from (find_plugin_manifest_path): the root plugin.json
# when it names an Agent Plugins schema, otherwise the first of the other three.
_MANIFESTS = ('plugin.json', '.codex-plugin/plugin.json', '.claude-plugin/plugin.json', '.cursor-plugin/plugin.json')
_AGENT_PLUGIN_SCHEMAS = 'https://agent-plugins.org/schemas/'


class _Stop(Enum):
    SWAPPED = 'swapped'
    ASIDE = 'aside'
    UNSTARTED = 'unstarted'
    LOST = 'lost'


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def _same(left, right):
    return _encoded(left) == _encoded(right)


def _digest(value):
    return str(bytes_digest(_encoded(value))).removeprefix('sha256:')


def _linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def image(path: Path, fields=None, syntax='toml'):
    """Raw path, entry-kind and byte evidence. Portable lock normalization is not ownership evidence.

    Leaves out the folders and files under __pycache__, which Python writes beside any module it imports,
    as tree_digest does. A link or special file there is still refused.
    """
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
        if _PYTHON_CACHE in entry.relative_to(path).parts and (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            continue
        if stat.S_ISDIR(mode):
            entries[relative] = {'kind': 'directory'}
        elif stat.S_ISREG(mode):
            entries[relative] = {'kind': 'file', 'sha256': str(bytes_digest(entry.read_bytes()))}
        else:
            raise ConcurrentEdit(f'Unsupported installed entry: {entry}')
    return {'kind': 'tree', 'entries': entries}


def copy_tree(source: Path, destination: Path, copy_function=shutil.copy2):
    """Copy a tree without the Python caches that image() leaves out."""
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(_PYTHON_CACHE), copy_function=copy_function)


def _versioned(tree: Path, version: str):
    """Give each manifest in tree that Codex may read a plugin's version from that version, so whichever one a Codex
    release reads names it."""
    written = 0
    for relative in _MANIFESTS:
        path = tree / relative
        if _linked(path) or not path.is_file():
            continue
        try:
            manifest = json.loads(path.read_bytes())
        except ValueError as error:
            raise BuildError(f'{path}: not a JSON manifest: {error}') from error
        if not isinstance(manifest, dict):
            raise BuildError(f'{path}: not a JSON object')
        if relative == 'plugin.json' and not str(manifest.get('$schema', '')).startswith(_AGENT_PLUGIN_SCHEMAS):
            continue
        manifest['version'] = version
        path.write_bytes((json.dumps(manifest, indent=2, ensure_ascii=False) + '\n').encode())
        written += 1
    if not written:
        raise BuildError(f'{tree}: no manifest for Codex to read the version from')


class HeldMove(ConcurrentEdit):
    """A move Windows refused: of the tree at path into its place, or, with into, of a plugin's stage into the new
    folder into, which was then not created."""

    def __init__(self, path, refusal: StillHeld, into=None):
        super().__init__(f'{path}: {refusal}')
        self.path, self.refusal, self.into = path, refusal, into

    @property
    def line(self):
        move, outcome = ((f'its move to {self.into}', f'{self.into} was not created, the folder the app loads stays '
                          'in use, and the next pull tries again') if self.into else
                         ('the move', 'this change waits, and the next pull tries it again'))
        return (f'Move refused: {self.path}: Windows refused {move} on each of {self.refusal.attempts} tries over '
                f'{self.refusal.waited:g} seconds ({self.refusal}), as it does while a program holds a file in the '
                f'folder being moved open, or while access rules forbid the move; {outcome}')


def _claude_cache_image(path):
    if _linked(path) or not path.is_dir():
        return image(path)
    entries = {}
    for entry in sorted(path.iterdir()):
        # Claude Code's session leases and orphan time, which it writes while a sync reads, so they go unread. The
        # sync never moves or writes a folder Claude Code loads, so they need no other check.
        if entry.name in {'.in_use', '.orphaned_at'}:
            continue
        child = image(entry)
        if entry.name == _PYTHON_CACHE:
            continue
        if child['kind'] == 'tree':
            entries[entry.name] = {'kind': 'directory'}
            entries.update({f'{entry.name}/{name}': value for name, value in child['entries'].items()})
        elif child != _ABSENT:
            entries[entry.name] = child
    return {'kind': 'tree', 'entries': entries}


def _as_prepared(prepared, path, before, read):
    """Whether path still reads as it did when the sync prepared the artifact. For a Claude plugin, the folder named
    after the pinned release's version may also have gone from missing to a copy of exactly that release: once the
    sync pins the marketplace, Claude Code copies the release there on its own, as it did on each machine on
    19 November 2026 while the sync downloaded the Codex runtime, and the native install the sync runs next writes the
    same bytes there."""
    now = read(path)
    return _same(now, before) or (prepared.artifact.claude_cache and path == prepared.artifact.destination
                                  and _missing(before) and prepared.pristine is not None
                                  and _same(now, prepared.pristine))


def _remnant(actual, expected):
    """Whether a tree is the expected one, whole or with entries gone, as a removal that stopped part-way leaves
    it."""
    return (actual['kind'] == expected['kind'] == 'tree'
            and all(expected['entries'].get(path) == value for path, value in actual['entries'].items()))


def _unfinished_stage(found, target, stage, source):
    return found['kind'] == target['kind'] == 'tree' and all(
        target['entries'].get(path) == value
        or (value['kind'] == 'file' and target['entries'].get(path, {}).get('kind') == 'file'
            and (source / path).read_bytes().startswith((stage / path).read_bytes()))
        for path, value in found['entries'].items())


def _own_work(destination, held):
    """Whether held is the staged tree or the tree moved aside in one of destination's own work folders."""
    work = held.parent
    suffix = work.name.removeprefix('.sync-' + destination.name + '-')
    return (work.parent == destination.parent and suffix != work.name
            and re.fullmatch('[0-9a-f]{32}', suffix) is not None
            and held.name in ('staged', 'displaced') and not _linked(work) and work.is_dir())


def _left_by_swap(pending):
    """The trees an earlier engine's unfinished in-place replacement of a plugin recorded at its paths, as (path,
    image): whichever state it stopped in, the plugin's folder holds one of them, nothing, or only the lease markers
    of a Claude session that started while the folder was moved aside, which a Claude cache's image leaves out."""
    if not pending:
        return []
    path = Path(pending['target']['path'])
    return [(Path(pending['before_path']), pending['before']), (path, pending['destination_before']),
            (path, pending.get('pristine')), (path, pending['target']['image']), (path, {'kind': 'tree', 'entries': {}})]


def _directory_baselines(receipt):
    accepted, pending = (receipt or {}).get('accepted'), (receipt or {}).get('pending')
    known = [accepted['image']] if accepted else []
    if pending:
        known.append(pending['target']['image'])
        if pending.get('pristine') is not None:
            known.append(pending['pristine'])
    return known


def _fields_image(values, syntax):
    return {'kind': f'{syntax}-fields', 'fields': [{'path': list(key), **value} for key, value in sorted(values.items())]}


def _missing(value):
    return value == _ABSENT or (value['kind'].endswith('-fields') and
                                all(not field['present'] for field in value['fields']))


def _physical(path):
    # On Windows, resolve() gives a dangling link's target as the link holds it, so a link in that target stays
    # unresolved until the target exists (seen with Python 3.14.8 on 13 November 2026). The second resolve() resolves
    # it, so a path reads the same before and after its target is written.
    return path.resolve().resolve()


def _bindings(paths):
    return [{'path': str(path), 'physical': str(_physical(path)),
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
    loaded: Path | None = None      # Claude: the catalog folder Claude Code runs the plugin from, not its cache

    @property
    def claude_cache(self):
        return self.kind == 'plugin' and self.key.host == Host.CLAUDE

    @property
    def catalogued(self):
        """A Codex plugin of the engine's personal catalog, whose entry names the source Codex installs it from."""
        return (self.kind == 'plugin' and self.key.host == Host.CODEX
                and self.key.subject.endswith('@' + ENGINE_MARKETPLACE[Host.CODEX]))


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
    taken_over: dict | None = None
    receipt: dict | None = None
    retained: tuple[tuple[Path, dict], ...] = ()


@dataclass(frozen=True)
class Prepared:
    artifacts: tuple[object, ...]
    built: dict
    claude: dict
    catalogs: dict[Path, bytes | None]


class AcceptedInstallations:
    def __init__(self, setup, device, *, configurations=True):
        self.setup, self.device, self.roots = setup, device, device.roots
        self.directory = self.roots.codex_home / 'dotfiles/accepted'
        self.configurations = desired(setup.roots.public.parent if setup.roots.public else None,
                                      setup.roots.private.parent if setup.roots.private else None,
                                      self.roots, device.platform) if configurations else ()
        self.opencode = None
        if configurations:
            receipt = _Artifact('configuration:opencode', None, None, Path(), Path(), (), '', 'fields')
            self.opencode = opencode.plan(setup, self.roots, device.platform, adopted=self._path(receipt).is_file())
            if self.opencode is not None:
                self.configurations += self.opencode.configurations
        self._prepared = {}
        self._installing = set()   # the plugins this sync has still to install with the host's own command
        self.staging = contextlib.ExitStack()   # holds the trees prepare stages; leaving it removes them
        self._refused_move = None

    def _path(self, artifact):
        return self.directory / (hashlib.sha256(artifact.identity.encode()).hexdigest() + '.json')

    def accepted(self, identity):
        """A plugin's accepted record (its path and image), or None when it has none or a replacement is pending."""
        receipt = self._receipt(_Artifact(identity, None, None, Path(), Path(), (), '', 'plugin'))
        return receipt['accepted'] if receipt and not receipt.get('pending') else None

    def catalog_source(self, identity):
        """The staged source the personal catalog names for a Codex plugin: its accepted record's, while that source
        and the folder the record names are intact. None, for the release, where the record names no source, where
        either changed, and while this sync has still to install the plugin with `codex plugin add`, which installs
        whatever the catalog names."""
        record = None if identity in self._installing else self.accepted(identity)
        source = record and record.get('source')
        if not source:
            return None
        try:
            if (_same(image(Path(source['path'])), source['image'])
                    and any(_same(image(Path(record['path'])), held) for held in (record['image'], source['image']))):
                return Path(source['path'])
        except (ConcurrentEdit, OSError):
            pass
        return None

    def names_later(self, identity):
        """Whether this sync names the plugin's catalog source itself once the plugin's folder is in place, which it
        does for each plugin it applies without `codex plugin add`."""
        return identity not in self._installing and any(item.artifact.identity == identity
                                                        for item in self._prepared.values())

    def _catalogued(self, artifact):
        """The source the personal catalog names for a plugin, or None."""
        data = contents(self.roots.agents_home / 'plugins/marketplace.json')
        plugins = json.loads(data).get('plugins', []) if data is not None else []
        name = artifact.key.subject.split('@', 1)[0]
        entry = next((plugin for plugin in plugins if isinstance(plugin, dict) and plugin.get('name') == name), {})
        source = entry.get('source')
        if not isinstance(source, dict) or source.get('source') != 'local' or not isinstance(source.get('path'), str):
            return None
        return self.roots.agents_home.parent / source['path']

    def _catalog_step(self, artifact, accepted):
        """An in-step Codex personal plugin in a folder the engine named needs the catalog to name the source staged
        for that folder, which carries the folder's name as its version. Codex reinstalls a plugin from that source
        whenever the version it reads there differs from the plugin's highest folder, and deletes the plugin's other
        folders."""
        if not artifact.catalogued or Path(accepted['path']) == artifact.destination:
            return None, ''
        source = accepted.get('source')
        if source is None or self._catalogued(artifact) != Path(source['path']):
            return Kind.CHANGED, "Point the personal catalog at the adapted build, with its folder's name as its version"
        actual = image(Path(source['path']))
        if _missing(actual):
            return Kind.CHANGED, 'Stage the adapted build the personal catalog names again'
        if not _same(actual, source['image']):
            return Kind.LOCAL_EDIT, f'The adapted build the personal catalog names was edited: {source["path"]}'
        return None, ''

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
                    install = next(i for i in package.placement(key.host).installs
                                   if plugin_id(package.name, i, key.host) == key.subject)
                    directory = isinstance(install, AlignedInstall)
                    if directory:
                        destination = self.roots.claude_home / 'marketplaces' / marketplace / 'plugins' / name / version
                        current = detail.loaded if isinstance(detail, InstallDetail) and detail.loaded else destination
                    plugin_key = FactKey(key.host, Surface.PLUGIN, key.subject)
                    installed = observations[state.host].facts.get(plugin_key)
                    pin = installed and installed.value.pin
                    # Codex names an installed version after the folders under the plugin's cache folder, so it
                    # names the engine's work folder while a replacement has the version folder moved aside.
                    if key.host == Host.CODEX and (work := _WORK.fullmatch(current.name)):
                        current = current.parent / work[1]
                        pin = Released(work[1]) if isinstance(pin, Released) else pin
                    desired_plugin = facts[plugin_key].value
                    needs_native = installed is None or (not isinstance(desired_plugin.pin, Unpinned) and
                                                         pin != desired_plugin.pin)
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
                                           (destination,), str(bytes_digest(_encoded(fingerprint))),
                                           'directory' if directory else 'plugin', fact.value,
                                           native=needs_native and not directory, provenance=fingerprint,
                                           loaded=detail.loaded if isinstance(detail, InstallDetail) else None))
                elif key.surface in (Surface.AGENT, Surface.LOOSE_SKILL):
                    path = self.roots.codex_home / 'agents' / key.subject if key.surface == Surface.AGENT else self.expand(key.subject)
                    fingerprint = (str(bytes_digest(_encoded({'digest': fact.value.digest, 'paths': device_paths,
                                                             'renderer': _RENDERER})))
                                   if key.surface == Surface.AGENT else str(fact.value.digest))
                    units.append(_Artifact(f'{key.host}:{key.surface.name}:{key.subject}', key, entry, path, path,
                                           (path,), fingerprint, 'role' if key.surface == Surface.AGENT else 'loose', fact.value))
                elif key.surface == Surface.RULES:
                    alias = self.roots.codex_home / 'AGENTS.md' if key.host == Host.CODEX else self.roots.claude_home / 'CLAUDE.md'
                    path = _physical(alias)
                    if path in rules:
                        previous = rules[path]
                        rules[path] = replace(previous, aliases=(*previous.aliases, alias))
                    else:
                        rules[path] = _Artifact('rules:' + str(path), key, entry, path, path, (alias,),
                                                str(self.setup.rules.digest), 'file', fact.value, self.setup.rules.text)
        units.extend(rules.values())
        if self.opencode is not None:
            destination = self.opencode.destination
            units.append(_Artifact('opencode:projection', None, None, destination, destination,
                                   (destination,), str(self.opencode.intent), 'opencode'))
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

    def _read(self, artifact, path=None):
        if artifact.claude_cache:
            return _claude_cache_image(path or artifact.current)
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

    def _read_as_recorded(self, artifact, pending, path):
        if artifact.fields is None:
            return self._read(artifact, path)
        return image(path, dict.fromkeys(tuple(field['path']) for field in pending['target']['image']['fields']),
                     artifact.syntax)

    def _known_pending(self, artifact, pending):
        target = pending['target']
        path = Path(target['path'])
        actual = self._read_as_recorded(artifact, pending, path)
        displaced_known = False
        if pending.get('displaced') and actual == _ABSENT:
            displaced = Path(pending['displaced'])
            if displaced.exists() and _same(self._read_as_recorded(artifact, pending, displaced), pending['swap_before']):
                displaced_known, actual = True, dict(_ABSENT)
        if not displaced_known and not any(_same(actual, known) for known in (target['image'], pending['before'])):
            raise ConcurrentEdit(f'Unknown partial installation preserved: {artifact.identity}')
        return actual

    def _stopped(self, artifact, pending):
        path, displaced = Path(pending['target']['path']), Path(pending['displaced'])
        actual = self._read_as_recorded(artifact, pending, path)
        if _same(actual, pending['target']['image']):
            return _Stop.SWAPPED
        vacant = actual == _ABSENT
        if displaced.exists() or _linked(displaced):
            if (vacant and _own_work(path, displaced)
                    and _same(self._read_as_recorded(artifact, pending, displaced), pending['swap_before'])):
                return _Stop.ASIDE
            return None
        if _same(actual, pending['destination_before']):
            return _Stop.UNSTARTED
        return _Stop.LOST if vacant else None

    def _settled_read(self, artifact, stale, state, path):
        if state is _Stop.ASIDE and path == Path(stale['target']['path']):
            return stale['swap_before']
        return self._read(artifact, path)

    def _taken_over(self, artifact, receipt, state):
        stale = receipt['pending']
        path = Path(stale['target']['path'])
        verified = [(Path(stale['before_path']), stale['before'])]
        if state in (_Stop.ASIDE, _Stop.UNSTARTED):
            verified.append((path, stale['destination_before']))
        accepted = stale['target'] if state is _Stop.SWAPPED else receipt.get('accepted')
        return accepted, partial(self._settled_read, artifact, stale, state), verified

    def _baseline(self, artifact, accepted, read, verified):
        actual = read(artifact.current)
        if (_missing(actual)
                or (accepted and Path(accepted['path']) == artifact.current
                    and self._unchanged(artifact, actual, accepted['image']))
                or any(place == artifact.current and _same(actual, held) for place, held in verified)
                # A folder named for the image it holds is one the engine wrote and nobody edited, also when the sync
                # that wrote it stopped before its receipt.
                or named_for(artifact.current.name, _digest(actual))):
            return actual
        raise ConcurrentEdit(f'Unverified existing content preserved: {artifact.identity}; an independently pinned baseline is required')

    def _destination_admissible(self, artifact, at_target):
        if any(part in {'.git', 'node_modules'} for path in at_target.get('entries', {}) for part in Path(path).parts):
            raise ConcurrentEdit(f'Unowned tree content preserved: {artifact.destination}')

    def _assessment(self, artifact):
        receipt = self._receipt(artifact)
        if artifact.kind == 'directory':
            self._directory_evidence(receipt)
            accepted = receipt and receipt.get('accepted')
            actual = self._read(artifact)
            if accepted and not _missing(actual):
                if (not any(_same(actual, known) for known in _directory_baselines(receipt))
                        and self.roots.tree_digest(artifact.current) != artifact.expected.digest):
                    raise ConcurrentEdit(f'Loaded aligned build differs from its accepted image: {artifact.identity}')
                if (accepted.get('delivery') == 'directory' and accepted['path'] == str(artifact.destination)
                        and artifact.current == artifact.destination and accepted['intent'] == artifact.intent
                        and _same(actual, accepted['image'])):
                    return None, ''
            return Kind.CHANGED, 'Verify and accept the catalog-selected aligned build'
        pending = receipt and receipt.get('pending')
        if pending and artifact.kind == 'plugin':
            self._baseline(artifact, receipt.get('accepted'), partial(self._read, artifact), _left_by_swap(pending))
            return Kind.CHANGED, 'Write a folder of its own beside the replacement an earlier engine left unfinished'
        if pending:
            state = self._stopped(artifact, pending)
            if pending['target']['intent'] == artifact.intent and state is not _Stop.LOST:
                self._known_pending(artifact, pending)
                return Kind.CHANGED, 'Finish a verified pending replacement'
            if state is None:
                raise ConcurrentEdit(f'Unknown partial installation preserved: {artifact.identity}')
            accepted, read, verified = self._taken_over(artifact, receipt, state)
            self._baseline(artifact, accepted, read, verified)
            self._destination_admissible(artifact, read(artifact.destination))
            return Kind.CHANGED, 'Take over a stopped replacement for the newer release'
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
                if _same(actual, (accepted.get('source') or {}).get('image')):
                    return Kind.CHANGED, 'Write the accepted image again in place of the copy Codex made of its catalog source'
                return Kind.LOCAL_EDIT, 'Installed bytes differ from the accepted image'
            if accepted['intent'] == artifact.intent and _same(accepted['bindings'], _bindings(artifact.aliases)):
                # That folder is the built release itself, so it holds the locked tree, while the cache holds the
                # accepted image of the release with its install overlay.
                if artifact.loaded is not None and self.roots.tree_digest(artifact.loaded) != artifact.expected.digest:
                    return Kind.LOCAL_EDIT, (f'The folder Claude Code runs it from, {self.roots.tokens(artifact.loaded)}, '
                                             'differs from its locked release')
                return self._catalog_step(artifact, accepted)
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
        """The Claude plugins whose install record names the folder of an earlier engine's unfinished in-place
        replacement, by tree key, with that folder. The pull gives each one a folder of its own and points the record
        there, so skill reads that fail because that folder is missing do not block it. The plan defers only the reads
        that Claude's observation marks as a missing folder."""
        changed = {difference.key for difference in owned if difference.kind == Kind.CHANGED}
        recoverable = {}
        for artifact in artifacts:
            if artifact.kind != 'plugin' or artifact.key.host != Host.CLAUDE or artifact.key not in changed:
                continue
            plugin = FactKey(Host.CLAUDE, Surface.PLUGIN, artifact.key.subject)
            detail = observations[Host.CLAUDE].details.get(plugin)
            if not isinstance(detail, InstallDetail):
                continue
            try:
                pending = (self._receipt(artifact) or {}).get('pending')
                if pending and detail.root == Path(pending['target']['path']):
                    recoverable[artifact.key] = self.roots.tokens(detail.root)
            except (ConcurrentEdit, OSError, ValueError, KeyError, TypeError):
                continue
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
        catalogs = {self.roots.claude_home / 'marketplaces/codex-aligned/.claude-plugin/marketplace.json':
                    contents(self.roots.claude_home / 'marketplaces/codex-aligned/.claude-plugin/marketplace.json')
                    for a in artifacts if a.kind == 'directory'}
        names = set(packages) | {a.entry.name for a in needed if a.kind in ('plugin', 'directory', 'role')}
        built, claude, roles = {}, {}, {}
        for name in sorted(names):
            package = self.setup.packages[name]
            if package.source is None or not any(p and any(i.managed for i in p.installs) for p in (package.codex, package.claude)):
                continue
            built[name] = builder.codex(package)
            claude.update(builder.claude(package, built[name]))
            if package.codex and any(i.enabled for i in package.codex.installs):
                roles.update(builder.roles(package, built[name]))
        prepared = []
        for artifact in needed:
            source, data, pristine = None, artifact.data, None
            if artifact.kind in ('plugin', 'directory'):
                package = self.setup.packages[artifact.entry.name]
                release = built[package.name] if artifact.key.host == Host.CODEX else claude[artifact.key.subject]
                if self.roots.tree_digest(release.root) != self.setup.lock[package.name].trees[artifact.key.host].digest:
                    raise BuildError(f'Prepared plugin differs from its pinned tree: {artifact.identity}')
                pristine = image(release.root)
                if artifact.kind == 'directory':
                    source = release.root
                    prepared.append(self._prepare_artifact(artifact, self._target(artifact, source, data), source, data, pristine))
                    continue
                scratch = self.roots.engine / '.sync/accepted-images'
                scratch.mkdir(parents=True, exist_ok=True)
                source = Path(self.staging.enter_context(
                    staging_directory(prefix=package.name + '-', dir=scratch))) / 'tree'
                copy_tree(release.root, source)
                adapted = False
                for path, before, after, _, _ in builder.overlay(package, artifact.key.host, artifact.key.subject, source):
                    if before != after:
                        adapted = True
                        if after is None:
                            path.unlink(missing_ok=True)
                        else:
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(after.encode('utf-8'))
                # Codex's plugin/list refresh reinstalls each plugin of a non-curated marketplace whose source gives a
                # version other than its folder's name, and deletes its other folders. Sync points only the personal
                # catalog's source at the adapted build (_source), so any other marketplace would lose it.
                if adapted and artifact.key.host == Host.CODEX and not artifact.catalogued:
                    marketplace = artifact.key.subject.split('@', 1)[1]
                    raise BuildError(
                        f'{artifact.key.subject}: its install overlay changes the plugin, and Codex would replace that '
                        f'adapted build with the {marketplace} marketplace\'s release whenever a client lists plugins, '
                        'because sync points only the personal marketplace at adapted builds; install it with '
                        'via = "personal", or drop the overlay')
            elif artifact.kind == 'opencode':
                scratch = self.roots.engine / '.sync/accepted-images'
                scratch.mkdir(parents=True, exist_ok=True)
                source = Path(self.staging.enter_context(staging_directory(prefix='opencode-', dir=scratch))) / 'tree'
                opencode.materialise(self.opencode, builder, source)
            elif artifact.kind == 'role':
                data = roles[artifact.key.subject]
                if bytes_digest(self.roots.neutral(data)) != artifact.expected.digest:
                    raise BuildError(f'Prepared role differs from its lock: {artifact.identity}')
            elif artifact.kind == 'loose':
                skill = self.setup.loose[artifact.entry.name]
                root_name = next(root for root in SkillRoot if self._skill_root(root) / skill.name == artifact.destination)
                if isinstance(skill.source, LocalSource):
                    source = builder.local(skill.visibility, skill.source, root_name)
                else:
                    if self.setup.roots.private is None:
                        raise BuildError(f'{skill.name}: its snapshot requires the private repository')
                    source = self.setup.roots.private / 'snapshots' / str(skill.source.snapshots[root_name]).removeprefix('sha256:')
                if self.roots.tree_digest(source) != artifact.expected.digest:
                    raise BuildError(f'{skill.name}: source bytes do not match the locked loose skill')
            prepared.append(self._prepare_artifact(artifact, self._target(artifact, source, data), source, data, pristine))
        validate_runtime(self.configurations, self.roots, proposed=True)
        tokens = tuple(object() for _ in prepared)
        self._prepared.update(zip(tokens, prepared))
        self._installing = {item.artifact.identity for item in prepared if item.artifact.native}
        result = Prepared(tokens, built, claude, catalogs)
        self.preflight(result)
        return result

    def rehearse(self, observations):
        """Run prepare's checks against the accepted receipts, without writing, for each artifact whose target
        needs no build: rules and configurations. Plugins, roles and loose skills go unchecked here, because
        their targets exist only after their packages or sources are built."""
        for artifact in self.artifacts(observations):
            if artifact.kind not in ('file', 'fields'):
                continue
            kind, detail = self._assessment(artifact)
            if kind == Kind.LOCAL_EDIT:
                raise ConcurrentEdit(f'{artifact.identity}: {detail}')
            if kind is not None:
                self._prepare_artifact(artifact, self._target(artifact, None, artifact.data), None, artifact.data, None)

    def _target(self, artifact, source, data):
        if artifact.fields is not None:
            return _fields_image({k: {'present': True, 'value': v} for k, v in artifact.fields.items()}, artifact.syntax)
        if source is None:
            return {'kind': 'file', 'sha256': str(bytes_digest(data))}
        target = image(source)
        if artifact.claude_cache and {'.in_use', '.orphaned_at'} & target.get('entries', {}).keys():
            raise BuildError(f'Claude runtime metadata is not source content: {artifact.identity}')
        if any(part in {'.git', 'node_modules'} for path in target.get('entries', {}) for part in Path(path).parts):
            raise BuildError(f'Excluded source content is not ownership evidence: {artifact.identity}')
        return target

    def _discard(self, artifact, stale):
        """Remove the work folder of a replacement a newer release took over or that moved nothing aside, before
        its receipt stops naming it."""
        staged = Path(stale['staged'])
        if Path(stale['displaced']) != staged.parent / 'displaced':
            if staged.parent.exists():
                print(f'Work folder left in place: {staged.parent}: its receipt names a displaced tree outside it',
                      file=sys.stderr)
            return
        self._remove_work(artifact, Path(stale['target']['path']), staged, stale['target']['image'])

    def _remove_work(self, artifact, destination, held, expected):
        work = held.parent
        if not _own_work(destination, held):
            if work.exists():
                print(f"Work folder left in place: {work}: it is not one of {destination}'s work folders",
                      file=sys.stderr)
            return

        def kept():
            if {entry.name for entry in work.iterdir()} - {held.name}:
                return 'it holds more than the tree'
            if not held.exists():
                return None
            if not (isinstance(expected, dict) and expected.get('kind') == 'tree'
                    and isinstance(expected.get('entries'), dict)):
                return 'its record holds no image of the tree'
            actual = image(held)
            if not _remnant(actual, expected):
                return 'its tree is not the one its record holds'
            return None

        try:
            reason = kept()
            if reason is None:
                shutil.rmtree(work)
        except (ConcurrentEdit, OSError) as error:
            reason = error
        if reason is not None:
            print(f'Work folder left in place: {work}: {reason}', file=sys.stderr)

    def _accept(self, artifact, record, accepted, pending):
        changed, displaced = None, pending and Path(pending['displaced'])
        if displaced and displaced.exists():
            try:
                if not _same(self._read(artifact, displaced), pending['swap_before']):
                    changed = 'its tree is not the one its receipt holds'
            except (ConcurrentEdit, OSError) as error:
                changed = error
            if changed is None:
                record['backup'], record['backup_image'] = pending['displaced'], pending['swap_before']
            else:
                print(f'Work folder left in place: {displaced.parent}: the tree moved aside changed: {changed}',
                      file=sys.stderr)
        elif (not pending and accepted and accepted.get('backup') and accepted['path'] == record['path']
              and _same(accepted['image'], record['image'])):
            record.update({key: accepted[key] for key in ('backup', 'backup_image') if key in accepted})
        if pending and 'backup' not in record and not (changed and _own_work(Path(record['path']), displaced)):
            self._discard(artifact, pending)
        if accepted and accepted.get('backup') and accepted['backup'] != record.get('backup'):
            self._remove_work(artifact, Path(accepted['path']), Path(accepted['backup']), accepted.get('backup_image'))
        self._save(artifact, record)

    def _prepare_artifact(self, artifact, target, source, data, pristine):
        receipt = self._receipt(artifact)
        pending = receipt and receipt.get('pending')
        accepted, read, verified = receipt and receipt.get('accepted'), partial(self._read, artifact), []
        if artifact.kind == 'directory':
            retained = self._directory_evidence(receipt)
            verified = [(artifact.current, known) for known in (target, *_directory_baselines(receipt))]
            before = self._baseline(artifact, accepted, read, verified)
            destination = read(artifact.destination)
            if not _missing(destination) and not _same(destination, target):
                raise ConcurrentEdit(f'Immutable aligned build was edited: {artifact.destination}')
            return _PreparedArtifact(artifact, target, before, artifact.current,
                                     _bindings((*artifact.aliases, artifact.current)), destination, pristine, source, data,
                                     receipt=receipt, retained=retained)
        if artifact.kind == 'plugin':
            # The folder the host loads stays as it is, but the host stops loading it, so it has to hold what the
            # receipt, the pinned release, the target or an earlier engine's unfinished replacement put there, the
            # copy Codex makes of the catalog source the receipt names, or the image its engine-made name was made for.
            verified = [(artifact.current, pristine), (artifact.current, target), *_left_by_swap(pending)]
            if accepted and accepted.get('source'):
                verified.append((Path(accepted['path']), accepted['source']['image']))
            return _PreparedArtifact(artifact, target, self._baseline(artifact, accepted, read, verified),
                                     artifact.current, _bindings((*artifact.aliases, artifact.current)),
                                     read(artifact.destination), pristine, source, data)
        if pending:
            state = self._stopped(artifact, pending)
            if (state is not _Stop.LOST and pending['target']['intent'] == artifact.intent
                    and _same(pending['target']['image'], target)):
                self._known_pending(artifact, pending)
                return _PreparedArtifact(artifact, target, pending['before'], Path(pending['before_path']),
                                         pending['bindings'], pending['destination_before'], pristine, source, data,
                                         pending)
            if state is None:
                raise ConcurrentEdit(f'Unknown partial installation preserved: {artifact.identity}')
            accepted, read, verified = self._taken_over(artifact, receipt, state)
        at_target = read(artifact.destination)
        before = (read(artifact.current) if _same(at_target, target)
                  else self._baseline(artifact, accepted, read, verified))
        self._destination_admissible(artifact, at_target)
        return _PreparedArtifact(artifact, target, before, artifact.current,
                                 _bindings((*artifact.aliases, artifact.current)), at_target, pristine, source, data,
                                 taken_over=pending or None)

    def _check(self, prepared):
        artifact = prepared.artifact
        if not _same(prepared.bindings, _bindings(tuple(Path(b['path']) for b in prepared.bindings))):
            raise ConcurrentEdit(f'Path binding changed after preparation: {artifact.identity}')
        if prepared.pending:
            self._known_pending(artifact, prepared.pending)
        else:
            read = partial(self._read, artifact)
            if prepared.taken_over:
                state = self._stopped(artifact, prepared.taken_over)
                if state is None:
                    raise ConcurrentEdit(f'Unknown partial installation preserved: {artifact.identity}')
                read = partial(self._settled_read, artifact, prepared.taken_over, state)
            if not (_as_prepared(prepared, prepared.before_path, prepared.before, read)
                    and _as_prepared(prepared, artifact.destination, prepared.destination_before, read)):
                raise ConcurrentEdit(f'Changed after preparation: {artifact.identity}')
        if prepared.source is not None and not _same(image(prepared.source), prepared.target):
            raise ConcurrentEdit(f'Prepared source changed: {artifact.identity}')
        if artifact.kind == 'directory' and (not _same(self._receipt(artifact), prepared.receipt)
                                            or self._directory_evidence(prepared.receipt) != prepared.retained):
            raise ConcurrentEdit(f'Aligned receipt evidence changed after preparation: {artifact.identity}')

    def preflight(self, prepared):
        for path, before in prepared.catalogs.items():
            if contents(path) != before:
                raise ConcurrentEdit(f'Aligned catalog changed after preparation: {path}')
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

    def apply(self, token, files, *, native=None, catalog=None):
        """native(host, plugin id) installs a plugin with the host's own command. catalog(plugin id, source) names
        source, or the release when None, as the plugin's source in the personal catalog."""
        prepared = self._resolve(token)
        if prepared.artifact.kind == 'directory':
            self._directory(prepared)
            return
        self._check(prepared)
        artifact = prepared.artifact
        if artifact.kind == 'plugin':
            self._plugin(prepared, files, native, catalog)
            return
        if prepared.taken_over:
            self._settle(artifact, prepared.taken_over)
            prepared = replace(prepared, taken_over=None)
        destination = artifact.destination
        record = {'path': str(destination), 'bindings': _bindings(artifact.aliases),
                  'intent': artifact.intent, 'image': prepared.target, 'provenance': artifact.provenance}
        receipt = self._receipt(artifact)
        accepted = receipt and receipt.get('accepted')
        if _same(self._read(artifact, destination), prepared.target):
            self._accept(artifact, record, accepted, prepared.pending)
            return
        work = destination.parent / ('.sync-' + destination.name + '-' + uuid.uuid4().hex)
        pending = prepared.pending or {'before': prepared.before, 'before_path': str(prepared.before_path),
                                       'bindings': prepared.bindings, 'destination_before': prepared.destination_before,
                                       'target': record, 'staged': str(work / 'staged'),
                                       'displaced': str(work / 'displaced'), 'swap_before': prepared.destination_before}
        try:
            self._save(artifact, accepted, pending)
            self._check(prepared)
            actual = self._known_pending(artifact, pending)
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
        except (ConcurrentEdit, OSError) as error:
            self._try_put_back(artifact, pending)
            if error is self._refused_move and self._pending_unchanged(artifact, pending, prepared.target):
                raise HeldMove(destination, error) from error
            raise
        if not _same(self._read(artifact, destination), prepared.target):
            raise ConcurrentEdit(f'Effective output failed verification: {artifact.identity}')
        self._accept(artifact, record, accepted, pending)

    def _directory_evidence(self, receipt):
        if not receipt:
            return ()
        accepted, pending = receipt.get('accepted'), receipt.get('pending')
        known = {}
        if accepted:
            known.setdefault(Path(accepted['path']), []).append(accepted['image'])
        for path, expected in _left_by_swap(pending):
            if expected is not None:
                known.setdefault(path, []).append(expected)
        if pending:
            for key, expected in (('staged', pending['target']['image']), ('displaced', pending['swap_before'])):
                known.setdefault(Path(pending[key]), []).append(expected)
        retained = []
        for path, expected in known.items():
            actual = image(path) if accepted and accepted.get('delivery') == 'directory' else _claude_cache_image(path)
            if not _missing(actual) and not any(_same(actual, value) for value in expected):
                raise ConcurrentEdit(f'Previously accepted aligned content was edited: {path}')
            retained.append((path, actual))
        return tuple(retained)

    def _directory(self, prepared):
        artifact = prepared.artifact
        if not _same(self._receipt(artifact), prepared.receipt):
            raise ConcurrentEdit(f'Aligned receipt changed after preparation: {artifact.identity}')
        if self._directory_evidence(prepared.receipt) != prepared.retained:
            raise ConcurrentEdit(f'Previous aligned content changed after preparation: {artifact.identity}')
        if not _same(prepared.bindings, _bindings(tuple(Path(b['path']) for b in prepared.bindings))):
            raise ConcurrentEdit(f'Path binding changed after preparation: {artifact.identity}')
        if artifact.current != artifact.destination and not _same(self._read(artifact), prepared.before):
            raise ConcurrentEdit(f'Loaded aligned content changed after preparation: {artifact.identity}')
        directory = self.roots.claude_home / 'marketplaces/codex-aligned'
        catalog = json.loads((directory / '.claude-plugin/marketplace.json').read_bytes())
        if _loaded({'codex-aligned': (directory, catalog)}, artifact.key.subject, directory) != artifact.destination:
            raise ConcurrentEdit(f'Aligned catalog no longer selects the prepared build: {artifact.identity}')
        if not _same(image(artifact.destination), prepared.target) or not _same(image(prepared.source), prepared.target):
            raise ConcurrentEdit(f'Published aligned content differs from preparation: {artifact.identity}')
        record = {'path': str(artifact.destination), 'bindings': _bindings(artifact.aliases), 'intent': artifact.intent,
                  'image': prepared.target, 'provenance': artifact.provenance, 'delivery': 'directory'}
        if prepared.receipt:
            accepted = prepared.receipt.get('accepted') or {}
            if accepted.get('delivery') != 'directory':
                record['legacy'] = prepared.receipt
            elif 'legacy' in accepted:
                record['legacy'] = accepted['legacy']
        self._save(artifact, record)

    def _plugin(self, prepared, files, native, catalog):
        artifact = prepared.artifact
        if artifact.catalogued and catalog is None:
            raise RuntimeError(f'Catalog writer required: {artifact.identity}')
        loaded = artifact.current
        if artifact.native:
            if native is None:
                raise RuntimeError(f'Native installer required: {artifact.identity}')
            native(artifact.key.host, artifact.key.subject)
            self._installing.discard(artifact.identity)
            loaded = artifact.destination
            if not any(_same(self._read(artifact, loaded), known) for known in (prepared.pristine, prepared.target)):
                raise ConcurrentEdit(f'Unknown native output preserved: {artifact.identity}')
        record = self._claude_record(artifact, loaded) if artifact.key.host == Host.CLAUDE else None
        if _same(self._read(artifact, loaded), prepared.target):
            folder, source = loaded, self._source(prepared, loaded)
        else:
            folder, source = self._write_folder(prepared)
        if record is not None and Path(record[2]['installPath']) != folder:
            before, data, entry = record
            entry['installPath'] = str(folder)
            files.write(self.roots.claude_home / 'plugins/installed_plugins.json',
                        json.dumps(data, indent=2, ensure_ascii=False).encode(), expected=before)
        if not _same(self._read(artifact, folder), prepared.target):
            raise ConcurrentEdit(f'Effective output failed verification: {artifact.identity}')
        # After the check, so that a folder Codex replaced meanwhile stops the sync with the catalog naming the
        # source of the folder the receipt still names.
        if artifact.catalogued:
            catalog(artifact.key.subject, Path(source['path']) if source else None)
        receipt = self._receipt(artifact) or {}
        named = ((receipt.get('accepted') or {}).get('backup'), *((receipt.get('pending') or {}).get(key)
                                                                  for key in ('staged', 'displaced')))
        if left := sorted({str(Path(path).parent) for path in named if path and Path(path).parent.exists()}):
            print(f'Work folders left in place: {", ".join(left)}: an earlier engine replaced {artifact.identity} in '
                  'place there, and this one leaves those folders as they are', file=sys.stderr)
        accepted = {'path': str(folder), 'bindings': _bindings(artifact.aliases), 'intent': artifact.intent,
                    'image': prepared.target, 'provenance': artifact.provenance}
        if source:
            accepted['source'] = source
        self._save(artifact, accepted)

    def _source(self, prepared, folder):
        """For a Codex plugin of the personal catalog in a folder the engine named, the source the catalog is to name:
        the accepted image with folder's name as the version of each manifest Codex may read it from, staged under
        ~/.agents/plugins/sources/<plugin>/ in a folder named by the digest of its tree, as {'path', 'image'}. None for
        any other plugin, and for one in the folder of its release, whose version its manifest already gives."""
        artifact = prepared.artifact
        if not artifact.catalogued or folder == artifact.destination:
            return None
        sources = self.roots.agents_home / 'plugins/sources'
        sources.mkdir(parents=True, exist_ok=True)
        work = Path(staging_folder(prefix=f'.sync-{folder.parent.name}-', dir=sources))
        staged, destination = work / 'tree', None
        try:
            copy_tree(prepared.source, staged, shutil.copy)
            _versioned(staged, folder.name)
            expected = image(staged)
            destination = sources / folder.parent.name / str(self.roots.tree_digest(staged)).removeprefix('sha256:')
            if destination.exists() or _linked(destination):
                if not _same(image(destination), expected):
                    raise ConcurrentEdit(f'Cached immutable source was edited: {destination}')
            else:
                def ready():
                    if destination.exists() or _linked(destination):
                        raise ConcurrentEdit(f'Tree changed during replacement: {destination}')
                    if not _same(image(staged), expected):
                        raise ConcurrentEdit(f'Prepared stage was edited: {staged}')

                destination.parent.mkdir(exist_ok=True)
                ready()
                while_held(lambda: staged.rename(destination), before_retry=ready)
        except StillHeld as refusal:
            raise HeldMove(staged, refusal, into=destination) from refusal
        finally:
            try:
                shutil.rmtree(work)
            except OSError as error:
                print(f'Work folder left in place: {work}: {error}', file=sys.stderr)
        return {'path': str(destination), 'image': expected}

    def _claude_record(self, artifact, loaded):
        """Claude Code's install record as read, parsed, and with the plugin's user-scope entry, which must name the
        folder it loads. Only that entry's installPath changes. An isolated probe on 14 November 2026 showed
        `claude plugin update` putting a record whose version is not the upstream one back on the upstream folder, and
        leaving one whose version is the upstream one where it points. Other entries, such as a project's, are not the
        engine's."""
        before = contents(self.roots.claude_home / 'plugins/installed_plugins.json')
        data = json.loads(before) if before is not None else {}
        users = [entry for entry in data.get('plugins', {}).get(artifact.key.subject, []) if entry.get('scope') == 'user']
        if len(users) != 1 or Path(users[0].get('installPath', '')) != loaded:
            raise ConcurrentEdit(f"Claude Code's install record does not name {loaded} for {artifact.identity}")
        return before, data, users[0]

    def _write_folder(self, prepared):
        """Stage the target beside the plugin's folder, outside the one Codex reads versions from, and move it into
        a version folder of its own with one rename, so a host sees all of it or none. The folder's catalog source,
        if it needs one, is staged first, so a refused move of either leaves no new folder. Gives the folder and that
        source."""
        artifact = prepared.artifact
        plugin = artifact.destination.parent
        version = self.setup.lock[artifact.entry.name].trees[artifact.key.host].version
        plugin.mkdir(parents=True, exist_ok=True)
        name = version_folder(version, _digest(prepared.target), datetime.now(timezone.utc),
                              [path.name for path in plugin.iterdir()])
        folder = plugin / (name.replace('+', '-') if artifact.key.host == Host.CLAUDE else name)
        source = self._source(prepared, folder)
        work = Path(staging_folder(prefix=f'.sync-{plugin.name}-', dir=plugin.parent))
        staged = work / 'staged'

        def ready():
            if folder.exists() or _linked(folder):
                raise ConcurrentEdit(f'Tree changed during replacement: {folder}')
            if not _same(self._read(artifact, staged), prepared.target):
                raise ConcurrentEdit(f'Prepared stage was edited: {staged}')

        try:
            copy_tree(prepared.source, staged, shutil.copy)
            ready()
            while_held(lambda: staged.rename(folder), before_retry=ready)
        except StillHeld as refusal:
            raise HeldMove(staged, refusal, into=folder) from refusal
        finally:
            try:
                shutil.rmtree(work)
            except OSError as error:
                print(f'Work folder left in place: {work}: {error}', file=sys.stderr)
        return folder, source

    def _pending_unchanged(self, artifact, pending, target):
        try:
            receipt = self._receipt(artifact)
            if not receipt or not _same(receipt.get('pending'), pending):
                return False
            self._known_pending(artifact, pending)
            return (self._stopped(artifact, pending) is _Stop.UNSTARTED
                    and _same(image(Path(pending['staged'])), target))
        except (ConcurrentEdit, OSError, ValueError, KeyError, TypeError, AttributeError):
            return False

    def _settle(self, artifact, stale):
        receipt = self._receipt(artifact)
        state = self._stopped(artifact, stale)
        if state is None or not (receipt and _same(receipt.get('pending'), stale)):
            raise ConcurrentEdit(f'Pending replacement changed after preparation: {artifact.identity}')
        if state is _Stop.SWAPPED:
            self._accept(artifact, dict(stale['target']), receipt.get('accepted'), stale)
            return
        if state is _Stop.ASIDE:
            try:
                self._put_back(artifact, stale)
            except (ConcurrentEdit, OSError) as error:
                raise ConcurrentEdit(f'Old tree left aside: {stale["displaced"]}, from {stale["target"]["path"]}: '
                                     f'{error}') from error
        self._discard(artifact, stale)

    def _put_back(self, artifact, pending):
        destination, displaced = Path(pending['target']['path']), Path(pending['displaced'])
        if not displaced.exists():
            return

        def unchanged():
            if not _same(self._read(artifact, displaced), pending['swap_before']):
                raise ConcurrentEdit('it changed after it was moved aside')

        def ready():
            unchanged()
            if destination.exists() or _linked(destination):
                raise ConcurrentEdit('its place is taken')

        ready()
        while_held(lambda: displaced.rename(destination), before_retry=ready)

    def _try_put_back(self, artifact, pending):
        try:
            self._put_back(artifact, pending)
        except (ConcurrentEdit, OSError) as error:
            print(f'Old tree left aside: {pending["displaced"]}, from {pending["target"]["path"]}: {error}',
                  file=sys.stderr)

    def _tree(self, prepared, pending, accepted, actual):
        artifact = prepared.artifact
        destination, staged, displaced = artifact.destination, Path(pending['staged']), Path(pending['displaced'])
        if any(part in {'.git', 'node_modules'} for path in actual.get('entries', {}) for part in Path(path).parts):
            raise ConcurrentEdit(f'Unowned tree content preserved: {destination}')
        staged.parent.mkdir(parents=True, exist_ok=True)
        if staged.exists():
            found = image(staged)
            if not (staged.name == 'staged' and _own_work(destination, staged)
                    and (_same(found, prepared.target) or _unfinished_stage(found, prepared.target, staged, prepared.source))):
                raise ConcurrentEdit(f'Prepared stage was edited: {staged}')
            if not _same(found, prepared.target):
                shutil.rmtree(staged)
        if not staged.exists():
            # On Windows shutil.copy2 copies through CopyFile2, which gives a file its full length before its bytes,
            # so a copy killed inside a file leaves zeros where the stage rule needs a short file.
            copy_tree(prepared.source, staged, shutil.copy)
        if not _same(image(staged), prepared.target):
            raise ConcurrentEdit(f'Prepared stage failed verification: {staged}')

        def stage_intact():
            if not _same(image(staged), prepared.target):
                raise ConcurrentEdit(f'Prepared stage was edited: {staged}')

        def unchanged():
            if not _same(self._read(artifact, destination), actual):
                raise ConcurrentEdit(f'Tree changed before replacement: {destination}')

        def movable():
            unchanged()
            stage_intact()

        def ready():
            if destination.exists():
                raise ConcurrentEdit(f'Tree changed during replacement: {destination}')
            stage_intact()

        unchanged()
        pending['swap_before'] = actual if actual != _ABSENT else pending['swap_before']
        self._save(artifact, accepted, pending)
        unchanged()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if displaced.exists():
                raise ConcurrentEdit(f'Displaced tree already exists: {displaced}')
            stage_intact()
            self._move(lambda: destination.rename(displaced), movable)
        ready()
        self._move(lambda: staged.rename(destination), ready)

    def _move(self, move, before_retry):
        try:
            while_held(move, before_retry=before_retry)
        except StillHeld as refusal:
            self._refused_move = refusal
            raise

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
