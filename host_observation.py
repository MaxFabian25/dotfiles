"""Read-only access to one host on this device."""
from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Callable, Mapping

import portable
from host_state import (Commit, Detail, DeviceRoots, Fact, FactKey, FactValue, FileValue, FolderDetail, Host, Note, NoteKind,
                        Observation, Surface, TreeValue, Unknown, WorkingCopy, clone_url, parse_commit)


COMMAND_SECONDS = 120


def run(command: list[str], seconds: float | None = None, **options) -> str:
    options['env'] = portable.native_cli_environment(options.get('env'))
    return portable.checked_output(command, text=True, encoding='utf-8', timeout=seconds or COMMAND_SECONDS, **options)


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
        return Observation(self.host, version, self.facts, frozenset(offered), tuple(self.notes),
                           tuple(self.unknown), self.details)


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


def observe_trees(observer: Observer, trees: Mapping[str, Path]) -> None:
    for plugin, root in trees.items():
        try:
            digest = observer.roots.tree_digest(root)
        except OSError as error:
            observer.unread(root, error, {Surface.TREE}, {plugin})
            continue
        observer.add(Surface.TREE, plugin, TreeValue(digest), observer.key(Surface.PLUGIN, plugin))
