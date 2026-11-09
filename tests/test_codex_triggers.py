from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_triggers

ENTRY = {'target': 'prose', 'query': 'Proofread this paragraph', 'should_trigger': True}


def event(item_type, **fields):
    return json.dumps({'type': 'item.completed', 'item': {'type': item_type, **fields}})


def prints(*lines):
    return ''.join(f'print({line!r}, flush=True)\n' for line in lines)


@unittest.skipIf(os.name == 'nt', 'the stand-in codex is a script with a shebang')
class CodexTriggersTest(unittest.TestCase):
    @contextmanager
    def stand_in(self, body):
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / 'codex'
            fake.write_text(f'#!{sys.executable}\nimport sys, time\n{body}', encoding='utf-8')
            fake.chmod(0o755)
            with patch.dict(os.environ, {'PATH': f'{temporary}{os.pathsep}{os.environ["PATH"]}'}):
                yield Path(temporary)

    def run_stand_in(self, body):
        with self.stand_in(body):
            return codex_triggers.run_one(ENTRY)

    def measure(self, body):
        with self.stand_in(body) as folder:
            queries = folder / 'queries.json'
            queries.write_text(json.dumps([ENTRY]), encoding='utf-8')
            out = folder / 'out.json'
            run = subprocess.run([sys.executable, '-I', codex_triggers.__file__, str(queries), str(out), '--runs', '1'],
                                 capture_output=True, text=True, encoding='utf-8')
            return run, out.exists()

    def test_a_tool_call_naming_the_skill_counts(self):
        call = event('command_execution', command='cat /home/me/.agents/skills/prose/SKILL.md')
        self.assertTrue(self.run_stand_in(prints('not json', '3', call)))

    def test_a_windows_path_counts(self):
        call = event('command_execution', command=r'type C:\Users\me\.agents\skills\prose\SKILL.md')
        self.assertTrue(self.run_stand_in(prints(call)))

    def test_a_skill_whose_name_ends_the_same_does_not_count(self):
        call = event('command_execution', command='cat /home/me/.agents/skills/better-prose/SKILL.md')
        self.assertFalse(self.run_stand_in(prints(call)))

    def test_text_naming_the_skill_does_not_count(self):
        message = event('agent_message', text='I would read prose/SKILL.md')
        self.assertFalse(self.run_stand_in(prints(message)))

    def test_stops_counting_after_the_tool_limit(self):
        other = event('command_execution', command='ls')
        late = event('command_execution', command='cat prose/SKILL.md')
        lines = [other] * (codex_triggers.MAX_TOOLS * 2) + [late]
        self.assertFalse(self.run_stand_in(prints(*lines)))

    def test_the_time_limit_stops_a_silent_run(self):
        started = time.monotonic()
        with patch.object(codex_triggers, 'TIMEOUT', 0.2):
            self.assertFalse(self.run_stand_in('time.sleep(30)\n'))
        self.assertLess(time.monotonic() - started, 10)

    def test_a_failed_turn_stops_the_measurement_and_names_the_failure(self):
        warning = event('error', message='`[features].multi_agent` is deprecated.')
        failed = json.dumps({'type': 'turn.failed', 'error': {'message': 'You’ve hit your usage limit.'}})
        run, scored = self.measure(prints(warning, failed) + 'sys.exit(1)\n')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('You’ve hit your usage limit.', run.stderr)
        self.assertFalse(scored)

    def test_codex_failing_before_any_event_stops_the_measurement(self):
        run, scored = self.measure("sys.stderr.write('Error: No such file or directory (os error 2)\\n')\nsys.exit(1)\n")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('No such file or directory', run.stderr)
        self.assertFalse(scored)


if __name__ == '__main__':
    unittest.main()
