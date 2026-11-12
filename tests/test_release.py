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


def sync_with_changes(*held):
    """A worktree's sync.py whose candidate check finds one change to apply and the held Claude caches."""
    result = json.dumps({'blocking': 0, 'held': list(held)})
    return f'print({result!r})\nraise SystemExit(1)\n'


SYNC_WITH_CHANGES = sync_with_changes()
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
        environment = patch.dict(os.environ)  # start notes the T3 thread it runs in, so no test inherits one
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop('T3CODE_THREAD_ID', None)
        self.root = Path(self.temp.name)
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

    def test_finish_stops_before_any_work_on_a_message_file_whose_subject_runs_into_its_body(self):
        release_main(['start', 'run-on', *self.options()], self.public)
        for tree in ('public', 'private'):
            (self.worktrees / 'run-on' / tree / 'content').write_text('second\n', encoding='utf-8')
        good, bad = self.root / 'message', self.root / 'run-on-message'
        good.write_text('Change the content\n\nWith a body.\n', encoding='utf-8')
        bad.write_text('Change the content\nwith a body that starts on line 2.\n', encoding='utf-8')
        started = AssertionError('the finish, or its unit, was started')
        for option in ('--message', '--private-message', '--pair-message'):
            for detach in ([], ['--detach']):
                with self.subTest(option=option, detach=detach):
                    files = {'--message': good, '--private-message': good, option: bad}
                    argv = ['finish', 'run-on', *(part for name, file in files.items() for part in (name, str(file))),
                            '--push', *detach, *self.options()]
                    errors = io.StringIO()
                    with redirect_stderr(errors), patch.object(release, 'detach_command', side_effect=started), \
                            patch.object(release, 'wait', side_effect=started), \
                            patch.object(release, 'finish', side_effect=started):
                        self.assertEqual(release_main(argv, self.public), 3)
                    self.assertIn(f'Release stopped: {option} {bad}: line 2 is not blank', errors.getvalue())
                    self.assertIn('nothing was started' if detach else 'this run committed nothing', errors.getvalue())
        bad.write_text('\nChange the content\nwith a body under a blank first line.\n', encoding='utf-8')
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(release_main(['finish', 'run-on', '--message', str(bad), '--private-message', str(good),
                                           *self.options()], self.public), 3)
        self.assertIn('line 3 is not blank', errors.getvalue())
        for text, reason in (('', ' holds no text'), (' \n\n', ' holds no text'), (None, ' cannot be read: [Errno 2]'),
                             ('Change the content\n\xa0\nwith a line 2 that git does not count as blank.\n',
                              ': line 2 is not blank')):
            with self.subTest(text=text):
                bad.unlink(missing_ok=True)
                if text is not None:
                    bad.write_text(text, encoding='utf-8')
                errors = io.StringIO()
                with redirect_stderr(errors):
                    self.assertEqual(release_main(['finish', 'run-on', '--message', str(good), '--private-message',
                                                   str(bad), *self.options()], self.public), 3)
                self.assertIn(f'Release stopped: --private-message {bad}{reason}', errors.getvalue())
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'first')

    def test_finish_takes_a_message_of_one_line_or_of_a_subject_a_blank_line_and_a_body(self):
        release_main(['start', 'worded-well', *self.options()], self.public)
        for tree in ('public', 'private'):
            (self.worktrees / 'worded-well' / tree / 'content').write_text('second\n', encoding='utf-8')
        message, private_message = self.root / 'message', self.root / 'private-message'
        message.write_text('Change the content', encoding='utf-8')
        # Windows line ends, a second line of spaces and a form feed in the subject are all one subject to git.
        private_message.write_bytes(b'\r\nChange the private\x0ccontent\r\n \t\r\nWith a body\r\nof two lines.\r\n')
        self.assertEqual(release_main(['finish', 'worded-well', '--message', str(message), '--private-message',
                                       str(private_message), '--push', *self.options()], self.public), 0)
        self.assertEqual(git(self.public_origin, 'log', '-1', '--format=%s', 'main'), 'Change the content')
        self.assertEqual(git(self.private_origin, 'log', '-2', '--format=%s', 'main').split('\n'),
                         ['Pair "Change the content"', 'Change the private\x0ccontent'])

    def test_clean_keeps_an_unpushed_branch(self):
        release_main(['start', 'draft', *self.options()], self.public)
        (self.worktrees / 'draft/public/content').write_text('unpushed\n', encoding='utf-8')
        git(self.worktrees / 'draft/public', 'commit', '-qam', 'Unpushed')
        self.assertEqual(release_main(['clean', 'draft', *self.options()], self.public), 3)
        self.assertTrue((self.worktrees / 'draft/public').exists())
        # The private side is checked before the public side goes.
        release_main(['start', 'sketch', *self.options()], self.public)
        (self.worktrees / 'sketch/private/notes').write_text('unpushed\n', encoding='utf-8')
        git(self.worktrees / 'sketch/private', 'add', '-A')
        git(self.worktrees / 'sketch/private', 'commit', '-qm', 'Unpushed')
        self.assertEqual(release_main(['clean', 'sketch', *self.options()], self.public), 3)
        self.assertTrue((self.worktrees / 'sketch/public').exists())
        self.assertNotEqual(git(self.public, 'branch', '--list', 'sketch'), '')

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

    def push_from_another_session(self, origin, name, files):
        other = self.clone(origin, name)
        for relative, text in files.items():
            (other / relative).parent.mkdir(parents=True, exist_ok=True)
            (other / relative).write_text(text, encoding='utf-8')
        git(other, 'add', '-A')
        git(other, 'commit', '-qm', 'Another session')
        git(other, 'push', '-q', 'origin', 'HEAD:main')

    def test_finish_meets_origin_before_the_tests_and_commits_nothing(self):
        message = self.root / 'message'
        message.write_text('Edit the doc\n', encoding='utf-8')
        release_main(['start', 'clash', *self.options()], self.public)
        release_main(['start', 'shared', *self.options()], self.public)
        (self.worktrees / 'clash/public/doc').write_text('MINE\ntwo\nthree\nfour\nfive\n', encoding='utf-8')
        (self.worktrees / 'clash/public/tests/test_new.py').write_text(PASSING_TEST, encoding='utf-8')
        (self.worktrees / 'shared/public/doc').write_text('one\ntwo\nthree\nfour\nMINE\n', encoding='utf-8')
        self.push_from_another_session(self.public_origin, 'other-public', {'doc': 'THEIRS\ntwo\nthree\nfour\nfive\n'})
        for name, stop in (('clash', r'conflicts with origin/main \(public: doc\). Commit the worktree'),
                           ('shared', r'changed the same files on origin/main since this branch started \(public: doc\)')):
            with self.subTest(name), redirect_stderr(io.StringIO()) as errors:
                tree = self.worktrees / name / 'public'
                before = git(tree, 'rev-parse', 'HEAD'), git(tree, 'status', '--porcelain')
                self.assertEqual(release_main(['finish', name, '--message', str(message), '--push', '--overlap-reviewed'
                                               if name == 'clash' else '--push', *self.options()], self.public), 3)
                self.assertRegex(errors.getvalue(), stop)
                self.assertIn('this run committed nothing', errors.getvalue())
                self.assertEqual((git(tree, 'rev-parse', 'HEAD'), git(tree, 'status', '--porcelain')), before)
                self.assertFalse((self.worktrees / name / 'build/tests.log').exists())

    def test_a_trial_merge_reads_the_worktree_and_leaves_out_what_finish_writes_anew(self):
        entry, lock = 'setup/skills/kit.toml', 'setup/lock.toml'
        pinned = '[source]\nlocal = "skills/kit"\ntree = "sha256:{}"\n\n[place]\nagents = {}\n'
        self.push_from_another_session(self.private_origin, 'seed', {
            entry: pinned.format('a', 'true'), 'setup/skills/moved.toml': pinned.format('a', 'true'),
            'setup/skills/noted.toml': pinned.format('a', 'true'),
            **{f'setup/skills/{name}.toml': pinned.format('a', 'true') for name in ('tailed', 'ended', 'crlf')},
            'setup/skills/twin.toml': pinned.format('a', 'true') + 'tree = "a"\n', lock: 'a\n', 'notes': 'one\n'})
        release_main(['start', 'kit', *self.options()], self.public)
        mine = self.worktrees / 'kit/private'
        for relative, text in ((entry, pinned.format('b', 'true')), ('setup/skills/moved.toml', pinned.format('b', 'false')),
                               ('setup/skills/noted.toml', '# why it is placed\n' + pinned.format('b', 'true')),
                               ('setup/skills/tailed.toml', pinned.format('b"  # why this tree', 'true')),
                               ('setup/skills/ended.toml', pinned.format('b', 'true').rstrip('\n')),
                               ('setup/skills/crlf.toml', pinned.format('b', 'true').replace('\n', '\r\n')),
                               ('setup/skills/twin.toml', pinned.format('b', 'true') + 'tree = "b"\n'),
                               ('setup/skills/new.toml', pinned.format('b', 'true')), (lock, 'b\n'), ('fresh', 'mine\n')):
            (mine / relative).write_text(text, encoding='utf-8')
        self.push_from_another_session(self.private_origin, 'theirs', {
            entry: pinned.format('c', 'true'), 'setup/skills/moved.toml': pinned.format('c', 'true'),
            lock: 'c\n', 'fresh': 'theirs\n'})
        before = git(mine, 'rev-parse', 'HEAD'), git(mine, 'status', '--porcelain')

        regenerated = release.pin_only(mine, ['kit', 'moved', 'noted', 'twin', 'tailed', 'ended', 'crlf', 'new',
                                              'absent']) | {lock}
        # moved.toml also changed where the skill is placed, noted.toml gained a comment origin's copy lacks, and
        # twin.toml changed a tree key outside its source. tailed.toml gained a comment after its pin, ended.toml
        # lost its final newline and crlf.toml changed its line endings, each of which origin's copy would undo.
        self.assertEqual(regenerated, {entry, lock})
        self.assertEqual(release.pin_only(mine, []), frozenset())
        self.assertEqual(release.conflicts(mine), ['fresh', lock, entry, 'setup/skills/moved.toml'])
        self.assertEqual(release.conflicts(mine, regenerated), ['fresh', 'setup/skills/moved.toml'])
        self.assertEqual(release.overlap(mine, regenerated), ['fresh', 'setup/skills/moved.toml'])
        self.assertEqual(release.overlap(mine), ['fresh', entry, 'setup/skills/moved.toml'])
        self.assertEqual((git(mine, 'rev-parse', 'HEAD'), git(mine, 'status', '--porcelain')), before)
        # A merge that git could not try is a stop with git's words, not a merge without conflicts, though git
        # exits 1 for both.
        with patch.object(release, 'snapshot', return_value='0' * 40), \
                self.assertRaisesRegex(RuntimeError, 'private: git merge-tree exited 1: .*0000.* not something'):
            release.conflicts(mine)
        (mine / 'fresh').unlink()
        (mine / 'setup/skills/moved.toml').write_text(pinned.format('a', 'true'), encoding='utf-8')
        self.assertEqual(release.conflicts(mine, regenerated), [])

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
                         f'{release.DENYLIST}\nconfig/tool/helper.py\nconfig/tool/test_tool.py')

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

    def test_finish_meets_an_origin_that_moved_while_the_tests_ran(self):
        message = self.root / 'message'
        message.write_text('Edit the doc\n', encoding='utf-8')
        suite = release.test_suite
        for name, theirs, flags, stop in (
                ('clash', 'THEIRS\ntwo\nthree\nfour\nfive\n', ['--overlap-reviewed'],
                 r'conflicts with origin/main \(public: doc\). Commit the worktree'),
                ('shared', 'THEIRS\ntwo\nthree\nfour\nFIVE\n', [],
                 r'changed the same files on origin/main since this branch started \(public: doc\)')):
            release_main(['start', name, *self.options()], self.public)
            tree = self.worktrees / name / 'public'
            (tree / 'doc').write_text('MINE\ntwo\nthree\nfour\nfive\n' if name == 'clash' else
                                      'one\ntwo\nMINE\nfour\nfive\n', encoding='utf-8')

            def push_during_the_suite(*arguments):
                suite(*arguments)
                self.push_from_another_session(self.public_origin, f'other-{name}', {'doc': theirs})

            with self.subTest(name), redirect_stderr(io.StringIO()) as errors, \
                    patch.object(release, 'test_suite', push_during_the_suite):
                self.assertEqual(release_main(['finish', name, '--message', str(message), '--push', *flags,
                                               *self.options()], self.public), 3)
            self.assertRegex(errors.getvalue(), stop)
            self.assertNotIn('committed nothing', errors.getvalue())  # the commit was made before origin moved
            self.assertEqual(git(tree, 'log', '-1', '--format=%s'), 'Edit the doc')
            self.assertEqual(git(self.public_origin, 'show', 'main:doc'), theirs.strip())

    def test_finish_leaves_out_of_both_stops_only_what_its_flags_write_anew(self):
        kit, pack, lock = 'setup/skills/kit.toml', 'setup/packages/pack.toml', 'setup/lock.toml'
        pinned = '[source]\nlocal = "skills/kit"\ntree = "sha256:{}"\n'
        allowed = ALLOWLIST + '!/setup/\n!/setup/**\n'
        self.push_from_another_session(self.public_origin, 'seed-public', {pack: pinned.format('a'), '.gitignore': allowed})
        self.push_from_another_session(self.private_origin, 'seed-private', {kit: pinned.format('a'),
                                                                               lock: '[kit]\ntree = "a"\n'})
        release_main(['start', 'pins', *self.options()], self.public)
        public, private = self.worktrees / 'pins/public', self.worktrees / 'pins/private'
        for tree, files in ((public, {pack: pinned.format('b'), 'content': 'mine\n'}),
                            (private, {kit: pinned.format('b'), lock: '[kit]\ntree = "b"\n', 'notes': 'mine\n'})):
            for relative, text in files.items():
                (tree / relative).write_text(text, encoding='utf-8')
        self.push_from_another_session(self.public_origin, 'their-public', {pack: pinned.format('c')})
        self.push_from_another_session(self.private_origin, 'their-private', {kit: pinned.format('c'),
                                                                                lock: '[kit]\ntree = "c"\n'})
        message = self.root / 'message'
        message.write_text('Pin the kit\n', encoding='utf-8')
        finish = ['finish', 'pins', '--message', str(message), '--private-message', str(message), *self.options()]
        both = ['--entry', 'kit', '--entry', 'pack']
        # The fixture has no lock_setup.py, and each case here ends before or without a relock that matters.
        with patch.object(release, 'run'):
            for flags, named in (([], f'(public: {pack}; private: {lock}, {kit})'),
                                 (both, f'(public: {pack}; private: {kit})'),  # only --refresh-owned pins anew
                                 (['--entry', 'kit', '--refresh-owned'], f'(public: {pack})')):
                with self.subTest(flags=flags), redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(release_main([*finish, *flags], self.public), 3)
                    self.assertIn(f'conflicts with origin/main {named}. Commit the worktree', errors.getvalue())
                    self.assertIn('this run committed nothing', errors.getvalue())
            # With both entries pinned anew nothing is left to stop on, and each rebase takes origin's copies.
            self.assertEqual(release_main([*finish, *both, '--refresh-owned'], self.public), 0)
        self.assertEqual((public / pack).read_text(encoding='utf-8'), pinned.format('c'))
        self.assertEqual((private / kit).read_text(encoding='utf-8'), pinned.format('c'))
        self.assertEqual((private / lock).read_text(encoding='utf-8'), '[kit]\ntree = "c"\n')
        self.assertEqual(((public / 'content').read_text(encoding='utf-8'),
                          (private / 'notes').read_text(encoding='utf-8')), ('mine\n', 'mine\n'))

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

    def test_status_and_a_later_start_name_the_t3_thread_that_started_each_release(self):
        with patch.dict(os.environ, {'T3CODE_THREAD_ID': 'thread-first'}):
            release_main(['start', 'first', *self.options()], self.public)
        (self.worktrees / 'first/public/content').write_text('edited\n', encoding='utf-8')
        with redirect_stdout(io.StringIO()) as output:
            release_main(['start', 'later', *self.options()], self.public)
        self.assertIn('Other open releases; read them before you edit the same files:\n'
                      '  first/public: 1 uncommitted: content\n'
                      '  first: T3 thread thread-first', output.getvalue())
        with patch.object(release, 'running', return_value=[]), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(['status', *self.options()], self.public), 0)
        self.assertEqual(output.getvalue().splitlines()[1:], [
            f'Release worktrees under {self.worktrees}:',
            '  first/private: on origin/main and clean',
            '  first/public: 1 uncommitted: content',
            '  first: T3 thread thread-first',
            '  later/private: on origin/main and clean',
            '  later/public: on origin/main and clean'])

    def test_a_start_outside_t3_drops_the_thread_noted_by_an_abandoned_release_of_its_name(self):
        release.thread_note(self.worktrees / 'again').parent.mkdir(parents=True)
        release.thread_note(self.worktrees / 'again').write_text('thread-abandoned\n', encoding='utf-8')
        release_main(['start', 'again', *self.options()], self.public)
        with patch.object(release, 'running', return_value=[]), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(['status', *self.options()], self.public), 0)
        self.assertNotIn('thread', output.getvalue())

    def test_status_names_an_unreadable_thread_note_and_goes_on(self):
        release_main(['start', 'first', *self.options()], self.public)
        release_main(['start', 'later', *self.options()], self.public)
        release.thread_note(self.worktrees / 'first').mkdir()
        with patch.object(release, 'running', return_value=[]), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(['status', *self.options()], self.public), 0)
        lines = output.getvalue().splitlines()
        self.assertTrue(lines[4].startswith('  first: T3 thread unreadable: '), lines)
        self.assertEqual(lines[5:], ['  later/private: on origin/main and clean', '  later/public: on origin/main and clean'])

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
                    self.assertIn('this run committed nothing', error.getvalue())
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
        with redirect_stderr(io.StringIO()) as error, patch.object(release, 'unit_active', return_value=True):
            self.assertEqual(release_main(['result', 'away', '--timeout', '0.1', *self.options()], self.public), 4)
        self.assertIn('Still running', error.getvalue())
        # A unit that was killed is gone and wrote no status. On 10 November 2026 result waited an hour on one.
        with redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()) as output, \
                patch.object(release, 'unit_active', return_value=False):
            self.assertEqual(release_main(['result', 'away', '--timeout', '600', *self.options()], self.public),
                             release.CRASHED)
        self.assertIn('ended without a status', error.getvalue())
        self.assertIn('Release stopped: nothing to release', output.getvalue())
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

    def test_a_finish_that_ends_on_an_uncaught_error_still_reports_a_status(self):
        status = self.root / 'finish.exit'

        def crash(argv, report):
            report.append(status)
            raise AttributeError("module 'portable' has no attribute 'require_readable'")
        with patch.object(release, 'dispatch', crash), self.assertRaises(AttributeError):
            release.main(['finish', 'unit'])
        self.assertEqual(status.read_text(encoding='utf-8'), f'{release.CRASHED}\n')

    def test_a_finish_that_waited_for_another_run_starts_again_from_the_engine_as_it_is_now(self):
        release_main(['start', 'late', *self.options()], self.public)
        status = self.root / 'finish.exit'
        argv = ['finish', 'late', '--report', str(status), *self.options()]
        busy = iter([['release PID 1 since 23:54: release.py finish other'], []])

        class Restarted(Exception):
            pass

        def restart(program, arguments):
            raise Restarted(arguments)
        with patch.object(release, 'running', lambda names=None: next(busy, [])), \
                patch.object(release.os, 'execv', restart), redirect_stderr(io.StringIO()), \
                redirect_stdout(io.StringIO()), self.assertRaises(Restarted) as restarted:
            release_main(argv, self.public)
        self.assertEqual(restarted.exception.args[0][1:], ['-B', str(self.public / 'release.py'), *argv])
        with patch.object(release.os, 'execv', restart), redirect_stderr(io.StringIO()):  # nothing to wait for
            self.assertEqual(release_main(argv, self.public), 3)

    def test_finish_without_a_message_stops_before_committing(self):
        release_main(['start', 'quiet', *self.options()], self.public)
        (self.worktrees / 'quiet/public/content').write_text('edited\n', encoding='utf-8')
        self.assertEqual(release_main(['finish', 'quiet', *self.options()], self.public), 3)
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'first')

    def review_output(self, name):
        shown = io.StringIO()
        with redirect_stdout(shown):
            self.assertEqual(release_main(['review', name, *self.options()], self.public), 0)
        return shown.getvalue()

    def test_finish_stops_on_changed_code_until_a_record_reviews_exactly_that_code(self):
        release_main(['start', 'coded', *self.options()], self.public)
        module = self.worktrees / 'coded/public/tests/test_other.py'
        module.write_text(PASSING_TEST, encoding='utf-8')
        (self.worktrees / 'coded/private/tool.py').write_text('value = 1\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a test\n', encoding='utf-8')
        finish = ['finish', 'coded', '--message', str(message), '--private-message', str(message), '--push',
                  *self.options()]
        record = self.root / 'findings.md'

        def stopped(arguments):
            errors = io.StringIO()
            with redirect_stderr(errors):
                self.assertEqual(release_main(arguments, self.public, review=False), 3)
            self.assertEqual(git(self.public_origin, 'ls-tree', '--name-only', 'main', 'tests/'), SEEDED_TESTS)
            return errors.getvalue()

        told = stopped(finish)
        self.assertIn('no --reviewed FILE names a review of it. Run release.py review coded', told)
        self.assertIn('this run committed nothing', told)
        self.assertEqual(git(self.worktrees / 'coded/public', 'rev-list', '--count', 'origin/main..HEAD'), '0')
        shown = self.review_output('coded')
        self.assertIn('code this branch changes: tests/test_other.py', shown)
        self.assertIn('code this branch changes: tool.py', shown)
        self.assertIn('status --short --untracked-files=all', shown)
        self.assertIn(str(self.public / 'docs/agents/coding-standards.md'), shown)
        public_line, private_line = (line.split('writes: ')[1] for line in shown.splitlines() if 'writes: ' in line)
        self.assertRegex(public_line, r'^Reviewed: public [0-9a-f]{16}$')
        self.assertRegex(private_line, r'^Reviewed: private [0-9a-f]{16}$')
        self.assertIn('is not a file', stopped([*finish, '--reviewed', str(record)]))
        record.write_text(f'No findings.\n{public_line}\n', encoding='utf-8')
        self.assertIn(f'lacks "{private_line}"', stopped([*finish, '--reviewed', str(record)]))
        record.write_text(f'No findings.\n- {public_line}\n`{private_line}`\n', encoding='utf-8')
        self.assertIn('on a line of its own', stopped([*finish, '--reviewed', str(record)]))
        record.write_text(f'{public_line}\nOne finding, fixed.\n{private_line}\n', encoding='utf-8-sig')
        module.write_text(PASSING_TEST + '\n# edited after the review\n', encoding='utf-8')
        self.assertIn('reviews other code', stopped([*finish, '--reviewed', str(record)]))
        module.write_text(PASSING_TEST, encoding='utf-8')
        self.assertEqual(release_main([*finish, '--reviewed', str(record)], self.public, review=False), 0)
        self.assertIn('tests/test_other.py', git(self.public_origin, 'ls-tree', '--name-only', 'main', 'tests/'))

    def test_review_counts_scripts_and_named_build_files_and_leaves_out_text_and_stored_copies(self):
        release_main(['start', 'kinds', *self.options()], self.public)
        public, private = self.worktrees / 'kinds/public', self.worktrees / 'kinds/private'
        (public / 'content').write_text('second\n', encoding='utf-8')
        for relative, text in {'bin/tool': '#!/bin/sh\necho\n', 'notes.md': 'text\n', 'docker/Dockerfile': 'FROM x\n',
                               'setup/snapshots/abc/stored.py': 'value = 1\n', 'config/run.ps1': 'exit 0\n',
                               'units/pull.service': '[Unit]\n', '.github/workflows/tests.yml': 'on: push\n'}.items():
            (private / relative).parent.mkdir(parents=True, exist_ok=True)
            (private / relative).write_text(text, encoding='utf-8')
        wanted = release.review_lines(public, private)
        self.assertEqual(list(wanted), ['private'])
        self.assertEqual(wanted['private'][0], ['.github/workflows/tests.yml', 'bin/tool', 'config/run.ps1',
                                                'docker/Dockerfile', 'units/pull.service'])
        (private / 'config/run.ps1').write_text('exit 1\n', encoding='utf-8')
        self.assertNotEqual(release.review_lines(public, private), wanted)
        (private / 'config/run.ps1').write_text('exit 0\n', encoding='utf-8')
        self.assertEqual(release.review_lines(public, private), wanted)
        self.assertIn('changes no code', self.review_output_of_a_text_release())

    def review_output_of_a_text_release(self):
        release_main(['start', 'words', *self.options()], self.public)
        (self.worktrees / 'words/public/content').write_text('second\n', encoding='utf-8')
        return self.review_output('words')

    def test_review_counts_an_edited_and_a_deleted_tracked_file_and_survives_its_commit(self):
        seed = self.clone(self.private_origin, 'script-seed')
        (seed / 'tool').write_text('#!/bin/sh\necho\n', encoding='utf-8')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-qm', 'Seed a script without a suffix')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')
        git(self.private, 'pull', '-q')
        release_main(['start', 'tracked', *self.options()], self.public)
        public, private = self.worktrees / 'tracked/public', self.worktrees / 'tracked/private'
        (private / 'tool').unlink()
        (public / 'tests/test_seed.py').write_text(PASSING_TEST + '\n# edited\n', encoding='utf-8')
        (public / 'tests/test_new.py').write_text(PASSING_TEST, encoding='utf-8')
        (public / 'sync.py').unlink()
        wanted = release.review_lines(public, private)
        self.assertEqual(wanted['public'][0], ['sync.py', 'tests/test_new.py', 'tests/test_seed.py'])
        self.assertEqual(wanted['private'][0], ['tool'])
        git(public, 'add', '-A')
        git(public, 'commit', '-qm', 'Edit, add and delete code')
        self.assertEqual(release.review_lines(public, private), wanted)
        (public / 'tests/test_seed.py').write_text(PASSING_TEST + '\n# edited again\n', encoding='utf-8')
        self.assertNotEqual(release.review_lines(public, private), wanted)

    def test_a_review_holds_through_a_rebase_over_another_edit_and_through_a_half_published_release(self):
        release_main(['start', 'moved', *self.options()], self.public)
        public, private = self.worktrees / 'moved/public', self.worktrees / 'moved/private'
        (public / 'tests/test_seed.py').write_text(
            PASSING_TEST.replace('class Seed', '# this branch, above\nclass Seed') + '\n# this branch, below\n',
            encoding='utf-8')
        (private / 'tool.py').write_text('value = 1\n', encoding='utf-8')
        git(public, 'commit', '-qam', 'Edit the seed test in two places')
        wanted = release.review_lines(public, private)
        other = self.clone(self.public_origin, 'other-public')
        (other / 'tests/test_seed.py').write_text('# other\n# session\n' + PASSING_TEST, encoding='utf-8')
        (other / 'tests/test_theirs.py').write_text(PASSING_TEST, encoding='utf-8')
        git(other, 'add', '-A')
        git(other, 'commit', '-qm', 'Another session adds lines above this branch\'s edit, and a test of its own')
        git(other, 'push', '-q', 'origin', 'HEAD:main')
        git(public, 'fetch', '-q', 'origin')
        self.assertEqual(release.review_lines(public, private), wanted)  # origin moved, branch not yet rebased
        git(public, 'rebase', '-q', 'origin/main')
        rebased = (public / 'tests/test_seed.py').read_text(encoding='utf-8')
        self.assertIn('# other', rebased)
        self.assertEqual(release.review_lines(public, private), wanted)
        # The upper of the branch's two edits, which a fingerprint of the last one alone would miss.
        (public / 'tests/test_seed.py').write_text(rebased.replace('# this branch, above', '# edited after the review'),
                                                   encoding='utf-8')
        self.assertNotEqual(release.review_lines(public, private), wanted)
        (public / 'tests/test_seed.py').write_text(rebased, encoding='utf-8')
        git(public, 'push', '-q', 'origin', 'HEAD:main')
        git(public, 'fetch', '-q', 'origin')
        self.assertEqual(release.review_lines(public, private), {'private': wanted['private']})

    def test_a_script_that_gains_a_line_above_its_first_line_is_still_code(self):
        seed = self.clone(self.private_origin, 'script-seed')
        (seed / 'tool').write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-qm', 'Seed a script without a suffix')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')
        git(self.private, 'pull', '-q')
        release_main(['start', 'above', *self.options()], self.public)
        public, private = self.worktrees / 'above/public', self.worktrees / 'above/private'
        (private / 'tool').write_text('# a note\n#!/bin/sh\nexec real "$@"\n', encoding='utf-8')
        self.assertEqual(release.review_lines(public, private)['private'][0], ['tool'])

    def test_the_fingerprint_tells_removed_lines_apart_and_one_file_from_another(self):
        release_main(['start', 'cut', *self.options()], self.public)
        public, private = self.worktrees / 'cut/public', self.worktrees / 'cut/private'
        seen = []
        for removed in ('import unittest\n', '        pass\n'):
            (public / 'tests/test_seed.py').write_text(PASSING_TEST.replace(removed, ''), encoding='utf-8')
            seen.append(release.review_lines(public, private)['public'][1])
        git(public, 'checkout', '-q', '--', 'tests/test_seed.py')
        for name in ('tests/test_a.py', 'tests/test_b.py'):
            (public / name).write_text(PASSING_TEST, encoding='utf-8')
            seen.append(release.review_lines(public, private)['public'][1])
            (public / name).unlink()
        self.assertEqual(len(set(seen)), 4)

    def test_a_failed_read_of_the_fork_point_stops_with_gits_own_words(self):
        with self.assertRaises(RuntimeError) as stop:
            release.fork_bytes(self.public, 'no-such-commit', 'content')
        self.assertIn('git ls-tree exited 128', str(stop.exception))
        self.assertIn('no-such-commit', str(stop.exception))
        self.assertIsNone(release.fork_bytes(self.public, 'HEAD', 'tests'))
        self.assertEqual(release.fork_bytes(self.public, 'HEAD', 'content'), b'first\n')

    def test_the_pull_a_finish_starts_does_not_wait_a_second_time(self):
        (self.public / 'pull.py').write_text(
            'import pathlib, sys\npathlib.Path(__file__).with_name("pull-argv").write_text("\\n".join(sys.argv[1:]))\n',
            encoding='utf-8')
        release_main(['start', 'pulled', *self.options()], self.public)
        (self.worktrees / 'pulled/public/content').write_text('second\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        self.assertEqual(release_main(['finish', 'pulled', '--message', str(message), '--push', '--pull',
                                       *self.options()], self.public), 0)
        argv = (self.public / 'pull-argv').read_text(encoding='utf-8').split('\n')
        self.assertEqual(argv[-4:], ['--update', '--apply', '--wait', '0'])

    def test_a_file_without_a_line_diff_is_fingerprinted_by_both_sides(self):
        self.assertEqual(release.branch_lines(b'same\n', b'same\n'), [])
        binary = release.branch_lines(b'a\0one\n', b'a\0two\n')
        self.assertEqual(len(binary), 2)
        self.assertNotEqual(binary, release.branch_lines(b'a\0one\n', b'a\0three\n'))
        self.assertNotEqual(binary, release.branch_lines(b'a\0zero\n', b'a\0two\n'))
        self.assertEqual(release.branch_lines(None, b'value = 1\n'), [b'+value = 1'])
        # Git's marker for a last line without its newline is not one of the lines.
        self.assertEqual(release.branch_lines(b'a\nb\n', b'a\nc'), [b'-b', b'+c'])
        self.assertEqual(release.branch_lines(b'a\nb\n', b'a\nc\n'), [b'-b', b'+c'])

    def test_a_mode_only_change_keeps_its_line_when_another_session_edits_the_file(self):
        release_main(['start', 'mode', *self.options()], self.public)
        public, private = self.worktrees / 'mode/public', self.worktrees / 'mode/private'
        (public / 'tests/test_seed.py').chmod(0o755)
        git(public, 'commit', '-qam', 'Make the seed test executable')
        wanted = release.review_lines(public, private)
        self.assertEqual(wanted['public'][0], ['tests/test_seed.py'])
        other = self.clone(self.public_origin, 'other-public')
        (other / 'tests/test_seed.py').write_text(PASSING_TEST + '\n# other\n', encoding='utf-8')
        git(other, 'commit', '-qam', 'Another session edits the file')
        git(other, 'push', '-q', 'origin', 'HEAD:main')
        git(public, 'fetch', '-q', 'origin')
        git(public, 'rebase', '-q', 'origin/main')
        self.assertEqual(release.review_lines(public, private), wanted)

    def test_the_lines_ignore_a_global_attributes_file_and_a_failed_diff_stops(self):
        plain = release.branch_lines(b'one\r\n', b'two\r\n')
        self.assertEqual(plain, [b'-one\r', b'+two\r'])
        attributes, config = self.root / 'attributes', self.root / 'gitconfig'
        attributes.write_text('* binary\n', encoding='utf-8')
        config.write_text(f'[core]\n\tattributesFile = {attributes}\n', encoding='utf-8')
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(config)}):
            self.assertEqual(release.branch_lines(b'one\r\n', b'two\r\n'), plain)
        with patch.object(release, 'GIT', ('git', '--no-such-option')), self.assertRaises(RuntimeError) as stop:
            release.branch_lines(b'one\n', b'two\n')
        self.assertIn('git diff exited', str(stop.exception))
        # The machine's own attributes file sits under /etc, where a test cannot plant one, so this reads the
        # setting that turns it off from the environment the diff runs in.
        run, seen = subprocess.run, []

        def recorded(command, **options):
            seen.append(options['env'].get('GIT_ATTR_NOSYSTEM'))
            return run(command, **options)

        with patch.object(release.subprocess, 'run', recorded):
            release.branch_lines(b'one\n', b'two\n')
        self.assertEqual(seen, ['1'])

    def test_a_name_git_reads_as_a_pattern_and_a_link_are_read_as_they_are(self):
        seed = self.clone(self.private_origin, 'odd-seed')
        (seed / ':colon.py').write_text('value = 1\n', encoding='utf-8')
        (seed / 'colon.py').write_text('value = 2\n', encoding='utf-8')
        (seed / 'target').write_text('#!/bin/sh\n', encoding='utf-8')
        (seed / 'link').symlink_to('target')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-qm', 'Seed odd names')
        self.assertEqual(release.fork_bytes(seed, 'HEAD', ':colon.py'), b'value = 1\n')
        self.assertEqual(release.fork_bytes(seed, 'HEAD', 'link'), b'target')
        self.assertEqual(release.worktree_bytes(seed / 'link'), b'target')
        self.assertIsNone(release.worktree_bytes(seed / 'absent'))
        try:
            os.symlink(b'caf\xe9', os.fsencode(seed / 'latin'))
        except OSError:
            return  # a file system that refuses such text holds no such link to read
        self.assertEqual(release.worktree_bytes(seed / 'latin'), b'caf\xe9')

    def test_a_changed_link_is_reviewed_by_its_own_text(self):
        release_main(['start', 'linked', *self.options()], self.public)
        public, private = self.worktrees / 'linked/public', self.worktrees / 'linked/private'
        (private / 'words').write_text('one\n', encoding='utf-8')
        (private / 'run.sh').symlink_to('words')
        wanted = release.review_lines(public, private)
        self.assertEqual(wanted['private'][0], ['run.sh'])
        (private / 'words').write_text('two\n', encoding='utf-8')
        self.assertEqual(release.review_lines(public, private), wanted)
        (private / 'run.sh').unlink()
        (private / 'run.sh').symlink_to('elsewhere')
        self.assertNotEqual(release.review_lines(public, private), wanted)

    def refuse_private_pushes(self):
        hook = self.private_origin / 'hooks/pre-receive'
        hook.write_text('#!/bin/sh\necho "refused by the test" >&2\nexit 1\n', encoding='utf-8')
        hook.chmod(0o755)
        return hook

    def finish_stopped(self, finish):
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(release_main(finish, self.public), 3)
        return errors.getvalue()

    def half_publish(self, name):
        """A release whose public commit is on origin and whose private push was refused: the arguments of its
        finish, the hook that refuses, and what the finish said."""
        release_main(['start', name, *self.options()], self.public)
        (self.worktrees / name / 'public/content').write_text('second\n', encoding='utf-8')
        (self.worktrees / name / 'private/notes').write_text('with it\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        finish = ['finish', name, '--message', str(message), '--private-message', str(message), '--push',
                  *self.options()]
        hook = self.refuse_private_pushes()
        return finish, hook, self.finish_stopped(finish)

    def another_session_pushes(self, origin, relative):
        """A commit by another session that adds one file to an origin, and its id."""
        other = self.root / f'other-{origin.name}'
        if not other.exists():
            self.clone(origin, other.name)
        git(other, 'pull', '-q', '--rebase')
        (other / relative).write_text('meanwhile\n', encoding='utf-8')
        git(other, 'add', '-A')
        git(other, 'commit', '-qm', f'Another session adds {relative}')
        git(other, 'push', '-q', 'origin', 'HEAD:main')
        return git(other, 'rev-parse', 'HEAD')

    def test_a_rerun_after_a_half_published_release_pairs_the_private_half_with_the_pushed_commit(self):
        finish, hook, told = self.half_publish('half')
        self.assertIn('the public commit is on origin without its pair; finish again', told)
        pushed = git(self.public_origin, 'rev-parse', 'main')
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'second')
        self.assertNotIn(pushed, git(self.private_origin, 'show', 'main:release.toml'))
        hook.unlink()

        self.assertEqual(release_main(finish, self.public), 0)

        self.assertEqual(git(self.public_origin, 'rev-parse', 'main'), pushed)
        self.assertEqual(git(self.private_origin, 'show', 'main:release.toml'), f'public_commit = "{pushed}"')
        self.assertEqual(git(self.private_origin, 'show', 'main:notes'), 'with it')
        self.assertFalse((self.worktrees / 'half/build/public-pushed').exists())

    def test_a_rerun_pairs_with_the_public_head_after_another_session_pushed_on_top_of_the_noted_commit(self):
        finish, hook, _ = self.half_publish('topped')
        top = self.another_session_pushes(self.public_origin, 'tests/theirs.txt')
        self.assertIn('the public commit is on origin without its pair', self.finish_stopped(finish))
        self.assertEqual(git(self.worktrees / 'topped/public', 'rev-parse', 'HEAD'), top)  # past the noted commit
        hook.unlink()

        self.assertEqual(release_main(finish, self.public), 0)

        self.assertEqual(git(self.private_origin, 'show', 'main:release.toml'), f'public_commit = "{top}"')
        self.assertEqual(git(self.private_origin, 'show', 'main:notes'), 'with it')
        self.assertFalse((self.worktrees / 'topped/build/public-pushed').exists())

    def test_a_note_counts_while_the_public_head_holds_its_commit_and_any_other_note_stops(self):
        release_main(['start', 'noted', *self.options()], self.public)
        base = self.worktrees / 'noted'
        public = base / 'public'
        self.assertFalse(release.half_published(base, public))
        note = release.public_pushed(base)
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(git(public, 'rev-parse', 'HEAD') + '\n', encoding='utf-8')
        self.assertTrue(release.half_published(base, public))
        git(public, 'commit', '-q', '--allow-empty', '-m', 'On top')
        self.assertTrue(release.half_published(base, public))
        on_top = git(public, 'rev-parse', 'HEAD')
        git(public, 'reset', '-q', '--hard', 'HEAD~1')
        # A commit the HEAD does not hold, one the repository does not have, a name git would resolve, and bytes
        # that are no text.
        for content, problem in ((on_top.encode(), f'does not hold {on_top}'), (b'0' * 40, 'git could not say'),
                                 (b'HEAD', 'no full commit id'), (b'\xff\xfe', 'no full commit id')):
            note.write_bytes(content + b'\n')
            with self.assertRaises(RuntimeError) as stop:
                release.half_published(base, public)
            self.assertIn(problem, str(stop.exception))
            self.assertIn(str(note), str(stop.exception))
        note.write_bytes(b'0' * 40 + b'\n')
        with self.assertRaises(RuntimeError) as stop:
            release.half_published(base, public)
        self.assertRegex(str(stop.exception), r'git could not say .*: (fatal|error): \S')  # git's own words

    @unittest.skipIf(os.name == 'nt' or not hasattr(os, 'geteuid') or os.geteuid() == 0, 'needs a file it cannot read')
    def test_a_note_that_cannot_be_read_stops_the_finish(self):
        finish, hook, _ = self.half_publish('unread')
        hook.unlink()
        note = release.public_pushed(self.worktrees / 'unread')
        note.chmod(0)
        self.addCleanup(note.chmod, 0o600)
        self.assertIn('Permission denied', self.finish_stopped(finish))
        self.assertNotIn(git(self.public_origin, 'rev-parse', 'main'),
                         git(self.private_origin, 'show', 'main:release.toml'))

    def test_the_note_names_the_pushed_commit_where_a_push_leaves_origin_main_behind(self):
        # As with a GitHub SSH origin pushed over HTTPS: git moves origin/main only for a URL the remote lists.
        with patch.object(release, 'push_url', lambda root: 'file://' + git(root, 'remote', 'get-url', 'origin')):
            self.half_publish('behind')
        pushed = git(self.public_origin, 'rev-parse', 'main')
        self.assertNotEqual(git(self.worktrees / 'behind/public', 'rev-parse', 'origin/main'), pushed)
        self.assertEqual(release.public_pushed(self.worktrees / 'behind').read_text(encoding='utf-8'), pushed + '\n')

    def test_a_private_only_release_is_not_paired_with_another_sessions_unpaired_public_commit(self):
        first = git(self.public_origin, 'rev-parse', 'main')
        self.another_session_pushes(self.public_origin, 'tests/theirs.txt')
        release_main(['start', 'private-only', *self.options()], self.public)
        (self.worktrees / 'private-only/private/notes').write_text('on its own\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add notes\n', encoding='utf-8')
        finish = ['finish', 'private-only', '--private-message', str(message), '--push', *self.options()]
        hook = self.refuse_private_pushes()
        self.assertIn('push failed and nothing was pushed', self.finish_stopped(finish))
        self.assertFalse((self.worktrees / 'private-only/build/public-pushed').exists())
        hook.unlink()

        self.assertEqual(release_main(finish, self.public), 0)

        self.assertEqual(git(self.private_origin, 'show', 'main:release.toml'), f'public_commit = "{first}"')
        self.assertEqual(git(self.private_origin, 'show', 'main:notes'), 'on its own')

    def test_a_private_only_release_is_not_paired_with_a_public_commit_pushed_after_it_started(self):
        first = git(self.public_origin, 'rev-parse', 'main')
        release_main(['start', 'overtaken', *self.options()], self.public)
        (self.worktrees / 'overtaken/private/notes').write_text('on its own\n', encoding='utf-8')
        self.another_session_pushes(self.public_origin, 'tests/theirs.txt')
        message = self.root / 'message'
        message.write_text('Add notes\n', encoding='utf-8')

        self.assertEqual(release_main(['finish', 'overtaken', '--private-message', str(message), '--push',
                                       *self.options()], self.public), 0)

        self.assertEqual(git(self.private_origin, 'show', 'main:release.toml'), f'public_commit = "{first}"')
        self.assertEqual(git(self.private_origin, 'show', 'main:notes'), 'on its own')

    def test_a_release_with_a_public_commit_of_its_own_needs_no_note_and_replaces_one_it_cannot_use(self):
        release_main(['start', 'renewed', *self.options()], self.public)
        (self.worktrees / 'renewed/public/content').write_text('second\n', encoding='utf-8')
        note = release.public_pushed(self.worktrees / 'renewed')
        note.parent.mkdir(parents=True)
        note.write_text('0' * 40 + '\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')

        self.assertEqual(release_main(['finish', 'renewed', '--message', str(message), '--push', *self.options()],
                                      self.public), 0)

        pushed = git(self.public_origin, 'rev-parse', 'main')
        self.assertEqual(git(self.private_origin, 'show', 'main:release.toml'), f'public_commit = "{pushed}"')
        self.assertFalse(note.exists())

    def test_a_refused_public_push_leaves_no_note_and_says_that_nothing_was_pushed(self):
        release_main(['start', 'shut', *self.options()], self.public)
        (self.worktrees / 'shut/public/content').write_text('second\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        hook = self.public_origin / 'hooks/pre-receive'  # a dry run passes it, so only the push itself is refused
        hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
        hook.chmod(0o755)
        told = self.finish_stopped(['finish', 'shut', '--message', str(message), '--push', *self.options()])
        self.assertIn('push failed and nothing was pushed', told)
        self.assertFalse(release.public_pushed(self.worktrees / 'shut').exists())
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'first')

    def test_an_origin_that_keeps_moving_before_anything_was_pushed_says_so(self):
        release_main(['start', 'outrun', *self.options()], self.public)
        (self.worktrees / 'outrun/private/notes').write_text('on its own\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add notes\n', encoding='utf-8')
        push, moves = release.push, []

        def origin_moves_first(root):
            moves.append(self.another_session_pushes(self.private_origin, f'moved-{len(moves)}'))
            push(root)

        with patch.object(release, 'push', origin_moves_first):
            told = self.finish_stopped(['finish', 'outrun', '--private-message', str(message), '--push',
                                        *self.options()])
        self.assertIn(f'push failed {release.ATTEMPTS} times because origin keeps moving, and nothing was pushed', told)

    def test_a_stop_after_the_public_push_says_that_the_public_commit_is_out(self):
        finish, hook, _ = self.half_publish('stopped')
        hook.unlink()
        public = self.worktrees / 'stopped/public'
        with patch.object(release.subprocess, 'run', self.crashed_sync(release.subprocess.run)):
            told = self.finish_stopped(finish)
        self.assertIn('the pull on this device would stop on this release (sync exited 3); the public commit is on '
                      'origin without its pair; finish again', told)
        self.assertTrue(release.public_pushed(self.worktrees / 'stopped').is_file())
        self.assertTrue(public.is_dir())

    def crashed_sync(self, run):
        """subprocess.run, except that the device check's sync exits 3."""
        def stand_in(command, **options):
            if any(str(part).endswith('sync.py') for part in command):
                return subprocess.CompletedProcess(command, 3, '', 'crashed by the test\n')
            return run(command, **options)
        return stand_in

    def test_a_rerun_that_finds_the_pair_already_made_drops_its_note(self):
        release_main(['start', 'done', *self.options()], self.public)
        (self.worktrees / 'done/public/content').write_text('second\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        finish = ['finish', 'done', '--message', str(message), '--push', *self.options()]
        hook = self.refuse_private_pushes()
        self.finish_stopped(finish)
        note = release.public_pushed(self.worktrees / 'done')
        self.assertTrue(note.is_file())
        hook.unlink()
        # Another session's release on top, as a pair: its private commit names a public commit that holds this
        # release's.
        top = self.another_session_pushes(self.public_origin, 'tests/theirs.txt')
        other = self.clone(self.private_origin, 'other-pair')
        (other / 'release.toml').write_text(f'public_commit = "{top}"\n', encoding='utf-8')
        git(other, 'commit', '-qam', 'Pair another session')
        git(other, 'push', '-q', 'origin', 'HEAD:main')

        self.assertIn('nothing to release', self.finish_stopped(finish))

        self.assertFalse(note.exists())

    def test_clean_keeps_a_half_published_release_that_changed_only_the_public_side(self):
        release_main(['start', 'lone', *self.options()], self.public)
        (self.worktrees / 'lone/public/content').write_text('second\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        self.refuse_private_pushes()
        self.finish_stopped(['finish', 'lone', '--message', str(message), '--push', *self.options()])
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(release_main(['clean', 'lone', *self.options()], self.public), 3)
        self.assertIn('is on origin without its pair', errors.getvalue())
        self.assertIn('nothing was removed', errors.getvalue())
        self.assertTrue((self.worktrees / 'lone/public').is_dir())
        self.assertTrue(release.public_pushed(self.worktrees / 'lone').is_file())
        self.assertEqual(release_main(['clean', 'lone', '--force', *self.options()], self.public), 0)
        self.assertFalse((self.worktrees / 'lone').exists())

    def test_a_failed_push_says_that_an_earlier_attempt_pushed_the_public_commit(self):
        release_main(['start', 'tried', *self.options()], self.public)
        (self.worktrees / 'tried/public/content').write_text('second\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        finish = ['finish', 'tried', '--message', str(message), '--push', *self.options()]
        push, moves = release.push, []

        def origin_moves_first(root):
            if root.name == 'private':
                moves.append(self.another_session_pushes(self.private_origin, f'moved-{len(moves)}'))
            push(root)

        def origin_moves_once_then_refuses(root):
            if root.name == 'private' and not moves:
                moves.append(self.another_session_pushes(self.private_origin, 'moved'))
            elif root.name == 'private':
                self.refuse_private_pushes()
            push(root)

        with patch.object(release, 'push', origin_moves_once_then_refuses):
            told = self.finish_stopped(finish)
        self.assertIn('push failed and the public commit is on origin without its pair; finish again', told)
        pushed = git(self.public_origin, 'rev-parse', 'main')
        (self.private_origin / 'hooks/pre-receive').unlink()
        with patch.object(release, 'push', origin_moves_first):
            told = self.finish_stopped(finish)
        self.assertEqual(len(moves), 1 + release.ATTEMPTS)
        self.assertIn(f'push failed {release.ATTEMPTS} times because origin keeps moving, and the public commit is on '
                      'origin without its pair; finish again', told)

        self.assertEqual(release_main(finish, self.public), 0)

        self.assertEqual(git(self.private_origin, 'show', 'main:release.toml'), f'public_commit = "{pushed}"')

    def test_clean_removes_nothing_from_a_half_published_release(self):
        self.half_publish('kept')
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(release_main(['clean', 'kept', *self.options()], self.public), 3)
        self.assertIn('nothing was removed', errors.getvalue())
        self.assertTrue((self.worktrees / 'kept/public').is_dir())
        self.assertNotEqual(git(self.public, 'branch', '--list', 'kept'), '')
        self.assertTrue(release.public_pushed(self.worktrees / 'kept').is_file())

    def test_a_start_that_stops_keeps_the_note_and_a_start_of_new_branches_drops_it(self):
        finish, hook, _ = self.half_publish('again')
        base = self.worktrees / 'again'
        # As a clean before 11 November 2026 left it: the public worktree and branch gone, the private ones and
        # the note still there.
        git(self.public, 'worktree', 'remove', '--force', str(base / 'public'))
        git(self.public, 'branch', '-q', '-D', 'again')
        top = self.another_session_pushes(self.public_origin, 'tests/theirs.txt')
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(release_main(['start', 'again', *self.options()], self.public), 3)
        self.assertTrue(release.public_pushed(base).is_file())
        hook.unlink()
        self.assertEqual(release_main(finish, self.public), 0)
        self.assertEqual(git(self.private_origin, 'show', 'main:release.toml'), f'public_commit = "{top}"')

        stale = release.public_pushed(self.worktrees / 'abandoned')
        stale.parent.mkdir(parents=True)
        stale.write_text(top + '\n', encoding='utf-8')
        self.assertEqual(release_main(['start', 'abandoned', *self.options()], self.public), 0)
        self.assertFalse(stale.exists())

    def test_a_detached_finish_stops_on_unreviewed_code_before_it_starts_a_unit(self):
        release_main(['start', 'apart', *self.options()], self.public)
        (self.worktrees / 'apart/public/tests/test_other.py').write_text(PASSING_TEST, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a test\n', encoding='utf-8')
        errors = io.StringIO()
        # No systemd-run to find, so a finish that got past the stop would start nothing here either.
        with redirect_stderr(errors), patch.object(release.shutil, 'which', return_value=None):
            self.assertEqual(release_main(['finish', 'apart', '--detach', '--message', str(message), *self.options()],
                                          self.public, review=False), 3)
        self.assertIn('nothing was started', errors.getvalue())
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(release_main(['finish', 'apart', '--detach', '--message', str(message), '--reviewed',
                                           'findings.md', *self.options()], self.public, review=False), 3)
        self.assertIn('absolute path for findings.md', errors.getvalue())

    def test_a_code_edit_made_while_finish_runs_is_stopped_before_the_push(self):
        release_main(['start', 'late', *self.options()], self.public)
        module = self.worktrees / 'late/public/tests/test_other.py'
        module.write_text(PASSING_TEST, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a test\n', encoding='utf-8')
        suite = release.test_suite

        def edit_during_the_suite(*arguments):
            suite(*arguments)
            module.write_text(PASSING_TEST + '\n# slipped in\n', encoding='utf-8')

        errors = io.StringIO()
        with redirect_stderr(errors), patch.object(release, 'test_suite', edit_during_the_suite):
            self.assertEqual(release_main(['finish', 'late', '--message', str(message), '--push', *self.options()],
                                          self.public), 3)
        self.assertIn('the code changed after its review, and nothing was pushed', errors.getvalue())
        self.assertEqual(git(self.public_origin, 'ls-tree', '--name-only', 'main', 'tests/'), SEEDED_TESTS)

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


def release_main(argv, engine, review=True):
    """release.main with ENGINE set to the test's canonical public clone, and this machine's locks in its folder.
    A finish that changes code gets the review record it needs unless review is False, so that each test meets
    only the stop it is about."""
    original = release.ENGINE, release.STATE
    release.ENGINE, release.STATE = engine, Path(engine).parent / 'state'
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
        release.ENGINE, release.STATE = original


if __name__ == '__main__':
    unittest.main()
