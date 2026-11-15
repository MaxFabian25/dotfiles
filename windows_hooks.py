"""Deliver the private Windows hook selection without enabling the Linux hook set."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

from home_files import Delivery, deliver as deliver_files
from sync_files import Files, contents

SOURCE = 'config/agent-env/windows-hooks.json'
SCRIPT = re.compile(r'\.claude[/\\]bin[/\\]([\w.-]+\.py)')
TARGETS = {'claude': '.claude/settings.json', 'codex': '.codex/hooks.json'}


def relative(text: str) -> str:
    path = PurePosixPath(text)
    if not text or path.is_absolute() or '..' in path.parts or '\\' in text or ':' in text:
        raise ValueError(f'Windows hook path must be relative: {text!r}')
    return path.as_posix()


def render(source: dict, home: Path) -> dict:
    native = home.as_posix()
    if any(c in native for c in "\"'%!$`\r\n\x00"):
        raise ValueError('Windows hook home contains shell-sensitive characters')
    windows = PureWindowsPath(native)
    wsl = ('/mnt/' + windows.drive[0].lower() + '/' + '/'.join(windows.parts[1:])
           if windows.drive else native)
    return json.loads(json.dumps(source).replace('{home}', native).replace('{wsl_home}', wsl))


def shaped(document) -> bool:
    hooks = document.get('hooks', {}) if isinstance(document, dict) else None
    return isinstance(hooks, dict) and all(
        isinstance(groups, list) and all(isinstance(group, dict) and isinstance(group.get('hooks'), list)
            and all(isinstance(hook, dict) and isinstance(hook.get('command', ''), str)
                    for hook in group['hooks']) for group in groups)
        for groups in hooks.values())


def merged(current: dict, wanted: dict) -> dict:
    result = json.loads(json.dumps(current))
    events = result.setdefault('hooks', {})
    for event, groups in wanted.items():
        owned = {name for group in groups for hook in group['hooks']
                 for name in SCRIPT.findall(hook.get('command', ''))}
        kept = []
        for group in events.get(event, []):
            if group in groups:
                if group not in kept:
                    kept.append(group)
            else:
                hooks = [hook for hook in group['hooks']
                         if not owned.intersection(SCRIPT.findall(hook.get('command', '')))]
                if len(hooks) == len(group['hooks']):
                    kept.append(group)
                elif hooks:
                    kept.append({**group, 'hooks': hooks})
        events[event] = kept + [group for group in groups if group not in kept]
    return result


def deliver(private: Path, home: Path, state: Path, *, apply: bool) -> Delivery:
    path = private / SOURCE
    if not path.is_file():
        return Delivery((), ())
    source = json.loads(path.read_text(encoding='utf-8'))
    sources = {relative(target): private / relative(origin) for target, origin in source['files'].items()}
    registrations = render(source['registrations'], home)
    if set(registrations) - TARGETS.keys() or not all(shaped(doc) for doc in registrations.values()):
        raise ValueError(f'{path}: invalid Windows hook registrations')
    for target, origin in sources.items():
        if not origin.is_file():
            raise ValueError(f'{path}: missing Windows hook source for {target}')
    delivery = deliver_files(private, home, state, apply=apply, sources=sources)
    written, left = list(delivery.written), list(delivery.left)
    if left:
        return Delivery(tuple(written), (*left, (SOURCE, 'hook registrations wait for the selected files')))
    files = Files(state / 'home-file-backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    for host, wanted in registrations.items():
        target = home / TARGETS[host]
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f'{target}: Windows hook settings are not a plain file')
        before = contents(target)
        current = json.loads(before) if before is not None else {}
        if not shaped(current):
            raise ValueError(f'{target}: invalid Windows hook settings')
        after = merged(current, wanted['hooks'])
        if after != current:
            if apply:
                files.write(target, (json.dumps(after, indent=2, ensure_ascii=False) + '\n').encode(), expected=before)
            written.append(TARGETS[host])
    return Delivery(tuple(written), tuple(left))
