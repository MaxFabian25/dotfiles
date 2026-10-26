"""The reviewed pin of OpenAI's Codex workspace runtime for Linux, the public setup/codex-runtime.toml.

The description loads the pin, so it lives apart from codex_runtime, whose bundle code imports drift and the host
observation stack.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import tomllib

from host_state import Digest, Platform, parse_digest

ORIGIN = 'https://persistent.oaistatic.com/codex-primary-runtime/'   # every archive URL starts here
PIN = 'codex-runtime.toml'                                           # in the public setup root
VERSION = re.compile(r'\d+(\.\d+)+')
_HEADER = """\
# OpenAI's Codex workspace runtime for Linux x64, which carries the openai-primary-runtime plugin catalog.
# The Codex app installs it on macOS and Windows. On Linux every pull installs exactly this archive at
# ~/.cache/codex-runtimes/codex-primary-runtime and registers its catalog with Codex.
# `codex_runtime.py review --write` writes this file from LATEST.json and the archive. Do not edit it by hand.
"""


def url_problem(url: str, version: str) -> str | None:
    if not url.startswith(f'{ORIGIN}{version}/') or not url.endswith('.tar.xz'):
        return f'expected a .tar.xz under {ORIGIN}{version}/'
    return None


@dataclass(frozen=True)
class RuntimePin:
    version: str                     # LATEST.json's bundleVersion, e.g. '26.1025.11240'
    url: str                         # its archiveUrl, versioned and immutable
    archive: Digest                  # sha256 of the .tar.xz
    archive_bytes: int
    tree: Digest                     # digest(manifest) of the unpacked bundle
    tree_bytes: int                  # the sum of its file sizes, for the free-space check
    plugins: tuple[str, ...]         # the catalog's plugin names, sorted

    @property
    def platform(self) -> Platform:
        """The one platform this pin is for. Setup.for_platform keeps the pin only there."""
        return Platform.LINUX

    @classmethod
    def read(cls, path: Path, errors: list[str]) -> RuntimePin | None:
        """None when the file is absent. Each problem goes to errors as 'path:key: message'."""
        if not path.is_file():
            return None
        try:
            data = tomllib.loads(path.read_bytes().decode('utf-8'))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            errors.append(f'{path}:: {error}')
            return None
        before = len(errors)

        def problem(key, message):
            errors.append(f'{path}:{key}: {message}')

        def get(key, kind):
            value = data.get(key)
            if not isinstance(value, kind) or isinstance(value, bool):
                problem(key, 'missing' if key not in data else f'expected {kind.__name__}')
                return None
            return value

        if data.get('public') is not True:
            problem('public', 'a public file must say public = true')
        for key in sorted(data.keys() - {'public', 'version', 'url', 'archive', 'archive_bytes', 'tree', 'tree_bytes',
                                         'plugins'}):
            problem(key, 'unknown key')
        version, url = get('version', str), get('url', str)
        if version is not None and not VERSION.fullmatch(version):
            problem('version', f'not a bundle version: {version!r}')
        if url is not None and version is not None and (message := url_problem(url, version)):
            problem('url', message)
        digests = {}
        for key in ('archive', 'tree'):
            if (text := get(key, str)) is not None:
                try:
                    digests[key] = parse_digest(text)
                except ValueError as error:
                    problem(key, str(error))
        sizes = {key: get(key, int) for key in ('archive_bytes', 'tree_bytes')}
        for key, size in sizes.items():
            if size is not None and size <= 0:
                problem(key, 'expected a positive size')
        plugins = get('plugins', list)
        if plugins is not None and (not all(isinstance(name, str) and name for name in plugins)
                                    or list(plugins) != sorted(set(plugins))):
            problem('plugins', 'expected sorted, distinct plugin names')
        if len(errors) > before:
            return None
        return cls(version, url, digests['archive'], sizes['archive_bytes'], digests['tree'], sizes['tree_bytes'],
                   tuple(plugins))

    def render(self) -> str:
        """The file review --write writes. read() of it gives this pin back."""
        values = {'public': True, 'version': self.version, 'url': self.url, 'archive': self.archive,
                  'archive_bytes': self.archive_bytes, 'tree': self.tree, 'tree_bytes': self.tree_bytes,
                  'plugins': list(self.plugins)}
        return _HEADER + ''.join(f'{key} = {json.dumps(value)}\n' for key, value in values.items())
