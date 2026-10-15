from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
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


Configuration = FileConfiguration | TomlConfiguration


def desired(public: Path | None, private: Path | None, roots: DeviceRoots) -> tuple[Configuration, ...]:
    result: list[Configuration] = []
    mise = public / 'config/mise/config.toml' if public else None
    if mise and mise.is_file():
        source = tomllib.loads(mise.read_text(encoding='utf-8'))
        if set(source) != {'tools'} or not isinstance(source['tools'], dict):
            raise ValueError('Public mise configuration must contain only a tools table')
        result.append(TomlConfiguration('mise-tools', roots.home / '.config/mise/config.toml',
                                        {('tools', name): value for name, value in source['tools'].items()}))
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


def read_fields(data: bytes | None, fields: Mapping[tuple[str, ...], object]) -> dict[tuple[str, ...], object]:
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


def patch_fields(data: bytes | None, fields: Mapping[tuple[str, ...], object]) -> bytes:
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


def validate_runtime(configurations: tuple[Configuration, ...], roots: DeviceRoots, *, proposed: bool):
    models = next((item for item in configurations if item.name == 'pstack-models'), None)
    runtime = next((item for item in configurations if item.name == 'pstack-runtime'), None)
    if models is not None:
        validate_models(roots.home, models.data, codex_home=roots.codex_home)
    if runtime is not None:
        enabled = runtime.fields[('features', 'multi_agent_v2', OVERRIDE_KEY)]
        validate_codex(enabled, proposed=proposed)
