from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


SYNC_WITH_CHANGES = 'print("Managed changes: 1; blocking issues: 0.")\nraise SystemExit(1)\n'
PASSING_TEST = 'import unittest\n\n\nclass Seed(unittest.TestCase):\n    def test_passes(self):\n        pass\n'


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = self.root / 'release'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'release', '-f', str(self.key)], check=True)
        # The release's device check runs the worktree's sync.py, which exits 1 when it has changes to apply.
        self.public_origin = self.origin('public-origin', {'content': 'first\n', 'doc': 'one\ntwo\nthree\nfour\nfive\n',
                                                           'sync.py': SYNC_WITH_CHANGES, 'tests/test_seed.py': PASSING_TEST})
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
            PASSING_TEST.replace('pass', 'self.fail("broken")'), encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add a failing test\n', encoding='utf-8')
        self.assertEqual(release_main(['finish', 'failing', '--message', str(message), '--push', *self.options()],
                                      self.public), 3)
        self.assertIn('broken', (self.worktrees / 'failing/build/tests.log').read_text(encoding='utf-8'))
        self.assertEqual(git(self.public_origin, 'ls-tree', '--name-only', 'main', 'tests/'), 'tests/test_seed.py')

    def test_finish_stops_when_the_device_check_crashes(self):
        release_main(['start', 'crash', *self.options()], self.public)
        (self.worktrees / 'crash/public/sync.py').write_text('raise KeyError("models")\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Crash the pull\n', encoding='utf-8')
        self.assertEqual(release_main(['finish', 'crash', '--message', str(message), '--push', *self.options()],
                                      self.public), 3)
        self.assertEqual(git(self.public_origin, 'show', 'main:sync.py'), SYNC_WITH_CHANGES.strip())

    def test_finish_without_a_message_stops_before_committing(self):
        release_main(['start', 'quiet', *self.options()], self.public)
        (self.worktrees / 'quiet/public/content').write_text('edited\n', encoding='utf-8')
        self.assertEqual(release_main(['finish', 'quiet', *self.options()], self.public), 3)
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'first')

    def options(self):
        return ['--worktrees', str(self.worktrees), '--private', str(self.private)]


def release_main(argv, engine):
    """release.main with ENGINE set to the test's canonical public clone."""
    original = release.ENGINE
    release.ENGINE = engine
    try:
        return release.main(argv)
    finally:
        release.ENGINE = original


if __name__ == '__main__':
    unittest.main()
