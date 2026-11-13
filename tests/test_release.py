from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release


def setUpModule():
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
        # A finish that starts again replaces its process, which here is the test run itself. A test of that names
        # its own stand-in.
        restart = patch.object(release.os, 'execv', side_effect=AssertionError('a finish started again'))
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
                    # --report is what a detached unit's finish gets, and what makes it wait for another release.
                    argv = ['finish', 'run-on', *(part for name, file in files.items() for part in (name, str(file))),
                            '--push', *(detach or ['--report', str(self.root / 'status')]), *self.options()]
                    log, status = (self.worktrees / 'run-on/build' / name for name in ('finish.log', 'finish.exit'))
                    log.parent.mkdir(exist_ok=True)
                    log.write_text('the log of an earlier finish\n', encoding='utf-8')
                    status.write_text('3\n', encoding='utf-8')
                    errors = io.StringIO()
                    with redirect_stderr(errors), patch.object(release, 'detach_command', side_effect=started), \
                            patch.object(release, 'review_stop', side_effect=started), \
                            patch.object(release, 'running', side_effect=started), \
                            patch.object(release, 'wait', side_effect=started), \
                            patch.object(release, 'finish', side_effect=started):
                        self.assertEqual(release_main(argv, self.public), 3)
                    self.assertEqual(log.read_text(encoding='utf-8'), 'the log of an earlier finish\n')
                    self.assertEqual(status.read_text(encoding='utf-8'), '3\n')  # result NAME still reads that finish
                    self.assertIn(f'Release stopped: {option} {bad}: line 2 is not blank', errors.getvalue())
                    self.assertIn('nothing was started' if detach else 'this run committed nothing', errors.getvalue())
        bad.write_text('\nChange the content\nwith a body under a blank first line.\n', encoding='utf-8')
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(release_main(['finish', 'run-on', '--message', str(bad), '--private-message', str(good),
                                           *self.options()], self.public), 3)
        self.assertIn('line 3 is not blank', errors.getvalue())
        for text, reason in ((b'', ' holds no text'), (b' \n\n', ' holds no text'), (None, ' cannot be read: [Errno 2]'),
                             ('Change the content\n\xa0\nwith a line 2 that git does not count as blank.\n'.encode(),
                              ': line 2 is not blank'),
                             (b'Change the\0content\n', ' holds a NUL byte'),
                             ('Change the content\n'.encode('utf-16'), ' holds a NUL byte, which git refuses in a commit '
                              "message, after the tests; a file that Windows PowerShell's > wrote is UTF-16, which holds "
                              'NUL bytes; save it as UTF-8'),
                             ('Change the content\n\nWith a body.\n'.encode('utf-8-sig'), ' starts with a byte-order mark'),
                             ('\n'.encode('utf-8-sig'), ' starts with a byte-order mark'),
                             ('\xc6ndr indholdet\n'.encode('latin-1'),
                              ' is not UTF-8 (invalid continuation byte at byte 0), so git would read each byte that is '
                              'not UTF-8 as Latin-1, which gives wrong characters for any other encoding; save it as '
                              'UTF-8')):
            for option, other in (('--message', '--private-message'), ('--private-message', '--message')):
                with self.subTest(text=text, option=option):
                    bad.unlink(missing_ok=True)
                    if text is not None:
                        bad.write_bytes(text)
                    errors = io.StringIO()
                    with redirect_stderr(errors):
                        self.assertEqual(release_main(['finish', 'run-on', option, str(bad), other, str(good),
                                                       *self.options()], self.public), 3)
                    self.assertIn(f'Release stopped: {option} {bad}{reason}', errors.getvalue())
                    self.assertIn('; this run committed nothing', errors.getvalue())
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

    def test_finish_runs_each_private_test_file_by_itself_and_stops_on_a_failure(self):
        release_main(['start', 'private', *self.options()], self.public)
        folder = self.worktrees / 'private/private/config/tool'
        folder.mkdir(parents=True)
        (folder / 'helper.py').write_text('VALUE = 1\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a private tool\n', encoding='utf-8')
        finish = ['finish', 'private', '--private-message', str(message), '--push', *self.options()]
        log = self.worktrees / 'private/build/private-tests.log'
        imports = ('import os\nfrom pathlib import Path\nimport sys\nimport unittest\n\n{path}import helper\n\n\n'
                   'class Tool(unittest.TestCase):\n'
                   '    def test_imports_its_helper_against_missing_config_roots(self):\n'
                   '        self.assertEqual(helper.VALUE, 1)\n'
                   '        for variable in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):\n'
                   '            self.assertFalse(Path(os.environ[variable]).exists())\n' + RUN_ALONE)
        for text, stop, logged in (
                (FAILING_TEST + RUN_ALONE, 'the private tests in config/tool/test_tool.py failed', 'broken'),
                (FAILING_TEST + RUN_ALONE.replace('main()', 'main(exit=False)'),
                 'the private tests in config/tool/test_tool.py failed', 'FAILED (failures=1)'),
                (FAILING_TEST.replace('self.fail', 'import sys; sys.stderr.buffer.write(b"caf\\xe9\\n"); self.fail')
                 + RUN_ALONE, 'the private tests in config/tool/test_tool.py failed', 'caf\ufffd\n'),
                (PASSING_TEST, 'config/tool/test_tool.py, so end it with unittest.main()',
                 '== config/tool/test_tool.py\n'),
                (imports.format(path=''), 'the private tests in config/tool/test_tool.py failed',
                 "ModuleNotFoundError: No module named 'helper'")):
            with self.subTest(stop=stop, logged=logged):
                (folder / 'test_tool.py').write_text(text, encoding='utf-8')
                with redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(release_main(finish, self.public), 3)
                self.assertIn(stop, error.getvalue())
                self.assertIn(logged, log.read_text(encoding='utf-8'))
                self.assertEqual(git(self.private_origin, 'log', '--format=%s', 'main'), 'seed')
        (folder / 'test_tool.py').write_text(
            imports.format(path='sys.path.insert(0, str(Path(__file__).resolve().parent))\n'), encoding='utf-8')
        (folder / 'test_more.py').write_text(PASSING_TEST + RUN_ALONE, encoding='utf-8')
        (folder.parent / 'other').mkdir()
        (folder.parent / 'other/test_other.py').write_text(PASSING_TEST + RUN_ALONE, encoding='utf-8')
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(finish, self.public), 0)
        self.assertIn('Ran 3 private tests in 2 folders: OK', output.getvalue())
        self.assertEqual(git(self.private_origin, 'ls-tree', '-r', '--name-only', 'main', 'config/'),
                         f'config/other/test_other.py\n{release.DENYLIST}\nconfig/tool/helper.py\n'
                         'config/tool/test_more.py\nconfig/tool/test_tool.py')

    def test_finish_runs_the_private_test_files_at_the_same_time(self):
        release_main(['start', 'together', *self.options()], self.public)
        meeting = self.root / 'meeting'
        meeting.mkdir()
        meet = ('import os\nfrom pathlib import Path\nimport time\nimport unittest\n\n\nclass Meet(unittest.TestCase):\n'
                '    def test_meets_the_other_file(self):\n'
                '        meeting = Path(os.environ["MEETING"])\n'
                '        (meeting / Path(__file__).name).touch()\n'
                '        deadline = time.monotonic() + 60\n'
                '        while len(list(meeting.iterdir())) < 2 and time.monotonic() < deadline:\n'
                '            time.sleep(0.05)\n'
                '        self.assertEqual(len(list(meeting.iterdir())), 2)\n' + RUN_ALONE)
        for name in ('test_one.py', 'test_two.py'):
            (self.worktrees / 'together/private' / name).write_text(meet, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add two tests\n', encoding='utf-8')
        os.environ['MEETING'] = str(meeting)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(['finish', 'together', '--private-message', str(message), *self.options()],
                                          self.public), 0)
        self.assertIn('Ran 2 private tests in 1 folders: OK', output.getvalue())

    def test_a_private_test_file_reads_an_ended_stdin_while_the_finish_keeps_its_own_open(self):
        release_main(['start', 'stdin', *self.options()], self.public)
        (self.worktrees / 'stdin/private/test_reads.py').write_text(
            'import sys\nimport threading\nimport unittest\n\n\nclass Reads(unittest.TestCase):\n'
            '    def test_reads_an_ended_stdin(self):\n'
            '        read = []\n'
            '        reader = threading.Thread(target=lambda: read.append(sys.stdin.read()), daemon=True)\n'
            '        reader.start()\n'
            '        reader.join(30)\n'
            '        self.assertEqual(read, [""])\n' + RUN_ALONE, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a test that reads stdin\n', encoding='utf-8')
        read, write = os.pipe()
        kept = os.dup(0)
        os.dup2(read, 0)
        try:
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(release_main(['finish', 'stdin', '--private-message', str(message), *self.options()],
                                              self.public), 0)
        finally:
            os.dup2(kept, 0)
            for descriptor in (read, write, kept):
                os.close(descriptor)
        self.assertIn('Ran 1 private tests in 1 folders: OK', output.getvalue())

    def test_finish_names_every_failing_private_test_file_and_keeps_each_ones_output(self):
        release_main(['start', 'failures', *self.options()], self.public)
        private = self.worktrees / 'failures/private'
        for folder, body in (('first', 'self.fail("first reason")'),
                             ('hangs', 'print("hangs output", flush=True); '
                                       'print("hangs reason", file=__import__("sys").stderr, flush=True); '
                                       '__import__("time").sleep(3600)'),
                             ('second', 'raise ValueError("second reason")')):
            (private / 'config' / folder).mkdir(parents=True)
            (private / 'config' / folder / 'test_tool.py').write_text(
                PASSING_TEST.replace('        pass', f'        {body}').replace('test_passes', f'test_{folder}')
                + RUN_ALONE,
                encoding='utf-8')
        (private / 'test_passing.py').write_text(PASSING_TEST + RUN_ALONE, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add failing tests\n', encoding='utf-8')
        with redirect_stderr(io.StringIO()) as error, patch.object(release, 'PRIVATE_TEST_TIMEOUT_SECONDS', 10):
            self.assertEqual(release_main(['finish', 'failures', '--private-message', str(message), *self.options()],
                                          self.public), 3)
        self.assertIn('the private tests in config/first/test_tool.py failed (test_first); config/hangs/test_tool.py '
                      'ran past the 10 s limit; the private tests in config/second/test_tool.py failed (test_second); '
                      'read', error.getvalue())
        log = (self.worktrees / 'failures/build/private-tests.log').read_text(encoding='utf-8')
        for folder, kept in (('first', ['FAIL: test_first', 'AssertionError: first reason']),
                             ('hangs', ['hangs output', 'hangs reason']),
                             ('second', ['ERROR: test_second', 'ValueError: second reason'])):
            block = log.split(f'== config/{folder}/test_tool.py\n')[1].split('\n== ')[0]
            for text in kept:
                self.assertIn(text, block)
        self.assertIn('== test_passing.py\n', log)

    def test_finish_stops_on_a_private_test_file_link_that_points_at_nothing(self):
        release_main(['start', 'link', *self.options()], self.public)
        (self.worktrees / 'link/private/config/tool').mkdir()
        (self.worktrees / 'link/private/config/tool/test_tool.py').symlink_to('tool_tests.py')
        message = self.root / 'message'
        message.write_text('Link a test\n', encoding='utf-8')
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'link', '--private-message', str(message), *self.options()],
                                          self.public), 3)
        self.assertIn('these private test file links point at nothing, so their tests would not run: '
                      'config/tool/test_tool.py;', error.getvalue())
        self.assertFalse((self.worktrees / 'link/build/tests.log').exists())

    def test_the_private_tests_pass_in_a_session_that_forces_colour(self):
        release_main(['start', 'coloured', *self.options()], self.public)
        # The file runs with -I, which ignores PYTHON_COLORS, but the Python run it starts does not use -I.
        (self.worktrees / 'coloured/private/test_tool.py').write_text(
            'import subprocess\nimport sys\nimport unittest\n\n\nclass Seed(unittest.TestCase):\n'
            '    def test_a_child_writes_no_colour(self):\n'
            '        child = subprocess.run([sys.executable, "-c", "1/0"], capture_output=True, text=True)\n'
            '        self.assertNotIn("\\x1b[", child.stderr)\n' + RUN_ALONE, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a private test\n', encoding='utf-8')
        os.environ.update(FORCE_COLOR='1', PYTHON_COLORS='1')
        os.environ.pop('NO_COLOR', None)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(['finish', 'coloured', '--private-message', str(message), *self.options()],
                                          self.public), 0)
        self.assertIn('Ran 1 private tests in 1 folders: OK', output.getvalue())

    def test_finish_runs_a_private_test_file_in_a_folder_whose_name_is_not_ascii(self):
        release_main(['start', 'named', *self.options()], self.public)
        folder = self.worktrees / 'named/private/config/straße'
        folder.mkdir(parents=True)
        (folder / 'test_tool.py').write_text(FAILING_TEST + RUN_ALONE, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a private tool\n', encoding='utf-8')
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'named', '--private-message', str(message), *self.options()],
                                          self.public), 3)
        self.assertIn('the private tests in config/straße/test_tool.py failed', error.getvalue())

    def test_finish_does_not_run_a_private_test_file_the_release_deletes(self):
        seed = self.clone(self.private_origin, 'private-seed')
        for name in ('test_kept.py', 'test_gone.py'):
            (seed / name).write_text(PASSING_TEST + RUN_ALONE, encoding='utf-8')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-qm', 'Two tests')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')
        release_main(['start', 'fewer', *self.options()], self.public)
        (self.worktrees / 'fewer/private/test_gone.py').unlink()
        message = self.root / 'message'
        message.write_text('Delete a test\n', encoding='utf-8')
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(['finish', 'fewer', '--private-message', str(message), '--push',
                                           *self.options()], self.public), 0)
        self.assertIn('Ran 1 private tests in 1 folders: OK', output.getvalue())
        self.assertEqual(git(self.private_origin, 'ls-tree', '--name-only', 'main', 'test_gone.py'), '')

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

        def make_a_file_during_the_wait(*arguments):
            (tree / 'late.md').write_text('left out\n', encoding='utf-8')
            return hold(*arguments)

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
        self.assertIn('The tests did not run again', output.getvalue())
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

    def test_a_finish_tests_without_the_lock_and_waits_for_another_release_only_to_commit(self):
        import portable
        release_main(['start', 'second', *self.options()], self.public)
        tree = self.worktrees / 'second/public'
        (tree / 'tests/test_other.py').write_text(PASSING_TEST, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a test\n', encoding='utf-8')
        finish = ['finish', 'second', '--message', str(message), '--push', '--timeout', '0', *self.options()]
        lock = self.root / 'state/release.lock'
        with portable.exclusive_lock(lock, record=True):
            with redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(release_main(finish, self.public), 3)
        # The other release held the lock all the while, and this one's tests ran and passed all the same.
        self.assertIn('Ran 2 tests', output.getvalue())
        self.assertIn(f'another release, pull or sync still runs here after 0 seconds (release PID {os.getpid()} since',
                      error.getvalue())
        self.assertIn('committed nothing', error.getvalue())
        self.assertEqual(git(tree, 'log', '-1', '--format=%s'), 'seed')
        self.assertEqual(git(self.public_origin, 'ls-tree', '--name-only', 'main', 'tests/'), SEEDED_TESTS)
        # The lock is this finish's own from the commit on: no other release can take it then.
        commit, taken = release.commit, []

        def commit_under_the_lock(*arguments):
            with self.assertRaises(BlockingIOError):
                with portable.exclusive_lock(lock):
                    pass
            taken.append(release.running(('release',)))
            commit(*arguments)

        with patch.object(release, 'commit', commit_under_the_lock), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(finish, self.public), 0)
        self.assertIn(f'release PID {os.getpid()} since', taken[0][0])
        self.assertRegex(output.getvalue(), r'Held the release lock for \d+ seconds, after waiting \d+ seconds for it\n')
        self.assertLess(output.getvalue().index('The tests did not run again'),
                        output.getvalue().index('Held the release lock'))
        self.assertIsNone(portable.lock_holder(lock))
        with portable.exclusive_lock(lock):  # and it is free again once the finish has ended
            pass
        self.assertIn('tests/test_other.py', git(self.public_origin, 'ls-tree', '--name-only', 'main', 'tests/'))

    def test_another_release_goes_out_while_a_finish_runs_its_tests(self):
        message = self.root / 'message'
        message.write_text('Change one file\n', encoding='utf-8')
        for name, file in (('slow', 'content'), ('quick', 'doc')):
            release_main(['start', name, *self.options()], self.public)
            (self.worktrees / name / 'public' / file).write_text(f'{name}\n', encoding='utf-8')
        suite, finished = release.test_suite, []

        def release_the_other_during_the_suite(public, *arguments):
            suite(public, *arguments)
            if public.parent.name == 'slow':
                finished.append(release_main(['finish', 'quick', '--message', str(message), '--push', *self.options()],
                                             self.public))

        with patch.object(release, 'test_suite', release_the_other_during_the_suite), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(release_main(['finish', 'slow', '--message', str(message), '--push', *self.options()],
                                          self.public), 0)
        self.assertEqual(finished, [0])
        self.assertEqual(git(self.public_origin, 'show', 'main:doc'), 'quick')
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'slow')

    def test_a_finish_waits_for_the_lock_and_again_when_another_waiter_took_it_first(self):
        import portable
        import threading
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        lock = self.root / 'state/release.lock'
        # Without record the holder writes no note, which is what a waiter meets when another waiter took the lock
        # a moment before it: running() names no release, and the lock is taken.
        for name, record in (('noted', True), ('unnoted', False)):
            release_main(['start', name, *self.options()], self.public)
            (self.worktrees / name / 'public/content').write_text(f'{name}\n', encoding='utf-8')
            held, freed = threading.Event(), threading.Event()

            def hold_for_a_while():
                with portable.exclusive_lock(lock, record=record):
                    held.set()
                    freed.wait(60)

            holder = threading.Thread(target=hold_for_a_while)
            holder.start()
            self.addCleanup(holder.join)
            self.addCleanup(freed.set)
            self.assertTrue(held.wait(60))

            def free_the_lock(seconds, freed=freed, holder=holder):
                freed.set()
                holder.join()

            with self.subTest(name), patch.object(release, 'lock_pause', free_the_lock), \
                    redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()):
                self.assertEqual(release_main(['finish', name, '--message', str(message), '--push', '--timeout', '60',
                                               *self.options()], self.public), 0)
                self.assertEqual('Waiting for release PID' in error.getvalue(), record)
                self.assertTrue(freed.is_set(), 'the finish never paused for the lock')
            self.assertEqual(git(self.public_origin, 'show', 'main:content'), name)
        release_main(['start', 'late', *self.options()], self.public)
        (self.worktrees / 'late/public/content').write_text('late\n', encoding='utf-8')
        with portable.exclusive_lock(lock), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'late', '--message', str(message), '--push', '--timeout', '0',
                                           *self.options()], self.public), 3)
        self.assertIn(f'still runs here after 0 seconds (another run holds {lock})', error.getvalue())
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'unnoted')
        # A pull is waited for like a release: one that runs as the finish starts, and one that starts during the tests.
        pull, tested = portable.exclusive_lock(self.root / 'state/pull.lock', record=True), release.tested
        finish = ['finish', 'late', '--message', str(message), '--push', '--timeout', '0', *self.options()]
        with pull, redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertIn('another pull or sync still runs here after 0 seconds; this run committed nothing', error.getvalue())

        def pull_starts_during_the_tests(*arguments):
            self.enterContext(portable.exclusive_lock(self.root / 'state/pull.lock', record=True))
            return tested(*arguments)

        with patch.object(release, 'tested', pull_starts_during_the_tests), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertIn(f'another release, pull or sync still runs here after 0 seconds (pull PID {os.getpid()} since',
                      error.getvalue())
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'unnoted')

    def test_a_later_finish_of_the_same_files_does_not_run_the_tests_again(self):
        import portable
        release_main(['start', 'again', *self.options()], self.public)
        tree = self.worktrees / 'again/public'
        module = tree / 'tests/test_other.py'
        module.write_text(FAILING_TEST, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a test\n', encoding='utf-8')
        finish = ['finish', 'again', '--message', str(message), '--push', '--timeout', '0', *self.options()]
        note, lock = self.worktrees / 'again/build/tests-passed', self.root / 'state/release.lock'
        suite, runs = release.test_suite, []

        def counted(*arguments):
            runs.append(arguments)
            suite(*arguments)

        def stopped_behind_the_tests():
            with portable.exclusive_lock(lock, record=True), redirect_stderr(io.StringIO()), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(release_main(finish, self.public), 3)
            return output.getvalue()

        with patch.object(release, 'test_suite', counted):
            stopped_behind_the_tests()  # on the failing test
            self.assertFalse(note.exists())
            module.write_text(PASSING_TEST, encoding='utf-8')
            stopped_behind_the_tests()
            self.assertEqual(len(runs), 2)  # a failed run vouches for nothing
            self.assertIn('The tests did not run again: they passed on these files before', stopped_behind_the_tests())
            self.assertEqual(len(runs), 2)
            module.write_text(PASSING_TEST + '\n# another line\n', encoding='utf-8')
            self.assertNotIn('The tests did not run again', stopped_behind_the_tests())
            self.assertEqual(len(runs), 3)  # other files than the ones that passed
            (tree / 'notes').write_text('untracked\n', encoding='utf-8')
            (tree / '.gitignore').write_text(ALLOWLIST + '!/notes\n', encoding='utf-8')
            stopped_behind_the_tests()
            self.assertEqual(len(runs), 4)  # a new file counts too
            note.write_text('not JSON', encoding='utf-8')
            stopped_behind_the_tests()
            self.assertEqual(len(runs), 5)  # a note that cannot be read vouches for nothing
            git(tree, 'add', '-A')
            git(tree, 'commit', '-qm', 'By hand')
            stopped_behind_the_tests()
            self.assertEqual(len(runs), 5)  # the same files, committed
            (tree / 'notes').write_text('edited\n', encoding='utf-8')
            git(tree, 'commit', '-qam', 'By hand again')
            stopped_behind_the_tests()
            self.assertEqual(len(runs), 6)  # a commit of other files is no worktree without work of its own
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(release_main(finish, self.public), 0)
        self.assertEqual(len(runs), 6)
        self.assertIn('The tests did not run again', output.getvalue())
        self.assertEqual(git(self.public_origin, 'show', 'main:notes'), 'edited')

    def test_a_worktree_without_work_of_its_own_stays_tested_when_origin_moves(self):
        import portable
        release_main(['start', 'half', *self.options()], self.public)
        (self.worktrees / 'half/private/notes').write_text('mine\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add notes\n', encoding='utf-8')
        finish = ['finish', 'half', '--private-message', str(message), '--push', '--timeout', '0', *self.options()]
        with portable.exclusive_lock(self.root / 'state/release.lock', record=True), redirect_stderr(io.StringIO()), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(release_main(finish, self.public), 3)
        # Another session's push moves origin/main, and with it the public worktree, which holds none of this
        # release's work. finish tests no rebase either.
        self.push_from_another_session(self.public_origin, 'other', {'doc': 'theirs\n'})
        git(self.public, 'fetch', '-q', 'origin')  # as that session's pull does, for every worktree of the checkout
        with patch.object(release, 'test_suite', side_effect=AssertionError('the tests ran again')), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(release_main(finish, self.public), 0)
        self.assertEqual((self.worktrees / 'half/public/doc').read_text(encoding='utf-8'), 'theirs\n')
        self.assertEqual(git(self.private_origin, 'show', 'main:notes'), 'mine')

    def test_finish_stops_on_files_that_changed_during_or_after_their_tests(self):
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        suite, hold = release.test_suite, release.hold
        for name, told in (('during', 'the worktrees changed while their tests ran (public: content), so the tests '
                            'vouch for neither state; finish again once the edits are done; this run committed nothing'),
                           ('after', 'the worktrees changed after their tests ran (public: content), and the commit '
                            'took the change; finish again, which tests them as they are now; nothing was pushed')):
            release_main(['start', name, *self.options()], self.public)
            file = self.worktrees / name / 'public/content'
            file.write_text(f'tested {name}\n', encoding='utf-8')

            def edit_during_the_suite(*arguments):
                suite(*arguments)
                file.write_text(f'slipped in {name}\n', encoding='utf-8')

            def edit_during_the_wait(*arguments):
                file.write_text(f'slipped in {name}\n', encoding='utf-8')
                return hold(*arguments)

            late = (patch.object(release, 'test_suite', edit_during_the_suite) if name == 'during' else
                    patch.object(release, 'hold', edit_during_the_wait))
            finish = ['finish', name, '--message', str(message), '--push', *self.options()]
            with self.subTest(name), late, redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()):
                self.assertEqual(release_main(finish, self.public), 3)
            self.assertIn(told, error.getvalue())
            self.assertEqual(git(self.public_origin, 'show', 'main:content'),
                             'first' if name == 'during' else 'slipped in during')
            self.assertEqual((self.worktrees / name / 'build/tests-passed').exists(), name == 'after')
            # The next finish tests the files as they are now, and releases them.
            runs = []

            def counted(*arguments):
                runs.append(arguments)
                suite(*arguments)

            with patch.object(release, 'test_suite', counted), redirect_stdout(io.StringIO()):
                self.assertEqual(release_main(finish, self.public), 0)
            self.assertEqual(len(runs), 1)
            self.assertEqual(git(self.public_origin, 'show', 'main:content'), f'slipped in {name}')

    def test_each_run_of_the_tests_keeps_the_logs_of_the_run_before(self):
        import portable
        release_main(['start', 'logs', *self.options()], self.public)
        (self.worktrees / 'logs/public/tests/test_other.py').write_text(PASSING_TEST, encoding='utf-8')
        folder = self.worktrees / 'logs/private/config/tool'
        folder.mkdir(parents=True)
        module = folder / 'test_tool.py'
        module.write_text(FAILING_TEST + RUN_ALONE, encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add tests\n', encoding='utf-8')
        finish = ['finish', 'logs', '--message', str(message), '--private-message', str(message), '--push',
                  *self.options()]
        build = self.worktrees / 'logs/build'
        with redirect_stderr(io.StringIO()):
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertIn('broken', (build / 'private-tests.log').read_text(encoding='utf-8'))
        self.assertEqual(sorted(path.name for path in build.glob('*.log')), ['private-tests.log', 'tests.log'])
        module.write_text(PASSING_TEST + RUN_ALONE, encoding='utf-8')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(release_main(finish, self.public), 0)
        self.assertNotIn('broken', (build / 'private-tests.log').read_text(encoding='utf-8'))
        for name, told in (('tests', 'Ran 2 tests'), ('private-tests', 'broken')):
            kept = list(build.glob(f'{name}.*.log'))
            self.assertEqual(len(kept), 1)
            self.assertRegex(kept[0].name, rf'^{name}\.\d{{8}}-\d{{6}}\.log$')
            self.assertIn(told, kept[0].read_text(encoding='utf-8'))
            kept[0].unlink()
        # Two logs written in the same second both stay, and an empty one is not worth a name.
        log = build / 'private-tests.log'
        stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(log.stat().st_mtime))
        first = release.keep(log)
        self.assertEqual(first.name, f'private-tests.{stamp}.log')
        log.write_text('second\n', encoding='utf-8')
        os.utime(log, (first.stat().st_mtime, first.stat().st_mtime))
        self.assertEqual(release.keep(log).name, f'private-tests.{stamp}-2.log')
        self.assertIn('== config/tool', first.read_text(encoding='utf-8'))
        log.write_text('', encoding='utf-8')
        self.assertIsNone(release.keep(log))
        self.assertIsNone(release.keep(build / 'absent.log'))

    def test_a_file_the_allowlist_leaves_out_keeps_the_tests_from_counting_as_passed(self):
        release_main(['start', 'needed', *self.options()], self.public)
        tree = self.worktrees / 'needed/public'
        (tree / 'tests/test_other.py').write_text(
            'from pathlib import Path\nimport unittest\n\n\nclass Needs(unittest.TestCase):\n'
            '    def test_the_file_is_there(self):\n'
            '        self.assertTrue((Path(__file__).resolve().parents[1] / "needed.md").is_file())\n', encoding='utf-8')
        (tree / 'needed.md').write_text('the test passes only beside this file\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a test\n', encoding='utf-8')
        finish = ['finish', 'needed', '--message', str(message), '--push', *self.options()]
        with redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertIn('Ran 2 tests', output.getvalue())
        self.assertIn('.gitignore leaves out needed.md;', error.getvalue())
        self.assertFalse((self.worktrees / 'needed/build/tests-passed').exists())
        # Deleting the file, as the stop offers, leaves the files of the commit as they were. The tests run again.
        (tree / 'needed.md').unlink()
        with redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertNotIn('The tests did not run again', output.getvalue())
        self.assertIn('the test suite failed', error.getvalue())
        self.assertEqual(git(self.public_origin, 'ls-tree', '--name-only', 'main', 'tests/'), SEEDED_TESTS)

    def test_a_failed_read_of_the_engines_commit_stops_the_finish_with_gits_words(self):
        release_main(['start', 'unread', *self.options()], self.public)
        tree = self.worktrees / 'unread/public'
        (tree / 'content').write_text('unread\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        finish = ['finish', 'unread', '--message', str(message), '--push', *self.options()]
        read = release.git
        for fails_at, done in ((1, 'nothing'), (2, 'the tests')):  # as the finish starts, and before it takes the lock
            reads = []

            def engine_unreadable(root, *arguments):
                if root == self.public and arguments == ('rev-parse', 'HEAD'):
                    reads.append(arguments)
                    if len(reads) == fails_at:
                        raise RuntimeError('dotfiles: git rev-parse failed: fatal: not a git repository')
                return read(root, *arguments)

            with self.subTest(fails_at=fails_at), patch.object(release, 'git', engine_unreadable), \
                    redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()):
                self.assertEqual(release_main(finish, self.public), 3)
            self.assertIn('Release stopped: dotfiles: git rev-parse failed: fatal: not a git repository', error.getvalue())
            self.assertEqual(len(reads), fails_at)
            self.assertEqual((self.worktrees / 'unread/build/tests-passed').exists(), done == 'the tests')
            self.assertEqual(git(tree, 'log', '-1', '--format=%s'), 'seed')
            self.assertFalse((self.root / 'state/release.lock.holder').exists())
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'first')

    def test_start_finish_and_clean_try_each_fetch_that_fails_a_second_time(self):
        import pull
        fetch, fetches = pull.git, []

        def every_first_fetch_fails(root, *arguments):
            if arguments[0] == 'fetch':
                fetches.append(root)
                if len(fetches) % 2:  # as when another release's pull fetches into the same repository at that moment
                    raise RuntimeError(f"{root.name}: git fetch failed: error: cannot lock ref 'refs/remotes/origin/main'")
            return fetch(root, *arguments)

        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        # release.py and pull.py each hold the name git, and a fetch that went past the helper would use release's.
        with patch.object(pull, 'git', every_first_fetch_fails), patch.object(release, 'git', every_first_fetch_fails), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(release_main(['start', 'beside', *self.options()], self.public), 0)
            self.assertEqual(len(fetches), 4)
            (self.worktrees / 'beside/public/content').write_text('beside\n', encoding='utf-8')
            self.assertEqual(release_main(['finish', 'beside', '--message', str(message), '--push', *self.options()],
                                          self.public), 0)
            finished = len(fetches)
            self.assertEqual(release_main(['clean', 'beside', *self.options()], self.public), 0)
        # start fetches both repositories; finish asks each tree for conflicts and overlap before the tests and after
        # the commit, and rebases it; clean fetches both. Each of those fetched twice.
        self.assertEqual(finished, 2 * (2 + 2 * (2 + 2 + 1)))
        self.assertEqual(len(fetches), finished + 2 * 2)
        self.assertEqual(fetches[0::2], fetches[1::2])
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'beside')
        self.assertFalse((self.worktrees / 'beside').exists())

    def test_a_second_finish_of_one_release_stops_and_status_names_the_first(self):
        release_main(['start', 'once', *self.options()], self.public)
        (self.worktrees / 'once/public/content').write_text('once\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        finish = ['finish', 'once', '--message', str(message), '--push', *self.options()]
        suite, seen = release.test_suite, {}

        def a_second_finish_and_status_during_the_suite(*arguments):
            suite(*arguments)
            with redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()) as output:
                seen['second'] = release_main(finish, self.public)
                release_main(['status', *self.options()], self.public)
            seen['error'], seen['status'] = error.getvalue(), output.getvalue()

        with patch.object(release, 'test_suite', a_second_finish_and_status_during_the_suite), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(release_main(finish, self.public), 0)
        self.assertEqual(seen['second'], 3)
        self.assertIn(f'another finish of once runs (PID {os.getpid()} since', seen['error'])
        self.assertIn('this run did nothing', seen['error'])
        self.assertIn('Running on this machine: no release, pull or sync', seen['status'])  # it is in its tests
        self.assertIn(f'  once: its finish runs, PID {os.getpid()} since', seen['status'])
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'once')
        with redirect_stdout(io.StringIO()) as output:
            release_main(['status', *self.options()], self.public)
        self.assertNotIn('its finish runs', output.getvalue())
        # The moment after another finish took the release's lock, before it wrote its note.
        import portable
        with portable.exclusive_lock(self.worktrees / 'once/build/finish.lock'), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertIn('another finish of once runs (no process it names)', error.getvalue())
        # A finish of a release that was never started makes no folder for its lock.
        with redirect_stderr(io.StringIO()):
            self.assertEqual(release_main(['finish', 'absent', *self.options()], self.public), 3)
        self.assertFalse((self.worktrees / 'absent').exists())

    def test_a_finish_loads_the_engine_modules_it_needs_before_its_first_check(self):
        release_main(['start', 'early', *self.options()], self.public)
        loaded = []

        def first_check(tree, name):
            loaded.append(all(module in sys.modules for module in ('description', 'description_toml', 'drift', 'host_state')))
            return 'stopped here'

        with patch.dict(sys.modules), patch.object(release, 'unfinished', first_check), redirect_stderr(io.StringIO()):
            for module in ('description_toml', 'drift', 'host_state'):
                sys.modules.pop(module, None)
            self.assertEqual(release_main(['finish', 'early', *self.options()], self.public), 3)
        self.assertEqual(loaded, [True, True])

    def test_a_stop_names_how_the_worktrees_changed(self):
        release_main(['start', 'named', *self.options()], self.public)
        public, private = self.worktrees / 'named/public', self.worktrees / 'named/private'
        before = release.tested_state(public, private)
        self.assertEqual(before, {'public': 'origin/main', 'private': 'origin/main'})
        (public / 'content').write_text('named\n', encoding='utf-8')
        first = release.tested_state(public, private)
        self.assertEqual(release.state_changes(public, private, before, first), 'public: it holds work where it held none')
        self.assertEqual(release.state_changes(public, private, first, before), 'public: its work is gone')
        self.assertEqual(release.state_changes(public, private, first, first), '')
        (public / '.gitignore').write_text(ALLOWLIST + '!/more/\n!/more/**\n', encoding='utf-8')
        (public / 'more').mkdir()
        for number in range(9):
            (public / 'more' / f'file{number}').write_text('more\n', encoding='utf-8')
        (private / 'notes').write_text('mine\n', encoding='utf-8')
        second = release.tested_state(public, private)
        self.assertEqual(release.state_changes(public, private, first, second),
                         'public: .gitignore, more/file0, more/file1, more/file2, more/file3, more/file4, more/file5, '
                         'more/file6 and 2 more; private: it holds work where it held none')

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
        self.assertIn("No release holds this machine's lock and no pull or sync runs here.", output.getvalue())

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
        self.assertEqual(release.edited_owned(self.public, self.private, self.private), {})

    def test_an_owned_source_edited_after_its_pin_is_named(self):
        import description as d
        import description_toml
        from host_state import DeviceRoots
        (self.public / release.SETUP).parent.mkdir()
        (self.public / release.SETUP).write_text('schema = 1\n', encoding='utf-8')
        skill = self.public / 'skills/mine/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('Pinned\n', encoding='utf-8')
        mine = d.LooseSkill('mine', d.Visibility.PUBLIC, '',
                            d.LocalSource('skills/mine', DeviceRoots.current().tree_digest(skill.parent)), {})
        setup = d.Setup(roots=d.Roots(self.public / 'setup', None), packages={}, repos={}, loose={'mine': mine},
                        host_skills={}, rules=None, note_rules=(), public_repositories=frozenset(), lock={})
        with patch.object(description_toml, 'load', return_value=setup):
            self.assertEqual(release.edited_owned(self.public, self.private, self.private), {})
            skill.write_text('Edited\n', encoding='utf-8')
            self.assertEqual(release.edited_owned(self.public, self.private, self.private), {'mine': 'skills/mine'})

    def unloadable_setup(self, name):
        """Start a release whose public setup this engine cannot load, as an engine that retired a build cannot load
        a setup that still names it, and return its two worktrees."""
        release_main(['start', name, *self.options()], self.public)
        public, private = self.worktrees / name / 'public', self.worktrees / name / 'private'
        (public / release.SETUP).parent.mkdir(parents=True)
        (public / release.SETUP).write_text('[retired]\nbuild = "academic"\n', encoding='utf-8')
        return public, private

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

    def test_finish_on_a_setup_an_engine_newer_than_both_worktrees_cannot_load_says_to_commit_and_rebase(self):
        public, private = self.unloadable_setup('stale')
        (public / 'content').write_text('mine\n', encoding='utf-8')
        (private / 'notes').write_text('mine\n', encoding='utf-8')
        self.another_session(self.public_origin, 'content', 'theirs\n')
        self.another_session(self.private_origin, 'notes', 'theirs\n')
        for canonical in (self.public, self.private):  # the pull that the finish waited for
            git(canonical, 'pull', '-q', '--ff-only')

        said = self.stopped_finish('stale')

        self.assertIn('Release stopped: the setup does not load:\n', said)
        self.assertIn('retired: unknown key', said)
        self.assertIn(f'This branch is older than the engine: {public}, {private} lacks commits the engine holds', said)
        self.assertIn('Another session changed the same files on origin/main since this branch started '
                      '(public: content; private: notes). Read its commits with git log -p HEAD..origin/main -- FILE '
                      'and reconcile the two designs before you rebase', said)
        self.assertIn(f'run git -C {public} rebase origin/main and git -C {private} rebase origin/main, settle any '
                      'conflict it stops on and git rebase --continue', said)
        self.assertIn('this run committed nothing', said)
        self.assertEqual(git(public, 'status', '--porcelain', '--', 'content'), 'M content')

    def test_following_the_stop_for_an_older_branch_keeps_its_review_and_releases(self):
        seed = self.clone(self.public_origin, 'setup-seed')
        (seed / release.SETUP).parent.mkdir(parents=True)
        (seed / release.SETUP).write_text('[retired]\nbuild = "academic"\n', encoding='utf-8')
        git(seed, 'add', '-f', release.SETUP)
        git(seed, 'commit', '-qm', 'A setup the next engine retires')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')
        git(self.public, 'pull', '-q', '--ff-only')
        release_main(['start', 'follow', *self.options()], self.public)
        public = self.worktrees / 'follow/public'
        (public / 'tests/helper.py').write_text('VALUE = 1\n', encoding='utf-8')
        git(seed, 'rm', '-q', release.SETUP)  # the release that retired the build
        git(seed, 'commit', '-qm', 'Retire the build')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')
        git(self.public, 'pull', '-q', '--ff-only')
        reviewed = release.review_lines(public, self.worktrees / 'follow/private')
        record = self.root / 'review.md'
        record.write_text('No findings.\n' + ''.join(f'{line}\n' for _, line in reviewed.values()), encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a helper\n', encoding='utf-8')
        finish = ['finish', 'follow', '--message', str(message), '--reviewed', str(record), '--push', *self.options()]

        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(finish, self.public), 3)
        self.assertIn(f'run git -C {public} rebase origin/main, settle', error.getvalue())
        git(public, 'add', '-A')
        git(public, 'commit', '-q', '-F', str(message))
        git(public, 'rebase', '-q', 'origin/main')

        self.assertEqual(release.review_lines(public, self.worktrees / 'follow/private'), reviewed)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(finish, self.public), 0)
        self.assertIn('pushed', output.getvalue())
        self.assertEqual(git(self.public_origin, 'show', 'main:tests/helper.py'), 'VALUE = 1')

    def test_finish_brings_a_worktree_without_work_of_its_own_up_to_the_engine(self):
        seed = self.clone(self.public_origin, 'setup-seed')
        (seed / release.SETUP).parent.mkdir(parents=True)
        (seed / release.SETUP).write_text('[retired]\nbuild = "academic"\n', encoding='utf-8')
        git(seed, 'add', '-f', release.SETUP)
        git(seed, 'commit', '-qm', 'A setup the next engine retires')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')
        git(self.public, 'pull', '-q', '--ff-only')
        release_main(['start', 'private-only', *self.options()], self.public)
        (self.worktrees / 'private-only/private/notes').write_text('mine\n', encoding='utf-8')
        git(seed, 'rm', '-q', release.SETUP)  # the release that retired the build, pulled here meanwhile
        git(seed, 'commit', '-qm', 'Retire the build')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')
        git(self.public, 'pull', '-q', '--ff-only')
        message = self.root / 'message'
        message.write_text('Add notes\n', encoding='utf-8')

        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
            status = release_main(['finish', 'private-only', '--private-message', str(message), '--push',
                                   *self.options()], self.public)

        self.assertEqual(status, 0, error.getvalue())
        self.assertIn('pushed', output.getvalue())
        self.assertEqual(git(self.private_origin, 'show', 'main:notes'), 'mine')
        self.assertEqual(git(self.worktrees / 'private-only/public', 'rev-parse', 'HEAD'),
                         git(self.public_origin, 'rev-parse', 'main'))

    def test_finish_names_only_the_worktree_that_is_older_than_its_checkout(self):
        public, private = self.unloadable_setup('half')
        (private / 'notes').write_text('mine\n', encoding='utf-8')
        (public / 'content').write_text('mine\n', encoding='utf-8')
        self.another_session(self.private_origin, 'elsewhere', 'theirs\n')
        git(self.private, 'pull', '-q', '--ff-only')
        # Origin changed the public file too, but the engine here has not pulled it, so the public worktree is not
        # older than the engine, and the overlap check after the tests still names that file.
        self.another_session(self.public_origin, 'content', 'theirs\n')

        said = self.stopped_finish('half')

        self.assertIn(f'This branch is older than the engine: {private} lacks commits the engine holds', said)
        self.assertIn(f'run git -C {private} rebase origin/main, settle any conflict', said)
        self.assertNotIn(f'git -C {public} rebase', said)
        self.assertNotIn('Another session', said)

    def test_finish_on_an_engine_older_than_the_branch_says_to_pull(self):
        for origin, canonical, path in ((self.public_origin, self.public, 'content'),
                                        (self.private_origin, self.private, 'notes')):
            with self.subTest(canonical=canonical.name):
                self.another_session(origin, path, 'on origin, but not yet pulled here\n')
                name = f'ahead-{canonical.name}'
                self.unloadable_setup(name)

                said = self.stopped_finish(name)

                self.assertIn('retired: unknown key', said)
                self.assertIn(f'The engine is older than this branch: {canonical} lacks commits the branch started '
                              f'from. Pull this machine with {sys.executable} -B {self.public / "pull.py"} --update '
                              '--apply', said)
                self.assertNotIn('rebase', said)
                git(canonical, 'pull', '-q', '--ff-only')

    def test_a_worktree_without_work_of_its_own_is_brought_up_before_the_setup_is_read(self):
        public, private = self.unloadable_setup('level')
        self.another_session(self.private_origin, 'elsewhere', 'theirs\n')
        git(self.private, 'pull', '-q', '--ff-only')

        said = self.stopped_finish('level')

        self.assertIn('retired: unknown key\nFix it, then finish again', said)  # the private worktree is level now
        self.assertEqual(git(private, 'rev-parse', 'HEAD'), git(self.private_origin, 'rev-parse', 'main'))

    def test_a_commit_git_cannot_find_is_a_stop_with_its_words(self):
        with self.assertRaisesRegex(RuntimeError, 'git merge-base exited 128: .+'):
            release.holds(self.public, '0' * 40)

    def test_finish_on_a_setup_the_branch_itself_broke_names_the_problem(self):
        self.unloadable_setup('broken')

        said = self.stopped_finish('broken')

        self.assertIn('Release stopped: the setup does not load:\n', said)
        self.assertIn('retired: unknown key\nFix it, then finish again; this run committed nothing', said)
        self.assertNotIn('rebase', said)

    def test_finish_keeps_the_setup_problem_when_it_cannot_tell_whether_the_branch_is_older(self):
        self.unloadable_setup('offline')
        (self.public / 'doc').write_text('pulled\n', encoding='utf-8')
        git(self.public, 'commit', '-qam', 'Pulled here')  # the engine moved on
        git(self.public, 'remote', 'set-url', 'origin', str(self.root / 'unreachable'))

        said = self.stopped_finish('offline')

        self.assertIn('retired: unknown key\nfinish could not tell whether the worktrees are older than the engine: ',
                      said)
        self.assertIn('this run committed nothing', said)

    def test_finish_waits_for_a_stopped_rebase_and_then_releases_every_commit(self):
        release_main(['start', 'clash', *self.options()], self.public)
        public = self.worktrees / 'clash/public'
        for path, text in (('content', 'mine\n'), ('doc', 'mine\n')):
            (public / path).write_text(text, encoding='utf-8')
            git(public, 'commit', '-qam', f'Change {path}')
        self.another_session(self.public_origin, 'content', 'theirs\n')
        git(public, 'fetch', '-q', 'origin')
        self.assertNotEqual(subprocess.run(['git', '-C', str(public), 'rebase', 'origin/main'],
                                           capture_output=True).returncode, 0)
        stopped = (f'a rebase stopped part way in {public}; settle its conflicts, git add each file and run '
                   f'git -C {public} rebase --continue, then finish again; this run committed nothing')

        self.assertIn(stopped, self.stopped_finish('clash', '--overlap-reviewed'))
        (public / 'content').write_text('both\n', encoding='utf-8')
        git(public, 'add', 'content')
        self.assertIn(stopped, self.stopped_finish('clash', '--overlap-reviewed'))  # settled, but not continued
        subprocess.run(['git', '-C', str(public), 'rebase', '--continue'], env={**os.environ, 'GIT_EDITOR': 'true'},
                       capture_output=True, check=True)

        with redirect_stdout(io.StringIO()):
            self.assertEqual(release_main(['finish', 'clash', '--push', '--overlap-reviewed', *self.options()],
                                          self.public), 0)
        self.assertEqual((git(self.public_origin, 'show', 'main:content'), git(self.public_origin, 'show', 'main:doc')),
                         ('both', 'mine'))

    def test_finish_stops_on_files_a_stash_left_in_a_conflict(self):
        cases = [(side, origin, path, change) for side, origin, path in (('public', self.public_origin, 'content'),
                                                                         ('private', self.private_origin, 'notes'))
                 for change in ('edit', 'delete')]
        seed = self.clone(self.private_origin, 'notes-seed')
        (seed / 'notes').write_text('first\n', encoding='utf-8')
        git(seed, 'add', 'notes')
        git(seed, 'commit', '-qm', 'Add notes')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')
        for side, origin, path, change in cases:
            with self.subTest(side=side, change=change):
                name = f'stash-{side}-{change}'
                release_main(['start', name, *self.options()], self.public)
                tree = self.worktrees / name / side
                if change == 'edit':
                    (tree / path).write_text('mine\n', encoding='utf-8')
                    git(tree, 'add', path)
                else:  # a delete/modify conflict leaves no markers to take out
                    git(tree, 'rm', '-q', path)
                git(tree, 'stash', '-q')
                self.another_session(origin, path, f'theirs {change}\n')
                git(tree, 'fetch', '-q', 'origin')
                git(tree, 'merge', '-q', '--ff-only', 'origin/main')
                self.assertNotEqual(subprocess.run(['git', '-C', str(tree), 'stash', 'pop'],
                                                   capture_output=True).returncode, 0)

                said = self.stopped_finish(name, '--overlap-reviewed')

                self.assertIn(f'files in a conflict that is not settled ({side}: {path}); settle each one: take out '
                              'its conflict markers and git add it, or, where one side deleted it, git add it to keep '
                              'it or git rm it to delete it, then finish again; this run committed nothing', said)

    def test_finish_stops_on_each_step_that_stopped_part_way(self):
        def rebase(tree):
            return ['rebase', 'origin/main']

        def apply_rebase(tree):
            return ['rebase', '--apply', 'origin/main']

        def merge(tree):
            return ['merge', '-q', '--no-edit', 'origin/main']

        def cherry_pick(tree):  # two picks, the second in conflict, so git keeps a sequence to go on with
            return ['cherry-pick', 'origin/main~1', 'origin/main']

        def revert(tree):
            (tree / 'content').write_text('mine again\n', encoding='utf-8')
            git(tree, 'commit', '-qam', 'Mine again')
            return ['revert', '--no-edit', 'HEAD~1']

        def am(tree):
            patch_file = self.root / 'theirs.patch'
            patch_file.write_bytes(subprocess.check_output(['git', '-C', str(tree), 'format-patch', '-1', '--stdout',
                                                            'origin/main']))
            return ['am', str(patch_file)]
        for index, (start, step, going_on) in enumerate((
                (rebase, 'rebase', 'rebase --continue'), (apply_rebase, 'rebase', 'rebase --continue'),
                (merge, 'merge', 'merge --continue'), (cherry_pick, 'cherry-pick', 'cherry-pick --continue'),
                (revert, 'revert', 'revert --continue'), (am, 'git am', 'am --continue'))):
            with self.subTest(step=start.__name__):
                name = f'stopped-{index}'
                release_main(['start', name, *self.options()], self.public)
                tree = self.worktrees / name / 'public'
                (tree / 'content').write_text('mine\n', encoding='utf-8')
                git(tree, 'commit', '-qam', 'Mine')
                self.another_session(self.public_origin, 'doc', f'theirs {index}\n')
                self.another_session(self.public_origin, 'content', f'theirs {index}\n')
                git(tree, 'fetch', '-q', 'origin')
                command = start(tree)
                self.assertNotEqual(subprocess.run(['git', '-C', str(tree), *command], capture_output=True,
                                                   env={**os.environ, 'GIT_EDITOR': 'true'}).returncode, 0)

                said = self.stopped_finish(name, '--overlap-reviewed')

                self.assertIn(f'a {step} stopped part way in {tree}; settle its conflicts, git add each file and '
                              f'run git -C {tree} {going_on}, then finish again', said)

    def test_finish_stops_between_the_picks_of_a_sequence(self):
        release_main(['start', 'picks', *self.options()], self.public)
        tree = self.worktrees / 'picks/public'
        (tree / 'content').write_text('mine\n', encoding='utf-8')
        git(tree, 'commit', '-qam', 'Mine')
        self.another_session(self.public_origin, 'content', 'theirs\n')
        self.another_session(self.public_origin, 'doc', 'theirs\n')
        git(tree, 'fetch', '-q', 'origin')
        self.assertNotEqual(subprocess.run(['git', '-C', str(tree), 'cherry-pick', 'origin/main~1', 'origin/main'],
                                           capture_output=True).returncode, 0)
        (tree / 'content').write_text('both\n', encoding='utf-8')
        git(tree, 'add', 'content')
        git(tree, 'commit', '-q', '--no-edit')  # the first pick is settled, the second still to come

        said = self.stopped_finish('picks', '--overlap-reviewed')

        self.assertIn(f'a cherry-pick or revert stopped part way in {tree}; settle its conflicts, git add each file '
                      f'and run git -C {tree} cherry-pick --continue or git revert --continue', said)

    def test_finish_on_a_detached_head_with_commits_of_its_own_says_how_to_keep_them(self):
        release_main(['start', 'away', *self.options()], self.public)
        private = self.worktrees / 'away/private'
        git(private, 'checkout', '-q', '--detach')
        (private / 'committed').write_text('one\n', encoding='utf-8')
        git(private, 'add', 'committed')
        git(private, 'commit', '-qm', 'On a detached HEAD')
        (private / 'uncommitted').write_text('two\n', encoding='utf-8')
        message = self.root / 'private-message'
        message.write_text('Add two files\n', encoding='utf-8')

        said = self.stopped_finish('away')
        self.assertIn(f'{private} is on a detached HEAD that holds its release branch away and may hold commits of '
                      f'its own; move the branch there with git -C {private} switch -C away, then finish again', said)
        git(private, 'switch', '-q', '-C', 'away')

        with redirect_stdout(io.StringIO()):
            self.assertEqual(release_main(['finish', 'away', '--private-message', str(message), '--push',
                                           *self.options()], self.public), 0)
        self.assertEqual((git(self.private_origin, 'show', 'main:committed'),
                          git(self.private_origin, 'show', 'main:uncommitted')), ('one', 'two'))

    def test_finish_on_a_detached_head_whose_release_branch_is_gone_says_to_make_it(self):
        release_main(['start', 'gone', *self.options()], self.public)
        private = self.worktrees / 'gone/private'
        git(private, 'checkout', '-q', '--detach')
        (private / 'committed').write_text('one\n', encoding='utf-8')
        git(private, 'add', 'committed')
        git(private, 'commit', '-qm', 'Add a file')
        git(private, 'branch', '-q', '-D', 'gone')

        said = self.stopped_finish('gone')
        self.assertIn(f'{private} has no release branch gone; git -C {private} log origin/main..HEAD lists the '
                      'commits HEAD holds that origin/main lacks. Where HEAD holds the release, make the branch there '
                      f'with git -C {private} switch -c gone, then finish again; this run committed nothing', said)
        git(private, 'switch', '-q', '-c', 'gone')

        with redirect_stdout(io.StringIO()):
            self.assertEqual(release_main(['finish', 'gone', '--push', *self.options()], self.public), 0)
        self.assertEqual(git(self.private_origin, 'show', 'main:committed'), 'one')

    def test_finish_stops_where_git_cannot_read_which_branch_head_is_on(self):
        release_main(['start', 'garbled', *self.options()], self.public)
        private = self.worktrees / 'garbled/private'
        (private / 'committed').write_text('one\n', encoding='utf-8')
        git(private, 'add', 'committed')
        git(private, 'commit', '-qm', 'Add a file')
        head = Path(git(private, 'rev-parse', '--path-format=absolute', '--git-path', 'HEAD'))
        head.write_text('ref: refs/heads/a..b\n', encoding='utf-8')  # git finds the worktree but no branch
        before = self.origin_heads()
        with redirect_stderr(io.StringIO()) as error:  # review=False: the helper's review record reads HEAD first
            self.assertEqual(release_main(['finish', 'garbled', '--push', *self.options()], self.public, review=False),
                             3)
        self.assertIn(f'{private}: git symbolic-ref exited 128: fatal: No such ref: HEAD; {head} names no branch git '
                      f'can read. Point it at the release branch with git -C {private} symbolic-ref HEAD '
                      'refs/heads/garbled, then finish again; this run committed nothing', error.getvalue())
        self.assertEqual(self.origin_heads(), before)
        git(private, 'symbolic-ref', 'HEAD', 'refs/heads/garbled')

        with redirect_stdout(io.StringIO()):
            self.assertEqual(release_main(['finish', 'garbled', '--push', *self.options()], self.public), 0)
        self.assertEqual(git(self.private_origin, 'show', 'main:committed'), 'one')

    def test_finish_stops_with_gits_words_where_the_release_branch_is_a_broken_ref(self):
        release_main(['start', 'broken', *self.options()], self.public)
        private = self.worktrees / 'broken/private'
        git(private, 'checkout', '-q', '--detach')
        common = Path(git(private, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
        (common / 'refs/heads/broken').write_text('not an object id\n', encoding='utf-8')

        said = self.stopped_finish('broken')
        self.assertIn(f'{private}: git rev-parse --verify refs/heads/broken exited 1: warning: ignoring broken ref '
                      'refs/heads/broken; this run committed nothing', said)
        self.assertNotIn('switch -c', said)

    def seed_relock(self):
        """The relock stand-in on the public origin, a private lock and kit entry the relock changes, and the
        message options."""
        seed = self.clone(self.public_origin, 'relock-seed')
        (seed / 'lock_setup.py').write_text(RELOCK_STAND_IN, encoding='utf-8')
        (seed / '.gitignore').write_text(ALLOWLIST + '!/lock_setup.py\n!/setup/\n!/setup/**\n', encoding='utf-8')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-qm', 'Add a relock stand-in')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')
        git(self.public, 'pull', '-q', '--ff-only')
        private_seed = self.clone(self.private_origin, 'relock-private-seed')
        (private_seed / 'setup/skills').mkdir(parents=True)
        (private_seed / 'setup/lock.toml').write_text('[kit]\ntree = "seed"\n', encoding='utf-8')
        (private_seed / KIT).write_text('[source]\npath = "kit"\ntree = "seed"\n', encoding='utf-8')
        git(private_seed, 'add', '-A')
        git(private_seed, 'commit', '-qm', 'Lock the kit')
        git(private_seed, 'push', '-q', 'origin', 'HEAD:main')
        git(self.private, 'pull', '-q', '--ff-only')
        message, private_message = self.root / 'message', self.root / 'private-message'
        message.write_text('Change the content\n', encoding='utf-8')
        private_message.write_text('Change the notes\n', encoding='utf-8')
        return ['--message', str(message), '--private-message', str(private_message)]

    def origin_heads(self):
        return git(self.public_origin, 'rev-parse', 'main'), git(self.private_origin, 'rev-parse', 'main')

    def assert_relocked(self, name, before, public_commits, private_commits, refreshed=False):
        """Each origin got these commits since before, newest first, with the relock of release name in the
        newest commit of the branch's own: the one made by hand when nothing else was uncommitted, and otherwise
        the one finish made. Refreshed: the private kit entry's pin went into that commit too."""
        for origin, start, commits in ((self.public_origin, before[0], public_commits),
                                       (self.private_origin, before[1], private_commits)):
            self.assertEqual(git(origin, 'log', '--format=%s', f'{start}..main').splitlines(), commits)
            self.assertEqual(git(origin, 'show', 'main:setup/lock.toml'), f'[kit]\ntree = "{name}"')
            own = next(number for number, subject in enumerate(commits) if not subject.startswith('Pair '))
            held = git(origin, 'show', '--name-only', '--format=', f'main~{own}').splitlines()
            self.assertIn('setup/lock.toml', held)
            if refreshed and origin == self.private_origin:
                self.assertIn(KIT, held)
                self.assertIn(f'tree = "{name}"', git(origin, 'show', f'main:{KIT}'))

    def test_finish_amends_a_relock_into_the_commit_a_branch_already_has(self):
        messages = self.seed_relock()
        # By hand: each side committed and clean. Entry edited: the same, and the private commit also changed the
        # kit entry's file beyond its pin. Uncommitted: neither committed. Relock only: the public side holds
        # nothing but the relock. Both: committed, then edited again.
        by_hand = ['Change the content by hand'], ['Pair "Change the content by hand"', 'Change the notes by hand']
        for case, public_commits, private_commits in (
                ('by-hand', *by_hand), ('entry-edited', *by_hand),
                ('uncommitted', ['Change the content'], ['Pair "Change the content"', 'Change the notes']),
                ('relock-only', ['Change the content'], ['Pair "Change the content"', 'Change the notes']),
                ('both', ['Change the content', 'Change the content by hand'],
                 ['Pair "Change the content"', 'Change the notes', 'Change the notes by hand'])):
            with self.subTest(case=case):
                name = f'relock-{case}'
                release_main(['start', name, *self.options()], self.public)
                public, private = self.worktrees / name / 'public', self.worktrees / name / 'private'
                content = git(self.public_origin, 'show', 'main:content')
                for tree, path in ((public, 'content'), (private, 'notes')):
                    if case == 'relock-only' and tree == public:
                        continue
                    (tree / path).write_text(f'{case}\n', encoding='utf-8')
                    if case == 'entry-edited' and tree == private:
                        (tree / KIT).write_text((tree / KIT).read_text(encoding='utf-8') + 'note = "mine"\n',
                                                encoding='utf-8')
                        git(tree, 'add', KIT)
                    if case not in ('uncommitted', 'relock-only'):
                        git(tree, 'add', path)
                        git(tree, 'commit', '-qm', f'Change the {path} by hand')
                    if case == 'both':  # an edit on the public side, a new file on the private one
                        (tree / (path if tree == public else f'{path}-new')).write_text(f'{case} again\n',
                                                                                         encoding='utf-8')
                before = self.origin_heads()

                with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(release_main(['finish', name, '--entry', 'kit', '--refresh-owned',
                                                   *([] if case in ('by-hand', 'entry-edited') else messages),
                                                   '--push', *self.options()], self.public), 0, error.getvalue())
                if case == 'by-hand':
                    for tree, path in ((public, 'content'), (private, 'notes')):
                        self.assertIn(f'{tree}: amended the relock into ', out.getvalue())
                        self.assertIn(f' Change the {path} by hand\n', out.getvalue())

                self.assert_relocked(name, before, public_commits, private_commits, refreshed=True)
                self.assertEqual(git(self.public_origin, 'show', 'main:content'),
                                 {'both': 'both again', 'relock-only': content}.get(case, case))
                if case == 'entry-edited':
                    self.assertIn('note = "mine"', git(self.private_origin, 'show', f'main:{KIT}'))
                self.assertEqual(git(self.private_origin, 'show', 'main:notes'), case)
                if case == 'both':
                    self.assertEqual(git(self.private_origin, 'show', 'main:notes-new'), 'both again')
                release_main(['clean', name, *self.options()], self.public)

    def test_a_finish_after_one_that_stopped_past_its_relock_amends_that_relock(self):
        self.seed_relock()
        release_main(['start', 'again', *self.options()], self.public)
        public, private = self.worktrees / 'again/public', self.worktrees / 'again/private'
        (public / 'doc').write_text('ONE\ntwo\nthree\nfour\nfive\n', encoding='utf-8')
        (private / 'notes').write_text('mine\n', encoding='utf-8')
        for tree, path in ((public, 'doc'), (private, 'notes')):
            git(tree, 'add', path)
            git(tree, 'commit', '-qm', f'Change the {path} by hand')
        self.another_session(self.public_origin, 'doc', 'one\ntwo\nthree\nfour\nFIVE\n')
        before = self.origin_heads()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'again', '--entry', 'kit', '--push', *self.options()],
                                          self.public), 3)
        self.assertIn('another session changed the same files on origin/main', error.getvalue())

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'again', '--entry', 'kit', '--overlap-reviewed', '--push',
                                           *self.options()], self.public), 0, error.getvalue())
        self.assert_relocked('again', before, ['Change the doc by hand'],
                             ['Pair "Change the doc by hand"', 'Change the notes by hand'])

    def test_finish_stops_on_a_file_made_after_the_commit_rather_than_amend_it_with_the_relock(self):
        self.seed_relock()
        release_main(['start', 'stray', *self.options()], self.public)
        public, private = self.worktrees / 'stray/public', self.worktrees / 'stray/private'
        for tree, path in ((public, 'content'), (private, 'notes')):
            (tree / path).write_text('stray\n', encoding='utf-8')
            git(tree, 'add', path)
            git(tree, 'commit', '-qm', f'Change the {path} by hand')
        self.another_session(self.public_origin, 'doc', 'theirs\n')  # so the closing rebase relocks again
        before = self.origin_heads()

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'stray', '--entry', 'kit', '--push', *self.options()],
                                          self.public), 3)
        self.assertIn(f'{public}: setup/stray.toml changed after the commit, and an amend takes only what the relock '
                      'writes; commit or remove them, then finish again; nothing was pushed', error.getvalue())
        self.assertEqual(self.origin_heads(), before)

    def test_finish_commits_what_the_worktree_holds_where_the_index_holds_something_else(self):
        messages = self.seed_relock()
        release_main(['start', 'staged', *self.options()], self.public)
        public, private = self.worktrees / 'staged/public', self.worktrees / 'staged/private'
        for tree, path in ((public, 'content'), (private, 'notes')):
            (tree / path).write_text('staged\n', encoding='utf-8')
            git(tree, 'add', path)
            git(tree, 'commit', '-qm', f'Change the {path} by hand')
        (public / 'tests/test_untested.py').write_text('staged only\n', encoding='utf-8')
        git(public, 'add', 'tests/test_untested.py')
        (public / 'tests/test_untested.py').unlink()  # the index alone holds it now
        before = self.origin_heads()

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'staged', '--entry', 'kit', '--push', *self.options()],
                                          self.public), 3)
        self.assertIn(f'{public}: pass --message with the public commit message; this run committed nothing',
                      error.getvalue())
        self.assertEqual(self.origin_heads(), before)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'staged', '--entry', 'kit', *messages, '--push',
                                           *self.options()], self.public), 0, error.getvalue())
        self.assertNotIn('test_untested.py', git(self.public_origin, 'ls-tree', '-r', '--name-only', 'main'))
        self.assertEqual(git(self.public_origin, 'log', '--format=%s', f'{before[0]}..main').splitlines(),
                         ['Change the content', 'Change the content by hand'])

    def test_a_relock_after_the_closing_rebase_amends_the_pin_it_writes_again(self):
        messages = self.seed_relock()
        release_main(['start', 'moved', *self.options()], self.public)
        (self.worktrees / 'moved/private/notes').write_text('moved\n', encoding='utf-8')
        self.another_session(self.private_origin, 'setup/lock.toml', '[kit]\ntree = "theirs"\n')

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'moved', '--entry', 'kit', '--refresh-owned', *messages,
                                           '--push', *self.options()], self.public), 0, error.getvalue())
        self.assertIn('tree = "moved-again"', git(self.private_origin, 'show', f'main:{KIT}'))
        self.assertEqual(git(self.private_origin, 'show', 'main:setup/lock.toml'), '[kit]\ntree = "moved"')

    def test_finish_commits_a_mended_entry_file_whose_committed_copy_is_not_toml(self):
        messages = self.seed_relock()
        release_main(['start', 'mended', *self.options()], self.public)
        private = self.worktrees / 'mended/private'
        (private / 'notes').write_text('mended\n', encoding='utf-8')
        (private / KIT).write_text('[source\n', encoding='utf-8')
        git(private, 'add', '-A')
        git(private, 'commit', '-qm', 'Change the notes by hand')
        (private / KIT).write_text('[source]\npath = "kit"\ntree = "seed"\n', encoding='utf-8')

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'mended', '--entry', 'kit', '--refresh-owned', *messages,
                                           '--push', *self.options()], self.public), 0, error.getvalue())
        self.assertEqual(git(self.private_origin, 'show', f'main:{KIT}'), '[source]\npath = "kit"\ntree = "mended"')

    def test_finish_commits_a_relock_apart_from_a_newest_commit_that_is_a_merge_or_a_pair(self):
        messages = self.seed_relock()
        release_main(['start', 'merged', *self.options()], self.public)
        public, private = self.worktrees / 'merged/public', self.worktrees / 'merged/private'
        for tree, path in ((public, 'content'), (private, 'notes')):
            (tree / path).write_text('merged\n', encoding='utf-8')
            git(tree, 'add', path)
            git(tree, 'commit', '-qm', f'Change the {path} by hand')
        self.another_session(self.private_origin, 'other', 'theirs\n')
        git(private, 'fetch', '-q', 'origin')
        git(private, 'merge', '-q', '--no-edit', 'origin/main')
        self.another_session(self.private_origin, 'more', 'theirs\n')  # so the rebase drops the merge
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'merged', '--entry', 'kit', *messages, '--push',
                                           *self.options()], self.public), 0, error.getvalue())
        self.assertEqual(git(self.private_origin, 'show', 'main:setup/lock.toml'), '[kit]\ntree = "merged"')

        release_main(['start', 'paired', *self.options()], self.public)
        for tree, path in (('public', 'content'), ('private', 'notes')):
            (self.worktrees / 'paired' / tree / path).write_text('paired\n', encoding='utf-8')
        before = self.origin_heads()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            # Without --push first, which leaves the pair as the private branch's newest commit.
            self.assertEqual(release_main(['finish', 'paired', *messages, *self.options()], self.public), 0,
                             error.getvalue())
            self.assertEqual(release_main(['finish', 'paired', '--entry', 'kit', *messages, '--push',
                                           *self.options()], self.public), 0, error.getvalue())
        self.assertEqual(git(self.private_origin, 'show', 'main:setup/lock.toml'), '[kit]\ntree = "paired"')
        for commit in git(self.private_origin, 'rev-list', f'{before[1]}..main').splitlines():
            if git(self.private_origin, 'log', '-1', '--format=%s', commit).startswith('Pair '):
                self.assertEqual(git(self.private_origin, 'show', '--name-only', '--format=', commit), 'release.toml')

    def test_finish_on_a_detached_head_apart_from_the_branch_is_not_told_to_move_the_branch(self):
        release_main(['start', 'apart', *self.options()], self.public)
        private = self.worktrees / 'apart/private'
        (private / 'on-branch').write_text('one\n', encoding='utf-8')
        git(private, 'add', 'on-branch')
        git(private, 'commit', '-qm', 'On the branch')
        git(private, 'checkout', '-q', '--detach', 'origin/main')
        (private / 'detached').write_text('two\n', encoding='utf-8')
        git(private, 'add', 'detached')
        git(private, 'commit', '-qm', 'On a detached HEAD')

        said = self.stopped_finish('apart')

        self.assertIn(f'{private} is off its release branch apart; git -C {private} log apart..HEAD lists', said)
        self.assertNotIn('switch -C', said)

    def test_a_release_branch_named_like_the_stash_is_its_own_branch(self):
        release_main(['start', 'stash', *self.options()], self.public)
        public = self.worktrees / 'stash/public'
        (public / 'content').write_text('mine\n', encoding='utf-8')
        git(public, 'stash', '-q')
        git(public, 'stash', 'apply', '-q')  # refs/stash now stands beside refs/heads/stash
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'stash', '--message', str(message), '--push', *self.options()],
                                          self.public), 0, error.getvalue())
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'mine')

    def test_finish_on_another_branch_names_the_commits_only_it_holds(self):
        release_main(['start', 'elsewhere', *self.options()], self.public)
        private = self.worktrees / 'elsewhere/private'
        git(private, 'switch', '-q', '-c', 'side')

        said = self.stopped_finish('elsewhere')

        self.assertIn(f'{private} is off its release branch elsewhere; git -C {private} log elsewhere..HEAD lists the '
                      'commits only HEAD holds', said)

    def test_the_fast_forward_keeps_a_new_file_the_allowlist_ignores(self):
        release_main(['start', 'newtool', *self.options()], self.public)
        public = self.worktrees / 'newtool/public'
        (public / 'newtool.py').write_text('mine\n', encoding='utf-8')  # ignored until a ! line names it
        seed = self.clone(self.public_origin, 'newtool-seed')
        (seed / 'newtool.py').write_text('theirs\n', encoding='utf-8')
        git(seed, 'add', '-f', 'newtool.py')
        git(seed, 'commit', '-qm', 'Their newtool')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')
        git(self.public, 'pull', '-q', '--ff-only')

        said = self.stopped_finish('newtool')

        self.assertIn(f'{public} has no work of its own to rebase, but finish could not bring it up to origin/main: ',
                      said)
        self.assertEqual((public / 'newtool.py').read_text(encoding='utf-8'), 'mine\n')

    def test_a_committed_branch_behind_a_moved_origin_is_left_to_the_rebase_and_releases(self):
        release_main(['start', 'committed', *self.options()], self.public)
        public = self.worktrees / 'committed/public'
        (public / 'content').write_text('mine\n', encoding='utf-8')
        git(public, 'commit', '-qam', 'Mine')
        self.another_session(self.public_origin, 'doc', 'theirs\n')
        git(self.public, 'pull', '-q', '--ff-only')

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'committed', '--push', *self.options()], self.public), 0,
                             error.getvalue())
        self.assertEqual((git(self.public_origin, 'show', 'main:content'), git(self.public_origin, 'show', 'main:doc')),
                         ('mine', 'theirs'))

    def test_finish_stops_on_a_package_file_that_is_not_toml(self):
        for name, content in (('garbled', b'x = \n'), ('undecodable', b'x = "\xff\xfe"\n')):
            with self.subTest(content=content):
                release_main(['start', name, *self.options()], self.public)
                package = self.worktrees / name / 'public/setup/packages/bad.toml'
                package.parent.mkdir(parents=True)
                package.write_bytes(content)

                said = self.stopped_finish(name)
                self.assertIn(f'Release stopped: {package} is not TOML (', said)
                self.assertTrue(said.rstrip().endswith('); this run committed nothing'), said)

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
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'away', '--detach', '--message', '/tmp/m', '--pair-message', 'pair.txt',
                                           *self.options()], self.public), 3)
        self.assertIn('--detach needs an absolute path for pair.txt', error.getvalue())
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
        busy = iter([['pull PID 1 since 23:54: pull.py --update --apply'], []])

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

    def test_a_finish_starts_again_before_the_lock_when_a_pull_changed_the_engine_meanwhile(self):
        release_main(['start', 'tested', *self.options()], self.public)
        (self.worktrees / 'tested/public/content').write_text('tested\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        argv = ['finish', 'tested', '--message', str(message), '--push', *self.options()]
        suite, runs = release.test_suite, []

        class Restarted(Exception):
            pass

        def restart(program, arguments):
            raise Restarted(arguments)

        def pull_during_the_suite(*arguments):
            runs.append(arguments)
            suite(*arguments)
            if len(runs) == 1:  # another release goes out and its pull moves the engine's checkout
                self.push_from_another_session(self.public_origin, 'other', {'doc': 'theirs\n'})
                git(self.public, 'pull', '-q', '--ff-only', 'origin', 'main')

        with patch.object(release, 'test_suite', pull_during_the_suite), patch.object(release.os, 'execv', restart), \
                redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(Restarted) as restarted:
                release_main(argv, self.public)
            self.assertEqual(restarted.exception.args[0][3:5], ['finish', 'tested'])
            self.assertIn('Starting again because a pull changed the engine while this finish ran its checks or '
                          'waited, to run the engine as it is now', output.getvalue())
            self.assertRegex(output.getvalue(), r'Waited \d+ seconds for the release lock before the engine changed\n')
            self.assertNotIn('Held the release lock', output.getvalue())
            self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'first')  # it took no lock and no commit
            self.assertEqual(git(self.worktrees / 'tested/public', 'log', '-1', '--format=%s'), 'seed')
            self.assertFalse((self.root / 'state/release.lock.holder').exists())
            self.assertEqual(release_main(argv, self.public), 0)  # as the engine starts it again
        self.assertEqual(len(runs), 1)  # and the tests that passed did not run a second time
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'tested')

    def test_a_detached_finish_keeps_the_log_of_the_attempt_before_and_starts_no_second_unit(self):
        release_main(['start', 'twice', *self.options()], self.public)
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        argv = ['finish', 'twice', '--detach', '--message', str(message), *self.options()]
        log, status = self.worktrees / 'twice/build/finish.log', self.worktrees / 'twice/build/finish.exit'
        log.parent.mkdir(parents=True)
        log.write_text('Release stopped: the first attempt\n', encoding='utf-8')
        status.write_text('3\n', encoding='utf-8')
        started = []

        def unit(args, argv, environ):
            started.append(argv)
            return [sys.executable, '-c', 'pass']

        with patch.object(release, 'detach_command', unit), patch.object(release.shutil, 'which', return_value='/bin/x'):
            with patch.object(release, 'unit_state', return_value='active'), redirect_stderr(io.StringIO()) as error:
                self.assertEqual(release_main(argv, self.public), 3)
            self.assertIn('a detached finish of twice still runs as unit dotfiles-release-twice', error.getvalue())
            self.assertEqual(started, [])
            self.assertEqual(log.read_text(encoding='utf-8'), 'Release stopped: the first attempt\n')
            self.assertEqual(status.read_text(encoding='utf-8'), '3\n')
            for state in ('inactive', None):  # None: systemd could not be asked
                log.write_text(f'Release stopped: an attempt, unit {state}\n', encoding='utf-8')
                with self.subTest(state=state), patch.object(release, 'unit_state', return_value=state), \
                        redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(release_main(argv, self.public), 0)
                    kept = sorted(log.parent.glob('finish.*.log'), key=lambda path: path.stat().st_mtime_ns)[-1]
                    self.assertIn(f'The log of the attempt before is kept as {kept}', output.getvalue())
                    self.assertEqual(kept.read_text(encoding='utf-8'), f'Release stopped: an attempt, unit {state}\n')
                    self.assertEqual(log.read_text(encoding='utf-8'), '')
                    self.assertFalse(status.exists())
        self.assertEqual(len(started), 2)
        self.assertEqual(len(list(log.parent.glob('finish.*.log'))), 2)
        # result keeps waiting when systemd cannot be asked, and detach above went on.
        with patch.object(release.subprocess, 'run', side_effect=OSError('no systemctl')):
            self.assertIsNone(release.unit_state('twice'))
            self.assertTrue(release.unit_active('twice'))
        for state, active in (('active', True), ('deactivating', True), ('inactive', False), ('failed', False)):
            with patch.object(release.subprocess, 'run', return_value=subprocess.CompletedProcess([], 3, f'{state}\n')):
                self.assertEqual(release.unit_active('twice'), active)

    def test_finish_without_a_message_stops_before_committing(self):
        release_main(['start', 'quiet', *self.options()], self.public)
        (self.worktrees / 'quiet/public/content').write_text('edited\n', encoding='utf-8')
        (self.worktrees / 'quiet/private/notes').write_text('mine\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        # Before the tests, and so before the wait for the lock: the stop used to come after both.
        for given, option, tree in (([], '--message', 'public'), (['--message', str(message)], '--private-message', 'private')):
            with self.subTest(option), patch.object(release, 'test_suite', side_effect=AssertionError('the tests ran')), \
                    redirect_stderr(io.StringIO()) as error:
                self.assertEqual(release_main(['finish', 'quiet', *given, *self.options()], self.public), 3)
            self.assertIn(f'{self.worktrees / "quiet" / tree}: pass {option} with the {tree} commit message; this run '
                          'committed nothing', error.getvalue())
            self.assertEqual(git(self.worktrees / 'quiet/public', 'log', '-1', '--format=%s'), 'seed')
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
        # The stand-in also notes whether a release held this machine's lock while it ran, and exits 5.
        (self.public / 'pull.py').write_text(
            'import pathlib, sys\npathlib.Path(__file__).with_name("pull-argv").write_text("\\n".join(sys.argv[1:]))\n'
            'here = pathlib.Path(__file__).resolve().parent\n'
            '(here / "pull-lock").write_text(str((here.parent / "state/release.lock.holder").exists()))\n'
            'raise SystemExit(5)\n', encoding='utf-8')
        release_main(['start', 'pulled', *self.options()], self.public)
        (self.worktrees / 'pulled/public/content').write_text('second\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        self.assertEqual(release_main(['finish', 'pulled', '--message', str(message), '--push', '--pull',
                                       *self.options()], self.public), 5)  # the pull's own status
        argv = (self.public / 'pull-argv').read_text(encoding='utf-8').split('\n')
        self.assertEqual(argv[-4:], ['--update', '--apply', '--wait', '0'])
        self.assertEqual((self.public / 'pull-lock').read_text(encoding='utf-8'), 'True')
        self.assertFalse((self.root / 'state/release.lock.holder').exists())

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
        check = release.reused_release_names

        def edit_after_the_first_review_check(*arguments):
            # Before the tests, so they run on the edited file and pass, and the commit holds what was tested.
            module.write_text(PASSING_TEST + '\n# slipped in\n', encoding='utf-8')
            return check(*arguments)

        errors = io.StringIO()
        with redirect_stderr(errors), patch.object(release, 'reused_release_names', edit_after_the_first_review_check):
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


class NativeWindows(unittest.TestCase):
    """setUpModule skips this file on Windows, so this runs the engine on Linux as Windows itself would."""

    STOP = 'Release stopped: run this from WSL or Omarchy, not from Windows itself; nothing was changed\n'

    def as_windows(self, box, *argv):
        """release.py run as on Windows, with HOME, the working folder and every place it takes in box, and no
        program on PATH, so that a run the stop misses cannot reach git, systemd or the network."""
        engine = Path(release.__file__).resolve().parent
        # Its modules load first, because shutil imports a Windows-only module on win32.
        script = (f"import runpy, sys; sys.path.insert(0, {str(engine)!r}); import release; "
                  f"sys.platform = 'win32'; runpy.run_path({str(engine / 'release.py')!r}, run_name='__main__')")
        places = ['--worktrees', str(Path(box) / 'worktrees'), '--private', str(Path(box) / 'private')]
        return subprocess.run([sys.executable, '-I', '-B', '-c', script, *argv, *places], capture_output=True,
                              text=True, timeout=60, cwd=box,
                              env={'PATH': str(Path(box) / 'no-programs'), 'HOME': box})

    def test_on_windows_itself_each_release_step_stops_with_a_sentence_before_anything_runs(self):
        with tempfile.TemporaryDirectory() as box:
            # --report is the file a detached finish's status goes to, which main writes as it ends.
            for argv in (['start', 'late'], ['finish', 'late', '--push', '--pull', '--report', str(Path(box) / 'exit')],
                         ['finish'], ['finish', 'late', '--detach'], ['review', 'late'], ['clean', 'late', '--force'],
                         ['result', 'late', '--timeout', '0']):
                with self.subTest(argv=argv):
                    done = self.as_windows(box, *argv)
                    self.assertEqual(os.listdir(box), [])
                    self.assertEqual((done.returncode, done.stdout, done.stderr), (3, '', self.STOP))

    def test_on_windows_itself_status_and_wait_still_read_the_machine(self):
        with tempfile.TemporaryDirectory() as box:
            done = self.as_windows(box, 'wait', '--timeout', '0')
            self.assertEqual((done.returncode, done.stdout, done.stderr),
                             (0, "No release holds this machine's lock and no pull or sync runs here.\n", ''))
            # status goes on to read the release worktrees with git, which PATH here lacks.
            done = self.as_windows(box, 'status')
            self.assertTrue(done.stdout.startswith('Running on this machine: no release, pull or sync\n'), done.stdout)
            self.assertNotIn(self.STOP, done.stderr)
            self.assertEqual(os.listdir(box), [])


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


if __name__ == '__main__':
    unittest.main()
