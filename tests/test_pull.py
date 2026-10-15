from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pull import paired, run, update


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


class PairedPullTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.public_origin = self.repository('public-origin')
        self.initial = self.commit(self.public_origin, 'content', 'first')
        self.private_origin = self.repository('private-origin')
        self.commit(self.private_origin, 'release.toml', f'public_commit = "{self.initial}"\n')
        self.public = self.clone(self.public_origin, 'public')
        self.private = self.clone(self.private_origin, 'private')

    def repository(self, name):
        path = self.root / name
        path.mkdir()
        git(path, 'init', '-q', '-b', 'main')
        git(path, 'config', 'user.email', 'test@example.invalid')
        git(path, 'config', 'user.name', 'Test')
        return path

    def commit(self, root, name, text):
        (root / name).write_text(text, encoding='utf-8')
        git(root, 'add', name)
        git(root, 'commit', '-qm', 'fixture')
        return git(root, 'rev-parse', 'HEAD')

    def clone(self, source, name):
        path = self.root / name
        git(self.root, 'clone', '-q', str(source), str(path))
        return path

    def test_public_tip_waits_for_private_release_and_repeat_is_noop(self):
        candidate = self.commit(self.public_origin, 'content', 'second')
        update(self.public, self.private)
        self.assertEqual(git(self.public, 'rev-parse', 'HEAD'), self.initial)
        paired(self.public, self.private)
        private_commit = self.commit(self.private_origin, 'release.toml', f'public_commit = "{candidate}"\n')
        update(self.public, self.private)
        self.assertEqual(git(self.public, 'rev-parse', 'HEAD'), candidate)
        self.assertEqual(git(self.private, 'rev-parse', 'HEAD'), private_commit)
        before = (self.public / 'content').stat().st_mtime_ns
        update(self.public, self.private)
        self.assertEqual((self.public / 'content').stat().st_mtime_ns, before)

    def test_bad_pair_does_not_advance_either_checkout(self):
        self.commit(self.public_origin, 'content', 'second')
        self.commit(self.private_origin, 'release.toml', f'public_commit = "{"f" * 40}"\n')
        previous = git(self.private, 'rev-parse', 'HEAD')
        with self.assertRaises(RuntimeError):
            update(self.public, self.private)
        self.assertEqual(git(self.public, 'rev-parse', 'HEAD'), self.initial)
        self.assertEqual(git(self.private, 'rev-parse', 'HEAD'), previous)

    def test_dirty_private_checkout_preserves_public_and_local_bytes(self):
        candidate = self.commit(self.public_origin, 'content', 'second')
        self.commit(self.private_origin, 'release.toml', f'public_commit = "{candidate}"\n')
        (self.private / 'local-work').write_text('keep me')
        with self.assertRaisesRegex(RuntimeError, 'local changes'):
            update(self.public, self.private)
        self.assertEqual(git(self.public, 'rev-parse', 'HEAD'), self.initial)
        self.assertEqual((self.private / 'local-work').read_text(), 'keep me')

    def test_pair_check_refuses_uncommitted_code_even_without_update(self):
        (self.public / 'content').write_text('unreviewed local change')
        with self.assertRaisesRegex(RuntimeError, 'clean checkouts'):
            paired(self.public, self.private)

    def test_repeated_success_does_not_rewrite_release_receipt(self):
        candidate = self.commit(self.public_origin, 'sync.py', 'raise SystemExit(0)\n')
        self.commit(self.private_origin, 'release.toml', f'public_commit = "{candidate}"\n')
        args = SimpleNamespace(root=self.public, private=self.private, update=True, apply=True)
        with patch('pull.Path.home', return_value=self.root / 'home'):
            self.assertEqual(run(args), 0)
            receipt = self.root / 'home/.local/state/dotfiles/last-applied.json'
            before = receipt.read_bytes(), receipt.stat().st_mtime_ns
            self.assertEqual(run(args), 0)
            self.assertEqual((receipt.read_bytes(), receipt.stat().st_mtime_ns), before)


if __name__ == '__main__':
    unittest.main()
