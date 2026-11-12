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
READER = 'import json, sys; print(json.dumps({p: open(p, encoding="utf-8").read() for p in sys.argv[1:]}))'
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
        if REFUSED(flags):
            ctypes.set_errno(errno.EPERM)
            return -1
        return libc.mount(source, target, kind, flags, data)
ctypes.CDLL = lambda *arguments, **options: Refusing()
'''
SIGTERM_BEFORE_THE_COMMAND_STARTS = '''
import os, signal, subprocess
start = subprocess.Popen
def late(*arguments, **options):
    os.kill(os.getpid(), signal.SIGTERM)
    return start(*arguments, **options)
subprocess.Popen = late
'''
CODEX_STAND_IN = '''
import json, os
path = os.path.join(os.environ['HOME'], '.agents', 'skills', 'foo', 'SKILL.md')
if 'zebracorn' in open(path, encoding='utf-8').read():
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

    def reads(self, *paths, installed=(), claude=None, codex=None):
        options = [item for path in installed for item in ('--installed', path)]
        run = self.run_tool(self.draft, *options, '--', sys.executable, '-c', READER, *paths,
                            claude=claude, codex=codex)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run, {Path(path): text for path, text in json.loads(run.stdout).items()}

    def start_waiter(self, **options):
        return subprocess.Popen([sys.executable, '-I', TOOL, str(self.draft), '--', sys.executable, '-c', WAITER,
                                 str(self.signals)], stderr=subprocess.PIPE, text=True, env=self.environment(),
                                **options)

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
        copy = self.install(self.copy('.agents'))
        marker = self.root / 'ran'
        for step, refused in (('the bind', 'flags == 4096'), ('the read-only remount', 'flags & 32')):
            with self.subTest(step=step):
                run = self.run_main(f'REFUSED = lambda flags: {refused}\n{REFUSED_MOUNT}', self.draft, '--',
                                    sys.executable, '-c', f'open({str(marker)!r}, "w")')
                self.assertEqual(run.returncode, 1, run.stderr)
                self.assertNotIn('Traceback', run.stderr)
                self.assertIn(str(copy), run.stderr)
                self.assertIn(os.strerror(errno.EPERM), run.stderr)
                self.assertNotIn('in place of', run.stderr)
                self.assertFalse(marker.exists(), 'the command ran')

    def test_sigterm_before_the_command_starts_still_stops_it(self):
        self.install(self.copy('.agents'))
        began = time.monotonic()
        run = self.run_main(SIGTERM_BEFORE_THE_COMMAND_STARTS, self.draft, '--', sys.executable, '-c',
                            'import time; time.sleep(60)')
        self.assertEqual(run.returncode, 128 + signal.SIGTERM, run.stderr)
        self.assertLess(time.monotonic() - began, 30)

    def test_a_skill_installed_nowhere_stops_before_the_command(self):
        self.install(self.copy('.agents', 'bar'), skill('Another skill.', 'bar'))
        message = self.refused(self.draft)
        for root in ('.claude', '.agents', '.codex'):
            self.assertIn(str(self.copy(root)), message)
        self.assertIn('The skill foo', message)
        self.assertIn('--installed', message)

    def test_an_installed_path_that_is_no_file_stops_before_the_command(self):
        self.install(self.copy('.agents'))
        missing = self.home / '.claude/plugins/cache/market/tools/1.0/skills/foo/SKILL.md'
        self.assertIn(str(missing), self.refused(self.draft, '--installed', missing))

    def test_a_draft_that_names_no_skill_stops_before_the_command(self):
        self.install(self.copy('.agents'))
        for text in (None, '# foo\n', '---\ndescription: x\n---\n', '---\ndescription: x\n---\nname: foo\n',
                     '---\nname: other\nname: foo\n---\n',
                     skill('x', name='../foo'), b'\xff\xfe'):
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

    def changed_during_the_run(self, change):
        tool = self.start_waiter()
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

    def test_sigterm_to_the_tool_stops_the_command(self):
        self.install(self.copy('.agents'))
        tool = self.start_waiter()
        command = self.wait_until_ready(tool)
        tool.send_signal(signal.SIGTERM)
        tool.communicate(timeout=TIMEOUT)
        self.assertEqual(tool.returncode, 128 + signal.SIGTERM)
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

        def hits(installed, draft, behind_the_tool=True):
            self.install(self.copy('.agents'), installed)
            self.install(self.draft, draft)
            out = self.root / 'out.json'
            out.unlink(missing_ok=True)
            command = [sys.executable, '-I', str(ROOT / 'codex_triggers.py'), str(queries), str(out), '--runs', '2']
            prefix = [sys.executable, '-I', TOOL, str(self.draft), '--'] if behind_the_tool else []
            run = subprocess.run(prefix + command, capture_output=True, text=True, timeout=TIMEOUT,
                                 env=self.environment(path=stand_in.parent))
            self.assertEqual(run.returncode, 0, run.stderr)
            return json.loads(out.read_text(encoding='utf-8'))[0]['hits']
        self.assertEqual(hits(marked, plain, behind_the_tool=False), 2, 'the stand-in sees the installed description')
        self.assertEqual(hits(marked, plain), 0)
        self.assertEqual(hits(plain, marked), 2)


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
