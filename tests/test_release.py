from contextlib import redirect_stderr, redirect_stdout
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
import release


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


SYNC_WITH_CHANGES = 'print("Managed changes: 1; blocking issues: 0.")\nraise SystemExit(1)\n'
PASSING_TEST = 'import unittest\n\n\nclass Seed(unittest.TestCase):\n    def test_passes(self):\n        pass\n'
FAILING_TEST = PASSING_TEST.replace('        pass', '        self.fail("broken")')
ALLOWLIST = '/*\n!/.gitignore\n!/content\n!/doc\n!/sync.py\n!/tests/\n!/tests/**\n__pycache__/\n.sync/\n'
# finish runs the suite through the real runner, so the fixture's origin carries it.
RUNNER = (Path(release.__file__).resolve().parent / 'tests/run.py').read_text(encoding='utf-8')
SEEDED_TESTS = 'tests/run.py\ntests/test_seed.py'


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        tailnet = patch.object(release, 'tailnet', return_value=None)  # no test asks the machine's own Tailscale
        tailnet.start()
        self.addCleanup(tailnet.stop)
        self.root = Path(self.temp.name)
        self.key = self.root / 'release'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'release', '-f', str(self.key)], check=True)
        # The release's device check runs the worktree's sync.py, which exits 1 when it has changes to apply.
        self.public_origin = self.origin('public-origin', {'content': 'first\n', 'doc': 'one\ntwo\nthree\nfour\nfive\n',
                                                           'sync.py': SYNC_WITH_CHANGES, 'tests/run.py': RUNNER,
                                                           'tests/test_seed.py': PASSING_TEST,
                                                           '.gitignore': ALLOWLIST})
        first = git(self.public_origin, 'rev-parse', 'main')
        self.private_origin = self.origin('private-origin', {'release.toml': f'public_commit = "{first}"\n'})
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

    def test_finish_pairs_on_a_moved_private_main(self):
        self.assertEqual(release_main(['start', 'change', *self.options()], self.public), 0)
        (self.worktrees / 'change/public/content').write_text('second\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        other = self.clone(self.private_origin, 'other-session')
        (other / 'notes').write_text('meanwhile\n', encoding='utf-8')
        git(other, 'add', '-A')
        git(other, 'commit', '-qm', 'Another session')
        git(other, 'push', '-q', 'origin', 'HEAD:main')

        self.assertEqual(release_main(['finish', 'change', '--message', str(message), '--push', *self.options()],
                                      self.public), 0)

        public_head = git(self.public_origin, 'rev-parse', 'main')
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'second')
        self.assertEqual(git(self.private_origin, 'show', 'main:release.toml'), f'public_commit = "{public_head}"')
        self.assertEqual(git(self.private_origin, 'show', 'main:notes'), 'meanwhile')
        self.assertEqual(git(self.private_origin, 'log', '-1', '--format=%s', 'main'), 'Pair "Change the content"')
        for origin in (self.public_origin, self.private_origin):
            self.assertIn('gpgsig -----BEGIN SSH SIGNATURE-----', git(origin, 'cat-file', 'commit', 'main'))

        self.assertEqual(release_main(['clean', 'change', *self.options()], self.public), 0)
        self.assertFalse((self.worktrees / 'change').exists())
        self.assertEqual(git(self.public, 'branch', '--list', 'change'), '')

    def test_finish_takes_the_pair_message_from_a_file_like_the_other_messages(self):
        release_main(['start', 'worded', *self.options()], self.public)
        (self.worktrees / 'worded/public/content').write_text('second\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        pair_message = self.root / 'pair-message'
        pair_message.write_text('Pair the content change\n\nWith a body.\n', encoding='utf-8')

        self.assertEqual(release_main(['finish', 'worded', '--message', str(message), '--pair-message', str(pair_message),
                                       '--push', *self.options()], self.public), 0)

        self.assertEqual(git(self.private_origin, 'log', '-1', '--format=%B', 'main'),
                         'Pair the content change\n\nWith a body.')

    def test_clean_keeps_an_unpushed_branch(self):
        release_main(['start', 'draft', *self.options()], self.public)
        (self.worktrees / 'draft/public/content').write_text('unpushed\n', encoding='utf-8')
        git(self.worktrees / 'draft/public', 'commit', '-qam', 'Unpushed')
        self.assertEqual(release_main(['clean', 'draft', *self.options()], self.public), 3)
        self.assertTrue((self.worktrees / 'draft/public').exists())

    def test_pushes_a_github_ssh_origin_over_https(self):
        git(self.private, 'remote', 'set-url', 'origin', 'git@github.com:Owner/private.git')
        self.assertEqual(release.push_url(self.private), 'https://github.com/Owner/private.git')

    def test_finish_stops_on_a_file_origin_also_changed_until_reviewed(self):
        release_main(['start', 'edit', *self.options()], self.public)
        (self.worktrees / 'edit/public/doc').write_text('ONE\ntwo\nthree\nfour\nfive\n', encoding='utf-8')
        other = self.clone(self.public_origin, 'other-public')
        (other / 'doc').write_text('one\ntwo\nthree\nfour\nFIVE\n', encoding='utf-8')
        git(other, 'commit', '-qam', 'Another session')
        git(other, 'push', '-q', 'origin', 'HEAD:main')
        message = self.root / 'message'
        message.write_text('Edit the doc\n', encoding='utf-8')
        finish = ['finish', 'edit', '--message', str(message), '--push', *self.options()]
        self.assertEqual(release_main(finish, self.public), 3)
        self.assertEqual(git(self.public_origin, 'show', 'main:doc'), 'one\ntwo\nthree\nfour\nFIVE')
        self.assertEqual(release_main([*finish, '--overlap-reviewed'], self.public), 0)
        self.assertEqual(git(self.public_origin, 'show', 'main:doc'), 'ONE\ntwo\nthree\nfour\nFIVE')

    def test_a_refused_push_publishes_neither_half(self):
        release_main(['start', 'refused', *self.options()], self.public)
        (self.worktrees / 'refused/public/content').write_text('second\n', encoding='utf-8')
        git(self.private, 'remote', 'set-url', '--push', 'origin', str(self.root / 'missing.git'))
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        self.assertEqual(release_main(['finish', 'refused', '--message', str(message), '--push', *self.options()],
                                      self.public), 3)
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'first')

    def test_finish_stops_before_pushing_a_release_the_pull_would_stop_on(self):
        release_main(['start', 'broken', *self.options()], self.public)
        (self.worktrees / 'broken/public/sync.py').write_text(
            'import sys\nprint("Sync stopped: unconfirmed alias", file=sys.stderr)\nraise SystemExit(3)\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Break the pull\n', encoding='utf-8')
        self.assertEqual(release_main(['finish', 'broken', '--message', str(message), '--push', *self.options()],
                                      self.public), 3)
        self.assertEqual(git(self.public_origin, 'show', 'main:sync.py'), SYNC_WITH_CHANGES.strip())
        self.assertEqual(git(self.private_origin, 'log', '--format=%s', 'main'), 'seed')

    def test_finish_runs_the_whole_suite_and_stops_on_a_failure(self):
        release_main(['start', 'failing', *self.options()], self.public)
        (self.worktrees / 'failing/public/tests/test_other.py').write_text(
            FAILING_TEST, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a failing test\n', encoding='utf-8')
        self.assertEqual(release_main(['finish', 'failing', '--message', str(message), '--push', *self.options()],
                                      self.public), 3)
        self.assertIn('broken', (self.worktrees / 'failing/build/tests.log').read_text(encoding='utf-8'))
        self.assertEqual(git(self.public_origin, 'ls-tree', '--name-only', 'main', 'tests/'), SEEDED_TESTS)

    def test_finish_runs_the_suite_against_missing_config_roots(self):
        release_main(['start', 'isolated', *self.options()], self.public)
        (self.worktrees / 'isolated/public/tests/test_config.py').write_text(
            'import os\nfrom pathlib import Path\nimport unittest\n\n\nclass Config(unittest.TestCase):\n'
            '    def test_roots_are_missing(self):\n'
            '        for variable in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):\n'
            '            self.assertFalse(Path(os.environ[variable]).exists())\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Check the config roots\n', encoding='utf-8')
        self.assertEqual(release_main(['finish', 'isolated', '--message', str(message), '--push', *self.options()],
                                      self.public), 0)

    def test_finish_runs_the_private_tests_and_stops_on_a_failure(self):
        release_main(['start', 'private', *self.options()], self.public)
        folder = self.worktrees / 'private/private/config/tool'
        folder.mkdir(parents=True)
        (folder / 'helper.py').write_text('VALUE = 1\n', encoding='utf-8')
        (folder / 'test_tool.py').write_text(FAILING_TEST, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a private tool\n', encoding='utf-8')
        finish = ['finish', 'private', '--private-message', str(message), '--push', *self.options()]
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertIn('the private tests in config/tool failed', error.getvalue())
        self.assertIn('broken', (self.worktrees / 'private/build/private-tests.log').read_text(encoding='utf-8'))
        self.assertEqual(git(self.private_origin, 'log', '--format=%s', 'main'), 'seed')
        # Each folder is its own top level, as its tests run alone, so a test imports the helpers beside it.
        (folder / 'test_tool.py').write_text(
            'import os\nfrom pathlib import Path\nimport unittest\n\nimport helper\n\n\nclass Tool(unittest.TestCase):\n'
            '    def test_imports_its_helper_against_missing_config_roots(self):\n'
            '        self.assertEqual(helper.VALUE, 1)\n'
            '        for variable in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):\n'
            '            self.assertFalse(Path(os.environ[variable]).exists())\n', encoding='utf-8')
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(finish, self.public), 0)
        self.assertIn('Ran 1 private tests in 1 folders: OK', output.getvalue())
        self.assertEqual(git(self.private_origin, 'ls-tree', '-r', '--name-only', 'main', 'config/'),
                         'config/tool/helper.py\nconfig/tool/test_tool.py')

    def test_finish_stops_on_a_public_line_the_private_denylist_names(self):
        release_main(['start', 'leak', *self.options()], self.public)
        tree, private = self.worktrees / 'leak/public', self.worktrees / 'leak/private'
        (private / 'config/privacy').mkdir(parents=True)
        (private / 'config/privacy/public-denylist.txt').write_text('# hosts\nsecret-host\n', encoding='utf-8')
        (tree / 'content').write_text('first\nreach Secret-Host over ssh\n', encoding='utf-8')
        (tree / 'notes.md').write_text('secret-host, but the allowlist keeps it out\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Document the host\n', encoding='utf-8')
        finish = ['finish', 'leak', '--message', str(message), '--private-message', str(message), *self.options()]
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertIn('content:2 in ', error.getvalue())
        self.assertNotIn('notes.md', error.getvalue())
        self.assertEqual(git(tree, 'rev-parse', 'HEAD'), git(self.public_origin, 'rev-parse', 'main'))
        (tree / 'notes.md').unlink()
        (tree / 'content').write_text('second\n', encoding='utf-8')
        message.write_text('Name secret-host here\n', encoding='utf-8')
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertIn('the public commit message in ', error.getvalue())
        message.write_text('Change the content\n', encoding='utf-8')
        self.assertEqual(release_main(finish, self.public), 0)

    def test_finish_reports_the_suite_result_and_its_log(self):
        message = self.root / 'message'
        message.write_text('Change the tree\n', encoding='utf-8')
        for name, changed, text, expected in (
                ('data', 'content', 'second\n', 'The public test suite did not run'),
                ('code', 'tests/test_other.py', PASSING_TEST, 'Ran 2 tests in')):
            with self.subTest(name):
                release_main(['start', name, *self.options()], self.public)
                (self.worktrees / name / 'public' / changed).write_text(text, encoding='utf-8')
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(release_main(['finish', name, '--message', str(message), *self.options()],
                                                  self.public), 0)
                self.assertIn(expected, output.getvalue())
                self.assertEqual(str(self.worktrees / name / 'build/tests.log') in output.getvalue(), name == 'code')

    def test_the_public_suite_runs_only_for_a_release_that_changes_code(self):
        seed = self.clone(self.public_origin, 'code-seed')
        for relative, text in {'tests/test_broken.py': FAILING_TEST,
                               'code/old.py': 'value = 1\n', 'uv.lock': 'old\n', 'pyproject.toml': 'old\n',
                               'config/mise/config.toml': 'old\n', '.github/workflows/tests.yml': 'old\n',
                               '.gitignore': ALLOWLIST + '!/code/\n!/uv.lock\n!/pyproject.toml\n!/config/\n!/.github/\n',
                               }.items():
            (seed / relative).parent.mkdir(parents=True, exist_ok=True)
            (seed / relative).write_text(text, encoding='utf-8')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-qm', 'Seed a failing module')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')
        release_main(['start', 'scope', *self.options()], self.public)
        public, private = self.worktrees / 'scope/public', self.worktrees / 'scope/private'

        def write(root, relative):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text('changed\n', encoding='utf-8')

        write(public, 'content')
        log = self.root / 'logs/data.log'
        with redirect_stdout(io.StringIO()) as output:
            release.test_suite(public, private, log)
        self.assertFalse(log.exists())
        self.assertIn('The public test suite did not run', output.getvalue())

        for name, change in {
                'new python file': lambda: write(public, 'code/new.py'),
                'non-ascii python file': lambda: write(public, 'code/æ.py'),
                'deleted python file': lambda: (public / 'code/old.py').unlink(),
                'python file renamed away': lambda: git(public, 'mv', 'code/old.py', 'code/old.txt'),
                'private python file': lambda: write(private, 'tool.py'),
                'tests folder': lambda: write(public, 'tests/data.txt'),
                'uv.lock': lambda: write(public, 'uv.lock'),
                'pyproject.toml': lambda: write(public, 'pyproject.toml'),
                'config/mise': lambda: write(public, 'config/mise/config.toml'),
                '.github/workflows': lambda: write(public, '.github/workflows/tests.yml')}.items():
            with self.subTest(name):
                for root in (public, private):
                    git(root, 'checkout', '-q', '--no-overlay', 'HEAD', '--', '.')
                    git(root, 'clean', '-fdq')
                change()
                log = self.root / 'logs' / f'{name.replace("/", "-")}.log'
                with self.assertRaises(RuntimeError):
                    release.test_suite(public, private, log)
                self.assertIn('broken', log.read_text(encoding='utf-8'))

    def test_finish_stops_on_a_new_file_the_allowlist_leaves_out(self):
        release_main(['start', 'notes', *self.options()], self.public)
        tree = self.worktrees / 'notes/public'
        (tree / 'notes.md').write_text('left out\n', encoding='utf-8')
        (tree / 'tests/__pycache__').mkdir()
        (tree / 'tests/__pycache__/cache.pyc').write_bytes(b'cache')
        message = self.root / 'message'
        message.write_text('Add notes\n', encoding='utf-8')
        finish = ['finish', 'notes', '--message', str(message), '--push', *self.options()]
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertIn('.gitignore leaves out notes.md;', error.getvalue())
        (tree / 'notes.md').unlink()
        (tree / 'content').write_text('second\n', encoding='utf-8')
        self.assertEqual(release_main(finish, self.public), 0)
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'second')

    def test_finish_stops_on_a_suite_that_writes_into_the_engine_cache(self):
        release_main(['start', 'cache', *self.options()], self.public)
        (self.worktrees / 'cache/public/tests/test_cache.py').write_text(
            'from pathlib import Path\nimport unittest\n\n\nclass Cache(unittest.TestCase):\n'
            '    def test_writes_the_cache(self):\n'
            '        Path(".sync/checkouts").mkdir(parents=True)\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Write the cache\n', encoding='utf-8')
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'cache', '--message', str(message), '--push', *self.options()],
                                          self.public), 3)
        self.assertIn('the test suite wrote into', error.getvalue())
        self.assertEqual(git(self.public_origin, 'ls-tree', '--name-only', 'main', 'tests/'), SEEDED_TESTS)

    def test_finish_stops_when_the_device_check_crashes(self):
        release_main(['start', 'crash', *self.options()], self.public)
        (self.worktrees / 'crash/public/sync.py').write_text('raise KeyError("models")\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Crash the pull\n', encoding='utf-8')
        self.assertEqual(release_main(['finish', 'crash', '--message', str(message), '--push', *self.options()],
                                      self.public), 3)
        self.assertEqual(git(self.public_origin, 'show', 'main:sync.py'), SYNC_WITH_CHANGES.strip())

    def test_finish_stops_on_a_plugin_with_trusted_hooks_whose_content_may_change_until_reapproved(self):
        seed = self.clone(self.private_origin, 'private-seed')
        (seed / 'setup').mkdir()
        (seed / 'setup/lock.toml').write_text(
            '[hooked]\ncodex = { version = "1.0.0", tree = "sha256:aa" }\nhooks = 2\n\n'
            '[plain]\ncodex = { version = "1.0.0", tree = "sha256:cc" }\n', encoding='utf-8')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-qm', 'Lock the plugins')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')
        release_main(['start', 'hooks', *self.options()], self.public)
        lock = self.worktrees / 'hooks/private/setup/lock.toml'
        lock.write_text(lock.read_text(encoding='utf-8').replace('aa', 'bb').replace('cc', 'dd'), encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change both plugins\n', encoding='utf-8')
        finish = ['finish', 'hooks', '--private-message', str(message), '--push', *self.options()]
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertIn('may change the Codex content of hooked, whose Codex hooks are trusted', error.getvalue())
        self.assertIn('"sha256:aa"', git(self.private_origin, 'show', 'main:setup/lock.toml'))
        self.assertEqual(release_main([*finish, '--hooks-reapproved'], self.public), 0)
        self.assertIn('"sha256:bb"', git(self.private_origin, 'show', 'main:setup/lock.toml'))

    def test_finish_stops_on_a_selection_that_changes_under_the_same_release_name(self):
        self.seed_package(package_toml('a' * 40, ('./one', './two'), 2))
        release_main(['start', 'select', *self.options()], self.public)
        package = self.worktrees / 'select/public' / PACKAGE
        package.write_text(package_toml('a' * 40, ('./one',), 2), encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Drop a skill\n', encoding='utf-8')
        finish = ['finish', 'select', '--message', str(message), '--push', *self.options()]
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertIn('plugin changed its selection', error.getvalue())
        self.assertIn('Raise packaging by one', error.getvalue())
        self.assertIn('./two', git(self.public_origin, 'show', f'main:{PACKAGE}'))
        package.write_text(package_toml('a' * 40, ('./one',), 3), encoding='utf-8')
        self.assertEqual(release_main(finish, self.public), 0)
        self.assertNotIn('./two', git(self.public_origin, 'show', f'main:{PACKAGE}'))

    def test_finish_keeps_a_new_package_and_a_selection_at_a_new_commit(self):
        self.seed_package(package_toml('a' * 40, ('./one', './two'), 2))
        release_main(['start', 'moved', *self.options()], self.public)
        worktree = self.worktrees / 'moved/public'
        (worktree / PACKAGE).write_text(package_toml('b' * 40, ('./one',), 2), encoding='utf-8')
        (worktree / PACKAGE).with_name('fresh.toml').write_text(package_toml('c' * 40, ('./one',), 1), encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Move the pin\n', encoding='utf-8')
        self.assertEqual(release_main(['finish', 'moved', '--message', str(message), '--push', *self.options()],
                                      self.public), 0)
        self.assertIn('b' * 40, git(self.public_origin, 'show', f'main:{PACKAGE}'))

    def test_finish_pushes_and_names_claude_sessions_holding_a_plugin_it_replaces(self):
        release_main(['start', 'held', *self.options()], self.public)
        held = 'print("Claude lease: plugin:claude:kit@m at /cache/kit/1.0.0, held by running Claude processes 4242")\n'
        (self.worktrees / 'held/public/sync.py').write_text(held + SYNC_WITH_CHANGES, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Hold the plugin\n', encoding='utf-8')
        finish = ['finish', 'held', '--message', str(message), '--push', *self.options()]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(finish, self.public), 0)
        self.assertIn('Claude lease: plugin:claude:kit@m at /cache/kit/1.0.0, held by running Claude processes 4242; '
                      'the pull here swaps it at once if only skill text changes', output.getvalue())
        self.assertEqual(git(self.public_origin, 'show', 'main:sync.py'), (held + SYNC_WITH_CHANGES).strip())

    def test_a_pushed_finish_says_which_of_the_users_other_machines_are_online(self):
        status = {'Self': {'UserID': 7, 'HostName': 'here', 'OS': 'linux'}, 'Peer': {
            'a': {'UserID': 7, 'HostName': 'Work-PC', 'OS': 'windows', 'Online': True, 'LastSeen': '0001-01-01T00:00:00Z'},
            'b': {'UserID': 7, 'HostName': 'laptop', 'OS': 'macOS', 'Online': False, 'LastSeen': '2026-10-29T08:00:00Z'},
            'c': {'UserID': 7, 'HostName': 'phone', 'OS': 'iOS', 'Online': True},
            'd': {'UserID': 9, 'HostName': 'exit-node', 'OS': '', 'Online': True}}}
        self.assertEqual(release.other_machines(status), [
            'Other machine laptop (macOS): offline, last seen 2026-10-29, so queue its steps in '
            'maintenance/pending-sync.md',
            'Other machine Work-PC (windows): online now, so pull there'])
        self.assertIn('check each machine before you say it is unreachable', release.other_machines(None)[0])
        release_main(['start', 'told', *self.options()], self.public)
        (self.worktrees / 'told/public/content').write_text('told\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Tell the machines\n', encoding='utf-8')
        for flags, told in ((['--push'], True), ([], False)):
            with self.subTest(flags=flags), patch.object(release, 'tailnet', return_value=status), \
                    redirect_stdout(io.StringIO()) as output:
                release_main(['finish', 'told', '--message', str(message), *flags, *self.options()], self.public)
            self.assertEqual('Other machine Work-PC (windows): online now' in output.getvalue(), told)

    def test_a_rebase_takes_origins_copy_of_a_regenerated_file_only_where_it_alone_conflicts(self):
        lock = 'setup/lock.toml'
        regenerated = frozenset({lock})

        def commit(root, files, subject, push=False):
            for relative, text in files.items():
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_text(text, encoding='utf-8')
            git(root, 'add', '-A')
            git(root, 'commit', '-qm', subject)
            if push:
                git(root, 'push', '-q', 'origin', 'HEAD:main')

        commit(self.clone(self.private_origin, 'seed'), {lock: '[kit]\ntree = "a"\n'}, 'Lock', push=True)
        mine = self.clone(self.private_origin, 'mine')
        commit(mine, {lock: '[kit]\ntree = "b"\n', 'notes': 'mine\n'}, 'Change the kit')
        lock_only = self.clone(self.private_origin, 'lock-only')
        commit(lock_only, {lock: '[kit]\ntree = "d"\n'}, 'Relock the kit')
        commit(self.clone(self.private_origin, 'theirs'), {lock: '[kit]\ntree = "c"\n'}, 'Their kit', push=True)

        before = git(mine, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(RuntimeError, 'rebase it by hand'):
            release.rebase(mine)
        self.assertEqual(git(mine, 'rev-parse', 'HEAD'), before)
        self.assertEqual(git(mine, 'status', '--porcelain'), '')
        with self.assertRaisesRegex(RuntimeError, 'rebase it by hand'):  # nothing of its own would be left
            release.rebase(lock_only, regenerated)
        self.assertEqual(git(lock_only, 'status', '--porcelain'), '')

        self.assertEqual(release.rebase(mine, regenerated), 'regenerate')
        self.assertEqual((mine / lock).read_text(encoding='utf-8'), '[kit]\ntree = "c"\n')
        self.assertEqual((mine / 'notes').read_text(encoding='utf-8'), 'mine\n')
        self.assertEqual(git(mine, 'log', '--format=%B', '-1'), 'Change the kit')
        self.assertEqual(git(mine, 'log', '--format=%s', '-3').splitlines(), ['Change the kit', 'Their kit', 'Lock'])
        self.assertIn('gpgsig -----BEGIN SSH SIGNATURE-----', git(mine, 'cat-file', 'commit', 'HEAD'))
        self.assertEqual(release.rebase(mine, regenerated), '')

    def test_finish_stops_while_another_release_finishes_on_this_machine(self):
        import portable
        release_main(['start', 'second', *self.options()], self.public)
        (self.worktrees / 'second/public/content').write_text('second\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        finish = ['finish', 'second', '--message', str(message), '--push', *self.options()]
        with portable.exclusive_lock(self.root / 'state/release.lock', record=True):
            with redirect_stderr(io.StringIO()) as error:
                self.assertEqual(release_main(finish, self.public), 3)
        self.assertIn(f'another release is finishing on this machine (release PID {os.getpid()} since', error.getvalue())
        self.assertIn('release.py wait --timeout SECONDS', error.getvalue())
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'first')
        self.assertEqual(release_main(finish, self.public), 0)
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'second')

    def test_wait_returns_once_no_release_pull_or_sync_runs_here(self):
        import portable
        with portable.exclusive_lock(self.root / 'state/pull.lock', record=True):
            with redirect_stderr(io.StringIO()) as error:
                self.assertEqual(release_main(['wait', '--timeout', '0'], self.public), 3)
        self.assertIn(f'pull PID {os.getpid()} since', error.getvalue())
        child = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'],
                               capture_output=True, text=True, check=True)
        (self.root / 'state/pull.lock.holder').write_text(json.dumps({'pid': int(child.stdout), 'command': 'pull.py'}))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(['wait', '--timeout', '5'], self.public), 0)
        self.assertIn('No release, pull or sync runs on this machine.', output.getvalue())

    def test_status_and_start_list_the_open_releases(self):
        release_main(['start', 'first', *self.options()], self.public)
        (self.worktrees / 'first/public/content').write_text('edited\n', encoding='utf-8')
        (self.worktrees / 'first/public/tests/notes.py').write_text('', encoding='utf-8')
        with redirect_stdout(io.StringIO()) as output:
            release_main(['start', 'later', *self.options()], self.public)
        self.assertIn('Other open releases; read them before you edit the same files:\n'
                      '  first/public: 2 uncommitted: content, tests/notes.py', output.getvalue())
        self.assertNotIn('first/private', output.getvalue())
        # Without the machine's own locks: a pull that runs here during the suite would be listed.
        with patch.object(release, 'running', return_value=[]), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(['status', *self.options()], self.public), 0)
        self.assertEqual(output.getvalue().splitlines(), [
            'Running on this machine: no release, pull or sync',
            f'Release worktrees under {self.worktrees}:',
            '  first/private: on origin/main and clean',
            '  first/public: 2 uncommitted: content, tests/notes.py',
            '  later/private: on origin/main and clean',
            '  later/public: on origin/main and clean'])

    def test_finish_stops_before_the_suite_on_an_owned_source_edited_without_its_pin(self):
        release_main(['start', 'owned', *self.options()], self.public)
        (self.worktrees / 'owned/public/tests/test_seed.py').write_text(FAILING_TEST, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Edit an owned skill\n', encoding='utf-8')
        finish = ['finish', 'owned', '--message', str(message), *self.options()]
        with patch.object(release, 'edited_owned', return_value={'mine': 'skills/mine', 'other': 'skills/other'}):
            for named in ([], ['--entry', 'mine'], ['--entry', 'mine', '--refresh-owned']):
                with self.subTest(named=named), redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(release_main([*finish, *named], self.public), 3)
                    self.assertIn('changed after its tree was pinned', error.getvalue())
                    self.assertIn('--entry other --refresh-owned', error.getvalue())
                    self.assertNotIn('test suite failed', error.getvalue())  # stopped before the failing suite ran
        self.assertEqual(release.unpinned({'mine': 'skills/mine'}, ['mine'], True), {})

    def test_a_tree_without_a_setup_description_has_no_owned_sources(self):
        self.assertEqual(release.edited_owned(self.public, self.private), {})

    def test_a_detached_finish_runs_the_same_arguments_as_a_unit_and_result_reads_how_it_ended(self):
        release_main(['start', 'away', *self.options()], self.public)
        args = release.argparse.Namespace(name='away', worktrees=self.worktrees, timeout=0.1)
        argv = ['finish', 'away', '--detach', '--message', '/tmp/m', '--push']
        command = release.detach_command(args, argv, {'PATH': '/bin', 'HOME': '/home/x'})
        log, status = self.worktrees / 'away/build/finish.log', self.worktrees / 'away/build/finish.exit'
        self.assertEqual(command[:4], ['systemd-run', '--user', '--collect', '--unit=dotfiles-release-away'])
        self.assertEqual(command[4:8], ['--setenv', 'PATH=/bin', '--setenv', 'HOME=/home/x'])  # no SSH agent to pass
        self.assertIn(f'--property=StandardOutput=append:{log}', command)
        self.assertEqual(command[-7:], ['finish', 'away', '--message', '/tmp/m', '--push', '--report', str(status)])
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['result', 'away', *self.options()], self.public), 3)
        self.assertIn('no detached finish of away was started', error.getvalue())
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'away', '--detach', '--message', 'm.txt', *self.options()],
                                          self.public), 3)
        self.assertIn('--detach needs an absolute path for m.txt', error.getvalue())
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('Ran 3 tests: OK\nRelease stopped: nothing to release\n', encoding='utf-8')
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['result', 'away', '--timeout', '0.1', *self.options()], self.public), 4)
        self.assertIn('Still running', error.getvalue())
        status.write_text('3\n', encoding='utf-8')
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(['result', 'away', *self.options()], self.public), 3)
        self.assertIn('Release stopped: nothing to release', output.getvalue())
        self.assertIn('finish away exited 3', output.getvalue())

    def test_a_finish_started_by_a_unit_reports_its_status_to_the_file_result_reads(self):
        release_main(['start', 'unit', *self.options()], self.public)
        status = self.root / 'finish.exit'
        with redirect_stderr(io.StringIO()):
            self.assertEqual(release_main(['finish', 'unit', '--report', str(status), *self.options()], self.public), 3)
        self.assertEqual(status.read_text(encoding='utf-8'), '3\n')  # nothing to release

    def test_finish_without_a_message_stops_before_committing(self):
        release_main(['start', 'quiet', *self.options()], self.public)
        (self.worktrees / 'quiet/public/content').write_text('edited\n', encoding='utf-8')
        self.assertEqual(release_main(['finish', 'quiet', *self.options()], self.public), 3)
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'first')

    def options(self):
        return ['--worktrees', str(self.worktrees), '--private', str(self.private)]

    def seed_package(self, text):
        seed = self.clone(self.public_origin, 'public-seed')
        (seed / PACKAGE).parent.mkdir(parents=True)
        (seed / PACKAGE).write_text(text, encoding='utf-8')
        (seed / '.gitignore').write_text(ALLOWLIST + '!/setup/\n!/setup/**\n', encoding='utf-8')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-qm', 'Add a package')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')


PACKAGE = 'setup/packages/plugin.toml'


def package_toml(commit, select, packaging):
    chosen = ', '.join(f'"{path}"' for path in select)
    return f'[source]\nbuild = "generic"\ncommit = "{commit}"\npackaging = {packaging}\nselect = [{chosen}]\n'


def release_main(argv, engine):
    """release.main with ENGINE set to the test's canonical public clone, and this machine's locks in its folder."""
    original = release.ENGINE, release.STATE
    release.ENGINE, release.STATE = engine, Path(engine).parent / 'state'
    try:
        return release.main(argv)
    finally:
        release.ENGINE, release.STATE = original


if __name__ == '__main__':
    unittest.main()
