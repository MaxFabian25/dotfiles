from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homes import isolate_git


def setUpModule():
    isolate_git()


class CommandTest(unittest.TestCase):
    def operation(self, home, code):
        from sync_files import Files
        from sync_hosts import Apply
        script = home.write(home.bin / 'install.py', code)
        operation = Apply.__new__(Apply)
        operation.roots = home.roots
        operation.files = Files(home.roots.home / 'backups')
        operation.device = SimpleNamespace(host=lambda host: SimpleNamespace(cli=[sys.executable, str(script)]))
        return operation

    def test_command_reads_normalised_stdout_despite_bad_stderr_and_keeps_environment_cwd_and_backups(self):
        import os
        from unittest.mock import patch
        from homes import Home
        from host_state import Host
        for host in Host:
            with self.subTest(host=host), Home() as home:
                paths = [home.roots.codex_home / 'config.toml'] if host == Host.CODEX else [
                    home.roots.claude_home / name for name in ('settings.json', 'plugins/installed_plugins.json',
                                                             'plugins/known_marketplaces.json')]
                for path in paths:
                    home.write(path, 'before\n')
                operation = self.operation(home, 'import json, os, pathlib, sys\n'
                    'print(json.dumps({key: os.environ[key] for key in '
                    '("CODEX_HOME", "CLAUDE_CONFIG_DIR", "COMMAND_TEST", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_VALUE_1")}))\n'
                    'print(pathlib.Path.cwd())\n'
                    f'for name in {list(map(str, paths))!r}:\n'
                    '    pathlib.Path(name).write_bytes(b"after\\n")\n'
                    'sys.stdout.flush()\n'
                    'sys.stdout.buffer.write(b"one\\r\\ntwo\\rthree\\n")\n'
                    'sys.stderr.buffer.write(b"warning: caf\\xe9\\r\\n")\n')
                with patch.dict(os.environ, {'COMMAND_TEST': 'retained', 'GIT_CONFIG_COUNT': '0'}):
                    output = operation.command(host, 'plugin', 'install', 'kit', '--json')
                environment, cwd, *lines = output.split('\n')
                self.assertEqual(json.loads(environment), {
                    'CODEX_HOME': str(home.roots.codex_home), 'CLAUDE_CONFIG_DIR': str(home.roots.claude_home),
                    'COMMAND_TEST': 'retained', 'GIT_CONFIG_VALUE_0': 'false', 'GIT_CONFIG_VALUE_1': 'lf'})
                self.assertEqual(Path(cwd).resolve(), home.roots.home.resolve())
                self.assertEqual(lines, ['one', 'two', 'three', ''])
                self.assertEqual({p.name: p.read_bytes() for p in operation.files.backup.iterdir() if p.name != 'files.json'},
                                 {f'{at:04d}-{path.name}': b'before\n' for at, path in enumerate(paths, 1)})
                self.assertTrue(all(path.read_bytes() == b'after\n' for path in paths))

    def test_command_failure_keeps_the_diagnostic_tail_and_uses_stdout_only_when_stderr_is_empty(self):
        from homes import Home
        from host_state import Host
        diagnostic = ('discarded prefix' + '界' * 2100).encode() + b'\r\nfatal: caf\xe9\rreason\r\n'
        tail = ('界' * 2100 + '\nfatal: caf\N{REPLACEMENT CHARACTER}\nreason\n')[-2000:]
        for stdout, stderr, expected in ((b'ignored \xff', diagnostic, tail),
                                         (diagnostic, b'', tail), (b'', b'', '')):
            with self.subTest(stderr=bool(stderr), stdout=bool(stdout)), Home() as home:
                operation = self.operation(home, 'import sys\n'
                    f'sys.stdout.buffer.write({stdout!r})\n'
                    f'sys.stderr.buffer.write({stderr!r})\n'
                    'sys.exit(7)\n')
                with self.assertRaises(RuntimeError) as failed:
                    operation.command(Host.CODEX, 'plugin', 'add', 'kit@personal', '--json')
                self.assertEqual(str(failed.exception), 'codex plugin add kit@personal failed: ' + expected)

    def test_install_restores_a_kept_disabled_plugin_after_a_bad_diagnostic_or_unreadable_stdout(self):
        import tomllib
        import description as d
        from homes import Home
        from host_state import Host
        for host in Host:
            for status in (0, 7):
                with self.subTest(host=host, status=status), Home() as home:
                    install = d.EngineInstall(False, keep_current=True)
                    identity = d.plugin_id('kit', install, host)
                    if host == Host.CODEX:
                        path = home.roots.codex_home / 'config.toml'
                        enabled = f'[plugins."{identity}"]\nenabled = true\n'
                    else:
                        path = home.roots.claude_home / 'settings.json'
                        enabled = json.dumps({'enabledPlugins': {identity: True}})
                    home.write(path, '')
                    operation = self.operation(home, 'import pathlib, sys\n'
                        f'pathlib.Path({str(path)!r}).write_bytes({enabled.encode()!r})\n'
                        'sys.stdout.buffer.write(b"unreadable \\xff\\r\\n")\n'
                        'sys.stderr.buffer.write(b"error: caf\\xe9\\r\\n")\n'
                        f'sys.exit({status})\n')
                    operation.current = SimpleNamespace(observations={host: SimpleNamespace(facts={})})
                    operation.setup = SimpleNamespace(packages={'kit': SimpleNamespace(
                        placement=lambda _: SimpleNamespace(installs=(install,)))})
                    with self.assertRaises(RuntimeError if status else subprocess.SubprocessError) as failed:
                        operation._install(host, identity)
                    self.assertIn('caf\N{REPLACEMENT CHARACTER}' if status else 'not UTF-8', str(failed.exception))
                    switch = (tomllib.loads(path.read_text())['plugins'][identity]['enabled'] if host == Host.CODEX
                              else json.loads(path.read_text())['enabledPlugins'][identity])
                    self.assertIs(switch, False)


class FileSafetyTest(unittest.TestCase):
    def test_claude_explicit_host_setting_uses_the_native_user_invocable_only_value(self):
        from host_state import FactKey, Host, SkillMode, Surface
        from sync_files import Files
        from sync_hosts import Apply
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'settings.json'
            path.write_text('{"skillOverrides":{"unrelated":"off"}}')
            operation = object.__new__(Apply)
            operation.roots = SimpleNamespace(claude_home=root)
            operation.files = Files(root / 'backups')
            operation.current = SimpleNamespace(changes=[SimpleNamespace(
                key=FactKey(Host.CLAUDE, Surface.SKILL, 'fewer-permission-prompts'), expected=SkillMode.EXPLICIT)])
            operation._skills()
            self.assertEqual(json.loads(path.read_text())['skillOverrides'],
                             {'unrelated': 'off', 'fewer-permission-prompts': 'user-invocable-only'})

    def test_claude_override_for_a_skill_the_description_dropped_is_removed(self):
        from host_state import FactKey, Host, Surface
        from sync_files import Files
        from sync_hosts import Apply
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'settings.json'
            path.write_text('{"skillOverrides":{"unrelated":"off","kit:dropped":"off"}}')
            operation = object.__new__(Apply)
            operation.roots = SimpleNamespace(claude_home=root)
            operation.files = Files(root / 'backups')
            operation.current = SimpleNamespace(changes=[SimpleNamespace(
                key=FactKey(Host.CLAUDE, Surface.SKILL, 'kit@personal-collections:dropped'), expected=None)])
            operation._skills()
            self.assertEqual(json.loads(path.read_text())['skillOverrides'], {'unrelated': 'off'})

    def test_concurrent_edit_is_not_overwritten(self):
        from sync_files import ConcurrentEdit, Files
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'config.toml'
            config.write_bytes(b'new user change\n')
            files = Files(root / 'backups')
            with self.assertRaises(ConcurrentEdit):
                files.write(config, b'sync change\n', expected=b'old bytes\n')
            self.assertEqual(config.read_bytes(), b'new user change\n')
            self.assertFalse((root / 'backups').exists())

    def test_toml_patch_preserves_comments_and_unmanaged_secrets(self):
        from sync_files import Files
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'config.toml'
            original = b'# keep this comment\n[mcp_servers.example.env]\nTOKEN = "test-only-secret"\n\n[plugins."kit@personal"]\nenabled = false\n'
            config.write_bytes(original)
            files = Files(root / 'backups')
            files.toml(config, lambda data: data['plugins']['kit@personal'].__setitem__('enabled', True))
            self.assertEqual(config.read_bytes(), original.replace(b'enabled = false', b'enabled = true'))
            self.assertEqual(next((root / 'backups').glob('*-config.toml')).read_bytes(), original)

    def test_plugin_toggle_reaches_a_new_table_among_spread_out_plugin_tables(self):
        import tomllib
        from sync_files import Files
        from sync_hosts import set_plugin_enabled
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'config.toml'
            config.write_bytes(b'[plugins."a@x"]\nenabled = true\n\n[tui]\nk = 1\n\n[plugins."b@x"]\n')
            files = Files(root / 'backups')
            for identity in ('a@x', 'b@x', 'new@x'):
                files.toml(config, lambda data, i=identity: set_plugin_enabled(data, i, False))
            plugins = tomllib.loads(config.read_text())['plugins']
            self.assertEqual(plugins, {name: {'enabled': False} for name in ('a@x', 'b@x', 'new@x')})

    def test_noop_does_not_create_a_backup_or_rewrite_the_file(self):
        from sync_files import Files
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'settings.json'
            path.write_bytes(b'{ "enabledPlugins": {} }\n')
            before = path.stat().st_mtime_ns
            files = Files(root / 'backups')
            files.json(path, lambda data: data.setdefault('enabledPlugins', {}))
            self.assertEqual(path.stat().st_mtime_ns, before)
            self.assertFalse((root / 'backups').exists())

class PlanTest(unittest.TestCase):
    def test_noop_apply_does_not_create_any_files(self):
        from device_sync import apply
        with tempfile.TemporaryDirectory() as temp:
            current = SimpleNamespace(blocked=(), changes=())
            self.assertEqual(apply(None, None, current), (current, None))
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_mac_only_package_is_disabled_elsewhere_not_installed(self):
        from description import (ClaudePlacement, Package, MarketplaceInstall, Roots, Setup, ToolNotes, Visibility)
        from host_state import Platform, SkillMode
        package = Package('swift-lsp', Visibility.PRIVATE, '', None, {'swift': SkillMode.EXPLICIT}, ToolNotes.AUDIT,
                          None, ClaudePlacement((MarketplaceInstall('claude-plugins-official', True),)),
                          frozenset({Platform.MACOS}))
        setup = Setup(Roots(Path('/public'), None), {'swift-lsp': package}, {}, {}, {}, None, (), frozenset(), {})
        self.assertTrue(setup.for_platform(Platform.MACOS).packages['swift-lsp'].claude.installs[0].enabled)
        for platform in (Platform.WINDOWS, Platform.LINUX):
            filtered = setup.for_platform(platform).packages['swift-lsp']
            self.assertFalse(filtered.claude.installs[0].enabled)
            self.assertEqual(filtered.modes, {})

    def test_a_kept_current_package_is_plainly_disabled_on_a_platform_it_is_not_for(self):
        from description import ClaudePlacement, EngineInstall, Package, Roots, Setup, ToolNotes, Visibility
        from host_state import Platform
        package = Package('kit', Visibility.PRIVATE, '', None, {}, ToolNotes.AUDIT, None,
                          ClaudePlacement((EngineInstall(False, keep_current=True),)), frozenset({Platform.MACOS}))
        setup = Setup(Roots(Path('/public'), None), {'kit': package}, {}, {}, {}, None, (), frozenset(), {})
        self.assertEqual(setup.for_platform(Platform.MACOS).packages['kit'].claude.installs, (EngineInstall(False, keep_current=True),))
        self.assertEqual(setup.for_platform(Platform.LINUX).packages['kit'].claude.installs, (EngineInstall(False),))

    def test_disabled_missing_plugin_is_not_an_install_request(self):
        from drift import _classify
        from host_state import FactKey, Host, NoteKind, Observation, PluginValue, Surface, Unpinned
        observation = Observation(Host.CLAUDE, 'test', {}, frozenset(), ())
        key = FactKey(Host.CLAUDE, Surface.PLUGIN, 'swift-lsp@claude-plugins-official')
        self.assertEqual(_classify(key, PluginValue(False, Unpinned()), None, observation), NoteKind.DORMANT)

    def test_unknown_enabled_plugin_is_left_untouched(self):
        from description import Roots, Setup
        from device import Device
        from device_sync import plan
        from host_state import DeviceRoots, Fact, FactKey, Host, Observation, Platform, PluginValue, Surface, Unpinned
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            roots = DeviceRoots.current(root)
            key = FactKey(Host.CODEX, Surface.PLUGIN, 'local-tool@personal')
            observation = Observation(Host.CODEX, 'test', {key: Fact(key, PluginValue(True, Unpinned()))},
                                      frozenset({key.subject}), ())
            state = SimpleNamespace(host=Host.CODEX, project=lambda setup: {}, observe=lambda request: observation)
            setup = Setup(Roots(root / 'public', None), {}, {}, {}, {}, None, (), frozenset(), {})
            result = plan(setup, Device(Platform.LINUX, roots, (state,), ()))
            self.assertEqual(result.changes, ())
            self.assertEqual(len(result.unmanaged), 1)
            self.assertEqual(result.exit_code, 0)

    def test_a_listed_unmanaged_item_becomes_a_note_and_a_stale_listing_shows(self):
        from description import Roots, Setup
        from device import Device
        from device_sync import plan
        from host_state import (DeviceRoots, Fact, FactKey, Host, NoteKind, Observation, Platform, PluginValue, Surface,
                                Unpinned)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key = FactKey(Host.CODEX, Surface.PLUGIN, 'local-tool@personal')
            gone = FactKey(Host.CODEX, Surface.LOOSE_SKILL, '{agents_home}/skills/gone')
            absent_host = FactKey(Host.CLAUDE, Surface.LOOSE_SKILL, '{claude_home}/skills/elsewhere')
            observation = Observation(Host.CODEX, 'test', {key: Fact(key, PluginValue(True, Unpinned()))},
                                      frozenset({key.subject}), ())
            state = SimpleNamespace(host=Host.CODEX, project=lambda setup: {}, observe=lambda request: observation)
            setup = Setup(Roots(root / 'public', None), {}, {}, {}, {}, None, (), frozenset(), {},
                          {Platform.LINUX: frozenset({key, gone, absent_host})})
            result = plan(setup, Device(Platform.LINUX, DeviceRoots.current(root), (state,), ()))
            self.assertEqual((result.changes, result.unmanaged, len(result.expected), result.exit_code), ((), (), 1, 0))
            self.assertEqual([(n.kind, n.subject, n.detail) for n in result.result.notes], [
                (NoteKind.DEVICE_LOCAL, 'expected unmanaged items', '1 named in devices/linux.toml; sync leaves them alone'),
                (NoteKind.RESIDUE, gone.subject, 'listed in devices/linux.toml but no longer an unmanaged difference here')])
            from drift import render
            counted, listed = render(result.result), render(result.result, notes=True)
            self.assertIn('notes (not differences; --notes lists each)\n'
                          '  residue         1  codex 1\n  device-local    1  codex 1\n', counted)
            self.assertNotIn('no longer an unmanaged difference', counted)
            self.assertIn(f'  residue       codex   {gone.subject}: listed in devices/linux.toml but no longer an '
                          'unmanaged difference here\n', listed)


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


class PinnedBuildTest(unittest.TestCase):
    def test_locking_enabled_catalog_plugin_does_not_build_application_content(self):
        from description import CodexPlacement, Lock, MarketplaceInstall, Package, Roots, Setup, ToolNotes, Visibility, intent
        from lock_setup import refresh
        from unittest.mock import Mock
        package = Package('drive', Visibility.PRIVATE, '', None, {}, ToolNotes.AUDIT,
                          CodexPlacement((MarketplaceInstall('openai-curated-remote', True),)), None)
        setup = Setup(Roots(Path('/public'), Path('/private')), {'drive': package}, {}, {}, {}, None, (), frozenset(), {})
        builder = Mock()
        updated = refresh(setup, builder, ['drive'])
        self.assertEqual(updated.lock['drive'], Lock(intent(package, {})))
        builder.codex.assert_not_called()

    def test_locking_a_snapshot_loose_skill_records_the_tree_stored_for_each_place(self):
        from description import LooseSkill, Roots, Setup, SkillRoot, SnapshotSource, Visibility
        from host_state import DeviceRoots
        from lock_setup import refresh
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder:
            roots = DeviceRoots.current(Path(folder))
            private = Path(folder) / 'private/setup'
            snapshots, trees = {}, {}
            for place in (SkillRoot.CLAUDE, SkillRoot.AGENTS):
                staged = Path(folder) / place.value
                staged.mkdir()
                (staged / 'SKILL.md').write_text(f'---\nname: notes\ndescription: The {place.value} copy.\n---\n')
                snapshots[place] = roots.tree_digest(staged)
                trees[place] = private / 'snapshots' / snapshots[place].removeprefix('sha256:')
                trees[place].parent.mkdir(parents=True, exist_ok=True)
                staged.rename(trees[place])
            skill = LooseSkill('notes', Visibility.PRIVATE, '', SnapshotSource(snapshots), {place: True for place in snapshots})
            setup = Setup(Roots(Path(folder) / 'setup', private), {}, {}, {'notes': skill}, {}, None, (), frozenset(), {})
            builder = Mock(roots=roots)
            self.assertEqual(refresh(setup, builder, ['notes']).lock['notes'].placements, snapshots)
            self.assertNotEqual(snapshots[SkillRoot.CLAUDE], snapshots[SkillRoot.AGENTS])

    def kept_and_dormant(self):
        from dataclasses import replace
        from description import (BuildSource, CodexPlacement, EngineInstall, GenericRecipe, Lock, LockedTree, Package, Roots,
                                 Setup, ToolNotes, Visibility, intent)
        from host_state import Digest, Host
        source = BuildSource(GenericRecipe('https://example.invalid/kit.git', 'a' * 40, None, 'plugin.json', ()), 1, 'Kit')
        kept = Package('kept', Visibility.PRIVATE, '', source, {}, ToolNotes.AUDIT,
                       CodexPlacement((EngineInstall(False, keep_current=True),)), None)
        dormant = replace(kept, name='dormant', codex=CodexPlacement((EngineInstall(False),)))
        tree = {Host.CODEX: LockedTree('1.0', Digest('sha256:' + '1' * 64))}
        lock = {p.name: Lock(intent(p, {}), tree) for p in (kept, dormant)}
        return Setup(Roots(Path('/public'), Path('/private')), {'kept': kept, 'dormant': dormant}, {}, {}, {}, None, (),
                     frozenset(), lock)

    def builder(self):
        from unittest.mock import Mock
        builder = Mock()
        builder.codex.return_value = SimpleNamespace(version='1.0', digest='sha256:' + '1' * 64)
        builder.claude.return_value = {}
        builder.roles.return_value = {}
        return builder

    def built(self, builder):
        return [call.args[0].name for call in builder.codex.call_args_list]

    def test_a_kept_current_package_without_a_locked_tree_is_stale_and_a_plain_disabled_one_is_not(self):
        from dataclasses import replace
        from description import Lock
        setup = self.kept_and_dormant()
        setup = replace(setup, lock={name: Lock(lock.intent) for name, lock in setup.lock.items()})
        self.assertEqual(setup.stale(), frozenset({'kept'}))

    def test_locking_builds_a_kept_current_package_and_skips_a_plain_disabled_one(self):
        from description import LockedTree
        from host_state import Host
        from lock_setup import refresh
        setup = self.kept_and_dormant()
        builder = self.builder()
        claude = SimpleNamespace(version='1.0.claude1', digest='sha256:' + '2' * 64)
        builder.claude.side_effect = lambda entry, built, verify: {f'{entry.name}@personal-collections': claude}
        updated = refresh(setup, builder, ['kept', 'dormant'])
        self.assertEqual(self.built(builder), ['kept'])
        self.assertEqual(updated.lock['kept'].trees, {Host.CODEX: LockedTree('1.0', 'sha256:' + '1' * 64),
                                                      Host.CLAUDE: LockedTree('1.0.claude1', 'sha256:' + '2' * 64)})

    def test_the_lock_command_lists_the_files_it_would_write_and_writes_none_without_the_flag(self):
        import contextlib
        from dataclasses import replace
        import io
        from unittest import mock
        from description import Roots
        import lock_setup
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            setup = replace(self.kept_and_dormant(), roots=Roots(root / 'public/setup', root / 'private/setup'))
            argv = ['lock_setup.py', '--public', str(root / 'public/setup'), '--output', str(root / 'out'), '--entry', 'kept']
            with mock.patch('lock_setup.load', return_value=setup), mock.patch('lock_setup.Builder', return_value=self.builder()), \
                    mock.patch('lock_setup.Sources'), mock.patch('sys.argv', argv), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(lock_setup.main(), 0)
            lines = out.getvalue().splitlines()
            self.assertTrue(lines and all(line.startswith(f'would write {root}') for line in lines), lines)
            self.assertFalse((root / 'public').exists() or (root / 'private').exists())

    def test_the_build_check_verifies_a_kept_current_package_and_skips_a_plain_disabled_one(self):
        import contextlib
        import io
        from unittest import mock
        import build_setup
        builder = self.builder()
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch('build_setup.load', return_value=self.kept_and_dormant()), \
                mock.patch('build_setup.Builder', return_value=builder), mock.patch('build_setup.Sources'), \
                mock.patch('sys.argv', ['build_setup.py', '--public', '/public', '--output', temp, '--platform', 'linux']), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(build_setup.main(), 0)
        self.assertEqual(self.built(builder), ['kept'])
        self.assertIn('1 packages rebuilt and verified; 0 failed.', out.getvalue())

    def test_a_sync_builds_a_kept_current_package_and_skips_a_plain_disabled_one(self):
        from accepted_installations import AcceptedInstallations
        from host_state import DeviceRoots, Platform
        with tempfile.TemporaryDirectory() as temp:
            device = SimpleNamespace(roots=DeviceRoots.current(Path(temp)), hosts=(), platform=Platform.LINUX)
            owner = AcceptedInstallations(self.kept_and_dormant(), device, configurations=False)
            builder = self.builder()
            prepared = owner.prepare({}, builder, packages={'kept', 'dormant'})
        self.assertEqual(self.built(builder), ['kept'])
        self.assertEqual(sorted(prepared.built), ['kept'])

    def test_refreshing_an_owned_skill_stops_on_a_description_over_the_limit(self):
        from description import LocalSource, LooseSkill, Roots, Setup, SkillRoot, Visibility
        from host_state import Digest
        from lock_setup import check_descriptions, refresh
        from sync_sources import BuildError
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / 'private/skills/mine/SKILL.md'
            skill.parent.mkdir(parents=True)
            mine = LooseSkill('mine', Visibility.PRIVATE, '', LocalSource('skills/mine', Digest('sha256:' + '0' * 64)),
                              {SkillRoot.CLAUDE: True})
            setup = Setup(Roots(root / 'public/setup', root / 'private/setup'), {}, {}, {'mine': mine}, {}, None, (),
                          frozenset(), {})
            skill.write_text('---\nname: mine\ndescription: ' + 'x' * 1024 + '\n---\nBody\n')
            check_descriptions(skill.parent)
            skill.write_text('---\nname: mine\ndescription: ' + 'x' * 1025 + '\n---\nBody\n')
            builder = Mock()
            with self.assertRaisesRegex(BuildError, 'SKILL.md: the description has 1025 characters; the limit is 1024'):
                refresh(setup, builder, ['mine'], owned=True)
            builder.roots.tree_digest.assert_not_called()
            # Without --refresh-owned an edited owned tree keeps its declared digest and its text is not checked.
            self.assertEqual(refresh(setup, builder, ['mine']).loose['mine'].source.digest, Digest('sha256:' + '0' * 64))

    def test_rebuild_uses_the_pin_even_after_upstream_moves_and_rejects_changed_output(self):
        from dataclasses import replace
        from description import BuildSource, CodexPlacement, EngineInstall, GenericRecipe, Lock, LockedTree, Package, Roots, Setup, ToolNotes, Visibility, intent
        from host_state import DeviceRoots, Host
        from sync_sources import Builder, Sources
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'upstream'
            repo.mkdir()
            git(repo, 'init', '-b', 'main')
            git(repo, 'config', 'user.name', 'Test')
            git(repo, 'config', 'user.email', 'test@example.invalid')
            git(repo, 'remote', 'add', 'origin', 'https://example.invalid/kit.git')
            (repo / '.cursor-plugin').mkdir()
            (repo / '.cursor-plugin/plugin.json').write_text(json.dumps({'name': 'kit', 'version': '1.0.0', 'author': {'name': 'Test'}}))
            skill = repo / 'skills/one/SKILL.md'
            skill.parent.mkdir(parents=True)
            skill.write_text('---\nname: one\ndescription: Test\n---\nPinned content.\n')
            git(repo, 'add', '.')
            git(repo, 'commit', '-m', 'Initial source')
            commit = git(repo, 'rev-parse', 'HEAD')
            recipe = GenericRecipe('https://example.invalid/kit.git', commit, None, '.cursor-plugin/plugin.json', ('./skills/one',))
            source = BuildSource(recipe, 1, 'Kit', metadata={'url': 'https://example.invalid/kit'})
            package = Package('kit', Visibility.PUBLIC, '', source, {}, ToolNotes.AUDIT, CodexPlacement((EngineInstall(True),)), None)
            setup = Setup(Roots(root / 'public', None), {'kit': package}, {}, {}, {}, None, (), frozenset(), {})
            roots = replace(DeviceRoots.current(root), engine=root / 'engine')
            builder = Builder(setup, roots, roots.engine, sources=Sources(root / 'sources', seeds=(repo,)))
            first = builder.codex(package, verify=False)
            locked = Lock(intent(package, {}), {Host.CODEX: LockedTree(first.version, first.digest)})
            builder.setup = replace(setup, lock={'kit': locked})
            skill.write_text('---\nname: one\ndescription: Test\n---\nNew upstream content.\n')
            git(repo, 'add', '.')
            git(repo, 'commit', '-m', 'Move upstream')
            second = builder.codex(package)
            self.assertEqual(first.digest, second.digest)
            self.assertIn('Pinned content.', (second.root / 'skills/one/SKILL.md').read_text())
            (second.root / 'skills/one/SKILL.md').write_text('modified generated output')
            with self.assertRaisesRegex(RuntimeError, 'Existing release differs'):
                builder.codex(package)

    def test_a_snapshot_package_builds_its_codex_and_claude_releases_in_the_sync_folder(self):
        from dataclasses import replace
        from description import (BuildSource, ClaudePlacement, CodexPlacement, EngineInstall, Package, Roots, Setup,
                                 SnapshotRecipe, ToolNotes, Visibility)
        from host_state import DeviceRoots
        from standalone_package import snapshot
        from sync_sources import Builder, Sources
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'input/one').mkdir(parents=True)
            (root / 'input/one/SKILL.md').write_text('---\nname: one\ndescription: Test\n---\nSnapshot content.\n')
            stored = snapshot(root / 'input', {'commit': None, 'selectedSources': {}}, root / 'private/setup/snapshots')
            source = BuildSource(SnapshotRecipe('sha256:' + stored.name, {'one': None}), 1, 'Kit',
                                 metadata={'description': 'Kit skills.', 'sourceKind': 'personal',
                                           'claudeDescriptions': {'kit': 'Kit skills.'}})
            package = Package('kit', Visibility.PRIVATE, '', source, {}, ToolNotes.AUDIT,
                              CodexPlacement((EngineInstall(True),)), ClaudePlacement((EngineInstall(True),)))
            setup = Setup(Roots(root / 'public/setup', root / 'private/setup'), {'kit': package}, {}, {}, {}, None, (),
                          frozenset(), {})
            roots = replace(DeviceRoots.current(root), engine=root / 'engine')
            builder = Builder(setup, roots, roots.engine, sources=Sources(root / 'sources'))
            codex = builder.codex(package, verify=False)
            [claude] = builder.claude(package, codex, verify=False).values()
            for built in (codex, claude):
                self.assertTrue(built.root.is_relative_to(roots.engine), built.root)
                self.assertIn('Snapshot content.', (built.root / 'skills/one/SKILL.md').read_text())
            self.assertTrue(claude.root.is_relative_to(roots.engine / 'claude'), claude.root)

    def test_relative_paths_cannot_escape_the_source(self):
        from sync_sources import BuildError, child
        with tempfile.TemporaryDirectory() as temp:
            for path in ('../outside', '/absolute', 'C:/outside', 'nested/../../outside', 'nested\\outside'):
                with self.subTest(path=path), self.assertRaises(BuildError):
                    child(Path(temp), path)


class NativeCatalogTest(unittest.TestCase):
    def test_repository_engine_catalogs_use_the_verified_local_tree(self):
        from description import CodexPlacement, EngineInstall, RepoSource
        from host_state import DeviceRoots, Host
        from sync_files import Files
        from sync_hosts import Apply
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            operation = Apply.__new__(Apply)
            operation.roots = DeviceRoots.current(root)
            operation.files = Files(root / 'backups')
            operation.catalogs = {}
            operation.claude = {}
            operation.built = {'interfaces': SimpleNamespace(root=root / 'built')}
            operation.setup = SimpleNamespace(
                packages={'interfaces': SimpleNamespace(source=RepoSource('interfaces'),
                                                         codex=CodexPlacement((EngineInstall(True),)))},
                repos={'interfaces': SimpleNamespace(url='https://github.com/example/interfaces.git', commit='a' * 40)})
            commands = []
            operation.command = lambda *args: commands.append(args)
            operation._catalogs()
            catalog = json.loads((operation.roots.agents_home / 'plugins/marketplace.json').read_text())
            self.assertEqual(catalog['plugins'], [{
                'name': 'interfaces',
                'source': {'source': 'local', 'path': './built'},
                'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_USE'},
                'category': 'Developer Tools'}])
            self.assertEqual(commands, [(Host.CODEX, 'plugin', 'marketplace', 'add', str(root), '--json')])

    def catalog_operation(self, root, catalogs, listed):
        """An Apply with nothing built, the engine catalogs that differ, and each catalog file listing `listed`."""
        from host_state import DeviceRoots
        from sync_files import Files
        from sync_hosts import Apply
        home = root / 'home'
        operation = Apply.__new__(Apply)
        operation.roots = DeviceRoots(home, home / '.codex', home / '.claude', home / '.agents', root / 'engine')
        operation.files = Files(root / 'backups')
        operation.built, operation.claude, operation.catalogs = {}, {}, catalogs
        paths = {'personal': operation.roots.agents_home / 'plugins/marketplace.json',
                 'personal-collections': operation.roots.engine / 'claude/.claude-plugin/marketplace.json',
                 'codex-aligned': operation.roots.claude_home / 'marketplaces/codex-aligned/.claude-plugin/marketplace.json'}
        for name, path in paths.items():
            path.parent.mkdir(parents=True)
            head = {'interface': {'displayName': 'Personal'}} if name == 'personal' else {'owner': {'name': 'Dotfiles'}}
            path.write_text(json.dumps({'name': name, **head, 'plugins': [
                {'name': plugin, 'source': f'./plugins/{plugin}/1', 'version': '1'} for plugin in listed]}))
        operation.commands = []
        operation.command = lambda *args: operation.commands.append(args)
        return operation, paths

    def test_a_claude_catalog_that_differs_keeps_only_its_managed_plugins_even_with_nothing_built(self):
        from host_state import Host
        with tempfile.TemporaryDirectory() as temp:
            operation, paths = self.catalog_operation(Path(temp), {(Host.CLAUDE, 'personal-collections'): frozenset({'kept'})},
                                                      ('kept', 'retired'))
            untouched = {name: paths[name].read_bytes() for name in ('personal', 'codex-aligned')}
            operation._catalogs()
            self.assertEqual([e['name'] for e in json.loads(paths['personal-collections'].read_text())['plugins']], ['kept'])
            self.assertEqual({name: paths[name].read_bytes() for name in untouched}, untouched,
                             'catalogs in step are left as they are')
            self.assertEqual(operation.commands, [(Host.CLAUDE, 'plugin', 'marketplace', 'add',
                                                   str(operation.roots.engine / 'claude'), '--scope', 'user', '--json')])

    def test_an_engine_catalog_already_right_on_disk_is_registered_again_when_its_fact_differs(self):
        from host_state import Host
        with tempfile.TemporaryDirectory() as temp:
            wanted = frozenset({'kept'})
            operation, paths = self.catalog_operation(Path(temp), {(Host.CODEX, 'personal'): wanted,
                                                                   (Host.CLAUDE, 'codex-aligned'): wanted}, ('kept',))
            before = {name: path.read_bytes() for name, path in paths.items()}
            operation._catalogs()
            self.assertEqual({name: path.read_bytes() for name, path in paths.items()}, before)
            self.assertEqual(operation.commands, [
                (Host.CODEX, 'plugin', 'marketplace', 'add', str(operation.roots.home), '--json'),
                (Host.CLAUDE, 'plugin', 'marketplace', 'add', str(operation.roots.claude_home / 'marketplaces/codex-aligned'),
                 '--scope', 'user', '--json')])

    def test_claude_registers_an_exact_checkout_without_treating_the_pin_as_a_branch(self):
        from dataclasses import replace
        from description import HostMarketplace, MarketplaceMode
        from host_state import DeviceRoots, FactKey, GitMarketplace, Host, Surface
        from sync_hosts import Apply
        from sync_sources import Sources
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            upstream = root / 'upstream'
            upstream.mkdir()
            git(upstream, 'init', '-b', 'main')
            git(upstream, 'config', 'user.name', 'Test')
            git(upstream, 'config', 'user.email', 'test@example.invalid')
            git(upstream, 'remote', 'add', 'origin', 'https://github.com/example/skills.git')
            (upstream / 'source').write_text('Pinned bytes')
            git(upstream, 'add', '.')
            git(upstream, 'commit', '-m', 'Pinned source')
            commit = git(upstream, 'rev-parse', 'HEAD')
            (upstream / 'source').write_text('Moved upstream')
            git(upstream, 'commit', '-am', 'Upstream moved')
            operation = Apply.__new__(Apply)
            operation.roots = replace(DeviceRoots.current(root), engine=root / 'engine')
            operation.builder = SimpleNamespace(sources=Sources(root / 'sources', seeds=(upstream,)))
            repo = SimpleNamespace(name='skills', url='https://github.com/example/skills.git', commit=commit,
                                   marketplace=lambda host: HostMarketplace('skills', MarketplaceMode.CHECKOUT))
            operation.setup = SimpleNamespace(repos={'skills': repo})
            operation.current = SimpleNamespace(changes=[SimpleNamespace(
                key=FactKey(Host.CLAUDE, Surface.MARKETPLACE, 'skills'),
                expected=GitMarketplace(repo.url, commit), entry=SimpleNamespace(name='skills'))])
            commands = []
            operation.command = lambda *args: commands.append(args)
            operation._marketplaces()
            checkout = operation.roots.engine / '.sync/checkouts/skills'
            self.assertEqual(commands, [(Host.CLAUDE, 'plugin', 'marketplace', 'add', str(checkout), '--scope', 'user', '--json')])
            self.assertEqual(git(checkout, 'rev-parse', 'HEAD'), commit)
            self.assertEqual(git(checkout, 'remote', 'get-url', 'origin'), 'https://github.com/example/skills.git')
            self.assertEqual((checkout / 'source').read_text(), 'Pinned bytes')


class RuntimeCatalogPluginTest(unittest.TestCase):
    def installs(self, changes, installed):
        """The plugins _plugins adds, for pdf@openai-primary-runtime held at `installed` and the plan's changes."""
        from description import CodexPlacement, MarketplaceInstall, Package, ToolNotes, Visibility
        from host_state import Fact, FactKey, Host, PluginValue, Surface
        from sync_hosts import Apply
        identity = 'pdf@openai-primary-runtime'
        key = FactKey(Host.CODEX, Surface.PLUGIN, identity)
        operation = Apply.__new__(Apply)
        operation.device = SimpleNamespace(hosts=(SimpleNamespace(host=Host.CODEX),))
        operation.current = SimpleNamespace(
            changes=tuple(SimpleNamespace(key=key, expected=PluginValue(True, pin)) for pin in changes),
            observations={Host.CODEX: SimpleNamespace(facts={key: Fact(key, PluginValue(True, installed))})})
        operation.packages = {'pdf'}
        operation.setup = SimpleNamespace(packages={'pdf': Package(
            'pdf', Visibility.PRIVATE, '', None, {}, ToolNotes.AUDIT,
            CodexPlacement((MarketplaceInstall('openai-primary-runtime', True),)), None)})
        added = []
        operation._install = lambda host, plugin: added.append(plugin)
        operation._enabled = lambda host, plugin, enabled: self.fail('an enabled plugin was enabled again')
        operation._plugins()
        return added

    def test_a_plugin_behind_its_pinned_catalog_is_added_again(self):
        from host_state import Released
        self.assertEqual(self.installs([Released('26.2')], Released('26.1')), ['pdf@openai-primary-runtime'])

    def test_an_unpinned_catalog_plugin_reported_at_a_version_is_never_re_added(self):
        from host_state import Released, Unpinned
        for changes in ([], [Unpinned()]):
            with self.subTest(changes=changes):
                self.assertEqual(self.installs(changes, Released('26.1')), [])

    def test_a_stale_on_in_the_file_is_turned_off_for_a_plugin_the_description_wants_off(self):
        from description import CodexPlacement, MarketplaceInstall, Package, ToolNotes, Visibility
        from host_state import Fact, FactKey, Host, PluginValue, Surface, Unpinned
        from sync_hosts import Apply
        identity = 'pdf@openai-curated-remote'
        key = FactKey(Host.CODEX, Surface.PLUGIN, identity)
        for file_on, expected in ((True, [(Host.CODEX, identity, False)]), (False, [])):
            with self.subTest(file_on=file_on):
                operation = Apply.__new__(Apply)
                operation.device = SimpleNamespace(hosts=(SimpleNamespace(host=Host.CODEX),))
                operation.current = SimpleNamespace(changes=(), observations={Host.CODEX: SimpleNamespace(
                    facts={key: Fact(key, PluginValue(False, Unpinned(), file_on))})})
                operation.packages = {'pdf'}
                operation.setup = SimpleNamespace(packages={'pdf': Package(
                    'pdf', Visibility.PRIVATE, '', None, {}, ToolNotes.AUDIT,
                    CodexPlacement((MarketplaceInstall('openai-curated-remote', False),)), None)})
                written = []
                operation._enabled = lambda *change: written.append(change)
                operation._plugins()
                self.assertEqual(written, expected)


class NativeGitMarketplaceTest(unittest.TestCase):
    def setUp(self):
        from description import HostMarketplace
        from host_state import DeviceRoots, FactKey, GitMarketplace, Host, Surface
        from sync_files import Files
        from sync_hosts import Apply
        from sync_sources import Sources
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.upstream = self.root / 'upstream'
        self.upstream.mkdir()
        git(self.upstream, 'init', '-b', 'main')
        git(self.upstream, 'config', 'user.name', 'Test')
        git(self.upstream, 'config', 'user.email', 'test@example.invalid')
        self.url = 'https://github.com/example/skills.git'
        git(self.upstream, 'remote', 'add', 'origin', self.url)
        (self.upstream / '.claude-plugin').mkdir()
        (self.upstream / '.claude-plugin/marketplace.json').write_text(json.dumps({'name': 'skills', 'plugins': []}))
        (self.upstream / 'source').write_text('Pinned bytes')
        git(self.upstream, 'add', '.')
        git(self.upstream, 'commit', '-m', 'Pinned source')
        self.pin = git(self.upstream, 'rev-parse', 'HEAD')
        (self.upstream / 'source').write_text('Upstream moved')
        git(self.upstream, 'commit', '-am', 'Upstream moved')
        self.operation = Apply.__new__(Apply)
        self.operation.roots = DeviceRoots.current(self.root)
        self.operation.builder = SimpleNamespace(sources=Sources(self.root / 'sources', seeds=(self.upstream,)))
        self.operation.files = Files(self.root / 'backups')
        self.spec = HostMarketplace('skills')
        self.repo = SimpleNamespace(name='skills', url=self.url, commit=self.pin, marketplace=lambda host: self.spec)
        self.operation.setup = SimpleNamespace(repos={'skills': self.repo})
        self.operation.current = SimpleNamespace(changes=[SimpleNamespace(
            key=FactKey(Host.CLAUDE, Surface.MARKETPLACE, 'skills'),
            expected=GitMarketplace(self.url, self.pin), entry=SimpleNamespace(name='skills'))])
        self.settings = self.operation.roots.claude_home / 'settings.json'
        self.registry = self.operation.roots.claude_home / 'plugins/known_marketplaces.json'
        self.cache = self.operation.roots.claude_home / 'plugins/marketplaces/skills'
        self.commands = []

        def command(*args):
            self.commands.append(args)
            self.register()

        self.operation.command = command

    def register(self):
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'clone', str(self.upstream), str(self.cache)], capture_output=True, check=True)
        git(self.cache, 'remote', 'set-url', 'origin', self.url)
        source = {'source': 'git', 'url': self.url, 'ref': 'main'}
        self.settings.write_text(json.dumps({'unmanaged': {'keep': True}, 'extraKnownMarketplaces': {
            'skills': {'source': source, 'future-setting': 7}}}))
        self.registry.write_text(json.dumps({'skills': {
            'source': source, 'installLocation': str(self.cache), 'lastUpdated': 'native-time', 'future-record': 9}}))

    def test_fresh_git_registration_pins_native_checkout_and_preserves_metadata(self):
        from host_state import Host
        self.operation._marketplaces()
        self.assertEqual(self.commands, [(Host.CLAUDE, 'plugin', 'marketplace', 'add', self.url, '--scope', 'user', '--json')])
        self.assertEqual(git(self.cache, 'rev-parse', 'HEAD'), self.pin)
        self.assertEqual((self.cache / 'source').read_text(), 'Pinned bytes')
        self.assertEqual(git(self.cache, 'status', '--porcelain'), '')
        self.assertEqual((self.cache / '.git/HEAD').read_text().strip(), self.pin)
        settings = json.loads(self.settings.read_bytes())
        registered = json.loads(self.registry.read_bytes())['skills']
        self.assertEqual(settings['unmanaged'], {'keep': True})
        self.assertEqual(settings['extraKnownMarketplaces']['skills']['future-setting'], 7)
        self.assertEqual(registered['future-record'], 9)
        self.assertEqual(registered['lastUpdated'], 'native-time')
        self.assertEqual(registered['source'], {'source': 'git', 'url': self.url, 'ref': self.pin})
        self.assertIs(settings['extraKnownMarketplaces']['skills']['autoUpdate'], False)
        self.assertIs(registered['autoUpdate'], False)
        before = [path.stat().st_mtime_ns for path in (self.settings, self.registry, self.cache / '.git/HEAD')]
        self.operation._marketplaces()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(before, [path.stat().st_mtime_ns for path in (self.settings, self.registry, self.cache / '.git/HEAD')])

    def test_existing_dirty_native_checkout_is_preserved_before_any_write(self):
        self.register()
        (self.cache / 'source').write_text('User changed this')
        before = self.settings.read_bytes(), self.registry.read_bytes(), git(self.cache, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(RuntimeError, 'local changes'):
            self.operation._marketplaces()
        self.assertEqual(self.commands, [])
        self.assertEqual((self.settings.read_bytes(), self.registry.read_bytes(), git(self.cache, 'rev-parse', 'HEAD')), before)
        self.assertEqual((self.cache / 'source').read_text(), 'User changed this')

    def test_existing_local_registration_is_not_relabelled_as_git(self):
        self.register()
        source = {'source': 'directory', 'path': str(self.cache)}
        for path, nested in ((self.settings, True), (self.registry, False)):
            data = json.loads(path.read_bytes())
            entries = data['extraKnownMarketplaces'] if nested else data
            entries['skills']['source'] = source
            path.write_text(json.dumps(data))
        before = self.settings.read_bytes(), self.registry.read_bytes(), git(self.cache, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(RuntimeError, 'source disagrees'):
            self.operation._marketplaces()
        self.assertEqual(self.commands, [])
        self.assertEqual((self.settings.read_bytes(), self.registry.read_bytes(), git(self.cache, 'rev-parse', 'HEAD')), before)

    def test_existing_registration_origin_name_and_path_must_agree(self):
        from unittest.mock import patch
        self.register()
        before = self.settings.read_bytes(), self.registry.read_bytes()
        for change in ('origin', 'name', 'path', 'source'):
            with self.subTest(change=change):
                if change == 'origin':
                    git(self.cache, 'remote', 'set-url', 'origin', 'https://github.com/elsewhere/skills.git')
                elif change == 'name':
                    (self.cache / '.claude-plugin/marketplace.json').write_text('{"name":"other"}')
                else:
                    record = json.loads(before[1])
                    if change == 'path':
                        record['skills']['installLocation'] = str(self.upstream)
                    else:
                        record['skills']['source']['ref'] = 'different'
                    self.registry.write_text(json.dumps(record))
                with patch.object(self.operation.builder.sources, 'checkout') as write:
                    with self.assertRaises(RuntimeError):
                        self.operation._marketplaces()
                    write.assert_not_called()
                self.assertEqual(self.commands, [])
                git(self.cache, 'remote', 'set-url', 'origin', self.url)
                (self.cache / '.claude-plugin/marketplace.json').write_bytes((self.upstream / '.claude-plugin/marketplace.json').read_bytes())
                self.registry.write_bytes(before[1])


class RehearsalTest(unittest.TestCase):
    def operation(self, root, expected):
        from host_state import FactKey, Host, Surface
        from sync_files import RehearsalFiles
        from sync_hosts import Rehearsal
        operation = object.__new__(Rehearsal)
        operation.roots = SimpleNamespace(claude_home=root)
        operation.device = SimpleNamespace(hosts=())
        operation.packages = set()
        operation.files = RehearsalFiles()
        operation.current = SimpleNamespace(changes=[SimpleNamespace(
            key=FactKey(Host.CLAUDE, Surface.SKILL, 'kit@personal-collections:dropped'), expected=expected)])
        return operation

    def test_a_settings_edit_runs_against_the_real_file_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'settings.json'
            path.write_text('{"skillOverrides":{"kit:dropped":"off"}}')
            before = path.read_bytes()
            self.operation(root, None).run()
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(sorted(item.name for item in root.iterdir()), ['settings.json'])

    def test_a_crash_in_a_settings_edit_reaches_the_caller(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'settings.json').write_text('{}')
            with self.assertRaises(AttributeError):
                self.operation(root, object()).run()


class SyncMainTest(unittest.TestCase):
    def test_a_candidate_stops_when_the_settings_rehearsal_crashes(self):
        from unittest import mock
        import sync
        device = mock.Mock()
        device.roots.installed_engine.return_value = None
        with mock.patch('sync.load'), mock.patch('sync.Device.current', return_value=device), \
                mock.patch('sync.plan'), mock.patch('accepted_installations.AcceptedInstallations'), \
                mock.patch('configuration.validate_runtime'), mock.patch('sync_hosts.Rehearsal') as rehearsal:
            rehearsal.return_value.run.side_effect = AttributeError('mode is None')
            with self.assertRaises(AttributeError):
                sync.main(['--candidate'])

    def test_a_blocked_apply_prints_the_rows_that_block_it_and_an_applied_one_only_its_result(self):
        from unittest import mock
        import sync
        device = mock.Mock()
        device.roots.installed_engine.return_value = None
        applied = SimpleNamespace(blocked=(), changes=('a',), unmanaged=(), expected=(), held=(), result='applied',
                                  exit_code=0)
        summary = 'Managed changes: 1; blocking issues: 0; unmanaged differences left untouched: 0.\n'
        for blocked, outcome, code, printed in (
                (('row',), RuntimeError('Sync is blocked'), 3, 'plan  kit  stale-lock  run lock_setup.py\n'),
                ((), (applied, None), 0, 'applied  kit  stale-lock  run lock_setup.py\n' + summary)):
            current = SimpleNamespace(blocked=blocked, changes=('a',), result='plan')
            with self.subTest(blocked=blocked), mock.patch('sync.load'), \
                    mock.patch('sync.Device.current', return_value=device), mock.patch('sync.plan', return_value=current), \
                    mock.patch('sync.render', side_effect=lambda result, notes: f'{result}  kit  stale-lock  run lock_setup.py\n'), \
                    mock.patch('sync.apply', side_effect=[outcome]), \
                    redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
                self.assertEqual(sync.main(['--apply']), code)
                self.assertEqual(stdout.getvalue(), printed)

    def test_with_ending_the_sync_prints_as_without_it_then_records_its_status_and_exits_0(self):
        from unittest import mock
        import sync

        class Output(io.StringIO):
            def __init__(self, ending):
                super().__init__()
                self.ending, self.after_the_record = ending, []

            def write(self, text):
                self.after_the_record.append(self.ending.exists())
                return super().write(text)

        device = mock.Mock()
        device.roots.installed_engine.return_value = None
        with tempfile.TemporaryDirectory() as temp:
            ending = Path(temp) / 'ending'
            for code, stop in ((0, None), (1, None), (2, None), (3, RuntimeError('a description that cannot be read'))):
                current = SimpleNamespace(blocked=(), changes=('a',) * code, unmanaged=(), expected=(), result='plan',
                                          exit_code=code)
                printed = []
                for flags, returned in (([], code), (['--ending', str(ending)], 0)):
                    stdout, stderr = Output(ending), Output(ending)
                    with self.subTest(code=code, flags=flags), mock.patch('sync.load', side_effect=stop), \
                            mock.patch('sync.Device.current', return_value=device), \
                            mock.patch('sync.plan', return_value=current), \
                            mock.patch('sync.render', return_value='rows\n'), \
                            redirect_stdout(stdout), redirect_stderr(stderr):
                        self.assertEqual(sync.main(['--check', *flags]), returned)
                    printed.append((stdout.getvalue(), stderr.getvalue()))
                    self.assertNotIn(True, stdout.after_the_record + stderr.after_the_record)
                self.assertEqual(printed[0], printed[1])
                self.assertIn('Sync stopped: ' if stop else 'Managed changes: ', ''.join(printed[0]))
                self.assertEqual(json.loads(ending.read_bytes()), {'status': code})
                ending.unlink()

    def test_with_ending_a_sync_that_crashes_records_nothing(self):
        from unittest import mock
        import sync
        import sync_ending
        with tempfile.TemporaryDirectory() as temp:
            ending = Path(temp) / 'ending'
            with mock.patch('sync.load', side_effect=KeyError('a step that crashes')), self.assertRaises(KeyError):
                sync.main(['--check', sync_ending.FLAG, str(ending)])
            self.assertFalse(ending.exists())

    def test_a_native_command_past_its_time_limit_stops_the_sync_with_3(self):
        from unittest import mock
        import sync
        device = mock.Mock()
        device.roots.installed_engine.return_value = None
        late = subprocess.TimeoutExpired(['codex', 'plugin', 'add'], 180, stderr=b'still adding\n')
        with mock.patch('sync.load'), mock.patch('sync.Device.current', return_value=device), \
                mock.patch('sync.plan', return_value=SimpleNamespace(blocked=(), changes=('a',), result='plan')), \
                mock.patch('sync.apply', side_effect=late), redirect_stdout(io.StringIO()), \
                redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(sync.main(['--apply']), 3)
        self.assertEqual(stderr.getvalue(), f'Sync stopped: {late}\n')

    def test_a_failed_git_command_stops_the_sync_with_3_on_one_line_that_starts_with_its_own_words(self):
        from unittest import mock
        import invocation_overrides
        import sync

        with tempfile.TemporaryDirectory() as temp:
            command = ['rev-parse', '--verify', 'main^{commit}']
            said = ' '.join(subprocess.run(['git', '-C', temp, *command], capture_output=True,
                                           text=True).stderr.splitlines())
            with mock.patch('sync.load', side_effect=lambda *args: invocation_overrides.git_line(Path(temp), *command)), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(sync.main(['--apply']), 3)
        self.assertRegex(stderr.getvalue(),
                         rf'^Sync stopped: {re.escape(said)} \(exit 128 from git .+ rev-parse --verify main\^{{commit}}\)\n$')


if __name__ == '__main__':
    unittest.main()
