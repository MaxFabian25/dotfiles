from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402
from release_case import (ALLOWLIST, FAILING_TEST, PACKAGE, PASSING_TEST, ReleaseCase, SEEDED_TESTS,
                          SYNC_WITH_CHANGES, git, package_toml, release_main, sync_with_changes)  # noqa: E402
from release_case import skip_on_windows as setUpModule  # noqa: E402, F401


class ChecksTest(ReleaseCase):
    def test_finish_stops_on_a_public_line_the_private_denylist_names(self):
        release_main(['start', 'leak', *self.options()], self.public)
        tree, private = self.worktrees / 'leak/public', self.worktrees / 'leak/private'
        (private / release.DENYLIST).write_text('# hosts\nsecret-host\n', encoding='utf-8')
        (tree / 'content').write_text('first\nreach Secret-Host over ssh\n', encoding='utf-8')
        (tree / 'notes.md').write_text('secret-host, but the allowlist keeps it out\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Document the host\n', encoding='utf-8')
        finish = ['finish', 'leak', '--message', str(message), '--private-message', str(message), *self.options()]
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertIn('content:2 in ', error.getvalue())
        self.assertNotIn('secret-host', error.getvalue().lower())
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

    def test_finish_stops_when_the_private_denylist_is_missing_or_lists_no_strings(self):
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        for name, text in (('unlisted', None), ('emptied', ''), ('commented', '# hosts\n# seeded-host\n')):
            with self.subTest(name):
                release_main(['start', name, *self.options()], self.public)
                tree, private = self.worktrees / name / 'public', self.worktrees / name / 'private'
                if text is None:
                    (private / release.DENYLIST).unlink()
                else:
                    (private / release.DENYLIST).write_text(text, encoding='utf-8')
                (tree / 'content').write_text('reach seeded-host over ssh\n', encoding='utf-8')
                finish = ['finish', name, '--message', str(message), '--private-message', str(message), *self.options()]
                with redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(release_main(finish, self.public), 3)
                self.assertIn(str(private / release.DENYLIST), error.getvalue())
                self.assertEqual(git(tree, 'rev-parse', 'HEAD'), git(self.public_origin, 'rev-parse', 'main'))
                self.assertEqual(git(private, 'rev-parse', 'HEAD'), git(self.private_origin, 'rev-parse', 'main'))

    def test_finish_reads_a_denylist_string_behind_a_byte_order_mark_or_before_a_comment(self):
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        for name, text, stop in (('marked', '\ufeffseeded-host\n', 'content:1 in '),
                                 ('joined', '# hosts\n\ufeffseeded-host\n', 'content:1 in '),
                                 ('noted', 'seeded-host # the host\n', 'content:1 in '),
                                 ('tabbed', 'seeded-host\t#the host\n', 'content:1 in '),
                                 ('marked-comments', '\ufeff# hosts\n  # seeded-host\n', 'lists no strings')):
            with self.subTest(name):
                release_main(['start', name, *self.options()], self.public)
                tree, private = self.worktrees / name / 'public', self.worktrees / name / 'private'
                (private / release.DENYLIST).write_text(text, encoding='utf-8')
                (tree / 'content').write_text('reach seeded-host over ssh\n', encoding='utf-8')
                finish = ['finish', name, '--message', str(message), '--private-message', str(message), *self.options()]
                with redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(release_main(finish, self.public), 3)
                self.assertIn(stop, error.getvalue())
                self.assertEqual(git(tree, 'rev-parse', 'HEAD'), git(self.public_origin, 'rev-parse', 'main'))

    def test_a_denylist_keeps_a_number_sign_inside_a_string(self):
        release_main(['start', 'sharp', *self.options()], self.public)
        tree, private = self.worktrees / 'sharp/public', self.worktrees / 'sharp/private'
        (private / release.DENYLIST).write_text('team#seeded\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        finish = ['finish', 'sharp', '--message', str(message), '--private-message', str(message), *self.options()]
        (tree / 'content').write_text('the team works\n', encoding='utf-8')
        self.assertEqual(release_main(finish, self.public), 0)
        (tree / 'content').write_text('ask team#seeded\n', encoding='utf-8')
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertIn('content:1 in ', error.getvalue())

    def test_finish_stops_on_a_denylist_that_is_not_utf_8(self):
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        for name, encoding, fault in (
                ('wide', 'utf-16', "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"),
                ('unmarked', 'utf-16-le', 'it holds a NUL byte, as a UTF-16 file does')):
            with self.subTest(name):
                release_main(['start', name, *self.options()], self.public)
                tree, private = self.worktrees / name / 'public', self.worktrees / name / 'private'
                (private / release.DENYLIST).write_text('seeded-host\n', encoding=encoding)
                (tree / 'content').write_text('reach seeded-host over ssh\n', encoding='utf-8')
                finish = ['finish', name, '--message', str(message), '--private-message', str(message), *self.options()]
                with redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(release_main(finish, self.public), 3)
                self.assertIn(f'{private / release.DENYLIST} is not UTF-8 ({fault}), so nothing checks',
                              error.getvalue())
                self.assertIn('save it as UTF-8, then finish again; this run committed nothing', error.getvalue())
                self.assertEqual(git(tree, 'rev-parse', 'HEAD'), git(self.public_origin, 'rev-parse', 'main'))

    def test_a_denylist_stop_after_an_earlier_finish_committed_does_not_say_nothing_was_committed(self):
        release_main(['start', 'again', *self.options()], self.public)
        tree, private = self.worktrees / 'again/public', self.worktrees / 'again/private'
        (tree / 'content').write_text('second\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        finish = ['finish', 'again', '--message', str(message), '--private-message', str(message), *self.options()]
        self.assertEqual(release_main(finish, self.public), 0)
        self.assertEqual(git(tree, 'rev-list', '--count', 'origin/main..HEAD'), '1')

        def stopped():
            with redirect_stderr(io.StringIO()) as error:
                self.assertEqual(release_main(finish, self.public), 3)
            self.assertNotIn('nothing was committed', error.getvalue())
            self.assertIn('this run committed nothing', error.getvalue())
            return error.getvalue()

        (tree / 'content').write_text('reach seeded-host over ssh\n', encoding='utf-8')
        self.assertIn('content:1 in ', stopped())
        (private / release.DENYLIST).write_text('', encoding='utf-8')
        self.assertIn(f'{private / release.DENYLIST} lists no strings', stopped())
        (private / release.DENYLIST).unlink()
        self.assertIn(f'{private / release.DENYLIST} is missing', stopped())

    def test_finish_reports_the_suite_result_and_its_log(self):
        message = self.root / 'message'
        message.write_text('Change the tree\n', encoding='utf-8')
        for name, changed, text, expected in (
                ('data', 'content', 'second\n', 'Ran 1 test in'),
                ('code', 'tests/test_other.py', PASSING_TEST, 'Ran 2 tests in')):
            with self.subTest(name):
                release_main(['start', name, *self.options()], self.public)
                (self.worktrees / name / 'public' / changed).write_text(text, encoding='utf-8')
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(release_main(['finish', name, '--message', str(message), *self.options()],
                                                  self.public), 0)
                self.assertIn(expected, output.getvalue())
                self.assertIn(str(self.worktrees / name / 'build/tests.log'), output.getvalue())

    def test_the_public_suite_runs_for_data_and_code_changes(self):
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
        with self.assertRaisesRegex(RuntimeError, 'test suite failed'):
            release.test_suite(public, private, log)
        self.assertIn('broken', log.read_text(encoding='utf-8'))

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
        import portable
        release_main(['start', 'notes', *self.options()], self.public)
        tree = self.worktrees / 'notes/public'
        (tree / 'notes.md').write_text('left out\n', encoding='utf-8')
        (tree / 'tests/__pycache__').mkdir()
        (tree / 'tests/__pycache__/cache.pyc').write_bytes(b'cache')
        message = self.root / 'message'
        message.write_text('Add notes\n', encoding='utf-8')
        finish = ['finish', 'notes', '--message', str(message), '--push', '--timeout', '0', *self.options()]
        # Before the wait for another release, which here would end in a stop of its own.
        with portable.exclusive_lock(self.root / 'state/release.lock', record=True), \
                redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertIn('.gitignore leaves out notes.md;', error.getvalue())
        (tree / 'notes.md').unlink()
        (tree / 'content').write_text('second\n', encoding='utf-8')
        self.assertEqual(release_main(finish, self.public), 0)
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'second')
        # A file made while the finish waits for the lock stops it too, before the commit that would leave it out.
        hold = release.hold

        def make_a_file_during_the_wait(*arguments, **keywords):
            (tree / 'late.md').write_text('left out\n', encoding='utf-8')
            return hold(*arguments, **keywords)

        (tree / 'content').write_text('third\n', encoding='utf-8')
        with patch.object(release, 'hold', make_a_file_during_the_wait), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertIn('.gitignore leaves out late.md;', error.getvalue())
        self.assertEqual(git(tree, 'status', '--porcelain'), 'M content')
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'second')
        # The next finish does not test these files again, and still stops on that file before it waits.
        with portable.exclusive_lock(self.root / 'state/release.lock', record=True), \
                redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertNotIn('Both complete suites run', output.getvalue())
        self.assertIn('.gitignore leaves out late.md;', error.getvalue())

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

    def test_finish_stops_on_a_blocked_pull_whose_words_are_not_utf_8(self):
        release_main(['start', 'coded', *self.options()], self.public)
        (self.worktrees / 'coded/public/sync.py').write_text(
            'import sys\nsys.stdout.buffer.write(b"plan \\xb7 one row\\n")\n'
            'sys.stderr.buffer.write(b"Sync stopped: caf\\xe9 \\xb7 blocked\\n")\nraise SystemExit(3)\n',
            encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Block the pull\n', encoding='utf-8')
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'coded', '--message', str(message), '--push', *self.options()],
                                          self.public), 3)
        self.assertIn('plan \ufffd one row\nSync stopped: caf\ufffd \ufffd blocked\n', error.getvalue())
        self.assertIn('the pull on this device would stop on this release (sync exited 3)', error.getvalue())
        self.assertEqual(git(self.public_origin, 'show', 'main:sync.py'), SYNC_WITH_CHANGES.strip())

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
        self.assertIn('this run committed nothing', error.getvalue())
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
        held = sync_with_changes({'identity': 'plugin:claude:kit@m', 'cache': '/cache/kit/1.0.0', 'holders': [4242]})
        (self.worktrees / 'held/public/sync.py').write_text(held, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Hold the plugin\n', encoding='utf-8')
        finish = ['finish', 'held', '--message', str(message), '--push', *self.options()]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(finish, self.public), 0)
        self.assertIn('Claude lease: plugin:claude:kit@m at /cache/kit/1.0.0, held by running Claude processes 4242; '
                      'the pull here swaps it at once if only skill text changes', output.getvalue())
        self.assertEqual(git(self.public_origin, 'show', 'main:sync.py'), held.strip())

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
        status['Peer']['a']['Online'] = False
        for same_pc, told in (('work-pc', "this PC's Windows side, which WSL reaches without the tailnet, so pull it "
                               'with pull-machines'), ('laptop', 'so queue its steps'), (None, 'so queue its steps')):
            with self.subTest(same_pc=same_pc):
                lines = release.other_machines(status, same_pc)
                self.assertIn(told, lines[1])
                self.assertIn('so queue its steps', lines[0])
        status['Peer']['a']['Online'] = True
        self.assertIn('online now, so pull there', release.other_machines(status, 'work-pc')[1])
        release_main(['start', 'told', *self.options()], self.public)
        (self.worktrees / 'told/public/content').write_text('told\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Tell the machines\n', encoding='utf-8')
        for flags, told in ((['--push'], True), ([], False)):
            with self.subTest(flags=flags), patch.object(release, 'tailnet', return_value=status), \
                    redirect_stdout(io.StringIO()) as output:
                release_main(['finish', 'told', '--message', str(message), *flags, *self.options()], self.public)
            self.assertEqual('Other machine Work-PC (windows): online now' in output.getvalue(), told)
        status['Peer']['a']['Online'] = False
        for kernel, told in (('6.18.58.1-microsoft-standard-WSL2', 'Work-PC (windows): shown as offline, but it is this '
                              "PC's Windows side"), ('6.8.0-45-generic', 'Work-PC (windows): offline, last seen')):
            (self.worktrees / 'told/public/content').write_text(f'{kernel}\n', encoding='utf-8')
            with self.subTest(kernel=kernel), patch.object(release, 'tailnet', return_value=status), \
                    patch.object(release.platform, 'node', return_value='Work-PC'), \
                    patch.object(release.platform, 'release', return_value=kernel), \
                    redirect_stdout(io.StringIO()) as output:
                release_main(['finish', 'told', '--message', str(message), '--push', *self.options()], self.public)
            self.assertIn(told, output.getvalue())

    def test_finish_stops_on_a_package_file_that_is_not_toml(self):
        for name, content in (('garbled', b'x = \n'), ('undecodable', b'x = "\xff\xfe"\n')):
            with self.subTest(content=content):
                release_main(['start', name, *self.options()], self.public)
                package = self.worktrees / name / 'public/setup/packages/bad.toml'
                package.parent.mkdir(parents=True)
                package.write_bytes(content)

                said = self.stopped_finish(name)
                self.assertIn(f'Release stopped: {package} is not TOML (', said)
                self.assertIn('); this run committed nothing', said)

    def test_finish_stops_on_a_package_file_that_origin_holds_as_no_toml(self):
        seed = self.clone(self.public_origin, 'package-seed')
        (seed / PACKAGE).parent.mkdir(parents=True)
        (seed / PACKAGE).write_text('x = \n', encoding='utf-8')
        git(seed, 'add', '-f', PACKAGE)
        git(seed, 'commit', '-qm', 'A package file that is not TOML')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')
        git(self.public, 'pull', '-q', '--ff-only')
        release_main(['start', 'repair', *self.options()], self.public)
        (self.worktrees / 'repair/public' / PACKAGE).write_text('[source]\n', encoding='utf-8')

        said = self.stopped_finish('repair')
        self.assertIn(f'Release stopped: {PACKAGE} as origin/main held it at ', said)
        self.assertIn(f'and git log origin/main -- {PACKAGE} names the commit that did; this run committed nothing',
                      said)

    def seed_package(self, text):
        seed = self.clone(self.public_origin, 'public-seed')
        (seed / PACKAGE).parent.mkdir(parents=True)
        (seed / PACKAGE).write_text(text, encoding='utf-8')
        (seed / '.gitignore').write_text(ALLOWLIST + '!/setup/\n!/setup/**\n', encoding='utf-8')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-qm', 'Add a package')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')


if __name__ == '__main__':
    unittest.main()
