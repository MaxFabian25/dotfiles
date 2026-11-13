from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_case import ALLOWLIST, KIT, RELOCK_STAND_IN, ReleaseCase, git, release_main  # noqa: E402
from release_case import skip_on_windows as setUpModule  # noqa: E402, F401
import release  # noqa: E402


class RelockTest(ReleaseCase):
    def test_relock_creates_signed_public_work_on_origin_only_side_and_tests_final_outputs(self):
        messages = self.seed_relock()
        script = RELOCK_STAND_IN.replace('    setup.mkdir(exist_ok=True)',
                                        '    if option == "--public" and not runs:\n        continue\n'
                                        '    setup.mkdir(exist_ok=True)')
        self.another_session(self.public_origin, 'lock_setup.py', script)
        git(self.public, 'pull', '-q', '--ff-only')
        release_main(['start', 'origin-only', *self.options()], self.public)
        public, private = self.worktrees / 'origin-only/public', self.worktrees / 'origin-only/private'
        (private / 'notes').write_text('own private work\n', encoding='utf-8')
        observed = self.root / 'generated-observation.json'
        (private / 'test_generated.py').write_text(
            'import json, os, subprocess, unittest\nfrom pathlib import Path\n'
            'class Generated(unittest.TestCase):\n'
            '    def test_final_outputs(self):\n'
            '        root = Path(__file__).resolve().parent\n'
            '        public = root.parent / "public"\n'
            '        pointer = subprocess.check_output(["git", "-C", str(public), "rev-parse", "HEAD"]).decode().strip()\n'
            '        self.assertIn(pointer, (root / "release.toml").read_text())\n'
            '        files = {"public/" + name: (public / name).read_text() for name in ("setup/lock.toml",)}\n'
            '        files.update({"private/" + name: (root / name).read_text()\n'
            '                      for name in ("setup/lock.toml", "setup/skills/kit.toml", "release.toml")})\n'
            '        self.assertTrue(all("origin-only" in text for name, text in files.items()\n'
            '                            if not name.endswith("release.toml")))\n'
            '        Path(os.environ["GENERATED_OBSERVATION"]).write_text(json.dumps(files))\n'
            'if __name__ == "__main__":\n    unittest.main()\n', encoding='utf-8')
        self.another_session(self.private_origin, 'other', 'other private work\n')
        public_before = git(self.public_origin, 'rev-parse', 'main')
        private_before = git(self.private_origin, 'rev-parse', 'main')
        argv = ['finish', 'origin-only', '--entry', 'kit', '--refresh-owned', '--push', *self.options()]
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main([*argv, *messages[2:]], self.public), 3)
        self.assertIn('pass --message with the public commit message', error.getvalue())
        self.assertIn('Local commits remain', error.getvalue())
        self.assertEqual(self.origin_heads(), (public_before, private_before))
        self.assertEqual(git(public, 'rev-parse', 'HEAD'), public_before)
        with patch.dict(os.environ, {'GENERATED_OBSERVATION': str(observed)}):
            self.assertEqual(release_main([*argv, *messages], self.public), 0)
        self.assertEqual(git(self.public_origin, 'rev-parse', 'main^'), public_before)
        self.assertIn('gpgsig -----BEGIN SSH SIGNATURE-----', git(self.public_origin, 'cat-file', 'commit', 'main'))
        for path, text in json.loads(observed.read_text()).items():
            side, name = path.split('/', 1)
            origin = self.public_origin if side == 'public' else self.private_origin
            self.assertEqual(release.git_bytes(origin, 'show', f'main:{name}'), text.encode())

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
        self.assertIn(f'{public}: setup/stray.toml changed after the commit; only relock outputs may be committed here',
                      error.getvalue())
        self.assertIn('nothing was pushed', error.getvalue())
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
        merged = git(private, 'rev-parse', 'HEAD')
        with patch.object(release, 'rebase', side_effect=RuntimeError('stop before rebase')), \
                redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['finish', 'merged', '--entry', 'kit', *messages,
                                          *self.options()], self.public), 3)
        self.assertIn('stop before rebase', error.getvalue())
        self.assertEqual(git(private, 'rev-parse', 'HEAD^'), merged)
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
            self.assertEqual(release_main(['finish', 'paired', *messages, *self.options()], self.public), 0,
                             error.getvalue())
            saved = json.loads((self.worktrees / 'paired/build/tests-passed').read_text())
            git(self.worktrees / 'paired/private', 'merge', '--ff-only', saved['commits']['private'])
            self.assertEqual(release_main(['finish', 'paired', '--entry', 'kit', *messages, '--push',
                                           *self.options()], self.public), 0, error.getvalue())
        self.assertEqual(git(self.private_origin, 'show', 'main:setup/lock.toml'), '[kit]\ntree = "paired"')
        for commit in git(self.private_origin, 'rev-list', f'{before[1]}..main').splitlines():
            if git(self.private_origin, 'log', '-1', '--format=%s', commit).startswith('Pair '):
                self.assertEqual(git(self.private_origin, 'show', '--name-only', '--format=', commit), 'release.toml')


if __name__ == '__main__':
    unittest.main()
