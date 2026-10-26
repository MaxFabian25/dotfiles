"""Opt-in native-loader integration test. Uses isolated application homes; no account or live config."""
from dataclasses import replace
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import shutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from description import Roots
from description_toml import load
from device import Device
from device_sync import apply, plan
from host_state import DeviceRoots, Host, FactKey, Surface
from drift import render
from sync_sources import Builder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('public', type=Path)
    parser.add_argument('private', type=Path, nargs='?')
    args = parser.parse_args()
    setup = load(Roots(args.public, args.private))
    setup = replace(setup, packages={'prose': setup.packages['prose']}, repos={}, loose={},
                    host_skills={}, rules=None, lock={'prose': setup.lock['prose']}, runtime=None)
    real = Device.current()
    if real.absent:
        raise RuntimeError(f'Both native CLIs are required: {real.absent}')
    scratch = Path.home() / '.local/state/dotfiles/integration'
    scratch.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix='native-', dir=scratch))
    try:
        roots = DeviceRoots(home, home / '.codex', home / '.claude', home / '.agents', home / 'engine')
        for folder in (roots.codex_home, roots.claude_home):
            folder.mkdir()
        test_environment = {'HOME': str(home), 'CODEX_HOME': str(roots.codex_home),
                            'CLAUDE_CONFIG_DIR': str(roots.claude_home)}
        if os.name == 'nt':
            test_environment.update(USERPROFILE=str(home), HOMEDRIVE=home.drive,
                                    HOMEPATH=str(home)[len(home.drive):])
        os.environ.update(test_environment)
        device = replace(real, roots=roots, hosts=tuple(type(h)(roots, h.cli) for h in real.hosts))
        before = plan(setup, device, configurations=False)
        if before.blocked:
            raise RuntimeError(render(before.result))
        after, backup = apply(setup, device, before, builder=Builder(setup, roots, roots.engine), configurations=False)
        if after.exit_code:
            raise RuntimeError(render(after.result))
        mtimes = {p: p.stat().st_mtime_ns for p in home.rglob('*') if p.is_file()}
        paths = set(home.rglob('*'))
        again, second_backup = apply(setup, device, after, configurations=False)
        assert second_backup is None and again.exit_code == 0
        assert set(home.rglob('*')) == paths
        assert all(p.stat().st_mtime_ns == stamp for p, stamp in mtimes.items())
        installed = after.observations[Host.CODEX].details[FactKey(Host.CODEX, Surface.PLUGIN, 'prose@personal')].root
        edited = installed / 'skills/prose/SKILL.md'
        original = edited.read_bytes()
        edited.write_bytes(original + b'\nA deliberate local edit in an isolated smoke fixture.\n')
        conflict = plan(setup, device, configurations=False)
        assert conflict.blocked and conflict.exit_code != 0
        try:
            apply(setup, device, conflict, configurations=False)
        except RuntimeError:
            pass
        else:
            raise AssertionError('A pre-existing local edit did not block native reconciliation')
        assert edited.read_bytes() == original + b'\nA deliberate local edit in an isolated smoke fixture.\n'
        print(json.dumps({'native_install_verified': True, 'second_apply_noop': True,
                          'preplan_edit_preserved': True, 'platform': device.platform.value,
                          'live_homes_untouched': True}))
    except BaseException:
        print(f'Isolated integration evidence retained at {home}', file=sys.stderr)
        raise
    else:
        shutil.rmtree(home)


if __name__ == '__main__':
    main()
