"""Read-only access to one host on this device."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Callable, Mapping

from invocation_overrides import Overlay, plan
import portable
from host_state import (Commit, Detail, DeviceRoots, Fact, FactKey, FactValue, FileValue, FolderDetail, Host, Note, NoteKind,
                        Observation, OverlayBlocked, OverlayPending, OverlaySpec, Surface, TreeValue, Unknown, WorkingCopy,
                        clone_url, parse_commit)


def run(command: list[str], **options) -> str:
    options['env'] = portable.native_cli_environment(options.get('env'))
    return subprocess.run(command, capture_output=True, text=True, encoding='utf-8', check=True, timeout=120, **options).stdout


class Unread(Exception):
    """A source this observation needed and could not read; its facts become unknown, never absent."""


def checkout(path: Path) -> tuple[str, Commit, bool] | None:
    """(origin, HEAD, clean) of the git checkout whose top is path, read without touching its index; None when
    path is not the top of a checkout with an origin. Raises Unread when git cannot read one that is."""
    if not (Path(path) / '.git').exists():
        return None
    git = [*portable.GIT, '--no-optional-locks', '-C', str(path)]
    try:
        origin = run([*git, 'config', '--default', '', '--get', 'remote.origin.url']).strip()
        if not origin:
            return None
        head = parse_commit(run([*git, 'rev-parse', 'HEAD']).strip())
        clean = not run([*git, 'status', '--porcelain']).strip()
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise Unread(f'git cannot read the checkout: {error}') from error
    return clone_url(origin), head, clean


class Observer:
    def __init__(self, host: Host, roots: DeviceRoots) -> None:
        self.host, self.roots = host, roots
        self.facts: dict[FactKey, Fact] = {}
        self.notes: list[Note] = []
        self.unknown: list[Unknown] = []
        self.edits: set[FactKey] = set()
        self.details: dict[FactKey, Detail] = {}

    def key(self, surface: Surface, subject: str) -> FactKey:
        return FactKey(self.host, surface, subject)

    def add(self, surface: Surface, subject: str, value: FactValue, follows: FactKey | None = None,
            detail: Detail | None = None) -> FactKey:
        key = self.key(surface, subject)
        self.facts[key] = Fact(key, value, None, follows)
        if detail is not None:
            self.details[key] = detail
        return key

    def note(self, kind: NoteKind, subject: str, detail: str) -> None:
        self.notes.append(Note(kind, self.host, subject, detail))

    def unread(self, source: str | Path, reason: object, surfaces: set[Surface], subjects: set[str] | None = None,
               *, missing_tree: FactKey | None = None) -> None:
        """Record a source that could not be read and the facts it would have produced, all of them or only
        those about the given subjects."""
        self.unknown.append(Unknown(self.roots.tokens(source), str(reason), frozenset(surfaces),
                                    None if subjects is None else frozenset(subjects), missing_tree))

    def finish(self, version: str, offered: set[str]) -> Observation:
        return Observation(self.host, version, self.facts, frozenset(offered), frozenset(self.edits),
                           tuple(self.notes), tuple(self.unknown), self.details)


class Receipt:
    """The overlay's record of the bytes it replaced, read without taking its lock or writing."""

    def __init__(self, roots: DeviceRoots) -> None:
        path = roots.codex_home / 'skill-updates/invocation-overrides/state.json'
        self.path = path
        self.present = path.is_file()
        self.files: Mapping[str, Mapping] = json.loads(portable.read_utf8(path))['files'] if self.present else {}

    def pristine(self, root: Path) -> tuple[dict[Path, bytes | None], list[Path]]:
        """The swap that reads an overlaid tree as its release, and the files no record explains. Bytes are
        compared as bytes, and a file the overlay wrote that is now gone is an edit."""
        swap, edited = {}, []
        for filename, record in self.files.items():
            path = Path(filename)
            if not path.is_relative_to(root):
                continue
            current = path.read_bytes() if path.is_file() else None
            base = _encoded(record['base'])
            if current == base:
                continue
            written = [record['effective'], *([record['previous']] if 'previous' in record else [])]
            if current in [_encoded(text) for text in written]:
                swap[path] = base
            else:
                edited.append(path)
        return swap, edited


def _encoded(text: str | None) -> bytes | None:
    return None if text is None else text.encode('utf-8')


def overlay_state(roots: DeviceRoots, host: Host, identity: str, root: Path,
                  spec: OverlaySpec) -> OverlaySpec | OverlayPending | OverlayBlocked:
    """The spec itself when the overlay tool, run in check mode with a registry derived from the spec for
    this one install, would change nothing; else the files it would change, or why it cannot run."""
    label, name = host.value.capitalize(), identity.split('@')[0]
    registry = {'targets': {label: {}}, 'explicitTargets': {label: {identity: list(spec.explicit)}}}
    adaptations: dict = {'plugins': {}}
    if spec.adaptation is not None:
        clone = next((path for path in sorted((roots.engine / 'harness-adaptations').glob('*'))
                      if (found := checkout(path)) and found[0] == clone_url(spec.adaptation.repository)), None)
        if clone is None:
            raise Unread(f'no clone of {spec.adaptation.repository} under {{engine}}/harness-adaptations')
        adaptations['plugins'][name] = {'repository': str(clone), 'baseRef': spec.adaptation.base,
                                        'adaptedRef': spec.adaptation.adapted, 'hosts': [label], 'identities': [identity]}
    if spec.rules:
        adaptations['toolNotes'] = {'plugins': [name], 'rules': [{'match': r.match, 'note': r.note} for r in spec.rules]}
    overlay = Overlay(roots.codex_home / 'skill-updates/invocation-overrides')
    try:
        writes, _ = plan(registry, [(label, identity, root)], overlay, adaptations)
        pending = overlay.apply(writes, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise Unread(f'overlay check failed: {error}') from error
    except ValueError as error:
        return OverlayBlocked(str(error))
    return OverlayPending(pending) if pending else spec


def observe_loose(observer: Observer, folder: Path, keep: Callable[[Path], bool]) -> None:
    """Every loose skill folder under one skills root, by content digest; links are followed. A link into a
    checkout is described by that checkout when its folder is committed there, and is otherwise the user's
    working copy."""
    try:
        children = sorted(folder.iterdir()) if folder.is_dir() else []
    except OSError as error:
        observer.unread(folder, error, {Surface.LOOSE_SKILL})
        return
    for child in children:
        if not (child.is_dir() and keep(child)):
            continue
        subject = f'{observer.roots.tokens(folder)}/{child.name}'
        try:
            linked = _linked_checkout(child)
            if linked and not linked[3]:
                observer.add(Surface.LOOSE_SKILL, subject, WorkingCopy(linked[0], linked[2]), detail=FolderDetail(child))
                continue
            detail = FolderDetail(child, *linked[:3]) if linked else FolderDetail(child)
            observer.add(Surface.LOOSE_SKILL, subject, FileValue(observer.roots.tree_digest(child)), detail=detail)
        except (OSError, Unread) as error:
            observer.unread(child, error, {Surface.LOOSE_SKILL}, {subject})


def _linked_checkout(folder: Path) -> tuple[str, Commit, str, bool] | None:
    if not folder.is_symlink():
        return None
    target = folder.resolve()
    top = next((parent for parent in (target, *target.parents) if (parent / '.git').exists()), None)
    found = checkout(top) if top else None
    if found is None:
        return None
    url, head, _ = found
    within = portable.posix(target.relative_to(top))
    git = [*portable.GIT, '--no-optional-locks', '-C', str(top)]
    try:
        tracked = bool(run([*git, 'ls-tree', '-r', '--name-only', 'HEAD', '--', within]).strip())
        changed = bool(run([*git, 'status', '--porcelain', '--untracked-files=all', '--', within]).strip())
    except (OSError, subprocess.SubprocessError) as error:
        raise Unread(f'git cannot read the checkout: {error}') from error
    return url, head, within, tracked and not changed


def observe_trees(observer: Observer, trees: Mapping[str, Path], overlays: Mapping[str, tuple[Path, OverlaySpec]]) -> None:
    roots = observer.roots
    for plugin, (root, spec) in overlays.items():
        try:
            observer.add(Surface.OVERLAY, plugin, overlay_state(roots, observer.host, plugin, root, spec),
                         observer.key(Surface.PLUGIN, plugin))
        except Unread as error:
            observer.unread(root, error, {Surface.OVERLAY}, {plugin})
    if not trees:
        return
    try:
        receipt = Receipt(roots)
    except (OSError, ValueError, KeyError) as error:
        observer.unread(roots.codex_home / 'skill-updates/invocation-overrides/state.json', error, {Surface.TREE})
        return
    for plugin, root in trees.items():
        try:
            swap, edited = receipt.pristine(root)
            digest = roots.tree_digest(root, swap=swap)
        except OSError as error:
            observer.unread(root, error, {Surface.TREE}, {plugin})
            continue
        key = observer.add(Surface.TREE, plugin, TreeValue(digest), observer.key(Surface.PLUGIN, plugin))
        if edited or not receipt.present:
            observer.edits.add(key)
