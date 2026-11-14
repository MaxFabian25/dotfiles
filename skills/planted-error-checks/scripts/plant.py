"""Plant one defect at a time in a copy of a source file and run its test file on each copy.

Usage: python3 -I plant.py SOURCE TEST PLANTS_JSON [ROOT] [-- TEST_ARGUMENT...]

PLANTS_JSON holds a list of [label, old, new]: each plant replaces the one place where `old` occurs in SOURCE
with `new`. ROOT is the folder copied for each run, which must hold SOURCE, TEST and every file TEST reads; it
defaults to SOURCE's folder. TEST runs from its own folder in the copy as `python -I -B TEST`, so it must find
SOURCE by a path relative to itself. It runs with NO_COLOR set and PYTHON_COLORS left out, so its headings stay
plain. Everything after `--` is passed to TEST as its arguments. In each of them, `{fresh}` becomes the path of a
folder that does not exist yet, a new one for each run, for a TEST that writes its results to a folder it must
create itself. The folder goes when its run ends.

TEST runs at low priority where the system allows it. On stderr, the script warns at start when the machine's
1-minute load is above its number of CPUs, and once the unchanged copy has passed it gives that run's wall and CPU
time multiplied by the number of plants, an estimate of the whole run. The run goes on either way.

Prints CAUGHT, MISSED, TIMED OUT or NOT APPLIED for each plant, after the unchanged copy has passed. A CAUGHT
line names the tests that failed. In a run that holds unittest's `Ran N tests` line, those are its `FAIL: name`
and `ERROR: name` headings. In any other run, they are the lines that a TEST of its own making prints as `FAIL`,
two spaces and the check's name, which ends at the next two spaces. It gives the run's last line when the run
names none, as a plant that breaks the import does. Check that those are the tests of the planted behaviour.
Exits 0 when every plant was caught, 1 when one was not, and 2, with the reason on stderr, when the unchanged
copy fails or the arguments are wrong.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import resource
except ImportError:  # Windows, which has no CPU time of finished child processes
    resource = None

LIMIT = 1800  # seconds for one run of TEST
FAILED = re.compile(r'^(?:FAIL|ERROR): (\S+)', re.MULTILINE)  # unittest's heading of a failed test
RAN = re.compile(r'^Ran \d+ tests? in ', re.MULTILINE)  # unittest's last line but one, which only its runs hold
# A failed check of a TEST that prints its own lines, such as "FAIL  totals match  expected 3, got 4"
FAILED_CHECK = re.compile(r'^FAIL  (\S.*?)(?:  |$)', re.MULTILINE)
FRESH = '{fresh}'  # in a TEST argument, the path of a folder that does not exist yet
TAIL = 40  # lines of a failed unchanged run that stderr shows
NAMED = 3  # failed tests a CAUGHT line names


def run(root, source, test, text, arguments=()):
    """The exit status and output of TEST in a fresh copy of ROOT whose SOURCE holds text, or None and the limit
    when it ran past it. TEST gets the arguments, with FRESH as a path beside the copy that holds nothing yet."""
    with tempfile.TemporaryDirectory() as folder:
        copy = Path(folder) / root.name
        shutil.copytree(root, copy, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '.git', '.venv'))
        (copy / source.relative_to(root)).write_bytes(text.encode('utf-8'))
        # Without colour, because with FORCE_COLOR set unittest colours the headings that FAILED reads. TEST runs
        # with -I, but the Python runs it starts read PYTHON_COLORS before NO_COLOR.
        environment = {name: value for name, value in os.environ.items()
                       if name != 'PYTHON_COLORS'} | {'NO_COLOR': '1'}
        try:
            passed = [argument.replace(FRESH, str(Path(folder) / f'{root.name}.fresh')) for argument in arguments]
            done = subprocess.run([sys.executable, '-I', '-B', test.name, *passed], cwd=copy / test.parent.relative_to(root),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=LIMIT, env=environment)
        except subprocess.TimeoutExpired:
            return None, f'ran past {LIMIT} s'
        return done.returncode, done.stdout.decode(errors='replace')


def failures(output):
    """What a CAUGHT line says failed: the tests the run names, or the run's last line."""
    names = list(dict.fromkeys((FAILED if RAN.search(output) else FAILED_CHECK).findall(output)))
    if names:
        more = f' and {len(names) - NAMED} more' if len(names) > NAMED else ''
        return f'{", ".join(names[:NAMED])}{more} failed'
    last = output.strip().splitlines()[-1:] or ['no output']
    return f'no test named, the run ended with: {last[0].strip()}'


def children_cpu():
    """The CPU seconds of this process's finished child processes, or None where the system counts none."""
    if resource is None:
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def overload():
    """A warning when the 1-minute load is above the number of CPUs, or None, also when either cannot be read."""
    try:
        load, cpus = os.getloadavg()[0], os.cpu_count()
    except (AttributeError, OSError):  # Windows has no load average, and a Unix may fail to give one
        return None
    if cpus is None or load <= cpus:
        return None
    return (f'The 1-minute load is {load:.1f} on {cpus} CPUs, so each plant runs slowly and slows the other work here. '
            'Run this on the Omarchy laptop with omarchy-run instead, or under nice -n 19 with fewer workers beside it.')


def applies(original, old, new):
    return original.count(old) == 1 and old != new


def estimate(wall, cpu, count, listed):
    """The unchanged run's wall and CPU seconds, None where unknown, scaled to the count of plants that apply out of
    the listed ones. Each figure is rounded before it is multiplied, so the printed sums add up."""
    wall, cpu = round(wall, 1), None if cpu is None else round(cpu, 1)
    return (f'The unchanged copy took {wall:.1f} s, so {count} plants should take about {wall * count:.1f} s'
            + (f' ({listed - count} more will not apply)' if listed > count else '')
            + ('. This system counts no CPU time of child processes.' if cpu is None
               else f'. It used {cpu:.1f} CPU seconds, so they should use about {cpu * count:.1f}.'))


def load(path):
    """The plants in a JSON file, or a stop that says what is wrong with it."""
    try:
        plants = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise SystemExit(f'{path}: {error}') from None
    if (not isinstance(plants, list) or not plants
            or not all(isinstance(plant, list) and len(plant) == 3 and all(isinstance(part, str) for part in plant)
                       for plant in plants)):
        raise SystemExit(f'{path} must hold a list of at least one [label, old, new], each of three strings')
    return plants


def main(argv):
    arguments = []
    if '--' in argv:
        argv, arguments = argv[:argv.index('--')], argv[argv.index('--') + 1:]
    if len(argv) not in (3, 4):
        print(__doc__, file=sys.stderr)
        return 2
    source, test = Path(argv[0]).resolve(), Path(argv[1]).resolve()
    root = Path(argv[3]).resolve() if len(argv) == 4 else source.parent
    for path in (source, test):
        if not path.is_file() or not path.is_relative_to(root):
            print(f'{path} is no file under {root}', file=sys.stderr)
            return 2
    try:
        plants = load(Path(argv[2]))
    except SystemExit as stop:
        print(stop, file=sys.stderr)
        return 2
    if hasattr(os, 'nice'):  # Windows has none
        os.nice(10)
    if warning := overload():
        print(warning, file=sys.stderr, flush=True)
    try:
        original = source.read_bytes().decode('utf-8')
        began, cpu = time.perf_counter(), children_cpu()
        status, output = run(root, source, test, original, arguments)
        wall = time.perf_counter() - began
    except (OSError, UnicodeDecodeError) as error:  # a SOURCE that is not UTF-8, or a ROOT that cannot be copied
        print(f'no copy of {root} could run with {source}: {error}', file=sys.stderr)
        return 2
    if status != 0:
        print('the unchanged copy fails, so a failing plant would prove nothing. Its run '
              + (output if status is None else f'exited {status} and ended with:\n'
                 + '\n'.join(output.splitlines()[-TAIL:])), file=sys.stderr)
        return 2
    print(estimate(wall, None if cpu is None else children_cpu() - cpu,
                   sum(applies(original, old, new) for _, old, new in plants), len(plants)), file=sys.stderr, flush=True)
    caught = 0
    for label, old, new in plants:
        if not applies(original, old, new):
            print(f'NOT APPLIED {label}: the text to replace occurs {original.count(old)} times'
                  + (' and the plant changes nothing' if old == new else ''), flush=True)
            continue
        status, output = run(root, source, test, original.replace(old, new), arguments)
        if status is None:
            print(f'TIMED OUT {label}: the test {output}, which shows no test of it', flush=True)
        elif status == 0:
            print(f'MISSED {label}', flush=True)
        else:
            caught += 1
            print(f'CAUGHT {label}: {failures(output)}', flush=True)
    print(f'{caught} of {len(plants)} plants caught')
    return 0 if caught == len(plants) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
