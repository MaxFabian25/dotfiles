from dataclasses import replace
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
import codex_host
import claude_host
import device
import drift
d, hs = description, host_state
S, M, K = hs.Surface, hs.SkillMode, drift.Kind
C1, OLD = 'a' * 40, 'c' * 40


class Device(homes.Home):
    """Both hosts running one engine build (kit, aligned into Claude) and a disabled native duplicate."""

    def __enter__(self):
        super().__enter__()
        r = self.roots
        self.codex_inventory = [{'pluginId': 'kit@personal', 'name': 'kit', 'marketplaceName': 'personal', 'version': '1.0',
                                 'installed': True, 'enabled': True, 'source': {'source': 'local', 'path': str(r.engine / 'kit/1.0')}}]
        self.toggles = '[[skills.config]]\nname = "kit:fix-ci"\nenabled = false\n'
        codex_cache = r.codex_home / 'plugins/cache/personal/kit/1.0'
        for name in ('fix-ci', 'weekly-review'):
            self.skill(codex_cache, name)
        self.skill(codex_cache, 'deslop', explicit_sidecar=True)
        self.codex_release = r.tree_digest(codex_cache)
        skill = codex_cache / 'skills/weekly-review/SKILL.md'
        original = skill.read_text()
        skill.write_text(original + 'Use TodoWrite (Codex: `update_plan`).\n')
        self.receipt({skill: {'base': original, 'effective': skill.read_text()}})
        self.write(r.agents_home / 'plugins/marketplace.json', '{"name": "personal", "plugins": [{"name": "kit"}]}')

        claude_cache = r.claude_home / 'plugins/cache'
        aligned = claude_cache / 'codex-aligned/kit/1.0-codex-aligned.x'
        self.skill(aligned, 'deslop', frontmatter_explicit=True)
        self.skill(aligned, 'weekly-review')
        self.write(aligned / '.claude-plugin/codex-alignment.json',
                   json.dumps({'codexSource': str(codex_cache), 'excludedSkillNames': ['kit:fix-ci']}))
        self.skill(claude_cache / 'interfaces/interfaces/1.10.3', 'better-interface', frontmatter_explicit=True)
        self.claude_inventory = [
            {'id': 'kit@codex-aligned', 'version': '1.0-codex-aligned.x', 'scope': 'user', 'enabled': True, 'installPath': str(aligned)},
            {'id': 'interfaces@interfaces', 'version': '1.10.3', 'scope': 'user', 'enabled': False,
             'installPath': str(claude_cache / 'interfaces/interfaces/1.10.3')}]
        self.write(r.claude_home / 'plugins/installed_plugins.json', json.dumps({'plugins': {
            'interfaces@interfaces': [{'scope': 'user', 'gitCommitSha': OLD}]}}))
        self.write(r.claude_home / 'marketplaces/codex-aligned/.claude-plugin/marketplace.json',
                   json.dumps({'name': 'codex-aligned', 'plugins': []}))
        marketplace = r.engine / '.sync/checkouts/interfaces'
        self.write(marketplace / '.claude-plugin/marketplace.json', json.dumps({'name': 'interfaces', 'plugins': []}))
        git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@t', '-C', str(marketplace)]
        for args in (('init', '-q'), ('remote', 'add', 'origin', 'https://github.com/example/interfaces.git'),
                     ('add', '-A'), ('commit', '-qm', 'Pinned marketplace')):
            subprocess.run([*git, *args], check=True, capture_output=True)
        self.marketplace_pin = subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True).strip()
        self.write(r.claude_home / 'settings.json', json.dumps({
            'enabledPlugins': {'kit@codex-aligned': True, 'interfaces@interfaces': False},
            'extraKnownMarketplaces': {
                'codex-aligned': {'source': {'source': 'directory', 'path': str(r.claude_home / 'marketplaces/codex-aligned')}},
                'interfaces': {'source': {'source': 'directory', 'path': str(marketplace)}}}}))
        kit = d.Package('kit', d.Visibility.PUBLIC, '',
                        d.BuildSource(d.GenericRecipe('https://github.com/example/kit.git', C1, None, 'plugin.json', ()), 1, 'Kit'),
                        {'fix-ci': M.OFF, 'deslop': M.EXPLICIT}, d.ToolNotes.AUDIT,
                        d.CodexPlacement((d.EngineInstall(True),)), d.ClaudePlacement((d.AlignedInstall(True),)))
        interfaces = d.Package('interfaces', d.Visibility.PUBLIC, '', d.RepoSource('interfaces'),
                               {'better-interface': M.EXPLICIT}, d.ToolNotes.AUDIT, None,
                               d.ClaudePlacement((d.MarketplaceInstall('interfaces', False),)))
        repo = d.Repo('interfaces', d.Visibility.PUBLIC, '', 'https://github.com/example/interfaces.git', self.marketplace_pin,
                      None, d.HostMarketplace('interfaces', d.MarketplaceMode.CHECKOUT))
        lock = {'kit': d.Lock(d.intent(kit, {}), {hs.Host.CODEX: d.LockedTree('1.0', self.codex_release),
                                              hs.Host.CLAUDE: d.LockedTree('1.0-codex-aligned.x', r.tree_digest(aligned))}),
                'interfaces': d.Lock(d.intent(interfaces, {'interfaces': repo}))}
        self.setup = d.Setup(roots=d.Roots(r.home / 'public/setup', r.home / 'private/setup'),
                             packages={'kit': kit, 'interfaces': interfaces}, repos={'interfaces': repo}, loose={},
                             host_skills={}, rules=None, note_rules=(), public_repositories=frozenset(), lock=lock)
        return self

    def device(self, codex_list=True, absent=(), codex_available=()):
        r = self.roots
        self.write(r.codex_home / 'config.toml', self.toggles)
        answers = {'--version': 'codex-cli 1\n'}
        if codex_list:
            answers['plugin list --available --json'] = json.dumps({'installed': self.codex_inventory,
                                                                    'available': list(codex_available)})
        claude = {'--version': '2 (Claude Code)\n',
                  'plugin list --available --json': json.dumps({'installed': self.claude_inventory, 'available': []})}
        hosts = [codex_host.CodexState(r, self.cli('codex', answers)), claude_host.ClaudeState(r, self.cli('claude', claude))]
        return device.Device(hs.Platform.MACOS, r, tuple(h for h in hosts if h.host not in absent), tuple(absent))

    def diff(self, **options):
        return drift.diff(self.setup, self.device(**options))


def kinds(result):
    return [(x.kind, x.key.host.value if x.key else None, x.key.subject if x.key else None) for x in result.differences]


class DiffTest(unittest.TestCase):
    def test_attention_is_visible_even_when_its_plugin_is_missing(self):
        with Device() as device:
            result = device.diff()
        plugin = hs.FactKey(hs.Host.CODEX, S.PLUGIN, 'kit@personal')
        hook = hs.FactKey(hs.Host.CODEX, S.HOOK, 'kit@personal')
        result = replace(result, differences=(
            drift.Difference(K.MISSING, plugin, hs.PluginValue(True, hs.Unpinned()), None, None),
            drift.Difference(K.APPROVAL, hook, hs.Approved(1), None, None, follows=plugin),
        ))
        rendered = drift.render(result)
        self.assertIn('\nattention\n', rendered)
        self.assertIn('hook', rendered)
        self.assertIn('approval', rendered)
        self.assertNotIn('facts follow it', rendered)

    def test_a_claude_mode_is_in_step_with_an_aligned_copy_that_keeps_the_skill(self):
        with Device() as device:
            aligned = device.roots.claude_home / 'plugins/cache/codex-aligned/kit/1.0-codex-aligned.x'
            device.skill(aligned, 'fix-ci', frontmatter_explicit=True)
            (aligned / '.claude-plugin/codex-alignment.json').write_text(json.dumps({'excludedSkillNames': []}))
            kit = device.setup.packages['kit']
            kit = replace(kit, claude=replace(kit.claude, modes={'fix-ci': M.EXPLICIT}))
            lock = replace(device.setup.lock['kit'], intent=d.intent(kit, {}), trees={
                **device.setup.lock['kit'].trees, hs.Host.CLAUDE: d.LockedTree('1.0-codex-aligned.x', device.roots.tree_digest(aligned))})
            device.setup = replace(device.setup, packages={**device.setup.packages, 'kit': kit},
                                   lock={**device.setup.lock, 'kit': lock})
            result = device.diff()
        self.assertEqual(result.differences, ())
        self.assertEqual(result.exit_code, 0)

    def test_a_description_against_its_own_home_is_in_step(self):
        with Device() as device:
            result = device.diff()
        self.assertEqual(result.differences, ())
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.trees, 2)
        self.assertIn('\nIn step. No differences.\n', drift.render(result))
        self.assertEqual([(n.kind, n.subject, n.detail) for n in result.notes if n.kind == hs.NoteKind.DORMANT],
                         [(hs.NoteKind.DORMANT, 'interfaces@interfaces',
                           f'disabled at {OLD[:7]}; the description pins {device.marketplace_pin[:7]}')])
        self.assertEqual(json.loads(drift.to_json(result))['exit'], 0)

    def test_flipping_one_toggle_yields_exactly_one_missing(self):
        with Device() as device:
            device.toggles = ''
            result = device.diff()
        self.assertEqual(kinds(result), [(K.MISSING, 'codex', 'kit:fix-ci')])
        self.assertEqual((result.differences[0].expected, result.differences[0].observed), (M.OFF, None))
        self.assertEqual(result.exit_code, 1)

    def test_removing_the_receipt_yields_a_local_edit(self):
        with Device() as device:
            (device.roots.codex_home / 'skill-updates/invocation-overrides/state.json').unlink()
            result = device.diff()
        self.assertEqual(kinds(result), [(K.LOCAL_EDIT, 'codex', 'kit@personal')])
        self.assertEqual(result.exit_code, 2)

    def test_an_undeclared_enabled_plugin_is_extra_and_a_disabled_one_is_residue(self):
        with Device() as device:
            stray = {'pluginId': 'stray@personal', 'name': 'stray', 'marketplaceName': 'personal', 'version': '1',
                     'installed': True, 'source': {'source': 'local', 'path': str(device.roots.engine / 'stray/1')}}
            device.skill(device.roots.codex_home / 'plugins/cache/personal/stray/1', 'x', explicit_sidecar=True)
            device.codex_inventory.append({**stray, 'enabled': True})
            extra = device.diff()
            device.codex_inventory[-1] = {**stray, 'enabled': False}
            residue = device.diff()
        self.assertEqual(kinds(extra), [(K.EXTRA, 'codex', 'stray@personal'), (K.EXTRA, 'codex', 'stray:x')])
        self.assertEqual(extra.differences[1].follows, hs.FactKey(hs.Host.CODEX, S.PLUGIN, 'stray@personal'))
        self.assertEqual(extra.exit_code, 1)
        self.assertEqual(kinds(residue), [])
        self.assertIn((hs.NoteKind.RESIDUE, 'stray@personal'), [(n.kind, n.subject) for n in residue.notes])

    def test_a_catalog_this_device_does_not_offer_is_a_note(self):
        with Device() as device:
            connector = d.Package('connector', d.Visibility.PRIVATE, '', None, {'tool': M.EXPLICIT}, d.ToolNotes.AUDIT,
                                  d.CodexPlacement((d.MarketplaceInstall('openai-bundled', True),)), None)
            device.setup = d.Setup(**{**device.setup.__dict__, 'packages': {**device.setup.packages, 'connector': connector}})
            result = device.diff()
        self.assertEqual(kinds(result), [])
        self.assertIn((hs.NoteKind.UNAVAILABLE, 'connector@openai-bundled'), [(n.kind, n.subject) for n in result.notes])
        self.assertEqual(result.exit_code, 0)

    def test_rules_and_host_toggles_never_borrow_a_package_name(self):
        with Device() as device:
            named = d.Package('rules', d.Visibility.PUBLIC, '', None, {}, d.ToolNotes.AUDIT, None, None)
            device.setup = d.Setup(**{**device.setup.__dict__, 'packages': {**device.setup.packages, 'rules': named},
                                      'rules': d.Rules(b'# rules\n'), 'host_skills': {hs.Host.CODEX: {'plugin-creator': M.OFF}}})
            result = device.diff()
        self.assertEqual(sorted((x.kind, x.key.subject, x.entry) for x in result.differences), [
            (K.MISSING, 'AGENTS.md', drift.EntryRef('rules/AGENTS.md', d.Visibility.PRIVATE)),
            (K.MISSING, 'CLAUDE.md', drift.EntryRef('rules/AGENTS.md', d.Visibility.PRIVATE)),
            (K.MISSING, 'plugin-creator', drift.EntryRef('hosts/codex.toml', d.Visibility.PRIVATE)),
        ])

    def test_a_catalog_plugin_the_device_offers_is_missing_and_one_it_lacks_is_a_note(self):
        with Device() as device:
            gmail = d.Package('gmail', d.Visibility.PRIVATE, '', None, {}, d.ToolNotes.AUDIT,
                              d.CodexPlacement((d.MarketplaceInstall('openai-curated-remote', True),)), None)
            device.setup = d.Setup(**{**device.setup.__dict__, 'packages': {**device.setup.packages, 'gmail': gmail}})
            offered = device.diff(codex_available=[{'pluginId': 'gmail@openai-curated-remote', 'name': 'gmail',
                                                    'marketplaceName': 'openai-curated-remote', 'installed': False}])
            lacking = device.diff()
        self.assertEqual(kinds(offered), [(K.MISSING, 'codex', 'gmail@openai-curated-remote')])
        self.assertEqual(offered.exit_code, 1)
        self.assertEqual(kinds(lacking), [])
        self.assertIn((hs.NoteKind.UNAVAILABLE, 'gmail@openai-curated-remote'), [(n.kind, n.subject) for n in lacking.notes])

    def test_an_equal_value_from_an_unread_source_is_incomplete(self):
        with Device() as device:
            device.toggles = '[[skills.config]\n'
            result = device.diff()
        self.assertEqual([(x.kind, x.key.subject if x.key else None) for x in result.differences if x.key and x.key.host == hs.Host.CODEX],
                         [(K.INCOMPLETE, 'kit@personal'), (K.INCOMPLETE, 'kit:deslop'), (K.INCOMPLETE, 'kit:fix-ci'),
                          (K.INCOMPLETE, 'personal')])
        self.assertTrue(all(x.detail.startswith('{codex_home}/config.toml: ') for x in result.differences), result.differences)
        self.assertEqual(result.exit_code, 3)

    def test_an_unreadable_inventory_is_incomplete(self):
        with Device() as device:
            result = device.diff(codex_list=False)
        self.assertEqual(kinds(result), [(K.INCOMPLETE, 'codex', 'kit@personal'), (K.INCOMPLETE, 'codex', 'kit@personal'),
                                         (K.INCOMPLETE, 'codex', 'kit:deslop'), (K.INCOMPLETE, 'codex', 'kit:fix-ci')])
        self.assertEqual({x.detail.split(':')[0] for x in result.differences}, {'codex plugin list --available --json'})
        self.assertEqual(result.exit_code, 3)

    def test_a_stale_entry_keeps_what_its_old_lock_covers_out_of_the_comparison(self):
        with Device() as device:
            role = device.write(device.roots.codex_home / 'agents/kit_reviewer.toml', 'name = "kit_reviewer"\n')
            kit = d.Package(**{**device.setup.packages['kit'].__dict__,
                               'codex': d.CodexPlacement((d.EngineInstall(True),), roles=d.PluginRoles('agents'))})
            lock = d.Lock(d.intent(kit, {}), device.setup.lock['kit'].trees, {'kit_reviewer.toml': device.roots.file_digest(role)})
            device.setup = d.Setup(**{**device.setup.__dict__, 'packages': {**device.setup.packages, 'kit': kit},
                                      'lock': {**device.setup.lock, 'kit': lock}})
            fresh = device.diff()
            edited = d.Package(**{**kit.__dict__, 'modes': {**kit.modes, 'weekly-review': M.EXPLICIT}})
            device.setup = d.Setup(**{**device.setup.__dict__, 'packages': {**device.setup.packages, 'kit': edited}})
            role.write_text('name = "kit_reviewer"\nedited = true\n')
            stale = device.diff()
        self.assertEqual(kinds(fresh), [])
        self.assertEqual(kinds(stale), [(K.STALE_LOCK, None, None), (K.MISSING, 'claude', 'kit@codex-aligned:weekly-review'),
                                        (K.MISSING, 'codex', 'kit:weekly-review')])

    def test_an_overlay_that_cannot_be_replayed_needs_a_person_and_says_why(self):
        with Device() as device:
            kit = d.Package(**{**device.setup.packages['kit'].__dict__, 'tool_notes': d.ToolNotes.APPLY})
            device.setup = d.Setup(**{**device.setup.__dict__, 'packages': {**device.setup.packages, 'kit': kit},
                                      'lock': {**device.setup.lock, 'kit': d.Lock(d.intent(kit, {}), device.setup.lock['kit'].trees)},
                                      'note_rules': (d.NoteRule(r'\bTodoWrite\b', ' (Codex: `update_plan`)'),)})
            device.write(device.roots.codex_home / 'plugins/cache/personal/kit/1.0/skills/deslop/SKILL.md', 'no frontmatter\n')
            result = device.diff()
        blocked = [x for x in result.differences if x.key and x.key.surface == S.OVERLAY and x.key.host == hs.Host.CODEX]
        self.assertEqual([(x.kind, x.kind.verdict) for x in blocked], [(K.OVERLAY_CONFLICT, drift.Verdict.ATTENTION)])
        self.assertIn('overlay-conflict  want applied · have Missing skill frontmatter', drift.render(result))

    def test_a_missing_host_and_a_stale_lock_need_a_person(self):
        with Device() as device:
            absent = device.diff(absent=(hs.Host.CLAUDE,))
            kit = device.setup.packages['kit']
            edited = d.Package(**{**kit.__dict__, 'modes': {**kit.modes, 'weekly-review': M.EXPLICIT}})
            device.setup = d.Setup(**{**device.setup.__dict__, 'packages': {**device.setup.packages, 'kit': edited}})
            stale = device.diff()
        self.assertEqual([(x.kind, x.host) for x in absent.differences], [(K.HOST_ABSENT, hs.Host.CLAUDE)])
        self.assertEqual(absent.exit_code, 2)
        self.assertEqual(kinds(stale), [(K.STALE_LOCK, None, None), (K.MISSING, 'claude', 'kit@codex-aligned:weekly-review'),
                                        (K.MISSING, 'codex', 'kit:weekly-review')])
        self.assertEqual(stale.exit_code, 2)
        self.assertIn('host-absent claude  host is not installed here', drift.render(absent))
        self.assertIn('stale-lock  the entry changed after its lock was written', drift.render(stale))

    def test_an_owned_source_edited_after_its_pin_needs_a_person(self):
        with Device() as device:
            source = device.roots.home / 'private/skills/mine'
            device.write(source / 'SKILL.md', '---\nname: mine\n---\nPinned\n')
            device.write(device.roots.claude_home / 'skills/mine/SKILL.md', '---\nname: mine\n---\nPinned\n')
            digest = device.roots.tree_digest(source)
            mine = d.LooseSkill('mine', d.Visibility.PRIVATE, '', d.LocalSource('skills/mine', digest), {d.SkillRoot.CLAUDE: True})
            device.setup = d.Setup(**{**device.setup.__dict__, 'loose': {'mine': mine}, 'lock': {
                **device.setup.lock, 'mine': d.Lock(d.intent(mine, {}), placements={d.SkillRoot.CLAUDE: digest})}})
            fresh = device.diff()
            (source / 'SKILL.md').write_text('---\nname: mine\n---\nEdited\n')
            edited = device.diff()
        self.assertEqual(kinds(fresh), [])
        self.assertEqual(kinds(edited), [(K.STALE_LOCK, None, None)])
        self.assertEqual(edited.exit_code, 2)
        self.assertIn('stale-lock  skills/mine changed after its tree was pinned; '
                      'run lock_setup.py --entry mine --refresh-owned --write', drift.render(edited))


if __name__ == '__main__':
    unittest.main()
