import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FileSafetyTest(unittest.TestCase):
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
        from host_state import Host, Platform, SkillMode
        package = Package('swift-lsp', Visibility.PRIVATE, '', None, {'swift': SkillMode.EXPLICIT}, ToolNotes.AUDIT,
                          None, ClaudePlacement((MarketplaceInstall('claude-plugins-official', True),)),
                          frozenset({Platform.MACOS}))
        setup = Setup(Roots(Path('/public'), None), {'swift-lsp': package}, {}, {}, {}, None, (), frozenset(), {})
        self.assertTrue(setup.for_platform(Platform.MACOS).packages['swift-lsp'].claude.installs[0].enabled)
        for platform in (Platform.WINDOWS, Platform.LINUX):
            filtered = setup.for_platform(platform).packages['swift-lsp']
            self.assertFalse(filtered.claude.installs[0].enabled)
            self.assertEqual(filtered.modes, {})

    def test_disabled_missing_plugin_is_not_an_install_request(self):
        from drift import _classify
        from host_state import FactKey, Host, NoteKind, Observation, PluginValue, Surface, Unpinned
        observation = Observation(Host.CLAUDE, 'test', {}, frozenset(), frozenset(), ())
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
                                      frozenset({key.subject}), frozenset(), ())
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
                                      frozenset({key.subject}), frozenset(), ())
            state = SimpleNamespace(host=Host.CODEX, project=lambda setup: {}, observe=lambda request: observation)
            setup = Setup(Roots(root / 'public', None), {}, {}, {}, {}, None, (), frozenset(), {},
                          {Platform.LINUX: frozenset({key, gone, absent_host})})
            result = plan(setup, Device(Platform.LINUX, DeviceRoots.current(root), (state,), ()))
            self.assertEqual((result.changes, result.unmanaged, len(result.expected), result.exit_code), ((), (), 1, 0))
            self.assertEqual([(n.kind, n.subject, n.detail) for n in result.result.notes], [
                (NoteKind.DEVICE_LOCAL, 'expected unmanaged items', '1 named in devices/linux.toml; sync leaves them alone'),
                (NoteKind.RESIDUE, gone.subject, 'listed in devices/linux.toml but no longer an unmanaged difference here')])


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


class PinnedBuildTest(unittest.TestCase):
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
            operation.catalogs_needed = set()
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


class SyncMainTest(unittest.TestCase):
    def test_a_blocked_apply_prints_the_rows_that_block_it(self):
        from unittest import mock
        import sync
        device = mock.Mock()
        device.roots.installed_engine.return_value = None
        current = SimpleNamespace(blocked=('row',), changes=(), result='plan')
        with mock.patch('sync.load'), mock.patch('sync.Device.current', return_value=device), \
                mock.patch('sync.plan', return_value=current), \
                mock.patch('sync.render', return_value='attention\n  kit  stale-lock  run lock_setup.py\n'), \
                mock.patch('sync.apply', side_effect=RuntimeError('Sync is blocked')), \
                mock.patch('sys.stdout') as stdout, mock.patch('sys.stderr'):
            self.assertEqual(sync.main(['--apply']), 3)
        self.assertIn('stale-lock  run lock_setup.py', ''.join(call.args[0] for call in stdout.write.call_args_list))


if __name__ == '__main__':
    unittest.main()
