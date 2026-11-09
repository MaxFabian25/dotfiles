"""Measure how often the installed skills load in Codex for a set of prompts, for before and after numbers on a skill
description change.

Runs `codex exec --ephemeral -s read-only` once per prompt and run, from an empty temporary folder, at medium effort
so a measurement stays affordable. A run counts as triggered when a tool call names <target>/SKILL.md, and it stops
at that call, after a few tool calls, or at the time limit. A run that fails, such as one at Codex's usage limit,
stops the measurement with Codex's error instead of counting as a miss. Usage:

    python3 -I codex_triggers.py QUERIES.json OUT.json [--runs 2] [--workers 4]

QUERIES.json is a list of {"target", "query", "should_trigger"}, where target is the skill name.
"""

import argparse
import json
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

MAX_TOOLS = 5
TIMEOUT = 180


def run_one(entry):
    names_skill = re.compile(rf"(?<![\w-]){re.escape(entry['target'])}/SKILL\.md")
    with (tempfile.TemporaryDirectory(prefix='codex-trigger-') as folder,
          tempfile.TemporaryFile('w+', encoding='utf-8') as errors):
        command = [shutil.which('codex') or 'codex', 'exec', '--ephemeral', '--skip-git-repo-check', '-s', 'read-only',
                   '--json', '-c', 'model_reasoning_effort=medium', '-C', folder, entry['query']]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=errors, text=True, encoding='utf-8')
        # A timer, because a check between lines of output never runs while Codex prints nothing.
        timer = threading.Timer(TIMEOUT, process.kill)
        timer.start()
        tools = 0
        try:
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get('type') == 'turn.failed':
                    raise RuntimeError(f"Codex run failed: {event['error']['message']}")
                item = event.get('item') if isinstance(event, dict) else None
                if not isinstance(item, dict) or item.get('type') in ('agent_message', 'reasoning'):
                    continue
                if names_skill.search(json.dumps(item).replace('\\\\', '/')):
                    return True
                tools += 1
                if tools >= MAX_TOOLS * 2:  # A tool call has a started and a completed event.
                    return False
            if process.wait() > 0:  # The time limit's kill gives a negative status, and that run stays a miss.
                errors.seek(0)
                raise RuntimeError(f'codex exited with status {process.returncode}: {errors.read().strip()}')
            return False
        finally:
            timer.cancel()
            process.kill()
            process.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('queries')
    parser.add_argument('out')
    parser.add_argument('--runs', type=int, default=2)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    with open(args.queries, encoding='utf-8') as source:
        queries = json.load(source)
    jobs = [entry for entry in queries for _ in range(args.runs)]
    with ThreadPoolExecutor(args.workers) as pool:
        hits = list(pool.map(run_one, jobs))
    rows = []
    for index, entry in enumerate(queries):
        got = hits[index * args.runs:(index + 1) * args.runs]
        rows.append({**entry, 'hits': sum(got), 'runs': args.runs})
    with open(args.out, 'w', encoding='utf-8') as target:
        json.dump(rows, target, indent=1)
    for row in rows:
        kind = 'should' if row['should_trigger'] else 'not   '
        print(f"{row['hits']}/{row['runs']}  {kind}  {row['target']}: {row['query'][:70]}")


if __name__ == '__main__':
    main()
