import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import skill_draft

TOOL = str(ROOT / 'skill_draft.py')
TIMEOUT = 120


def skill(description, name='foo'):
    return f'---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n'


INSTALLED, DRAFT = skill('Installed wording.'), skill('Draft wording.')
READER = ('import json, os, sys; print(json.dumps({p: sorted(os.listdir(p)) if os.path.isdir(p) else '
          'open(p, encoding="utf-8").read() for p in sys.argv[1:]}))')
WAITER = '''
import os, pathlib, sys, time
folder = pathlib.Path(sys.argv[1])
def wait_for_release():
    deadline = time.monotonic() + 60
    while not (folder / 'release').exists():
        if time.monotonic() > deadline:
            sys.exit(9)
        time.sleep(0.05)
(folder / 'pid').write_text(str(os.getpid()))
(folder / 'pid').rename(folder / 'ready')
try:
    wait_for_release()
except KeyboardInterrupt:
    wait_for_release()
    sys.exit(5)
'''
PROBE = '''
import ctypes, os, sys
uid, gid = os.getuid(), os.getgid()
os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
for name, text in (('setgroups', 'deny'), ('uid_map', f'{uid} {uid} 1'), ('gid_map', f'{gid} {gid} 1')):
    with open(f'/proc/self/{name}', 'w') as target:
        target.write(text)
if ctypes.CDLL(None, use_errno=True).mount(os.fsencode(sys.argv[1]), os.fsencode(sys.argv[2]), None, 4096, None):
    sys.exit(os.strerror(ctypes.get_errno()))
'''
REFUSED_MOUNT = '''
import ctypes, errno
libc = ctypes.CDLL(None, use_errno=True)
class Refusing:
    def mount(self, source, target, kind, flags, data):
        if REFUSED(kind, flags):
            ctypes.set_errno(errno.EPERM)
            return -1
        return libc.mount(source, target, kind, flags, data)
ctypes.CDLL = lambda *arguments, **options: Refusing()
'''
REFUSED_CALL = '''
import errno, os
def refusing(path, *arguments, **options):
    raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
setattr(os, CALL, refusing)
'''
DELETED_WORKING_FOLDER = '''
import os, tempfile
folder = tempfile.mkdtemp()
os.chdir(folder)
os.rmdir(folder)
'''
STOP_BEFORE_THE_COMMAND_STARTS = '''
import os, signal, subprocess
start = subprocess.Popen
def late(*arguments, **options):
    os.kill(os.getpid(), STOP)
    return start(*arguments, **options)
subprocess.Popen = late
'''
CODEX_STAND_IN = '''
import json, os
path = os.path.join(os.environ['HOME'], '.agents', 'skills', 'foo', 'SKILL.md')
if os.path.isfile(path) and 'zebracorn' in open(path, encoding='utf-8').read():
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution', 'command': f'cat {path}'}}))
'''


class NamespaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder, 'first'), Path(folder, 'second')
            first.write_text('first', encoding='utf-8')
            second.write_text('second', encoding='utf-8')
            probe = subprocess.run([sys.executable, '-I', '-c', PROBE, first, second], capture_output=True, text=True,
                                   timeout=TIMEOUT)
        if probe.returncode:
            reason = (probe.stderr.strip().splitlines() or [f'status {probe.returncode}'])[-1]
            raise unittest.SkipTest(f'this platform or kernel refuses a private user and mount namespace: {reason}')

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.home = self.root / 'home'
        self.draft = self.install(self.root / 'draft/SKILL.md', DRAFT)
        self.signals = self.root / 'signals'
        self.signals.mkdir()
        self.addCleanup((self.signals / 'release').write_text, '')

    def install(self, path, text=INSTALLED):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def copy(self, root, name='foo'):
        return self.home / root / 'skills' / name / 'SKILL.md'

    def environment(self, claude=None, codex=None, path=None):
        environment = {key: value for key, value in os.environ.items()
                       if key not in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME')}
        environment['HOME'] = str(self.home)
        for key, value in (('CLAUDE_CONFIG_DIR', claude), ('CODEX_HOME', codex)):
            if value is not None:
                environment[key] = str(value)
        if path is not None:
            environment['PATH'] = f'{path}{os.pathsep}{environment["PATH"]}'
        return environment

    def run_tool(self, *arguments, claude=None, codex=None):
        return subprocess.run([sys.executable, '-I', TOOL, *map(str, arguments)], capture_output=True, text=True,
                              env=self.environment(claude, codex), timeout=TIMEOUT)

    def run_main(self, setup, *arguments):
        code = (f'import sys\nsys.path.insert(0, {str(ROOT)!r})\nimport skill_draft\n{setup}\n'
                'sys.argv[0] = "skill_draft.py"\nsys.exit(skill_draft.main())')
        return subprocess.run([sys.executable, '-c', code, *map(str, arguments)], capture_output=True, text=True,
                              env=self.environment(), timeout=TIMEOUT)

    def reads(self, *paths, installed=(), new=False, claude=None, codex=None):
        options = [item for path in installed for item in ('--installed', path)] + ['--new'] * new
        run = self.run_tool(self.draft, *options, '--', sys.executable, '-c', READER, *paths,
                            claude=claude, codex=codex)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run, {Path(path): text for path, text in json.loads(run.stdout).items()}

    def start_waiter(self, *arguments, **options):
        return subprocess.Popen([sys.executable, '-I', TOOL, str(self.draft), *arguments, '--', sys.executable, '-c',
                                 WAITER, str(self.signals)], stderr=subprocess.PIPE, text=True,
                                env=self.environment(), **options)

    def wait_until_ready(self, tool):
        deadline = time.monotonic() + TIMEOUT
        while not (self.signals / 'ready').exists():
            self.assertIsNone(tool.poll(), 'the tool ended before its command started')
            self.assertLess(time.monotonic(), deadline, 'the command never started')
            time.sleep(0.05)
        return int((self.signals / 'ready').read_text())

    def refused(self, *arguments):
        marker = self.root / 'ran'
        run = self.run_tool(*arguments, '--', sys.executable, '-c', f'open({str(marker)!r}, "w")')
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertNotIn('Traceback', run.stderr)
        self.assertFalse(marker.exists(), 'the command ran')
        return run.stderr

    def test_the_command_reads_the_draft_at_each_installed_copy_and_the_disk_keeps_the_installed_text(self):
        copies = [self.install(self.copy(root)) for root in ('.claude', '.agents', '.codex')]
        other = self.install(self.copy('.claude', 'bar'), skill('Another skill.', 'bar'))
        run, seen = self.reads(*copies, other)
        self.assertEqual(seen, {**{path: DRAFT for path in copies}, other: skill('Another skill.', 'bar')})
        for path in copies:
            self.assertIn(f'in place of {path}', run.stderr)
            self.assertEqual(path.read_text(encoding='utf-8'), INSTALLED)
        self.assertNotIn(str(other), run.stderr)

    def test_a_skill_installed_for_one_host_only_is_replaced_there(self):
        copy = self.install(self.copy('.agents'))
        run, seen = self.reads(copy)
        self.assertEqual(seen, {copy: DRAFT})
        self.assertEqual(run.stderr.count('in place of'), 1, run.stderr)

    def test_the_hosts_config_folders_choose_the_copies(self):
        claude, codex = self.home / 'claude-config', self.home / 'codex-home'
        chosen = [self.install(claude / 'skills/foo/SKILL.md'), self.install(codex / 'skills/foo/SKILL.md')]
        unread = [self.install(self.copy('.claude')), self.install(self.copy('.codex'))]
        _, seen = self.reads(*chosen, *unread, claude=claude, codex=codex)
        self.assertEqual(seen, {**{path: DRAFT for path in chosen}, **{path: INSTALLED for path in unread}})
        _, seen = self.reads(*chosen, *unread, claude='', codex='')
        self.assertEqual(seen, {**{path: INSTALLED for path in chosen}, **{path: DRAFT for path in unread}})

    def test_installed_replaces_a_plugin_copy_and_leaves_the_default_places(self):
        plugin = self.install(self.home / '.claude/plugins/cache/market/tools/1.0/skills/foo/SKILL.md')
        default = self.install(self.copy('.agents'))
        run, seen = self.reads(plugin, default, installed=[plugin])
        self.assertEqual(seen, {plugin: DRAFT, default: INSTALLED})
        self.assertNotIn(str(default), run.stderr)
        self.draft.write_text('# A copy with no frontmatter\n', encoding='utf-8')
        _, seen = self.reads(plugin, installed=[plugin])
        self.assertEqual(seen, {plugin: '# A copy with no frontmatter\n'})

    def test_the_top_level_name_chooses_the_skill(self):
        foo = self.install(self.copy('.agents'))
        other = self.install(self.copy('.agents', 'other'), skill('Another skill.', 'other'))
        for name in ('foo', '"foo"'):
            nested = f'---\nmetadata:\n  name: other\nname: {name}\ndescription: Draft wording.\n---\n'
            self.draft.write_text(nested, encoding='utf-8')
            _, seen = self.reads(foo, other)
            self.assertEqual(seen, {foo: nested, other: skill('Another skill.', 'other')})

    def test_the_command_cannot_write_to_the_installed_path(self):
        copy = self.install(self.copy('.agents'))
        code = ('import sys\ntry:\n    open(sys.argv[1], "a").write("written")\n'
                'except OSError as error:\n    print(error.errno)')
        run = self.run_tool(self.draft, '--', sys.executable, '-c', code, copy)
        self.assertEqual((run.returncode, run.stdout.strip()), (0, str(errno.EROFS)), run.stderr)
        self.assertEqual(self.draft.read_text(encoding='utf-8'), DRAFT)
        self.assertEqual(copy.read_text(encoding='utf-8'), INSTALLED)

    @unittest.skipUnless(os.access('/dev/shm', os.W_OK) and os.statvfs('/dev/shm').f_flag & os.ST_NOSUID,
                         'this machine has no writable nosuid /dev/shm')
    def test_a_draft_on_a_nosuid_mount_is_bound(self):
        copy = self.install(self.copy('.agents'))
        folder = tempfile.TemporaryDirectory(dir='/dev/shm')
        self.addCleanup(folder.cleanup)
        self.draft = self.install(Path(folder.name, 'SKILL.md'), DRAFT)
        _, seen = self.reads(copy)
        self.assertEqual(seen, {copy: DRAFT})

    def test_the_command_runs_as_the_same_user_and_can_write(self):
        self.install(self.copy('.agents'))
        written = self.home / 'written'
        code = f'import os; print(os.getuid(), os.getgid()); open({str(written)!r}, "w").close()'
        run = self.run_tool(self.draft, '--', sys.executable, '-c', code)
        self.assertEqual((run.returncode, run.stdout.split()), (0, [str(os.getuid()), str(os.getgid())]), run.stderr)
        self.assertEqual(written.stat().st_uid, os.getuid())

    def test_a_draft_that_is_the_installed_copy_stops_before_the_command(self):
        copy = self.install(self.copy('.agents'))
        self.assertIn(str(copy), self.refused(copy))
        self.assertIn(str(copy), self.refused(copy, '--installed', copy))

    def test_no_command_is_a_usage_error(self):
        self.install(self.copy('.agents'))
        for arguments in ((self.draft,), (self.draft, '--')):
            run = self.run_tool(*arguments)
            self.assertEqual(run.returncode, 2, run.stderr)
            self.assertIn('usage', run.stderr)

    def test_a_command_that_cannot_start_stops_with_the_systems_error(self):
        self.install(self.copy('.agents'))
        missing = self.root / 'no-such-command'
        run = self.run_tool(self.draft, '--', missing)
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertNotIn('Traceback', run.stderr)
        self.assertIn(f'Cannot run {missing}', run.stderr)
        self.assertIn(os.strerror(errno.ENOENT), run.stderr)

    def test_a_refused_mount_stops_before_the_command_with_the_systems_error(self):
        copy = self.copy('.agents')
        folder = copy.parent.parent
        self.install(folder / 'bar/SKILL.md', skill('Another skill.', 'bar'))
        marker = self.root / 'ran'
        for step, refused, target, options in (
                ('refused to bind', 'flags == 4096', copy, ()),
                ('read-only', 'flags & 32', copy, ()),
                ('a tmpfs', 'kind == b"tmpfs"', folder, ('--new',)),
                ('an overlay', 'kind == b"overlay"', folder, ('--new',))):
            with self.subTest(step=step):
                if not options:
                    self.install(copy)
                run = self.run_main(f'REFUSED = lambda kind, flags: {refused}\n{REFUSED_MOUNT}', self.draft,
                                    *options, '--', sys.executable, '-c', f'open({str(marker)!r}, "w")')
                if not options:
                    copy.unlink()
                    copy.parent.rmdir()
                self.assertEqual(run.returncode, 1, run.stderr)
                self.assertNotIn('Traceback', run.stderr)
                self.assertIn(step, run.stderr)
                self.assertIn(str(target), run.stderr)
                self.assertIn(os.strerror(errno.EPERM), run.stderr)
                self.assertNotIn('sessions read', run.stderr)
                self.assertFalse(marker.exists(), 'the command ran')
                self.assertEqual(sorted(os.listdir(folder)), ['bar'])

    def test_a_stop_signal_before_the_command_starts_still_stops_it(self):
        self.install(self.copy('.agents'))
        for stop in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(stop=stop.name):
                began = time.monotonic()
                run = self.run_main(f'STOP = {int(stop)}\n{STOP_BEFORE_THE_COMMAND_STARTS}', self.draft, '--',
                                    sys.executable, '-c', 'import time; time.sleep(60)')
                self.assertEqual(run.returncode, 128 + stop, run.stderr)
                self.assertLess(time.monotonic() - began, 30)

    def test_a_skill_installed_nowhere_stops_before_the_command(self):
        self.install(self.copy('.agents', 'bar'), skill('Another skill.', 'bar'))
        message = self.refused(self.draft)
        for root in ('.claude', '.agents', '.codex'):
            self.assertIn(str(self.copy(root)), message)
        self.assertIn('The skill foo', message)
        self.assertIn('--installed', message)
        self.assertIn('--new', message)

    def test_new_adds_the_draft_to_both_skills_folders_for_the_command_only(self):
        other = self.install(self.copy('.claude', 'bar'), skill('Another skill.', 'bar'))
        notes = self.install(self.home / '.agents/skills/notes.txt', 'notes')
        codex = self.install(self.copy('.codex', 'baz'), skill('A Codex skill.', 'baz')).parent.parent
        claude, agents = other.parent.parent, notes.parent
        added = [self.copy('.claude'), self.copy('.agents')]
        run, seen = self.reads(*added, other, claude, agents, codex, new=True)
        self.assertEqual(seen, {**{path: DRAFT for path in added}, other: skill('Another skill.', 'bar'),
                                claude: ['bar', 'foo'], agents: ['foo', 'notes.txt'], codex: ['baz']})
        self.assertEqual(run.stderr.count('a skill added for the command only'), 2, run.stderr)
        for path in added:
            self.assertIn(str(path), run.stderr)
        self.assertEqual((sorted(os.listdir(claude)), sorted(os.listdir(agents))), (['bar'], ['notes.txt']))
        self.assertEqual(other.read_text(encoding='utf-8'), skill('Another skill.', 'bar'))

    def test_new_adds_the_skill_to_the_one_skills_folder_that_exists(self):
        self.install(self.copy('.agents', 'bar'), skill('Another skill.', 'bar'))
        run, seen = self.reads(self.copy('.agents'), new=True)
        self.assertEqual(seen, {self.copy('.agents'): DRAFT})
        self.assertEqual(run.stderr.count('a skill added for the command only'), 1, run.stderr)
        self.assertFalse((self.home / '.claude').exists())

    def test_a_write_in_a_folder_that_new_adds_to_stays_off_the_disk(self):
        other = self.install(self.copy('.agents', 'bar'), skill('Another skill.', 'bar'))
        folder = other.parent.parent
        code = ('import sys\nfolder, other, added = sys.argv[1:]\nopen(folder + "/written", "w").write("written")\n'
                'open(other, "a").write("appended")\ntry:\n    open(added, "a").write("written")\n'
                'except OSError as error:\n    print(error.errno)')
        run = self.run_tool(self.draft, '--new', '--', sys.executable, '-c', code, folder, other, self.copy('.agents'))
        self.assertEqual((run.returncode, run.stdout.strip()), (0, str(errno.EROFS)), run.stderr)
        self.assertEqual(sorted(os.listdir(folder)), ['bar'])
        self.assertEqual(other.read_text(encoding='utf-8'), skill('Another skill.', 'bar'))
        self.assertEqual(self.draft.read_text(encoding='utf-8'), DRAFT)

    def test_new_for_an_installed_skill_stops_before_the_command(self):
        for root in ('.claude', '.agents', '.codex'):
            (self.home / root / 'skills').mkdir(parents=True, exist_ok=True)
        for root in ('.claude', '.agents', '.codex'):
            with self.subTest(root=root):
                copy = self.install(self.copy(root))
                message = self.refused(self.draft, '--new')
                copy.unlink()
                copy.parent.rmdir()
                self.assertIn(str(copy), message)
                self.assertIn('The skill foo', message)
                self.assertIn('--new', message)

    def test_new_where_the_skill_name_is_taken_stops_before_the_command(self):
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        self.copy('.agents').parent.parent.mkdir(parents=True)
        self.copy('.agents').parent.symlink_to(elsewhere)
        taken = self.copy('.agents').parent
        self.assertIn(f'{taken} exists without a SKILL.md', self.refused(self.draft, '--new'))
        self.assertEqual(os.listdir(elsewhere), [])
        elsewhere.rmdir()
        self.assertIn(f'{taken} exists without a SKILL.md', self.refused(self.draft, '--new'))
        taken.unlink()
        taken.mkdir()
        self.assertIn(f'{taken} exists without a SKILL.md', self.refused(self.draft, '--new'))

    def test_new_adds_the_skill_once_where_the_two_skills_folders_are_one_folder(self):
        other = self.install(self.copy('.agents', 'bar'), skill('Another skill.', 'bar'))
        folder = other.parent.parent
        (self.home / '.claude').mkdir()
        (self.home / '.claude/skills').symlink_to(folder)
        added = [self.copy('.claude'), self.copy('.agents')]
        _, seen = self.reads(*added, folder, new=True)
        self.assertEqual(seen, {**{path: DRAFT for path in added}, folder: ['bar', 'foo']})
        self.assertEqual(os.listdir(folder), ['bar'])

    def test_new_keeps_a_command_started_inside_a_skills_folder_off_the_disk(self):
        folder = self.copy('.agents').parent.parent
        folder.mkdir(parents=True)
        code = 'import os; open("written", "w").close(); print(sorted(os.listdir(".")))'
        run = subprocess.run([sys.executable, '-I', TOOL, str(self.draft), '--new', '--', sys.executable, '-c', code],
                             capture_output=True, text=True, env=self.environment(), cwd=folder, timeout=TIMEOUT)
        self.assertEqual((run.returncode, run.stdout.strip()), (0, "['foo', 'written']"), run.stderr)
        self.assertEqual(os.listdir(folder), [])

    def test_a_failed_system_call_while_placing_the_draft_stops_before_the_command(self):
        copy = self.copy('.agents')
        folder = copy.parent.parent
        folder.mkdir(parents=True)
        marker = self.root / 'ran'
        command = ('--', sys.executable, '-c', f'open({str(marker)!r}, "w")')
        for call, options in (('open', ('--new',)), ('mkdir', ('--new',)), ('statvfs', ())):
            with self.subTest(call=call):
                if not options:
                    self.install(copy)
                run = self.run_main(f'CALL = {call!r}\n{REFUSED_CALL}', self.draft, *options, *command)
                self.assertEqual(run.returncode, 1, run.stderr)
                self.assertNotIn('Traceback', run.stderr)
                self.assertIn(os.strerror(errno.EACCES), run.stderr)
                self.assertIn(str(folder), run.stderr)
                self.assertNotIn('sessions read', run.stderr)
                self.assertFalse(marker.exists(), 'the command ran')

    def test_new_from_a_deleted_working_folder_stops_before_the_command(self):
        self.copy('.agents').parent.parent.mkdir(parents=True)
        marker = self.root / 'ran'
        run = self.run_main(DELETED_WORKING_FOLDER, self.draft, '--new', '--', sys.executable, '-c',
                            f'open({str(marker)!r}, "w")')
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertNotIn('Traceback', run.stderr)
        self.assertIn(os.strerror(errno.ENOENT), run.stderr)
        self.assertFalse(marker.exists(), 'the command ran')

    def test_new_with_no_skills_folder_stops_before_the_command(self):
        self.install(self.copy('.codex', 'baz'), skill('A Codex skill.', 'baz'))
        message = self.refused(self.draft, '--new')
        for root in ('.claude', '.agents'):
            self.assertIn(str(self.home / root / 'skills'), message)

    def test_new_with_installed_is_a_usage_error(self):
        copy = self.install(self.copy('.agents'))
        marker = self.root / 'ran'
        run = self.run_tool(self.draft, '--new', '--installed', copy, '--', sys.executable, '-c',
                            f'open({str(marker)!r}, "w")')
        self.assertEqual(run.returncode, 2, run.stderr)
        self.assertIn('usage', run.stderr)
        self.assertFalse(marker.exists(), 'the command ran')

    def test_an_installed_path_that_is_no_file_stops_before_the_command(self):
        self.install(self.copy('.agents'))
        missing = self.home / '.claude/plugins/cache/market/tools/1.0/skills/foo/SKILL.md'
        self.assertIn(str(missing), self.refused(self.draft, '--installed', missing))

    def test_a_draft_that_names_no_skill_stops_before_the_command(self):
        self.install(self.copy('.agents'))
        for text in (None, '# foo\n', '---\ndescription: x\n---\n', '---\ndescription: x\n---\nname: foo\n',
                     '---\nname: other\nname: foo\n---\n',
                     skill('x', name='../foo'), skill('x', name='.'), skill('x', name='..'), b'\xff\xfe'):
            with self.subTest(text=text):
                self.draft.unlink(missing_ok=True)
                if isinstance(text, str):
                    self.draft.write_text(text, encoding='utf-8')
                elif text is not None:
                    self.draft.write_bytes(text)
                self.assertIn(str(self.draft), self.refused(self.draft))

    def test_the_command_exit_status_passes_through(self):
        self.install(self.copy('.agents'))
        for code, status in (('pass', 0), ('raise SystemExit(3)', 3), ('os.kill(os.getpid(), signal.SIGKILL)', 137)):
            with self.subTest(code=code):
                run = self.run_tool(self.draft, '--', sys.executable, '-c', f'import os, signal\n{code}')
                self.assertEqual(run.returncode, status, run.stderr)

    def changed_during_the_run(self, change, *arguments):
        tool = self.start_waiter(*arguments)
        self.wait_until_ready(tool)
        change()
        (self.signals / 'release').write_text('')
        _, stderr = tool.communicate(timeout=TIMEOUT)
        return tool.returncode, [line for line in stderr.splitlines() if 'Measure again' in line]

    def test_an_installed_copy_replaced_during_the_run_fails_the_measurement(self):
        replaced, kept = self.install(self.copy('.agents')), self.install(self.copy('.claude'))

        def pull():
            self.assertEqual(replaced.read_text(encoding='utf-8'), INSTALLED, 'outside the command, the disk text')
            replaced.parent.rename(replaced.parent.with_name('foo.old'))
            self.install(replaced, skill('Pulled wording.'))
        status, warnings = self.changed_during_the_run(pull)
        self.assertNotEqual(status, 0)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn(str(replaced), warnings[0])
        self.assertNotIn(str(kept), warnings[0])

    def test_an_installed_copy_removed_during_the_run_fails_the_measurement(self):
        removed = self.install(self.copy('.agents'))
        status, warnings = self.changed_during_the_run(
            lambda: removed.parent.rename(removed.parent.with_name('foo.old')))
        self.assertNotEqual(status, 0)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertTrue(warnings[0].startswith(str(removed)), warnings)
        self.assertIn(os.strerror(errno.ENOENT), warnings[0])

    def test_a_draft_edited_during_the_run_fails_the_measurement(self):
        copies = [self.install(self.copy('.agents')), self.install(self.copy('.claude'))]
        status, warnings = self.changed_during_the_run(
            lambda: self.draft.write_text(skill('Edited.'), encoding='utf-8'))
        self.assertNotEqual(status, 0)
        self.assertEqual(len(warnings), 3, warnings)
        for path in (self.draft, *copies):
            self.assertEqual(sum(warning.startswith(str(path)) for warning in warnings), 1, warnings)

    def test_a_draft_replaced_by_a_new_file_during_the_run_fails_the_measurement(self):
        self.install(self.copy('.agents'))

        def atomic_save():
            self.install(self.root / 'draft/new', skill('Edited.')).replace(self.draft)
        status, warnings = self.changed_during_the_run(atomic_save)
        self.assertNotEqual(status, 0)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertTrue(warnings[0].startswith(str(self.draft)), warnings)

    def test_a_copy_installed_in_another_place_during_the_run_fails_the_measurement(self):
        self.install(self.copy('.agents'))
        status, warnings = self.changed_during_the_run(lambda: self.install(self.copy('.claude')))
        self.assertNotEqual(status, 0)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertTrue(warnings[0].startswith(str(self.copy('.claude'))), warnings)

    def test_with_new_a_draft_replaced_by_a_new_file_during_the_run_fails_the_measurement(self):
        (self.home / '.agents/skills').mkdir(parents=True)

        def atomic_save():
            self.install(self.root / 'draft/new', skill('Edited.')).replace(self.draft)
        status, warnings = self.changed_during_the_run(atomic_save, '--new')
        self.assertNotEqual(status, 0)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertTrue(warnings[0].startswith(str(self.draft)), warnings)

    def test_with_new_a_skills_folder_replaced_during_the_run_fails_the_measurement(self):
        folder = self.home / '.agents/skills'
        folder.mkdir(parents=True)

        def pull():
            folder.rename(folder.with_name('skills.old'))
            folder.mkdir()
        status, warnings = self.changed_during_the_run(pull, '--new')
        self.assertNotEqual(status, 0)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertTrue(warnings[0].startswith(str(self.copy('.agents'))), warnings)

    def test_with_new_a_copy_installed_for_codex_during_the_run_fails_the_measurement(self):
        (self.home / '.agents/skills').mkdir(parents=True)
        status, warnings = self.changed_during_the_run(lambda: self.install(self.copy('.codex')), '--new')
        self.assertNotEqual(status, 0)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertTrue(warnings[0].startswith(str(self.copy('.codex'))), warnings)

    def test_a_stop_signal_to_the_tool_stops_the_command(self):
        self.install(self.copy('.agents'))
        for stop in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(stop=stop.name):
                tool = self.start_waiter()
                command = self.wait_until_ready(tool)
                tool.send_signal(stop)
                tool.communicate(timeout=TIMEOUT)
                (self.signals / 'ready').unlink()
                self.assertEqual(tool.returncode, 128 + stop)
                with self.assertRaises(ProcessLookupError, msg='the command still runs'):
                    os.kill(command, 0)

    def test_ctrl_c_lets_the_command_finish_and_gives_its_status(self):
        self.install(self.copy('.agents'))
        tool = self.start_waiter(start_new_session=True)
        self.wait_until_ready(tool)
        os.killpg(tool.pid, signal.SIGINT)
        (self.signals / 'release').write_text('')
        _, stderr = tool.communicate(timeout=TIMEOUT)
        self.assertEqual(tool.returncode, 5, stderr)

    def test_codex_triggers_behind_the_tool_measures_the_draft(self):
        marked, plain = skill('Use for zebracorn work.'), skill('Use for other work.')
        stand_in = self.install(self.root / 'bin/codex', f'#!{sys.executable} -I\n{CODEX_STAND_IN}')
        stand_in.chmod(0o755)
        queries = self.install(self.root / 'queries.json',
                               json.dumps([{'target': 'foo', 'query': 'Do the work', 'should_trigger': True}]))

        def hits(installed, draft, *options, behind_the_tool=True):
            if installed is not None:
                self.install(self.copy('.agents'), installed)
            self.install(self.draft, draft)
            out = self.root / 'out.json'
            out.unlink(missing_ok=True)
            command = [sys.executable, '-I', str(ROOT / 'codex_triggers.py'), str(queries), str(out), '--runs', '2']
            prefix = [sys.executable, '-I', TOOL, str(self.draft), *options, '--'] if behind_the_tool else []
            run = subprocess.run(prefix + command, capture_output=True, text=True, timeout=TIMEOUT,
                                 env=self.environment(path=stand_in.parent))
            self.assertEqual(run.returncode, 0, run.stderr)
            return json.loads(out.read_text(encoding='utf-8'))[0]['hits']
        self.assertEqual(hits(marked, plain, behind_the_tool=False), 2, 'the stand-in sees the installed description')
        self.assertEqual(hits(marked, plain), 0)
        self.assertEqual(hits(plain, marked), 2)
        self.copy('.agents').unlink()
        self.copy('.agents').parent.rmdir()
        self.assertEqual(hits(None, marked, behind_the_tool=False), 0, 'the stand-in finds no skill to read')
        self.assertEqual(hits(None, marked, '--new'), 2)
        self.assertEqual(hits(None, plain, '--new'), 0)


class WithoutNamespacesTest(unittest.TestCase):
    def test_refuses_where_os_unshare_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            draft, installed = Path(folder, 'draft.md'), Path(folder, 'SKILL.md')
            draft.write_text(DRAFT, encoding='utf-8')
            installed.write_text(INSTALLED, encoding='utf-8')
            marker = Path(folder, 'ran')
            argv = ['skill_draft.py', str(draft), '--installed', str(installed), '--', sys.executable, '-c',
                    f'open({str(marker)!r}, "w")']
            with patch.dict(os.__dict__), patch.object(sys, 'argv', argv), self.assertRaises(SystemExit) as stopped:
                os.__dict__.pop('unshare', None)
                skill_draft.main()
            self.assertIn('Linux', str(stopped.exception.code))
            self.assertIn('Python 3.12', str(stopped.exception.code))
            self.assertFalse(marker.exists(), 'the command ran')

    @unittest.skipUnless(hasattr(os, 'unshare'), 'this platform has no os.unshare to refuse')
    def test_names_the_step_the_kernel_refuses(self):
        # The kernel gives a process with a second thread no user namespace, so the tool's own refusal runs here.
        release = threading.Event()
        second = threading.Thread(target=release.wait)
        second.start()
        self.addCleanup(second.join)
        self.addCleanup(release.set)
        with tempfile.TemporaryDirectory() as folder:
            draft, installed = Path(folder, 'draft.md'), Path(folder, 'SKILL.md')
            draft.write_text(DRAFT, encoding='utf-8')
            installed.write_text(INSTALLED, encoding='utf-8')
            marker = Path(folder, 'ran')
            argv = ['skill_draft.py', str(draft), '--installed', str(installed), '--', sys.executable, '-c',
                    f'open({str(marker)!r}, "w")']
            with patch.object(sys, 'argv', argv), self.assertRaises(SystemExit) as stopped:
                skill_draft.main()
            self.assertIn('namespace', str(stopped.exception.code))
            self.assertIn(os.strerror(errno.EINVAL), str(stopped.exception.code))
            self.assertFalse(marker.exists(), 'the command ran')


if __name__ == '__main__':
    unittest.main()
