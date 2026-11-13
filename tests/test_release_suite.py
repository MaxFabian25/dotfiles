from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402
from release_case import (FAILING_TEST, PASSING_TEST, RUN_ALONE, ReleaseCase, SEEDED_TESTS, git,
                          release_main)  # noqa: E402
from release_case import skip_on_windows as setUpModule  # noqa: E402, F401


class SuiteTest(ReleaseCase):
    def test_finish_runs_the_whole_suite_and_stops_on_a_failure(self):
        release_main(['start', 'failing', *self.options()], self.public)
        (self.worktrees / 'failing/public/tests/test_other.py').write_text(
            FAILING_TEST, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a failing test\n', encoding='utf-8')
        self.assertEqual(release_main(['finish', 'failing', '--message', str(message), '--push', *self.options()],
                                      self.public), 3)
        self.assertIn('broken', (self.worktrees / 'failing/build/tests.log').read_text(encoding='utf-8'))
        self.assertEqual(git(self.public_origin, 'ls-tree', '--name-only', 'main', 'tests/'), SEEDED_TESTS)

    def test_finish_runs_the_suite_against_missing_config_roots(self):
        release_main(['start', 'isolated', *self.options()], self.public)
        (self.worktrees / 'isolated/public/tests/test_config.py').write_text(
            'import os\nfrom pathlib import Path\nimport unittest\n\n\nclass Config(unittest.TestCase):\n'
            '    def test_roots_are_missing(self):\n'
            '        for variable in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):\n'
            '            self.assertFalse(Path(os.environ[variable]).exists())\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Check the config roots\n', encoding='utf-8')
        self.assertEqual(release_main(['finish', 'isolated', '--message', str(message), '--push', *self.options()],
                                      self.public), 0)

    def test_finish_runs_each_private_test_file_by_itself_and_stops_on_a_failure(self):
        release_main(['start', 'private', *self.options()], self.public)
        folder = self.worktrees / 'private/private/config/tool'
        folder.mkdir(parents=True)
        (folder / 'helper.py').write_text('VALUE = 1\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a private tool\n', encoding='utf-8')
        finish = ['finish', 'private', '--private-message', str(message), '--push', *self.options()]
        log = self.worktrees / 'private/build/private-tests.log'
        imports = ('import os\nfrom pathlib import Path\nimport sys\nimport unittest\n\n{path}import helper\n\n\n'
                   'class Tool(unittest.TestCase):\n'
                   '    def test_imports_its_helper_against_missing_config_roots(self):\n'
                   '        self.assertEqual(helper.VALUE, 1)\n'
                   '        for variable in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):\n'
                   '            self.assertFalse(Path(os.environ[variable]).exists())\n' + RUN_ALONE)
        for text, stop, logged in (
                (FAILING_TEST + RUN_ALONE, 'the private tests in config/tool/test_tool.py failed', 'broken'),
                (FAILING_TEST + RUN_ALONE.replace('main()', 'main(exit=False)'),
                 'the private tests in config/tool/test_tool.py failed', 'FAILED (failures=1)'),
                (FAILING_TEST.replace('self.fail', 'import sys; sys.stderr.buffer.write(b"caf\\xe9\\n"); self.fail')
                 + RUN_ALONE, 'the private tests in config/tool/test_tool.py failed', 'caf\ufffd\n'),
                (PASSING_TEST, 'config/tool/test_tool.py, so end it with unittest.main()',
                 '== config/tool/test_tool.py\n'),
                (imports.format(path=''), 'the private tests in config/tool/test_tool.py failed',
                 "ModuleNotFoundError: No module named 'helper'")):
            with self.subTest(stop=stop, logged=logged):
                (folder / 'test_tool.py').write_text(text, encoding='utf-8')
                with redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(release_main(finish, self.public), 3)
                self.assertIn(stop, error.getvalue())
                self.assertIn(logged, log.read_text(encoding='utf-8'))
                self.assertEqual(git(self.private_origin, 'log', '--format=%s', 'main'), 'seed')
        (folder / 'test_tool.py').write_text(
            imports.format(path='sys.path.insert(0, str(Path(__file__).resolve().parent))\n'), encoding='utf-8')
        (folder / 'test_more.py').write_text(PASSING_TEST + RUN_ALONE, encoding='utf-8')
        (folder.parent / 'other').mkdir()
        (folder.parent / 'other/test_other.py').write_text(PASSING_TEST + RUN_ALONE, encoding='utf-8')
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(finish, self.public), 0)
        self.assertIn('Ran 3 private tests in 2 folders: OK', output.getvalue())
        self.assertEqual(git(self.private_origin, 'ls-tree', '-r', '--name-only', 'main', 'config/'),
                         f'config/other/test_other.py\n{release.DENYLIST}\nconfig/tool/helper.py\n'
                         'config/tool/test_more.py\nconfig/tool/test_tool.py')

    def test_finish_runs_the_private_test_files_at_the_same_time(self):
        release_main(['start', 'together', *self.options()], self.public)
        meeting = self.root / 'meeting'
        meeting.mkdir()
        meet = ('import os\nfrom pathlib import Path\nimport time\nimport unittest\n\n\nclass Meet(unittest.TestCase):\n'
                '    def test_meets_the_other_file(self):\n'
                '        meeting = Path(os.environ["MEETING"])\n'
                '        (meeting / Path(__file__).name).touch()\n'
                '        deadline = time.monotonic() + 60\n'
                '        while len(list(meeting.iterdir())) < 2 and time.monotonic() < deadline:\n'
                '            time.sleep(0.05)\n'
                '        self.assertEqual(len(list(meeting.iterdir())), 2)\n' + RUN_ALONE)
        for name in ('test_one.py', 'test_two.py'):
            (self.worktrees / 'together/private' / name).write_text(meet, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add two tests\n', encoding='utf-8')
        os.environ['MEETING'] = str(meeting)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(['finish', 'together', '--private-message', str(message), *self.options()],
                                          self.public), 0)
        self.assertIn('Ran 2 private tests in 1 folders: OK', output.getvalue())

    def test_finish_names_each_private_test_file_that_skipped_tests_and_how_many(self):
        release_main(['start', 'skips', *self.options()], self.public)
        skipping = PASSING_TEST.replace('    def test_passes', '    @unittest.skip("no tool here")\n'
                                        '    def test_skips(self):\n        pass\n\n    def test_passes')
        for folder, text in (('config/one', skipping), ('config/two', PASSING_TEST)):
            (self.worktrees / 'skips/private' / folder).mkdir(parents=True)
            (self.worktrees / 'skips/private' / folder / 'test_tool.py').write_text(text + RUN_ALONE, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add two tests\n', encoding='utf-8')
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(['finish', 'skips', '--private-message', str(message), *self.options()],
                                          self.public), 0)
        self.assertIn('Ran 3 private tests in 2 folders: OK; skipped tests: config/one/test_tool.py 1; log ',
                      output.getvalue())

    def test_a_private_test_file_reads_an_ended_stdin_while_the_finish_keeps_its_own_open(self):
        release_main(['start', 'stdin', *self.options()], self.public)
        (self.worktrees / 'stdin/private/test_reads.py').write_text(
            'import sys\nimport threading\nimport unittest\n\n\nclass Reads(unittest.TestCase):\n'
            '    def test_reads_an_ended_stdin(self):\n'
            '        read = []\n'
            '        reader = threading.Thread(target=lambda: read.append(sys.stdin.read()), daemon=True)\n'
            '        reader.start()\n'
            '        reader.join(30)\n'
            '        self.assertEqual(read, [""])\n' + RUN_ALONE, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a test that reads stdin\n', encoding='utf-8')
        read, write = os.pipe()
        kept = os.dup(0)
        os.dup2(read, 0)
        try:
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(release_main(['finish', 'stdin', '--private-message', str(message), *self.options()],
                                              self.public), 0)
        finally:
            os.dup2(kept, 0)
            for descriptor in (read, write, kept):
                os.close(descriptor)
        self.assertIn('Ran 1 private tests in 1 folders: OK', output.getvalue())

    def test_finish_names_every_failing_private_test_file_and_keeps_each_ones_output(self):
        release_main(['start', 'failures', *self.options()], self.public)
        private = self.worktrees / 'failures/private'
        for folder, body in (('first', 'self.fail("first reason")'),
                             ('hangs', 'print("hangs output", flush=True); '
                                       'print("hangs reason", file=__import__("sys").stderr, flush=True); '
                                       '__import__("time").sleep(3600)'),
                             ('second', 'raise ValueError("second reason")')):
            (private / 'config' / folder).mkdir(parents=True)
            (private / 'config' / folder / 'test_tool.py').write_text(
                PASSING_TEST.replace('        pass', f'        {body}').replace('test_passes', f'test_{folder}')
                + RUN_ALONE,
                encoding='utf-8')
        (private / 'test_passing.py').write_text(PASSING_TEST + RUN_ALONE, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add failing tests\n', encoding='utf-8')
        with redirect_stderr(io.StringIO()) as error, patch.object(release, 'PRIVATE_TEST_TIMEOUT_SECONDS', 10):
            self.assertEqual(release_main(['finish', 'failures', '--private-message', str(message), *self.options()],
                                          self.public), 3)
        self.assertIn('the private tests in config/first/test_tool.py failed (test_first); config/hangs/test_tool.py '
                      'ran past the 10 s limit; the private tests in config/second/test_tool.py failed (test_second); '
                      'read', error.getvalue())
        log = (self.worktrees / 'failures/build/private-tests.log').read_text(encoding='utf-8')
        for folder, kept in (('first', ['FAIL: test_first', 'AssertionError: first reason']),
                             ('hangs', ['hangs output', 'hangs reason']),
                             ('second', ['ERROR: test_second', 'ValueError: second reason'])):
            block = log.split(f'== config/{folder}/test_tool.py\n')[1].split('\n== ')[0]
            for text in kept:
                self.assertIn(text, block)
        self.assertIn('== test_passing.py\n', log)

    def test_finish_stops_on_a_private_test_file_link_that_points_at_nothing(self):
        release_main(['start', 'link', *self.options()], self.public)
        (self.worktrees / 'link/private/config/tool').mkdir()
        (self.worktrees / 'link/private/config/tool/test_tool.py').symlink_to('tool_tests.py')
        message = self.root / 'message'
        message.write_text('Link a test\n', encoding='utf-8')
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'link', '--private-message', str(message), *self.options()],
                                          self.public), 3)
        self.assertIn('these private test file links point at nothing, so their tests would not run: '
                      'config/tool/test_tool.py;', error.getvalue())
        self.assertFalse((self.worktrees / 'link/build/tests.log').exists())

    def test_the_private_tests_pass_in_a_session_that_forces_colour(self):
        release_main(['start', 'coloured', *self.options()], self.public)
        # The file runs with -I, which ignores PYTHON_COLORS, but the Python run it starts does not use -I.
        (self.worktrees / 'coloured/private/test_tool.py').write_text(
            'import subprocess\nimport sys\nimport unittest\n\n\nclass Seed(unittest.TestCase):\n'
            '    def test_a_child_writes_no_colour(self):\n'
            '        child = subprocess.run([sys.executable, "-c", "1/0"], capture_output=True, text=True)\n'
            '        self.assertNotIn("\\x1b[", child.stderr)\n' + RUN_ALONE, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a private test\n', encoding='utf-8')
        os.environ.update(FORCE_COLOR='1', PYTHON_COLORS='1')
        os.environ.pop('NO_COLOR', None)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(['finish', 'coloured', '--private-message', str(message), *self.options()],
                                          self.public), 0)
        self.assertIn('Ran 1 private tests in 1 folders: OK', output.getvalue())

    def test_finish_runs_a_private_test_file_in_a_folder_whose_name_is_not_ascii(self):
        release_main(['start', 'named', *self.options()], self.public)
        folder = self.worktrees / 'named/private/config/straße'
        folder.mkdir(parents=True)
        (folder / 'test_tool.py').write_text(FAILING_TEST + RUN_ALONE, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a private tool\n', encoding='utf-8')
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'named', '--private-message', str(message), *self.options()],
                                          self.public), 3)
        self.assertIn('the private tests in config/straße/test_tool.py failed', error.getvalue())

    def test_finish_does_not_run_a_private_test_file_the_release_deletes(self):
        seed = self.clone(self.private_origin, 'private-seed')
        for name in ('test_kept.py', 'test_gone.py'):
            (seed / name).write_text(PASSING_TEST + RUN_ALONE, encoding='utf-8')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-qm', 'Two tests')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')
        release_main(['start', 'fewer', *self.options()], self.public)
        (self.worktrees / 'fewer/private/test_gone.py').unlink()
        message = self.root / 'message'
        message.write_text('Delete a test\n', encoding='utf-8')
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(['finish', 'fewer', '--private-message', str(message), '--push',
                                           *self.options()], self.public), 0)
        self.assertIn('Ran 1 private tests in 1 folders: OK', output.getvalue())
        self.assertEqual(git(self.private_origin, 'ls-tree', '--name-only', 'main', 'test_gone.py'), '')


if __name__ == '__main__':
    unittest.main()
