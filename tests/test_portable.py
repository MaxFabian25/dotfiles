import contextlib
import ctypes
from datetime import datetime, timezone
import importlib.util
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

ENGINE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('portable', ENGINE / 'portable.py')
portable = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portable)

STARTS_A_CHILD = ('import os, pathlib, subprocess, sys, time\n'
                  'child = subprocess.Popen([sys.executable, "-c", "import signal, time; '
                  'signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])\n'
                  "pathlib.Path(sys.argv[1] + '.part').write_text(f'{os.getpid()} {child.pid}')\n"
                  "pathlib.Path(sys.argv[1] + '.part').replace(sys.argv[1])\n"
                  'time.sleep(60)\n')
KILL = getattr(signal, 'SIGKILL', signal.SIGTERM)  # os.kill ends a process on Windows with any signal


def started(test, path):
    ending = time.monotonic() + 60
    while not path.exists():
        if time.monotonic() > ending:
            raise AssertionError(f'{path} was never written')
        time.sleep(0.05)
    pids = [int(pid) for pid in path.read_text().split()]
    test.addCleanup(end, pids)
    return pids


def end(pids):
    for pid in pids:
        if portable.process_running(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, KILL)


def gone(pid):
    ending = time.monotonic() + 10
    while portable.process_running(pid):
        if time.monotonic() > ending:
            return False
        time.sleep(0.05)
    return True


def archive(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as bundle:
        for name, value in entries:
            info = tarfile.TarInfo(name)
            if isinstance(value, tuple):
                info.type, info.linkname = tarfile.SYMTYPE, value[1]
                bundle.addfile(info)
            elif value is None:
                info.type = tarfile.DIRTYPE
                bundle.addfile(info)
            else:
                info.size = len(value)
                bundle.addfile(info, io.BytesIO(value))
    return buffer.getvalue()


class PortableTest(unittest.TestCase):
    def test_windows_cli_environment_recovers_missing_executable_extensions(self):
        for inherited in ({}, {'PATHEXT': ''}):
            with self.subTest(inherited=inherited), patch.object(portable, 'WINDOWS', True):
                before = dict(inherited)
                environment = portable.native_cli_environment(inherited)
                self.assertIn('.EXE', environment['PATHEXT'].split(';'))
                self.assertEqual(inherited, before)

    def test_cli_environment_preserves_custom_extensions_and_posix_environment(self):
        with patch.object(portable, 'WINDOWS', True):
            self.assertEqual(portable.native_cli_environment({'PATHEXT': '.EXE;.PY'})['PATHEXT'], '.EXE;.PY')
        with patch.object(portable, 'WINDOWS', False):
            self.assertNotIn('PATHEXT', portable.native_cli_environment({}))

    def test_homes_honour_the_apps_variables_only_for_the_real_home(self):
        with patch.dict(os.environ, {'CODEX_HOME': '/elsewhere/codex', 'CLAUDE_CONFIG_DIR': '/elsewhere/claude'}):
            self.assertEqual(portable.codex_home(), Path('/elsewhere/codex'))
            self.assertEqual(portable.claude_home(Path.home()), Path('/elsewhere/claude'))
            self.assertEqual(portable.codex_home(Path('/sandbox')), Path('/sandbox/.codex'))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(portable.claude_home(Path('/sandbox')), Path('/sandbox/.claude'))

    def test_cli_override_must_name_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory) / 'codex'
            cli.write_text('')
            with patch.dict(os.environ, {'CODEX_CLI': str(cli)}):
                self.assertEqual(portable.codex_cli(), str(cli))
            with patch.dict(os.environ, {'CODEX_CLI': str(cli) + '-missing'}):
                with self.assertRaises(FileNotFoundError):
                    portable.codex_cli()

    def test_windows_discovery_reads_paths_despite_bad_stderr_and_falls_back_when_it_cannot_read_them(self):
        with tempfile.TemporaryDirectory() as directory:
            app, fallback = Path(directory) / 'app codex.exe', Path(directory) / 'path codex.exe'
            app.touch()
            fallback.touch()
            run = portable.subprocess.run
            for stdout, stderr, status, expected in (
                    ((str(app) + '\r\n').encode(), b'warning: caf\xe9\r\n', 0, app),
                    (b'\xff\r\n', b'', 0, fallback),
                    (str(app).encode(), b'error: caf\xe9\r\n', 1, fallback),
                    (b'', b'', 0, fallback),
                    ((str(app) + '-missing').encode(), b'', 0, fallback)):
                with self.subTest(stdout=stdout, status=status):
                    def discover(command, **options):
                        code = ('import sys; '
                                f'sys.stdout.buffer.write({stdout!r}); sys.stderr.buffer.write({stderr!r}); '
                                f'sys.exit({status})')
                        return run([sys.executable, '-c', code], **options)
                    with patch.object(portable, 'WINDOWS', True), patch.dict(os.environ, {'CODEX_CLI': ''}), \
                            patch.object(portable.shutil, 'which', return_value=str(fallback)), \
                            patch.object(portable.subprocess, 'run', side_effect=discover):
                        self.assertEqual(portable.codex_cli(), str(expected))

    def test_windows_discovery_preserves_explicit_override_and_process_failure_fallbacks(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory) / 'codex.exe'
            cli.touch()
            with patch.object(portable, 'WINDOWS', True), patch.dict(os.environ, {'CODEX_CLI': str(cli)}), \
                    patch.object(portable.subprocess, 'run', side_effect=AssertionError('discovery ran despite override')):
                self.assertEqual(portable.codex_cli(), str(cli))
                with patch.dict(os.environ, {'CODEX_CLI': str(cli) + '-missing'}), self.assertRaises(FileNotFoundError):
                    portable.codex_cli()
            for error in (FileNotFoundError('PowerShell is absent'), subprocess.TimeoutExpired('powershell.exe', 15)):
                with self.subTest(error=error), patch.object(portable, 'WINDOWS', True), \
                        patch.dict(os.environ, {'CODEX_CLI': ''}), \
                        patch.object(portable.shutil, 'which', return_value=str(cli)), \
                        patch.object(portable.subprocess, 'run', side_effect=error):
                    self.assertEqual(portable.codex_cli(), str(cli))
                    with patch.object(portable.shutil, 'which', return_value=None), self.assertRaises(FileNotFoundError):
                        portable.codex_cli()

    def test_text_round_trips_bytes_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'note.md'
            text = 'line one\r\nline two — “quoted”\n'
            portable.write_utf8(path, text)
            self.assertEqual(path.read_bytes(), text.encode('utf-8'))
            self.assertEqual(portable.read_utf8(path), text)

    def test_a_replace_a_windows_lock_refuses_is_retried_and_elsewhere_fails_at_once(self):
        refused = PermissionError(13, 'Access is denied')
        with patch.object(portable, 'RETRIES_REFUSALS', False), patch.object(portable.os, 'replace', side_effect=refused), \
                patch.object(portable.time, 'sleep') as sleep:
            with self.assertRaises(PermissionError) as raised:
                portable.replace('a', 'b')
        self.assertIs(type(raised.exception), PermissionError)
        self.assertIs(raised.exception, refused)
        sleep.assert_not_called()
        with patch.object(portable, 'RETRIES_REFUSALS', True), \
                patch.object(portable.os, 'replace', side_effect=[refused, None]) as move, \
                patch.object(portable.time, 'sleep') as sleep:
            portable.replace('a', 'b')
        self.assertEqual((move.call_count, sleep.call_count), (2, 1))

    def test_a_move_windows_never_frees_raises_its_last_refusal_as_still_held_after_the_attempts_it_was_given(self):
        refusals = [PermissionError(13, 'Access is denied', f'a{attempt}', None, 'b') for attempt in range(3)]
        last = refusals[-1]
        last.winerror = 5
        with patch.object(portable, 'RETRIES_REFUSALS', True), \
                patch.object(portable.os, 'replace', side_effect=refusals) as move, \
                patch.object(portable.time, 'sleep') as sleep:
            with self.assertRaises(portable.StillHeld) as raised:
                portable.replace('a', 'b', attempts=3, delay=7)
        held = raised.exception
        self.assertEqual([str(held), held.errno, held.strerror, held.filename, held.filename2, held.winerror],
                         [str(last), 13, 'Access is denied', 'a2', 'b', 5])
        self.assertIsInstance(held, PermissionError)
        self.assertIs(held.__cause__, last)
        self.assertEqual((held.attempts, held.waited), (3, 14))
        self.assertEqual((move.call_count, sleep.call_args_list), (3, [unittest.mock.call(7)] * 2))

    def test_a_refusal_still_held_reads_as_the_refusal_whatever_fields_it_has(self):
        fields = ('errno', 'strerror', 'filename', 'filename2', 'winerror')
        for refusal in (PermissionError(13, 'Access is denied', 'a'), PermissionError(13, 'Access is denied'),
                        PermissionError('the move was refused')):
            with self.subTest(refusal=str(refusal)), patch.object(portable, 'RETRIES_REFUSALS', True), \
                    patch.object(portable.time, 'sleep'):
                with self.assertRaises(portable.StillHeld) as raised:
                    portable.while_held(unittest.mock.Mock(side_effect=refusal), attempts=2)
                self.assertEqual([str(raised.exception)] + [getattr(raised.exception, name, None) for name in fields],
                                 [str(refusal)] + [getattr(refusal, name, None) for name in fields])

    def test_only_a_refusal_is_retried_and_the_check_before_each_retry_can_stop_it(self):
        missing, refused, changed = FileNotFoundError(2, 'gone'), PermissionError(13, 'Access is denied'), RuntimeError('changed')
        with patch.object(portable, 'RETRIES_REFUSALS', True), patch.object(portable.time, 'sleep') as sleep:
            move = unittest.mock.Mock(side_effect=missing)
            with self.assertRaises(FileNotFoundError):
                portable.while_held(move)
            self.assertEqual((move.call_count, sleep.call_count), (1, 0))
            steps = unittest.mock.Mock()
            steps.attach_mock(sleep, 'wait')
            steps.move.side_effect = [refused, refused, 'moved']
            self.assertEqual(portable.while_held(steps.move, before_retry=steps.check), 'moved')
            self.assertEqual([step[0] for step in steps.mock_calls],
                             ['move', 'wait', 'check', 'move', 'wait', 'check', 'move'])
            # A check that cannot read what it checks is refused too, and that is not the move's refusal to wait out.
            for stop in (changed, PermissionError(13, 'the check could not read the tree')):
                move, check = unittest.mock.Mock(side_effect=refused), unittest.mock.Mock(side_effect=stop)
                with self.assertRaises(type(stop)) as raised:
                    portable.while_held(move, before_retry=check)
                self.assertIs(raised.exception, stop)
                self.assertEqual((move.call_count, check.call_count), (1, 1))

    def test_a_command_gives_its_stdout_as_bytes_or_as_text_whatever_its_stderr_holds(self):
        command = [sys.executable, '-c', 'import sys; sys.stdout.buffer.write(b"one\\r\\ntwo\\rthree\\n"); '
                                         'sys.stderr.buffer.write(b"warning: caf\\xe9\\r\\n")']
        self.assertEqual(portable.checked_output(command), b'one\r\ntwo\rthree\n')
        self.assertEqual(portable.checked_output(command, text=True), 'one\ntwo\nthree\n')
        for options in ({'encoding': 'utf-8'}, {'errors': 'replace'}, {'universal_newlines': True}):
            with self.subTest(options=options), self.assertRaises(TypeError):
                portable.checked_output(command, **options)

    def test_text_stdout_that_is_not_utf8_raises_a_subprocess_error_naming_the_command(self):
        import subprocess
        command = [sys.executable, '-c', 'import sys; sys.stdout.buffer.write(b"caf\\xe9\\n")']
        self.assertEqual(portable.checked_output(command), b'caf\xe9\n')
        with self.assertRaises(subprocess.SubprocessError) as unread:
            portable.checked_output(command, text=True)
        self.assertIn(subprocess.list2cmdline(command), str(unread.exception))
        self.assertIn('not UTF-8', str(unread.exception))

    def test_a_failed_command_raises_a_called_process_error_whose_text_starts_with_its_last_words(self):
        import subprocess
        tmpfs = (b'fatal: not a git repository (or any parent up to mount point /dev)\n'
                 b'Stopping at filesystem boundary (GIT_DISCOVERY_ACROSS_FILESYSTEM not set).\n')
        dubious = (b"fatal: detected dubious ownership in repository at '/r'\n"
                   b'To add an exception for this directory, call:\n\n\tgit config --global --add safe.directory /r\n')
        for stderr, options, said in (
                (b'warning: first\nerror: the last words\n\n', {}, 'error: the last words'),
                (b'error: \xff\n', {}, 'error: \N{REPLACEMENT CHARACTER}'),
                (b'hint: run\n\tgit config --global --add safe.directory /x\n', {},
                 'git config --global --add safe.directory /x'),
                (b'error: as text\n', {'text': True}, 'error: as text'),
                (b'50%\rerror: caf\xe9 not found\r\n', {'text': True}, 'error: caf\N{REPLACEMENT CHARACTER} not found'),
                (tmpfs, {}, 'fatal: not a git repository (or any parent up to mount point /dev) '
                            'Stopping at filesystem boundary (GIT_DISCOVERY_ACROSS_FILESYSTEM not set).'),
                (dubious, {}, "fatal: detected dubious ownership in repository at '/r' ... "
                              'git config --global --add safe.directory /r'),
                (b'Error: no config\n\nCaused by:\n    No such file (os error 2)\n', {},
                 'Error: no config ... No such file (os error 2)'),
                (b'Traceback (most recent call last):\n  File "x", line 1\nValueError: bad\n', {}, 'ValueError: bad'),
                (b'', {}, None)):
            command = [sys.executable, '-c', 'import sys; sys.stdout.buffer.write(b"partial \\xff\\n"); '
                                             f'sys.stderr.buffer.write({stderr!r}); sys.exit(2)']
            with self.subTest(stderr=stderr, **options):
                with self.assertRaises(subprocess.CalledProcessError) as failed:
                    portable.checked_output(command, **options)
                self.assertEqual((failed.exception.returncode, failed.exception.cmd), (2, command))
                text = str(failed.exception)
                if said:
                    self.assertTrue(text.startswith(f'{said} (exit 2 from '), text)
                else:
                    self.assertEqual(text, str(subprocess.CalledProcessError(2, command)))

    def test_a_failed_command_whose_stderr_windows_could_not_decode_raises_with_pythons_text(self):
        import subprocess
        command = ['git', 'rev-parse']
        lost = subprocess.CompletedProcess(command, 128, b'', None)
        with patch.object(portable.subprocess, 'run', return_value=lost), \
                self.assertRaises(subprocess.CalledProcessError) as failed:
            portable.checked_output(command, text=True)
        self.assertEqual(str(failed.exception), str(subprocess.CalledProcessError(128, command)))

    @unittest.skipIf(sys.platform == 'win32', 'a Windows process ends with an exit code, not a signal')
    def test_a_failed_command_that_a_signal_ended_names_the_signal(self):
        import subprocess
        command = [sys.executable, '-c', 'import os, signal, sys; sys.stderr.write("killed\\n"); sys.stderr.flush(); '
                                         'os.kill(os.getpid(), signal.SIGKILL)']
        with self.assertRaises(subprocess.CalledProcessError) as failed:
            portable.checked_output(command)
        self.assertTrue(str(failed.exception).startswith('killed (signal 9 from '), str(failed.exception))

    def test_posix_keys_use_forward_slashes(self):
        self.assertEqual(portable.posix(Path('skills') / 'a' / 'SKILL.md'), 'skills/a/SKILL.md')
        self.assertEqual(portable.posix(portable.PurePath('skills/a')), 'skills/a')

    def test_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'state/lock'
            with portable.exclusive_lock(lock):
                with self.assertRaises(BlockingIOError):
                    with portable.exclusive_lock(lock):
                        pass
            with portable.exclusive_lock(lock):
                pass

    def test_a_recording_lock_names_its_holder_only_while_it_runs(self):
        import json
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'state/pull.lock'
            before = datetime.now(timezone.utc).replace(microsecond=0)  # the note gives whole seconds
            with portable.exclusive_lock(lock, record=True):
                holder = portable.lock_holder(lock)
                self.assertEqual(holder['pid'], os.getpid())
                self.assertIn(Path(sys.argv[0]).name, holder['command'])
                # release.py status prints since's hour and minute as this machine's clock time, and the private CI
                # census's finish_sampler.py reads when the hold began from it.
                since = datetime.fromisoformat(holder['since'])
                self.assertEqual(since.utcoffset(), datetime.now().astimezone().utcoffset(), holder['since'])
                self.assertLessEqual(before, since, holder['since'])
                self.assertLessEqual(since, datetime.now(timezone.utc), holder['since'])
            self.assertIsNone(portable.lock_holder(lock))
            self.assertFalse(lock.with_name('pull.lock.holder').exists())
            child = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'],
                                   capture_output=True, text=True, check=True)
            # A holder file a crashed run left behind names a process that has ended.
            lock.with_name('pull.lock.holder').write_text(json.dumps({'pid': int(child.stdout), 'command': 'pull.py'}))
            self.assertIsNone(portable.lock_holder(lock))

    @unittest.skipIf(sys.platform == 'darwin', 'macOS has no /proc to read a start time from')
    def test_a_holder_note_whose_process_id_another_program_now_holds_names_no_holder(self):
        import json
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'state/pull.lock'
            with portable.exclusive_lock(lock, record=True):
                note = json.loads(lock.with_name('pull.lock.holder').read_text())
            with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']) as other:
                try:
                    lock.with_name('pull.lock.holder').write_text(json.dumps({**note, 'pid': other.pid}))
                    self.assertIsNone(portable.lock_holder(lock))
                finally:
                    other.kill()

    def test_a_process_that_ended_does_not_run_while_another_still_holds_a_handle_to_it(self):
        import subprocess
        with subprocess.Popen([sys.executable, '-c', '']) as child:
            child.wait()
            self.assertFalse(portable.process_running(child.pid))

    @unittest.skipIf(os.name == 'nt', 'Windows sends no SIGINT to one process')
    def test_an_interrupted_command_is_stopped_with_what_it_started_only_when_its_caller_asks(self):
        for asks, stopped in ((', stopped_by=BaseException', True), ('', False)):
            with self.subTest(asks=asks), tempfile.TemporaryDirectory() as directory:
                marker = Path(directory) / 'started'
                # A caller started with & from a script ignores SIGINT until it restores Python's own handler.
                caller = ('import signal, sys\nsignal.signal(signal.SIGINT, signal.default_int_handler)\n'
                          f'sys.path.insert(0, {str(ENGINE)!r})\nfrom portable import run_tree\n'
                          f'run_tree([sys.executable, "-c", {STARTS_A_CHILD!r}, {str(marker)!r}], timeout=None{asks})\n')
                with subprocess.Popen([sys.executable, '-c', caller], stderr=subprocess.DEVNULL) as process:
                    command, child = started(self, marker)
                    process.send_signal(signal.SIGINT)
                    self.assertNotEqual(process.wait(timeout=30), 0)
                if stopped:
                    self.assertEqual((gone(command), gone(child)), (True, True))
                else:
                    self.assertEqual(list(map(portable.process_running, (command, child))), [True, True])

    def test_a_command_past_its_time_limit_is_stopped_with_what_it_started(self):
        for limit in (3, 10, 30):
            with tempfile.TemporaryDirectory() as directory:
                marker = Path(directory) / 'started'
                with self.assertRaises(subprocess.TimeoutExpired):
                    portable.run_tree([sys.executable, '-c', STARTS_A_CHILD, str(marker)], timeout=limit)
                if marker.exists():
                    command, child = started(self, marker)
                    self.assertEqual((gone(command), gone(child)), (True, True))
                    return
        self.fail('The command never started its child within the longest limit')

    @unittest.skipIf(os.name == 'nt', 'a Windows job holds every process the command starts')
    def test_a_child_that_left_the_commands_session_delays_the_stop_by_seconds_only(self):
        leaves = ('import pathlib, subprocess, sys, time\n'
                  "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], "
                  'start_new_session=True)\n'
                  "pathlib.Path(sys.argv[1] + '.part').write_text(str(child.pid))\n"
                  "pathlib.Path(sys.argv[1] + '.part').replace(sys.argv[1])\n"
                  'time.sleep(120)\n')
        for limit in (3, 10, 30):
            with tempfile.TemporaryDirectory() as directory:
                marker = Path(directory) / 'started'
                began = time.monotonic()
                with self.assertRaises(subprocess.TimeoutExpired) as raised:
                    portable.run_tree([sys.executable, '-c', leaves, str(marker)], timeout=limit,
                                      stdout=subprocess.PIPE)
                if marker.exists():
                    started(self, marker)
                    self.assertLess(time.monotonic() - began, limit + 20)
                    self.assertEqual(raised.exception.timeout, limit, 'not the error of the command\'s own limit')
                    return
        self.fail('The command never started its child within the longest limit')

    def test_a_command_that_ends_in_time_returns_as_from_subprocess_run(self):
        result = portable.run_tree([sys.executable, '-c', 'import sys; print("out"); sys.exit(4)'], timeout=60,
                                   stdout=subprocess.PIPE, text=True)
        self.assertEqual((result.returncode, result.stdout), (4, 'out\n'))

    @unittest.skipIf(os.name == 'nt', "subprocess's reader threads keep a decode error to themselves on Windows")
    def test_output_the_caller_cannot_decode_raises_as_from_subprocess_run(self):
        with self.assertRaises(UnicodeDecodeError):
            portable.run_tree([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(bytes([255]))'], timeout=60,
                              stopped_by=BaseException, stdout=subprocess.PIPE, text=True, encoding='utf-8')

    def test_the_job_is_ended_when_taskkill_runs_past_its_limit(self):
        library = MagicMock()
        library.QueryInformationJobObject.return_value = 0
        with patch.object(portable, 'WINDOWS', True), patch.object(portable, '_kernel32', lambda: library), \
                patch.object(portable.subprocess, 'run', side_effect=subprocess.TimeoutExpired('taskkill', 30)):
            with self.assertRaises(subprocess.TimeoutExpired):
                portable._stop_tree(MagicMock(pid=4711), 99)
        library.TerminateJobObject.assert_called_once_with(99, 1)

    def test_the_stop_waits_until_the_job_holds_no_process(self):
        with patch.object(portable, 'WINDOWS', True), patch.object(portable, '_kernel32', MagicMock), \
                patch.object(portable, '_job_processes', side_effect=[2, 1, 0]) as counted, \
                patch.object(portable.subprocess, 'run'):
            portable._stop_tree(MagicMock(pid=4711), 99)
        self.assertEqual(counted.call_count, 3)

    @unittest.skipUnless(os.name == 'nt', 'Windows jobs')
    def test_a_job_counts_its_processes_until_it_is_ended(self):
        kernel32 = portable._kernel32()
        job = kernel32.CreateJobObjectW(None, None)
        self.addCleanup(kernel32.CloseHandle, job)
        sleeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        self.assertTrue(kernel32.AssignProcessToJobObject(job, sleeper._handle))
        self.assertGreaterEqual(portable._job_processes(job), 1)
        kernel32.TerminateJobObject(job, 1)
        sleeper.wait(timeout=10)
        ending = time.monotonic() + 5
        while portable._job_processes(job) and time.monotonic() < ending:
            time.sleep(0.05)
        self.assertEqual(portable._job_processes(job), 0)

    @unittest.skipUnless(os.name == 'nt' and not ctypes.windll.shell32.IsUserAnAdmin(),
                         'Windows refuses to open its System process, PID 4, to a run that is not elevated')
    def test_a_process_windows_will_not_open_counts_as_running(self):
        self.assertTrue(portable.process_running(4, '1'))

    def test_extract_materialises_links_inside_the_archive(self):
        data = archive([('pkg', None), ('pkg/CLAUDE.md', b'rules\n'), ('pkg/AGENTS.md', ('link', 'CLAUDE.md')),
                        ('pkg/shared', None), ('pkg/shared/a.md', b'a\n'), ('pkg/shared/b.md', ('link', 'a.md')),
                        ('pkg/copy', ('link', 'shared'))])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            portable.extract_tar(data, root)
            self.assertFalse((root / 'pkg/AGENTS.md').is_symlink())
            self.assertEqual((root / 'pkg/AGENTS.md').read_bytes(), b'rules\n')
            self.assertEqual((root / 'pkg/copy/b.md').read_bytes(), b'a\n')
            self.assertFalse(any(path.is_symlink() for path in root.rglob('*')))

    def test_extract_rejects_links_that_leave_the_archive_or_dangle(self):
        for link in ['../../outside', 'missing.md']:
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    portable.extract_tar(archive([('pkg', None), ('pkg/x', ('link', link))]), directory)


if __name__ == '__main__':
    unittest.main()
