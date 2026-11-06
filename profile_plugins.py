"""Keep the plugins of the Orca account homes on this device on the copies ~/.codex accepted.

Each Orca account home is a separate Codex home with its own plugin cache. Codex refreshes a plugin there from
the catalog, which lacks the install overlay and harness adaptation the accepted ~/.codex copy carries. Sync
replaces that native copy, an older version, or its own earlier copy with the accepted ~/.codex copy, for plugins
the home already has installed and enabled. It installs nothing new, enables nothing, approves no hook, and leaves
settings and credentials alone.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import tomllib
import uuid

from accepted_installations import copy_tree, image
from codex_host import _hook_keys
from description import EngineInstall, Hooks, MarketplaceInstall, RepoSource, plugin_id
from drift import Difference, EntryRef, Kind
from host_observation import Unread
from host_state import Host, Note, NoteKind
from portable import replace
from sync_files import ConcurrentEdit, contents
from sync_sources import BuildError, child

_ABSENT = {'kind': 'absent'}


def profiles(home):
    parents = (home / '.local/share/orca/codex-accounts', home / 'AppData/Roaming/orca/codex-accounts',
               home / 'Library/Application Support/Orca/codex-accounts',
               home / 'Library/Application Support/orca/codex-accounts')
    return tuple(sorted({path for parent in parents for path in parent.glob('*/home') if path.is_dir()}))


@dataclass(frozen=True)
class Mirror:
    profile: Path
    subject: str                     # the plugin id, e.g. pstack@personal
    canonical: Path                  # the accepted copy in ~/.codex
    target: Path                     # the same version in the account home
    before: dict                     # its image when planned
    superseded: tuple[tuple[Path, dict], ...]   # the home's other versions of the plugin, which Codex no longer loads
    config: bytes


def _receipt_path(roots, profile, subject):
    name = hashlib.sha256(f'{profile}\n{subject}'.encode()).hexdigest() + '.json'
    return roots.codex_home / 'dotfiles/account-homes' / name


def _copied(roots, profile, subject):
    """The image sync last copied into this home for this plugin, if any."""
    data = contents(_receipt_path(roots, profile, subject))
    if data is None:
        return None
    record = json.loads(data)
    if record.get('schema') != 1 or record.get('profile') != str(profile) or record.get('subject') != subject:
        raise ValueError(f'Unsupported account home receipt for {subject} in {profile}')
    return record


def _plugins(setup):
    for package in setup.packages.values():
        tree = setup.lock[package.name].trees.get(Host.CODEX) if package.codex and package.name in setup.lock else None
        if tree is None:
            continue
        for install in package.codex.installs:
            if isinstance(install, (EngineInstall, MarketplaceInstall)) and install.enabled:
                subject = plugin_id(package.name, install, Host.CODEX)
                name, marketplace = subject.split('@', 1)
                yield package, install, subject, marketplace, f'plugins/cache/{marketplace}/{name}/{tree.version}'


def _native(setup, roots, package, install, name):
    """The tree Codex itself installs this plugin from, when this device has it."""
    if isinstance(install, EngineInstall):
        data = contents(roots.agents_home / 'plugins/marketplace.json')
        listed = next((p for p in json.loads(data)['plugins'] if p['name'] == name), None) if data else None
        source = listed['source'] if listed else {}
        return child(roots.home, source['path'].removeprefix('./')) if source.get('source') == 'local' else None
    if isinstance(package.source, RepoSource):
        commit = setup.repos[package.source.repo].commit
        for engine in (roots.engine, roots.installed_engine()):
            if engine is not None and (engine / 'native' / package.name / commit).is_dir():
                return engine / 'native' / package.name / commit
    return None


def _known_copy(actual, references):
    """Whether each entry of a copy is that entry of a reference: the accepted copy, the native release, or the copy
    sync made. A native copy of the release, one with some of the install overlay, and sync's own copy all pass."""
    entries = actual.get('entries', {})
    return actual['kind'] == 'tree' and all(any(reference.get('entries', {}).get(path) == value
                                                for reference in references) for path, value in entries.items())


def inspect(setup, roots, accepted, approved=None):
    """The mirrors this device's account homes need, what blocks them, and notes on plugins left alone.

    accepted(identity) returns the receipt's accepted record of a ~/.codex install, or None while it is pending.
    approved(home) returns the plugin hook keys Codex reports trusted in that home; None skips the hook check.
    """
    mirrors, differences, notes = [], [], []
    canonical_config = contents(roots.codex_home / 'config.toml')
    markets = tomllib.loads(canonical_config.decode('utf-8')).get('marketplaces', {}) if canonical_config else {}
    for profile in profiles(roots.home):
        config = contents(profile / 'config.toml')
        if config is None:
            continue
        settings = tomllib.loads(config.decode('utf-8'))
        trusted = None
        for package, install, subject, marketplace, relative in _plugins(setup):
            if settings.get('plugins', {}).get(subject, {}).get('enabled') is not True:
                continue
            entry = EntryRef(package.name, package.visibility)
            canonical = roots.codex_home / relative
            try:
                shared = (profile / relative).resolve() == canonical.resolve()
                target = profile / relative if shared else child(profile, relative)
                if not shared:
                    if not target.parent.is_dir():
                        continue
                    if settings.get('marketplaces', {}).get(marketplace) != markets.get(marketplace):
                        notes.append(Note(NoteKind.DEVICE_LOCAL, Host.CODEX, subject,
                                          f'{profile} configures the {marketplace} marketplace differently from '
                                          '~/.codex; its copy is left to Codex'))
                        continue
                    record = accepted(f'plugin:{Host.CODEX}:{subject}')
                    accepted_image = record['image'] if record and Path(record['path']) == canonical else None
                    actual = image(target)
                    if actual != accepted_image:
                        copied = _copied(roots, profile, subject)
                        native = _native(setup, roots, package, install, subject.split('@', 1)[0])
                        references = [accepted_image] if accepted_image else []
                        references += [image(native)] if native is not None else []
                        references += [copied['image']] if copied and copied['path'] == str(target) else []
                        if actual != _ABSENT and not _known_copy(actual, references):
                            raise ConcurrentEdit(f'{target} has files that are in neither the accepted ~/.codex copy, '
                                                 'the release Codex installs, nor the copy sync made; preserved for review')
                        others = tuple(path for path in sorted(target.parent.iterdir())
                                       if path != target and not path.name.startswith('.'))
                        if actual == _ABSENT and not others:
                            continue
                        mirrors.append(Mirror(profile, subject, canonical, target, actual,
                                              tuple((path, image(path)) for path in others), config))
                        differences.append(Difference(Kind.TREE, None, None, None, entry, host=Host.CODEX,
                                                      detail=f'{profile}: {subject} {target.name} from ~/.codex'))
                        continue
                if approved is not None and package.codex.hooks == Hooks.TRUSTED:
                    defined = _hook_keys(subject, target)
                    if defined:
                        trusted = approved(profile) if trusted is None else trusted
                        if missing := defined - trusted:
                            differences.append(Difference(Kind.APPROVAL, None, None, None, entry, host=Host.CODEX,
                                                          detail=f'{profile}: approve {len(missing)} {subject} '
                                                                 'hooks in this Orca home'))
            except Unread as error:
                differences.append(Difference(Kind.INCOMPLETE, None, None, None, entry, host=Host.CODEX,
                                              detail=f'{profile}: {error}'))
            except (OSError, ValueError, KeyError, BuildError, ConcurrentEdit) as error:
                differences.append(Difference(Kind.LOCAL_EDIT, None, None, None, entry, host=Host.CODEX,
                                              detail=f'{profile}: {error}'))
    return tuple(mirrors), tuple(differences), tuple(notes)


def preflight(mirrors):
    for mirror in mirrors:
        if contents(mirror.profile / 'config.toml') != mirror.config:
            raise ConcurrentEdit(f'Account home settings changed during sync: {mirror.profile}')
        for path, expected in ((mirror.target, mirror.before), *mirror.superseded):
            if image(path) != expected:
                raise ConcurrentEdit(f'Account home plugin changed during sync: {path}')


def apply(mirrors, roots, accepted, files):
    """Copy each accepted ~/.codex plugin into its account home. The copies it replaces move to the sync backup."""
    preflight(mirrors)
    for mirror in mirrors:
        record = accepted(f'plugin:{Host.CODEX}:{mirror.subject}')
        if record is None or Path(record['path']) != mirror.canonical or image(mirror.canonical) != record['image']:
            raise ConcurrentEdit(f'~/.codex has no verified copy of {mirror.subject} to share: {mirror.canonical}')
        work = mirror.target.parent / f'.sync-{mirror.target.name}-{uuid.uuid4().hex}'
        staged = work / 'staged'
        copy_tree(mirror.canonical, staged)
        if image(staged) != record['image']:
            raise ConcurrentEdit(f'Staged copy failed verification: {staged}')
        preflight((mirror,))
        backup = (files.backup / 'account-homes' / mirror.profile.parent.name /
                  mirror.target.parent.relative_to(mirror.profile / 'plugins/cache'))
        backup.mkdir(parents=True, exist_ok=True, mode=0o700)
        moved = []
        for path in (mirror.target, *(path for path, _ in mirror.superseded)):
            if path.exists():
                shutil.move(path, backup / path.name)
                moved.append({'from': str(path), 'to': str(backup / path.name)})
        (backup / 'moved.json').write_text(json.dumps(moved, indent=2) + '\n', encoding='utf-8')
        staged.rename(mirror.target)
        work.rmdir()
        if image(mirror.target) != record['image']:
            raise ConcurrentEdit(f'Account home copy failed verification: {mirror.target}')
        _save(roots, mirror, record['image'])
        files.changed.append(mirror.target)


def _save(roots, mirror, copied):
    path = _receipt_path(roots, mirror.profile, mirror.subject)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = {'schema': 1, 'profile': str(mirror.profile), 'subject': mirror.subject, 'path': str(mirror.target),
            'image': copied}
    with tempfile.NamedTemporaryFile(prefix='.receipt-', dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(json.dumps(data, sort_keys=True).encode() + b'\n')
    try:
        temporary.chmod(0o600)
        replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
