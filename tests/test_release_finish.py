from contextlib import redirect_stderr
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402
from release_case import PASSING_TEST, ReleaseCase, SYNC_WITH_CHANGES, git, release_main  # noqa: E402
from release_case import skip_on_windows as setUpModule  # noqa: E402, F401


class FinishTest(ReleaseCase):
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


if __name__ == '__main__':
    unittest.main()
