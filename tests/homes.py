"""Synthetic device homes for the host tests: config files, caches and a CLI that replays recorded JSON."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

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


def isolate_git():
    """As a test module's setUpModule: git reads no global or system config until the module's tests end. A
    developer's own config, such as commit.gpgsign = true, would otherwise stop or change the fixtures' commits."""
    patcher = mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'})
    patcher.__enter__()  # not start(), which a test's patch.stopall() would undo for the rest of the module
    unittest.addModuleCleanup(patcher.__exit__, None, None, None)


def hold_built_trees_once(case, meanwhile=None):
    """As a test's setUp: the first move of each tree out of its staging folder, or into one beside it, is refused
    the way Windows refuses it while another process holds the folder. Returns the targets refused so far.
    meanwhile, given the refused target, does what another process does during the wait before the next attempt."""
    import portable
    refused, waiting, device, sleep = [], [], portable.WINDOWS, portable.time.sleep
    case.addCleanup(setattr, portable, 'WINDOWS', device)

    def held_once(move):
        def attempt(source, target, *args, **kwargs):
            source, target = Path(source), Path(target)
            if (source.parent.name.startswith('.') and target.parent == source.parent.parent or
                    target.parent.name.startswith('.') and source.parent == target.parent.parent):
                if target not in refused:
                    refused.append(target)
                    # portable.while_held retries only on Windows, so the device is Windows from the refusal until
                    # the wait that follows it.
                    portable.WINDOWS = True
                    waiting.append(target)
                    raise PermissionError(13, 'Access is denied', str(target))
            return move(source, target, *args, **kwargs)
        return attempt

    def wait(seconds):
        if not waiting:
            return sleep(seconds)  # a wait of some other code's
        portable.WINDOWS = device
        target = waiting.pop()
        if meanwhile:
            meanwhile(target)

    for patcher in (mock.patch.object(portable.time, 'sleep', wait), mock.patch('os.rename', held_once(os.rename)),
                    mock.patch('os.replace', held_once(os.replace))):
        patcher.start()
        case.addCleanup(patcher.stop)
    return refused


def projection_roots():
    """Roots for tests that only project a description. The home does not exist, so neither this device's
    config nor a CODEX_HOME or CLAUDE_CONFIG_DIR exported by an agent session can reach the test."""
    base = Path(tempfile.gettempdir()) / 'dotfiles-tests-absent-home'
    return host_state.DeviceRoots(home=base, codex_home=base / '.codex', claude_home=base / '.claude',
                                  agents_home=base / '.agents', engine=host_state.ENGINE)


class Home:
    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name).resolve()
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
        path.write_text(text, encoding='utf-8', newline='\n')  # the same bytes on Windows as elsewhere
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
