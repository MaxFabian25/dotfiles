import argparse
from contextlib import redirect_stderr
import contextlib
import io
import json
from pathlib import Path
import os
import shutil
import sys
from types import SimpleNamespace
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homes import child_stderr_dropped, isolate_git, recording_sync
import portable
import pull
from pull import git as pull_git, install_tools, paired, pull_lock, run, unsigned, update


def setUpModule():
    isolate_git()


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

    def test_a_pull_tries_each_fetch_that_fails_a_second_time(self):
        candidate = self.commit(self.public_origin, 'content', 'second')
        private_commit = self.commit(self.private_origin, 'release.toml', f'public_commit = "{candidate}"\n')
        fetches = []

        def every_first_fetch_fails(root, *arguments):
            if arguments[0] == 'fetch':
                fetches.append(root)
                if len(fetches) % 2:  # as when a release finish in a worktree of this checkout fetches at the same time
                    raise RuntimeError(f"{root.name}: git fetch failed: error: cannot lock ref 'refs/remotes/origin/main'")
            return pull_git(root, *arguments)

        with patch('pull.git', every_first_fetch_fails):
            update(self.public, self.private, self.signers)
        self.assertEqual(fetches, [self.public, self.public, self.private, self.private])
        self.assertEqual(git(self.public, 'rev-parse', 'HEAD'), candidate)
        self.assertEqual(git(self.private, 'rev-parse', 'HEAD'), private_commit)

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
        candidate = self.commit(self.public_origin, 'sync.py', recording_sync(0))
        self.commit(self.private_origin, 'release.toml', f'public_commit = "{candidate}"\n')
        args = SimpleNamespace(root=self.public, private=self.private, update=True, apply=True, notes=False, wait=0)
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
            candidate = self.commit(self.public_origin, 'sync.py', recording_sync(code))
            self.commit(self.private_origin, 'release.toml', f'public_commit = "{candidate}"\n')
            args = SimpleNamespace(root=self.public, private=self.private, update=True, apply=apply, notes=False, wait=0)
            with patch('pull.Path.home', return_value=self.root / 'home'), redirect_stderr(io.StringIO()) as errors, \
                    patch('pull.install_tools', lambda home, state: calls.append((code, apply))):
                self.assertEqual(run(args), code)
            self.assertEqual('managed changes remain after applying' in errors.getvalue(), (code, apply) == (1, True))
            # Changes that remain after applying leave the pair unrecorded, so only the last pull writes a receipt.
            self.assertEqual(receipt.exists(), (code, apply) == (0, True))
        self.assertEqual(calls, [(1, True), (0, True)])

    def test_a_sync_that_does_not_end_by_recording_its_status_stops_the_pull_and_changes_nothing(self):
        pinned = self.root / 'home/.config/dotfiles/allowed_signers'
        pinned.parent.mkdir(parents=True)
        shutil.copyfile(self.signers, pinned)
        receipt = self.root / 'home/.local/state/dotfiles/last-applied.json'
        tool, source = self.root / 'home/.local/bin/tool', 'config/agent-env/home/.local/bin/tool'
        (self.private_origin / source).parent.mkdir(parents=True)
        argv = ['pull.py', '--root', str(self.public), '--private', str(self.private), '--update', '--apply',
                '--wait', '0']
        installs = []

        def release(sync, number):
            candidate = self.commit(self.public_origin, 'sync.py', sync)
            self.commit(self.private_origin, source, f'tool {number}\n')
            self.commit(self.private_origin, 'release.toml', f'public_commit = "{candidate}"\n')
            with patch('pull.Path.home', return_value=self.root / 'home'), patch.object(pull.sys, 'platform', 'linux'), \
                    patch.object(sys, 'argv', argv), \
                    patch('pull.install_tools', lambda home, state: installs.append(number)), \
                    contextlib.redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors, \
                    child_stderr_dropped():
                return pull.main(), errors.getvalue()

        self.assertEqual(release(recording_sync(0), 0), (0, ''))
        before = receipt.read_bytes()
        crashed = 'sync.py exited 1 without recording how it ended, as after a crash or a kill; '
        for number, (sync, stop) in enumerate((
                ("raise KeyError('a stand-in sync that crashes')\n", crashed),
                ('def a stand-in sync with a syntax error\n', crashed),
                ('import a_module_no_stand_in_sync_can_find\n', crashed),
                ('raise SystemExit(1)\n', crashed),
                ('raise SystemExit(0)\n', 'sync.py exited 0 without recording how it ended; '),
                (recording_sync(1, then='raise SystemExit(1)\n'), 'sync.py recorded status 1 and then exited 1; '),
                (recording_sync(0, then="ending.write_text('{')\n"),
                 'sync.py exited 0, and its record of how it ended cannot be read: ')), 1):
            with self.subTest(sync=sync.splitlines()[-1]):
                status, said = release(sync, number)
                self.assertEqual(status, 3)
                self.assertTrue(said.startswith(f'Dotfiles pull stopped: {stop}'), said)
                self.assertNotIn('managed changes remain', said)
                self.assertEqual(receipt.read_bytes(), before)
                self.assertEqual(tool.read_text(encoding='utf-8'), 'tool 0\n')
        self.assertEqual(installs, [0])

    def test_the_sync_gets_the_pulls_mode_notes_private_checkout_and_time_limit(self):
        kept = self.root / 'arguments.json'
        keep_arguments = f'import json\npathlib.Path({str(kept)!r}).write_text(json.dumps(sys.argv[1:]))\n'
        candidate = self.commit(self.public_origin, 'sync.py', recording_sync(0, then=keep_arguments))
        self.commit(self.private_origin, 'release.toml', f'public_commit = "{candidate}"\n')
        pinned = self.root / 'home/.config/dotfiles/allowed_signers'
        pinned.parent.mkdir(parents=True)
        shutil.copyfile(self.signers, pinned)
        limits = []
        run_sync = pull.run_sync

        def noting_the_limit(*args, timeout, **options):
            limits.append(timeout)
            return run_sync(*args, timeout=timeout, **options)

        for apply, notes in ((False, False), (True, True)):
            with self.subTest(apply=apply, notes=notes):
                args = SimpleNamespace(root=self.public, private=self.private, update=True, apply=apply, notes=notes,
                                       wait=0)
                with patch('pull.Path.home', return_value=self.root / 'home'), patch('pull.install_tools'), \
                        patch('pull.run_sync', noting_the_limit):
                    self.assertEqual(run(args), 0)
                arguments = json.loads(kept.read_bytes())
                self.assertEqual([flag in arguments for flag in ('--apply', '--check', '--notes')],
                                 [apply, not apply, notes])
                self.assertEqual(arguments[arguments.index('--private') + 1], str(self.private.resolve() / 'setup'))
        self.assertEqual(limits, [1200, 1200])


class PullLockTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.lock = Path(temp.name) / 'pull.lock'
        poll = patch.object(pull, 'LOCK_POLL', 0.01)
        poll.start()
        self.addCleanup(poll.stop)

    def test_a_pull_stops_by_naming_the_pull_that_still_holds_the_lock(self):
        errors = io.StringIO()
        with portable.exclusive_lock(self.lock, record=True), redirect_stderr(errors):
            with self.assertRaises(RuntimeError) as stop:
                pull_lock(self.lock, 0.05)
        self.assertIn(f'PID {os.getpid()}', str(stop.exception))
        self.assertIn('another pull still runs on this machine after 0.05 seconds', str(stop.exception))
        self.assertEqual(errors.getvalue().count('Waiting for another pull'), 1)

    def test_a_stop_without_a_holder_note_names_the_lock(self):
        with portable.exclusive_lock(self.lock), redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError) as stop:
                pull_lock(self.lock, 0)
        self.assertIn(f'the run that holds {self.lock}', str(stop.exception))

    def test_a_pull_waits_for_the_pull_ahead_of_it_and_then_holds_the_lock(self):
        ahead = contextlib.ExitStack()
        ahead.enter_context(portable.exclusive_lock(self.lock, record=True))
        errors = io.StringIO()
        with patch.object(pull.time, 'sleep', side_effect=lambda delay: ahead.close()) as slept, \
                redirect_stderr(errors), pull_lock(self.lock, 30):
            with self.assertRaises(BlockingIOError):
                with portable.exclusive_lock(self.lock):
                    pass
        self.assertEqual(slept.call_count, 1)
        self.assertIn(f'Waiting for another pull: PID {os.getpid()}', errors.getvalue())
        with portable.exclusive_lock(self.lock):
            pass

    def test_run_waits_on_the_lock_for_the_seconds_it_was_given(self):
        home = Path(self.lock).parent / 'home'
        args = SimpleNamespace(root=home, private=None, update=False, apply=False, notes=False, wait=0.05)
        with patch('pull.Path.home', return_value=home), redirect_stderr(io.StringIO()), \
                portable.exclusive_lock(home / '.local/state/dotfiles/pull.lock', record=True):
            with self.assertRaises(RuntimeError) as stop:
                run(args)
        self.assertIn('another pull still runs on this machine after 0.05 seconds', str(stop.exception))

    def test_a_pull_waits_by_default_and_refuses_a_wait_that_is_no_number_of_seconds(self):
        seen = []
        with patch.object(pull, 'run', side_effect=lambda args: seen.append(args.wait) or 0):
            with patch.object(sys, 'argv', ['pull.py']):
                self.assertEqual(pull.main(), 0)
            with patch.object(sys, 'argv', ['pull.py', '--wait', '0.5']):
                self.assertEqual(pull.main(), 0)
            for refused in ('nan', '-1'):
                with patch.object(sys, 'argv', ['pull.py', '--wait', refused]), redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit):
                    pull.main()
        self.assertEqual(seen, [pull.LOCK_WAIT, 0.5])
        self.assertGreater(pull.LOCK_WAIT, 0)
        with patch.object(pull, 'run', side_effect=RuntimeError('another pull still runs')), \
                patch.object(sys, 'argv', ['pull.py']), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(pull.main(), 3)
        self.assertEqual(errors.getvalue(), 'Dotfiles pull stopped: another pull still runs\n')

    def test_the_wait_is_a_finite_number_of_seconds_from_zero(self):
        self.assertEqual(pull.seconds('0'), 0)
        for text in ('nan', 'inf', '-1'):
            with self.assertRaises(argparse.ArgumentTypeError):
                pull.seconds(text)

    def test_a_free_lock_is_taken_without_a_word(self):
        errors = io.StringIO()
        with redirect_stderr(errors), pull_lock(self.lock, 0):
            self.assertEqual(portable.lock_holder(self.lock)['pid'], os.getpid())
        self.assertEqual(errors.getvalue(), '')


class PullEnvironmentTest(unittest.TestCase):
    def test_git_helpers_receive_windows_executable_extensions_when_parent_has_none(self):
        with tempfile.TemporaryDirectory() as temporary, patch('portable.WINDOWS', True), \
                patch.dict(os.environ, {'PATHEXT': ''}):
            extensions = pull_git(Path(temporary), '-c',
                'alias.dotfiles-pathext=!printf "%s" "$PATHEXT"', 'dotfiles-pathext')
            self.assertIn('.EXE', extensions.split(';'))
            self.assertEqual(os.environ['PATHEXT'], '')

    def test_a_fetch_that_lost_origins_ref_to_another_fetch_is_tried_once_more(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pull_git(root, 'init', '-q', '--bare', '-b', 'main', str(root / 'origin'))
            pull_git(root, 'clone', '-q', str(root / 'origin'), str(root / 'seed'))
            seed, clone = root / 'seed', root / 'clone'

            def push(subject):
                pull_git(seed, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-q',
                         '--allow-empty', '-m', subject)
                pull_git(seed, 'push', '-q', 'origin', 'HEAD:main')
                return pull_git(seed, 'rev-parse', 'HEAD')

            push('first')
            pull_git(root, 'clone', '-q', str(root / 'origin'), str(clone))
            # What a fetch meets while another fetch in a worktree of the same repository moves origin/main.
            taken = clone / '.git/refs/remotes/origin/main.lock'
            tries = []

            def git_beside_another_fetch(tree, *arguments):
                tries.append(arguments)
                try:
                    return pull_git(tree, *arguments)
                finally:
                    taken.unlink(missing_ok=True)  # the other fetch ends

            second = push('second')
            taken.write_text('', encoding='utf-8')
            with patch('pull.git', git_beside_another_fetch):
                pull.fetch(clone)
            self.assertEqual(len(tries), 2)
            self.assertEqual(pull_git(clone, 'rev-parse', 'origin/main'), second)
            # A fetch that fails twice stops with git's words, and one that ran out of time is not tried again.
            push('third')
            taken.write_text('', encoding='utf-8')
            with patch('pull.git', lambda tree, *arguments: (tries.append(arguments), pull_git(tree, *arguments))[1]), \
                    self.assertRaisesRegex(RuntimeError, 'clone: git fetch failed: .*main.lock'):
                pull.fetch(clone)
            self.assertEqual(len(tries), 4)
            self.assertEqual(pull_git(clone, 'rev-parse', 'origin/main'), second)
            taken.unlink()

            def out_of_time(tree, *arguments):
                tries.append(arguments)
                raise RuntimeError('clone: git fetch did not finish within 180 seconds')

            with patch('pull.git', out_of_time), self.assertRaisesRegex(RuntimeError, 'did not finish within'):
                pull.fetch(clone)
            self.assertEqual(len(tries), 5)

    def test_git_stops_child_processes_at_its_time_limit(self):
        # A loaded machine can use up a short limit before Git reaches the alias, so a run with no marker proves nothing.
        # Git for Windows' shell gives $! as its own process ID, and /proc/$!/winpid as the one Windows knows.
        pid = 'cat /proc/$!/winpid' if portable.WINDOWS else 'echo $!'
        for limit in (1, 5, 30):
            with tempfile.TemporaryDirectory() as temporary, patch('pull.GIT_TIMEOUT', limit):
                marker = Path(temporary) / 'grandchild'
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, f'did not finish within {limit} seconds'):
                    pull_git(Path(temporary), '-c',
                             f'alias.dotfiles-hang=!sleep 60 & {pid} > "{marker}"; wait', 'dotfiles-hang')
                self.assertLess(time.monotonic() - started, limit + 14)
                written = marker.read_text().strip() if marker.exists() else ''
                if not written:
                    continue
                grandchild = int(written)
                for _ in range(50):
                    try:
                        if not portable.WINDOWS:
                            os.kill(grandchild, 0)
                        elif not portable.process_running(grandchild):  # os.kill(pid, 0) is no probe there
                            return
                    except ProcessLookupError:
                        return
                    time.sleep(0.1)
                self.fail('The child process of git outlived the time limit')
        self.fail('Git never reached its alias within the longest limit')


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
