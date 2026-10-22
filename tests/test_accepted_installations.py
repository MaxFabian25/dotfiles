from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from codex_host import CodexState
from description import (Adaptation, AlignedInstall, ClaudePlacement, CodexPlacement, EngineInstall, Hooks, LocalSource, Lock, LockedTree,
                         LooseSkill, NoteRule, Package, PluginRoles, Repo, RepoSource, Roots, Rules, Setup, SkillRoot,
                         ToolNotes, Visibility, intent)
from device import Device
from device_sync import apply, plan
from host_observation import Observer, Unread
from host_state import (Approved, DeviceRoots, EngineMarketplace, FactKey, FileValue, Host, InstallDetail,
                        Platform, PluginValue, Released, SkillMode, Surface, TreeValue, Unknown, bytes_digest)
from sync_files import ConcurrentEdit
from sync_sources import BuildError, Builder, Sources


class DiskHost:
    host = Host.CODEX
    project = CodexState.project

    def __init__(self, roots):
        self.roots, self.installed, self.approved = roots, {}, True

    def observe(self, request):
        seen = Observer(self.host, self.roots)
        if (self.roots.agents_home / 'plugins/marketplace.json').is_file():
            seen.add(Surface.MARKETPLACE, 'personal', EngineMarketplace())
        for identity, (version, directory) in self.installed.items():
            seen.add(Surface.PLUGIN, identity, PluginValue(True, Released(version)),
                     detail=InstallDetail(version, directory))
            if directory.is_dir():
                seen.add(Surface.TREE, identity, TreeValue(self.roots.tree_digest(directory)))
            if self.approved:
                seen.add(Surface.HOOK, identity, Approved(1))
        for path in (self.roots.codex_home / 'agents').glob('*.toml'):
            seen.add(Surface.AGENT, path.name, FileValue(self.roots.file_digest(path)))
        for directory in (self.roots.codex_home / 'skills').glob('*'):
            if directory.is_dir():
                seen.add(Surface.LOOSE_SKILL, self.roots.tokens(directory), FileValue(self.roots.tree_digest(directory)))
        rules = self.roots.codex_home / 'AGENTS.md'
        if rules.is_file():
            seen.add(Surface.RULES, 'AGENTS.md', FileValue(bytes_digest(rules.read_bytes())))
        return seen.finish('disk test host', set(self.installed))


class InstallationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        home = self.root / 'home'
        home.mkdir()
        self.roots = DeviceRoots(home, home / '.codex', home / '.claude', home / '.agents', self.root / 'engine')
        self.public = self.root / 'public'
        source = self.public / 'owned/kit'
        (source / '.codex-plugin').mkdir(parents=True)
        (source / '.codex-plugin/plugin.json').write_text('{"name":"kit","version":"1.0.0"}')
        (source / 'skills/one').mkdir(parents=True)
        (source / 'skills/one/SKILL.md').write_text('---\nname: one\ndescription: Use a tool.\n---\nUse Read now.\n')
        (source / 'ordinary.txt').write_text('ordinary release bytes\n')
        (source / 'agents').mkdir()
        self.role = b'model = "example-model"\n'
        (source / 'agents/reviewer.toml').write_bytes(self.role)
        self.source = source
        loose_source = self.public / 'owned/loose'
        loose_source.mkdir()
        (loose_source / 'SKILL.md').write_text('A loose skill.\n')
        package = Package('kit', Visibility.PUBLIC, '', LocalSource('owned/kit', self.roots.tree_digest(source)),
                          {}, ToolNotes.APPLY, CodexPlacement((EngineInstall(True),), roles=PluginRoles('agents')), None)
        loose = LooseSkill('loose', Visibility.PUBLIC, '', LocalSource('owned/loose', self.roots.tree_digest(loose_source)),
                           {SkillRoot.CODEX: True})
        locks = {'kit': Lock(intent(package, {}), {Host.CODEX: LockedTree('1.0.0', self.roots.tree_digest(source))},
                             {'reviewer.toml': bytes_digest(self.role)}),
                 'loose': Lock(intent(loose, {}), placements={SkillRoot.CODEX: self.roots.tree_digest(loose_source)})}
        self.setup = Setup(Roots(self.public / 'setup', None), {'kit': package}, {}, {'loose': loose}, {},
                           Rules(b'Managed rules.\n'), (NoteRule('Read', 'use the local reader'),), frozenset(), locks)
        self.host = DiskHost(self.roots)
        self.device = Device(Platform.LINUX, self.roots, (self.host,), ())
        self.calls = []
        self.interrupt = None
        self.seeds = ()

        def command(operation, host, *args):
            self.calls.append((host, args))
            if args[:2] == ('plugin', 'add'):
                release = operation.built['kit']
                destination = self.roots.codex_home / 'plugins/cache/personal/kit' / release.version
                if destination.exists():
                    shutil.rmtree(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(release.root, destination)
                self.host.installed[args[2]] = (release.version, destination)
                if self.interrupt:
                    self.interrupt(destination)
            return ''
        self.addCleanup(patch.stopall)
        patch('sync_hosts.Apply.command', command).start()

    def run_apply(self, current=None):
        return apply(self.setup, self.device, current or plan(self.setup, self.device),
                     builder=Builder(self.setup, self.roots, self.roots.engine,
                                     sources=Sources(self.roots.engine / 'sources', seeds=self.seeds)))[0]

    def installed(self):
        return self.host.installed['kit@personal'][1]

    def receipt_paths(self):
        return sorted((self.roots.codex_home / 'dotfiles/accepted').glob('*.json'))

    def snapshot(self):
        return {p.relative_to(self.root): (p.lstat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                for p in self.root.rglob('*')}

    def test_install_then_strict_noop_and_complete_manifest(self):
        after = self.run_apply()
        self.assertEqual(after.exit_code, 0)
        self.assertIn('use the local reader', (self.installed() / 'skills/one/SKILL.md').read_text())
        self.assertEqual((self.roots.codex_home / 'agents/reviewer.toml').read_bytes(), self.role)
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A loose skill.\n')
        self.assertEqual((self.roots.codex_home / 'AGENTS.md').read_text(), 'Managed rules.\n')
        receipt = next(json.loads(p.read_bytes()) for p in self.receipt_paths()
                       if json.loads(p.read_bytes())['identity'].startswith('plugin:'))
        self.assertEqual(receipt['accepted']['image']['entries']['ordinary.txt'],
                         {'kind': 'file', 'sha256': str(bytes_digest(b'ordinary release bytes\n'))})
        snapshot = self.snapshot()
        with patch('sync_sources.Builder', side_effect=AssertionError('no-op built sources')):
            result, backup = apply(self.setup, self.device, plan(self.setup, self.device))
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(backup)
        self.assertEqual(self.snapshot(), snapshot)

    def test_preplan_ordinary_edit_is_blocked_despite_global_overlay_receipt(self):
        self.run_apply()
        global_receipt = self.roots.codex_home / 'skill-updates/invocation-overrides/state.json'
        global_receipt.parent.mkdir(parents=True)
        global_receipt.write_text('{"schemaVersion":1,"files":{}}')
        ordinary = self.installed() / 'ordinary.txt'
        ordinary.write_text('uncommitted user work\n')
        before = self.snapshot()
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 2)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(ordinary.read_text(), 'uncommitted user work\n')

    def test_repository_receipt_preserves_the_resolved_pin_and_replayable_selection(self):
        from tests.test_sync import git
        repo = self.root / 'upstream'
        shutil.copytree(self.source, repo / 'plugins/kit')
        (repo / '.codex-plugin').mkdir()
        (repo / '.codex-plugin/marketplace.json').write_text(json.dumps(
            {'name': 'origin', 'plugins': [{'name': 'kit', 'source': './plugins/kit'}]}))
        git(repo, 'init', '-b', 'main')
        git(repo, 'config', 'user.name', 'Test')
        git(repo, 'config', 'user.email', 'test@example.invalid')
        git(repo, 'remote', 'add', 'origin', 'https://example.invalid/kit.git')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'Pinned source')
        commit = git(repo, 'rev-parse', 'HEAD')
        repository = Repo('origin', Visibility.PUBLIC, '', 'https://example.invalid/kit.git', commit, None, None)
        self.seeds = (repo,)
        for path, selection in (('plugins/kit', {'path': 'plugins/kit'}), (None, {'marketplace_plugin': 'kit'})):
            with self.subTest(selection=selection):
                package = replace(self.setup.packages['kit'], source=RepoSource('origin', path))
                repositories = {'origin': repository}
                lock = replace(self.setup.lock['kit'], intent=intent(package, repositories))
                self.setup = replace(self.setup, packages={'kit': package}, repos=repositories,
                                     lock={**self.setup.lock, 'kit': lock})
                self.assertEqual(self.run_apply().exit_code, 0)
                receipt = next(json.loads(p.read_bytes()) for p in self.receipt_paths()
                               if json.loads(p.read_bytes())['identity'].startswith('plugin:'))
                self.assertEqual(receipt['accepted']['provenance']['repository'], {
                    'url': 'https://example.invalid/kit.git', 'commit': commit, 'selection': selection})
                before = self.snapshot()
                self.assertEqual(self.run_apply().exit_code, 0)
                self.assertEqual(self.snapshot(), before)

    def test_role_rules_and_loose_preplan_edits_are_protected(self):
        self.run_apply()
        paths = [self.roots.codex_home / 'agents/reviewer.toml', self.roots.codex_home / 'AGENTS.md',
                 self.roots.codex_home / 'skills/loose/SKILL.md']
        for path in paths:
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(b'local work\n')
                current = plan(self.setup, self.device)
                self.assertEqual(current.exit_code, 2)
                with self.assertRaises(RuntimeError):
                    self.run_apply(current)
                self.assertEqual(path.read_bytes(), b'local work\n')
                path.write_bytes(original)

    def test_receiptless_exact_content_is_independently_adopted_without_content_writes(self):
        self.run_apply()
        for path in self.receipt_paths():
            path.unlink()
        installed = [p for p in self.roots.home.rglob('*') if p.is_file()]
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in installed}
        self.calls.clear()
        result = self.run_apply()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual({p: (p.stat().st_mtime_ns, p.read_bytes()) for p in installed}, before)
        self.assertEqual(len(self.receipt_paths()), 4)
        self.assertEqual(self.calls, [])

    def test_receiptless_mismatch_refuses_all_live_changes(self):
        self.run_apply()
        for path in self.receipt_paths():
            path.unlink()
        ordinary = self.installed() / 'ordinary.txt'
        ordinary.write_text('unknown historical bytes\n')
        (self.roots.codex_home / 'AGENTS.md').unlink()
        self.calls.clear()
        with self.assertRaisesRegex(ConcurrentEdit, 'independently pinned baseline'):
            self.run_apply()
        self.assertEqual(ordinary.read_text(), 'unknown historical bytes\n')
        self.assertFalse((self.roots.codex_home / 'AGENTS.md').exists())
        self.assertEqual(self.calls, [])

    def test_edit_after_prepare_is_rechecked_before_mutation(self):
        from sync_hosts import Apply
        self.run_apply()
        self.setup = replace(self.setup, rules=Rules(b'Updated reviewed rules.\n'))
        rules = self.roots.codex_home / 'AGENTS.md'
        original = Apply.run

        def race(operation):
            rules.write_bytes(b'new user edit\n')
            return original(operation)
        with patch.object(Apply, 'run', race), self.assertRaises(ConcurrentEdit):
            self.run_apply()
        self.assertEqual(rules.read_bytes(), b'new user edit\n')

    def test_pristine_pending_retry_finishes_overlay_without_reinstalling(self):
        def crash(path):
            raise RuntimeError('interrupted after native output')
        self.interrupt = crash
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.run_apply()
        self.assertNotIn('use the local reader', (self.installed() / 'skills/one/SKILL.md').read_text())
        self.interrupt = None
        self.calls.clear()
        result = self.run_apply()
        self.assertEqual(result.exit_code, 0)
        self.assertIn('use the local reader', (self.installed() / 'skills/one/SKILL.md').read_text())
        self.assertFalse(any(args[:2] == ('plugin', 'add') for _, args in self.calls))

    def test_unknown_partial_native_output_is_preserved_on_retry(self):
        def crash(path):
            (path / 'ordinary.txt').write_text('partial native output\n')
            raise RuntimeError('interrupted')
        self.interrupt = crash
        with self.assertRaises(RuntimeError):
            self.run_apply()
        before = self.snapshot()
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 2)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.installed() / 'ordinary.txt').read_text(), 'partial native output\n')

    def test_catalog_only_drift_reconstructs_entries_without_reinstalling(self):
        self.run_apply()
        catalog = self.roots.agents_home / 'plugins/marketplace.json'
        catalog.unlink()
        self.calls.clear()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(json.loads(catalog.read_bytes())['plugins'][0]['name'], 'kit')
        self.assertFalse(any(args[:2] == ('plugin', 'add') for _, args in self.calls))

    def test_outside_home_source_is_staged_and_edited_cache_is_refused(self):
        self.run_apply()
        catalog = self.roots.agents_home / 'plugins/marketplace.json'
        source = json.loads(catalog.read_bytes())['plugins'][0]['source']
        self.assertEqual(source['source'], 'local')
        staged = self.roots.home / source['path']
        self.assertEqual((staged / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        (staged / 'ordinary.txt').write_text('edited staging cache\n')
        catalog.unlink()
        with self.assertRaisesRegex(ConcurrentEdit, 'immutable source'):
            self.run_apply()
        self.assertEqual((staged / 'ordinary.txt').read_text(), 'edited staging cache\n')

    def test_unowned_extra_content_and_linked_working_copy_are_blocked(self):
        self.run_apply()
        extra = self.installed() / 'node_modules/local/index.js'
        extra.parent.mkdir(parents=True)
        extra.write_text('user owned dependency\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 2)
        with self.assertRaises(RuntimeError):
            self.run_apply()
        self.assertEqual(extra.read_text(), 'user owned dependency\n')
        shutil.rmtree(self.installed() / 'node_modules')
        loose = self.roots.codex_home / 'skills/loose'
        moved = self.root / 'working-copy'
        loose.rename(moved)
        loose.symlink_to(moved, target_is_directory=True)
        self.assertEqual(plan(self.setup, self.device).exit_code, 2)
        self.assertTrue(loose.is_symlink())

    def test_approval_only_plan_is_not_success_and_does_not_write(self):
        self.run_apply()
        package = self.setup.packages['kit']
        package = replace(package, codex=replace(package.codex, hooks=Hooks.TRUSTED))
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}), hooks=1)
        self.setup = replace(self.setup, packages={'kit': package}, lock={**self.setup.lock, 'kit': lock})
        self.run_apply()
        self.host.approved = False
        before = self.snapshot()
        current = plan(self.setup, self.device)
        self.assertEqual([d.kind.value for d in current.blocked], ['approval'])
        self.assertEqual(current.exit_code, 2)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertEqual(self.snapshot(), before)

    def test_toml_fields_share_the_owner_and_preserve_unmanaged_values(self):
        config_source = self.public / 'config/mise/config.toml'
        config_source.parent.mkdir(parents=True)
        config_source.write_text('[tools]\nnode = "24"\n')
        live = self.roots.home / '.config/mise/config.toml'
        live.parent.mkdir(parents=True)
        live.write_text('# keep me\n[env]\nSECRET = "test-only-local"\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('SECRET = "test-only-local"', live.read_text())
        self.assertIn('node = "24"', live.read_text())
        live.write_text(live.read_text().replace('node = "24"', 'node = "20"'))
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 2)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertIn('node = "20"', live.read_text())

    def test_receiptless_mismatched_toml_field_is_not_overwritten(self):
        config_source = self.public / 'config/mise/config.toml'
        config_source.parent.mkdir(parents=True)
        config_source.write_text('[tools]\nnode = "24"\n')
        live = self.roots.home / '.config/mise/config.toml'
        live.parent.mkdir(parents=True)
        live.write_text('[tools]\nnode = "20"\n[env]\nSECRET = "test-only-local"\n')
        with self.assertRaises(ConcurrentEdit):
            self.run_apply()
        self.assertEqual(live.read_text(), '[tools]\nnode = "20"\n[env]\nSECRET = "test-only-local"\n')
        self.assertEqual(self.calls, [])

    def test_accepted_adaptation_checks_without_its_clone_or_builder(self):
        from tests.test_sync import git
        repo = self.root / 'adaptation'
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        git(repo, 'config', 'user.name', 'Test')
        git(repo, 'config', 'user.email', 'test@example.invalid')
        git(repo, 'remote', 'add', 'origin', 'https://example.invalid/adaptation.git')
        (repo / 'adapted.txt').write_text('base\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'Base')
        base = git(repo, 'rev-parse', 'HEAD')
        (repo / 'adapted.txt').write_text('adapted\n')
        git(repo, 'commit', '-am', 'Adapted')
        adapted = git(repo, 'rev-parse', 'HEAD')
        (self.source / 'adapted.txt').write_text('base\n')
        package = self.setup.packages['kit']
        package = replace(package, source=replace(package.source, digest=self.roots.tree_digest(self.source)),
                          codex=replace(package.codex, adaptation=Adaptation('https://example.invalid/adaptation.git', base, adapted)))
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}),
                       trees={Host.CODEX: LockedTree('1.0.0', self.roots.tree_digest(self.source))})
        self.setup = replace(self.setup, packages={'kit': package}, lock={**self.setup.lock, 'kit': lock})
        self.seeds = (repo,)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.installed() / 'adapted.txt').read_text(), 'adapted\n')
        shutil.rmtree(self.roots.engine / 'harness-adaptations')
        before = self.snapshot()
        with patch('sync_sources.Builder', side_effect=AssertionError('check built sources')):
            current = plan(self.setup, self.device)
            self.assertEqual(current.exit_code, 0)
            self.assertEqual(apply(self.setup, self.device, current), (current, None))
        self.assertEqual(self.snapshot(), before)

    def test_pending_before_file_write_retries_only_accepted_content(self):
        from sync_files import Files
        self.run_apply()
        self.setup = replace(self.setup, rules=Rules(b'New reviewed rules.\n'))
        with patch.object(Files, 'write', side_effect=RuntimeError('before file mutation')):
            with self.assertRaisesRegex(RuntimeError, 'before file mutation'):
                self.run_apply()
        self.assertEqual((self.roots.codex_home / 'AGENTS.md').read_bytes(), b'Managed rules.\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.roots.codex_home / 'AGENTS.md').read_bytes(), b'New reviewed rules.\n')

    def test_pending_target_finishes_with_receipt_only(self):
        from accepted_installations import AcceptedInstallations
        self.run_apply()
        self.setup = replace(self.setup, rules=Rules(b'New reviewed rules.\n'))
        save = AcceptedInstallations._save

        def crash(owner, artifact, accepted, pending=None):
            if artifact.identity.startswith('rules:') and pending is None:
                raise RuntimeError('before acceptance')
            return save(owner, artifact, accepted, pending)
        with patch.object(AcceptedInstallations, '_save', crash), self.assertRaisesRegex(RuntimeError, 'before acceptance'):
            self.run_apply()
        path = self.roots.codex_home / 'AGENTS.md'
        before = path.stat().st_mtime_ns
        self.assertEqual(path.read_bytes(), b'New reviewed rules.\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_interrupted_tree_swap_recovers_its_recorded_stage(self):
        self.run_apply()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        rename = Path.rename
        failed = False

        def crash(path, target):
            nonlocal failed
            if path.name == 'staged' and not failed:
                failed = True
                raise RuntimeError('between tree renames')
            return rename(path, target)
        with patch.object(Path, 'rename', crash), self.assertRaisesRegex(RuntimeError, 'between tree renames'):
            self.run_apply()
        self.assertFalse(self.installed().exists())
        self.calls.clear()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (self.installed() / 'skills/one/SKILL.md').read_text())
        self.assertFalse(any(args[:2] == ('plugin', 'add') for _, args in self.calls))

    def _interrupted_claude_install(self, *, aligned=False, implicit=False):
        from claude_host import ClaudeState
        from sync_sources import Built

        marketplace_name = 'codex-aligned' if aligned else 'personal-collections'
        plugin = 'kit@' + marketplace_name
        skill = self.source / 'skills/one/SKILL.md'
        if not implicit:
            skill.write_text(skill.read_text().replace('description: Use a tool.',
                                                       'description: Use a tool.\ndisable-model-invocation: true'))
        if aligned:
            alignment = self.source / '.claude-plugin/codex-alignment.json'
            alignment.parent.mkdir()
            alignment.write_text('{"excludedSkillNames": []}')
        package = replace(self.setup.packages['kit'], source=LocalSource('owned/kit', self.roots.tree_digest(self.source)), codex=None,
                          claude=ClaudePlacement((AlignedInstall(True) if aligned else EngineInstall(True),)),
                          modes={} if implicit else {'one': SkillMode.EXPLICIT})
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}),
                       trees={Host.CLAUDE: LockedTree('1.0.0', self.roots.tree_digest(self.source))}, roles={})
        self.setup = replace(self.setup, packages={'kit': package}, loose={}, rules=None, lock={'kit': lock})
        self.device = Device(Platform.WINDOWS, self.roots, (ClaudeState(self.roots, 'claude'),), ())
        marketplace = (self.roots.claude_home / 'marketplaces/codex-aligned' if aligned
                       else self.roots.engine / 'claude')
        self.roots.claude_home.mkdir(parents=True)
        (self.roots.claude_home / 'settings.json').write_text(json.dumps({
            'extraKnownMarketplaces': {marketplace_name: {'source': {
                'source': 'directory', 'path': str(marketplace)}}}}))
        inventory = []

        def observe_command(command, **_):
            if command[-1] == '--version':
                return 'claude-test\n'
            if command[-4:] == ['plugin', 'list', '--available', '--json']:
                return json.dumps({'installed': inventory, 'available': []})
            raise AssertionError(command)

        def native_command(operation, host, *args):
            self.calls.append((host, args))
            if args[:2] in (('plugin', 'install'), ('plugin', 'update')):
                release = operation.claude[plugin]
                destination = self.roots.claude_home / 'plugins/cache' / marketplace_name / 'kit' / release.version
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(release.root, destination, dirs_exist_ok=True)
                inventory[:] = [{'id': plugin, 'scope': 'user', 'enabled': True,
                                 'version': release.version, 'installPath': str(destination)}]
                if self.interrupt:
                    self.interrupt(destination)
            return ''

        def build_claude(builder, package, codex, *, verify=True):
            version = builder.setup.lock['kit'].trees[Host.CLAUDE].version
            release = marketplace / 'plugins/kit' / version
            release.parent.mkdir(parents=True, exist_ok=True)
            if release.exists():
                shutil.rmtree(release)
            shutil.copytree(codex.root, release)
            return {plugin: Built('kit', release, version, builder.roots.tree_digest(release))}

        rename = Path.rename
        failed = False

        def crash(path, target):
            nonlocal failed
            if path.name == 'staged' and not failed:
                failed = True
                raise RuntimeError('between tree renames')
            return rename(path, target)

        patch('claude_host.run', observe_command).start()
        patch('sync_hosts.Apply.command', native_command).start()
        patch.object(Builder, 'claude', build_claude).start()
        if aligned:
            self.assertEqual(self.run_apply().exit_code, 0)
            (self.source / 'ordinary.txt').write_text('revised release bytes\n')
            package = replace(package, source=LocalSource('owned/kit', self.roots.tree_digest(self.source)))
            lock = replace(lock, intent=intent(package, {}),
                           trees={Host.CLAUDE: LockedTree('1.0.0', self.roots.tree_digest(self.source))})
            self.setup = replace(self.setup, packages={'kit': package}, lock={'kit': lock})
        with patch.object(Path, 'rename', crash), self.assertRaisesRegex(RuntimeError, 'between tree renames'):
            self.run_apply()
        destination = self.roots.claude_home / 'plugins/cache' / marketplace_name / 'kit/1.0.0'
        self.assertFalse(destination.exists())
        return destination

    def _pending_claude_receipt(self):
        return next(path for path in self.receipt_paths()
                    if json.loads(path.read_bytes())['identity'] == 'plugin:claude:kit@personal-collections')

    def _installed_claude(self):
        destination = self._interrupted_claude_install()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.calls.clear()
        return destination

    def test_claude_runtime_metadata_is_not_owned_and_exact_adoption_preserves_it(self):
        destination = self._installed_claude()
        leases = destination / '.in_use'
        leases.mkdir()
        (leases / '17').write_text('{"pid":17,"procStart":"Thu Oct 15 12:30:00 2026"}')
        (leases / '29.tmp.0123abcd').write_text('{"pid":29,"procStartFt":"134044866000000000"}')
        (destination / '.orphaned_at').write_text('1792091304500')
        before = self.snapshot()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calls, [])
        receipt = self._pending_claude_receipt()
        receipt.unlink()
        self.assertEqual(self.run_apply().exit_code, 0)
        accepted = json.loads(receipt.read_bytes())['accepted']['image']['entries']
        self.assertNotIn('.in_use', accepted)
        self.assertNotIn('.orphaned_at', accepted)
        self.assertEqual((leases / '17').read_text(), '{"pid":17,"procStart":"Thu Oct 15 12:30:00 2026"}')
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertEqual(self.calls, [])

    def test_claude_malformed_runtime_metadata_blocks_without_writes(self):
        destination = self._installed_claude()
        cases = [
            ('.in_use', b'17'),
            ('.orphaned_at', None),
            ('.orphaned_at', b''),
            ('.orphaned_at', b'1792091304500\n'),
            ('.orphaned_at', b'8640000000000001'),
            ('.in_use/notes.txt', b'{"pid":17}'),
            ('.in_use/17/child', b'{"pid":17}'),
            ('.in_use/17', b'not json'),
            ('.in_use/17', '{"pid":17}'.encode('utf-16')),
            ('.in_use/17', b'{}'),
            ('.in_use/17', b'{"pid":18}'),
            ('.in_use/17', b'{"pid":true}'),
            ('.in_use/17', b'{"pid":17,"procStart":17}'),
            ('.in_use/17', b'{"pid":17,"procStartFt":null}'),
            ('.in_use/17', b'{"pid":17,"other":"user content"}'),
            ('.in_use/17', b'{"pid":18,"pid":17}'),
            ('.in_use/2147483648', b'{"pid":2147483648}'),
            ('.in_use/17.tmp.1234ABCD', b'{"pid":17}'),
            ('.in_use/17.tmp.1234abcd', b''),
            ('.in_use/17.tmp.1234abcd', b'{"pid":17,"procStart":"' + b'0' * 4096 + b'"}'),
        ]
        for name, data in cases:
            with self.subTest(name=name, data=data[:80] if data else data):
                entry = destination / name
                entry.parent.mkdir(parents=True, exist_ok=True)
                entry.mkdir() if data is None else entry.write_bytes(data)
                before = self.snapshot()
                current = plan(self.setup, self.device)
                self.assertEqual(current.exit_code, 2)
                with self.assertRaisesRegex(RuntimeError, 'blocked'):
                    self.run_apply(current)
                self.assertEqual(self.snapshot(), before)
                top = destination / Path(name).parts[0]
                shutil.rmtree(top) if top.is_dir() else top.unlink()

    def test_claude_runtime_links_are_rejected(self):
        destination = self._installed_claude()
        external = self.root / 'external-marker'
        external.write_text('{"pid":17}')
        external_directory = self.root / 'external-leases'
        external_directory.mkdir()
        for name, target, hardlink in [('.in_use', external_directory, False),
                                       ('.in_use/17', external, False),
                                       ('.in_use/17', external, True),
                                       ('.orphaned_at', external, False)]:
            with self.subTest(name=name, hardlink=hardlink):
                entry = destination / name
                entry.parent.mkdir(parents=True, exist_ok=True)
                entry.hardlink_to(target) if hardlink else entry.symlink_to(target, target_is_directory=target.is_dir())
                self.assertEqual(plan(self.setup, self.device).exit_code, 2)
                self.assertEqual(external.read_text(), '{"pid":17}')
                entry.unlink()
                if entry.parent.name == '.in_use':
                    entry.parent.rmdir()

    def test_claude_lease_disappearing_during_read_is_a_noop(self):
        destination = self._installed_claude()
        lease = destination / '.in_use/17'
        lease.parent.mkdir()
        lease.write_text('{"pid":17,"procStart":"42"}')
        read_bytes = Path.read_bytes

        def released(path):
            if path == lease:
                path.unlink()
            return read_bytes(path)

        with patch.object(Path, 'read_bytes', released):
            self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertEqual(tuple(lease.parent.iterdir()), ())
        self.assertEqual(self.calls, [])

    def test_claude_same_path_replacement_preserves_populated_lease(self):
        destination = self._installed_claude()
        lease = destination / '.in_use/17'
        lease.parent.mkdir()
        lease.write_text('{"pid":17,"procStart":"42"}')
        before = (destination / 'skills/one/SKILL.md').read_bytes()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        with self.assertRaisesRegex(ConcurrentEdit, 'lease markers'):
            self.run_apply()
        self.assertEqual((destination / 'skills/one/SKILL.md').read_bytes(), before)
        self.assertEqual(lease.read_text(), '{"pid":17,"procStart":"42"}')
        lease.unlink()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (destination / 'skills/one/SKILL.md').read_text())
        self.assertEqual(self.calls, [])

    def test_claude_lease_replaced_during_disappearance_check_is_not_ignored(self):
        destination = self._installed_claude()
        lease = destination / '.in_use/17'
        lease.parent.mkdir()
        lease.write_text('{"pid":17}')
        read_bytes = Path.read_bytes

        def replaced_marker(path):
            if path == lease:
                path.write_text('replacement bytes')
                raise FileNotFoundError(path)
            return read_bytes(path)

        with patch.object(Path, 'read_bytes', replaced_marker):
            self.assertEqual(plan(self.setup, self.device).exit_code, 2)
        self.assertEqual(lease.read_text(), 'replacement bytes')
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')

    def test_claude_pending_displaced_runtime_metadata_recovers_but_stage_metadata_does_not(self):
        destination = self._interrupted_claude_install()
        pending = json.loads(self._pending_claude_receipt().read_bytes())['pending']
        displaced = Path(pending['displaced'])
        (displaced / '.in_use').mkdir()
        (displaced / '.orphaned_at').write_text('1792091304500')
        staged_marker = Path(pending['staged']) / '.in_use'
        staged_marker.mkdir()
        self.assertEqual(plan(self.setup, self.device).exit_code, 3)
        staged_marker.rmdir()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertFalse((destination / '.orphaned_at').exists())
        self.assertEqual((displaced / '.orphaned_at').read_text(), '1792091304500')

    def test_claude_source_runtime_metadata_is_not_adopted_as_content(self):
        destination = self._installed_claude()
        builder = Builder(self.setup, self.roots, self.roots.engine)
        source = builder.codex(self.setup.packages['kit']).root
        (source / '.in_use').mkdir()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        before = (destination / 'skills/one/SKILL.md').read_bytes()
        with self.assertRaisesRegex(BuildError, 'runtime metadata is not source content'):
            self.run_apply()
        self.assertEqual((destination / 'skills/one/SKILL.md').read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_claude_version_update_pending_retry_tolerates_old_native_bookkeeping_only(self):
        destination = self._installed_claude()
        old_lease = destination / '.in_use/17'
        old_lease.parent.mkdir()
        old_lease.write_text('{"pid":17,"procStart":"42"}')
        (self.source / '.codex-plugin/plugin.json').write_text('{"name":"kit","version":"2.0.0"}')
        (self.source / 'ordinary.txt').write_text('second release bytes\n')
        digest = self.roots.tree_digest(self.source)
        package = replace(self.setup.packages['kit'], source=LocalSource('owned/kit', digest))
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}),
                       trees={Host.CLAUDE: LockedTree('2.0.0', digest)})
        self.setup = replace(self.setup, packages={'kit': package}, lock={'kit': lock})

        def native_bookkeeping(new):
            (destination / '.orphaned_at').write_text('1792091304500')
            (new / '.in_use').mkdir()

        self.interrupt = native_bookkeeping
        rename = Path.rename

        def crash(path, target):
            if path.name == 'staged':
                raise RuntimeError('between version tree renames')
            return rename(path, target)

        with patch.object(Path, 'rename', crash), self.assertRaisesRegex(RuntimeError, 'between version tree renames'):
            self.run_apply()
        self.assertEqual((destination / '.orphaned_at').read_text(), '1792091304500')
        self.assertEqual(old_lease.read_text(), '{"pid":17,"procStart":"42"}')
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        (destination / 'ordinary.txt').write_text('user edit in old cache\n')
        edited = plan(self.setup, self.device)
        self.assertEqual(edited.exit_code, 3)
        self.assertTrue(any('Previous installation changed' in difference.detail for difference in edited.blocked))
        (destination / 'ordinary.txt').write_text('ordinary release bytes\n')
        self.calls.clear()
        self.assertEqual(self.run_apply().exit_code, 0)
        current = destination.with_name('2.0.0')
        self.assertEqual((current / 'ordinary.txt').read_text(), 'second release bytes\n')
        self.assertFalse((current / '.orphaned_at').exists())
        self.assertEqual(old_lease.read_text(), '{"pid":17,"procStart":"42"}')
        self.assertEqual(self.calls, [])

    def test_claude_version_update_adopts_the_pinned_release_a_native_listing_cached_and_registers_it(self):
        destination = self._installed_claude()
        (self.source / '.codex-plugin/plugin.json').write_text('{"name":"kit","version":"2.0.0"}')
        (self.source / 'ordinary.txt').write_text('second release bytes\n')
        digest = self.roots.tree_digest(self.source)
        package = replace(self.setup.packages['kit'], source=LocalSource('owned/kit', digest))
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}),
                       trees={Host.CLAUDE: LockedTree('2.0.0', digest)})
        self.setup = replace(self.setup, packages={'kit': package}, lock={'kit': lock})
        current = destination.with_name('2.0.0')
        shutil.copytree(self.source, current)
        (current / '.in_use').mkdir()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((current / 'ordinary.txt').read_text(), 'second release bytes\n')
        self.assertIn('use the local reader', (current / 'skills/one/SKILL.md').read_text())
        self.assertIn(('plugin', 'update'), [args[:2] for _, args in self.calls])

    def test_claude_version_update_preserves_other_content_at_the_new_cache_path(self):
        destination = self._installed_claude()
        (self.source / '.codex-plugin/plugin.json').write_text('{"name":"kit","version":"2.0.0"}')
        digest = self.roots.tree_digest(self.source)
        package = replace(self.setup.packages['kit'], source=LocalSource('owned/kit', digest))
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}),
                       trees={Host.CLAUDE: LockedTree('2.0.0', digest)})
        self.setup = replace(self.setup, packages={'kit': package}, lock={'kit': lock})
        current = destination.with_name('2.0.0')
        shutil.copytree(self.source, current)
        (current / 'ordinary.txt').write_text('unknown bytes\n')
        with self.assertRaisesRegex(ConcurrentEdit, 'Unverified destination preserved'):
            self.run_apply()
        self.assertEqual((current / 'ordinary.txt').read_text(), 'unknown bytes\n')
        self.assertEqual(self.calls, [])

    def test_claude_lease_created_after_swap_receipt_stops_the_rename(self):
        from accepted_installations import AcceptedInstallations

        destination = self._installed_claude()
        before = (destination / 'skills/one/SKILL.md').read_bytes()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        save = AcceptedInstallations._save
        writes = 0

        def concurrent_lease(owner, artifact, accepted, pending=None):
            nonlocal writes
            save(owner, artifact, accepted, pending)
            if pending:
                writes += 1
                if writes == 2:
                    lease = destination / '.in_use/17'
                    lease.parent.mkdir()
                    lease.write_text('{"pid":17}')

        with patch.object(AcceptedInstallations, '_save', concurrent_lease), self.assertRaisesRegex(ConcurrentEdit, 'lease markers'):
            self.run_apply()
        self.assertEqual((destination / 'skills/one/SKILL.md').read_bytes(), before)
        self.assertEqual((destination / '.in_use/17').read_text(), '{"pid":17}')
        (destination / '.in_use/17').unlink()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (destination / 'skills/one/SKILL.md').read_text())

    def test_claude_historical_receipt_metadata_is_not_silently_normalized(self):
        destination = self._installed_claude()
        (destination / '.in_use').mkdir()
        receipt = self._pending_claude_receipt()
        value = json.loads(receipt.read_bytes())
        value['accepted']['image']['entries']['.in_use'] = {'kind': 'directory'}
        receipt.write_text(json.dumps(value))
        before = receipt.read_bytes()
        self.assertEqual(plan(self.setup, self.device).exit_code, 2)
        self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')

    def test_runtime_marker_names_outside_claude_cache_root_remain_owned_content(self):
        self.run_apply()
        for root in (self.installed(), self.roots.codex_home / 'skills/loose'):
            marker = root / '.in_use'
            marker.mkdir()
            self.assertEqual(plan(self.setup, self.device).exit_code, 2)
            marker.rmdir()
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)
        destination = self._installed_claude()
        for name in ('skills/.in_use', 'skills/.orphaned_at', '.last_used'):
            marker = destination / name
            marker.write_text('1792091304500')
            self.assertEqual(plan(self.setup, self.device).exit_code, 2)
            marker.unlink()
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_claude_skill_unread_during_verified_pending_swap_recovers(self):
        destination = self._interrupted_claude_install()
        settings = self.roots.claude_home / 'settings.json'
        configured = json.loads(settings.read_bytes())
        configured['skillOverrides'] = {'kit:one': 'off'}
        settings.write_text(json.dumps(configured))
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 1, [(d.kind.value, d.key, d.detail) for d in current.result.differences])
        self.assertEqual(current.observations[Host.CLAUDE].unknown[0].missing_tree,
                         FactKey(Host.CLAUDE, Surface.TREE, 'kit@personal-collections'))
        self.assertEqual([(d.kind.value, d.key.surface.name) for d in current.changes],
                         [('changed', 'TREE')])
        self.calls.clear()
        after_recovery = self.run_apply(current)
        self.assertEqual(self.calls, [])
        self.assertTrue((destination / 'skills/one/SKILL.md').is_file())
        self.assertEqual([(d.kind.value, d.key.subject) for d in after_recovery.changes
                          if d.key and d.key.surface == Surface.SKILL], [('changed', 'kit@personal-collections:one')])
        self.assertEqual(self.run_apply(after_recovery).exit_code, 0)
        self.assertEqual(self.calls, [])
        (destination / 'skills/one/SKILL.md').write_text('---\nnot valid yaml: [\n---\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 3)

    def test_aligned_claude_missing_cache_defers_alignment_and_skill_reads(self):
        destination = self._interrupted_claude_install(aligned=True)
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 1, [(d.kind.value, d.key, d.detail) for d in current.result.differences])
        self.assertEqual(len(current.observations[Host.CLAUDE].unknown), 1)
        self.calls.clear()
        self.assertEqual(self.run_apply(current).exit_code, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'revised release bytes\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)
        (destination / '.claude-plugin/codex-alignment.json').write_text('{bad json')
        self.assertEqual(plan(self.setup, self.device).exit_code, 3)

    def test_aligned_claude_implicit_skills_missing_cache_recovers(self):
        destination = self._interrupted_claude_install(aligned=True, implicit=True)
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 1, [(d.kind.value, d.key, d.detail) for d in current.result.differences])
        self.assertEqual(len(current.observations[Host.CLAUDE].unknown), 1)
        self.calls.clear()
        self.assertEqual(self.run_apply(current).exit_code, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'revised release bytes\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_pending_claude_cache_unknown_stage_or_displaced_blocks(self):
        self._interrupted_claude_install()
        pending = json.loads(self._pending_claude_receipt().read_bytes())['pending']
        for field in ('staged', 'displaced'):
            with self.subTest(field=field):
                ordinary = Path(pending[field]) / 'ordinary.txt'
                original = ordinary.read_bytes()
                ordinary.write_bytes(b'unrecognized bytes\n')
                before = self.snapshot()
                current = plan(self.setup, self.device)
                self.assertEqual(current.exit_code, 3)
                with self.assertRaises(RuntimeError):
                    self.run_apply(current)
                self.assertEqual(self.snapshot(), before)
                ordinary.write_bytes(original)

    def test_pending_claude_cache_changed_binding_or_stage_path_blocks(self):
        self._interrupted_claude_install()
        receipt = self._pending_claude_receipt()
        original = json.loads(receipt.read_bytes())
        for field in ('binding', 'stage'):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(original))
                if field == 'binding':
                    changed['pending']['target']['bindings'][0]['physical'] = '/retargeted'
                else:
                    changed['pending']['staged'] = str(Path(changed['pending']['staged']).parent.parent / 'elsewhere')
                receipt.write_text(json.dumps(changed))
                before = self.snapshot()
                current = plan(self.setup, self.device)
                self.assertEqual(current.exit_code, 3)
                with self.assertRaises(RuntimeError):
                    self.run_apply(current)
                self.assertEqual(self.snapshot(), before)
                receipt.write_text(json.dumps(original))

    def test_claude_missing_cache_without_pending_does_not_defer_skill_error(self):
        self._interrupted_claude_install()
        receipt = self._pending_claude_receipt()
        receipt.rename(receipt.with_suffix('.retained'))
        before = self.snapshot()
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 3)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertEqual(self.snapshot(), before)

    def test_pending_claude_linked_work_parent_blocks(self):
        self._interrupted_claude_install()
        pending = json.loads(self._pending_claude_receipt().read_bytes())['pending']
        work = Path(pending['staged']).parent
        retained = work.with_name(work.name + '-retained')
        work.rename(retained)
        work.symlink_to(retained, target_is_directory=True)
        before = self.snapshot()
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 3)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertEqual(self.snapshot(), before)

    def test_pending_claude_cache_other_skill_unread_still_blocks(self):
        from claude_host import ClaudeState

        destination = self._interrupted_claude_install()
        state = self.device.host(Host.CLAUDE)
        original = state.observe
        plugin = 'kit@personal-collections'
        extra = Unknown(self.roots.tokens(destination), 'other read failed', frozenset({Surface.SKILL}),
                        frozenset({plugin}))
        for first in (True, False):
            with self.subTest(extra_first=first):
                def observe(request):
                    result = original(request)
                    unknown = (extra, *result.unknown) if first else (*result.unknown, extra)
                    return replace(result, unknown=unknown)

                with patch.object(state, 'observe', observe):
                    before = self.snapshot()
                    current = plan(self.setup, self.device)
                    self.assertEqual(current.exit_code, 3)
                    with self.assertRaises(RuntimeError):
                        self.run_apply(current)
                    self.assertEqual(self.snapshot(), before)
        with patch.object(ClaudeState, '_explicit', side_effect=Unread('other skill read failed')):
            self.assertEqual(plan(self.setup, self.device).exit_code, 3)

    def test_pending_aligned_implicit_cache_other_orphan_unread_still_blocks(self):
        destination = self._interrupted_claude_install(aligned=True, implicit=True)
        state = self.device.host(Host.CLAUDE)
        original = state.observe
        extra = Unknown(self.roots.tokens(destination), 'independent unread', frozenset({Surface.SKILL}),
                        frozenset({'other@codex-aligned'}))

        def observe(request):
            result = original(request)
            return replace(result, unknown=(*result.unknown, extra))

        with patch.object(state, 'observe', observe):
            before = self.snapshot()
            current = plan(self.setup, self.device)
            self.assertEqual(current.exit_code, 3)
            self.assertEqual(len([d for d in current.blocked if d.kind.value == 'incomplete']), 1)
            with self.assertRaises(RuntimeError):
                self.run_apply(current)
            self.assertEqual(self.snapshot(), before)

    def test_claude_non_directory_or_unreadable_cache_has_no_missing_marker(self):
        destination = self._interrupted_claude_install()
        destination.write_text('not a directory')
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 3)
        self.assertIsNone(current.observations[Host.CLAUDE].unknown[0].missing_tree)
        destination.unlink()
        stat = Path.stat

        def denied(path, *args, **kwargs):
            if path == destination:
                raise PermissionError('test permission denial')
            return stat(path, *args, **kwargs)

        with patch.object(Path, 'stat', denied):
            current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 3)
        self.assertIsNone(current.observations[Host.CLAUDE].unknown[0].missing_tree)

    def test_whole_absence_can_recreate_an_accepted_tree(self):
        self.run_apply()
        shutil.rmtree(self.installed())
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.installed() / 'ordinary.txt').read_text(), 'ordinary release bytes\n')

    def test_stale_receipt_adopts_exact_new_desired_bytes(self):
        self.run_apply()
        self.setup = replace(self.setup, rules=Rules(b'Already deployed reviewed rules.\n'))
        path = self.roots.codex_home / 'AGENTS.md'
        path.write_bytes(self.setup.rules.text)
        before = path.stat().st_mtime_ns
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(path.read_bytes(), b'Already deployed reviewed rules.\n')

    def test_partial_deletion_and_raw_kind_changes_are_not_absence(self):
        self.run_apply()
        path = self.installed() / 'ordinary.txt'
        path.unlink()
        self.assertEqual(plan(self.setup, self.device).exit_code, 2)
        path.mkdir()
        self.assertEqual(plan(self.setup, self.device).exit_code, 2)
        self.assertTrue(path.is_dir())

    def test_malformed_receipt_is_incomplete_and_preserved(self):
        self.run_apply()
        receipt = self.receipt_paths()[0]
        receipt.write_text('{not valid json')
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 3)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertEqual(receipt.read_text(), '{not valid json')

    def test_shared_rule_alias_has_one_receipt_and_retargeting_blocks(self):
        from claude_host import ClaudeState
        from types import SimpleNamespace
        self.roots.claude_home.mkdir(parents=True)
        alias = self.roots.claude_home / 'CLAUDE.md'
        alias.symlink_to(self.roots.codex_home / 'AGENTS.md')

        def observe(request):
            seen = Observer(Host.CLAUDE, self.roots)
            if alias.is_file():
                seen.add(Surface.RULES, 'CLAUDE.md', FileValue(bytes_digest(alias.read_bytes())))
            return seen.finish('rules host', set())
        claude = SimpleNamespace(host=Host.CLAUDE, project=lambda setup: ClaudeState.project(None, setup), observe=observe)
        self.device = replace(self.device, hosts=(self.host, claude))
        self.assertEqual(self.run_apply().exit_code, 0)
        receipts = [json.loads(p.read_bytes()) for p in self.receipt_paths()]
        self.assertEqual(len([r for r in receipts if r['identity'].startswith('rules:')]), 1)
        self.assertEqual(alias.read_bytes(), b'Managed rules.\n')
        alias.unlink()
        destination = self.root / 'other-rules.md'
        alias.symlink_to(destination)
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 2)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertFalse(destination.exists())

    def test_private_models_and_runtime_fields_are_owned_without_touching_other_settings(self):
        private = self.root / 'private'
        source = private / 'config/pstack'
        source.mkdir(parents=True)
        models = b'## codex\ncode: auto@high\n## claude-code\ncode: inherit-parent@high\n'
        (source / 'models.md').write_bytes(models)
        (source / 'runtime.toml').write_text('spawn_overrides = true\n')
        self.setup = replace(self.setup, roots=replace(self.setup.roots, private=private / 'setup'))
        self.roots.codex_home.mkdir(parents=True)
        runtime = self.roots.codex_home / 'config.toml'
        runtime.write_text('# preserved\n[mcp_servers.example.env]\nTOKEN = "test-only-local"\n')
        with patch('accepted_installations.validate_runtime') as validate:
            self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual([call.kwargs['proposed'] for call in validate.call_args_list], [True, False])
        models_path = self.roots.home / '.config/pstack/models.md'
        self.assertEqual(models_path.read_bytes(), models)
        self.assertIn('TOKEN = "test-only-local"', runtime.read_text())
        self.assertIn('expose_spawn_agent_model_overrides = true', runtime.read_text())
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.receipt_paths()}
        with patch('accepted_installations.validate_runtime', side_effect=RuntimeError('effective runtime unavailable')):
            for _ in range(2):
                failed = plan(self.setup, self.device)
                self.assertEqual(failed.exit_code, 3)
                self.assertTrue(any('effective runtime unavailable' in issue.detail for issue in failed.blocked))
                with self.assertRaises(RuntimeError):
                    self.run_apply(failed)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.receipt_paths()}, before)
        models_path.write_bytes(models + b'local edit\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 2)
        models_path.write_bytes(models)
        runtime.write_text(runtime.read_text().replace('expose_spawn_agent_model_overrides = true',
                                                       'expose_spawn_agent_model_overrides = false'))
        self.assertEqual(plan(self.setup, self.device).exit_code, 2)

    def test_toml_unmanaged_concurrent_edit_survives_managed_update(self):
        from sync_hosts import Apply
        source = self.public / 'config/mise/config.toml'
        source.parent.mkdir(parents=True)
        source.write_text('[tools]\nnode = "24"\n')
        self.run_apply()
        source.write_text('[tools]\nnode = "25"\n')
        live = self.roots.home / '.config/mise/config.toml'
        run = Apply.run

        def race(operation):
            live.write_text(live.read_text() + '\n[env]\nSECRET = "test-only-concurrent-value"\n')
            return run(operation)
        with patch.object(Apply, 'run', race):
            self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('node = "25"', live.read_text())
        self.assertIn('SECRET = "test-only-concurrent-value"', live.read_text())


if __name__ == '__main__':
    unittest.main()
