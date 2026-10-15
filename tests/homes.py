"""Synthetic device homes for the host tests: config files, caches and a CLI that replays recorded JSON."""
import json
from pathlib import Path
import sys
import tempfile

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

import host_state  # noqa: E402

FAKE_CLI = '''import json, pathlib, sys
answers = json.loads(pathlib.Path(__file__).with_name(sys.argv[1]).read_text())
key = " ".join(sys.argv[2:])
with pathlib.Path(__file__).with_name(sys.argv[1] + ".log").open("a") as log:
    log.write(key + "\\n")
if key == "app-server --stdio" and "hooks/list" in answers:
    for expected in ("initialize", "initialized", "hooks/list"):
        message = json.loads(sys.stdin.readline())
        assert message["method"] == expected, message
        with pathlib.Path(__file__).with_name(sys.argv[1] + ".log").open("a") as log:
            log.write(json.dumps(message) + "\\n")
        if "id" in message:
            result = answers[expected] if expected == "hooks/list" else {}
            print(json.dumps({"id": message["id"], "result": result}), flush=True)
    sys.stdin.read()
    sys.exit(0)
if key not in answers:
    sys.exit("unrecorded: " + key)
sys.stdout.write(answers[key])
'''


class Home:
    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.roots = host_state.DeviceRoots(home=base, codex_home=base / '.codex', claude_home=base / '.claude',
                                            agents_home=base / '.agents', engine=base / 'engine')
        self.bin = base / 'bin'
        self.bin.mkdir()
        (self.bin / 'cli.py').write_text(FAKE_CLI)
        return self

    def __exit__(self, *exc):
        self.directory.cleanup()

    def write(self, path, text):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def calls(self, name):
        log = self.bin / f'{name}.json.log'
        return log.read_text().splitlines() if log.is_file() else []

    def cli(self, name, answers):
        """argv for a CLI that prints the recorded answer for each argument list, and fails on any other."""
        (self.bin / f'{name}.json').write_text(json.dumps(answers))
        return [sys.executable, str(self.bin / 'cli.py'), f'{name}.json']

    def skill(self, root, name, *, explicit_sidecar=False, frontmatter_explicit=False, body='Use it.\n'):
        extra = 'disable-model-invocation: true\n' if frontmatter_explicit else ''
        self.write(Path(root) / 'skills' / name / 'SKILL.md', f'---\nname: {name}\ndescription: {name}\n{extra}---\n{body}')
        if explicit_sidecar:
            self.write(Path(root) / 'skills' / name / 'agents/openai.yaml', 'policy:\n  allow_implicit_invocation: false\n')

    def receipt(self, files):
        self.write(self.roots.codex_home / 'skill-updates/invocation-overrides/state.json',
                   json.dumps({'schemaVersion': 1, 'files': {str(path): record for path, record in files.items()}}))
