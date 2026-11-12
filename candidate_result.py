"""What `sync.py --candidate --json` tells `release.py` about whether this device's pull could apply a release.

The release.py that reads a result is one release older than the sync.py that wrote it, whenever a release changes
the result. The reader ignores a key it does not know, so adding a key takes one release. Removing or renaming a
key takes two: first stop reading it, then stop writing it."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class HeldCache:
    """A Claude plugin cache the pull would replace in place while running Claude processes hold it."""
    identity: str
    cache: Path
    holders: tuple[int, ...]

    @property
    def lease(self):
        return (f'Claude lease: {self.identity} at {self.cache}, held by running Claude processes '
                f'{", ".join(map(str, self.holders))}')


@dataclass(frozen=True)
class CandidateResult:
    blocking: int
    held: tuple[HeldCache, ...] = ()


def dump(result):
    return json.dumps({'blocking': result.blocking,
                       'held': [{'identity': held.identity, 'cache': str(held.cache), 'holders': list(held.holders)}
                                for held in result.held]})


def read(text):
    """The result `dump` wrote. A ValueError names what is malformed."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f'the candidate result is not JSON: {error.msg} at line {error.lineno}') from None
    _fields(data, 'the candidate result', {'blocking': int, 'held': list})
    held = []
    for number, entry in enumerate(data['held']):
        where = f'held cache {number}'
        _fields(entry, where, {'identity': str, 'cache': str, 'holders': list})
        if any(type(pid) is not int for pid in entry['holders']):
            raise ValueError(f'{where} has holders that are not all process ids: {entry["holders"]!r}')
        held.append(HeldCache(entry['identity'], Path(entry['cache']), tuple(entry['holders'])))
    return CandidateResult(data['blocking'], tuple(held))


def _fields(value, where, kinds):
    if type(value) is not dict:
        raise ValueError(f'{where} is of type {type(value).__name__}, not an object')
    if missing := sorted(kinds.keys() - value.keys()):
        raise ValueError(f'{where} has no {", ".join(missing)}')
    for key, kind in kinds.items():
        if type(value[key]) is not kind:  # also refuses a bool, which isinstance counts as an int
            raise ValueError(f'{where} has {key} {value[key]!r}, which is not of type {kind.__name__}')
