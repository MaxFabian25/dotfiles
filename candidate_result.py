"""What `sync.py --candidate --json` tells `release.py` about whether this device's pull could apply a release.

The release.py that reads a result is the canonical checkout's, so it is at least one release older than the sync.py
that wrote it whenever a release changes the result. The reader ignores a key it does not know, so adding a key takes
one release. Removing or renaming a key takes two: first stop reading it, then, once every machine that finishes
releases has pulled the first, stop writing it."""
from __future__ import annotations

from dataclasses import dataclass
import json


@dataclass(frozen=True)
class CandidateResult:
    blocking: int


def dump(result):
    # A release.py from before plugins got folders of their own requires the list of held Claude caches, which is
    # always empty now. Drop the key once every machine that finishes releases runs a release.py that ignores it.
    return json.dumps({'blocking': result.blocking, 'held': []})


def read(text):
    """The result `dump` wrote. A ValueError names what is malformed."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f'the candidate result is not JSON: {error.msg}, line {error.lineno}') from None
    _fields(data, 'the candidate result', {'blocking': int})
    return CandidateResult(data['blocking'])


def _fields(value, where, kinds):
    if type(value) is not dict:
        raise ValueError(f'{where} is of type {type(value).__name__}, not an object')
    if missing := sorted(kinds.keys() - value.keys()):
        raise ValueError(f'{where} has no {", ".join(missing)}')
    for key, kind in kinds.items():
        if type(value[key]) is not kind:  # also refuses a bool, which isinstance counts as an int
            raise ValueError(f'{where} has {key} {value[key]!r}, which is not of type {kind.__name__}')
