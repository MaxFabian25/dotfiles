import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import skill_triggers

# A stand-in for `claude -p`: it streams one Skill call, split across two deltas, then a result that names the
# permission mode and model it was given and the files in the folder it ran in.
FAKE = r'''
import json, os, sys
def stream(event):
    print(json.dumps({'type': 'stream_event', 'event': event}), flush=True)
print(json.dumps({'type': 'system', 'subtype': 'init'}), flush=True)
stream({'type': 'content_block_start', 'content_block': {'type': 'tool_use', 'name': 'Skill'}})
stream({'type': 'content_block_delta', 'delta': {'partial_json': '{"skill": "prose:'}})
stream({'type': 'content_block_delta', 'delta': {'partial_json': 'prose"}'}})
stream({'type': 'content_block_stop'})
mode = sys.argv[sys.argv.index('--permission-mode') + 1]
model = sys.argv[sys.argv.index('--model') + 1]
print(json.dumps({'type': 'result', 'is_error': False, 'result': f'{mode} {model} {os.listdir(".")}'}), flush=True)
'''


@unittest.skipIf(os.name == 'nt', 'the stand-in claude is a script with a shebang')
class SkillTriggersTest(unittest.TestCase):
    def test_records_the_skills_a_session_loads(self):
        with tempfile.TemporaryDirectory() as temporary:
            bin_folder, folder = Path(temporary) / 'bin', Path(temporary) / 'probe'
            bin_folder.mkdir()
            folder.mkdir()
            fake = bin_folder / 'claude'
            fake.write_text(f'#!{sys.executable}\n' + FAKE, encoding='utf-8')
            fake.chmod(0o755)
            with patch.dict(os.environ, {'PATH': f'{bin_folder}{os.pathsep}{os.environ["PATH"]}'}):
                result = skill_triggers.run_one('Proofread this paragraph', folder, 'default', 'fable')
        self.assertEqual(result['skills'], ['prose:prose'])
        self.assertEqual(result['tools'], ['Skill'])
        self.assertEqual((result['end'], result['text']), ('result', 'default fable []'))
        self.assertTrue(skill_triggers.matches('prose', result['skills']))
        self.assertFalse(skill_triggers.matches('pros', result['skills']))


class SessionModelTest(unittest.TestCase):
    def test_reads_the_model_new_sessions_start_on(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with patch.object(Path, 'home', return_value=home):
                self.assertEqual(skill_triggers.session_model(), 'opus')
                (home / '.claude').mkdir()
                settings = home / '.claude/settings.json'
                for text, expected in (('{"model": "claude-fable-5-2"}', 'claude-fable-5-2'), ('{}', 'opus'),
                                       ('{"model": 7}', 'opus'), ('not json', 'opus'), ('[]', 'opus')):
                    settings.write_text(text, encoding='utf-8')
                    self.assertEqual(skill_triggers.session_model(), expected, text)


if __name__ == '__main__':
    unittest.main()
