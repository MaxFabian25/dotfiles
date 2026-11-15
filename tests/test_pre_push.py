from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homes import isolate_git
from leaks import DENYLIST

ROOT = Path(__file__).resolve().parents[1]


def setUpModule():
    isolate_git()


class PrePushTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        self.public = self.folder / 'dotfiles'
        self.public.mkdir()
        self.private = self.folder / 'dotfiles-private'
        self.denylist = self.private / DENYLIST
        self.denylist.parent.mkdir(parents=True)
        self.denylist.write_text('secret-host\n', encoding='utf-8')
        for name in ('leaks.py', 'portable.py'):
            shutil.copyfile(ROOT / name, self.public / name)
        shutil.copytree(ROOT / '.githooks', self.public / '.githooks')
        venv.EnvBuilder().create(self.public / '.venv')
        (self.public / '.gitignore').write_text('.venv/\n', encoding='utf-8')
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'core.hooksPath', str(self.public / '.githooks'))
        self.commit(self.public, 'Initial clean files')
        self.remote = self.folder / 'remote.git'
        self.git('init', '-q', '--bare', str(self.remote))
        self.git('remote', 'add', 'origin', str(self.remote))

    def git(self, *arguments, cwd=None, check=True):
        return subprocess.run(['git', '-C', str(cwd or self.public), *arguments], check=check,
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=120)

    def commit(self, root, message):
        self.git('add', '-A', cwd=root)
        self.git('commit', '-qm', message, '--allow-empty', cwd=root)
        return self.git('rev-parse', 'HEAD', cwd=root).stdout.strip()

    def linked(self):
        root = self.folder / 'tasks' / 'topic'
        self.git('worktree', 'add', '-qb', 'topic', str(root))
        decoy = root.parent / 'dotfiles-private' / DENYLIST
        decoy.parent.mkdir(parents=True)
        decoy.write_text('a-different-private-string\n', encoding='utf-8')
        return root

    def assert_rejected(self, done, reason):
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(reason, done.stdout + done.stderr)
        self.assertNotIn('secret-host', done.stdout + done.stderr)
        self.assertEqual(self.git('for-each-ref', '--format=%(refname)', cwd=self.remote).stdout, '')

    def test_clean_pushes_from_canonical_and_linked_worktrees_reach_the_remote(self):
        self.git('push', '-q', 'origin', 'main')
        linked = self.linked()
        (linked / 'notes.md').write_text('A clean change\n', encoding='utf-8')
        head = self.commit(linked, 'Clean topic')
        self.git('push', '-q', 'origin', 'HEAD', cwd=linked)
        self.assertEqual(self.git('rev-parse', 'refs/heads/topic', cwd=self.remote).stdout.strip(), head)

    def test_a_linked_worktree_push_rejects_a_private_string_removed_in_a_later_commit(self):
        linked = self.linked()
        (linked / 'notes.md').write_text('secret-host\n', encoding='utf-8')
        leaked = self.commit(linked, 'Add notes')
        (linked / 'notes.md').write_text('Cleaned notes\n', encoding='utf-8')
        self.commit(linked, 'Remove the private string')
        done = self.git('push', '-q', 'origin', 'HEAD', cwd=linked, check=False)
        self.assert_rejected(done, f'commit {leaked[:12]}: notes.md:1')

    def test_a_push_of_another_branch_checks_its_tip(self):
        self.git('checkout', '-qb', 'other')
        (self.public / 'notes.md').write_text('secret-host\n', encoding='utf-8')
        leaked = self.commit(self.public, 'Add notes')
        self.git('checkout', '-q', 'main')
        done = self.git('push', '-q', 'origin', 'other', check=False)
        self.assert_rejected(done, f'commit {leaked[:12]}: notes.md:1')

    def test_an_untrusted_remote_ref_does_not_exempt_a_pushed_commit(self):
        (self.public / 'notes.md').write_text('secret-host\n', encoding='utf-8')
        leaked = self.commit(self.public, 'Add notes')
        self.git('update-ref', 'refs/remotes/untrusted/already-held', leaked)
        (self.public / 'notes.md').write_text('Cleaned notes\n', encoding='utf-8')
        self.commit(self.public, 'Remove the private string')
        done = self.git('push', '-q', 'origin', 'main', check=False)
        self.assert_rejected(done, f'commit {leaked[:12]}: notes.md:1')

    def test_an_annotated_tags_own_message_is_checked(self):
        self.git('tag', '-a', 'release', '-m', 'Reach secret-host')
        done = self.git('push', '-q', 'origin', 'release', check=False)
        self.assert_rejected(done, 'its tag message')

    def test_tags_pointing_to_blobs_and_trees_are_checked(self):
        path = self.public / 'notes.md'
        path.write_text('secret-host\n', encoding='utf-8')
        blob = self.git('hash-object', '-w', str(path)).stdout.strip()
        self.git('add', 'notes.md')
        tree = self.git('write-tree').stdout.strip()
        for name, object_id, reason in (('blob', blob, 'its content'), ('tree', tree, 'notes.md:1')):
            with self.subTest(kind=name):
                self.git('tag', '-a', name, '-m', 'Clean tag message', object_id)
                done = self.git('push', '-q', 'origin', name, check=False)
                self.assert_rejected(done, reason)

    def test_clean_tags_pointing_to_blobs_and_trees_reach_the_remote(self):
        path = self.public / 'notes.md'
        path.write_text('Clean notes\n', encoding='utf-8')
        blob = self.git('hash-object', '-w', str(path)).stdout.strip()
        self.git('add', 'notes.md')
        tree = self.git('write-tree').stdout.strip()
        for name, object_id in (('blob', blob), ('tree', tree)):
            self.git('tag', name, object_id)
        self.git('push', '-q', 'origin', '--tags')
        for name, object_id in (('blob', blob), ('tree', tree)):
            self.assertEqual(self.git('rev-parse', f'refs/tags/{name}', cwd=self.remote).stdout.strip(), object_id)

    def test_all_refs_in_one_push_are_checked_before_any_reaches_the_remote(self):
        self.git('checkout', '-qb', 'other')
        leaked = self.commit(self.public, 'Reach secret-host')
        self.git('checkout', '-q', 'main')
        done = self.git('push', '-q', 'origin', 'main', 'other', check=False)
        self.assert_rejected(done, f'commit {leaked[:12]}: its message')

    def test_a_clean_push_ignores_uncommitted_files_that_it_does_not_send(self):
        (self.public / 'private-scratch.md').write_text('secret-host\n', encoding='utf-8')
        self.git('push', '-q', 'origin', 'main')
        self.assertEqual(self.git('rev-parse', 'refs/heads/main', cwd=self.remote).stdout,
                         self.git('rev-parse', 'HEAD').stdout)

    def test_a_new_branch_does_not_rescan_history_already_published_at_its_destination(self):
        (self.public / 'notes.md').write_text('secret-host\n', encoding='utf-8')
        self.commit(self.public, 'An already published fixture')
        (self.public / 'notes.md').write_text('Cleaned notes\n', encoding='utf-8')
        self.commit(self.public, 'Remove the private string')
        self.git('push', '-q', '--no-verify', 'origin', 'main')
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        linked = self.linked()
        (linked / 'more.md').write_text('A clean change\n', encoding='utf-8')
        head = self.commit(linked, 'Clean topic')
        self.git('push', '-q', 'origin', 'HEAD', cwd=linked)
        self.assertEqual(self.git('rev-parse', 'refs/heads/topic', cwd=self.remote).stdout.strip(), head)

    def test_missing_private_input_fails_closed(self):
        self.denylist.unlink()
        done = self.git('push', '-q', 'origin', 'main', check=False)
        self.assert_rejected(done, 'Leak check stopped:')

    def test_missing_runtime_fails_closed(self):
        (self.public / '.venv').rename(self.public / 'absent-venv')
        done = self.git('push', '-q', 'origin', 'main', check=False)
        self.assert_rejected(done, 'uv sync --locked')

    def test_deleting_a_ref_can_remove_already_published_private_history(self):
        self.git('push', '-q', 'origin', 'main')
        self.denylist.unlink()
        self.git('push', '-q', 'origin', ':main')
        self.assertEqual(self.git('for-each-ref', '--format=%(refname)', cwd=self.remote).stdout, '')


if __name__ == '__main__':
    unittest.main()
