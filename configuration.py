from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
import re
import tomllib

import tomlkit

from host_state import DeviceRoots
from pstack_runtime import OVERRIDE_KEY, model_entries, validate_codex, validate_models


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


Configuration = FileConfiguration | TomlConfiguration | NpmrcConfiguration


def desired(public: Path | None, private: Path | None, roots: DeviceRoots) -> tuple[Configuration, ...]:
    result: list[Configuration] = []
    mise = public / 'config/mise/config.toml' if public else None
    if mise and mise.is_file():
        source = tomllib.loads(mise.read_text(encoding='utf-8'))
        if set(source) != {'tools'} or not isinstance(source['tools'], dict):
            raise ValueError('Public mise configuration must contain only a tools table')
        result.append(TomlConfiguration('mise-tools', roots.home / '.config/mise/config.toml',
                                        {('tools', name): value for name, value in source['tools'].items()}))
    npm = public / 'config/npm/npmrc.toml' if public else None
    if npm and npm.is_file():
        source = tomllib.loads(npm.read_text(encoding='utf-8'))
        if not source or not all(isinstance(value, (str, int)) for value in source.values()):
            raise ValueError('Public npm configuration must contain only top-level string, integer or boolean keys')
        result.append(NpmrcConfiguration('npm-user', roots.home / '.npmrc',
                                         {(key,): _npmrc_value(value) for key, value in source.items()}))
    pstack = private / 'config/pstack' if private else None
    if pstack and pstack.is_dir():
        models = (pstack / 'models.md').read_bytes()
        model_entries(models)
        settings = tomllib.loads((pstack / 'runtime.toml').read_text(encoding='utf-8'))
        if set(settings) != {'spawn_overrides'} or not isinstance(settings['spawn_overrides'], bool):
            raise ValueError('Pstack runtime configuration needs one boolean spawn_overrides')
        result.extend([
            FileConfiguration('pstack-models', roots.home / '.config/pstack/models.md', models),
            TomlConfiguration('pstack-runtime', roots.codex_home / 'config.toml',
                              {('features', 'multi_agent_v2', OVERRIDE_KEY): settings['spawn_overrides']}),
        ])
    return tuple(result)


def read_fields(data: bytes | None, fields: Mapping[tuple[str, ...], object],
                syntax: str = 'toml') -> dict[tuple[str, ...], object]:
    if syntax == 'npmrc':
        return _read_npmrc(data, fields)
    document = tomllib.loads(data.decode('utf-8-sig')) if data is not None else {}
    values = {}
    for path in fields:
        value = document
        for key in path:
            if not isinstance(value, dict):
                raise ValueError('A managed TOML key traverses a non-table: ' + '.'.join(path))
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
