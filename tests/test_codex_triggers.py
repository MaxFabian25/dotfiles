import json
import os
from pathlib import Path
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
    def run_stand_in(self, body):
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / 'codex'
            fake.write_text(f'#!{sys.executable}\nimport time\n{body}', encoding='utf-8')
            fake.chmod(0o755)
            with patch.dict(os.environ, {'PATH': f'{temporary}{os.pathsep}{os.environ["PATH"]}'}):
                return codex_triggers.run_one(ENTRY)

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


if __name__ == '__main__':
    unittest.main()
