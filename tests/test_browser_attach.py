"""The attach helpers press only their own attach's prompts and stop only their own session's daemon."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/browser-and-computer-use/scripts'))
import ab_session  # noqa: E402
import approve_attach  # noqa: E402

# One Chrome window: a remote-debugging prompt, a site permission bubble with its own Allow button, and a page
# whose content holds a lookalike prompt.
TREE = {
    'app': ('application', 'Google Chrome', ['frame']),
    'frame': ('frame', 'Home - Google Chrome', ['prompt', 'bubble', 'page']),
    'prompt': ('alert', 'Allow remote debugging?', ['panel']),
    'panel': ('panel', '', ['allow', 'cancel']),
    'allow': ('push button', 'Allow', []),
    'cancel': ('push button', 'Cancel', []),
    'bubble': ('alert', 'example.com wants to use your camera', ['bubble-allow']),
    'bubble-allow': ('push button', 'Allow', []),
    'page': ('document web', 'Home', ['fake-prompt']),
    'fake-prompt': ('alert', 'Allow remote debugging?', ['fake-allow']),
    'fake-allow': ('push button', 'Allow', []),
}


class Process:
    """A command that runs for as long as there are screen contents left to show."""

    def __init__(self, views):
        self.views, self.returncode = views, 0

    def poll(self):
        return None if self.views else self.returncode


class ApproveTest(unittest.TestCase):
    def test_only_the_allow_button_of_a_remote_debugging_prompt_outside_page_content_is_found(self):
        found = approve_attach.allow_buttons(['app'], lambda node: TREE[node][:2], lambda node: TREE[node][2])
        self.assertEqual(found, {'allow'})

    def approve(self, views, times, limit=4):
        """Run approve over successive screen contents and clock readings; return what it pressed and reported."""
        views, times, pressed, said = list(views), iter(times), [], []
        status = approve_attach.approve(Process(views), lambda: set(views.pop(0)),
                                        lambda button: pressed.append(button) or True, limit, said.append,
                                        clock=lambda: next(times, 99.0), pause=0)
        self.assertEqual(status, 0)
        return pressed, said

    def test_a_prompt_on_screen_before_the_attach_is_never_pressed(self):
        pressed, said = self.approve([{'old'}, {'old', 'mine'}, {'old'}], [0.0, 0.5])
        self.assertEqual(pressed, ['mine'])
        self.assertEqual(said[0], '1 earlier prompt(s) left alone')

    def test_a_prompt_chrome_left_up_is_pressed_again_only_after_a_second(self):
        pressed, said = self.approve([set(), {'mine'}, {'mine'}, {'mine'}, set()], [0.0, 0.1, 0.5, 1.2])
        self.assertEqual(pressed, ['mine', 'mine'])
        self.assertIn('pressed Allow again after 1.2s: True', said)

    def test_no_more_prompts_are_approved_than_the_limit(self):
        pressed, _ = self.approve([set(), {'a', 'b', 'c'}, set()], [0.0, 0.1, 0.1, 0.1], limit=2)
        self.assertEqual(pressed, ['a', 'b'])


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_chrome_is_found_from_its_profile_lock(self):
        lock = self.root / 'SingletonLock'
        lock.symlink_to(f'Some-Host-{os.getpid()}')
        self.assertEqual(ab_session.chrome_pid(lock), os.getpid())
        ended = subprocess.Popen([sys.executable, '-c', 'pass'])
        ended.wait()
        stale = self.root / 'StaleLock'
        stale.symlink_to(f'Some-Host-{ended.pid}')
        for gone in (self.root / 'absent', stale):
            with self.subTest(lock=gone.name), self.assertRaises(SystemExit) as stopped:
                ab_session.chrome_pid(gone)
            self.assertIn('pass --approve=PID', str(stopped.exception))

    def test_approve_runs_the_same_attach_under_the_prompt_helper(self):
        command = ab_session.approve_command(4711, 'mine', ['get', 'url'])
        scripts = Path(ab_session.__file__).resolve().parent
        self.assertEqual(command[:7], ['uv', 'run', '-q', '--with', 'jeepney', 'python', '-I'])
        self.assertEqual(command[7:11], [str(scripts / 'approve_attach.py'), '--pid', '4711', '--'])
        self.assertEqual(command[11:], [sys.executable, str(scripts / 'ab_session.py'), 'attach', 'mine', 'get', 'url'])

    def proc(self, pid, session):
        (self.root / 'proc' / str(pid)).mkdir(parents=True)
        (self.root / 'proc' / str(pid) / 'environ').write_bytes(f'HOME=/x\0AGENT_BROWSER_SESSION={session}\0'.encode())
        return self.root / 'proc'

    def test_a_daemon_is_named_only_when_its_process_carries_the_session(self):
        (self.root / 'mine.pid').write_text('4711\n')
        self.assertEqual(ab_session.daemon_pid('mine', self.root, self.proc(4711, 'mine')), 4711)
        self.assertIsNone(ab_session.daemon_pid('other', self.root, self.root / 'proc'))  # no pid file
        (self.root / 'reused.pid').write_text('4712\n')
        self.assertIsNone(ab_session.daemon_pid('reused', self.root, self.proc(4712, 'someone-else')))
        (self.root / 'gone.pid').write_text('4713\n')
        self.assertIsNone(ab_session.daemon_pid('gone', self.root, self.root / 'proc'))  # no such process

    @unittest.skipUnless(Path('/proc/self/environ').exists(), 'needs /proc')
    def test_a_daemon_that_ignores_close_is_stopped_and_another_sessions_is_left_running(self):
        def daemon(session):
            # Ignores SIGTERM, as the stuck daemons did, so only SIGKILL ends it.
            process = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, '
                                        'signal.SIG_IGN); print("up", flush=True); time.sleep(60)'],
                                       env={**os.environ, 'AGENT_BROWSER_SESSION': session}, stdout=subprocess.PIPE)
            self.addCleanup(lambda: (process.kill(), process.wait(), process.stdout.close()))
            process.stdout.readline()
            return process

        mine, theirs = daemon('mine'), daemon('theirs')
        (self.root / 'mine.pid').write_text(str(mine.pid))
        (self.root / 'wrong.pid').write_text(str(theirs.pid))
        self.assertFalse(ab_session.stop_daemon('wrong', self.root))  # the PID belongs to another session
        self.assertTrue(ab_session.stop_daemon('mine', self.root))
        self.assertIsNotNone(mine.wait(timeout=5))
        self.assertIsNone(theirs.poll())


if __name__ == '__main__':
    unittest.main()
