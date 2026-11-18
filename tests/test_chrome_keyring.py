"""chrome_keyring.py lets Chrome start on the user's own profile only while the keyring holding its cookie key is unlocked."""

import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/browser-and-computer-use/scripts'))
import chrome_keyring  # noqa: E402


def busctl(stdout='', stderr='', code=0):
    return mock.patch.object(chrome_keyring.subprocess, 'run',
                             return_value=subprocess.CompletedProcess([], code, stdout, stderr))


class RefusalTest(unittest.TestCase):
    def test_allows_an_unlocked_keyring(self):
        with busctl('b false\n'):
            self.assertEqual(chrome_keyring.refusal(), '')

    def test_refuses_a_locked_keyring_and_says_how_to_unlock_it(self):
        with busctl('b true\n'):
            reason = chrome_keyring.refusal()
        self.assertIn('is locked', reason)
        self.assertIn('chrome_keyring.py unlock', reason)
        self.assertIn('Never unlock it yourself', reason)

    def test_refuses_when_the_keyring_does_not_answer(self):
        with busctl(stderr='Failed to get property Locked: Unknown object\n', code=1):
            self.assertIn('Unknown object', chrome_keyring.refusal())
        with mock.patch.object(chrome_keyring.subprocess, 'run', side_effect=FileNotFoundError('busctl')):
            self.assertIn('could not ask the keyring', chrome_keyring.refusal())

    def test_asks_the_default_collection_on_the_user_bus(self):
        with busctl('b false\n') as run:
            chrome_keyring.refusal({'XDG_RUNTIME_DIR': '/run/user/1000'})
        self.assertEqual(run.call_args.args[0][:4], ['busctl', '--user', 'get-property', 'org.freedesktop.secrets'])
        self.assertIn('/org/freedesktop/secrets/aliases/default', run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['env'], {'XDG_RUNTIME_DIR': '/run/user/1000'})


class CheckTest(unittest.TestCase):
    def test_exits_1_with_the_reason_while_locked(self):
        with busctl('b true\n'), contextlib.redirect_stderr(io.StringIO()) as said:
            self.assertEqual(chrome_keyring.main(['check']), 1)
        self.assertIn('is locked', said.getvalue())

    def test_exits_0_while_unlocked(self):
        with busctl('b false\n'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(chrome_keyring.main(['check']), 0)


if __name__ == '__main__':
    unittest.main()
