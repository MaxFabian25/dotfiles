import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402
from release_case import skip_on_windows as setUpModule  # noqa: E402, F401


class NativeWindows(unittest.TestCase):
    """setUpModule skips this file on Windows, so this runs the engine on Linux as Windows itself would."""

    STOP = 'Release stopped: run this from WSL or Omarchy, not from Windows itself; nothing was changed\n'

    def as_windows(self, box, *argv):
        """release.py run as on Windows, with HOME, the working folder and every place it takes in box, and no
        program on PATH, so that a run the stop misses cannot reach git, systemd or the network."""
        engine = Path(release.__file__).resolve().parent
        # Its modules load first, because shutil imports a Windows-only module on win32.
        script = (f"import runpy, sys; sys.path.insert(0, {str(engine)!r}); import release; "
                  f"sys.platform = 'win32'; runpy.run_path({str(engine / 'release.py')!r}, run_name='__main__')")
        places = ['--worktrees', str(Path(box) / 'worktrees'), '--private', str(Path(box) / 'private')]
        return subprocess.run([sys.executable, '-I', '-B', '-c', script, *argv, *places], capture_output=True,
                              text=True, timeout=60, cwd=box,
                              env={'PATH': str(Path(box) / 'no-programs'), 'HOME': box})

    def test_on_windows_itself_each_release_step_stops_with_a_sentence_before_anything_runs(self):
        with tempfile.TemporaryDirectory() as box:
            # --report is the file a detached finish's status goes to, which main writes as it ends.
            for argv in (['start', 'late'], ['finish', 'late', '--push', '--pull', '--report', str(Path(box) / 'exit')],
                         ['finish'], ['finish', 'late', '--detach'], ['review', 'late'], ['clean', 'late', '--force'],
                         ['result', 'late', '--timeout', '0']):
                with self.subTest(argv=argv):
                    done = self.as_windows(box, *argv)
                    self.assertEqual(os.listdir(box), [])
                    self.assertEqual((done.returncode, done.stdout, done.stderr), (3, '', self.STOP))

    def test_on_windows_itself_status_and_wait_still_read_the_machine(self):
        with tempfile.TemporaryDirectory() as box:
            done = self.as_windows(box, 'wait', '--timeout', '0')
            self.assertEqual((done.returncode, done.stdout, done.stderr),
                             (0, "No release holds this machine's lock and no pull or sync runs here.\n", ''))
            # status goes on to read the release worktrees with git, which PATH here lacks.
            done = self.as_windows(box, 'status')
            self.assertTrue(done.stdout.startswith('Running on this machine: no release, pull or sync\n'), done.stdout)
            self.assertNotIn(self.STOP, done.stderr)
            self.assertEqual(os.listdir(box), [])


if __name__ == '__main__':
    unittest.main()
