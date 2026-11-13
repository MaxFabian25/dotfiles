from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Mapping

import codex_host
import codex_runtime
import profile_plugins
from description import Setup
from device import Device
from drift import Diff, Difference, Kind, Verdict, diff
from host_state import FactKey, Host, Note, NoteKind, Observation, Platform, Request, Surface

if TYPE_CHECKING:
    from accepted_installations import HeldCache, HeldMove


@dataclass(frozen=True)
class Plan:
    result: Diff
    observations: Mapping[Host, Observation]
    changes: tuple[Difference, ...]
    blocked: tuple[Difference, ...]
    unmanaged: tuple[Difference, ...]
    configurations: bool = True
    expected: tuple[Difference, ...] = ()   # unmanaged, and named in this platform's device list
    profile_mirrors: tuple[profile_plugins.Mirror, ...] = ()
    held: tuple[HeldCache | HeldMove, ...] = ()

    @property
    def exit_code(self):
        if self.blocked:
            return 3 if any(d.kind == Kind.INCOMPLETE for d in self.blocked) else 2
        return 1 if self.changes else 0


def plan(setup: Setup, device: Device, *, configurations=True) -> Plan:
    from accepted_installations import AcceptedInstallations

    projected = setup.for_platform(device.platform)
    projects = {state.host: state.project(projected) for state in device.hosts}
    observations = {state.host: state.observe(Request.of(projects[state.host])) for state in device.hosts}
    result = diff(setup, device, observations=observations)
    owner = AcceptedInstallations(projected, device, configurations=configurations)
    artifacts, owned = owner.inspect(observations)
    if Host.CODEX in observations:
        owned += codex_runtime.inspect(projected.runtime, device.roots, observations[Host.CODEX].offered)
    codex = next((state for state in device.hosts if state.host == Host.CODEX), None)
    approved = (lambda home: codex_host._approved_hooks(codex.cli, device.roots.home, codex_home=home)) if codex else None
    profile_mirrors, profile_differences, profile_notes = profile_plugins.inspect(
        projected, device.roots, setup.account_homes.get(device.platform, ()), owner.accepted, approved)
    owned += profile_differences
    stale = setup.stale()
    managed = {key for facts in projects.values() for key, fact in facts.items()
               if fact.entry not in stale and key.surface in
               (Surface.TREE, Surface.AGENT, Surface.LOOSE_SKILL, Surface.RULES)}
    owned = tuple(d for d in owned if d.entry is None or d.entry.name not in stale)
    recoverable = owner.recoverable_missing_caches(artifacts, owned, observations)

    def pending_cache(cause, tree, root):
        return (cause.missing_tree == tree and cause.source == root
                and cause.surfaces == frozenset({Surface.SKILL})
                and cause.subjects == frozenset({tree.subject}))

    def deferred_skill(difference):
        key = difference.key
        if difference.kind != Kind.INCOMPLETE:
            return False
        if key is None:
            cause = difference.cause
            tree = cause.missing_tree if cause else None
            root = recoverable.get(tree)
            return root is not None and pending_cache(cause, tree, root)
        if key.surface != Surface.SKILL:
            return False
        fact = projects[key.host].get(key)
        plugin = fact.follows if fact else None
        if plugin is None or plugin.surface != Surface.PLUGIN:
            return False
        tree = FactKey(key.host, Surface.TREE, plugin.subject)
        root = recoverable.get(tree)
        if root is None:
            return False
        observed = observations[key.host].facts.get(key)
        causes = [cause for cause in observations[key.host].unknown
                  if any(item and cause.covers(item) for item in (fact, observed))]
        return bool(causes) and all(pending_cache(cause, tree, root) for cause in causes)

    retained = tuple(d for d in result.differences if d.key not in managed and not deferred_skill(d))
    unmanaged = tuple(d for d in retained + owned if d.kind == Kind.EXTRA and d.entry is None)
    expected, notes = _expected(setup.expected_unmanaged.get(device.platform, frozenset()), unmanaged,
                                device.platform, observations.keys())
    result = replace(result, differences=tuple(d for d in retained + owned if d not in expected),
                     notes=result.notes + notes + profile_notes)
    blocked = tuple(d for d in result.differences if d.kind.verdict == Verdict.ATTENTION)
    unmanaged = tuple(d for d in unmanaged if d not in expected)
    changes = tuple(d for d in result.drift() if d not in unmanaged)
    return Plan(result, observations, changes, blocked, unmanaged, configurations, expected, profile_mirrors)


def _expected(listed: frozenset[FactKey], unmanaged: tuple[Difference, ...], platform: Platform,
              hosts) -> tuple[tuple[Difference, ...], tuple[Note, ...]]:
    """The unmanaged differences the device list names, with the notes that replace them: a count per host, and
    one per listed item that is no longer an unmanaged difference, so the list cannot hide a stale entry."""
    known = tuple(d for d in unmanaged if d.key in listed or d.follows in listed)
    source = f'devices/{platform.value}.toml'
    notes = [Note(NoteKind.DEVICE_LOCAL, host, 'expected unmanaged items', f'{count} named in {source}; sync leaves them alone')
             for host, count in sorted(Counter(d.key.host for d in known).items())]
    notes += [Note(NoteKind.RESIDUE, key.host, key.subject, f'listed in {source} but no longer an unmanaged difference here')
              for key in sorted(listed - {d.key for d in known}) if key.host in hosts]
    return known, tuple(notes)


def changed_packages(current: Plan) -> frozenset[str]:
    return frozenset(d.entry.name for d in current.changes if d.entry is not None
                     and d.key is not None and d.key.surface in
                     (Surface.PLUGIN, Surface.TREE, Surface.SKILL, Surface.AGENT))


def apply(setup: Setup, device: Device, current: Plan, *, builder=None, configurations=None):
    from portable import exclusive_lock
    from sync_hosts import Apply
    from sync_sources import Builder

    if current.blocked:
        raise RuntimeError('Sync is blocked by incomplete reads, stale locks, conflicts or local edits; no host files changed')
    if not current.changes:
        return current, None
    configurations = current.configurations if configurations is None else configurations
    if configurations != current.configurations:
        raise ValueError('The apply configuration scope must match the reviewed plan')
    with exclusive_lock(device.roots.codex_home / 'dotfiles/sync.lock', record=True):
        checked = plan(setup, device, configurations=configurations)
        if checked.result.differences != current.result.differences:
            raise RuntimeError('The device changed after planning; run sync again to review its current state')
        builder = builder or Builder(setup, device.roots, device.roots.engine)
        operation = Apply(setup.for_platform(device.platform), device, current, builder)
        with operation.owner.staging:
            operation.prepare()
            operation.run()
        after = replace(plan(setup, device, configurations=configurations), held=tuple(operation.held))
        return after, operation.files.backup if operation.files.backup.exists() else None
