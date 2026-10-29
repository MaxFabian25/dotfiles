"""Build reviewed entries and refresh only their locks. Never changes an installed application."""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from description import LocalSource, Lock, LockedTree, Package, Roots, SnapshotSource, Visibility, intent
from description_toml import load, render_root
from host_state import DeviceRoots, Host
from sync_files import Files, contents
from sync_sources import Builder, Sources, child


def refresh(setup, builder, names, *, owned=False):
    packages, loose, locks = dict(setup.packages), dict(setup.loose), dict(setup.lock)
    for name in names:
        entry = packages.get(name) or loose[name]
        if owned and isinstance(entry.source, LocalSource):
            tree = child(setup.roots.of(entry.visibility).parent, entry.source.path)
            entry = replace(entry, source=replace(entry.source, digest=builder.roots.tree_digest(tree)))
            (packages if isinstance(entry, Package) else loose)[name] = entry
        if isinstance(entry, Package):
            previous = locks.get(name, Lock(intent(entry, setup.repos)))
            trees, roles, hooks = dict(previous.trees), previous.roles, previous.hooks
            if entry.source is not None and any(p and any(i.enabled for i in p.installs) for p in (entry.codex, entry.claude)):
                built = builder.codex(entry, verify=False)
                trees[Host.CODEX] = LockedTree(built.version, built.digest)
                for built_claude in builder.claude(entry, built, verify=False).values():
                    trees[Host.CLAUDE] = LockedTree(built_claude.version, built_claude.digest)
                if entry.codex and any(i.enabled for i in entry.codex.installs):
                    from host_state import bytes_digest
                    roles = {n: bytes_digest(builder.roots.neutral(data))
                             for n, data in builder.roles(entry, built, verify=False).items()}
                    if entry.codex.hooks.value == 'trusted':
                        from codex_host import _hook_keys
                        hooks = len(_hook_keys(name, built.root))
            locks[name] = Lock(intent(entry, setup.repos), trees, roles, hooks=hooks)
        else:
            placements = {}
            for place in entry.place:
                source = entry.source
                if isinstance(source, LocalSource):
                    tree = builder.local(entry.visibility, source, place)
                elif isinstance(source, SnapshotSource):
                    tree = setup.roots.private / 'snapshots' / str(source.snapshots[place]).removeprefix('sha256:')
                else:
                    repo = setup.repos[source.repo]
                    tree = builder.directory / '.sync/loose' / name / repo.commit
                    if not tree.exists():
                        builder.sources.tree(repo.url, repo.commit, tree, source.path)
                placements[place] = builder.roots.tree_digest(tree)
            locks[name] = Lock(intent(entry, setup.repos), placements=placements)
    return replace(setup, packages=packages, loose=loose, lock=locks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public', type=Path, required=True)
    parser.add_argument('--private', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--entry', action='append', required=True)
    parser.add_argument('--seed', type=Path, action='append', default=[])
    parser.add_argument('--refresh-owned', action='store_true', help='accept reviewed edits to the named owned sources')
    parser.add_argument('--write', action='store_true', help='write manifests and locks; otherwise show files that would change')
    args = parser.parse_args()
    setup = load(Roots(args.public, args.private))
    if unknown := set(args.entry) - (setup.packages.keys() | setup.loose.keys()):
        parser.error('unknown entries: ' + ', '.join(sorted(unknown)))
    roots = replace(DeviceRoots.current(), engine=args.output)
    builder = Builder(setup, roots, args.output, sources=Sources(args.output / '.sync/sources', seeds=tuple(args.seed)))
    result = refresh(setup, builder, args.entry, owned=args.refresh_owned)
    writes = []
    for visibility in Visibility:
        root = setup.roots.of(visibility)
        if root is None:
            continue
        for relative, data in render_root(result, visibility, root).items():
            path = root / relative
            before = contents(path)
            if data != before:
                writes.append((path, data, before))
    files = Files(args.output / '.sync/lock-backups')
    for path, data, before in writes:
        print(('write ' if args.write else 'would write ') + str(path))
        if args.write:
            files.write(path, data, expected=before)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
