from contextlib import contextmanager
import errno
import io
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from read_account_skill import EXITED, WITHHELD, Client, main, select_source, stderr_lines, windows_handoff

STAND_IN = """import json, os, sys
assert sys.argv[1:] == ['app-server', '--stdio'], sys.argv
with open(sys.argv[0] + '.seen', 'w') as seen:
    json.dump({key: os.environ.get(key) for key in ('CODEX_HOME', 'CODEX_SQLITE_HOME')}, seen)
for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message:
        continue
    print(json.dumps({'method': 'notice'}), flush=True)
    result = {'data': [{'errors': [], 'skills': [{'name': 'example:task'}]}]} if message['method'] == 'skills/list' else {}
    print(json.dumps({'id': message['id'], 'result': result}), flush=True)
"""
# Lines that test the cleaning, then the end of Node's report when Codex's npm wrapper fails, whose reason is the
# seventh line from the end. Written as bytes, so that Windows adds no carriage returns of its own.
STDERR = (b'first line\n'
          b'\x1b[2m2026-11-13T16:24:24Z\x1b[0m \x1b[31mERROR\x1b[0m \x1b[2mcodex_app_server\x1b[0m\x1b[2m:\x1b[0m bad \xff\n'
          b'\x1b[3mrefresh_token\x1b[0m\x1b[2m=\x1b[0mmade-up\n\n' + b'x' * 401 + b'\n'
          b'Error: Missing optional dependency @openai/codex-linux-x64. Reinstall Codex: npm install -g @openai/codex@latest\n'
          b'    at findCodexExecutable (file:///codex/bin/codex.js:108:9)\n'
          b'    at file:///codex/bin/codex.js:113:20\n'
          b'    at ModuleJob.run (node:internal/modules/esm/module_job:569:25)\n'
          b'    at async node:internal/modules/esm/loader:650:26\n'
          b'    at async asyncRunEntryPointWithESMLoader (node:internal/modules/run_main:105:5)\n\n'
          b'Node.js v26.13.0\r\n')
SHOWN = [EXITED, *('app-server stderr: ' + line for line in (
    '2026-11-13T16:24:24Z ERROR codex_app_server: bad ' + chr(0xfffd), WITHHELD, 'x' * 400 + '...',
    'Error: Missing optional dependency @openai/codex-linux-x64. Reinstall Codex: npm install -g @openai/codex@latest',
    'at findCodexExecutable (file:///codex/bin/codex.js:108:9)', 'at file:///codex/bin/codex.js:113:20',
    'at ModuleJob.run (node:internal/modules/esm/module_job:569:25)', 'at async node:internal/modules/esm/loader:650:26',
    'at async asyncRunEntryPointWithESMLoader (node:internal/modules/run_main:105:5)', 'Node.js v26.13.0'))]


@contextmanager
def stand_in_app_server(script):
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary)
        (home / 'app-server').write_text(script, encoding='utf-8', newline='\n')
        with patch('read_account_skill.portable.codex_cli', return_value=sys.executable), \
                patch.object(Path, 'home', return_value=home):
            yield home


@contextmanager
def recorded_temporary_files():
    files = []
    temporary_file = tempfile.TemporaryFile

    def recorded():
        files.append(temporary_file())
        return files[-1]
    with patch('read_account_skill.tempfile.TemporaryFile', recorded):
        yield files


def started_and_exited(started):
    popen = subprocess.Popen

    def start(*args, **kwargs):
        process = popen(*args, **kwargs)
        process.wait()
        started.append((process, kwargs))
        return process
    return start


class SourceSelectionTest(unittest.TestCase):
    def test_reads_the_publisher_even_when_its_discovery_entry_is_disabled(self):
        source = {'name': 'example:task', 'pluginId': 'example@openai-curated-remote',
                  'path': str(Path('/publisher/skills/task/SKILL.md').absolute()), 'enabled': False}
        wrapper = {**source, 'pluginId': None, 'path': '/managed/SKILL.md', 'enabled': True}
        self.assertEqual(select_source([wrapper, source], 'example:task'), Path(source['path']))

    def test_refuses_missing_ambiguous_or_foreign_sources(self):
        source = {'name': 'example:task', 'pluginId': 'example@openai-curated-remote',
                  'path': str(Path('/publisher/skills/task/SKILL.md').absolute())}
        for skills in ([], [source, source], [{**source, 'pluginId': 'example@personal'}]):
            with self.subTest(skills=skills), self.assertRaises(RuntimeError):
                select_source(skills, 'example:task')

    def test_rejects_unqualified_names_and_non_skill_paths(self):
        with self.assertRaises(ValueError):
            select_source([], 'task')
        with self.assertRaises(RuntimeError):
            select_source([{'name': 'example:task', 'pluginId': 'example@openai-curated-remote',
                            'path': '/publisher/settings.json'}], 'example:task')

    @unittest.skipIf(sys.platform == 'win32', 'WSL path translation runs on POSIX')
    def test_wsl_uses_windows_python_and_preserves_the_native_profile(self):
        for profile in ('C:/Users/test person/profile/home', '/mnt/c/Users/test person/profile/home'):
            with self.subTest(profile=profile), patch('portable.WINDOWS', False), patch.object(Path, 'is_file', return_value=True):
                self.assertEqual(windows_handoff(profile, 'read_account_skill.py'), [
                    '/mnt/c/Users/test person/dotfiles/.venv/Scripts/python.exe',
                    'C:/Users/test person/dotfiles/read_account_skill.py', '--profile',
                    'C:/Users/test person/profile/home'])

    @unittest.skipIf(sys.platform == 'win32', 'WSL path translation runs on POSIX')
    def test_without_a_profile_option_a_wsl_read_hands_off_to_the_windows_profile_codex_home_names(self):
        profile = '/mnt/c/Users/test person/profile/home'
        with patch('portable.WINDOWS', False), patch.object(Path, 'is_file', return_value=True), \
                patch.dict(os.environ, {'CODEX_HOME': profile}), \
                patch('read_account_skill.subprocess.run', return_value=SimpleNamespace(returncode=5)) as run:
            self.assertEqual(main(['example:task']), 5)
        self.assertEqual(run.call_args.args[0][-3:], ['--profile', 'C:/Users/test person/profile/home', 'example:task'])

    def test_wsl_refuses_a_missing_windows_runtime(self):
        with patch('portable.WINDOWS', False), patch.object(Path, 'is_file', return_value=False):
            with self.assertRaises(RuntimeError):
                windows_handoff('C:/Users/test/.codex', 'read_account_skill.py')

    def test_native_profiles_need_no_handoff(self):
        with patch('portable.WINDOWS', False):
            self.assertIsNone(windows_handoff('/home/test/.codex', 'read_account_skill.py'))
        with patch('portable.WINDOWS', True):
            self.assertIsNone(windows_handoff('C:/Users/test/.codex', 'read_account_skill.py'))

    def test_publisher_unicode_is_utf8_even_under_a_legacy_console_encoding(self):
        with tempfile.TemporaryDirectory() as temporary:
            skill = Path(temporary) / 'SKILL.md'
            skill.write_bytes('Publisher café 🚀\n'.encode('utf-8'))
            buffer = io.BytesIO()
            output = io.TextIOWrapper(buffer, encoding='ascii')
            errors = io.TextIOWrapper(io.BytesIO(), encoding='ascii')
            with patch('read_account_skill.Client') as client, patch('read_account_skill.windows_handoff', return_value=None), \
                    patch('sys.stdout', output), patch('sys.stderr', errors):
                client.return_value.skills.return_value = [{'name': 'example:task',
                    'pluginId': 'example@openai-curated-remote', 'path': str(skill)}]
                self.assertEqual(main(['example:task']), 0)
                output.flush()
                self.assertIn('Publisher café 🚀', buffer.getvalue().decode('utf-8').splitlines())
            output.close()
            errors.close()


@unittest.skipIf(os.name == 'nt', 'the stand-in app-server is a script with a shebang')
class ClientTest(unittest.TestCase):
    def test_the_app_server_runs_on_the_profile_without_an_inherited_sqlite_home_and_lists_its_skills(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = root / 'codex'
            server.write_text(f'#!{sys.executable}\n' + STAND_IN)
            server.chmod(0o755)
            with patch('read_account_skill.portable.codex_cli', return_value=str(server)), \
                    patch.dict(os.environ, {'CODEX_SQLITE_HOME': str(root / 'sqlite')}):
                client = Client(root / 'profile')
                try:
                    skills = client.skills()
                finally:
                    client.close()
            self.assertEqual(skills, [{'name': 'example:task'}])
            self.assertEqual(json.loads(Path(f'{server}.seen').read_text()), {'CODEX_HOME': str(root / 'profile'), 'CODEX_SQLITE_HOME': None})

    def test_an_app_server_that_exits_before_replying_stops_the_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            server = Path(temporary) / 'codex'
            # It reads the request before it exits, so the client's write always reaches it.
            server.write_text(f'#!{sys.executable}\nimport sys\nsys.stdin.readline()\n')
            server.chmod(0o755)
            with patch('read_account_skill.portable.codex_cli', return_value=str(server)), \
                    self.assertRaisesRegex(RuntimeError, 'Native app-server exited before replying'):
                Client()


class ExitedServerTest(unittest.TestCase):
    def test_an_app_server_that_exits_before_the_first_write_stops_the_read(self):
        started = []
        with stand_in_app_server(''), patch('read_account_skill.subprocess.Popen', started_and_exited(started)), \
                self.assertRaises(RuntimeError) as caught:
            Client()
        self.assertEqual(str(caught.exception), EXITED)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        process, arguments = started[0]
        self.assertTrue(process.stdout.closed)
        self.assertTrue(arguments['stderr'].closed)


class ServerStderrTest(unittest.TestCase):
    def test_the_stop_shows_the_last_stderr_lines_of_a_server_that_exited_before_the_first_write(self):
        started = []
        with stand_in_app_server(f'import sys\nsys.stderr.buffer.write({STDERR!r})\nsys.exit(1)\n'), \
                patch('read_account_skill.subprocess.Popen', started_and_exited(started)), \
                self.assertRaises(RuntimeError) as caught:
            Client()
        self.assertEqual(str(caught.exception).split('\n'), SHOWN)
        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_the_read_prints_the_stderr_lines_of_a_server_that_exits_after_reading_the_request(self):
        errors = io.BytesIO()
        error_text = io.TextIOWrapper(errors, encoding='utf-8', newline='\n')
        # It exits with status 0, as Codex does when its input closes, so the lines do not hang on the status.
        script = f'import sys\nsys.stdin.readline()\nsys.stderr.buffer.write({STDERR!r})\nsys.exit(0)\n'
        with stand_in_app_server(script), patch('read_account_skill.windows_handoff', return_value=None), \
                patch('sys.stdout', io.TextIOWrapper(io.BytesIO(), encoding='utf-8')), patch('sys.stderr', error_text):
            self.assertEqual(main(['example:task']), 1)
            error_text.flush()
        self.assertEqual(errors.getvalue().decode('utf-8'), 'Account skill unavailable: ' + '\n'.join(SHOWN) + '\n')

    def test_a_server_that_closes_its_output_but_keeps_running_gives_the_stop_alone(self):
        script = ('import os, sys\nsys.stderr.buffer.write(b"still running\\n")\nsys.stderr.flush()\n'
                  'os.close(1)\nsys.stdin.read()\n')
        with stand_in_app_server(script), patch('read_account_skill.EXIT_WAIT_SECONDS', 0), \
                self.assertRaises(RuntimeError) as caught:
            Client()
        self.assertEqual(str(caught.exception), EXITED)

    def test_a_server_that_never_replies_gives_its_own_stop_without_stderr_lines(self):
        script = 'import sys\nsys.stderr.buffer.write(b"still running\\n")\nsys.stderr.flush()\nsys.stdin.read()\n'
        ticks = itertools.count(0, 100)

        def clock_after_stderr():
            began = time.monotonic()
            while not os.fstat(files[0].fileno()).st_size:
                if time.monotonic() - began > 60:
                    raise AssertionError('the stand-in wrote nothing to stderr')
                time.sleep(0.01)
            return next(ticks)
        with stand_in_app_server(script), recorded_temporary_files() as files, \
                patch('read_account_skill.time', SimpleNamespace(monotonic=clock_after_stderr)), \
                self.assertRaises(RuntimeError) as caught:
            Client()
        self.assertEqual(str(caught.exception), 'Native app-server timed out: initialize')

    def test_a_stderr_file_that_cannot_be_read_gives_the_stop_with_the_reading_error(self):
        class Unreadable(io.BytesIO):
            def seek(self, *args):
                raise OSError(errno.EIO, 'Input/output error')

        process = SimpleNamespace(stdin=SimpleNamespace(write=lambda text: None, flush=lambda: None, close=lambda: None),
                                  stdout=io.StringIO(), wait=lambda timeout: 1)
        with patch('read_account_skill.tempfile.TemporaryFile', return_value=Unreadable()), \
                patch('read_account_skill.portable.codex_cli', return_value='codex'), \
                patch('read_account_skill.subprocess.Popen', return_value=process), \
                self.assertRaises(RuntimeError) as caught:
            Client()
        self.assertEqual(str(caught.exception),
                         f'{EXITED}\napp-server stderr: could not be read: [Errno {errno.EIO}] Input/output error')

    def test_the_stderr_file_is_closed_when_the_app_server_cannot_start(self):
        with stand_in_app_server('') as home, patch('read_account_skill.portable.codex_cli', return_value=str(home / 'missing')), \
                recorded_temporary_files() as files, self.assertRaises(OSError):
            Client()
        self.assertTrue(files[0].closed)


class StderrLinesTest(unittest.TestCase):
    def shown(self, text):
        return stderr_lines(io.BytesIO(text.encode('utf-8')))

    def test_each_known_credential_form_is_withheld_whole(self):
        for line in ('config: password=made-up', 'refresh_token made-up', 'TOKEN made-up', 'passwd: made-up',
                     'client_secret made-up', 'API_KEY made-up', 'access-key: made-up', 'CREDENTIAL made-up',
                     'authorization: made-up', 'private_key = made-up', 'cookie: session=made-up',
                     '{"level":"ERROR","fields":{"refresh_token":"made-up"}}', 'secret_key: made-up',
                     'token\x1b]8;;x\x1b\\=made-up',
                     'https://user:made-up@example.com/', 'header Bearer made-up', 'Basic dXNlcjpwYXNz',
                     'ghp_' + 'A' * 36, 'AKIAABCDEFGHIJKLMNOP', 'xoxb-123-made-up', '-----BEGIN PRIVATE KEY-----',
                     '-----BEGIN PGP PRIVATE KEY BLOCK-----',
                     'sk-proj-' + 'a' * 20, 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.made-up',
                     '\x1b[3mrefresh_token\x1b[0m\x1b[2m=\x1b[0mmade-up', 'token\x00=made-up',
                     'x' * 400 + ' token=made-up'):
            with self.subTest(line=line):
                self.assertEqual(self.shown(line + '\n'), [WITHHELD])

    def test_every_line_of_a_private_key_is_withheld_even_when_its_first_line_is_not_among_those_shown(self):
        body = ['-----BEGIN PRIVATE KEY-----', *['TUFERS1VUA' * 6] * 12, '-----END PRIVATE KEY-----', 'after the key']
        self.assertEqual(self.shown('\n'.join(body) + '\n'), [WITHHELD] * 9 + ['after the key'])
        joined = ['-----BEGIN CERTIFICATE-----', 'TUFERS1VUA', '-----END CERTIFICATE----------BEGIN PRIVATE KEY-----',
                  'TUFERS1VUA', '-----END PRIVATE KEY-----', 'after the keys']
        self.assertEqual(self.shown('\n'.join(joined) + '\n'), [WITHHELD] * 5 + ['after the keys'])

    def test_control_characters_are_shown_as_spaces(self):
        self.assertEqual(self.shown('a\x07b\x1bc\x9bd\x1b(Be\n'), ['a b c de'])

    def test_a_line_of_400_characters_is_shown_whole(self):
        self.assertEqual(self.shown('y' * 400 + '\n'), ['y' * 400])

    def test_codex_lines_without_credentials_are_shown_as_printed(self):
        lines = ['WARNING: proceeding, even though we could not create PATH aliases: CODEX_HOME points to '
                 '"/x/no-such-codex-home", but that path does not exist',
                 'Error: CODEX_HOME points to "/x/no-such-codex-home", but that path does not exist',
                 'ERROR codex_app_server: Invalid configuration; using defaults. /x/config.toml:3:62: unexpected key',
                 'Your access token could not be refreshed. Please log out and sign in again.']
        self.assertEqual(self.shown('\n'.join(lines) + '\n'), lines)


class WriteFailureTest(unittest.TestCase):
    def client_error(self, windows, write_error=None, close_error=None):
        def write(text):
            if write_error:
                raise write_error

        def close():
            if close_error:
                raise close_error

        process = SimpleNamespace(stdin=SimpleNamespace(write=write, flush=lambda: None, close=close),
                                  stdout=io.StringIO(), wait=lambda timeout: 0)
        with patch('read_account_skill.portable.WINDOWS', windows), \
                patch('read_account_skill.portable.codex_cli', return_value='codex'), \
                patch('read_account_skill.subprocess.Popen', return_value=process), recorded_temporary_files() as files, \
                self.assertRaises(Exception) as caught:
            Client()
        self.assertTrue(files[0].closed)
        return caught.exception

    def test_windows_reports_the_broken_pipe_of_an_exited_server_as_einval(self):
        # As on the Windows PC, closing the pipe fails the same way as the write.
        write_error = OSError(errno.EINVAL, 'Invalid argument')
        error = self.client_error(True, write_error=write_error, close_error=OSError(errno.EINVAL, 'Invalid argument'))
        self.assertEqual((type(error), str(error), error.__cause__),
                         (RuntimeError, 'Native app-server exited before replying', write_error))

    def test_other_write_and_close_failures_reach_the_caller(self):
        for windows, write_error, close_error in ((False, OSError(errno.EINVAL, 'Invalid argument'), None),
                                                  (True, OSError(errno.EIO, 'Input/output error'), None),
                                                  (True, None, OSError(errno.EIO, 'Input/output error'))):
            with self.subTest(windows=windows, write_error=write_error, close_error=close_error):
                self.assertIs(self.client_error(windows, write_error, close_error), write_error or close_error)


if __name__ == '__main__':
    unittest.main()
