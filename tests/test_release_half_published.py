from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402
from release_case import ReleaseCase, git, release_main  # noqa: E402
from release_case import skip_on_windows as setUpModule  # noqa: E402, F401


class HalfPublishedTest(ReleaseCase):
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


if __name__ == '__main__':
    unittest.main()
