import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

RUNNER = Path(__file__).resolve().with_name('run.py').read_text(encoding='utf-8')
ONE_TEST = 'import unittest\n\n\nclass One(unittest.TestCase):\n    def test_passes(self):\n        pass\n'
TWO_TESTS = ONE_TEST + '\n    def test_prints_like_unittest(self):\n        print("Ran 99 tests in 0.000s")\n'
FAILING = ONE_TEST.replace('        pass', '        self.fail("the broken detail")')
INSTALLS_A_NAME = ONE_TEST.replace('import unittest\n', 'import sys\nimport unittest\n\nsys.modules["only_from_a"] = sys\n', 1)
NEEDS_THE_NAME = ONE_TEST.replace('import unittest\n', 'import unittest\n\nimport only_from_a\n', 1)
SLEEPS = ONE_TEST.replace('        pass', '        __import__("time").sleep(30)')
ONE_SKIP = ONE_TEST + '\n    @unittest.skip("not here")\n    def test_skipped(self):\n        pass\n'
CLASS_SKIP = ONE_TEST + ('\n\nclass Two(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n'
                         '        raise unittest.SkipTest("not on this machine")\n\n'
                         '    def test_first(self):\n        pass\n\n    def test_second(self):\n        pass\n')
MODULE_SKIP = ONE_TEST.replace('import unittest\n', 'import unittest\n\n\ndef setUpModule():\n'
                               '    raise unittest.SkipTest("not on this machine")\n', 1)
FAILS_AND_SKIPS = ONE_SKIP.replace('        pass', '        self.fail("the broken detail")', 1)
PRINTS_A_RESULT = ONE_TEST.replace('        pass', '        __import__("sys").stderr.write("OK (skipped=9)\\n")')
READS_STDIN = ONE_TEST.replace('import unittest\n', 'import sys\nimport threading\nimport unittest\n', 1).replace(
    '        pass', '        read = []\n'
    '        reader = threading.Thread(target=lambda: read.append(sys.stdin.read()), daemon=True)\n'
    '        reader.start()\n        reader.join(30)\n        self.assertEqual(read, [""])')
ENDS_WITH_A_NOTE = ONE_TEST.replace('import unittest\n', 'import atexit\nimport sys\nimport unittest\n\n'
                                    'atexit.register(print, "left OK (skipped=8)", file=sys.stderr)\n', 1)
# Checks that no parent of a temporary folder, made here or by a program this module starts, holds a .git.
FINDS_NO_GIT_ABOVE_ITS_TEMPORARY_FOLDERS = '''import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class Probe(unittest.TestCase):
    def test_no_parent_of_a_temporary_folder_holds_git_metadata(self):
        child = subprocess.run([sys.executable, '-c', 'import tempfile; print(tempfile.mkdtemp())'],
                               capture_output=True, text=True, check=True, timeout=60)
        for folder in Path(tempfile.mkdtemp()), Path(child.stdout.strip()):
            self.assertEqual([str(parent / '.git') for parent in folder.parents if os.path.lexists(parent / '.git')],
                             [], folder)
'''


def full_run(modules, runner=RUNNER, stdin=None, home=None):
    with tempfile.TemporaryDirectory() as folder:
        tests = Path(folder) / 'tests'
        tests.mkdir()
        (tests / 'run.py').write_text(runner, encoding='utf-8')
        for name, text in modules.items():
            (tests / name).write_text(text, encoding='utf-8')
        # The run's temporary folders sit under the home folder, and a module stopped at its time limit leaves its
        # folder there, so the run gets a home of its own. Path.home() reads USERPROFILE on Windows and HOME elsewhere.
        home = str(home or Path(folder) / 'home')
        return subprocess.run([sys.executable, '-B', str(tests / 'run.py')], cwd=folder, capture_output=True, text=True,
                              encoding='utf-8', timeout=300, stdin=stdin,
                              env=os.environ | {'HOME': home, 'USERPROFILE': home})


class FullRun(unittest.TestCase):
    def test_a_passing_run_reports_the_combined_count(self):
        for modules, count in (({'test_a.py': ONE_TEST, 'test_b.py': TWO_TESTS}, 'Ran 3 tests in '),
                               ({'test_a.py': ONE_TEST}, 'Ran 1 test in ')):
            with self.subTest(count):
                result = full_run(modules)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertRegex(result.stdout, rf'(?m)^{count}\d+\.\d{{3}}s\nOK\n\Z')
                self.assertNotIn('Skipped', result.stdout)

    def test_a_run_names_each_module_with_skips_and_its_count(self):
        result = full_run({'test_a.py': ONE_TEST, 'test_b.py': ONE_SKIP, 'test_c.py': CLASS_SKIP,
                           'test_d.py': PRINTS_A_RESULT, 'test_e.py': ENDS_WITH_A_NOTE})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertRegex(result.stdout, r'\ASkipped tests: test_b\.py 1, test_c\.py 1\nRan 6 tests in \S+\nOK\n\Z')

    def test_a_module_that_skips_itself_passes_and_is_counted(self):
        result = full_run({'test_a.py': ONE_TEST, 'test_b.py': MODULE_SKIP})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertRegex(result.stdout, r'\ASkipped tests: test_b\.py 1\nRan 1 test in \S+\nOK\n\Z')

    def test_a_failing_module_still_has_its_skips_counted(self):
        result = full_run({'test_a.py': FAILS_AND_SKIPS})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertRegex(result.stdout,
                         r'(?m)^Skipped tests: test_a\.py 1\nRan 2 tests in \S+\nFAILED \(test_a\.py\)\n\Z')

    def test_a_run_counts_skips_in_a_session_that_forces_colour(self):
        with patch.dict(os.environ, {'FORCE_COLOR': '1', 'PYTHON_COLORS': '1'}):
            os.environ.pop('NO_COLOR', None)
            result = full_run({'test_a.py': ONE_SKIP})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertRegex(result.stdout, r'\ASkipped tests: test_a\.py 1\nRan 2 tests in \S+\nOK\n\Z')

    def test_a_failing_module_fails_the_run_and_shows_its_output(self):
        result = full_run({'test_a.py': ONE_TEST, 'test_b.py': FAILING})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('== test_b.py', result.stdout)
        self.assertIn('the broken detail', result.stdout)
        self.assertNotIn('== test_a.py', result.stdout)
        self.assertRegex(result.stdout, r'(?m)^Ran 2 tests in \d+\.\d{3}s\nFAILED \(test_b\.py\)\n\Z')

    def test_a_module_cannot_lean_on_what_an_earlier_module_imported(self):
        result = full_run({'test_a_installs.py': INSTALLS_A_NAME, 'test_b_needs.py': NEEDS_THE_NAME})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('only_from_a', result.stdout)
        self.assertRegex(result.stdout, r'FAILED \(test_b_needs\.py\)\n\Z')

    def test_a_module_past_the_time_limit_fails_the_run_and_is_named(self):
        runner = RUNNER.replace('MODULE_TIMEOUT_SECONDS = 900', 'MODULE_TIMEOUT_SECONDS = 1')
        self.assertNotEqual(runner, RUNNER)
        result = full_run({'test_slow.py': SLEEPS}, runner)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('test_slow.py ran past the 1 s limit', result.stdout)
        self.assertRegex(result.stdout, r'FAILED \(test_slow\.py\)\n\Z')

    def test_a_module_reads_an_ended_stdin_while_the_runner_keeps_its_own_open(self):
        read, write = os.pipe()
        try:
            result = full_run({'test_a.py': READS_STDIN, 'test_b.py': READS_STDIN}, stdin=read)
        finally:
            os.close(read)
            os.close(write)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_a_run_with_no_tests_exits_5(self):
        self.assertEqual(full_run({}).returncode, 5)

    def test_a_git_folder_in_the_system_temporary_folder_reaches_no_module(self):
        # Codex's Linux sandbox leaves such a folder in /tmp while each of its commands runs.
        with tempfile.TemporaryDirectory() as folder:
            system = Path(folder) / 'tmp'
            (system / '.git').mkdir(parents=True)
            with patch.dict(os.environ, {'TMPDIR': str(system), 'TEMP': str(system), 'TMP': str(system)}):
                result = full_run({'test_a.py': FINDS_NO_GIT_ABOVE_ITS_TEMPORARY_FOLDERS}, home=Path(folder) / 'home')
            self.assertEqual(result.returncode, 0, result.stdout)
            # The run made its folders in the home it was given, so it read that home rather than the real one.
            self.assertTrue((Path(folder) / 'home' / '.cache' / 'dotfiles-tests').is_dir())

    def test_a_run_removes_the_folders_of_runs_stopped_over_a_day_ago(self):
        with tempfile.TemporaryDirectory() as home:
            parent = Path(home) / '.cache' / 'dotfiles-tests'
            old, fresh = parent / 'dotfiles-tests-old', parent / 'dotfiles-tests-fresh'
            for folder in old, fresh:
                (folder / 'tmpleft').mkdir(parents=True)
            day_ago = time.time() - 24 * 60 * 60 - 60
            os.utime(old, (day_ago, day_ago))
            almost = time.time() - 23 * 60 * 60
            os.utime(fresh, (almost, almost))
            result = full_run({'test_a.py': ONE_TEST}, home=home)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertFalse(old.exists())
            self.assertTrue((fresh / 'tmpleft').is_dir())

    def test_a_run_goes_on_when_another_run_removed_a_folder_it_listed(self):
        runner = RUNNER.replace("TEMPORARY_PARENT.glob('dotfiles-tests-*')",
                                "[*TEMPORARY_PARENT.glob('dotfiles-tests-*'), TEMPORARY_PARENT / 'dotfiles-tests-gone']")
        self.assertNotEqual(runner, RUNNER)
        result = full_run({'test_a.py': ONE_TEST}, runner)
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == '__main__':
    unittest.main()
