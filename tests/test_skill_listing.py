from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import skill_listing


class VerdictTest(unittest.TestCase):
    def test_an_over_budget_listing_names_the_overflow(self):
        code, message = skill_listing.verdict(
            '[DEBUG] Skill listing over budget: 118 skills, 61418 chars > 30000 budget\n'
            '[DEBUG] Sending 118 skills via attachment (initial)\n')
        self.assertEqual(code, 1)
        self.assertIn('61418 chars > 30000 budget', message)

    def test_a_listing_within_budget_passes(self):
        self.assertEqual(skill_listing.verdict('[DEBUG] Sending 124 skills via attachment (initial)\n'),
                         (0, 'Claude Code lists all 124 skills with their descriptions.'))

    def test_a_log_without_a_listing_is_inconclusive(self):
        self.assertEqual(skill_listing.verdict('')[0], 2)



@unittest.skipIf(os.name == 'nt', 'the stand-in claude is a script with a shebang')
class SessionTest(unittest.TestCase):
    def test_it_reads_the_debug_log_of_a_session_started_without_the_callers_claude_code_variables(self):
        with tempfile.TemporaryDirectory() as temporary:
            claude = Path(temporary) / 'claude'
            claude.write_text(f'''#!{sys.executable}
import os, sys
assert not any(key.startswith(('CLAUDECODE', 'CLAUDE_CODE')) for key in os.environ), 'inherited session'
log = sys.argv[sys.argv.index('--debug-file') + 1]
open(log, 'w').write('[DEBUG] Sending 7 skills via attachment (initial)\\n')
''')
            claude.chmod(0o755)
            with mock.patch.dict(os.environ, {'CLAUDE_CLI': str(claude), 'CLAUDECODE': '1', 'CLAUDE_CODE_ENTRYPOINT': 'cli'}), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(skill_listing.main(), 0)
        self.assertEqual(output.getvalue(), 'Claude Code lists all 7 skills with their descriptions.\n')


if __name__ == '__main__':
    unittest.main()
