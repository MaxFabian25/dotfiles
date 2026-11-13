from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402
from release_case import ReleaseCase, git, release_main  # noqa: E402
from release_case import skip_on_windows as setUpModule  # noqa: E402, F401


class BranchesTest(ReleaseCase):
    def unloadable_setup(self, name):
        """Start a release whose public setup this engine cannot load, as an engine that retired a build cannot load
        a setup that still names it, and return its two worktrees."""
        release_main(['start', name, *self.options()], self.public)
        public, private = self.worktrees / name / 'public', self.worktrees / name / 'private'
        (public / release.SETUP).parent.mkdir(parents=True)
        (public / release.SETUP).write_text('[retired]\nbuild = "academic"\n', encoding='utf-8')
        return public, private

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


if __name__ == '__main__':
    unittest.main()
