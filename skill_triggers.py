"""Measure how often the installed skills load for a set of prompts, for before and after numbers on a skill
description change.

Runs `claude -p` once per prompt and run, from an empty temporary folder, with the write-capable tools
disallowed, and records which skills each session loads within its first few tool calls. It uses the real
loader with every installed skill competing, which a test copy of one skill cannot show. A session that fails,
such as one refused at a rate limit, stops the measurement with the session's error instead of counting as a
miss, and OUT.json is not written. So does one that the model has not answered by the time limit, such as one
still retrying the API. A session stopped at the time limit after an answer, or at the tool limit, counts.
Usage:

    python3 -I skill_triggers.py QUERIES.json OUT.json [--runs 2] [--workers 6] [--permission-mode MODE]
        [--model MODEL]

QUERIES.json is a list of {"target", "query", "should_trigger"}, where target is the skill name. Each session
runs on the model that new Claude sessions start on, the `model` key of ~/.claude/settings.json, unless --model
names another, and at that model's saved effort. Compare a before and an after measurement only when both used
the same --permission-mode and the same model, which each output row records.
"""

import argparse
import json
import os
import select
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MAX_TOOLS = 5
TIMEOUT = 180


def session_model():
    try:
        model = json.loads((Path.home() / '.claude/settings.json').read_text(encoding='utf-8')).get('model')
    except (OSError, ValueError, AttributeError):
        model = None
    return model if isinstance(model, str) and model else 'opus'


class SessionFailed(Exception):
    pass


def run_one(query, folder, permission_mode='bypassPermissions', model='opus'):
    command = ['claude', '-p', query, '--model', model, '--permission-mode', permission_mode,
               '--disallowedTools', 'Bash,Edit,Write,NotebookEdit', '--no-session-persistence', '--max-turns', '6',
               '--output-format', 'stream-json', '--verbose', '--include-partial-messages']
    environment = {key: value for key, value in os.environ.items() if key != 'CLAUDECODE'}
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, stdin=subprocess.DEVNULL,
                                   cwd=folder, env=environment)
        out = process.stdout
        assert out is not None
        tools, skills = [], []
        current, partial = None, ''
        buffer, start = '', time.time()
        failed = None
        answered, retry, unanswered = False, None, False
        try:
            while time.time() - start < TIMEOUT and len(tools) < MAX_TOOLS:
                ready, _, _ = select.select([out], [], [], 1.0)
                if not ready:
                    if process.poll() is not None:
                        break
                    continue
                chunk = os.read(out.fileno(), 65536)
                if not chunk:
                    break
                buffer += chunk.decode('utf-8', 'replace')
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get('type') == 'result':
                        if event.get('is_error'):
                            failed = event
                            break
                        return {'tools': tools, 'skills': skills, 'end': 'result',
                                'text': str(event.get('result', ''))[:160]}
                    if event.get('type') == 'system' and event.get('subtype') == 'api_retry':
                        retry = event
                    if event.get('type') != 'stream_event':
                        continue
                    answered = True
                    inner = event.get('event', {})
                    kind = inner.get('type')
                    if kind == 'content_block_start' and inner.get('content_block', {}).get('type') == 'tool_use':
                        current, partial = inner['content_block'].get('name', ''), ''
                        tools.append(current)
                    elif kind == 'content_block_delta' and current:
                        partial += inner.get('delta', {}).get('partial_json', '')
                    elif kind == 'content_block_stop' and current:
                        if current == 'Skill':
                            try:
                                skills.append(json.loads(partial).get('skill', ''))
                            except json.JSONDecodeError:
                                skills.append(partial[:80])
                        current = None
                if failed:
                    break
            else:
                if answered:
                    return {'tools': tools, 'skills': skills, 'end': 'limit'}
                unanswered = True
        finally:
            # SIGTERM lets Claude remove its plugin-cache lease markers; SIGKILL leaves them behind.
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            out.close()
        errors.seek(0)
        stderr = errors.read().decode('utf-8', 'replace').strip()
        if failed:
            # An error result carries its message in result, or, as claude 2.1.322's schema says, in errors.
            reason = failed.get('result') or '; '.join(map(str, failed.get('errors') or [])) or stderr
            raise SessionFailed(f'The session for {query!r} failed: {reason or failed.get("subtype")}')
        if unanswered:
            last = ''
            if retry:
                # claude's schema gives no status for a connection error, such as a timeout.
                status = retry.get('error_status')
                cause = 'a connection error' if status is None else f'status {status}'
                last = f" Its last retry followed {cause}: {retry.get('error')}."
            raise SessionFailed(f'The session for {query!r} had no answer from the model within {TIMEOUT} seconds.'
                                + last + (f' {stderr}' if stderr else ''))
        # A session that runs its course ends its output with a result, so the loop's other breaks mean it failed.
        raise SessionFailed(f'claude exited with status {process.returncode} on {query!r} before its result: {stderr}')


def matches(target, skills):
    return any(skill == target or skill.endswith(':' + target) for skill in skills)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('queries', type=Path)
    parser.add_argument('out', type=Path)
    parser.add_argument('--runs', type=int, default=2)
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--permission-mode', default='bypassPermissions')
    parser.add_argument('--model', default=session_model())
    args = parser.parse_args()
    items = json.loads(args.queries.read_text(encoding='utf-8'))
    jobs = [(index, run) for index in range(len(items)) for run in range(args.runs)]
    try:
        with (tempfile.TemporaryDirectory(prefix='skill-triggers-') as folder,
              ThreadPoolExecutor(args.workers) as pool):
            # The first failure the map reaches cancels the sessions that have not started.
            results = list(pool.map(
                lambda job: (job, run_one(items[job[0]]['query'], folder, args.permission_mode, args.model)), jobs))
    except SessionFailed as failure:
        sys.exit(f'{failure}\nThe measurement stopped, so {args.out} was not written.')
    rows = [{**items[index], 'run': run, 'model': args.model, **result,
             'triggered': matches(items[index]['target'], result['skills'])}
            for (index, run), result in results]
    args.out.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding='utf-8')
    print(f'model: {args.model}')
    summary = {}
    for row in rows:
        counts = summary.setdefault(row['target'], {'pos_hit': 0, 'pos_n': 0, 'neg_hit': 0, 'neg_n': 0})
        key = 'pos' if row['should_trigger'] else 'neg'
        counts[key + '_n'] += 1
        counts[key + '_hit'] += row['triggered']
    for target, counts in summary.items():
        print(f"{target}: should-trigger {counts['pos_hit']}/{counts['pos_n']}, "
              f"near-miss false triggers {counts['neg_hit']}/{counts['neg_n']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
