"""Run the public test suite as CI and release.py do: without writing bytecode, and with Codex's and Claude's config
roots at a missing folder, because agent sessions export CODEX_HOME and CLAUDE_CONFIG_DIR at the device's real
config. With no arguments, each test module runs in its own process, up to 8 at a time, so no module can lean on what
another imported; the failing modules' output, each module's count of skips and a combined count follow. unittest
counts a class skipped as a whole once, whatever its number of tests. Any arguments go to unittest's discover in
one process, so -p 'test_sync.py' runs one module and -k NAME the tests whose name contains NAME. From the public
checkout, or a release worktree's public folder:

    uv run --locked python -B tests/run.py [-p PATTERN] [-k NAME] [-v]

Each run's temporary folders sit outside the system's, as TEMPORARY_PARENT says.
"""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

TESTS = Path(__file__).resolve().parent
RAN = re.compile(r'^Ran (\d+) tests? in', re.MULTILINE)
RESULT = re.compile(r'^(?:OK|FAILED)(?: \((.*)\))?$', re.MULTILINE)
SKIPPED = re.compile(r'\bskipped=(\d+)')
MODULE_TIMEOUT_SECONDS = 900
# Codex 0.169.0-alpha.6's Linux sandbox makes an empty .git, .agents, .codex and .aws at the top of each folder its
# command may write, /tmp among them, for as long as the command runs, and description_checks reads a .git in any
# parent of a root as damaged metadata. On 14 November 2026 a Codex session's commands on Omarchy gave a release's
# suite 11 failures and errors that way, so each run's temporary folders sit under the home folder instead. That
# holds while no Codex session works in the home folder itself or in its .cache.
TEMPORARY_PARENT = Path.home() / '.cache' / 'dotfiles-tests'
# A run stopped part-way leaves its folder there. A module stops at 15 minutes and release.py's whole suite at 30,
# so a run's folder untouched for a day is a stopped run's.
STALE_SECONDS = 24 * 60 * 60


def run_module(path):
    # Without colour, because with FORCE_COLOR set unittest colours the verdict that RESULT reads. Python reads
    # PYTHON_COLORS before NO_COLOR, and the -I runs that some tests start read only NO_COLOR.
    environment = {name: value for name, value in os.environ.items() if name != 'PYTHON_COLORS'} | {'NO_COLOR': '1'}
    try:
        return subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '-p', path.name], capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=MODULE_TIMEOUT_SECONDS,
                              env=environment, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(path.name, 1, '', f'{path.name} ran past the {MODULE_TIMEOUT_SECONDS} s limit')


def unittest_count(stderr):
    return int((RAN.findall(stderr) or ['0'])[-1])


def skipped_count(stderr):
    skipped = SKIPPED.search((RESULT.findall(stderr) or [''])[-1])
    return int(skipped[1]) if skipped else 0


def run_each_module():
    began = time.perf_counter()
    modules = sorted(TESTS.glob('test_*.py'))
    with ThreadPoolExecutor(min(8, os.cpu_count() or 1)) as pool:
        results = list(pool.map(run_module, modules))
    failed = [path.name for path, result in zip(modules, results) if result.returncode]
    for path, result in zip(modules, results):
        if result.returncode:
            print(f'== {path.name}', result.stdout, result.stderr, sep='\n')
    skipped = {path.name: count for path, result in zip(modules, results) if (count := skipped_count(result.stderr))}
    if skipped:
        print(f'Skipped tests: {", ".join(f"{name} {count}" for name, count in skipped.items())}')
    total = sum(unittest_count(result.stderr) for result in results)
    print(f'Ran {total} test{"s" * (total != 1)} in {time.perf_counter() - began:.3f}s')
    print(f'FAILED ({", ".join(failed)})' if failed else 'OK')
    return 1 if failed else 0 if total else 5


def main(argv):
    if not argv:
        return run_each_module()
    sys.dont_write_bytecode = True
    TEMPORARY_PARENT.mkdir(parents=True, exist_ok=True)
    for stale in TEMPORARY_PARENT.glob('dotfiles-tests-*'):
        try:
            if time.time() - stale.lstat().st_mtime > STALE_SECONDS:
                shutil.rmtree(stale, ignore_errors=True)
        except FileNotFoundError:  # another run removed it first
            pass
    with tempfile.TemporaryDirectory(prefix='dotfiles-tests-', dir=TEMPORARY_PARENT) as folder:
        os.environ['TMPDIR'] = folder
        absent = Path(folder) / 'absent-config'
        os.environ['CODEX_HOME'] = str(absent / 'codex')
        os.environ['CLAUDE_CONFIG_DIR'] = str(absent / 'claude')
        os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
        program = unittest.main(module=None, argv=['tests/run.py', 'discover', '-s', str(TESTS), *argv], exit=False)
    if not (program.result.testsRun or program.result.skipped):
        return 5
    return 0 if program.result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
