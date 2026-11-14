"""cdp.mjs answers --help and empty input with its usage line instead of a JSON parse error."""

import shutil
import subprocess
import unittest
from pathlib import Path

CDP = Path(__file__).resolve().parents[1] / 'skills/browser-and-computer-use/scripts/cdp.mjs'
USAGE = 'usage: node cdp.mjs [--url WS] NAME < steps.json'


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class CdpCliTest(unittest.TestCase):
    def run_cdp(self, *args, steps=''):
        return subprocess.run(['node', str(CDP), *args], input=steps, capture_output=True, text=True, timeout=60)

    def test_help_prints_usage_and_exits_zero(self):
        for flag in ('--help', '-h'):
            with self.subTest(flag=flag):
                result = self.run_cdp(flag)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), USAGE)

    def test_an_option_is_not_taken_as_a_session_name(self):
        result = self.run_cdp('-x', steps='[]')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), USAGE)

    def test_empty_input_names_the_missing_steps(self):
        for steps in ('', '\n', '  \n'):
            with self.subTest(steps=steps):
                result = self.run_cdp('some-session', steps=steps)
                self.assertEqual(result.returncode, 2)
                self.assertIn('no steps on standard input', result.stderr)
                self.assertNotIn('SyntaxError', result.stderr)


if __name__ == '__main__':
    unittest.main()
