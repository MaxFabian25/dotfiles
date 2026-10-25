"""Check that Claude Code lists every skill on this device with its description.

Claude Code drops descriptions, least-used skills first, once its skill listing passes its share of the
context window (skillListingBudgetFraction), and agents find a skill listed by name alone far less often.
The startup event lists every name either way, so this reads one headless session's debug log instead.
Exit 0 when the listing fits, 1 when it is over budget, 2 when the log says neither.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import portable

OVER = re.compile(r'Skill listing over budget[^\n]*')
SENT = re.compile(r'Sending (\d+) skills via attachment')


def verdict(log: str) -> tuple[int, str]:
    if over := OVER.search(log):
        return 1, (f'Claude Code drops skill descriptions here ({over[0]}). Raise skillListingBudgetFraction in '
                   'the Claude settings source, or remove skills.')
    if sent := SENT.search(log):
        return 0, f'Claude Code lists all {sent[1]} skills with their descriptions.'
    return 2, 'The debug log names no skill listing; check that the claude CLI still writes these lines.'


def main() -> int:
    env = {key: value for key, value in os.environ.items() if not key.startswith(('CLAUDECODE', 'CLAUDE_CODE'))}
    with tempfile.TemporaryDirectory() as temp:
        log = Path(temp) / 'debug.log'
        subprocess.run([portable.claude_cli(), '-p', '--model', 'opus', '--permission-mode', 'bypassPermissions',
                        '--no-session-persistence', '--debug-file', str(log), 'Reply OK.'],
                       stdin=subprocess.DEVNULL, capture_output=True, cwd=temp, env=env, timeout=600)
        text = log.read_text(encoding='utf-8', errors='replace') if log.is_file() else ''
    code, message = verdict(text)
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
