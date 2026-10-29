from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Mapping
import re
import tomllib

import tomlkit

from host_state import DeviceRoots, Platform
from pstack_runtime import OVERRIDE_KEY, model_entries, model_target, validate_codex, validate_models


@dataclass(frozen=True)
class FileConfiguration:
    name: str
    destination: Path
    data: bytes


@dataclass(frozen=True)
class TomlConfiguration:
    name: str
    destination: Path
    fields: Mapping[tuple[str, ...], object]


@dataclass(frozen=True)
class NpmrcConfiguration:
    name: str
    destination: Path
    fields: Mapping[tuple[str, ...], object]


@dataclass(frozen=True)
class JsonConfiguration:
    name: str
    destination: Path
    fields: Mapping[tuple[str, ...], object]


@dataclass(frozen=True)
class BlockedConfiguration:
    """A configuration this device cannot satisfy. The plan reports it and applies nothing."""
    name: str
    destination: Path
    reason: str


Configuration = FileConfiguration | TomlConfiguration | NpmrcConfiguration | JsonConfiguration | BlockedConfiguration


def desired(public: Path | None, private: Path | None, roots: DeviceRoots, platform: Platform,
            environ: Mapping[str, str] = os.environ) -> tuple[Configuration, ...]:
    result: list[Configuration] = []
    mise = public / 'config/mise/config.toml' if public else None
    if mise and mise.is_file():
        source = tomllib.loads(mise.read_text(encoding='utf-8'))
        if ('tools' not in source or not set(source) <= {'tools', 'env'}
                or not all(isinstance(table, dict) for table in source.values())):
            raise ValueError('Public mise configuration must contain only a tools table and an optional env table')
        result.append(TomlConfiguration('mise-tools', roots.home / '.config/mise/config.toml',
                                        {('tools', name): value for name, value in source['tools'].items()}
                                        | _leaf_fields(source.get('env', {}), ('env',))))
    npm = public / 'config/npm/npmrc.toml' if public else None
    if npm and npm.is_file():
        source = tomllib.loads(npm.read_text(encoding='utf-8'))
        if not source or not all(isinstance(value, (str, int)) for value in source.values()):
            raise ValueError('Public npm configuration must contain only top-level string, integer or boolean keys')
        result.append(NpmrcConfiguration('npm-user', roots.home / '.npmrc',
                                         {(key,): _npmrc_value(value) for key, value in source.items()}))
    chrome = public / 'config/agent-browser/chrome.toml' if public else None
    if chrome and chrome.is_file():
        result.append(_agent_browser(tomllib.loads(chrome.read_text(encoding='utf-8')), roots, platform, environ))
    pstack = private / 'config/pstack' if private else None
    if pstack and pstack.is_dir():
        models = (pstack / 'models.md').read_bytes()
        entries = model_entries(models)
        settings = tomllib.loads((pstack / 'runtime.toml').read_text(encoding='utf-8'))
        if (set(settings) - {'spawn_overrides', 'excluded_codex_models'}
                or not isinstance(settings.get('spawn_overrides'), bool)):
            raise ValueError('Pstack runtime configuration needs boolean spawn_overrides and optional excluded_codex_models')
        excluded = settings.get('excluded_codex_models', [])
        if (not isinstance(excluded, list)
                or any(not isinstance(slug, str) or not slug or re.search(r'[\s:@,]', slug)
                       or slug in {'auto', 'inherit-parent'} for slug in excluded)):
            raise ValueError('Pstack excluded_codex_models must be a list of nonempty bare model slugs')
        excluded = frozenset(excluded)
        for section, role, model, _effort in entries:
            if model in {'auto', 'inherit-parent'}:
                continue
            harness, slug = model_target(section, model)
            if harness == 'codex' and slug in excluded:
                raise ValueError(f'Excluded Codex model for {section}/{role}: {slug} (excluded_codex_models)')
        result.extend([
            FileConfiguration('pstack-models', roots.home / '.config/pstack/models.md', models),
            TomlConfiguration('pstack-runtime', roots.codex_home / 'config.toml',
                              {('features', 'multi_agent_v2', OVERRIDE_KEY): settings['spawn_overrides']}),
        ])
    return tuple(result)


def _leaf_fields(table: dict, prefix: tuple[str, ...]) -> dict[tuple[str, ...], object]:
    result = {}
    for key, value in table.items():
        result.update(_leaf_fields(value, (*prefix, key)) if isinstance(value, dict) else {(*prefix, key): value})
    return result


_WINDOWS_VARIABLE = re.compile(r'%([^%]+)%')


def _agent_browser(candidates: dict, roots: DeviceRoots, platform: Platform,
                   environ: Mapping[str, str]) -> JsonConfiguration | BlockedConfiguration:
    if (set(candidates) != {item.value for item in Platform}
            or not all(isinstance(paths, list) and paths and all(isinstance(path, str) for path in paths)
                       for paths in candidates.values())):
        raise ValueError('agent-browser Chrome candidates need a list of paths for each of macos, linux and windows')
    destination = roots.home / '.agent-browser/config.json'
    paths = [path for path in (_expand(template, roots.home, environ) for template in candidates[platform.value])
             if path is not None]
    try:
        current = read_fields(destination.read_bytes() if destination.is_file() else None,
                              {('executablePath',): None}, 'json')[('executablePath',)].get('value')
    except ValueError:
        current = None
    chosen = installed_chrome(current, paths)
    if chosen is None:
        return BlockedConfiguration('agent-browser', destination,
                                    'No installed Google Chrome found for agent-browser. Install Google Chrome; '
                                    'Chrome for Testing is not accepted. Checked: ' + ', '.join(paths))
    return JsonConfiguration('agent-browser', destination, {('executablePath',): chosen})


def _expand(template: str, home: Path, environ: Mapping[str, str]) -> str | None:
    if not all(environ.get(name) for name in _WINDOWS_VARIABLE.findall(template)):
        return None
    return _WINDOWS_VARIABLE.sub(lambda match: environ[match[1]], template.replace('{home}', str(home)))


def installed_chrome(current: object, candidates: list[str], is_file=os.path.isfile) -> str | None:
    """The current path when it is an installed candidate, so a working choice never churns, else the first
    installed candidate."""
    installed = [path for path in candidates if is_file(path)]
    same = lambda path: os.path.normcase(os.path.normpath(path))
    if isinstance(current, str) and any(same(current) == same(path) for path in installed):
        return current
    return installed[0] if installed else None


def _document(data: bytes | None, syntax: str) -> dict:
    if data is None:
        return {}
    text = data.decode('utf-8-sig')
    if syntax == 'toml':
        return tomllib.loads(text)
    document = json.loads(text)
    if not isinstance(document, dict):
        raise ValueError('A managed JSON file must hold an object')
    return document


def read_fields(data: bytes | None, fields: Mapping[tuple[str, ...], object],
                syntax: str = 'toml') -> dict[tuple[str, ...], object]:
    if syntax == 'npmrc':
        return _read_npmrc(data, fields)
    document = _document(data, syntax)
    values = {}
    for path in fields:
        value = document
        for key in path:
            if not isinstance(value, dict):
                raise ValueError(f'A managed {syntax.upper()} key traverses a non-table: ' + '.'.join(path))
            if key not in value:
                values[path] = {'present': False}
                break
            value = value[key]
        else:
            values[path] = {'present': True, 'value': value}
    return values


def patch_fields(data: bytes | None, fields: Mapping[tuple[str, ...], object], syntax: str = 'toml') -> bytes:
    if syntax == 'npmrc':
        return _patch_npmrc(data, fields)
    if syntax == 'json':
        return _patch_json(data, fields)
    before = data.decode('utf-8-sig') if data is not None else ''
    document = tomlkit.parse(before)
    for path, desired_value in fields.items():
        table = document
        for key in path[:-1]:
            table = table.setdefault(key, tomlkit.table())
            if not isinstance(table, dict):
                raise ValueError('A managed TOML key traverses a non-table: ' + '.'.join(path))
        if table.get(path[-1]) != desired_value:
            table[path[-1]] = desired_value
    after = tomlkit.dumps(document).encode('utf-8')
    if data is not None and data.startswith(b'\xef\xbb\xbf'):
        after = b'\xef\xbb\xbf' + after
    return after


def _patch_json(data: bytes | None, fields: Mapping[tuple[str, ...], object]) -> bytes:
    """Rewrites the file only when a managed value changes. A rewrite keeps every other key and their order."""
    document = _document(data, 'json')
    changed = False
    for path, desired_value in fields.items():
        table = document
        for key in path[:-1]:
            table = table.setdefault(key, {})
            if not isinstance(table, dict):
                raise ValueError('A managed JSON key traverses a non-object: ' + '.'.join(path))
        if path[-1] not in table or table[path[-1]] != desired_value:
            table[path[-1]] = desired_value
            changed = True
    if data is not None and not changed:
        return data
    after = (json.dumps(document, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    if data is not None and data.startswith(b'\xef\xbb\xbf'):
        after = b'\xef\xbb\xbf' + after
    return after


_NPMRC_SECTION = re.compile(r'\s*\[.*\]\s*$')


def _npmrc_value(value: str | int) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def _npmrc_lines(text: str) -> tuple[list[str], int]:
    """Lines with their endings, and the end of the top level that precedes any [section]."""
    lines = text.splitlines(keepends=True)
    top = next((index for index, line in enumerate(lines) if _NPMRC_SECTION.match(line)), len(lines))
    return lines, top


def _npmrc_entry(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped[0] in ';#':
        return None
    key, separator, value = stripped.partition('=')
    value = value.strip() if separator else 'true'
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
        value = value[1:-1]
    return key.strip(), value


def _npmrc_find(lines: list[str], top: int, key: str) -> tuple[int, str] | None:
    """npm reads the last top-level assignment of a key."""
    for index in reversed(range(top)):
        entry = _npmrc_entry(lines[index])
        if entry and entry[0] == key:
            return index, entry[1]
    return None


def _read_npmrc(data: bytes | None, fields: Mapping[tuple[str, ...], object]) -> dict[tuple[str, ...], object]:
    lines, top = _npmrc_lines(data.decode('utf-8-sig') if data is not None else '')
    values = {}
    for path in fields:
        found = _npmrc_find(lines, top, path[0])
        values[path] = {'present': False} if found is None else {'present': True, 'value': found[1]}
    return values


def _patch_npmrc(data: bytes | None, fields: Mapping[tuple[str, ...], object]) -> bytes:
    lines, top = _npmrc_lines(data.decode('utf-8-sig') if data is not None else '')
    newline = next((line[len(line.rstrip('\r\n')):] for line in lines if line.endswith('\n')), '\n')
    for (key,), desired_value in fields.items():
        found = _npmrc_find(lines, top, key)
        if found is not None:
            index, value = found
            if value != desired_value:
                lines[index] = f'{key}={desired_value}' + lines[index][len(lines[index].rstrip('\r\n')):]
            continue
        if top and not lines[top - 1].endswith('\n'):
            lines[top - 1] += newline
        lines.insert(top, f'{key}={desired_value}{newline}')
        top += 1
    after = ''.join(lines).encode('utf-8')
    if data is not None and data.startswith(b'\xef\xbb\xbf'):
        after = b'\xef\xbb\xbf' + after
    return after


def validate_runtime(configurations: tuple[Configuration, ...], roots: DeviceRoots, *, proposed: bool):
    models = next((item for item in configurations if item.name == 'pstack-models'), None)
    runtime = next((item for item in configurations if item.name == 'pstack-runtime'), None)
    if models is not None:
        validate_models(roots.home, models.data, codex_home=roots.codex_home)
    if runtime is not None:
        enabled = runtime.fields[('features', 'multi_agent_v2', OVERRIDE_KEY)]
        validate_codex(enabled, proposed=proposed)
