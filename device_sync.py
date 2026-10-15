from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

from description import Setup
from device import Device
from drift import Diff, Difference, Kind, Verdict, diff
from host_state import FactKey, Host, Observation, Request, Surface


@dataclass(frozen=True)
class Plan:
    result: Diff
    observations: Mapping[Host, Observation]
    changes: tuple[Difference, ...]
    blocked: tuple[Difference, ...]
    unmanaged: tuple[Difference, ...]
    configurations: bool = True

    @property
    def exit_code(self):
        if self.blocked:
            return 3 if any(d.kind == Kind.INCOMPLETE for d in self.blocked) else 2
        return 1 if self.changes else 0


def plan(setup: Setup, device: Device, *, configurations=True) -> Plan:
    from accepted_installations import AcceptedInstallations

    projected = setup.for_platform(device.platform)
    projects = {state.host: state.project(projected) for state in device.hosts}
    observations = {state.host: state.observe(replace(Request.of(projects[state.host]), overlays={}))
                    for state in device.hosts}
    result = diff(setup, device, observations=observations)
    owner = AcceptedInstallations(projected, device, configurations=configurations)
    artifacts, owned = owner.inspect(observations)
    stale = setup.stale()
    managed = {key for facts in projects.values() for key, fact in facts.items()
               if fact.entry not in stale and key.surface in
               (Surface.TREE, Surface.OVERLAY, Surface.AGENT, Surface.LOOSE_SKILL, Surface.RULES)}
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
    result = replace(result, differences=retained + owned)
    blocked = tuple(d for d in result.differences if d.kind.verdict == Verdict.ATTENTION)
    unmanaged = tuple(d for d in result.differences if d.kind == Kind.EXTRA and d.entry is None)
    changes = tuple(d for d in result.drift() if d not in unmanaged)
    return Plan(result, observations, changes, blocked, unmanaged, configurations)


def changed_packages(current: Plan) -> frozenset[str]:
    return frozenset(d.entry.name for d in current.changes if d.entry is not None
                     and d.key is not None and d.key.surface in
                     (Surface.PLUGIN, Surface.TREE, Surface.OVERLAY, Surface.SKILL, Surface.AGENT))


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
    with exclusive_lock(device.roots.codex_home / 'dotfiles/sync.lock'):
        checked = plan(setup, device, configurations=configurations)
        if checked.result.differences != current.result.differences:
            raise RuntimeError('The device changed after planning; run sync again to review its current state')
        builder = builder or Builder(setup, device.roots, device.roots.engine)
        operation = Apply(setup.for_platform(device.platform), device, current, builder)
        operation.prepare()
        operation.run()
        return plan(setup, device, configurations=configurations), operation.files.backup if operation.files.backup.exists() else None
