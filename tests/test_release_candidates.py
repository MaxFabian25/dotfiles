from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402
from release_case import CandidateCase, PASSING_TEST, RUN_ALONE, git, release_main  # noqa: E402
from release_case import skip_on_windows as setUpModule  # noqa: E402, F401


class CandidateTest(CandidateCase):
    def test_pair_preserves_source_index_bytes_modes_and_signs_captured_commits(self):
        self.prepare()
        git(self.mine, 'commit', '-qam', 'Captured public')
        raw = (f'# retained\r\npublic_commit = "{git(self.public_origin, "rev-parse", "main")}"  # pointer\r\n'
               'other = "unchanged"\r\n').encode()
        (self.private_tree / 'release.toml').write_bytes(raw)
        git(self.private_tree, 'add', 'release.toml')
        git(self.private_tree, 'update-index', '--chmod=+x', 'release.toml')
        git(self.private_tree, 'commit', '-qm', 'Keep metadata')
        source = self.source()
        before = (git(self.private_tree, 'write-tree'), git(self.private_tree, 'status', '--porcelain'),
                  (self.private_tree / 'release.toml').read_bytes())
        message = self.root / 'pair-message'
        message.write_bytes(b'Custom pair\n\nIts body.\n')
        (self.mine / 'content').write_text('later public\n', encoding='utf-8')
        git(self.mine, 'commit', '-qam', 'Later public')
        target = release.pair(self.mine, self.private_tree, source, message)
        self.assertEqual(git(self.private_tree, 'rev-parse', 'HEAD'), source.private)
        self.assertEqual((git(self.private_tree, 'write-tree'), git(self.private_tree, 'status', '--porcelain'),
                          (self.private_tree / 'release.toml').read_bytes()), before)
        self.assertEqual(git(self.private_tree, 'rev-parse', f'{target}^'), source.private)
        self.assertEqual(release.git_bytes(self.private_tree, 'show', f'{target}:release.toml'),
                         raw.replace(git(self.public_origin, 'rev-parse', 'main').encode(), source.public.encode()))
        self.assertEqual(git(self.private_tree, 'ls-tree', target, 'release.toml').split()[0], '100755')
        self.assertEqual(git(self.private_tree, 'log', '-1', '--format=%B', target), 'Custom pair\n\nIts body.')
        allowed = self.root / 'allowed-signers'
        allowed.write_text('test@example.invalid ' + self.key.with_suffix('.pub').read_text(), encoding='utf-8')
        git(self.private_tree, '-c', f'gpg.ssh.allowedSignersFile={allowed}', 'verify-commit', target)
        self.assertEqual(release.pair(self.mine, self.private_tree, release.Heads(source.public, target), None), target)

    def test_pair_rejects_missing_duplicate_malformed_and_nested_pointers(self):
        self.prepare()
        git(self.mine, 'commit', '-qam', 'Public')
        original = (self.private_tree / 'release.toml').read_bytes()
        for raw in (b'other = 1\n', original + original, b'public_commit = "bad"\n', b'[nested]\n' + original,
                    original + b'[nested]\n' + original, original.replace(b'"', b"'", 1)):
            with self.subTest(raw=raw):
                (self.private_tree / 'release.toml').write_bytes(raw)
                git(self.private_tree, 'commit', '-qam', 'Bad pointer')
                source = self.source()
                with self.assertRaisesRegex(RuntimeError, 'release.toml'):
                    release.pair(self.mine, self.private_tree, source, None)
                self.assertEqual(self.source(), source)
                self.assertFalse(release.tests_passed(self.base).exists())

    def test_signing_failure_preserves_sources_and_never_certifies_or_pushes(self):
        self.prepare()
        git(self.mine, 'commit', '-qam', 'Public')
        before, remote = self.source(), self.origin_heads()
        git(self.private_tree, 'config', 'user.signingkey', str(self.root / 'missing-signing-key'))
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main([*self.argv, '--push'], self.public), 3)
        self.assertIn('git commit-tree exited', error.getvalue())
        self.assertEqual(self.source(), before)
        self.assertEqual(self.origin_heads(), remote)
        self.assertFalse(release.tests_passed(self.base).exists())

    def test_finish_tests_the_exact_signed_trees_it_pushes_in_detached_siblings(self):
        self.prepare()
        observed = self.root / 'observed.json'
        os.environ['OBSERVED'] = str(observed)
        script = ('import json, os, subprocess, unittest\nfrom pathlib import Path\n'
                  'class FinalPair(unittest.TestCase):\n'
                  '    def test_final_pair(self):\n'
                  '        root = Path(__file__).resolve().parent\n'
                  '        def git(folder, *args):\n'
                  '            return subprocess.check_output(["git", "-C", str(folder), *args]).decode().strip()\n'
                  '        public = root.parent / "public"\n'
                  '        expected = git(public, "rev-parse", "HEAD")\n'
                  '        self.assertIn(expected, (root / "release.toml").read_text())\n'
                  '        self.assertEqual((root / "guide.md").read_text(), "combined\\n")\n'
                  '        data = {side: git(root.parent / side, "rev-parse", "HEAD^{tree}")\n'
                  '                for side in ("public", "private")}\n'
                  '        Path(os.environ["OBSERVED"]).write_text(json.dumps(data))\n' + RUN_ALONE)
        (self.private_tree / 'test_final.py').write_text(script, encoding='utf-8')
        self.push_from_another_session(self.private_origin, 'upstream', {'guide.md': 'combined\n'})
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main([*self.argv, '--push'], self.public), 0)
        read = json.loads(observed.read_text())
        for side, repo in (('public', self.public_origin), ('private', self.private_origin)):
            self.assertEqual(git(repo, 'rev-parse', 'main^{tree}'), read[side])
            self.assertIn('gpgsig -----BEGIN SSH SIGNATURE-----', git(repo, 'cat-file', 'commit', 'main'))
            self.assertIn(read[side], output.getvalue())
        self.assertNotEqual(git(self.private_tree, 'rev-parse', 'HEAD'), self.origin_heads()[1])
        self.assertFalse(list((self.base / 'build/candidates').iterdir()))
        self.assertTrue(release.tests_passed(self.base).exists())

    def test_documentation_only_candidate_runs_both_complete_suites(self):
        self.push_from_another_session(self.private_origin, 'private-test', {'test_seed.py': PASSING_TEST + RUN_ALONE})
        self.prepare()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release_main(self.argv, self.public), 0)
        self.assertIn('Ran 1 test', (self.base / 'build/tests.log').read_text())
        self.assertIn('Ran 1 private tests', output.getvalue())
        self.assertIn('Exact trees tested:', output.getvalue())
        self.assertIn('not pushed', output.getvalue())

    def test_receipt_cache_requires_exact_complete_trees_and_records_new_targets(self):
        self.prepare()
        git(self.mine, 'commit', '-qam', 'Public')
        with self.make_candidate() as first:
            release.tested(first, self.base)
            original = json.loads(release.tests_passed(self.base).read_text())
        with patch.dict(os.environ, {'GIT_COMMITTER_DATE': '2030-01-01T00:00:00+0000'}), self.make_candidate() as same:
            self.assertNotEqual(same.commits.private, first.commits.private)
            with patch.object(release, 'test_suite', side_effect=AssertionError('unchanged trees were retested')):
                release.tested(same, self.base)
            saved = json.loads(release.tests_passed(self.base).read_text())
            self.assertEqual(saved['commits']['private'], same.commits.private)
            self.assertEqual(saved['trees'], original['trees'])
        for invalid in ({'public': 'origin/main', 'private': 'origin/main'}, {'version': 1}, [],
                        {**original, 'paired': 'yes'}, {**original, 'trees': {'public': 'bad', 'private': '0' * 40}}):
            with self.subTest(invalid=invalid), self.make_candidate() as draft:
                release.tests_passed(self.base).write_text(json.dumps(invalid), encoding='utf-8')
                with patch.object(release, 'test_suite', side_effect=RuntimeError('suite called')):
                    with self.assertRaisesRegex(RuntimeError, 'suite called'):
                        release.tested(draft, self.base)
        release.tests_passed(self.base).write_text(json.dumps(original), encoding='utf-8')
        (self.private_tree / 'guide.md').write_text('new dependency\n', encoding='utf-8')
        git(self.private_tree, 'add', 'guide.md')
        git(self.private_tree, 'commit', '-qm', 'Guide')
        with self.make_candidate() as draft, redirect_stdout(io.StringIO()) as output:
            release.tested(draft, self.base)
        self.assertIn('Both complete suites run because private: guide.md', output.getvalue())

    def test_receipt_replace_failure_cannot_publish_and_keeps_previous_evidence(self):
        self.prepare()
        note = release.tests_passed(self.base)
        replace = Path.replace

        def refuse(path, target):
            if target == note:
                raise OSError('receipt replacement refused')
            return replace(path, target)

        before = self.origin_heads()
        with patch.object(Path, 'replace', refuse), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main([*self.argv, '--push'], self.public), 3)
        self.assertIn('receipt replacement refused', error.getvalue())
        self.assertEqual(self.origin_heads(), before)
        self.assertFalse(note.exists())
        self.assertTrue(list((self.base / 'build/candidates').iterdir()))

    def test_candidate_mutation_during_tests_or_device_check_blocks_publication(self):
        for boundary, flags in (('test_suite', ['--push']), ('device_check', ['--push']), ('device_check', [])):
            with self.subTest(boundary=boundary, flags=flags):
                self.prepare(boundary + ('-push' if flags else '-retain'))
                original = getattr(release, boundary)
                remote = self.origin_heads()

                def mutate(public, private, state):
                    original(public, private, state)
                    (public / 'content').write_text('untested\n', encoding='utf-8')

                with patch.object(release, boundary, mutate), redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(release_main([*self.argv, *flags], self.public), 3)
                self.assertIn('changed after preparation (content)', error.getvalue())
                self.assertEqual(self.origin_heads(), remote)
                if boundary == 'test_suite':
                    self.assertFalse(release.tests_passed(self.base).exists())

    def test_source_mutation_during_tests_preserves_tracked_staged_and_dropped_edits(self):
        suite = release.test_suite
        run = release.subprocess.run
        for kind in ('tracked', 'staged', 'dropped', 'head'):
            with self.subTest(kind=kind):
                self.prepare(kind)
                before = self.origin_heads()
                device_calls = []

                def record_device(command, *args, **kwargs):
                    if any(str(argument).endswith('/sync.py') for argument in command):
                        device_calls.append(command)
                    return run(command, *args, **kwargs)

                def mutate(public, private, log):
                    suite(public, private, log)
                    if kind == 'dropped':
                        (self.mine / 'unlisted').write_text('preserve\n', encoding='utf-8')
                    else:
                        (self.mine / 'content').write_text('preserve\n', encoding='utf-8')
                        if kind == 'staged':
                            git(self.mine, 'add', 'content')
                            (self.mine / 'content').write_text('second\n', encoding='utf-8')
                        if kind == 'head':
                            git(self.mine, 'commit', '-qam', 'Outside writer')

                with patch.object(release, 'test_suite', mutate), \
                        patch.object(release.subprocess, 'run', record_device), redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(release_main([*self.argv, '--push'], self.public), 3)
                self.assertEqual(self.origin_heads(), before)
                self.assertEqual(device_calls, [])
                self.assertIn('leaves out unlisted' if kind == 'dropped' else 'changed after preparation', error.getvalue())
                if kind == 'staged':
                    self.assertEqual(git(self.mine, 'show', ':content'), 'preserve')
                else:
                    self.assertEqual((self.mine / ('unlisted' if kind == 'dropped' else 'content')).read_text(), 'preserve\n')

    def test_candidate_creation_uses_captured_heads_and_refuses_changed_sources(self):
        self.prepare()
        git(self.mine, 'commit', '-qam', 'Public')
        source = self.source()
        (self.mine / 'content').write_text('late\n', encoding='utf-8')
        git(self.mine, 'commit', '-qam', 'Late')
        with self.assertRaisesRegex(RuntimeError, 'changed after preparation'):
            with release.candidate(self.mine, self.private_tree, self.base, source=source, paired=True, message=None):
                self.fail('candidate accepted a different source')

    def test_source_identity_change_with_equal_tree_stops_publication(self):
        self.prepare()
        suite, before = release.test_suite, self.origin_heads()

        def empty_commit(*arguments):
            suite(*arguments)
            git(self.mine, 'commit', '-q', '--allow-empty', '-m', 'Concurrent identity change')

        with patch.object(release, 'test_suite', empty_commit), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main([*self.argv, '--push'], self.public), 3)
        self.assertIn('HEAD/tree', error.getvalue())
        self.assertEqual(self.origin_heads(), before)

    def test_push_uses_the_explicit_commit_even_after_the_source_head_moves(self):
        self.prepare()
        git(self.mine, 'commit', '-qam', 'Tested target')
        tested = git(self.mine, 'rev-parse', 'HEAD')
        (self.mine / 'content').write_text('later untested content\n', encoding='utf-8')
        git(self.mine, 'commit', '-qam', 'Concurrent later commit')
        release.push(self.mine, tested)
        self.assertEqual(git(self.public_origin, 'rev-parse', 'main'), tested)
        self.assertEqual((self.mine / 'content').read_text(), 'later untested content\n')


if __name__ == '__main__':
    unittest.main()
