"""Compare the description with this device and say what differs, what sync fixes, and what needs a person."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, fields, is_dataclass, replace
import enum
import json
from pathlib import Path
from typing import Mapping

from description import Setup, SkillRoot, Visibility
from device import Device
from host_state import (CATALOGS, Approved, CheckoutMarketplace, EngineMarketplace, EntryName, Fact, FactKey, FactSet, FactValue,
                        FileValue, GitMarketplace, Host, Note, NoteKind, Observation, OverlayBlocked, OverlayPending, OverlaySpec,
                        Pinned, Platform, PluginValue, Released, Request, Surface, TreeValue, Unknown, Unpinned, WorkingCopy)

class Verdict(enum.StrEnum):
    DRIFT = 'drift'                  # sync fixes it
    ATTENTION = 'attention'          # a person must act; sync cannot or must not


class Kind(enum.StrEnum):
    MISSING = 'missing'
    EXTRA = 'extra'
    CHANGED = 'changed'
    TREE = 'tree'
    OVERLAY = 'overlay'
    OVERLAY_CONFLICT = 'overlay-conflict'   # the overlay cannot be replayed on the installed bytes
    APPROVAL = 'approval'
    LOCAL_EDIT = 'local-edit'
    STALE_LOCK = 'stale-lock'
    HOST_ABSENT = 'host-absent'
    INCOMPLETE = 'incomplete'        # a source could not be read, so the fact is unknown, not absent

    @property
    def verdict(self) -> Verdict:
        return _VERDICTS[self]


_VERDICTS = {
    Kind.MISSING: Verdict.DRIFT, Kind.EXTRA: Verdict.DRIFT, Kind.CHANGED: Verdict.DRIFT,
    Kind.TREE: Verdict.DRIFT, Kind.OVERLAY: Verdict.DRIFT,
    Kind.APPROVAL: Verdict.ATTENTION, Kind.LOCAL_EDIT: Verdict.ATTENTION, Kind.OVERLAY_CONFLICT: Verdict.ATTENTION,
    Kind.STALE_LOCK: Verdict.ATTENTION, Kind.HOST_ABSENT: Verdict.ATTENTION, Kind.INCOMPLETE: Verdict.ATTENTION,
}


@dataclass(frozen=True)
class EntryRef:
    name: EntryName
    visibility: Visibility


@dataclass(frozen=True)
class Difference:
    kind: Kind
    key: FactKey | None              # None for STALE_LOCK, HOST_ABSENT, and a source no compared fact came from
    expected: FactValue | None
    observed: FactValue | None
    entry: EntryRef | None
    follows: FactKey | None = None   # render folds it under the difference it follows
    host: Host | None = None         # the host a difference without a key is about
    detail: str = ''                 # the source an INCOMPLETE fact needed, and why it was not read
    cause: Unknown | None = None


@dataclass(frozen=True)
class Diff:
    platform: Platform
    roots: Mapping[Visibility, Path | None]
    hosts: Mapping[Host, str]        # host -> CLI version
    entries: Mapping[Visibility, int]
    compared: Mapping[Host, int]
    trees: int                       # TREE facts whose bytes matched the lock
    differences: tuple[Difference, ...]    # sorted by entry, then key
    notes: tuple[Note, ...]

    def drift(self) -> tuple[Difference, ...]:
        """Drift only, sorted by Surface, then entry: the order step 3 applies them in."""
        return tuple(sorted((d for d in self.differences if d.kind.verdict == Verdict.DRIFT),
                            key=lambda d: (d.key.surface if d.key else 10, d.entry.name if d.entry else '', d.key or d.detail)))

    @property
    def exit_code(self) -> int:
        """0 in step, 1 drift only, 2 anything needs a person, 3 a source could not be read."""
        kinds = {difference.kind for difference in self.differences}
        if Kind.INCOMPLETE in kinds:
            return 3
        if any(kind.verdict == Verdict.ATTENTION for kind in kinds):
            return 2
        return 1 if kinds else 0


def _classify(key: FactKey, expected: FactValue | None, observed: FactValue | None,
              observation: Observation) -> Kind | NoteKind | None:
    """The whole policy: what a pair of values means, by surface and by which side is present."""
    surface = key.surface
    if isinstance(observed, WorkingCopy):
        return NoteKind.DEVICE_LOCAL
    if expected is not None and observed is not None:
        if surface == Surface.PLUGIN:
            if expected.enabled != observed.enabled:
                return Kind.CHANGED
            if isinstance(expected.pin, Unpinned) or expected.pin == observed.pin:
                return None
            return NoteKind.DORMANT if not expected.enabled else Kind.CHANGED
        if expected == observed:
            return None
        if surface == Surface.TREE:
            return Kind.LOCAL_EDIT if key in observation.edits else Kind.TREE
        if isinstance(observed, OverlayBlocked):
            return Kind.OVERLAY_CONFLICT
        return {Surface.OVERLAY: Kind.OVERLAY, Surface.HOOK: Kind.APPROVAL}.get(surface, Kind.CHANGED)
    if expected is not None:
        if surface == Surface.PLUGIN and not expected.enabled:
            return NoteKind.DORMANT
        marketplace = key.subject.partition('@')[2]
        if surface == Surface.PLUGIN and marketplace in CATALOGS[key.host] and key.subject not in observation.offered:
            return NoteKind.UNAVAILABLE
        return Kind.APPROVAL if surface == Surface.HOOK else Kind.MISSING
    if surface == Surface.PLUGIN and not observed.enabled:
        return NoteKind.RESIDUE
    return Kind.EXTRA


def _visibility(setup: Setup, name: str) -> Visibility:
    entry = setup.packages.get(name) or setup.loose.get(name) or setup.repos.get(name)
    return entry.visibility if entry else Visibility.PRIVATE


def _targets(setup: Setup, host: Host) -> bool:
    roots = {Host.CODEX: {SkillRoot.CODEX, SkillRoot.AGENTS}, Host.CLAUDE: {SkillRoot.CLAUDE}}[host]
    return (any(package.placement(host) for package in setup.packages.values())
            or any(roots & skill.place.keys() for skill in setup.loose.values())
            or bool(setup.host_skills.get(host)) or setup.rules is not None)


def diff(setup: Setup, device: Device, *, observations: Mapping[Host, Observation] | None = None) -> Diff:
    """The one comparison. Read-only on every host."""
    stale = setup.stale()
    projected = setup.for_platform(device.platform)
    differences: list[Difference] = []
    notes: list[Note] = []
    for name in sorted(stale):
        differences.append(Difference(Kind.STALE_LOCK, None, None, None, EntryRef(name, _visibility(setup, name))))
    for host in device.absent:
        if _targets(setup, host):
            differences.append(Difference(Kind.HOST_ABSENT, None, None, None, None, host=host))
    versions, compared, trees = {}, {}, 0
    for state in device.hosts:
        expected, covered = _unlock(state.project(projected), stale)
        observation = observations[state.host] if observations is not None else state.observe(Request.of(expected))
        versions[state.host] = observation.version
        notes.extend(observation.notes)
        keys = sorted((expected.keys() | observation.facts.keys()) - covered)
        compared[state.host] = len(keys)
        outcomes: dict[FactKey, Kind | NoteKind | None] = {}
        unknown: dict[FactKey, Unknown] = {}
        for key in keys:
            want, have = expected.get(key), observation.facts.get(key)
            cause = next((u for u in observation.unknown if any(f and u.covers(f) for f in (want, have))), None)
            if cause is not None:
                outcomes[key], unknown[key] = Kind.INCOMPLETE, cause
                continue
            outcomes[key] = _classify(key, want and want.value, have and have.value, observation)
            if key.surface == Surface.TREE and outcomes[key] is None:
                trees += 1
        for cause in observation.unknown:
            if cause not in unknown.values():
                differences.append(Difference(Kind.INCOMPLETE, None, None, None, None, host=state.host,
                                              detail=f'{cause.source}: {cause.reason}', cause=cause))
        for key in keys:
            outcome = outcomes[key]
            if outcome is None:
                continue
            want, have = expected.get(key), observation.facts.get(key)
            fact = want or have
            follows = fact.follows
            if follows is not None and outcomes.get(follows) in (NoteKind.UNAVAILABLE, NoteKind.RESIDUE, NoteKind.DORMANT):
                continue
            if isinstance(outcome, NoteKind):
                notes.append(Note(outcome, state.host, key.subject, _note_detail(outcome, want, have)))
                continue
            name = fact.entry or (expected[follows].entry if follows in expected else None)
            entry = EntryRef(name, _visibility(setup, name)) if name else None
            folded = follows if outcomes.get(follows) in (Kind.MISSING, Kind.EXTRA, Kind.INCOMPLETE) else None
            cause = unknown.get(key)
            differences.append(Difference(outcome, key, want.value if want else None, have.value if have else None,
                                          entry, folded, detail=f'{cause.source}: {cause.reason}' if cause else '', cause=cause))
    differences.sort(key=lambda d: (d.entry.name if d.entry else '~', d.key is not None, d.key or d.host))
    entries = Counter(entry.visibility for entry in (*setup.packages.values(), *setup.repos.values(), *setup.loose.values()))
    return Diff(device.platform, {Visibility.PUBLIC: setup.roots.public, Visibility.PRIVATE: setup.roots.private},
                versions, {v: entries.get(v, 0) for v in Visibility}, compared, trees, tuple(differences), tuple(notes))


_LOCKED = frozenset({Surface.TREE, Surface.AGENT, Surface.HOOK, Surface.LOOSE_SKILL})


def _unlock(expected: FactSet, stale: frozenset[EntryName]) -> tuple[dict[FactKey, Fact], set[FactKey]]:
    kept, covered = {}, set()
    for key, fact in expected.items():
        if fact.entry not in stale:
            kept[key] = fact
        elif key.surface in _LOCKED:
            covered.add(key)
        elif key.surface == Surface.PLUGIN:
            kept[key] = replace(fact, value=PluginValue(fact.value.enabled, Unpinned()))
        else:
            kept[key] = fact
    return kept, covered


def _note_detail(kind: NoteKind, expected, observed) -> str:
    if kind == NoteKind.DORMANT:
        if observed is None:
            return 'disabled in the description and not installed on this device'
        return f'disabled at {_spell(observed.value.pin)}; the description pins {_spell(expected.value.pin)}'
    if kind == NoteKind.UNAVAILABLE:
        return 'its catalog is not offered on this device'
    if kind == NoteKind.DEVICE_LOCAL:
        copy = observed.value
        return f'uncommitted work in {copy.url} at {copy.within}; sync leaves it alone'
    return 'installed and disabled, and the description does not name it'


def _spell(value) -> str:
    match value:
        case None:
            return 'absent'
        case PluginValue(enabled=enabled, pin=pin):
            return ('enabled' if enabled else 'disabled') + ('' if isinstance(pin, Unpinned) else f' {_spell(pin)}')
        case Pinned(commit=commit):
            return commit[:7]
        case Released(version=version):
            return version
        case Unpinned():
            return 'unpinned'
        case GitMarketplace(url=url, commit=commit):
            return url + (f' at {commit[:7]}' if commit else '')
        case CheckoutMarketplace(url=url, commit=commit):
            return f'checkout of {url} at {commit[:7]}'
        case EngineMarketplace():
            return 'engine output'
        case TreeValue(digest=digest) | FileValue(digest=digest):
            return digest[:19]
        case OverlaySpec():
            return 'applied'
        case OverlayPending(files=files):
            return f'{files} files pending'
        case OverlayBlocked(problem=problem):
            return problem
        case Approved(hooks=hooks):
            return f'{hooks} hooks approved'
        case enum.Enum():
            return value.value
    return str(value)


def _home(path: Path | None) -> str:
    if path is None:
        return 'none'
    home = str(Path.home())
    return '~' + str(path)[len(home):] if str(path).startswith(home) else str(path)


def render(diff: Diff, *, full: bool = False, notes: bool = False) -> str:
    """Grouped by entry, private entries tagged. A difference that follows another (a skill of a missing
    plugin) folds into a count under it unless `full`."""
    lines = [f'description  public  {_home(diff.roots[Visibility.PUBLIC]):<40} {diff.entries[Visibility.PUBLIC]} entries',
             f'             private {_home(diff.roots[Visibility.PRIVATE]):<40} {diff.entries[Visibility.PRIVATE]} entries',
             f'device       {diff.platform.value} · ' + ' · '.join(diff.hosts.values()),
             'compared     ' + ' · '.join(f'{host.value} {count} facts' for host, count in diff.compared.items())
             + f' · {diff.trees} trees verified', '']
    if not diff.differences:
        lines.append('In step. No differences.')
    folded = Counter(d.follows for d in diff.differences if d.follows and not full and d.kind.verdict != Verdict.ATTENTION)
    for verdict, title in ((Verdict.DRIFT, 'drift (sync fixes these)'), (Verdict.ATTENTION, 'attention')):
        chosen = [d for d in diff.differences if d.kind.verdict == verdict
                  and (full or not d.follows or verdict == Verdict.ATTENTION)]
        if not chosen:
            continue
        lines.append(title)
        for difference in chosen:
            entry = difference.entry
            label = (entry.name + (' (private)' if entry.visibility == Visibility.PRIVATE else '')) if entry else '-'
            if difference.key is None:
                match difference.kind:
                    case Kind.HOST_ABSENT:
                        what = f'{difference.host.value}  host is not installed here'
                    case Kind.STALE_LOCK:
                        what = 'the entry changed after its lock was written'
                    case _:
                        what = f'{difference.host.value if difference.host else ""}  {difference.detail}'
                lines.append(f'  {label:<30} {difference.kind.value:<11} {what}')
                continue
            key = difference.key
            change = (difference.detail if difference.kind == Kind.INCOMPLETE
                      else f'want {_spell(difference.expected)} · have {_spell(difference.observed)}')
            lines.append(f'  {label:<30} {key.host.value:<6} {key.surface.name.lower():<11} {key.subject}  '
                         f'{difference.kind.value}  {change}')
            if folded.get(key):
                lines.append(f'  {"":<30} + {folded[key]} facts follow it')
    lines += ['', 'notes (not differences; --notes prints each in full)' if not notes else 'notes']
    for note in sorted(diff.notes, key=lambda n: (list(NoteKind).index(n.kind), n.host)):
        text = f'{note.subject}: {note.detail}'
        if not notes and len(text) > 100:
            text = text[:99] + '…'
        lines.append(f'  {note.kind.value:<13} {note.host.value:<7} {text}')
    if not diff.notes:
        lines.append('  none')
    return '\n'.join(lines) + '\n'


def _plain(value):
    if isinstance(value, enum.Enum):
        return value.value
    if is_dataclass(value):
        return {'type': type(value).__name__, **{f.name: _plain(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def to_json(diff: Diff) -> str:
    """Stable machine form for the maintainer automation, values in their TOML spelling."""
    return json.dumps({
        'platform': diff.platform.value, 'exit': diff.exit_code,
        'hosts': {host.value: version for host, version in diff.hosts.items()},
        'compared': {host.value: count for host, count in diff.compared.items()}, 'trees': diff.trees,
        'differences': [{
            'verdict': d.kind.verdict.value, 'kind': d.kind.value,
            'host': (d.key.host if d.key else d.host).value if (d.key or d.host) else None,
            'surface': d.key.surface.name.lower() if d.key else None, 'subject': d.key.subject if d.key else None,
            'entry': d.entry.name if d.entry else None, 'visibility': d.entry.visibility.value if d.entry else None,
            'expected': _plain(d.expected), 'observed': _plain(d.observed),
            'follows': d.follows.subject if d.follows else None, 'detail': d.detail or None,
        } for d in diff.differences],
        'notes': [{'kind': n.kind.value, 'host': n.host.value, 'subject': n.subject, 'detail': n.detail} for n in diff.notes],
    }, indent=1, ensure_ascii=False)
