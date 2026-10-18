"""Render or install this device's half-hourly pull schedule, after a paired release passes sync."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import getpass
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape

from pull import paired, signers
from sync_files import Files, backup_directory, contents
from host_state import DeviceRoots


LABEL = 'dev.dotfiles.pull'


def command(root, private):
    uv = shutil.which('uv')
    if not uv:
        raise RuntimeError('Install uv before scheduling dotfiles')
    return [uv, 'run', '--locked', '--project', str(root), 'python', str(root / 'pull.py'),
            '--root', str(root), '--private', str(private), '--update', '--apply']


def render(platform, home, argv):
    state = home / '.local/state/dotfiles'
    if platform == 'darwin':
        data = {'Label': LABEL, 'ProgramArguments': argv, 'RunAtLoad': True, 'StartInterval': 1800,
                'ProcessType': 'Background', 'StandardOutPath': str(state / 'pull.log'),
                'StandardErrorPath': str(state / 'pull-error.log'),
                'EnvironmentVariables': {'PATH': ':'.join([str(home / '.local/bin'), str(home / '.local/share/mise/shims'),
                                                          '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin'])}}
        return {home / 'Library/LaunchAgents' / f'{LABEL}.plist': plistlib.dumps(data)}
    if platform == 'win32':
        args = escape(subprocess.list2cmdline(argv[1:]))
        start = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(timespec='seconds')
        xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
 <RegistrationInfo><Description>Pull verified dotfiles releases for this user</Description></RegistrationInfo>
 <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(getpass.getuser())}</UserId></LogonTrigger>
 <TimeTrigger><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled>
 <Repetition><Interval>PT30M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition></TimeTrigger></Triggers>
 <Principals><Principal id="User"><UserId>{escape(getpass.getuser())}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
 <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT25M</ExecutionTimeLimit><Enabled>true</Enabled></Settings>
 <Actions Context="User"><Exec><Command>{escape(argv[0])}</Command><Arguments>{args}</Arguments></Exec></Actions>
</Task>'''
        return {state / 'pull-task.xml': xml.encode('utf-16')}
    def quote(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    search_path = ':'.join([str(home / '.local/bin'), str(home / '.local/share/mise/shims'),
                            str(home / '.cargo/bin'), '/usr/local/bin', '/usr/bin', '/bin'])
    service = ('[Unit]\nDescription=Pull verified dotfiles releases\n\n[Service]\nType=oneshot\nEnvironment='
               + quote('PATH=' + search_path) + '\nExecStart=' + ' '.join(map(quote, argv)) + '\n')
    timer = '[Unit]\nDescription=Check dotfiles every 30 minutes\n\n[Timer]\nOnStartupSec=2min\nOnUnitActiveSec=30min\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n'
    folder = home / '.config/systemd/user'
    return {folder / 'dotfiles-pull.service': service.encode(), folder / 'dotfiles-pull.timer': timer.encode()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--private', type=Path, required=True)
    parser.add_argument('--install', action='store_true', help='install and enable the local schedule')
    args = parser.parse_args()
    root, private = args.root.resolve(), args.private.resolve()
    argv = command(root, private)
    if not args.install:
        for path in render(sys.platform, Path.home(), argv):
            print(f'would install {path}')
        return 0
    if not signers().is_file():
        raise RuntimeError(f'{signers()}: pin trusted release signers before enabling automatic pulls')
    paired(root, private)
    check = subprocess.run([sys.executable, str(root / 'sync.py'), '--public', str(root / 'setup'),
                            '--private', str(private / 'setup'), '--check'], cwd=root)
    if check.returncode:
        raise RuntimeError('Resolve device drift before enabling its automatic pull schedule')
    files = render(sys.platform, Path.home(), argv)
    state = Path.home() / '.local/state/dotfiles'
    state.mkdir(parents=True, exist_ok=True)
    writer = Files(backup_directory(DeviceRoots.current()))
    for path, data in files.items():
        writer.write(path, data, expected=contents(path))
    if sys.platform == 'darwin':
        import os
        domain = f'gui/{os.getuid()}'
        subprocess.run(['launchctl', 'bootout', f'{domain}/{LABEL}'], capture_output=True)
        subprocess.run(['launchctl', 'bootstrap', domain, str(next(iter(files)))], check=True)
    elif sys.platform == 'win32':
        subprocess.run(['schtasks.exe', '/Create', '/TN', LABEL, '/XML', str(next(iter(files))), '/F'], check=True)
    else:
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now', 'dotfiles-pull.timer'], check=True)
    print('Local pull schedule enabled; daily source review remains on the maintainer machine.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
