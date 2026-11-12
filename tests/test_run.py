from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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
FAILS_AND_SKIPS = ONE_SKIP.replace('        pass', '        self.fail("the broken detail")', 1)
PRINTS_A_RESULT = ONE_TEST.replace('        pass', '        __import__("sys").stderr.write("OK (skipped=9)\\n")')
ENDS_WITH_A_NOTE = ONE_TEST.replace('import unittest\n', 'import atexit\nimport sys\nimport unittest\n\n'
                                    'atexit.register(print, "left OK (skipped=8)", file=sys.stderr)\n', 1)


def full_run(modules, runner=RUNNER):
    with tempfile.TemporaryDirectory() as folder:
        tests = Path(folder) / 'tests'
        tests.mkdir()
        (tests / 'run.py').write_text(runner, encoding='utf-8')
        for name, text in modules.items():
            (tests / name).write_text(text, encoding='utf-8')
        return subprocess.run([sys.executable, '-B', str(tests / 'run.py')], cwd=folder, capture_output=True, text=True,
                              encoding='utf-8', timeout=300)


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

    def test_a_failing_module_still_has_its_skips_counted(self):
        result = full_run({'test_a.py': FAILS_AND_SKIPS})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertRegex(result.stdout,
                         r'(?m)^Skipped tests: test_a\.py 1\nRan 2 tests in \S+\nFAILED \(test_a\.py\)\n\Z')

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

    def test_a_run_with_no_tests_exits_5(self):
        self.assertEqual(full_run({}).returncode, 5)


if __name__ == '__main__':
    unittest.main()
