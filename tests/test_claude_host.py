import json
from pathlib import Path
import subprocess
import sys
import unittest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))
sys.path.insert(0, str(TESTS))


import homes
import host_state
import description
import claude_host
d, hs = description, host_state
S, M = hs.Surface, hs.SkillMode
NEW, OLD = 'a' * 40, 'c' * 40
D1 = 'sha256:' + '1' * 64


def key(surface, subject):
    return hs.FactKey(hs.Host.CLAUDE, surface, subject)


class ProjectTest(unittest.TestCase):
    maxDiff = None

    def test_explicit_checkout_mode_is_not_projected_as_git_registration(self):
        repo = d.Repo('interfaces', d.Visibility.PUBLIC, '', 'https://github.com/example/interfaces.git', NEW,
                      None, d.HostMarketplace('interfaces', d.MarketplaceMode.CHECKOUT))
        setup = d.Setup(roots=d.Roots(Path('/p'), None), packages={}, repos={'interfaces': repo}, loose={},
                        host_skills={}, rules=None, note_rules=(), public_repositories=frozenset(), lock={})
        facts = claude_host.ClaudeState(homes.projection_roots(), 'claude').project(setup)
        self.assertEqual(facts[key(S.MARKETPLACE, 'interfaces')].value,
                         hs.CheckoutMarketplace('https://github.com/example/interfaces.git', NEW))

    def test_aligned_expectations_come_from_the_codex_placement(self):
        interfaces = d.Package('interfaces', d.Visibility.PUBLIC, '', d.RepoSource('interfaces'),
                               {'Better Interface': M.EXPLICIT, 'variant': M.OFF}, d.ToolNotes.AUDIT,
                               d.CodexPlacement((d.EngineInstall(True),)),
                               d.ClaudePlacement((d.AlignedInstall(True, frozenset()), d.MarketplaceInstall('interfaces', False))))
        course = d.Package('course', d.Visibility.PRIVATE, '',
                           d.BuildSource(d.CourseRecipe('https://github.com/example/course.git', NEW, ('s/a', 's/b'), ''), 1, 'C'),
                           {'a': M.EXPLICIT, 'b': M.EXPLICIT}, d.ToolNotes.APPLY,
                           d.CodexPlacement((d.EngineInstall(True),)),
                           d.ClaudePlacement((d.EngineInstall(True, 'course', ('a',)), d.EngineInstall(False, 'course-extra', ('b',)))))
        repo = d.Repo('interfaces', d.Visibility.PUBLIC, '', 'https://github.com/example/interfaces.git', NEW,
                      None, d.HostMarketplace('interfaces'))
        lock = {'interfaces': d.Lock(D1, {hs.Host.CLAUDE: d.LockedTree('1.0+codex-aligned.x', D1)}),
                'course': d.Lock(D1, {hs.Host.CLAUDE: d.LockedTree('0.1.claude1', D1)})}
        setup = d.Setup(roots=d.Roots(Path('/p'), None), packages={'interfaces': interfaces, 'course': course},
                        repos={'interfaces': repo}, loose={}, host_skills={hs.Host.CLAUDE: {'playwright': M.ON}}, rules=None,
                        note_rules=(), public_repositories=frozenset(), lock=lock)
        facts = claude_host.ClaudeState(homes.projection_roots(), 'claude').project(setup)
        aligned, native = key(S.PLUGIN, 'interfaces@codex-aligned'), key(S.PLUGIN, 'interfaces@interfaces')
        split = key(S.PLUGIN, 'course@personal-collections')
        self.assertEqual({k: (f.value, f.follows) for k, f in facts.items()}, {
            key(S.MARKETPLACE, 'interfaces'): (hs.GitMarketplace('https://github.com/example/interfaces.git', NEW), None),
            key(S.MARKETPLACE, 'codex-aligned'): (hs.EngineMarketplace(), None),
            key(S.MARKETPLACE, 'personal-collections'): (hs.EngineMarketplace(), None),
            aligned: (hs.PluginValue(True, hs.Released('1.0+codex-aligned.x')), None),
            key(S.TREE, 'interfaces@codex-aligned'): (hs.TreeValue(D1), aligned),
            key(S.SKILL, 'interfaces@codex-aligned:better-interface'): (M.EXPLICIT, aligned),
            key(S.SKILL, 'interfaces@codex-aligned:variant'): (M.OFF, aligned),
            native: (hs.PluginValue(False, hs.Pinned(NEW)), None),
            key(S.SKILL, 'interfaces@interfaces:Better Interface'): (M.EXPLICIT, native),
            split: (hs.PluginValue(True, hs.Released('0.1.claude1')), None),
            key(S.TREE, 'course@personal-collections'): (hs.TreeValue(D1), split),
            key(S.OVERLAY, 'course@personal-collections'): (hs.OverlaySpec((), None, ('a',)), split),
            key(S.SKILL, 'course@personal-collections:a'): (M.EXPLICIT, split),
            key(S.PLUGIN, 'course-extra@personal-collections'): (hs.PluginValue(False, hs.Unpinned()), None),
            key(S.SKILL, 'course-extra@personal-collections:b'): (M.EXPLICIT, key(S.PLUGIN, 'course-extra@personal-collections')),
            key(S.SKILL, 'playwright'): (M.ON, None),
        })

    def test_a_claude_mode_replaces_the_package_mode_in_the_aligned_copy_only(self):
        modes = {'handoff': M.OFF, 'shell': M.OFF, 'later': M.OFF, 'grill': M.EXPLICIT}
        kit = d.Package('kit', d.Visibility.PUBLIC, '',
                        d.BuildSource(d.GenericRecipe('https://github.com/example/kit.git', NEW, None, 'plugin.json',
                                                      ('handoff', 'shell', 'later', 'grill')), 1, 'Kit'),
                        modes, d.ToolNotes.AUDIT, d.CodexPlacement((d.EngineInstall(True),)),
                        d.ClaudePlacement((d.AlignedInstall(True),), {'handoff': M.EXPLICIT, 'shell': M.ON}))
        setup = d.Setup(roots=d.Roots(Path('/p'), None), packages={'kit': kit}, repos={}, loose={}, host_skills={},
                        rules=None, note_rules=(), public_repositories=frozenset(), lock={})
        facts = claude_host.ClaudeState(homes.projection_roots(), 'claude').project(setup)
        aligned = key(S.PLUGIN, 'kit@codex-aligned')
        self.assertEqual({k.subject: f.value for k, f in facts.items() if k.surface == S.SKILL}, {
            'kit@codex-aligned:handoff': M.EXPLICIT,
            'kit@codex-aligned:later': M.OFF,
            'kit@codex-aligned:grill': M.EXPLICIT,
        })
        self.assertEqual(facts[key(S.SKILL, 'kit@codex-aligned:handoff')].follows, aligned)


class Device(homes.Home):
    """A Claude home recorded from the shapes `claude plugin list --json` and settings.json have today."""

    def __enter__(self):
        super().__enter__()
        r = self.roots
        cache = r.claude_home / 'plugins/cache'

        def install(plugin_id, enabled, version, scope='user', **extra):
            name, marketplace = plugin_id.split('@')
            return {'id': plugin_id, 'version': version, 'scope': scope, 'enabled': enabled,
                    'installPath': str(cache / marketplace / name / version), **extra}

        self.inventory = [
            install('kit@codex-aligned', True, '1.0-codex-aligned.x'),
            install('old@codex-aligned', False, '0.1-codex-aligned.y'),
            install('interfaces@interfaces', False, '1.10.3'),
            install('skill-creator@claude-plugins-official', True, '641d925db5a6'),
            install('kit@codex-aligned', True, '1.0-codex-aligned.x', scope='local', projectPath=str(r.home / 'project')),
        ]
        self.skill(cache / 'codex-aligned/kit/1.0-codex-aligned.x', 'poteto-mode', frontmatter_explicit=True)
        self.write(cache / 'codex-aligned/kit/1.0-codex-aligned.x/.claude-plugin/codex-alignment.json',
                   json.dumps({'codexSource': str(r.codex_home), 'excludedSkillNames': ['kit:Fix CI']}))
        self.skill(cache / 'codex-aligned/old/0.1-codex-aligned.y', 'a')
        self.write(cache / 'codex-aligned/old/0.1-codex-aligned.y/.claude-plugin/codex-alignment.json',
                   json.dumps({'excludedSkillNames': ['old:b']}))
        self.skill(cache / 'interfaces/interfaces/1.10.3', 'better-interface', frontmatter_explicit=True)
        self.skill(cache / 'claude-plugins-official/skill-creator/641d925db5a6', 'skill-creator', frontmatter_explicit=True)
        self.write(r.claude_home / 'plugins/installed_plugins.json', json.dumps({'version': 2, 'plugins': {
            'interfaces@interfaces': [{'scope': 'user', 'version': '1.10.3', 'gitCommitSha': OLD}]}}))
        self.write(r.claude_home / 'plugins/known_marketplaces.json', json.dumps({'claude-plugins-official': {}}))
        self.write(r.claude_home / 'marketplaces/codex-aligned/.claude-plugin/marketplace.json',
                   json.dumps({'name': 'codex-aligned', 'plugins': []}))
        self.write(r.claude_home / 'settings.json', json.dumps({
            'enabledPlugins': {'kit@codex-aligned': True, 'interfaces@interfaces': False, 'gone@elsewhere': False},
            'extraKnownMarketplaces': {
                'interfaces': {'source': {'source': 'git', 'url': 'https://github.com/example/interfaces.git', 'ref': 'main'}},
                'openai-codex': {'source': {'source': 'github', 'repo': 'example/codex-plugin-cc'}},
                'codex-aligned': {'source': {'source': 'directory', 'path': str(r.claude_home / 'marketplaces/codex-aligned')}},
                'personal-collections': {'source': {'source': 'directory', 'path': str(r.home / 'elsewhere')}}},
            'skillOverrides': {'cli-creator': 'off', 'playwright': 'on'}}))
        self.write(r.claude_home / 'skills/cli-creator/SKILL.md', '---\nname: cli-creator\n---\n')
        self.write(r.claude_home / 'skills/synced/x/SKILL.md', '---\nname: x\n---\n')
        self.write(r.claude_home / 'skills/.trash/y/SKILL.md', '---\nname: y\n---\n')
        self.write(r.claude_home / 'CLAUDE.md', '# rules\n')
        return self

    def state(self, available=()):
        answers = {'--version': '2.1.0 (Claude Code)\n',
                   'plugin list --available --json': json.dumps({'installed': self.inventory, 'available': list(available)})}
        return claude_host.ClaudeState(self.roots, self.cli('claude', answers))

    def git_marketplace(self):
        directory = self.roots.engine / 'checkouts/interfaces'
        self.write(directory / '.claude-plugin/marketplace.json', json.dumps({'name': 'interfaces', 'plugins': []}))
        for args in (('init',), ('config', 'user.name', 'Test'), ('config', 'user.email', 'test@example.invalid'),
                     ('remote', 'add', 'origin', 'https://github.com/example/interfaces.git'),
                     ('add', '.'), ('commit', '-m', 'Pinned marketplace')):
            subprocess.run(['git', '-C', str(directory), *args], capture_output=True, check=True)
        commit = subprocess.check_output(['git', '-C', str(directory), 'rev-parse', 'HEAD'], text=True).strip()
        settings = self.roots.claude_home / 'settings.json'
        data = json.loads(settings.read_text())
        data['extraKnownMarketplaces']['interfaces']['source'] = {'source': 'directory', 'path': str(directory)}
        settings.write_text(json.dumps(data))
        return directory, commit

    def native_git_marketplace(self):
        directory, commit = self.git_marketplace()
        native = self.roots.claude_home / 'plugins/marketplaces/interfaces'
        native.parent.mkdir(parents=True, exist_ok=True)
        directory.rename(native)
        subprocess.run(['git', '-C', str(native), 'checkout', '--detach', commit], capture_output=True, check=True)
        source = {'source': 'git', 'url': 'https://github.com/example/interfaces.git', 'ref': commit}
        settings = self.roots.claude_home / 'settings.json'
        data = json.loads(settings.read_text())
        data['extraKnownMarketplaces']['interfaces'] = {'source': source, 'autoUpdate': False}
        settings.write_text(json.dumps(data))
        registry = self.roots.claude_home / 'plugins/known_marketplaces.json'
        registry.write_text(json.dumps({'interfaces': {'source': source, 'autoUpdate': False, 'installLocation': str(native)}}))
        return native, commit


class ObserveTest(unittest.TestCase):
    maxDiff = None

    def test_native_git_pin_comes_from_the_detached_checkout_and_update_policy(self):
        with Device() as device:
            _, commit = device.native_git_marketplace()
            observation = device.state().observe(hs.Request())
        self.assertEqual(observation.facts[key(S.MARKETPLACE, 'interfaces')].value,
                         hs.GitMarketplace('https://github.com/example/interfaces.git', commit))
        self.assertEqual(observation.facts[key(S.PLUGIN, 'interfaces@interfaces')].value,
                         hs.PluginValue(False, hs.Pinned(OLD)))

    def test_native_git_with_auto_updates_cannot_claim_an_exact_pin(self):
        for filename, nested in (('settings.json', True), ('plugins/known_marketplaces.json', False)):
            with self.subTest(filename=filename), Device() as device:
                device.native_git_marketplace()
                path = device.roots.claude_home / filename
                data = json.loads(path.read_text())
                entries = data['extraKnownMarketplaces'] if nested else data
                entries['interfaces']['autoUpdate'] = True
                path.write_text(json.dumps(data))
                observation = device.state().observe(hs.Request())
                self.assertEqual(observation.facts[key(S.MARKETPLACE, 'interfaces')].value,
                                 hs.GitMarketplace('https://github.com/example/interfaces.git', None))

    def test_native_detached_head_with_a_moving_ref_is_not_an_exact_pin(self):
        with Device() as device:
            device.native_git_marketplace()
            for filename, nested in (('settings.json', True), ('plugins/known_marketplaces.json', False)):
                path = device.roots.claude_home / filename
                data = json.loads(path.read_text())
                entries = data['extraKnownMarketplaces'] if nested else data
                entries['interfaces']['source']['ref'] = 'main'
                path.write_text(json.dumps(data))
            observation = device.state().observe(hs.Request())
        self.assertEqual(observation.facts[key(S.MARKETPLACE, 'interfaces')].value,
                         hs.GitMarketplace('https://github.com/example/interfaces.git', None))

    def test_dirty_native_git_cache_is_unknown_instead_of_pinned(self):
        with Device() as device:
            native, _ = device.native_git_marketplace()
            (native / 'unreviewed').write_text('Local change')
            observation = device.state().observe(hs.Request())
        self.assertNotIn(key(S.MARKETPLACE, 'interfaces'), observation.facts)
        self.assertTrue(any(u.subjects == frozenset({'interfaces'}) and 'local changes' in u.reason
                            for u in observation.unknown))

    def test_a_git_directory_preserves_its_origin_and_checkout_pin_separately_from_the_install_pin(self):
        with Device() as device:
            _, commit = device.git_marketplace()
            observation = device.state().observe(hs.Request())
        self.assertEqual(observation.facts[key(S.MARKETPLACE, 'interfaces')].value,
                         hs.CheckoutMarketplace('https://github.com/example/interfaces.git', commit))
        self.assertEqual(observation.facts[key(S.PLUGIN, 'interfaces@interfaces')].value,
                         hs.PluginValue(False, hs.Pinned(OLD)))

    def test_a_dirty_git_directory_cannot_claim_a_pinned_marketplace(self):
        with Device() as device:
            directory, _ = device.git_marketplace()
            (directory / 'unreviewed').write_text('Local change')
            observation = device.state().observe(hs.Request())
        self.assertNotIn(key(S.MARKETPLACE, 'interfaces'), observation.facts)
        self.assertEqual([(u.surfaces, u.subjects, u.reason) for u in observation.unknown],
                         [(frozenset({S.MARKETPLACE}), frozenset({'interfaces'}), 'marketplace checkout has local changes')])

    def test_directory_registration_without_a_matching_catalog_is_not_observed_as_managed(self):
        with Device() as device:
            catalog = device.roots.claude_home / 'marketplaces/codex-aligned/.claude-plugin/marketplace.json'
            for content in (None, '{"name": "another-marketplace"}'):
                with self.subTest(content=content):
                    if content is None:
                        catalog.unlink()
                    else:
                        catalog.write_text(content)
                    observation = device.state().observe(hs.Request())
                    self.assertNotIn(key(S.MARKETPLACE, 'codex-aligned'), observation.facts)

    def test_reads_installs_overrides_frontmatter_and_aligned_copies(self):
        with Device() as device:
            wanted = key(S.SKILL, 'skill-creator@claude-plugins-official:skill-creator')
            named = key(S.SKILL, 'interfaces@interfaces:better-interface')
            observation = device.state(available=[{'pluginId': 'playwright@claude-plugins-official', 'name': 'playwright',
                                                   'marketplaceName': 'claude-plugins-official'}]).observe(
                hs.Request(skills=frozenset({wanted.subject, named.subject})))
            loose = device.roots.tree_digest(device.roots.claude_home / 'skills/cli-creator')
        kit, interfaces = key(S.PLUGIN, 'kit@codex-aligned'), key(S.PLUGIN, 'interfaces@interfaces')
        self.assertEqual({k: f.value for k, f in observation.facts.items()}, {
            kit: hs.PluginValue(True, hs.Released('1.0-codex-aligned.x')),
            key(S.PLUGIN, 'old@codex-aligned'): hs.PluginValue(False, hs.Released('0.1-codex-aligned.y')),
            interfaces: hs.PluginValue(False, hs.Pinned(OLD)),
            key(S.PLUGIN, 'skill-creator@claude-plugins-official'): hs.PluginValue(True, hs.Unpinned()),
            key(S.MARKETPLACE, 'interfaces'): hs.GitMarketplace('https://github.com/example/interfaces.git', None),
            key(S.MARKETPLACE, 'openai-codex'): hs.GitMarketplace('https://github.com/example/codex-plugin-cc.git', None),
            key(S.MARKETPLACE, 'codex-aligned'): hs.EngineMarketplace(),
            key(S.SKILL, 'cli-creator'): M.OFF,
            key(S.SKILL, 'playwright'): M.ON,
            key(S.SKILL, 'kit@codex-aligned:fix-ci'): M.OFF,
            key(S.SKILL, 'kit@codex-aligned:poteto-mode'): M.EXPLICIT,
            key(S.SKILL, 'interfaces@interfaces:better-interface'): M.EXPLICIT,
            wanted: M.EXPLICIT,
            key(S.LOOSE_SKILL, '{claude_home}/skills/cli-creator'): hs.FileValue(loose),
            key(S.RULES, 'CLAUDE.md'): hs.FileValue(hs.bytes_digest(b'# rules\n')),
        })
        self.assertEqual(observation.facts[key(S.SKILL, 'kit@codex-aligned:fix-ci')].follows, kit)
        self.assertEqual(observation.offered, frozenset({'kit@codex-aligned', 'old@codex-aligned', 'interfaces@interfaces',
                                                         'skill-creator@claude-plugins-official', 'playwright@claude-plugins-official'}))
        self.assertEqual(observation.unknown, ())
        self.assertEqual([(n.kind.value, n.subject, n.detail) for n in observation.notes], [
            ('device-local', 'project installs', '1 project-scoped installs'),
            ('residue', 'enabledPlugins', '1 entries for plugins that are not installed'),
            ('unmodeled', 'extraKnownMarketplaces.personal-collections', 'directory source'),
        ])

    def test_a_disabled_duplicate_reads_at_its_own_old_pin_and_only_named_flags(self):
        with Device() as device:
            observation = device.state().observe(hs.Request())
        self.assertEqual(observation.facts[key(S.PLUGIN, 'interfaces@interfaces')].value,
                         hs.PluginValue(False, hs.Pinned(OLD)))
        self.assertNotIn(key(S.SKILL, 'interfaces@interfaces:better-interface'), observation.facts)

    def test_a_native_install_without_a_recorded_commit_is_unknown(self):
        with Device() as device:
            device.git_marketplace()
            records = device.roots.claude_home / 'plugins/installed_plugins.json'
            records.write_text(json.dumps({'version': 2, 'plugins': {'interfaces@interfaces': [{'scope': 'user', 'version': '1.10.3'}]}}))
            observation = device.state().observe(hs.Request())
        self.assertNotIn(key(S.PLUGIN, 'interfaces@interfaces'), observation.facts)
        self.assertEqual([(u.source, u.surfaces, u.subjects) for u in observation.unknown],
                         [('{claude_home}/plugins/installed_plugins.json', frozenset({S.PLUGIN}), frozenset({'interfaces@interfaces'}))])

    def test_an_overlaid_claude_cache_reads_as_its_release(self):
        with Device() as device:
            root = device.roots.claude_home / 'plugins/cache/codex-aligned/kit/1.0-codex-aligned.x'
            release = device.roots.tree_digest(root)
            path = root / 'skills/poteto-mode/SKILL.md'
            original = path.read_text()
            path.write_text(original + 'Use TodoWrite (Codex: `update_plan`).\n')
            device.receipt({path: {'base': original, 'effective': path.read_text()}})
            tree = key(S.TREE, 'kit@codex-aligned')
            observation = device.state().observe(hs.Request(trees=frozenset({'kit@codex-aligned'})))
        self.assertEqual(observation.facts[tree].value, hs.TreeValue(release))


if __name__ == '__main__':
    unittest.main()
