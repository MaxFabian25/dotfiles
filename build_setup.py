"""Rebuild the described packages into an isolated output directory and verify their locked bytes."""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import subprocess
import traceback

from description import AlignedInstall, EngineInstall, Roots
from description_toml import load
from host_state import DeviceRoots, Host, Platform
from sync_sources import Builder, BuildError, Sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public', type=Path, required=True)
    parser.add_argument('--private', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seed', type=Path, action='append', default=[])
    parser.add_argument('--platform', choices=[p.value for p in Platform])
    parser.add_argument('--package', action='append', default=[])
    parser.add_argument('--unmanaged', action='store_true',
                        help='also build each package that no install manages and that Codex installs through the '
                             'engine, with its engine and codex-aligned installs switched on, verified only where the '
                             'lock holds its tree, and print each built digest')
    args = parser.parse_args()
    # Git runs each fetch inside the checkout it fills, where a relative source path names nothing.
    output, seeds = args.output.absolute(), tuple(seed.absolute() for seed in args.seed)
    setup = load(Roots(args.public, args.private))
    if setup.stale():
        parser.error('stale locks: ' + ', '.join(sorted(setup.stale())))
    setup = setup.for_platform(Platform(args.platform) if args.platform else Platform.native())
    roots = replace(DeviceRoots.current(), engine=output)
    sources = Sources(output / '.sync/sources', seeds=seeds)
    builder = Builder(setup, roots, output, sources=sources)
    errors = []
    built = unmanaged = 0
    for name, package in sorted(setup.packages.items()):
        if package.source is None or (args.package and name not in args.package):
            continue
        managed = any(p and any(i.managed for i in p.installs) for p in (package.codex, package.claude))
        if not managed:
            # A codex-aligned copy is built from the Codex engine install, so without one there is nothing to switch on.
            # That leaves out a package whose only engine install is Claude's too; no package has that shape.
            if not (args.unmanaged and package.codex and any(isinstance(i, EngineInstall) for i in package.codex.installs)):
                continue
            package = replace(package, **{host.value: switched_on(package.placement(host)) for host in Host})
        try:
            codex = builder.codex(package)
            claude = builder.claude(package, codex)
            if not managed:
                unmanaged += 1
                for identity, tree in (('codex', codex), *sorted(claude.items())):
                    print(f'built unmanaged {name} {identity}: {tree.version} {tree.digest}', flush=True)
                continue
            roles = builder.roles(package, codex) if package.codex and any(i.enabled for i in package.codex.installs) else {}
            built += 1
            print(f'verified {name}: Codex tree, {len(claude)} Claude tree(s), {len(roles)} role(s)', flush=True)
        except Exception as error:  # a fault in one builder fails its package, not the run
            errors.append(name)
            # A fault of these types names itself in its text. Any other is a fault in the engine: a KeyError's text is
            # only the key, so it prints with its type, and its traceback says where.
            worded = isinstance(error, (BuildError, ValueError, OSError, RuntimeError, subprocess.CalledProcessError))
            print(f'FAILED {name}: {error if worded else repr(error)}', flush=True)
            if not worded:
                traceback.print_exc()
    built_unmanaged = f'{unmanaged} unmanaged packages built; ' if args.unmanaged else ''
    print(f'{built} packages rebuilt and verified; {built_unmanaged}{len(errors)} failed. '
          'No host configuration or installed cache changed.')
    return 1 if errors else 0


def switched_on(placement):
    if placement is None:
        return None
    return replace(placement, installs=tuple(replace(i, enabled=True) if isinstance(i, (EngineInstall, AlignedInstall))
                                             else i for i in placement.installs))


if __name__ == '__main__':
    raise SystemExit(main())
