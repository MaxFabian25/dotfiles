import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest


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
        from description import HostMarketplace
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
            operation.roots = DeviceRoots.current(root)
            operation.builder = SimpleNamespace(sources=Sources(root / 'sources', seeds=(upstream,)))
            repo = SimpleNamespace(name='skills', url='https://github.com/example/skills.git', commit=commit,
                                   marketplace=lambda host: HostMarketplace('skills'))
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


if __name__ == '__main__':
    unittest.main()
