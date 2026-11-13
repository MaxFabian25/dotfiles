from contextlib import redirect_stderr, redirect_stdout
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
from release_case import PASSING_TEST, ReleaseCase, SEEDED_TESTS, git, release_main  # noqa: E402
from release_case import skip_on_windows as setUpModule  # noqa: E402, F401


class ReviewTest(ReleaseCase):
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
            module.write_text(PASSING_TEST + '\n# slipped in\n', encoding='utf-8')
            return check(*arguments)

        errors = io.StringIO()
        with redirect_stderr(errors), patch.object(release, 'reused_release_names', edit_after_the_first_review_check):
            self.assertEqual(release_main(['finish', 'late', '--message', str(message), '--push', *self.options()],
                                          self.public), 3)
        self.assertIn('reviews other code', errors.getvalue())
        self.assertIn('nothing was pushed', errors.getvalue())
        self.assertEqual(git(self.public_origin, 'ls-tree', '--name-only', 'main', 'tests/'), SEEDED_TESTS)


if __name__ == '__main__':
    unittest.main()
