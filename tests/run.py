"""Run the public test suite as CI and release.py do: without writing bytecode, and with Codex's and Claude's config
roots at a missing folder, because agent sessions export CODEX_HOME and CLAUDE_CONFIG_DIR at the device's real
config. Other arguments go to unittest's discover, so -p 'test_sync.py' runs one module and -k NAME the tests whose
name contains NAME. From the public checkout, or a release worktree's public folder:

    uv run --locked python -B tests/run.py [-p PATTERN] [-k NAME] [-v]
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def main(argv):
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix='dotfiles-tests-') as folder:
        absent = Path(folder) / 'absent-config'
        os.environ['CODEX_HOME'] = str(absent / 'codex')
        os.environ['CLAUDE_CONFIG_DIR'] = str(absent / 'claude')
        os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
        program = unittest.main(module=None, argv=['tests/run.py', 'discover', '-s', str(TESTS), *argv], exit=False)
    if not program.result.testsRun:
        return 5
    return 0 if program.result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
