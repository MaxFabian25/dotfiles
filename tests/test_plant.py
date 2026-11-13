"""Tests for skills/planted-error-checks/scripts/plant.py."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parent.parent / 'skills/planted-error-checks/scripts/plant.py'
SOURCE = 'def double(number):\n    return number * 2\n\n\ndef label(number):\n    return f"n={number}"\n'
# Under -I Python leaves the script's folder off sys.path, so the test names its source by its own place.
TEST = ('from pathlib import Path\nimport sys\nimport unittest\nsys.path.insert(0, str(Path(__file__).parent))\n'
        'from value import double\n\n\nclass Value(unittest.TestCase):\n'
        '    def test_double(self):\n        self.assertEqual(double(3), 6)\n\n\nunittest.main()\n')

# A test of its own making: it takes a folder that must not exist yet, and prints a PASS or FAIL line per check.
CHECKS = ('from pathlib import Path\nimport sys\nsys.path.insert(0, str(Path(__file__).parent))\n'
          'from value import double, label\n\nevidence = Path(sys.argv[1])\nevidence.mkdir()\n'
          'print("ERROR: the fixture was slow to answer")  # a line of its own, not a failed test\n'
          'checks = [("doubles a number", double(3) == 6, f"got {double(3)}"), ("labels", label(1) == "n=1", ""),\n'
          '          ("takes its second argument", sys.argv[2:] == ["--strict"], str(sys.argv[2:])),\n'
          '          ("its folder is outside the copy", not evidence.resolve().is_relative_to(Path.cwd()), "")]\n'
          'for name, passed, detail in checks:\n'
          '    print(f"{\'PASS\' if passed else \'FAIL\'}  {name}" + ("" if passed else f"  {detail}"))\n'
          'sys.exit(not all(passed for _, passed, _ in checks))\n')


class Plant(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / 'project'
        (self.root / 'code').mkdir(parents=True)
        (self.root / 'code/value.py').write_bytes(SOURCE.encode())
        (self.root / 'code/test_value.py').write_text(TEST, encoding='utf-8')

    def plant(self, plants, *root, test='test_value.py'):
        """Run the script on a plants file that holds this list, this text or, for None, on a missing file."""
        file = self.root.parent / 'plants.json'
        file.unlink(missing_ok=True)
        if plants is not None:
            file.write_text(plants if isinstance(plants, str) else json.dumps(plants), encoding='utf-8')
        done = subprocess.run([sys.executable, '-I', '-B', str(SCRIPT), str(self.root / 'code/value.py'),
                               str(self.root / 'code' / test), str(self.root.parent / 'plants.json'), *root],
                              capture_output=True, text=True, timeout=300)
        return done.returncode, done.stdout.splitlines(), done.stderr

    def test_each_plant_is_caught_missed_or_not_applied_and_the_source_stays_as_it_was(self):
        tripled, relabelled = ['triples', 'number * 2', 'number * 3'], ['relabels', 'n={number}', 'N={number}']
        status, lines, _ = self.plant([tripled, relabelled, ['absent', 'number * 4', 'x'], ['twice', 'number', 'x'],
                                       ['same', 'number * 2', 'number * 2']])
        self.assertEqual(status, 1)
        self.assertEqual(lines, ['CAUGHT triples: test_double failed', 'MISSED relabels',
                                 'NOT APPLIED absent: the text to replace occurs 0 times',
                                 'NOT APPLIED twice: the text to replace occurs 4 times',
                                 'NOT APPLIED same: the text to replace occurs 1 times and the plant changes nothing',
                                 '1 of 5 plants caught'])
        self.assertEqual((self.root / 'code/value.py').read_text(encoding='utf-8'), SOURCE)
        self.assertEqual(self.plant([tripled], str(self.root))[:2],
                         (0, ['CAUGHT triples: test_double failed', '1 of 1 plants caught']))
        self.assertEqual(self.plant([relabelled])[0], 1)

    def test_a_test_gets_the_arguments_after_two_dashes_with_a_fresh_folder_for_each_run(self):
        (self.root / 'code/test_checks.py').write_text(CHECKS, encoding='utf-8')
        both = ['relabels and triples', 'return number * 2\n\n\ndef label(number):\n    return f"n={number}"',
                'return number * 3\n\n\ndef label(number):\n    return f"N={number}"']
        plants = [['triples', 'number * 2', 'number * 3'], both]
        status, lines, error = self.plant(plants, '--', '{fresh}', '--strict', test='test_checks.py')
        self.assertEqual((status, lines), (0, ['CAUGHT triples: doubles a number failed',
                                               'CAUGHT relabels and triples: doubles a number, labels failed',
                                               '2 of 2 plants caught']), error)
        status, lines, error = self.plant(plants[:1], test='test_checks.py')  # without the arguments it needs
        self.assertEqual((status, lines), (2, []))
        self.assertIn('IndexError', error)
        self.root = self.root.rename(self.root.parent / 'fresh')  # a ROOT of that name is still not the fresh folder
        self.assertEqual(self.plant(plants[:1], str(self.root), '--', '{fresh}', '--strict', test='test_checks.py')[:2],
                         (0, ['CAUGHT triples: doubles a number failed', '1 of 1 plants caught']))

    def test_a_unittest_run_that_also_prints_a_fail_line_names_only_its_failed_tests(self):
        printing = TEST.replace('        self.assert', '        print("FAIL  printed by the test")\n        self.assert')
        # A second test, so unittest writes "Ran 2 tests" where the other tests' runs end "Ran 1 test".
        printing = printing.replace('\n\n\nunittest.main()',
                                    '\n\n    def test_runs(self):\n        pass\n\n\nunittest.main()')
        self.assertIn('def test_runs', printing)
        (self.root / 'code/test_value.py').write_text(printing, encoding='utf-8')
        self.assertEqual(self.plant([['triples', 'number * 2', 'number * 3']])[:2],
                         (0, ['CAUGHT triples: test_double failed', '1 of 1 plants caught']))

    def test_a_caught_plant_names_its_failed_test_in_a_session_that_forces_colour(self):
        # The test file runs with -I, which ignores PYTHON_COLORS, but the Python run it starts does not use -I.
        child = ('        child = subprocess.run([sys.executable, "-c", "1/0"], capture_output=True, text=True)\n'
                 '        self.assertNotIn("\\x1b[", child.stderr)\n        self.assert')
        (self.root / 'code/test_value.py').write_text('import subprocess\n' + TEST.replace('        self.assert', child),
                                                     encoding='utf-8')
        # Python reads NO_COLOR before FORCE_COLOR, and tests/run.py sets it for this module.
        with patch.dict(os.environ, {'FORCE_COLOR': '1', 'PYTHON_COLORS': '1'}):
            os.environ.pop('NO_COLOR', None)
            status, lines, error = self.plant([['triples', 'number * 2', 'number * 3']])
        self.assertEqual((status, lines), (0, ['CAUGHT triples: test_double failed', '1 of 1 plants caught']), error)

    def test_a_caught_plant_that_fails_no_named_test_says_how_the_run_ended(self):
        status, lines, _ = self.plant([['breaks the syntax', 'def double(number):', 'def double(number:']])
        self.assertEqual(status, 0)
        self.assertRegex(lines[0], r'^CAUGHT breaks the syntax: no test named, the run ended with: SyntaxError: ')

    def test_a_plant_whose_test_runs_past_the_limit_is_not_caught_and_the_next_plant_still_runs(self):
        hangs = ['hangs', 'return number * 2', 'import time\n    time.sleep(60)']
        (self.root.parent / 'plants.json').write_text(json.dumps([hangs, ['triples', 'number * 2\n', 'number * 3\n']]),
                                                      encoding='utf-8')
        arguments = [str(self.root / 'code/value.py'), str(self.root / 'code/test_value.py'),
                     str(self.root.parent / 'plants.json')]
        short = (f'import runpy, sys; main = runpy.run_path({str(SCRIPT)!r})["main"]; '
                 f'main.__globals__["LIMIT"] = 5; sys.exit(main({arguments!r}))')
        done = subprocess.run([sys.executable, '-I', '-B', '-c', short], capture_output=True, text=True, timeout=300)
        self.assertEqual((done.returncode, done.stdout.splitlines()), (1, [
            'TIMED OUT hangs: the test ran past 5 s, which shows no test of it', 'CAUGHT triples: test_double failed',
            '1 of 2 plants caught']), done.stderr)

    def test_a_failing_unchanged_copy_or_a_file_outside_the_root_stops_before_any_plant(self):
        tripled = ['triples', 'number * 5', 'number * 3']
        status, lines, error = self.plant([tripled], str(self.root / 'code/missing'))
        self.assertEqual((status, lines), (2, []))
        self.assertIn('is no file under', error)
        (self.root / 'code/value.py').write_text(SOURCE.replace('* 2', '* 5'), encoding='utf-8')
        status, lines, error = self.plant([tripled])
        self.assertEqual((status, lines), (2, []))
        self.assertIn('the unchanged copy fails', error)
        self.assertIn('exited 1 and ended with:', error)
        self.assertIn('AssertionError: 15 != 6', error)  # the reason, which the caller would otherwise have to rerun for

    def test_a_source_that_is_not_utf8_or_a_root_that_cannot_be_copied_stops_with_the_reason(self):
        tripled = ['triples', 'number * 2', 'number * 3']
        with self.subTest('a root that holds a named pipe'):
            if sys.platform == 'win32':
                self.skipTest('Windows has no named pipes in a folder')
            os.mkfifo(self.root / 'code/pipe')
            status, lines, error = self.plant([tripled])
            self.assertEqual((status, lines), (2, []))
            self.assertRegex(error, r'^no copy of .*/code could run with .*value\.py: .*pipe')
            (self.root / 'code/pipe').unlink()
        (self.root / 'code/value.py').write_bytes(b'# caf\xe9\n' + SOURCE.encode())
        status, lines, error = self.plant([tripled])
        self.assertEqual((status, lines), (2, []))
        self.assertRegex(error, r"^no copy of .* could run with .*value\.py: 'utf-8' codec can't decode")

    def test_a_plants_file_that_is_missing_malformed_or_empty_stops_with_the_reason(self):
        for plants, reason in (([], 'must hold a list of at least one [label, old, new]'),
                               ([['two', 'parts']], 'must hold a list'), ([['label', 'old', 3]], 'must hold a list'),
                               ({'label': ['old', 'new']}, 'must hold a list')):
            with self.subTest(plants=plants):
                status, lines, error = self.plant(plants)
                self.assertEqual((status, lines), (2, []))
                self.assertIn(reason, error)
                self.assertNotIn('Traceback', error)
        for text in (None, '[["label", "old"'):
            with self.subTest(text=text):
                status, lines, error = self.plant(text)
                self.assertEqual((status, lines), (2, []))
                self.assertIn('plants.json: ', error)
                self.assertNotIn('Traceback', error)


if __name__ == '__main__':
    unittest.main()
