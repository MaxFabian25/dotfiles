from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pull import install_tools, paired, run, unsigned, update


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


class PairedPullTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = self.signing_key('release')
        self.signers = self.root / 'allowed_signers'
        self.signers.write_text(f'release@example.invalid namespaces="git" {self.public_key(self.key)}', encoding='utf-8')
        self.public_origin = self.repository('public-origin')
        self.initial = self.commit(self.public_origin, 'content', 'first')
        self.private_origin = self.repository('private-origin')
        self.commit(self.private_origin, 'release.toml', f'public_commit = "{self.initial}"\n')
        self.public = self.clone(self.public_origin, 'public')
        self.private = self.clone(self.private_origin, 'private')

    def signing_key(self, name):
        path = self.root / name
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', name, '-f', str(path)], check=True)
        return path

    def public_key(self, key):
        return Path(f'{key}.pub').read_text(encoding='utf-8')

    def repository(self, name):
        path = self.root / name
        path.mkdir()
        git(path, 'init', '-q', '-b', 'main')
        git(path, 'config', 'user.email', 'test@example.invalid')
        git(path, 'config', 'user.name', 'Test')
        git(path, 'config', 'gpg.format', 'ssh')
        git(path, 'config', 'user.signingkey', str(self.key))
        git(path, 'config', 'commit.gpgsign', 'true')
        return path

    def commit(self, root, name, text, *settings):
        (root / name).write_text(text, encoding='utf-8')
        git(root, 'add', name)
        git(root, *settings, 'commit', '-qm', 'fixture')
        return git(root, 'rev-parse', 'HEAD')

    def clone(self, source, name):
        path = self.root / name
        # pull.py reads checkouts with core.autocrlf=false; a CRLF checkout would look locally edited on Windows.
        git(self.root, 'clone', '-q', '-c', 'core.autocrlf=false', str(source), str(path))
        return path

    def assert_untouched(self, private_head):
        self.assertEqual(git(self.public, 'rev-parse', 'HEAD'), self.initial)
        self.assertEqual(git(self.private, 'rev-parse', 'HEAD'), private_head)

    def test_public_tip_waits_for_private_release_and_repeat_is_noop(self):
        candidate = self.commit(self.public_origin, 'content', 'second')
        update(self.public, self.private, self.signers)
        self.assertEqual(git(self.public, 'rev-parse', 'HEAD'), self.initial)
        paired(self.public, self.private)
        private_commit = self.commit(self.private_origin, 'release.toml', f'public_commit = "{candidate}"\n')
        update(self.public, self.private, self.signers)
        self.assertEqual(git(self.public, 'rev-parse', 'HEAD'), candidate)
        self.assertEqual(git(self.private, 'rev-parse', 'HEAD'), private_commit)
        before = (self.public / 'content').stat().st_mtime_ns
        update(self.public, self.private, self.signers)
        self.assertEqual((self.public / 'content').stat().st_mtime_ns, before)

    def test_bad_pair_does_not_advance_either_checkout(self):
        self.commit(self.public_origin, 'content', 'second')
        self.commit(self.private_origin, 'release.toml', f'public_commit = "{"f" * 40}"\n')
        previous = git(self.private, 'rev-parse', 'HEAD')
        with self.assertRaises(RuntimeError):
            update(self.public, self.private, self.signers)
        self.assert_untouched(previous)

    def test_dirty_private_checkout_preserves_public_and_local_bytes(self):
        candidate = self.commit(self.public_origin, 'content', 'second')
        self.commit(self.private_origin, 'release.toml', f'public_commit = "{candidate}"\n')
        (self.private / 'local-work').write_text('keep me')
        with self.assertRaisesRegex(RuntimeError, 'local changes'):
            update(self.public, self.private, self.signers)
        self.assertEqual(git(self.public, 'rev-parse', 'HEAD'), self.initial)
        self.assertEqual((self.private / 'local-work').read_text(), 'keep me')

    def test_unsigned_public_commit_stops_before_either_checkout_moves(self):
        candidate = self.commit(self.public_origin, 'content', 'second', '-c', 'commit.gpgsign=false')
        self.commit(self.private_origin, 'release.toml', f'public_commit = "{candidate}"\n')
        previous = git(self.private, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(RuntimeError, f'commit {candidate} is not signed'):
            update(self.public, self.private, self.signers)
        self.assert_untouched(previous)

    def test_unsigned_private_release_stops_before_either_checkout_moves(self):
        candidate = self.commit(self.public_origin, 'content', 'second')
        self.commit(self.private_origin, 'release.toml', f'public_commit = "{candidate}"\n', '-c', 'commit.gpgsign=false')
        previous = git(self.private, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(RuntimeError, 'is not signed'):
            update(self.public, self.private, self.signers)
        self.assert_untouched(previous)

    def test_valid_signature_from_unpinned_key_is_rejected(self):
        other = self.signing_key('other')
        candidate = self.commit(self.public_origin, 'content', 'second', '-c', f'user.signingkey={other}')
        self.commit(self.private_origin, 'release.toml', f'public_commit = "{candidate}"\n')
        previous = git(self.private, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(RuntimeError, f'commit {candidate} is not signed'):
            update(self.public, self.private, self.signers)
        self.assert_untouched(previous)

    def test_missing_signer_pin_stops_pull_without_fetching(self):
        candidate = self.commit(self.public_origin, 'content', 'second')
        self.commit(self.private_origin, 'release.toml', f'public_commit = "{candidate}"\n')
        previous = git(self.private, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(RuntimeError, 'pin trusted release signers'):
            update(self.public, self.private, self.root / 'missing')
        self.assert_untouched(previous)
        self.assertEqual(git(self.public, 'rev-parse', 'origin/main'), self.initial)

    def test_pair_check_refuses_uncommitted_code_even_without_update(self):
        (self.public / 'content').write_text('unreviewed local change')
        with self.assertRaisesRegex(RuntimeError, 'clean checkouts'):
            paired(self.public, self.private)

    def test_repeated_success_does_not_rewrite_release_receipt(self):
        candidate = self.commit(self.public_origin, 'sync.py', 'raise SystemExit(0)\n')
        self.commit(self.private_origin, 'release.toml', f'public_commit = "{candidate}"\n')
        args = SimpleNamespace(root=self.public, private=self.private, update=True, apply=True)
        pinned = self.root / 'home/.config/dotfiles/allowed_signers'
        pinned.parent.mkdir(parents=True)
        shutil.copyfile(self.signers, pinned)
        with patch('pull.Path.home', return_value=self.root / 'home'):
            self.assertEqual(run(args), 0)
            receipt = self.root / 'home/.local/state/dotfiles/last-applied.json'
            before = receipt.read_bytes(), receipt.stat().st_mtime_ns
            self.assertEqual(run(args), 0)
            self.assertEqual((receipt.read_bytes(), receipt.stat().st_mtime_ns), before)

    def test_tools_install_after_an_applied_sync_that_nothing_blocked(self):
        pinned = self.root / 'home/.config/dotfiles/allowed_signers'
        pinned.parent.mkdir(parents=True)
        shutil.copyfile(self.signers, pinned)
        receipt = self.root / 'home/.local/state/dotfiles/last-applied.json'
        calls = []
        for code, apply in ((0, False), (1, False), (2, True), (3, True), (1, True), (0, True)):
            candidate = self.commit(self.public_origin, 'sync.py', f'raise SystemExit({code})\n')
            self.commit(self.private_origin, 'release.toml', f'public_commit = "{candidate}"\n')
            args = SimpleNamespace(root=self.public, private=self.private, update=True, apply=apply)
            with patch('pull.Path.home', return_value=self.root / 'home'), \
                    patch('pull.install_tools', lambda home, state: calls.append((code, apply))):
                self.assertEqual(run(args), code)
            # Changes that remain after applying leave the pair unrecorded, so only the last pull writes a receipt.
            self.assertEqual(receipt.exists(), (code, apply) == (0, True))
        self.assertEqual(calls, [(1, True), (0, True)])


class WindowsVerifierTest(unittest.TestCase):
    def test_verification_uses_git_bundled_openssh_with_the_pinned_signers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / 'Git with spaces/usr/bin/ssh-keygen.exe'
            executable.parent.mkdir(parents=True)
            executable.touch()
            exec_path = root / 'Git with spaces/mingw64/libexec/git-core'
            allowed = root / 'allowed_signers'
            for status, expected in (('G', None), ('U', 'commit')):
                with self.subTest(status=status), patch('pull.WINDOWS', True), patch('pull.git',
                        side_effect=[str(exec_path), 'commit', 'tree value\ngpgsig -----BEGIN SSH SIGNATURE-----', status]) as command:
                    self.assertEqual(unsigned(root, 'old', 'new', allowed), expected)
                    self.assertEqual(command.call_args.args, (root, '-c',
                        f'gpg.ssh.program={executable.as_posix()}', '-c',
                        f'gpg.ssh.allowedSignersFile={allowed}', 'show', '-s', '--format=%G?', 'commit'))

    def test_missing_windows_verifier_stops_before_checking_commits(self):
        with tempfile.TemporaryDirectory() as temporary, patch('pull.WINDOWS', True), \
                patch('pull.git', return_value=str(Path(temporary) / 'Git/mingw64/libexec/git-core')) as command:
            with self.assertRaisesRegex(RuntimeError, 'SSH signature verifier is missing'):
                unsigned(Path(temporary), 'old', 'new', Path(temporary) / 'allowed_signers')
            self.assertEqual(command.call_count, 1)


class InstallToolsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.state = self.home / '.local/state/dotfiles'
        self.state.mkdir(parents=True)
        self.config = self.home / '.config/mise/config.toml'
        self.config.parent.mkdir(parents=True)
        self.config.write_text('[tools]\nnpm = "12.2.0"\n', encoding='utf-8')
        self.calls = []

    def install(self, code=0, mise='/opt/mise'):
        def fake_run(command, **kwargs):
            self.calls.append(command)
            return subprocess.CompletedProcess(command, code)
        with patch('pull.shutil.which', return_value=mise), patch('pull.subprocess.run', fake_run):
            install_tools(self.home, self.state)

    def test_installs_once_per_config_and_again_after_a_pin_changes(self):
        self.install()
        self.install()
        self.assertEqual(self.calls, [['/opt/mise', '--cd', str(self.home), 'install', '--yes']])
        self.config.write_text('[tools]\nnpm = "12.2.1"\n', encoding='utf-8')
        self.install()
        self.assertEqual(len(self.calls), 2)

    def test_failed_install_raises_and_the_next_pull_retries(self):
        with self.assertRaisesRegex(RuntimeError, 'mise install exited 1'):
            self.install(code=1)
        self.install()
        self.assertEqual(len(self.calls), 2)

    def test_missing_mise_is_reported(self):
        with self.assertRaisesRegex(RuntimeError, 'mise is not on PATH'):
            self.install(mise=None)

    def test_device_without_a_mise_config_installs_nothing(self):
        self.config.unlink()
        self.install()
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
