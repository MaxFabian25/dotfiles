from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402


def skip_on_windows():
    if sys.platform == 'win32':
        raise unittest.SkipTest('releases are made on WSL or Omarchy, never on Windows itself')


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


def sync_with_changes(*held):
    """A worktree's sync.py whose candidate check finds one change to apply and the held Claude caches."""
    result = json.dumps({'blocking': 0, 'held': list(held)})
    return f'print({result!r})\nraise SystemExit(1)\n'


SYNC_WITH_CHANGES = sync_with_changes()
PASSING_TEST = 'import unittest\n\n\nclass Seed(unittest.TestCase):\n    def test_passes(self):\n        pass\n'
FAILING_TEST = PASSING_TEST.replace('        pass', '        self.fail("broken")')
RUN_ALONE = '\n\nif __name__ == "__main__":\n    unittest.main()\n'
ALLOWLIST = '/*\n!/.gitignore\n!/content\n!/doc\n!/sync.py\n!/tests/\n!/tests/**\n__pycache__/\n.sync/\n'
# finish runs the suite through the real runner, so the fixture's origin carries it.
RUNNER = (Path(release.__file__).resolve().parent / 'tests/run.py').read_text(encoding='utf-8')
SEEDED_TESTS = 'tests/run.py\ntests/test_seed.py'
# A stand-in for lock_setup.py. It writes each setup's lock, as a relock of a changed entry does, with a tree named
# for the release, so that each release's relock changes the file, and with --refresh-owned the kit entry's pin.
# In the release named stray its second run, the relock after the closing rebase, also leaves a file of its own,
# and in the one named moved that run pins the kit anew.
KIT = 'setup/skills/kit.toml'
RELOCK_STAND_IN = r'''import re
import sys
from pathlib import Path
argv = sys.argv
output = Path(argv[argv.index("--output") + 1])
output.mkdir(parents=True, exist_ok=True)
runs = len(list(output.glob("run-*")))
(output / f"run-{runs}").write_text("", encoding="utf-8")
for option in ("--public", "--private"):
    setup = Path(argv[argv.index(option) + 1])
    name = setup.parent.parent.name
    setup.mkdir(exist_ok=True)
    (setup / "lock.toml").write_text(f'[kit]\ntree = "{name}"\n', encoding="utf-8")
    kit = setup / "skills/kit.toml"
    if "--refresh-owned" in argv and kit.is_file():
        pin = f"{name}-again" if name == "moved" and runs else name
        kit.write_text(re.sub(r'tree = "[^"]*"', f'tree = "{pin}"', kit.read_text(encoding="utf-8")), encoding="utf-8")
    if name == "stray" and runs and option == "--public":
        (setup / "stray.toml").write_text("made after the commit\n", encoding="utf-8")
'''


class ProcessReplaced(BaseException):
    pass


class ReleaseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        tailnet = patch.object(release, 'tailnet', return_value=None)  # no test asks the machine's own Tailscale
        tailnet.start()
        self.addCleanup(tailnet.stop)
        environment = patch.dict(os.environ)  # start notes the T3 thread it runs in, so no test inherits one
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop('T3CODE_THREAD_ID', None)
        # A finish that starts again replaces its process, which here is the test run itself. A test of that names
        # its own stand-in.
        restart = patch.object(release.os, 'execv', side_effect=ProcessReplaced('a finish started again'))
        restart.start()
        self.addCleanup(restart.stop)
        self.root = Path(self.temp.name).resolve()  # as finish names its paths; macOS's temp folder is a link
        self.key = self.root / 'release'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'release', '-f', str(self.key)], check=True)
        # The release's device check runs the worktree's sync.py, which exits 1 when it has changes to apply.
        self.public_origin = self.origin('public-origin', {'content': 'first\n', 'doc': 'one\ntwo\nthree\nfour\nfive\n',
                                                           'sync.py': SYNC_WITH_CHANGES, 'tests/run.py': RUNNER,
                                                           'tests/test_seed.py': PASSING_TEST,
                                                           '.gitignore': ALLOWLIST})
        first = git(self.public_origin, 'rev-parse', 'main')
        self.private_origin = self.origin('private-origin', {'release.toml': f'public_commit = "{first}"\n',
                                                             release.DENYLIST: '# hosts\nseeded-host\n'})
        self.public = self.clone(self.public_origin, 'dotfiles')
        self.private = self.clone(self.private_origin, 'dotfiles-private')
        self.worktrees = self.root / 'worktrees'

    def sign(self, root):
        for key, value in (('user.email', 'test@example.invalid'), ('user.name', 'Test'), ('gpg.format', 'ssh'),
                           ('user.signingkey', str(self.key)), ('commit.gpgsign', 'true')):
            git(root, 'config', key, value)

    def origin(self, name, files):
        bare = self.root / name
        git(self.root, 'init', '-q', '--bare', '-b', 'main', str(bare))
        seed = self.clone(bare, f'{name}-seed')
        for relative, text in files.items():
            (seed / relative).parent.mkdir(parents=True, exist_ok=True)
            (seed / relative).write_text(text, encoding='utf-8')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-qm', 'seed')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')
        return bare

    def clone(self, source, name):
        path = self.root / name
        git(self.root, 'clone', '-q', '-c', 'core.autocrlf=false', str(source), str(path))
        self.sign(path)
        return path

    def options(self):
        return ['--worktrees', str(self.worktrees), '--private', str(self.private)]

    def push_from_another_session(self, origin, name, files):
        other = self.clone(origin, name)
        for relative, text in files.items():
            (other / relative).parent.mkdir(parents=True, exist_ok=True)
            (other / relative).write_text(text, encoding='utf-8')
        git(other, 'add', '-A')
        git(other, 'commit', '-qm', 'Another session')
        git(other, 'push', '-q', 'origin', 'HEAD:main')

    def another_session(self, origin, path, text):
        other = self.clone(origin, f'other-session-{len(list(self.root.glob("other-session-*")))}')
        (other / path).write_text(text, encoding='utf-8')
        git(other, 'add', '-A')
        git(other, 'commit', '-qm', 'Another session')
        git(other, 'push', '-q', 'origin', 'HEAD:main')

    def stopped_finish(self, name, *extra):
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        before = git(self.public_origin, 'rev-parse', 'main'), git(self.private_origin, 'rev-parse', 'main')
        with redirect_stderr(io.StringIO()) as error:
            status = release_main(['finish', name, '--message', str(message), '--push', *extra, *self.options()],
                                  self.public)
        self.assertEqual(status, 3, error.getvalue())
        self.assertEqual((git(self.public_origin, 'rev-parse', 'main'), git(self.private_origin, 'rev-parse', 'main')),
                         before)
        return error.getvalue()

    def origin_heads(self):
        return git(self.public_origin, 'rev-parse', 'main'), git(self.private_origin, 'rev-parse', 'main')


PACKAGE = 'setup/packages/plugin.toml'


def package_toml(commit, select, packaging):
    chosen = ', '.join(f'"{path}"' for path in select)
    return f'[source]\nbuild = "generic"\ncommit = "{commit}"\npackaging = {packaging}\nselect = [{chosen}]\n'


def release_main(argv, engine, review=True):
    """release.main with ENGINE set to the test's canonical public clone, and this machine's locks in its folder.
    A finish that changes code gets the review record it needs unless review is False, so that each test meets
    only the stop it is about."""
    original = release.ENGINE, release.STATE, release.codex_home
    # The sync lock lives in Codex's home, so without its own one a real sync here reads as a run to wait for.
    release.ENGINE, release.STATE = engine, Path(engine).parent / 'state'
    release.codex_home = lambda: Path(engine).parent / 'codex'
    try:
        if review and argv[0] == 'finish' and '--reviewed' not in argv:
            base = Path(argv[argv.index('--worktrees') + 1]) / argv[1]
            if (base / 'public').is_dir():
                wanted = release.review_lines(base / 'public', base / 'private')
                if wanted:
                    record = Path(engine).parent / f'review-{argv[1]}.md'
                    record.write_text('No findings.\n' + ''.join(f'{line}\n' for _, line in wanted.values()),
                                      encoding='utf-8')
                    argv = [*argv, '--reviewed', str(record)]
        return release.main(argv)
    finally:
        release.ENGINE, release.STATE, release.codex_home = original
