from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402
from release_case import CandidateCase, ProcessReplaced, git, release_main  # noqa: E402
from release_case import skip_on_windows as setUpModule  # noqa: E402, F401


class BoundaryTest(CandidateCase):
    def test_recovery_by_later_pair_does_not_claim_the_old_private_target_was_pushed(self):
        self.prepare()
        push = release.push

        def refuse(root, commit):
            if root == self.private_tree:
                raise RuntimeError('private push refused')
            push(root, commit)

        with patch.object(release, 'push', refuse), redirect_stderr(io.StringIO()):
            self.assertEqual(release_main([*self.argv, '--push'], self.public), 3)
        saved = json.loads(release.tests_passed(self.base).read_text())
        self.push_from_another_session(self.private_origin, 'later-pair', {
            'release.toml': f'public_commit = "{self.origin_heads()[0]}"\n', 'later': 'another release\n'})
        before = self.origin_heads()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main([*self.argv, '--push'], self.public), 0)
        self.assertEqual(self.origin_heads(), before)
        self.assertIn('covered by a later pair', output.getvalue())
        self.assertNotIn('Exact trees pushed:', output.getvalue())
        self.assertIn(f'private {saved["trees"]["private"]}', output.getvalue())
        self.assertIn(f'private {git(self.private_origin, "rev-parse", "main^{tree}")}', output.getvalue())

    def test_pair_git_failures_stop_before_certification_and_preserve_source_state(self):
        self.prepare()
        git(self.mine, 'commit', '-qam', 'Public')
        before, remote = self.source(), self.origin_heads()
        original = release.git_bytes
        for command in ('show', 'read-tree', 'hash-object', 'update-index', 'write-tree', 'commit-tree'):
            with self.subTest(command=command):
                reached = []

                def fail(root, *args, **kwargs):
                    if root == self.private_tree and args[0] == command:
                        reached.append(True)
                        raise RuntimeError(f'git {command} failed: injected refusal')
                    return original(root, *args, **kwargs)

                with patch.object(release, 'git_bytes', fail), redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(release_main([*self.argv, '--push'], self.public), 3)
                self.assertTrue(reached)
                self.assertIn(f'git {command} failed: injected refusal', error.getvalue())
                self.assertEqual(self.source(), before)
                self.assertEqual(self.origin_heads(), remote)
                self.assertFalse(release.tests_passed(self.base).exists())
                self.assertEqual(git(self.private_tree, 'status', '--porcelain'), '')

    def test_receipt_rejects_unrelated_missing_and_inconsistent_objects(self):
        self.prepare()
        git(self.mine, 'commit', '-qam', 'Public')
        with self.make_candidate() as draft:
            release.tested(draft, self.base)
            original = json.loads(release.tests_passed(self.base).read_text())
            variants = []
            for field, side, value in (
                    ('source', 'public', git(self.public_origin, 'rev-parse', 'main')),
                    ('source', 'private', draft.trees.private),
                    ('commits', 'private', '0' * 40),
                    ('trees', 'public', git(self.mine, 'rev-parse', 'HEAD^' + '^{tree}')),
                    ('trees', 'private', draft.commits.private)):
                changed = copy.deepcopy(original)
                changed[field][side] = value
                variants.append(changed)
            variants.extend(({**original, 'workspace': '../another-release'}, {**original, 'paired': 0},
                             {**original, 'source': {'public': draft.source.public}}, {'version': 2}, []))
            false_match = copy.deepcopy(original)
            false_match['source']['public'] = git(self.mine, 'rev-parse', 'HEAD^')
            false_match['commits']['public'] = false_match['source']['public']
            variants.append(false_match)
            for bad in variants:
                with self.subTest(receipt=bad):
                    release.tests_passed(self.base).write_text(json.dumps(bad), encoding='utf-8')
                    with patch.object(release, 'test_suite', side_effect=RuntimeError('fresh tests required')):
                        with self.assertRaisesRegex(RuntimeError, 'fresh tests required'):
                            release.tested(draft, self.base)

    def test_receipt_flush_or_replace_interruptions_keep_prior_evidence_and_never_push(self):
        self.prepare()
        self.assertEqual(release_main(self.argv, self.public), 0)
        note = release.tests_passed(self.base)
        original = note.read_bytes()
        remote = self.origin_heads()
        (self.mine / 'content').write_text('fixed\n', encoding='utf-8')
        replace = Path.replace

        def refuse_receipt(path, target):
            if target == note:
                raise OSError('replace refused')
            return replace(path, target)

        for boundary in ('flush', 'replace'):
            with self.subTest(boundary=boundary):
                target = patch.object(release.os, 'fsync', side_effect=OSError('flush refused')) if boundary == 'flush' \
                    else patch.object(Path, 'replace', refuse_receipt)
                with target, redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(release_main([*self.argv, '--push'], self.public), 3)
                self.assertIn(f'{boundary} refused', error.getvalue())
                self.assertEqual(note.read_bytes(), original)
                self.assertEqual(self.origin_heads(), remote)
        self.assertEqual(release_main([*self.argv, '--push'], self.public), 0)
        self.assertEqual(git(self.public_origin, 'show', 'main:content'), 'fixed')

    def test_final_candidate_checks_are_not_replaced_by_early_source_checks(self):
        self.prepare()
        (self.mine / 'lock_setup.py').write_text('', encoding='utf-8')
        git(self.mine, 'add', '-f', 'lock_setup.py')
        remote = self.origin_heads()
        for name, verdict in (('review_stop', 'candidate review refused'), ('reused_release_names', ['package']),
                              ('edited_owned', {'entry': 'candidate stale pin'}), ('reapprovals', ['hook']),
                              ('leaks', ['fixture-private-marker'])):
            original, visited = getattr(release, name), []

            def refuse(public, private, *args):
                if public.parent.parent.name == 'candidates':
                    visited.append(True)
                    return verdict
                return original(public, private, *args)

            with self.subTest(check=name), patch.object(release, name, refuse), redirect_stderr(io.StringIO()) as error:
                flags = ['--entry', 'entry', '--refresh-owned'] if name == 'edited_owned' else []
                self.assertEqual(release_main([*self.argv, '--push', *flags], self.public), 3)
            self.assertTrue(visited)
            self.assertIn('Local commits remain', error.getvalue())
            self.assertEqual(self.origin_heads(), remote)
            self.assertFalse(release.tests_passed(self.base).exists())

    def test_cleanup_interruption_after_each_filesystem_transition_can_resume(self):
        self.prepare()
        self.assertEqual(release_main(self.argv, self.public), 0)
        for operation in ('replace', 'rmdir', 'unlink'):
            original = getattr(Path, operation)

            def crash(path, *args, **kwargs):
                result = original(path, *args, **kwargs)
                raise ProcessReplaced('cleanup interrupted')

            with self.subTest(operation=operation):
                with patch.object(Path, operation, crash), self.assertRaises(ProcessReplaced):
                    release.retire_candidates(self.mine, self.private_tree, self.base)
                release.retire_candidates(self.mine, self.private_tree, self.base)
                self.assertFalse(list((self.base / 'build/candidates').iterdir()))
                if operation != 'unlink':
                    with self.make_candidate():
                        pass
        self.assertTrue(release.tests_passed(self.base).exists())
        self.assertTrue((self.base / 'build/tests.log').exists())
        self.assertTrue(self.mine.is_dir())


if __name__ == '__main__':
    unittest.main()
