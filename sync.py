#!/usr/bin/env python3
"""Synchronize this device from the pinned public and private descriptions. The default only checks."""
import argparse
from pathlib import Path
import subprocess
import sys

from candidate_result import CandidateResult, HeldCache, dump
from description import DescriptionError, Roots
from description_toml import load
from device import Device
from device_sync import apply, plan
from drift import render, to_json
import sync_ending


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public', type=Path)
    parser.add_argument('--private', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='apply verified changes, with local backups')
    mode.add_argument('--check', action='store_true', help='report the plan without installing anything (default)')
    mode.add_argument('--candidate', action='store_true',
                      help="check from a release worktree's engine before the release is pushed")
    parser.add_argument('--json', action='store_true',
                        help='print the plan as JSON; with --candidate, only the result release.py reads')
    parser.add_argument('--notes', action='store_true', help='list each note in full instead of counting them')
    parser.add_argument(sync_ending.FLAG, dest='ending', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    status = run(args)
    if args.ending is None:
        return status
    sync_ending.record(args.ending, status)
    return 0


def run(args):
    try:
        setup = load(Roots.resolve(args.public, args.private))
        device = Device.current()
        installed = device.roots.installed_engine()
        # A candidate's engine is not the one Claude installs from, so its plan also lists the plugin builds it
        # cannot see as changes. Only a stop or a blocker tells whether the pull could apply the release.
        if installed is not None and installed != device.roots.engine.resolve() and not args.candidate:
            raise RuntimeError(f'this device installs from the checkout at {installed}, whose plugin builds this engine '
                               f'at {device.roots.engine} cannot see; run sync.py or pull.py from that checkout')
        current = plan(setup, device)
        if args.candidate:
            # The apply step, which a candidate never reaches, validates changed settings and checks each file
            # against the copy it last accepted, and a pull stops there too.
            from accepted_installations import AcceptedInstallations
            from configuration import validate_runtime
            owner = AcceptedInstallations(setup.for_platform(device.platform), device)
            validate_runtime(owner.configurations, device.roots, proposed=True)
            owner.rehearse(current.observations)
            from sync_hosts import Rehearsal
            Rehearsal(setup.for_platform(device.platform), device, current).run()
            held = tuple(HeldCache(identity, cache, tuple(holders))
                         for identity, cache, holders in owner.held_replacements(current.observations))
            if args.json:
                if current.blocked:  # release.py shows these rows, which name each blocker and its fix
                    print(render(current.result, notes=args.notes), end='', file=sys.stderr)
                print(dump(CandidateResult(len(current.blocked), held)))
                return current.exit_code
            for cache in held:
                print(cache.lease)
        if args.apply:
            if current.blocked and not args.json:
                print(render(current.result, notes=args.notes), end='')  # its attention rows name each blocker and its fix
            current, backup = apply(setup, device, current)
            if backup:
                print(f'Backups: {backup}', file=sys.stderr)
            for cache, holders in current.held:
                print(f'Claude lease: {cache}, held by running Claude processes {", ".join(map(str, holders))}, '
                      'stays as it is; the first pull after they exit replaces it', file=sys.stderr)
        print(to_json(current.result) if args.json else render(current.result, notes=args.notes),
              end='\n' if args.json else '')
        if not args.json:
            expected = f', plus {len(current.expected)} this device is expected to have' if current.expected else ''
            print(f'Managed changes: {len(current.changes)}; blocking issues: {len(current.blocked)}; '
                  f'unmanaged differences left untouched: {len(current.unmanaged)}{expected}.')
        return current.exit_code
    except (DescriptionError, RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        tail = _stderr_tail(error)
        print(f'Sync stopped: {error}' + (f' It said: {tail}' if tail else ''), file=sys.stderr)
        return 3


def _stderr_tail(error):
    stderr = error.stderr if isinstance(error, subprocess.CalledProcessError) else None
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors='replace')
    lines = (stderr or '').strip().splitlines()
    return lines[-1].strip() if lines else ''


if __name__ == '__main__':
    raise SystemExit(main())
