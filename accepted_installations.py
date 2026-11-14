from __future__ import annotations

import contextlib
from dataclasses import dataclass, replace
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
from description import (AlignedInstall, LocalSource, RepoSource, SkillRoot, ToolNotes, _canonical,
                         plugin_id)
from drift import Difference, EntryRef, Kind
from host_state import FactKey, Host, InstallDetail, Released, Surface, Unpinned, bytes_digest
from portable import WINDOWS, StillHeld, process_running, staging_directory, replace as replace_path, while_held
from sync_files import ConcurrentEdit, contents
from sync_sources import BuildError


_ABSENT = {'kind': 'absent'}
_RENDERER = 1
_PYTHON_CACHE = '__pycache__'
_WORK = re.compile(r'\.sync-(.+)-[0-9a-f]{32}')


class _Stop(Enum):
    SWAPPED = 'swapped'
    ASIDE = 'aside'
    UNSTARTED = 'unstarted'
    NATIVE = 'native'
    LOST = 'lost'


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def _same(left, right):
    return _encoded(left) == _encoded(right)


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


def _lease_held(marker):
    """Whether the Claude process a lease marker names still runs. A marker whose process has ended holds nothing,
    and sessions that exit uncleanly leave such markers behind. Linux markers record the process's start time."""
    # Claude's Windows markers write procStart in a format of their own, not the one process_start uses there.
    return process_running(marker['pid'], None if WINDOWS else marker.get('procStart'))


def _claude_runtime(path):
    """The process IDs whose live lease markers hold this Claude cache folder."""
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
            return ()
        if not stat.S_ISDIR(info.st_mode):
            raise ConcurrentEdit(f'Claude lease path is not a directory: {path}')
        leases = tuple(path.iterdir())
    except FileNotFoundError:
        if path.exists() or _linked(path):
            raise ConcurrentEdit(f'Claude runtime metadata changed during read: {path}')
        return ()
    holders = set()
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
        if _lease_held(marker):
            holders.add(marker['pid'])
    return tuple(sorted(holders))


def copy_tree(source: Path, destination: Path, copy_function=shutil.copy2):
    """Copy a tree without the Python caches that image() leaves out."""
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(_PYTHON_CACHE), copy_function=copy_function)


class HeldCache(ConcurrentEdit):
    """A Claude plugin cache that running Claude processes hold, raised before its replacement moves anything.
    Its replacement stays pending, so the pull applies the rest of the release and the first pull after those
    processes exit finishes it."""

    def __init__(self, path, holders):
        super().__init__(f'running Claude processes {", ".join(map(str, holders))} hold {path}')
        self.path, self.holders = path, holders

    @property
    def line(self):
        return (f'Claude lease: {self.path}, held by running Claude processes {", ".join(map(str, self.holders))}, '
                'stays as it is; the first pull after they exit replaces it')


class HeldMove(ConcurrentEdit):
    def __init__(self, path, refusal: StillHeld):
        super().__init__(f'{path}: {refusal}')
        self.path, self.refusal = path, refusal

    @property
    def line(self):
        return (f'Move refused: {self.path}: Windows refused the move on each of {self.refusal.attempts} tries over '
                f'{self.refusal.waited:g} seconds ({self.refusal}), as it does while a program holds a file in the '
                'folder being moved open, or while access rules forbid the move; this replacement waits as a pending '
                'one, and the next pull tries it again')


def _claude_cache_image(path, *, replacing=False):
    if _linked(path) or not path.is_dir():
        return image(path)
    entries = {}
    for entry in sorted(path.iterdir()):
        if entry.name in {'.in_use', '.orphaned_at'}:
            holders = _claude_runtime(entry)
            if holders and replacing:
                raise HeldCache(path, holders)
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


def _skill_text_only(before, after):
    """Whether two images of a Claude plugin cache differ only in the bytes of Markdown files under skills/ that both
    hold, as tool notes and skill invocation choices change them. A running Claude session reads such a file when it
    uses the skill, so it gets the new text the next time it opens the file, and it keeps the skill list and hook
    settings it loaded at startup either way."""
    if before.get('kind') != 'tree' or after.get('kind') != 'tree':
        return False
    old, new = before['entries'], after['entries']
    if old.keys() != new.keys():
        return False
    return all(old[path] == new[path] or (old[path]['kind'] == new[path]['kind'] == 'file' and path.endswith('.md')
                                          and PurePosixPath(path).parts[0] == 'skills')
               for path in old)


def _carry_leases(source, target, *, overwrite=False):
    """Move source's lease markers into target's, so that a later replacement of target sees the Claude sessions
    that wrote them. A marker still being written, a .tmp file, stays, because Claude Code renames it in place."""
    leases = source / '.in_use'
    if _linked(leases) or not leases.is_dir():
        return
    (target / '.in_use').mkdir(exist_ok=True)
    for marker in leases.iterdir():
        if '.tmp.' not in marker.name and (overwrite or not (target / '.in_use' / marker.name).exists()):
            marker.replace(target / '.in_use' / marker.name)


def _fold_leases(folder, tree):
    _carry_leases(folder, tree, overwrite=True)
    try:
        if (folder / '.in_use').exists():
            (folder / '.in_use').rmdir()
        folder.rmdir()
    except OSError as error:
        raise ConcurrentEdit(f'Tree changed before replacement: {folder}') from error


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


def _only_leases(path):
    """A Claude session that starts while a swap has the plugin's folder moved aside can make a folder there that
    holds only its lease marker, as Claude Code 2.1.322's marker writer creates the folder it writes into."""
    return not _linked(path) and path.is_dir() and {entry.name for entry in path.iterdir()} <= {'.in_use'}


def _own_work(destination, held):
    """Whether held is the staged tree or the tree moved aside in one of destination's own work folders."""
    work = held.parent
    suffix = work.name.removeprefix('.sync-' + destination.name + '-')
    return (work.parent == destination.parent and suffix != work.name
            and re.fullmatch('[0-9a-f]{32}', suffix) is not None
            and held.name in ('staged', 'displaced') and not _linked(work) and work.is_dir())


def _cache_inputs(provenance, notes):
    """What a Claude plugin cache's bytes follow in its provenance: the locked Claude tree, the package and its
    source, and the tool notes when the package applies them. Every plugin's intent names every tool note."""
    provenance = provenance or {}
    return [((provenance.get('lock') or {}).get('trees') or {}).get('claude'), provenance.get('package'),
            provenance.get('repository'), provenance.get('notes') if notes else None]


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
        self.opencode = None
        if configurations:
            receipt = _Artifact('configuration:opencode', None, None, Path(), Path(), (), '', 'fields')
            self.opencode = opencode.plan(setup, self.roots, device.platform, adopted=self._path(receipt).is_file())
            if self.opencode is not None:
                self.configurations += self.opencode.configurations
        self._prepared = {}
        self.staging = contextlib.ExitStack()   # holds the trees prepare stages; leaving it removes them
        self._refused_move = None

    def _path(self, artifact):
        return self.directory / (hashlib.sha256(artifact.identity.encode()).hexdigest() + '.json')

    def accepted(self, identity):
        """A plugin's accepted record (its path and image), or None when it has none or a replacement is pending."""
        receipt = self._receipt(_Artifact(identity, None, None, Path(), Path(), (), '', 'plugin'))
        return receipt['accepted'] if receipt and not receipt.get('pending') else None

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
                                           (destination,), str(bytes_digest(_encoded(fingerprint))), 'plugin', fact.value,
                                           native=needs_native, provenance=fingerprint,
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

    def _read_as_recorded(self, artifact, pending, path):
        if artifact.fields is None:
            return self._read(artifact, path)
        return image(path, dict.fromkeys(tuple(field['path']) for field in pending['target']['image']['fields']),
                     artifact.syntax)

    def _known_pending(self, artifact, pending):
        target = pending['target']
        path = Path(target['path'])
        actual = self._read_as_recorded(artifact, pending, path)
        known = [target['image'], pending.get('pristine')]
        if str(path) == pending['before_path']:
            known.append(pending['before'])
        else:
            known.append(pending['destination_before'])
        displaced_known = False
        if pending.get('displaced') and (actual == _ABSENT or (artifact.claude_cache and _only_leases(path))):
            displaced = Path(pending['displaced'])
            if displaced.exists() and _same(self._read_as_recorded(artifact, pending, displaced), pending['swap_before']):
                displaced_known, actual = True, dict(_ABSENT)
        if not displaced_known and not any(candidate is not None and _same(actual, candidate) for candidate in known):
            raise ConcurrentEdit(f'Unknown partial installation preserved: {artifact.identity}')
        old = Path(pending['before_path'])
        if old != path:
            # Codex's installer and Claude Code's sweep of its cache remove old version folders.
            found = self._read_as_recorded(artifact, pending, old)
            if found != _ABSENT and not _same(found, pending['before']):
                raise ConcurrentEdit(f'Previous installation changed during replacement: {artifact.identity}')
        return actual

    def _stopped(self, artifact, pending):
        path, displaced = Path(pending['target']['path']), Path(pending['displaced'])
        actual = self._read_as_recorded(artifact, pending, path)
        if _same(actual, pending['target']['image']):
            return _Stop.SWAPPED
        vacant = actual == _ABSENT or (artifact.claude_cache and _only_leases(path))
        if displaced.exists() or _linked(displaced):
            if (vacant and _own_work(path, displaced)
                    and _same(self._read_as_recorded(artifact, pending, displaced), pending['swap_before'])):
                return _Stop.ASIDE
            return None
        if _same(actual, pending['destination_before']):
            return _Stop.UNSTARTED
        if _same(actual, pending.get('pristine')):
            return _Stop.NATIVE
        return _Stop.LOST if vacant else None

    def _settled_read(self, artifact, stale, state, path):
        if state is _Stop.ASIDE and path == Path(stale['target']['path']):
            return stale['swap_before']
        return self._read(artifact, path)

    def _taken_over(self, artifact, receipt, state):
        stale = receipt['pending']
        path = Path(stale['target']['path'])
        verified = [(Path(stale['before_path']), stale['before'])]
        if state in (_Stop.NATIVE, _Stop.ASIDE, _Stop.UNSTARTED):
            verified += [(path, stale['destination_before']), (path, stale['pristine'])]
        elif state is _Stop.LOST and artifact.claude_cache:
            verified.append((path, {'kind': 'tree', 'entries': {}}))
        accepted = stale['target'] if state is _Stop.SWAPPED else receipt.get('accepted')
        return accepted, partial(self._settled_read, artifact, stale, state), verified

    def _baseline(self, artifact, accepted, read, verified):
        actual = read(artifact.current)
        if (_missing(actual)
                or (accepted and Path(accepted['path']) == artifact.current
                    and self._unchanged(artifact, actual, accepted['image']))
                or any(place == artifact.current and _same(actual, held) for place, held in verified)):
            return actual
        raise ConcurrentEdit(f'Unverified existing content preserved: {artifact.identity}; an independently pinned baseline is required')

    def _destination_admissible(self, artifact, at_target, verified, target=None, pristine=None):
        if artifact.current != artifact.destination and not _missing(at_target):
            if any(place == artifact.destination and _same(at_target, held) for place, held in verified):
                known = True
            elif target is None:
                known = (artifact.destination.is_dir()
                         and self.roots.tree_digest(artifact.destination) == artifact.expected.digest)
            else:
                # A native host can cache the pinned release before its installer registers it, as Claude Code's
                # plugin listing does for a marketplace that moved to the new version.
                known = _same(at_target, target) or (artifact.native and _same(at_target, pristine))
            if not known:
                raise ConcurrentEdit(f'Unverified destination preserved: {artifact.destination}')
        if any(part in {'.git', 'node_modules'} for path in at_target.get('entries', {}) for part in Path(path).parts):
            raise ConcurrentEdit(f'Unowned tree content preserved: {artifact.destination}')

    def _assessment(self, artifact):
        receipt = self._receipt(artifact)
        pending = receipt and receipt.get('pending')
        if pending:
            state = self._stopped(artifact, pending)
            if pending['target']['intent'] == artifact.intent and state is not _Stop.LOST:
                self._known_pending(artifact, pending)
                return Kind.CHANGED, 'Finish a verified pending replacement'
            if state is None:
                raise ConcurrentEdit(f'Unknown partial installation preserved: {artifact.identity}')
            accepted, read, verified = self._taken_over(artifact, receipt, state)
            self._baseline(artifact, accepted, read, verified)
            self._destination_admissible(artifact, read(artifact.destination), verified)
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
                return Kind.LOCAL_EDIT, 'Installed bytes differ from the accepted image'
            if accepted['intent'] == artifact.intent and _same(accepted['bindings'], _bindings(artifact.aliases)):
                # That folder is the built release itself, so it holds the locked tree, while the cache holds the
                # accepted image of the release with its install overlay.
                if artifact.loaded is not None and self.roots.tree_digest(artifact.loaded) != artifact.expected.digest:
                    return Kind.LOCAL_EDIT, (f'The folder Claude Code runs it from, {self.roots.tokens(artifact.loaded)}, '
                                             'differs from its locked release')
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
            if not isinstance(detail, InstallDetail):
                continue
            try:
                receipt = self._receipt(artifact)
                pending = receipt and receipt.get('pending')
                if not pending:
                    continue
                target = pending['target']
                stopped = (self._stopped(artifact, pending) if detail.root == Path(target['path'])
                           and (not detail.root.exists() or _only_leases(detail.root)) else None)
                newer = target['intent'] != artifact.intent
                staged = Path(pending['staged'])
                if stopped is _Stop.LOST or (stopped is _Stop.ASIDE and newer) or (
                        stopped is _Stop.UNSTARTED
                        and (newer or not staged.exists() or _remnant(image(staged), target['image']))):
                    recoverable[artifact.key] = self.roots.tokens(detail.root)
                    continue
                if detail.root != artifact.destination:
                    continue
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
                        or _linked(work) or (image(destination) != _ABSENT and not _only_leases(destination))
                        or not _same(image(staged), target['image'])
                        or pending['swap_before']['kind'] != 'tree'
                        or not _same(self._read_as_recorded(artifact, pending, displaced), pending['swap_before'])
                        or not any(_same(pending['swap_before'], known) for known in
                                   (pending['before'], pending['destination_before'], pending.get('pristine')) if known)):
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
            if package.source is None or not any(p and any(i.managed for i in p.installs) for p in (package.codex, package.claude)):
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
                source = Path(self.staging.enter_context(
                    staging_directory(prefix=package.name + '-', dir=scratch))) / 'tree'
                copy_tree(release.root, source)
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
        result = Prepared(tokens, built, claude)
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

    def held_replacements(self, observations):
        """The Claude plugin caches the pull would replace in place while running Claude processes hold them, as
        (identity, cache, PIDs). The pull leaves each one as it is until those processes exit. A cache is replaced in
        place when its version stays and what its bytes follow changes, or when a pending replacement is unfinished.
        A pending replacement of only its skills' Markdown text is left out, because the pull swaps it at once; an
        unprepared one may be such a change too, which the pull finds out only once it has built the target. A
        pending replacement whose old tree is moved aside is left out too, because no session runs from that tree."""
        held = []
        for artifact in self.artifacts(observations):
            if not artifact.claude_cache or not artifact.destination.is_dir():
                continue
            receipt = self._receipt(artifact) or {}
            pending, accepted = receipt.get('pending'), receipt.get('accepted')
            if pending:
                current, target = self._read(artifact, artifact.destination), pending['target']['image']
                replacing = (Path(pending['target']['path']) == artifact.destination and not _same(current, target)
                             and not _skill_text_only(current, target) and not Path(pending['displaced']).exists())
            elif accepted and Path(accepted['path']) == artifact.destination:
                notes = self.setup.packages[artifact.entry.name].tool_notes == ToolNotes.APPLY
                replacing = not _same(_cache_inputs(accepted.get('provenance'), notes),
                                      _cache_inputs(artifact.provenance, notes))
            else:
                continue
            if replacing and (holders := _claude_runtime(artifact.destination / '.in_use')):
                held.append((artifact.identity, artifact.destination, holders))
        return held

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
        # Claude Code writes an orphan marker into every folder under its plugin cache that is not an installed version.
        allowed = {held.name, '.orphaned_at'} if artifact.claude_cache else {held.name}

        def kept():
            names = {entry.name for entry in work.iterdir()}
            if names - allowed:
                return 'it holds more than the tree'
            if '.orphaned_at' in names:
                _claude_runtime(work / '.orphaned_at')
            if not held.exists():
                return None
            if not (isinstance(expected, dict) and expected.get('kind') == 'tree'
                    and isinstance(expected.get('entries'), dict)):
                return 'its record holds no image of the tree'
            # A tree moved aside from a Claude cache can hold its sessions' markers. A stage never held any.
            actual = self._read(artifact, held) if held.name == 'displaced' else image(held)
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
            if artifact.claude_cache:
                # Sessions that held the old tree read the new one from now on.
                _carry_leases(displaced, Path(record['path']))
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
        self._destination_admissible(artifact, at_target, verified, target, pristine)
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
            if not (_same(read(prepared.before_path), prepared.before)
                    and _same(read(artifact.destination), prepared.destination_before)):
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
                                       'target': record, 'pristine': prepared.pristine,
                                       'staged': str(work / 'staged'), 'displaced': str(work / 'displaced'),
                                       'swap_before': prepared.destination_before}
        try:
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
        except (ConcurrentEdit, OSError) as error:
            self._try_put_back(artifact, pending)
            if error is self._refused_move and self._pending_unchanged(artifact, pending, prepared.target):
                raise HeldMove(destination, error) from error
            raise
        if not _same(self._read(artifact, destination), prepared.target):
            raise ConcurrentEdit(f'Effective output failed verification: {artifact.identity}')
        self._accept(artifact, record, accepted, pending)

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

        unchanged()
        if destination.exists() or _linked(destination):
            if not (artifact.claude_cache and _only_leases(destination)):
                raise ConcurrentEdit('its place is taken')
            _fold_leases(destination, displaced)
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
            if not _same(found, prepared.target):
                if not (staged.name == 'staged' and _own_work(destination, staged)
                        and _unfinished_stage(found, prepared.target, staged, prepared.source)):
                    raise ConcurrentEdit(f'Prepared stage was edited: {staged}')
                shutil.rmtree(staged)
        if not staged.exists():
            # On Windows shutil.copy2 copies through CopyFile2, which gives a file its full length before its bytes,
            # so a copy killed inside a file leaves zeros where the stage rule needs a short file.
            copy_tree(prepared.source, staged, shutil.copy)
        if not _same(image(staged), prepared.target):
            raise ConcurrentEdit(f'Prepared stage failed verification: {staged}')
        # Running sessions hold a cache only against a change beyond its skills' Markdown text.
        text = artifact.claude_cache and _skill_text_only(actual, prepared.target)

        def stage_intact():
            if not _same(image(staged), prepared.target):
                raise ConcurrentEdit(f'Prepared stage was edited: {staged}')

        def unchanged():
            if not _same(self._read(artifact, destination, replacing=not text), actual):
                raise ConcurrentEdit(f'Tree changed before replacement: {destination}')

        def movable():
            unchanged()
            stage_intact()

        def ready():
            if destination.exists():
                raise ConcurrentEdit(f'Tree changed during replacement: {destination}')
            stage_intact()

        if artifact.claude_cache and actual == _ABSENT and displaced.exists() and _only_leases(destination):
            _fold_leases(destination, displaced)
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
