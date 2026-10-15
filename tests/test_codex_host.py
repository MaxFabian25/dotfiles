import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))
sys.path.insert(0, str(TESTS))


import homes
import host_state
import description
import codex_host
d, hs = description, host_state
S, M = hs.Surface, hs.SkillMode
C1, C2 = 'a' * 40, 'b' * 40
D1, D2, D3 = ('sha256:' + c * 64 for c in '123')
NOTES = (d.NoteRule(r'\bTodoWrite\b', ' (Codex: `update_plan`)'),)


def key(surface, subject):
    return hs.FactKey(hs.Host.CODEX, surface, subject)


def package(name, source, codex, modes=None, tool_notes=d.ToolNotes.AUDIT):
    return d.Package(name, d.Visibility.PUBLIC, '', source, modes or {}, tool_notes, codex, None)


def setup(packages=(), repos=(), loose=(), host_skills=None, rules=None, lock=None):
    return d.Setup(roots=d.Roots(Path('/public'), None), packages={p.name: p for p in packages},
                   repos={r.name: r for r in repos}, loose={s.name: s for s in loose}, host_skills=host_skills or {},
                   rules=rules, note_rules=NOTES, public_repositories=frozenset(), lock=lock or {})


KIT = package('kit', d.BuildSource(d.GenericRecipe('https://github.com/example/kit.git', C1, None, 'plugin.json', ()), 1, 'Kit'),
              d.CodexPlacement((d.EngineInstall(True),)), {'fix-ci': M.OFF, 'deslop': M.EXPLICIT})
IMPECCABLE = package('impeccable', d.RepoSource('impeccable', 'plugin'),
                     d.CodexPlacement((d.EngineInstall(True),), hooks=d.Hooks.TRUSTED), tool_notes=d.ToolNotes.APPLY)
CONNECTOR = package('connector', None, d.CodexPlacement((d.MarketplaceInstall('openai-curated-remote', False),)),
                    {'search': M.ON})
SKILLS = d.Repo('skills', d.Visibility.PUBLIC, '', 'https://github.com/example/skills.git', C2,
                d.HostMarketplace('skills'), None)
IMPECCABLE_REPO = d.Repo('impeccable', d.Visibility.PUBLIC, '', 'https://github.com/example/impeccable.git', C1, None, None)
DEFINE_GOAL = d.LooseSkill('define-goal', d.Visibility.PRIVATE, '', d.SnapshotSource({d.SkillRoot.CODEX: D3}), {d.SkillRoot.CODEX: False})


class ProjectTest(unittest.TestCase):
    def test_projects_each_choice_in_codex_spelling(self):
        lock = {'kit': d.Lock(D1, {hs.Host.CODEX: d.LockedTree('1.0+upstream', D1)}),
                'impeccable': d.Lock(D1, {}, {'impeccable_documenter.toml': D2}, hooks=3),
                'define-goal': d.Lock(D1, placements={d.SkillRoot.CODEX: D3})}
        facts = codex_host.CodexState(homes.host_state.DeviceRoots.current(), 'codex').project(setup(
            [KIT, IMPECCABLE, CONNECTOR], [SKILLS, IMPECCABLE_REPO], [DEFINE_GOAL], {hs.Host.CODEX: {'plugin-creator': M.OFF}},
            d.Rules(b''), lock))
        kit, impeccable, connector = key(S.PLUGIN, 'kit@personal'), key(S.PLUGIN, 'impeccable@personal'), key(S.PLUGIN, 'connector@openai-curated-remote')
        loose = key(S.LOOSE_SKILL, '{codex_home}/skills/define-goal')
        self.assertEqual({k: (f.value, f.entry, f.follows) for k, f in facts.items()}, {
            key(S.MARKETPLACE, 'skills'): (hs.GitMarketplace('https://github.com/example/skills.git', C2), 'skills', None),
            key(S.MARKETPLACE, 'personal'): (hs.EngineMarketplace(), None, None),
            kit: (hs.PluginValue(True, hs.Released('1.0+upstream')), 'kit', None),
            key(S.TREE, 'kit@personal'): (hs.TreeValue(D1), 'kit', kit),
            key(S.SKILL, 'kit:fix-ci'): (M.OFF, 'kit', kit),
            key(S.SKILL, 'kit:deslop'): (M.EXPLICIT, 'kit', kit),
            impeccable: (hs.PluginValue(True, hs.Unpinned()), 'impeccable', None),
            key(S.OVERLAY, 'impeccable@personal'): (hs.OverlaySpec(NOTES, None), 'impeccable', impeccable),
            key(S.HOOK, 'impeccable@personal'): (hs.Approved(3), 'impeccable', impeccable),
            key(S.AGENT, 'impeccable_documenter.toml'): (hs.FileValue(D2), 'impeccable', impeccable),
            connector: (hs.PluginValue(False, hs.Unpinned()), 'connector', None),
            key(S.SKILL, 'connector:search'): (M.ON, 'connector', connector),
            loose: (hs.FileValue(D3), 'define-goal', None),
            key(S.SKILL, '{codex_home}/skills/define-goal/SKILL.md'): (M.OFF, 'define-goal', loose),
            key(S.SKILL, 'plugin-creator'): (M.OFF, 'hosts/codex.toml', None),
            key(S.RULES, 'AGENTS.md'): (hs.FileValue(hs.bytes_digest(b'')), 'rules/AGENTS.md', None),
        })

    def test_a_plugin_from_a_checkout_marketplace_is_pinned_by_its_locked_version(self):
        local = d.Repo('local', d.Visibility.PRIVATE, '', 'https://github.com/example/local.git', C1,
                       d.HostMarketplace('local', d.MarketplaceMode.CHECKOUT), None)
        prose = package('prose', d.RepoSource('local'), d.CodexPlacement((d.MarketplaceInstall('local', True),)))
        lock = {'prose': d.Lock(D1, {hs.Host.CODEX: d.LockedTree('0.2.0+codex.1', D2)})}
        facts = codex_host.CodexState(hs.DeviceRoots.current(), 'codex').project(setup([prose], [local], lock=lock))
        self.assertEqual(facts[key(S.MARKETPLACE, 'local')].value, hs.CheckoutMarketplace('https://github.com/example/local.git', C1))
        self.assertEqual(facts[key(S.PLUGIN, 'prose@local')].value, hs.PluginValue(True, hs.Released('0.2.0+codex.1')))

    def test_a_repository_engine_install_uses_its_verified_release_while_native_installs_keep_the_git_pin(self):
        source = d.RepoSource('skills')
        package = d.Package('rpi', d.Visibility.PUBLIC, '', source, {}, d.ToolNotes.AUDIT,
                            d.CodexPlacement((d.EngineInstall(True), d.MarketplaceInstall('skills', True))), None)
        lock = {'rpi': d.Lock(D1, {hs.Host.CODEX: d.LockedTree('1.0.0', D2)})}
        facts = codex_host.CodexState(hs.DeviceRoots.current(), 'codex').project(setup([package], [SKILLS], lock=lock))
        self.assertEqual(facts[key(S.PLUGIN, 'rpi@personal')].value, hs.PluginValue(True, hs.Released('1.0.0')))
        self.assertEqual(facts[key(S.PLUGIN, 'rpi@skills')].value, hs.PluginValue(True, hs.Pinned(C2)))
        self.assertEqual(facts[key(S.TREE, 'rpi@personal')].value, hs.TreeValue(D2))

    def test_trusted_hooks_without_a_locked_count_project_nothing(self):
        facts = codex_host.CodexState(hs.DeviceRoots.current(), 'codex').project(setup([IMPECCABLE], [IMPECCABLE_REPO]))
        self.assertEqual(sorted(k.surface.name for k in facts), ['MARKETPLACE', 'OVERLAY', 'PLUGIN'])

    def test_a_disabled_build_is_not_pinned(self):
        disabled = package('kit', KIT.source, d.CodexPlacement((d.EngineInstall(False),)))
        lock = {'kit': d.Lock(D1, {hs.Host.CODEX: d.LockedTree('1.0', D1)})}
        facts = codex_host.CodexState(hs.DeviceRoots.current(), 'codex').project(setup([disabled], lock=lock))
        self.assertEqual(facts[key(S.PLUGIN, 'kit@personal')].value, hs.PluginValue(False, hs.Unpinned()))
        self.assertNotIn(key(S.TREE, 'kit@personal'), facts)


def plugin(home, plugin_id, enabled, version, source):
    name, marketplace = plugin_id.split('@')
    return {'pluginId': plugin_id, 'name': name, 'marketplaceName': marketplace, 'version': version,
            'installed': True, 'enabled': enabled, 'source': source}


class Device(homes.Home):
    """A Codex home recorded from the shapes `codex plugin list --json` and config.toml have today."""

    def __enter__(self):
        super().__enter__()
        r = self.roots
        self.inventory = [
            plugin(self, 'kit@personal', True, '1.0', {'source': 'local', 'path': str(r.engine / 'kit/1.0')}),
            plugin(self, 'impeccable@personal', True, '4.5.2', {'source': 'git-subdir', 'url': 'u', 'path': 'plugin', 'ref': C1}),
            plugin(self, 'rpi@skills', True, '1.0.0', {'source': 'local', 'path': str(r.codex_home / '.tmp/marketplaces/skills/plugins/rpi')}),
            plugin(self, 'sharepoint@openai-curated-remote', True, '1', {'source': 'remote'}),
            plugin(self, 'latex@openai-bundled', True, '0.2.8', {'source': 'local', 'path': 'x'}),
        ]
        self.write(r.codex_home / 'config.toml', f'''
[marketplaces.skills]
source_type = "git"
source = "https://github.com/example/skills.git"
ref = "{C2}"

[marketplaces.openai-bundled]
source_type = "local"
source = "{r.codex_home}/.tmp/bundled-marketplaces/openai-bundled"

[plugins."sharepoint@openai-curated-remote"]
enabled = false

[plugins."kit@personal"]
enabled = true

[plugins."gone@openai-curated"]
enabled = false

[[skills.config]]
name = "kit:fix-ci"
enabled = false

[[skills.config]]
name = "kit:weekly-review"
enabled = true

[[skills.config]]
name = "sharepoint:search"
enabled = true

[[skills.config]]
name = "retired:x"
enabled = false

[[skills.config]]
name = "plugin-creator"
enabled = false

[[skills.config]]
path = "{r.codex_home}/skills/define-goal/SKILL.md"
enabled = false

[[skills.config]]
path = "{r.home}/Documents/project/.codex/skills/x/SKILL.md"
enabled = false

[hooks.state."impeccable@personal:hooks/hooks.json:stop:0:0"]
enabled = true
trusted_hash = "sha256:x"

[projects."{r.home}/Documents/project"]
trust_level = "trusted"
''')
        cache = r.codex_home / 'plugins/cache'
        for name in ('fix-ci', 'weekly-review'):
            self.skill(cache / 'personal/kit/1.0', name)
        self.skill(cache / 'personal/kit/1.0', 'deslop', explicit_sidecar=True)
        self.skill(cache / 'personal/impeccable/4.5.2', 'impeccable', body='Track it with TodoWrite.\n')
        self.write(cache / 'personal/impeccable/4.5.2/hooks/hooks.json',
                   json.dumps({'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'x'}]}]}}))
        self.skill(cache / 'skills/rpi/1.0.0', 'rpi', explicit_sidecar=True)
        self.skill(cache / 'openai-curated-remote/sharepoint/1', 'search')
        self.skill(cache / 'openai-bundled/latex/0.2.8', 'texlive', explicit_sidecar=True)
        self.write(r.codex_home / 'agents/impeccable_documenter.toml', f'# Generated by {r.engine}/codex_agents.py. Do not edit.\n')
        self.write(r.codex_home / 'skills/define-goal/SKILL.md', '---\nname: define-goal\n---\n')
        self.write(r.codex_home / 'skills/.system/skill-creator/SKILL.md', '---\nname: skill-creator\n---\n')
        self.write(r.codex_home / 'AGENTS.md', '')
        self.write(r.agents_home / 'plugins/marketplace.json',
                   '{"name": "personal", "plugins": [{"name": "kit"}, {"name": "impeccable"}, {"name": "visualize"}]}')
        self.write(r.codex_home / '.tmp/marketplaces/skills/.codex-marketplace-install.json', json.dumps(
            {'source_type': 'git', 'source': 'https://github.com/example/skills.git', 'ref_name': C2, 'revision': C2}))
        self.available = []
        return self

    def state(self, list_answer=None):
        answers = {'--version': 'codex-cli 0.0.0\n'}
        if list_answer is not False:
            answers['plugin list --available --json'] = list_answer or json.dumps(
                {'installed': self.inventory, 'available': self.available})
        return codex_host.CodexState(self.roots, self.cli('codex', answers))

    def checkout(self, name, origin):
        clone = self.roots.home / 'Documents' / name
        git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@t', '-C', str(clone)]
        self.write(clone / '.agents/plugins/marketplace.json', '{"name": "%s", "plugins": []}' % name)
        subprocess.run(['git', 'init', '-q', str(clone)], check=True)
        subprocess.run([*git, 'remote', 'add', 'origin', origin], check=True)
        subprocess.run([*git, 'add', '-A'], check=True)
        subprocess.run([*git, 'commit', '-qm', 'marketplace'], check=True)
        return clone

    def fetched_checkout(self, origin='https://github.com/example/skills.git'):
        clone = self.roots.codex_home / '.tmp/marketplaces/skills'
        (clone / '.codex-marketplace-install.json').unlink()
        self.skill(clone / 'plugins/rpi', 'rpi', explicit_sidecar=True)
        git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@t', '-C', str(clone)]
        for args in (('init', '-q'), ('remote', 'add', 'origin', origin), ('add', '-A'), ('commit', '-qm', 'Fetched marketplace')):
            subprocess.run([*git, *args], check=True, capture_output=True)
        return clone, subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True).strip()


class ObserveTest(unittest.TestCase):
    maxDiff = None
    def test_staged_engine_releases_require_the_exact_registered_package_and_content_address(self):
        for scenario in ('exact', 'unregistered', 'wrong-package', 'wrong-digest'):
            with self.subTest(scenario=scenario), Device() as device:
                roots = device.roots
                source = roots.codex_home / 'plugins/cache/personal/kit/1.0'
                digest = str(roots.tree_digest(source)).removeprefix('sha256:')
                staged = roots.agents_home / 'plugins/sources' / ('other' if scenario == 'wrong-package' else 'kit') / (
                    '0' * 64 if scenario == 'wrong-digest' else digest)
                staged.parent.mkdir(parents=True)
                shutil.copytree(source, staged)
                device.inventory[0]['source'] = {'source': 'local', 'path': str(staged)}
                catalog = roots.agents_home / 'plugins/marketplace.json'
                data = json.loads(catalog.read_text())
                data['plugins'][0]['source'] = {'source': 'local', 'path': './unrelated' if scenario == 'unregistered'
                                                else './' + staged.relative_to(roots.home).as_posix()}
                catalog.write_text(json.dumps(data))
                observation = device.state().observe(hs.Request())
                key_ = key(S.PLUGIN, 'kit@personal')
                if scenario == 'exact':
                    self.assertEqual(observation.facts[key_].value, hs.PluginValue(True, hs.Released('1.0')))
                    self.assertEqual(observation.details[key_].release, staged)
                else:
                    self.assertNotIn(key_, observation.facts)
                    self.assertEqual([unknown.subjects for unknown in observation.unknown],
                                     [frozenset({'kit@personal'})])

    def test_reads_effective_state_from_the_cli_config_and_caches(self):
        with Device() as device:
            observation = device.state().observe(hs.Request())
            loose_digest = device.roots.tree_digest(device.roots.codex_home / 'skills/define-goal')
        kit = key(S.PLUGIN, 'kit@personal')
        values = {k: f.value for k, f in observation.facts.items()}
        self.assertEqual(values, {
            key(S.MARKETPLACE, 'skills'): hs.GitMarketplace('https://github.com/example/skills.git', C2),
            key(S.MARKETPLACE, 'personal'): hs.EngineMarketplace(),
            kit: hs.PluginValue(True, hs.Released('1.0')),
            key(S.PLUGIN, 'impeccable@personal'): hs.PluginValue(True, hs.Pinned(C1)),
            key(S.PLUGIN, 'rpi@skills'): hs.PluginValue(True, hs.Pinned(C2)),
            key(S.PLUGIN, 'sharepoint@openai-curated-remote'): hs.PluginValue(True, hs.Unpinned()),
            key(S.PLUGIN, 'latex@openai-bundled'): hs.PluginValue(True, hs.Unpinned()),
            key(S.SKILL, 'kit:fix-ci'): M.OFF,
            key(S.SKILL, 'kit:deslop'): M.EXPLICIT,
            key(S.SKILL, 'rpi:rpi'): M.EXPLICIT,
            key(S.SKILL, 'sharepoint:search'): M.ON,
            key(S.SKILL, 'plugin-creator'): M.OFF,
            key(S.SKILL, '{codex_home}/skills/define-goal/SKILL.md'): M.OFF,
            key(S.AGENT, 'impeccable_documenter.toml'): hs.FileValue(hs.bytes_digest(b'# Generated by {engine}/codex_agents.py. Do not edit.\n')),
            key(S.HOOK, 'impeccable@personal'): hs.Approved(1),
            key(S.LOOSE_SKILL, '{codex_home}/skills/define-goal'): hs.FileValue(loose_digest),
            key(S.RULES, 'AGENTS.md'): hs.FileValue(hs.bytes_digest(b'')),
        })
        self.assertEqual(observation.facts[key(S.SKILL, 'kit:fix-ci')].follows, kit)
        self.assertEqual(observation.offered, frozenset({'kit@personal', 'impeccable@personal', 'rpi@skills',
                                                         'sharepoint@openai-curated-remote', 'latex@openai-bundled'}))
        self.assertEqual([(n.kind.value, n.subject, n.detail) for n in observation.notes], [
            ('residue', 'personal', '1 marketplace entries without an install: visualize'),
            ('residue', 'sharepoint@openai-curated-remote', 'config.toml says off, Codex reports on'),
            ('residue', 'gone@openai-curated', 'config.toml has an entry for a plugin Codex does not have'),
            ('residue', 'skills.config', '1 toggles for 1 plugins Codex no longer has: retired'),
            ('residue', 'skills.config', '1 enables on skills of pinned plugins, which are on by default'),
            ('device-local', 'project trusts', '1 project trusts'),
            ('device-local', 'project skill toggles', '1 project skill toggles'),
            ('device-local', 'hook trust hashes', '1 hook trust hashes'),
        ])
        self.assertEqual(observation.unknown, ())

    def test_approvals_count_only_hooks_the_installed_plugin_defines(self):
        with Device() as device:
            device.write(device.roots.codex_home / 'plugins/cache/personal/impeccable/4.5.2/hooks/hooks.json', json.dumps({'hooks': {
                'SessionStart': [{'hooks': [{'type': 'command', 'command': 'a'}]}],
                'PostToolUse': [{'matcher': 'Edit', 'hooks': [{'type': 'command', 'command': 'b'}, {'type': 'command', 'command': 'c'}]}]}}))
            config = device.roots.codex_home / 'config.toml'
            config.write_text(config.read_text() + '\n[hooks.state."impeccable@personal:hooks/hooks.json:post_tool_use:0:1"]\n'
                              'enabled = true\ntrusted_hash = "sha256:y"\n')
            observation = device.state().observe(hs.Request())
        self.assertEqual(observation.facts[key(S.HOOK, 'impeccable@personal')].value, hs.Approved(1))
        self.assertEqual(observation.details[key(S.PLUGIN, 'impeccable@personal')].hooks, 3)
        self.assertIn(('residue', 'hooks.state', '1 approvals of hooks no installed plugin defines'),
                      [(n.kind.value, n.subject, n.detail) for n in observation.notes])

    def test_a_disabled_install_never_lends_its_flags_and_alone_is_read_only_where_named(self):
        with Device() as device:
            cache = device.roots.codex_home / 'plugins/cache'
            device.inventory += [plugin(device, 'kit@other', False, '0.9', {'source': 'local', 'path': 'x'}),
                                 plugin(device, 'solo@other', False, '1', {'source': 'local', 'path': 'x'})]
            device.skill(cache / 'other/kit/0.9', 'weekly-review', explicit_sidecar=True)
            device.skill(cache / 'other/solo/1', 'x', explicit_sidecar=True)
            config = device.roots.codex_home / 'config.toml'
            config.write_text(config.read_text() + '\n[marketplaces.other]\nsource_type = "local"\nsource = "/nowhere"\n')
            named = device.state().observe(hs.Request(skills=frozenset({'kit:weekly-review', 'solo:x'})))
            unnamed = device.state().observe(hs.Request())
            everything = device.state().observe(hs.Request(everything=True))
        self.assertNotIn(key(S.SKILL, 'kit:weekly-review'), named.facts)
        self.assertEqual(named.facts[key(S.SKILL, 'solo:x')].value, M.EXPLICIT)
        self.assertNotIn(key(S.SKILL, 'solo:x'), unnamed.facts)
        self.assertEqual(everything.facts[key(S.SKILL, 'solo:x')].value, M.EXPLICIT)
        self.assertNotIn(key(S.SKILL, 'kit:weekly-review'), everything.facts)

    def test_a_name_toggle_of_a_loose_skill_is_its_placement_toggle(self):
        with Device() as device:
            device.write(device.roots.codex_home / 'skills/cli-creator/SKILL.md', '---\nname: cli-creator\n---\n')
            config = device.roots.codex_home / 'config.toml'
            config.write_text(config.read_text() + '\n[[skills.config]]\nname = "cli-creator"\nenabled = false\n')
            observation = device.state().observe(hs.Request())
        self.assertEqual(observation.facts[key(S.SKILL, '{codex_home}/skills/cli-creator/SKILL.md')].value, M.OFF)
        self.assertNotIn(key(S.SKILL, 'cli-creator'), observation.facts)

    def test_catalog_skills_are_read_only_when_the_description_names_them(self):
        with Device() as device:
            wanted = key(S.SKILL, 'latex:texlive')
            observation = device.state().observe(hs.Request(skills=frozenset({'latex:texlive'})))
        self.assertEqual(observation.facts[wanted].value, M.EXPLICIT)

    def test_plugins_the_cli_lists_as_available_are_offered(self):
        with Device() as device:
            device.available = [plugin(device, 'gmail@openai-curated-remote', False, '1', {'source': 'remote'}) | {'installed': False}]
            observation = device.state().observe(hs.Request())
        self.assertIn('gmail@openai-curated-remote', observation.offered)
        self.assertNotIn(key(S.PLUGIN, 'gmail@openai-curated-remote'), observation.facts)

    def test_a_git_marketplace_plugin_is_pinned_by_what_codex_fetched_not_the_configured_ref(self):
        with Device() as device:
            config = device.roots.codex_home / 'config.toml'
            config.write_text(config.read_text().replace(f'ref = "{C2}"', f'ref = "{C1}"'))
            fetched = device.state().observe(hs.Request())
            (device.roots.codex_home / '.tmp/marketplaces/skills/.codex-marketplace-install.json').unlink()
            unrecorded = device.state().observe(hs.Request())
        self.assertEqual(fetched.facts[key(S.MARKETPLACE, 'skills')].value,
                         hs.GitMarketplace('https://github.com/example/skills.git', C1))
        self.assertEqual(fetched.facts[key(S.PLUGIN, 'rpi@skills')].value, hs.PluginValue(True, hs.Pinned(C2)))
        self.assertNotIn(key(S.PLUGIN, 'rpi@skills'), unrecorded.facts)
        self.assertEqual([(u.source, u.surfaces, u.subjects) for u in unrecorded.unknown],
                         [('{codex_home}/.tmp/marketplaces/skills/plugins/rpi', frozenset({S.PLUGIN}), frozenset({'rpi@skills'}))])
        self.assertEqual(unrecorded.unknown[0].reason, 'Codex keeps no record of the commit it fetched for skills')

    def test_a_native_temporary_git_clone_records_its_actual_pin_without_a_sidecar(self):
        with Device() as device:
            _, commit = device.fetched_checkout()
            observation = device.state().observe(hs.Request())
        self.assertNotEqual(commit, C2)
        self.assertEqual(observation.facts[key(S.PLUGIN, 'rpi@skills')].value, hs.PluginValue(True, hs.Pinned(commit)))
        self.assertEqual(observation.facts[key(S.MARKETPLACE, 'skills')].value,
                         hs.GitMarketplace('https://github.com/example/skills.git', C2))
        self.assertEqual(observation.unknown, ())

    def test_a_temporary_clone_cannot_claim_a_pin_from_another_origin_or_local_changes(self):
        for origin, dirty, reason in (
            ('https://github.com/example/other.git', False, 'different origin'),
            ('https://github.com/example/skills.git', True, 'local changes'),
        ):
            with self.subTest(origin=origin, dirty=dirty), Device() as device:
                clone, _ = device.fetched_checkout(origin)
                if dirty:
                    (clone / 'unreviewed').write_text('Local change')
                observation = device.state().observe(hs.Request())
            self.assertNotIn(key(S.PLUGIN, 'rpi@skills'), observation.facts)
            self.assertEqual([(u.surfaces, u.subjects) for u in observation.unknown],
                             [(frozenset({S.PLUGIN}), frozenset({'rpi@skills'}))])
            self.assertIn(reason, observation.unknown[0].reason)

    def test_a_plugin_served_from_a_checkout_is_pinned_by_its_version(self):
        with Device() as device:
            clone = device.checkout('local', 'https://github.com/example/local.git')
            config = device.roots.codex_home / 'config.toml'
            config.write_text(config.read_text() + f'\n[marketplaces.local]\nsource_type = "local"\nsource = "{clone}"\n')
            device.inventory.append(plugin(device, 'prose@local', True, '0.2.0+codex.1',
                                           {'source': 'git-subdir', 'url': 'https://github.com/example/prose.git', 'path': 'plugin', 'ref': 'main'}))
            device.skill(device.roots.codex_home / 'plugins/cache/local/prose/0.2.0+codex.1', 'prose')
            observation = device.state().observe(hs.Request())
            head = subprocess.run(['git', '-C', str(clone), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
        self.assertEqual(observation.facts[key(S.MARKETPLACE, 'local')].value,
                         hs.CheckoutMarketplace('https://github.com/example/local.git', head))
        self.assertEqual(observation.facts[key(S.PLUGIN, 'prose@local')].value, hs.PluginValue(True, hs.Released('0.2.0+codex.1')))

    def test_a_checkout_git_cannot_read_is_unknown_not_missing(self):
        with Device() as device:
            broken = device.write(device.roots.home / 'Documents/broken/.git', 'gitdir: /nowhere\n').parent
            config = device.roots.codex_home / 'config.toml'
            config.write_text(config.read_text() + f'\n[marketplaces.broken]\nsource_type = "local"\nsource = "{broken}"\n')
            observation = device.state().observe(hs.Request())
        self.assertNotIn(key(S.MARKETPLACE, 'broken'), observation.facts)
        self.assertEqual([(u.source, u.surfaces, u.subjects) for u in observation.unknown],
                         [('{home}/Documents/broken', frozenset({S.MARKETPLACE}), frozenset({'broken'}))])

    def test_a_malformed_sidecar_is_a_gap_not_a_crash(self):
        with Device() as device:
            device.write(device.roots.codex_home / 'plugins/cache/personal/kit/1.0/skills/deslop/agents/openai.yaml', 'policy: [\n')
            observation = device.state().observe(hs.Request())
        self.assertNotIn(key(S.SKILL, 'kit:deslop'), observation.facts)
        self.assertEqual([(u.source, u.surfaces, u.subjects) for u in observation.unknown],
                         [('{codex_home}/plugins/cache/personal/kit/1.0', frozenset({S.SKILL}), frozenset({'kit@personal'}))])
        self.assertIn('while parsing a flow node', observation.unknown[0].reason)

    def test_a_failed_inventory_is_unknown_even_when_nothing_is_expected(self):
        with Device() as device:
            observation = device.state(list_answer=False).observe(hs.Request())
        self.assertNotIn(key(S.PLUGIN, 'kit@personal'), observation.facts)
        self.assertEqual([(u.source, u.surfaces, u.subjects) for u in observation.unknown],
                         [('codex plugin list --available --json',
                           frozenset({S.PLUGIN, S.TREE, S.OVERLAY, S.HOOK, S.SKILL}), None)])
        self.assertTrue(observation.unknown[0].reason.startswith('Command'), observation.unknown[0].reason)
        self.assertEqual(observation.offered, frozenset())


class TreeTest(unittest.TestCase):
    def test_an_overlaid_cache_with_a_receipt_reads_as_its_release(self):
        with Device() as device:
            r = device.roots
            root = r.codex_home / 'plugins/cache/personal/kit/1.0'
            release = r.tree_digest(root)
            skill = root / 'skills/deslop/SKILL.md'
            sidecar = root / 'skills/fix-ci/agents/openai.yaml'
            original = skill.read_text()
            skill.write_text(original.replace('---\nUse', 'disable-model-invocation: true\n---\nUse'))
            device.write(sidecar, 'policy:\n  allow_implicit_invocation: false\n')
            device.receipt({skill: {'base': original, 'effective': skill.read_text()},
                            sidecar: {'base': None, 'effective': sidecar.read_text()}})
            tree = key(S.TREE, 'kit@personal')
            request = hs.Request(trees=frozenset({'kit@personal'}))
            observation = device.state().observe(request)
            self.assertEqual(observation.facts[tree].value, hs.TreeValue(release))
            self.assertEqual(observation.edits, frozenset())
            skill.write_text('a hand edit\n')
            observation = device.state().observe(request)
            self.assertNotEqual(observation.facts[tree].value, hs.TreeValue(release))
            self.assertEqual(observation.edits, frozenset({tree}))

    def test_overlay_reads_as_its_spec_only_when_every_note_is_applied(self):
        with Device() as device:
            overlay = key(S.OVERLAY, 'impeccable@personal')
            spec = hs.OverlaySpec(NOTES, None)
            expected = hs.Request(overlays={'impeccable@personal': spec})
            self.assertEqual(device.state().observe(expected).facts[overlay].value, hs.OverlayPending(1))
            path = device.roots.codex_home / 'plugins/cache/personal/impeccable/4.5.2/skills/impeccable/SKILL.md'
            path.write_text(path.read_text().replace('TodoWrite', 'TodoWrite (Codex: `update_plan`)'))
            self.assertEqual(device.state().observe(expected).facts[overlay].value, spec)

    def test_overlay_checks_the_adaptation_against_its_clone(self):
        with Device() as device:
            r = device.roots
            clone = r.engine / 'harness-adaptations/impeccable'
            git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@t', '-C', str(clone)]
            device.write(clone / 'skills/impeccable/SKILL.md', '---\nname: impeccable\ndescription: impeccable\n---\nTrack it with TodoWrite.\n')
            subprocess.run(['git', 'init', '-q', str(clone)], check=True)
            subprocess.run([*git, 'remote', 'add', 'origin', 'https://github.com/example/adaptations.git'], check=True)
            subprocess.run([*git, 'add', '-A'], check=True)
            subprocess.run([*git, 'commit', '-qm', 'upstream'], check=True)
            base = subprocess.run([*git, 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
            device.write(clone / 'skills/impeccable/SKILL.md', '---\nname: impeccable\ndescription: impeccable\n---\nTrack it with update_plan.\n')
            subprocess.run([*git, 'commit', '-qam', 'adapted'], check=True)
            adapted = subprocess.run([*git, 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
            overlay = key(S.OVERLAY, 'impeccable@personal')
            spec = hs.OverlaySpec((), d.Adaptation('https://github.com/example/adaptations.git', base, adapted))
            expected = hs.Request(overlays={'impeccable@personal': spec})
            self.assertEqual(device.state().observe(expected).facts[overlay].value, hs.OverlayPending(1))
            path = r.codex_home / 'plugins/cache/personal/impeccable/4.5.2/skills/impeccable/SKILL.md'
            path.write_text(path.read_text().replace('TodoWrite', 'update_plan'))
            self.assertEqual(device.state().observe(expected).facts[overlay].value, spec)
            subprocess.run(['rm', '-rf', str(clone)], check=True)
            observation = device.state().observe(expected)
            self.assertNotIn(overlay, observation.facts)
            self.assertEqual([(u.surfaces, u.subjects, u.reason) for u in observation.unknown],
                             [(frozenset({S.OVERLAY}), frozenset({'impeccable@personal'}),
                               'no clone of https://github.com/example/adaptations.git under {engine}/harness-adaptations')])


if __name__ == '__main__':
    unittest.main()
