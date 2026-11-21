"""Tests for skills/planted-error-checks/scripts/plant.py."""

import json
import os
from pathlib import Path
import re
import shutil
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
# Takes at least 0.7 s, 0.2 s of it on the CPU, however busy the machine is.
SLOW = 'import time\n' + TEST.replace('    def test_double(self):\n', '    def test_double(self):\n        time.sleep(0.5)\n'
                                      '        began = time.process_time()\n'
                                      '        while time.process_time() - began < 0.2:\n            pass\n')
ESTIMATE = re.compile(r'The unchanged copy took (\d+\.\d) s, so (\d+) plants should take about (\d+\.\d) s'
                      r' \(1 more will not apply\)\. It used (\d+\.\d) CPU seconds, so they should use about (\d+\.\d)\.')
QUIET = 'import os\nos.getloadavg = lambda: (0.5, 0.5, 0.5)\n'
TRIPLED, RELABELLED = ['triples', 'number * 2', 'number * 3'], ['relabels', 'n={number}', 'N={number}']
ABSENT = [['absent', 'number * 4', 'x'], ['also absent', 'number * 5', 'x'], ['still absent', 'number * 6', 'x']]


class Plant(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / 'project'
        (self.root / 'code').mkdir(parents=True)
        (self.root / 'code/value.py').write_bytes(SOURCE.encode())
        (self.root / 'code/test_value.py').write_text(TEST, encoding='utf-8')

    def plant(self, plants, *root, test='test_value.py', creationflags=0):
        """Run the script on a plants file that holds this list, this text or, for None, on a missing file."""
        file = self.root.parent / 'plants.json'
        file.unlink(missing_ok=True)
        if plants is not None:
            file.write_text(plants if isinstance(plants, str) else json.dumps(plants), encoding='utf-8')
        done = subprocess.run([sys.executable, '-I', '-B', str(SCRIPT), str(self.root / 'code/value.py'),
                               str(self.root / 'code' / test), str(self.root.parent / 'plants.json'), *root],
                              capture_output=True, text=True, timeout=300, creationflags=creationflags)
        return done.returncode, done.stdout.splitlines(), done.stderr

    def plant_after(self, prelude, plants, test='test_value.py'):
        """Run the script's main after prelude, Python that changes what the machine reports, with stdout and stderr
        in one stream in the order they were written."""
        (self.root.parent / 'plants.json').write_text(json.dumps(plants), encoding='utf-8')
        argv = [str(self.root / 'code/value.py'), str(self.root / 'code' / test), str(self.root.parent / 'plants.json')]
        program = f'{prelude}\nimport runpy, sys\nsys.exit(runpy.run_path({str(SCRIPT)!r})["main"]({argv!r}))\n'
        done = subprocess.run([sys.executable, '-I', '-B', '-c', program], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, timeout=300)
        return done.returncode, done.stdout.splitlines()

    @unittest.skipIf(sys.platform == 'win32', 'Windows has no resource module, so plant.py gives no CPU time')
    def test_before_the_first_plant_the_unchanged_runs_wall_and_cpu_time_are_scaled_to_every_plant(self):
        (self.root / 'code/test_value.py').write_text(SLOW, encoding='utf-8')
        status, lines = self.plant_after(QUIET, [TRIPLED, RELABELLED, ABSENT[0]])
        self.assertEqual(status, 1, lines)
        self.assertEqual([line.split(':')[0] for line in lines[1:]],
                         ['CAUGHT triples', 'MISSED relabels', 'NOT APPLIED absent', '1 of 3 plants caught'])
        match = ESTIMATE.fullmatch(lines[0])
        self.assertIsNotNone(match, lines[0])
        wall, count, all_wall, cpu, all_cpu = (float(figure) for figure in match.groups())
        self.assertEqual(count, 2)
        self.assertGreaterEqual(wall, 0.7)
        self.assertGreaterEqual(cpu, 0.2)
        self.assertLess(cpu, wall)
        self.assertAlmostEqual(all_wall, 2 * wall, delta=0.05)
        self.assertAlmostEqual(all_cpu, 2 * cpu, delta=0.05)

    def test_the_estimate_multiplies_the_figures_it_prints(self):
        program = (f'import runpy; print(runpy.run_path({str(SCRIPT)!r})["estimate"](0.26, 0.14, 2, 3)); '
                   f'print(runpy.run_path({str(SCRIPT)!r})["estimate"](0.26, None, 2, 2))')
        done = subprocess.run([sys.executable, '-I', '-B', '-c', program], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.stdout.splitlines(), [
            'The unchanged copy took 0.3 s, so 2 plants should take about 0.6 s (1 more will not apply). '
            'It used 0.1 CPU seconds, so they should use about 0.2.',
            'The unchanged copy took 0.3 s, so 2 plants should take about 0.6 s. '
            'This system counts no CPU time of child processes.'], done.stderr)

    def test_a_system_that_counts_no_child_cpu_time_still_gets_the_wall_time_estimate(self):
        (self.root / 'code/test_value.py').write_text(SLOW, encoding='utf-8')
        status, lines = self.plant_after(QUIET + 'import sys\nsys.modules["resource"] = None\n', [TRIPLED, RELABELLED])
        self.assertEqual(status, 1, lines)
        match = re.fullmatch(r'The unchanged copy took (\d+\.\d) s, so 2 plants should take about (\d+\.\d) s\. '
                             r'This system counts no CPU time of child processes\.', lines[0])
        self.assertIsNotNone(match, lines[0])
        self.assertGreaterEqual(float(match[1]), 0.7)
        self.assertAlmostEqual(float(match[2]), 2 * float(match[1]), delta=0.05)

    def test_a_load_above_the_cpu_count_warns_once_at_start_and_the_run_goes_on(self):
        tripled = [['triples', 'number * 2', 'number * 3']]
        status, lines = self.plant_after('import os\nos.getloadavg = lambda: (23.5, 9.0, 9.0)\nos.cpu_count = lambda: 22',
                                         tripled)
        self.assertEqual((status, lines[2:]), (0, ['CAUGHT triples: test_double failed', '1 of 1 plants caught']), lines)
        self.assertIn('The 1-minute load is 23.5 on 22 CPUs', lines[0])
        self.assertIn('omarchy-run', lines[0])
        self.assertIn('nice -n 19 with fewer workers', lines[0])
        self.assertRegex(lines[1], r'^The unchanged copy took ')
        for case, prelude in (
                ('a load at the cpu count', 'os.getloadavg = lambda: (22.0, 30.0, 30.0)\nos.cpu_count = lambda: 22'),
                ('no load average', 'del os.getloadavg' if hasattr(os, 'getloadavg') else ''),
                ('an unreadable load average', 'def unreadable():\n    raise OSError("no load")\n'
                                               'os.getloadavg = unreadable'),
                ('no cpu count', 'os.getloadavg = lambda: (99.0, 9.0, 9.0)\nos.cpu_count = lambda: None')):
            with self.subTest(case):
                status, lines = self.plant_after('import os\n' + prelude, tripled)
                self.assertEqual((status, lines[1:]), (0, ['CAUGHT triples: test_double failed', '1 of 1 plants caught']),
                                 lines)
                self.assertRegex(lines[0], r'^The unchanged copy took ')

    @unittest.skipUnless(hasattr(os, 'nice'), 'Windows has no nice')
    def test_the_test_runs_at_a_priority_ten_steps_lower_than_the_scripts_caller(self):
        expected = os.nice(0) + 10
        if expected > 19:
            self.skipTest('this process cannot drop ten more steps, so no change of priority can show')
        (self.root / 'code/test_priority.py').write_text('import os\nimport sys\nsys.exit(os.nice(0) != int(sys.argv[1]))\n',
                                                        encoding='utf-8')
        status, lines, error = self.plant(ABSENT[:1], '--', str(expected), test='test_priority.py')
        self.assertEqual((status, lines), (1, ['NOT APPLIED absent: the text to replace occurs 0 times',
                                               '0 of 1 plants caught']), error)

    @unittest.skipUnless(sys.platform == 'win32', 'only Windows has priority classes')
    def test_on_windows_the_test_runs_below_normal_priority(self):
        (self.root / 'code/test_priority.py').write_text(
            'import ctypes\nimport sys\nkernel = ctypes.windll.kernel32\n'
            'kernel.GetCurrentProcess.restype = ctypes.c_void_p\nkernel.GetPriorityClass.argtypes = [ctypes.c_void_p]\n'
            'seen = kernel.GetPriorityClass(kernel.GetCurrentProcess())\nprint(f"priority class {seen:#x}")\n'
            'sys.exit(seen != 0x4000)\n', encoding='utf-8')
        # A child of a below-normal process starts below normal too, as this test does inside a plant run.
        status, lines, error = self.plant(ABSENT[:1], test='test_priority.py',
                                          creationflags=subprocess.NORMAL_PRIORITY_CLASS)
        self.assertEqual((status, lines), (1, ['NOT APPLIED absent: the text to replace occurs 0 times',
                                               '0 of 1 plants caught']), error)

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

    def test_a_plant_that_crashes_the_checker_before_any_test_fails_is_crashed_and_not_caught(self):
        (self.root / 'code/test_checks.py').write_text(CHECKS, encoding='utf-8')
        breaks = ['breaks the syntax', 'def double(number):', 'def double(number:']
        for test, arguments, failed in (('test_value.py', (), 'test_double'),
                                        ('test_checks.py', ('--', '{fresh}', '--strict'), 'doubles a number')):
            with self.subTest(test):
                status, lines, error = self.plant([breaks, TRIPLED], *arguments, test=test)
                self.assertEqual((status, lines[1:]), (1, [f'CAUGHT triples: {failed} failed', '1 of 2 plants caught']),
                                 error)
                self.assertRegex(lines[0], r'^CRASHED breaks the syntax: no test failed, the run ended with: SyntaxError: ')

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
            self.assertRegex(error, r'(?m)^no copy of .*/code could run with .*value\.py: .*pipe')
            (self.root / 'code/pipe').unlink()
        (self.root / 'code/value.py').write_bytes(b'# caf\xe9\n' + SOURCE.encode())
        status, lines, error = self.plant([tripled])
        self.assertEqual((status, lines), (2, []))
        self.assertRegex(error, r"(?m)^no copy of .* could run with .*value\.py: 'utf-8' codec can't decode")

    @unittest.skipIf(shutil.which('git') is None, 'git is not installed')
    def test_copies_leave_out_git_unless_kept_and_a_linked_worktree_cannot_keep_it(self):
        listing = ('        listed = subprocess.run(["git", "ls-files", "value.py"], capture_output=True, text=True)\n'
                   '        self.assertEqual(listed.stdout, "value.py\\n", listed.stderr)\n        self.assert')
        (self.root / 'code/test_value.py').write_text('import subprocess\n' + TEST.replace('        self.assert', listing),
                                                     encoding='utf-8')
        for command in (['init', '-q'], ['add', '-A']):
            subprocess.run(['git', '-C', str(self.root), *command], check=True, capture_output=True)
        status, lines, error = self.plant([TRIPLED], str(self.root))
        self.assertEqual((status, lines), (2, []))
        self.assertIn('the unchanged copy fails', error)
        status, lines, error = self.plant([TRIPLED], str(self.root), '--keep-git')
        self.assertEqual((status, lines), (0, ['CAUGHT triples: test_double failed', '1 of 1 plants caught']), error)
        linked = self.root.parent / 'linked'
        shutil.copytree(self.root / 'code', linked / 'code')
        (linked / '.git').write_text(f'gitdir: {self.root / ".git"}\n', encoding='utf-8')
        self.root = linked
        status, lines, error = self.plant([TRIPLED], str(linked), '--keep-git')
        self.assertEqual((status, lines), (2, []))
        self.assertIn('is a file, as in a linked worktree', error)

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
