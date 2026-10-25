#!/usr/bin/env python3
"""Synchronize this device from the pinned public and private descriptions. The default only checks."""
import argparse
from pathlib import Path
import sys

from description import DescriptionError, Roots
from description_toml import load
from device import Device
from device_sync import apply, plan
from drift import render, to_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public', type=Path)
    parser.add_argument('--private', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='apply verified changes, with local backups')
    mode.add_argument('--check', action='store_true', help='report the plan without installing anything (default)')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        setup = load(Roots.resolve(args.public, args.private))
        device = Device.current()
        installed = device.roots.installed_engine()
        if installed is not None and installed != device.roots.engine.resolve():
            raise RuntimeError(f'this device installs from the checkout at {installed}, whose plugin builds this engine '
                               f'at {device.roots.engine} cannot see; run sync.py or pull.py from that checkout')
        current = plan(setup, device)
        if args.apply:
            if current.blocked and not args.json:
                print(render(current.result), end='')  # its attention rows name each blocker and its fix
            current, backup = apply(setup, device, current)
            if backup:
                print(f'Backups: {backup}', file=sys.stderr)
        print(to_json(current.result) if args.json else render(current.result), end='\n' if args.json else '')
        if not args.json:
            expected = f', plus {len(current.expected)} this device is expected to have' if current.expected else ''
            print(f'Managed changes: {len(current.changes)}; blocking issues: {len(current.blocked)}; '
                  f'unmanaged differences left untouched: {len(current.unmanaged)}{expected}.')
        return current.exit_code
    except (DescriptionError, RuntimeError, ValueError, OSError) as error:
        print(f'Sync stopped: {error}', file=sys.stderr)
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
