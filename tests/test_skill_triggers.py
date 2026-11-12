from contextlib import redirect_stdout
import io
import json
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


    def test_a_measurement_counts_hits_on_should_trigger_queries_apart_from_near_miss_triggers(self):
        items = [{'query': 'Proofread this', 'target': 'prose', 'should_trigger': True},
                 {'query': 'Write a poem', 'target': 'prose', 'should_trigger': False},
                 {'query': 'Fix my essay', 'target': 'prose', 'should_trigger': False}]
        loaded = {'Proofread this': ['prose:prose'], 'Write a poem': ['prose:prose'], 'Fix my essay': []}
        with tempfile.TemporaryDirectory() as temporary:
            queries, out = Path(temporary) / 'queries.json', Path(temporary) / 'out.json'
            queries.write_text(json.dumps(items), encoding='utf-8')
            with patch.object(sys, 'argv', ['skill_triggers.py', str(queries), str(out), '--runs', '1', '--model', 'm']), \
                    patch('skill_triggers.run_one', side_effect=lambda query, *rest: {'skills': loaded[query]}), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(skill_triggers.main(), 0)
            rows = json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(output.getvalue(), 'model: m\nprose: should-trigger 1/1, near-miss false triggers 1/2\n')
        self.assertEqual([row['triggered'] for row in rows], [True, True, False])


if __name__ == '__main__':
    unittest.main()
