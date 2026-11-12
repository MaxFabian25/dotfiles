"""Plant one defect at a time in a copy of a source file and run its test file on each copy.

Usage: python3 -I plant.py SOURCE TEST PLANTS_JSON [ROOT]

PLANTS_JSON holds a list of [label, old, new]: each plant replaces the one place where `old` occurs in SOURCE
with `new`. ROOT is the folder copied for each run, which must hold SOURCE, TEST and every file TEST reads; it
defaults to SOURCE's folder. TEST runs from its own folder in the copy as `python -I -B TEST`, so it must find
SOURCE by a path relative to itself.

Prints CAUGHT, MISSED, TIMED OUT or NOT APPLIED for each plant, after the unchanged copy has passed. A CAUGHT
line names the tests that failed, or the run's last line when it names none, as a plant that breaks the import
does: check that those are the tests of the planted behaviour. Exits 0 when every plant was caught, 1 when one
was not, and 2, with the reason on stderr, when the unchanged copy fails or the arguments are wrong.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

LIMIT = 1800  # seconds for one run of TEST
FAILED = re.compile(r'^(?:FAIL|ERROR): (\S+)', re.MULTILINE)  # unittest's heading of a failed test
TAIL = 40  # lines of a failed unchanged run that stderr shows
NAMED = 3  # failed tests a CAUGHT line names


def run(root, source, test, text):
    """The exit status and output of TEST in a fresh copy of ROOT whose SOURCE holds text, or None and the limit
    when it ran past it."""
    with tempfile.TemporaryDirectory() as folder:
        copy = Path(folder) / root.name
        shutil.copytree(root, copy, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '.git', '.venv'))
        (copy / source.relative_to(root)).write_bytes(text.encode('utf-8'))
        try:
            done = subprocess.run([sys.executable, '-I', '-B', test.name], cwd=copy / test.parent.relative_to(root),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=LIMIT)
        except subprocess.TimeoutExpired:
            return None, f'ran past {LIMIT} s'
        return done.returncode, done.stdout.decode(errors='replace')


def failures(output):
    """What a CAUGHT line says failed: the tests unittest names, or the run's last line."""
    names = list(dict.fromkeys(FAILED.findall(output)))
    if names:
        more = f' and {len(names) - NAMED} more' if len(names) > NAMED else ''
        return f'{", ".join(names[:NAMED])}{more} failed'
    last = output.strip().splitlines()[-1:] or ['no output']
    return f'no test named, the run ended with: {last[0].strip()}'


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
    original = source.read_bytes().decode('utf-8')
    status, output = run(root, source, test, original)
    if status != 0:
        print('the unchanged copy fails, so a failing plant would prove nothing. Its run '
              + (output if status is None else f'exited {status} and ended with:\n'
                 + '\n'.join(output.splitlines()[-TAIL:])), file=sys.stderr)
        return 2
    caught = 0
    for label, old, new in plants:
        if original.count(old) != 1 or old == new:
            print(f'NOT APPLIED {label}: the text to replace occurs {original.count(old)} times'
                  + (' and the plant changes nothing' if old == new else ''), flush=True)
            continue
        status, output = run(root, source, test, original.replace(old, new))
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
