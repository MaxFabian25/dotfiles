from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
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

# The result `claude -p` printed for a prompt that a rate limit refused on 13 November 2026, longer than the
# 160 characters a row keeps. A session the API refused that day, for an unknown model, exited with status 1.
REFUSAL = ('API Error: Request rejected (429) · All credentials for model claude-fable-5-2 are cooling down (last '
           "error: rate_limit_error: This request would exceed your account's rate limit. Please try again later.) "
           '(reset after 28m47s)')
REFUSED = rf'''
import json, sys
print(json.dumps({{'type': 'system', 'subtype': 'init'}}), flush=True)
print(json.dumps({{'type': 'result', 'subtype': 'success', 'is_error': True, 'result': {REFUSAL!r}}}), flush=True)
sys.exit(1)
'''

# The error variant of a result in claude 2.1.322's schema carries its message in errors, and may carry none. This
# session has begun its answer and stays up after its result, so the tool has to stop at the result rather than at
# the end of the output or the time limit.
ERROR_RESULT = r'''
import json, sys, time
print(STDERR, file=sys.stderr, flush=True)
print(json.dumps({'type': 'stream_event', 'event': {'type': 'content_block_start', 'content_block': {'type': 'text'}}}),
      flush=True)
print(json.dumps({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'errors': ERRORS}),
      flush=True)
time.sleep(600)
'''

# Five tool calls, the tool's limit, then a session that would go on.
FIVE_TOOLS = r'''
import json, time
for _ in range(5):
    print(json.dumps({'type': 'stream_event', 'event': {'type': 'content_block_start',
                      'content_block': {'type': 'tool_use', 'name': 'Read'}}}), flush=True)
time.sleep(600)
'''

# A session still retrying the API, with the fields of claude 2.1.322's api_retry event, or one the model has
# begun to answer, that has not ended when the tool's time limit comes.
RETRYING = r'''
import json, time
print(json.dumps({'type': 'system', 'subtype': 'api_retry', 'attempt': 3, 'max_retries': 10, 'retry_delay_ms': 8000,
                  'error_status': STATUS, 'error': ERROR}), flush=True)
open('printed', 'w').close()
time.sleep(600)
'''

ANSWERING = r'''
import json, time
print(json.dumps({'type': 'stream_event', 'event': {'type': 'content_block_start',
                  'content_block': {'type': 'text'}}}), flush=True)
open('printed', 'w').close()
time.sleep(600)
'''

CRASHED = r'''
import json, sys
print(json.dumps({'type': 'system', 'subtype': 'init'}), flush=True)
print('Invalid API key · Please run /login', file=sys.stderr, flush=True)
sys.exit(3)
'''


@unittest.skipIf(os.name == 'nt', 'the stand-in claude is a script with a shebang')
class SkillTriggersTest(unittest.TestCase):
    def run_with(self, source, limit_once_printed=None):
        """Run the stand-in through run_one. With limit_once_printed, the time limit is that many seconds on a
        clock that reaches it only at the check after one that saw the stand-in's `printed` marker, which the
        stand-in writes after flushing its lines, so the read between those two checks has taken them."""
        with tempfile.TemporaryDirectory() as temporary:
            bin_folder, folder = Path(temporary) / 'bin', Path(temporary) / 'probe'
            bin_folder.mkdir()
            folder.mkdir()
            fake = bin_folder / 'claude'
            fake.write_text(f'#!{sys.executable}\n' + source, encoding='utf-8')
            fake.chmod(0o755)
            with patch.dict(os.environ, {'PATH': f'{bin_folder}{os.pathsep}{os.environ["PATH"]}'}):
                if limit_once_printed is None:
                    return skill_triggers.run_one('Proofread this paragraph', folder, 'default', 'fable')
                marker, seen = folder / 'printed', []

                def now():
                    seen.append(marker.exists())
                    return limit_once_printed if len(seen) > 2 and seen[-2] else 0

                with patch.object(skill_triggers, 'TIMEOUT', limit_once_printed), \
                        patch.object(skill_triggers, 'time', SimpleNamespace(time=now)):
                    return skill_triggers.run_one('Proofread this paragraph', folder, 'default', 'fable')

    def test_records_the_skills_a_session_loads(self):
        result = self.run_with(FAKE)
        self.assertEqual(result['skills'], ['prose:prose'])
        self.assertEqual(result['tools'], ['Skill'])
        self.assertEqual((result['end'], result['text']), ('result', 'default fable []'))
        self.assertTrue(skill_triggers.matches('prose', result['skills']))
        self.assertFalse(skill_triggers.matches('pros', result['skills']))

    def test_a_session_that_ends_in_an_error_raises_the_error_instead_of_counting_as_a_miss(self):
        with self.assertRaises(skill_triggers.SessionFailed) as raised:
            self.run_with(REFUSED)
        self.assertEqual(str(raised.exception), f"The session for 'Proofread this paragraph' failed: {REFUSAL}")

    def test_an_error_result_without_result_text_raises_its_errors_or_else_the_error_output(self):
        cases = ((['Invalid config file', 'Org config refused'], 'Bad JSON', 'Invalid config file; Org config refused'),
                 ([], 'Bad JSON', 'Bad JSON'),
                 ([], '', 'error_during_execution'))
        for errors, stderr, reason in cases:
            with self.subTest(errors=errors, stderr=stderr), self.assertRaises(skill_triggers.SessionFailed) as raised:
                self.run_with(f'ERRORS = {errors!r}\nSTDERR = {stderr!r}\n' + ERROR_RESULT)
            self.assertEqual(str(raised.exception), f"The session for 'Proofread this paragraph' failed: {reason}")

    def test_a_session_stopped_at_the_tool_limit_counts(self):
        self.assertEqual(self.run_with(FIVE_TOOLS), {'tools': ['Read'] * 5, 'skills': [], 'end': 'limit'})

    def test_a_session_the_model_never_answered_by_the_time_limit_raises_its_last_retry(self):
        for status, error, cause in ((429, 'rate_limit', 'status 429: rate_limit'),
                                     (None, 'unknown', 'a connection error: unknown')):
            with self.subTest(status=status), self.assertRaises(skill_triggers.SessionFailed) as raised:
                self.run_with(f'STATUS = {status!r}\nERROR = {error!r}\n' + RETRYING, limit_once_printed=2)
            self.assertEqual(str(raised.exception), "The session for 'Proofread this paragraph' had no answer from "
                             f'the model within 2 seconds. Its last retry followed {cause}.')

    def test_a_session_stopped_at_the_time_limit_after_an_answer_counts(self):
        self.assertEqual(self.run_with(ANSWERING, limit_once_printed=2), {'tools': [], 'skills': [], 'end': 'limit'})

    def test_a_session_that_exits_without_a_result_raises_its_status_and_error_output(self):
        with self.assertRaises(skill_triggers.SessionFailed) as raised:
            self.run_with(CRASHED)
        self.assertEqual(str(raised.exception), "claude exited with status 3 on 'Proofread this paragraph' before its "
                         'result: Invalid API key · Please run /login')


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

    def test_a_failed_session_stops_the_measurement_with_its_error_and_writes_no_results(self):
        def run_one(query, *rest):
            if query == 'Write a poem':
                raise skill_triggers.SessionFailed('API Error: Request rejected (429)')
            return {'skills': []}

        items = [{'query': 'Proofread this', 'target': 'prose', 'should_trigger': True},
                 {'query': 'Write a poem', 'target': 'prose', 'should_trigger': False}]
        with tempfile.TemporaryDirectory() as temporary:
            queries, out = Path(temporary) / 'queries.json', Path(temporary) / 'out.json'
            queries.write_text(json.dumps(items), encoding='utf-8')
            with patch.object(sys, 'argv', ['skill_triggers.py', str(queries), str(out), '--runs', '1', '--model', 'm']), \
                    patch('skill_triggers.run_one', side_effect=run_one), \
                    redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as stopped:
                skill_triggers.main()
            self.assertFalse(out.exists())
        self.assertEqual(stopped.exception.code,
                         f'API Error: Request rejected (429)\nThe measurement stopped, so {out} was not written.')
        self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
