from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import portable  # noqa: E402
import release  # noqa: E402
from release_case import CandidateCase, ProcessReplaced, git, release_main  # noqa: E402
from release_case import skip_on_windows as setUpModule  # noqa: E402, F401


class RecoveryTest(CandidateCase):
    def test_crash_after_public_acceptance_before_note_recovers_the_receipted_target(self):
        self.prepare()
        push = release.push

        def crash(root, commit):
            push(root, commit)
            if root == self.mine:
                raise ProcessReplaced('public accepted before note')

        with patch.object(release, 'push', crash), self.assertRaises(ProcessReplaced):
            release_main([*self.argv, '--push'], self.public)
        saved = json.loads(release.tests_passed(self.base).read_text())
        self.assertEqual(self.origin_heads()[0], saved['commits']['public'])
        self.assertFalse(release.public_pushed(self.base).exists())
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['clean', 'candidate', *self.options()], self.public), 3)
        self.assertIn('on origin without its pair', error.getvalue())
        self.assertTrue(self.mine.is_dir())
        self.push_from_another_session(self.private_origin, 'private-next', {'notes': 'other work\n'})
        self.assertEqual(release_main([*self.argv, '--push'], self.public), 0)
        self.assertIn(saved['commits']['public'], git(self.private_origin, 'show', 'main:release.toml'))
        self.assertFalse(release.public_pushed(self.base).exists())

    def test_completed_publication_recovers_without_duplicate_pair_and_pulls_under_lock(self):
        self.prepare()
        push = release.push

        def crash(root, commit):
            push(root, commit)
            if root == self.private_tree:
                raise ProcessReplaced('both accepted before cleanup')

        with patch.object(release, 'push', crash), self.assertRaises(ProcessReplaced):
            release_main([*self.argv, '--push'], self.public)
        before = self.origin_heads()
        (self.public / 'pull.py').write_text(
            'from pathlib import Path\n'
            'root = Path(__file__).resolve().parent\n'
            'assert (root.parent / "state/release.lock.holder").is_file()\n'
            '(root / "pulled").write_text("under lock")\n', encoding='utf-8')
        with patch.object(release, 'test_suite', side_effect=AssertionError('completed pair was retested')):
            with portable.exclusive_lock(self.root / 'state/release.lock', record=True), \
                    redirect_stderr(io.StringIO()) as error:
                self.assertEqual(release_main([*self.argv, '--push', '--pull', '--timeout', '0'], self.public), 3)
            self.assertIn('pair is already published', error.getvalue())
            self.assertFalse((self.public / 'pulled').exists())
            self.assertEqual(release_main([*self.argv, '--push', '--pull'], self.public), 0)
        self.assertEqual((self.public / 'pulled').read_text(), 'under lock')
        self.assertEqual(self.origin_heads(), before)
        self.assertFalse(release.public_pushed(self.base).exists())
        self.assertFalse(list((self.base / 'build/candidates').iterdir()))

    def test_lost_private_acknowledgement_is_reconciled_as_success(self):
        self.prepare()
        push = release.push

        def lost(root, commit):
            push(root, commit)
            if root == self.private_tree:
                raise RuntimeError('connection lost after acceptance')

        with patch.object(release, 'push', lost):
            self.assertEqual(release_main([*self.argv, '--push'], self.public), 0)
        self.assertIn(self.origin_heads()[0], git(self.private_origin, 'show', 'main:release.toml'))
        self.assertFalse(release.public_pushed(self.base).exists())

    def test_unreachable_origin_after_ambiguous_push_keeps_evidence_and_reports_unknown(self):
        self.prepare()
        push, fetch = release.push, release.fetch
        sent = []

        def lost(root, commit):
            push(root, commit)
            sent.append(True)
            raise RuntimeError('connection lost after acceptance')

        def unreachable(root):
            if sent:
                raise RuntimeError('fetch failed: network unavailable')
            return fetch(root)

        with patch.object(release, 'push', lost), patch.object(release, 'fetch', unreachable), \
                redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main([*self.argv, '--push'], self.public), 3)
        self.assertIn('fetch failed: network unavailable', error.getvalue())
        self.assertIn('publication state could not be verified', error.getvalue())
        self.assertNotIn('nothing was pushed', error.getvalue())
        self.assertTrue(release.tests_passed(self.base).is_file())
        self.assertTrue(list((self.base / 'build/candidates').iterdir()))
        self.assertEqual(release_main([*self.argv, '--push'], self.public), 0)

    def test_successful_start_resets_old_evidence_but_partial_start_preserves_it(self):
        self.prepare()
        self.assertEqual(release_main(self.argv, self.public), 0)
        receipt = release.tests_passed(self.base).read_bytes()
        folders = list((self.base / 'build/candidates').iterdir())
        for tree, repo in ((self.mine, self.public),):
            git(repo, 'worktree', 'remove', '--force', str(tree))
            git(repo, 'branch', '-D', 'candidate')
        with redirect_stderr(io.StringIO()):
            self.assertEqual(release_main(['start', 'candidate', *self.options()], self.public), 3)
        self.assertEqual(release.tests_passed(self.base).read_bytes(), receipt)
        self.assertTrue(all(folder.is_dir() for folder in folders))
        for tree, repo in ((self.mine, self.public), (self.private_tree, self.private)):
            git(repo, 'worktree', 'remove', '--force', str(tree))
            git(repo, 'branch', '-D', 'candidate')
        self.assertEqual(release_main(['start', 'candidate', *self.options()], self.public), 0)
        self.assertFalse(release.tests_passed(self.base).exists())
        self.assertFalse(list((self.base / 'build/candidates').iterdir()))
        self.assertNotIn('candidates', git(self.public, 'worktree', 'list'))
        self.assertNotIn('candidates', git(self.private, 'worktree', 'list'))

    def test_candidate_creation_interruption_retains_registered_objects_and_can_resume(self):
        for side in ('public', 'private'):
            with self.subTest(side=side):
                self.prepare(side)
                (self.mine / 'content').write_text(f'{side}\n', encoding='utf-8')
                original = release.git

                def crash(root, *args):
                    result = original(root, *args)
                    if args[:2] == ('worktree', 'add') and '--detach' in args and Path(args[-2]).name == side:
                        raise ProcessReplaced('interrupted materialisation')
                    return result

                with patch.object(release, 'git', crash), self.assertRaises(ProcessReplaced):
                    release_main(self.argv, self.public)
                self.assertFalse(release.tests_passed(self.base).exists())
                self.assertEqual(git(self.private_tree, 'status', '--porcelain'), '')
                self.assertEqual(release_main([*self.argv, '--push'], self.public), 0)
                self.assertFalse(list((self.base / 'build/candidates').iterdir()))

    def test_cleanup_refuses_unowned_directory_and_another_repository_registration(self):
        self.prepare()
        self.assertEqual(release_main(self.argv, self.public), 0)
        foreign = self.base / 'build/candidates/round-foreign'
        foreign.mkdir()
        (foreign / 'keep').write_text('not a candidate\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'unrecognised candidate workspace'):
            release.retire_candidates(self.mine, self.private_tree, self.base)
        self.assertEqual((foreign / 'keep').read_text(), 'not a candidate\n')
        self.assertTrue(self.mine.is_dir())
        (foreign / 'keep').unlink()
        (foreign / 'owner.json').write_text('{}\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'unrecognised candidate workspace'):
            release.retire_candidates(self.mine, self.private_tree, self.base)
        self.assertTrue(foreign.is_dir())
        (foreign / 'owner.json').unlink()
        foreign.rmdir()
        with self.make_candidate() as draft:
            git(self.public, 'worktree', 'remove', '--force', str(draft.public))
            git(self.private, 'worktree', 'add', '--detach', str(draft.public), 'HEAD')
            with self.assertRaisesRegex(RuntimeError, 'not registered to its repository'):
                release.retire_candidates(self.mine, self.private_tree, self.base)
            self.assertTrue(draft.public.is_dir())

    def test_clean_uses_release_lock_and_removes_only_its_candidate_registrations(self):
        self.prepare('first')
        first, first_argv = self.base, self.argv
        self.assertEqual(release_main(self.argv, self.public), 0)
        self.prepare('second')
        self.assertEqual(release_main(self.argv, self.public), 0)
        other = list((self.base / 'build/candidates').iterdir())
        with portable.exclusive_lock(release.finish_lock(first)), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(['clean', 'first', '--force', *self.options()], self.public), 3)
        self.assertIn('another finish of first', error.getvalue())
        self.assertEqual(release_main(['clean', 'first', '--force', *self.options()], self.public), 0)
        self.assertTrue(all(folder.is_dir() for folder in other))
        self.assertNotIn(str(first), git(self.public, 'worktree', 'list'))
        self.assertNotIn(str(first), git(self.private, 'worktree', 'list'))

    def test_release_metadata_overlap_requires_review_but_pointer_only_legacy_pair_replays(self):
        pointer = git(self.public_origin, 'rev-parse', 'main')
        raw = f'public_commit = "{pointer}"\n' + ''.join(f'meta{i} = "old"\n' for i in range(10))
        self.push_from_another_session(self.private_origin, 'metadata', {'release.toml': raw})
        self.prepare('metadata')
        (self.private_tree / 'release.toml').write_text(raw.replace('meta0 = "old"', 'meta0 = "mine"'), encoding='utf-8')
        self.push_from_another_session(self.private_origin, 'other-metadata', {
            'release.toml': raw.replace('meta9 = "old"', 'meta9 = "theirs"')})
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main([*self.argv, '--push'], self.public), 3)
        self.assertIn('private: release.toml', error.getvalue())
        self.assertIn('--overlap-reviewed', error.getvalue())
        self.assertEqual(release_main([*self.argv, '--push', '--overlap-reviewed'], self.public), 0)
        combined = git(self.private_origin, 'show', 'main:release.toml')
        self.assertIn('meta0 = "mine"', combined)
        self.assertIn('meta9 = "theirs"', combined)
        self.prepare('legacy')
        (self.mine / 'content').write_text('legacy\n', encoding='utf-8')
        git(self.mine, 'commit', '-qam', 'Legacy public')
        target = release.pair(self.mine, self.private_tree, self.source(), None)
        git(self.private_tree, 'merge', '--ff-only', target)
        self.assertTrue(release.pair_only(self.private_tree, target))
        self.push_from_another_session(self.public_origin, 'other-public', {'doc': 'other\n'})
        newer = git(self.public_origin, 'rev-parse', 'main')
        before = git(self.private_origin, 'show', 'main:release.toml')
        self.push_from_another_session(self.private_origin, 'other-pair', {
            'release.toml': before.replace(self.origin_heads()[0], newer).replace(
                release.public_pointer(before.encode())[2].decode(), newer) + '\n'})
        self.assertEqual(release_main([*self.argv, '--push'], self.public), 0)
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'legacy')
        self.assertIn(self.origin_heads()[0], git(self.private_origin, 'show', 'main:release.toml'))


if __name__ == '__main__':
    unittest.main()
