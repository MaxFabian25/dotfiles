from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402
from release_case import (ALLOWLIST, FAILING_TEST, PASSING_TEST, RUN_ALONE, ReleaseCase,
                          SEEDED_TESTS, git, release_main)  # noqa: E402
from release_case import skip_on_windows as setUpModule  # noqa: E402, F401


class LockTest(ReleaseCase):
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
        for tree in (mine, lock_only):
            git(tree, 'fetch', '-q', 'origin')
        onto = git(mine, 'rev-parse', 'origin/main')
        with self.assertRaisesRegex(RuntimeError, 'rebase it by hand'):
            release.rebase(mine, onto=onto)
        self.assertEqual(git(mine, 'rev-parse', 'HEAD'), before)
        self.assertEqual(git(mine, 'status', '--porcelain'), '')
        with self.assertRaisesRegex(RuntimeError, 'rebase it by hand'):  # nothing of its own would be left
            release.rebase(lock_only, regenerated, onto=onto)
        self.assertEqual(git(lock_only, 'status', '--porcelain'), '')

        self.assertEqual(release.rebase(mine, regenerated, onto=onto), 'regenerate')
        self.assertEqual((mine / lock).read_text(encoding='utf-8'), '[kit]\ntree = "c"\n')
        self.assertEqual((mine / 'notes').read_text(encoding='utf-8'), 'mine\n')
        self.assertEqual(git(mine, 'log', '--format=%B', '-1'), 'Change the kit')
        self.assertEqual(git(mine, 'log', '--format=%s', '-3').splitlines(), ['Change the kit', 'Their kit', 'Lock'])
        self.assertIn('gpgsig -----BEGIN SSH SIGNATURE-----', git(mine, 'cat-file', 'commit', 'HEAD'))
        self.assertEqual(release.rebase(mine, regenerated, onto=onto), '')

    def test_a_finish_waits_for_preparation_then_tests_outside_the_lock(self):
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
        self.assertNotIn('Ran 2 tests', output.getvalue())
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
        self.assertLess(output.getvalue().index('Held the release lock'), output.getvalue().index('Ran 2 tests'))
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
            if public.parents[3].name == 'slow' and not finished:
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
            with patch.object(release, 'device_check', side_effect=RuntimeError('device blocked')), \
                    redirect_stderr(io.StringIO()), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(release_main(finish, self.public), 3)
            return output.getvalue()

        with patch.object(release, 'test_suite', counted):
            stopped_behind_the_tests()  # on the failing test
            self.assertFalse(note.exists())
            module.write_text(PASSING_TEST, encoding='utf-8')
            stopped_behind_the_tests()
            self.assertEqual(len(runs), 2)  # a failed run vouches for nothing
            self.assertIn('The tests did not run again: they passed on these exact trees before', stopped_behind_the_tests())
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
            git(tree, 'commit', '-q', '--allow-empty', '-m', 'By hand')
            stopped_behind_the_tests()
            self.assertEqual(len(runs), 6)
            (tree / 'notes').write_text('edited\n', encoding='utf-8')
            git(tree, 'commit', '-qam', 'By hand again')
            stopped_behind_the_tests()
            self.assertEqual(len(runs), 7)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(release_main(finish, self.public), 0)
        self.assertEqual(len(runs), 7)
        self.assertIn('The tests did not run again', output.getvalue())
        self.assertEqual(git(self.public_origin, 'show', 'main:notes'), 'edited')

    def test_a_worktree_without_work_of_its_own_needs_new_evidence_when_origin_moves(self):
        import portable
        release_main(['start', 'half', *self.options()], self.public)
        (self.worktrees / 'half/private/notes').write_text('mine\n', encoding='utf-8')
        message = self.root / 'message'
        message.write_text('Add notes\n', encoding='utf-8')
        finish = ['finish', 'half', '--private-message', str(message), '--push', '--timeout', '0', *self.options()]
        with patch.object(release, 'device_check', side_effect=RuntimeError('device blocked')), \
                redirect_stderr(io.StringIO()), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(release_main(finish, self.public), 3)
        self.push_from_another_session(self.public_origin, 'other', {'doc': 'theirs\n'})
        git(self.public, 'fetch', '-q', 'origin')  # as that session's pull does, for every worktree of the checkout
        with patch.object(release, 'test_suite', wraps=release.test_suite) as suite, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(release_main(finish, self.public), 0)
        self.assertEqual(suite.call_count, 1)
        self.assertEqual((self.worktrees / 'half/public/doc').read_text(encoding='utf-8'), 'theirs\n')
        self.assertEqual(git(self.private_origin, 'show', 'main:notes'), 'mine')

    def test_finish_stops_on_files_that_changed_during_or_after_their_tests(self):
        message = self.root / 'message'
        message.write_text('Change the content\n', encoding='utf-8')
        suite, hold = release.test_suite, release.hold
        for name in ('during', 'after'):
            release_main(['start', name, *self.options()], self.public)
            file = self.worktrees / name / 'public/content'
            file.write_text(f'tested {name}\n', encoding='utf-8')

            def edit_during_the_suite(*arguments):
                suite(*arguments)
                file.write_text(f'slipped in {name}\n', encoding='utf-8')

            def edit_during_the_wait(*arguments, **keywords):
                if (self.worktrees / name / 'build/tests-passed').exists():
                    file.write_text(f'slipped in {name}\n', encoding='utf-8')
                return hold(*arguments, **keywords)

            late = (patch.object(release, 'test_suite', edit_during_the_suite) if name == 'during' else
                    patch.object(release, 'hold', edit_during_the_wait))
            finish = ['finish', name, '--message', str(message), '--push', *self.options()]
            with self.subTest(name), late, redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()):
                self.assertEqual(release_main(finish, self.public), 3)
            self.assertIn('changed after preparation (content)', error.getvalue())
            self.assertIn('the edit is preserved', error.getvalue())
            self.assertNotIn('committed nothing', error.getvalue())
            self.assertEqual(git(self.public_origin, 'show', 'main:content'),
                             'first' if name == 'during' else 'slipped in during')
            self.assertTrue((self.worktrees / name / 'build/tests-passed').exists())
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
        self.assertNotIn('Ran 2 tests', output.getvalue())
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
        for fails_at in (1, 2):
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
            self.assertFalse((self.worktrees / 'unread/build/tests-passed').exists())
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
        self.assertGreater(finished, 4)
        self.assertGreater(len(fetches), finished)
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

    def test_a_stop_names_how_the_worktrees_changed(self):
        release_main(['start', 'named', *self.options()], self.public)
        public, private = self.worktrees / 'named/public', self.worktrees / 'named/private'
        before = release.tested_state(public, private)
        self.assertEqual(before, release.Trees(git(public, 'rev-parse', 'HEAD^{tree}'),
                                               git(private, 'rev-parse', 'HEAD^{tree}')))
        (public / 'content').write_text('named\n', encoding='utf-8')
        first = release.tested_state(public, private)
        self.assertEqual(release.state_changes(public, private, before, first), 'public: content')
        self.assertEqual(release.state_changes(public, private, first, before), 'public: content')
        self.assertEqual(release.state_changes(public, private, first, first), '')
        (public / '.gitignore').write_text(ALLOWLIST + '!/more/\n!/more/**\n', encoding='utf-8')
        (public / 'more').mkdir()
        for number in range(9):
            (public / 'more' / f'file{number}').write_text('more\n', encoding='utf-8')
        (private / 'notes').write_text('mine\n', encoding='utf-8')
        second = release.tested_state(public, private)
        self.assertEqual(release.state_changes(public, private, first, second),
                         'public: .gitignore, more/file0, more/file1, more/file2, more/file3, more/file4, more/file5, '
                         'more/file6 and 2 more; private: notes')

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


if __name__ == '__main__':
    unittest.main()
