from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import antigravity
from configuration import BlockedConfiguration, patch_fields
from description import (CodexPlacement, EngineInstall, LocalSource, Lock, LockedTree, LooseSkill,
                         Package, Roots, Rules, Setup, SkillRoot, SnapshotSource, ToolNotes, Visibility, intent)
from homes import Home
from host_state import Host, Platform, SkillMode
from invocation_overrides import frontmatter
from sync_sources import BuildError, Builder


class AntigravityTests(unittest.TestCase):
    def fixture(self, home, *, modes=None, names=('auto', 'manual', 'hidden', 'forced')):
        source = home.roots.home / 'source'
        for name in names:
            home.skill(source, name, explicit_sidecar=name == 'manual', frontmatter_explicit=name == 'forced')
        home.write(source / '.codex-plugin/plugin.json', '{"name":"kit","version":"1.0.0"}\n')
        home.write(source / 'references/shared.md', 'Package-relative support.\n')
        package = Package('kit', Visibility.PUBLIC, '', LocalSource('source', home.roots.tree_digest(source)),
                          modes or {}, ToolNotes.AUDIT, CodexPlacement((EngineInstall(True),)), None)
        setup = Setup(Roots(home.roots.home / 'setup', home.roots.home / 'private/setup'),
                      {'kit': package}, {}, {}, {}, Rules(b'Shared instructions.\n'), (), frozenset(), {})
        home.write(setup.roots.private.parent / 'config/antigravity/setup.toml', 'enabled = true\nbaseline = "codex"\n')
        builder = Builder(setup, home.roots, home.roots.engine)
        built = builder.codex(package, verify=False)
        setup = replace(setup, lock={'kit': Lock(intent(package, {}), {Host.CODEX: LockedTree(built.version, built.digest)})})
        return setup, Builder(setup, home.roots, home.roots.engine), source

    def generated(self, home, setup, builder, name='output'):
        plan = antigravity.plan(setup, home.roots, Platform.LINUX)
        path = antigravity.materialise(plan, builder, home.roots.home / name)
        return plan, path, json.loads((path / 'projection.json').read_bytes())

    def test_projection_preserves_payload_policy_rules_and_skills_manifest(self):
        with Home() as home:
            setup, builder, source = self.fixture(home, modes={'hidden': SkillMode.OFF, 'forced': SkillMode.ON})
            before = home.roots.tree_digest(source)
            plan, output, receipt = self.generated(home, setup, builder)
            self.assertEqual(sorted(path.name for path in (output / 'skills').iterdir()),
                             ['kit-auto', 'kit-forced', 'kit-manual'])
            for name, implicit in [('auto', True), ('manual', False), ('forced', True)]:
                path = output / 'skills' / ('kit-' + name) / 'SKILL.md'
                _, metadata = frontmatter(path.read_text())
                if implicit:
                    self.assertNotIn('disable-model-invocation', metadata)
                else:
                    self.assertTrue(metadata['disable-model-invocation'])
                record = next(record for record in receipt['skills'] if record['id'] == 'kit-' + name)
                self.assertTrue((output / record['source']).is_file())
                self.assertIn((plan.destination / record['source']).as_posix(), path.read_text())
            self.assertEqual((output / 'payload/kit/references/shared.md').read_bytes(), b'Package-relative support.\n')
            by_name = {item.name: item for item in plan.configurations}
            self.assertEqual(by_name['antigravity-rules'].destination, home.roots.home / '.gemini/AGENTS.md')
            self.assertEqual(by_name['antigravity-rules'].data, b'Shared instructions.\n')
            self.assertEqual(by_name['antigravity-skills'].destination, home.roots.home / '.gemini/config/skills.json')
            self.assertEqual(by_name['antigravity-skills'].fields[('entries',)],
                             [{'path': str(plan.destination / 'skills')}])
            self.assertEqual(home.roots.tree_digest(source), before)
            _, again, _ = self.generated(home, setup, builder, 'again')
            self.assertEqual(home.roots.tree_digest(output), home.roots.tree_digest(again))

    def test_host_modes_override_policy_without_changing_the_locked_build(self):
        with Home() as home:
            setup, _, _ = self.fixture(home, modes={'manual': SkillMode.EXPLICIT})
            setup = replace(setup, host_skills={Host.CODEX: {'kit:manual': SkillMode.ON, 'kit:auto': SkillMode.OFF}})
            builder = Builder(setup, home.roots, home.roots.engine)
            _, output, _ = self.generated(home, setup, builder)
            self.assertFalse((output / 'skills/kit-auto').exists())
            _, header = frontmatter((output / 'skills/kit-manual/SKILL.md').read_text())
            self.assertNotIn('disable-model-invocation', header)

    def test_loose_skills_from_agents_and_codex_roots_are_projected_and_off_skills_omitted(self):
        with Home() as home:
            setup, _, _ = self.fixture(home, names=())
            skills, locks = {}, dict(setup.lock)
            for name, root, enabled, manual in [('ordinary', SkillRoot.AGENTS, True, False),
                                                 ('manual', SkillRoot.AGENTS, True, True),
                                                 ('hidden', SkillRoot.AGENTS, False, False),
                                                 ('codex-only', SkillRoot.CODEX, True, False)]:
                tree = home.roots.home / 'loose' / name
                home.write(tree / 'SKILL.md', f'---\nname: {name}\ndescription: Loose skill.\n---\nRead references/a.md.\n')
                home.write(tree / 'references/a.md', 'Supporting text.\n')
                if manual:
                    home.write(tree / 'agents/openai.yaml', 'policy:\n  allow_implicit_invocation: false\n')
                digest = home.roots.tree_digest(tree)
                visibility, source = Visibility.PUBLIC, LocalSource('loose/' + name, digest)
                if name == 'codex-only':
                    stored = setup.roots.private / 'snapshots' / digest.removeprefix('sha256:')
                    stored.parent.mkdir(parents=True)
                    tree.rename(stored)
                    visibility, source = Visibility.PRIVATE, SnapshotSource({root: digest})
                skill = LooseSkill(name, visibility, '', source, {root: enabled})
                skills[name] = skill
                locks[name] = Lock(intent(skill, {}), placements={root: digest})
            setup = replace(setup, loose=skills, lock=locks)
            builder = Builder(setup, home.roots, home.roots.engine)
            _, output, receipt = self.generated(home, setup, builder)
            self.assertEqual(sorted(path.name for path in (output / 'skills').iterdir()),
                             ['codex-only', 'manual', 'ordinary'])
            self.assertFalse((output / 'skills/hidden').exists())
            manual_meta = frontmatter((output / 'skills/manual/SKILL.md').read_text())[1]
            self.assertTrue(manual_meta['disable-model-invocation'])
            self.assertTrue((output / 'payload/loose/ordinary/references/a.md').is_file())
            self.assertTrue((output / 'payload/loose/codex-only/references/a.md').is_file())
            self.assertEqual({record['id'] for record in receipt['skills']}, {'codex-only', 'manual', 'ordinary'})

    def test_skills_json_adoption_conflict_preserves_existing_file_until_retained(self):
        with Home() as home:
            setup, builder, _ = self.fixture(home)
            path = home.roots.home / '.gemini/config/skills.json'
            before = b'{"entries":[{"path":"/reviewed/local-skills"}]}\n'
            path.parent.mkdir(parents=True)
            path.write_bytes(before)
            plan = antigravity.plan(setup, home.roots, Platform.LINUX)
            self.assertTrue(any(isinstance(item, BlockedConfiguration) for item in plan.configurations))
            with self.assertRaisesRegex(BuildError, 'adoption is blocked'):
                antigravity.materialise(plan, builder, home.roots.home / 'blocked')
            self.assertEqual(path.read_bytes(), before)
            home.write(setup.roots.private.parent / 'config/antigravity/setup.toml',
                       'enabled = true\nbaseline = "codex"\n[retain]\nentries = [{path = "/reviewed/local-skills"}]\n')
            retained = antigravity.plan(setup, home.roots, Platform.LINUX)
            self.assertFalse(any(isinstance(item, BlockedConfiguration) for item in retained.configurations))
            by_name = {item.name: item for item in retained.configurations}
            merged = json.loads(patch_fields(before, by_name['antigravity-skills'].fields, 'json'))
            self.assertEqual(merged['entries'][0], {'path': '/reviewed/local-skills'})
            self.assertEqual(merged['entries'][1], {'path': str(retained.destination / 'skills')})

    def test_cua_mcp_cli_settings_and_hooks_configuration(self):
        with Home() as home:
            setup, _, _ = self.fixture(home)
            private_ag = setup.roots.private.parent / 'config/antigravity'
            home.write(private_ag / 'setup.toml',
                       'enabled = true\nbaseline = "codex"\nmcp = ["cua-driver"]\nhooks = true\n'
                       '[cli_settings]\ntoolPermission = "always-proceed"\nallowNonWorkspaceAccess = true\n')
            home.write(setup.roots.private.parent / 'config/mcp-tool-filters/filters.json',
                       '{"cua_browser_tools":["browser_click","get_browser_state","page"]}\n')
            home.write(private_ag / 'hooks.json', json.dumps({
                'credential-guard': {
                    'PreToolUse': [{'matcher': '*', 'hooks': [{'type': 'command', 'command': '{python} -I "{adapter}" pre-tool-use'}]}]
                }
            }))
            home.write(private_ag / 'hook_adapter.py', 'print("{}")\n')
            for platform, kernel, extra in [(Platform.LINUX, 'microsoft-standard-WSL2', {'DISPLAY': ':99'}),
                                            (Platform.LINUX, 'arch-linux', {'CUA_DRIVER_RS_ENABLE_WAYLAND': '1'}),
                                            (Platform.WINDOWS, 'windows', {})]:
                with self.subTest(platform=platform, kernel=kernel), \
                        patch('antigravity.shutil.which', return_value='cua-driver'), \
                        patch('antigravity.operating_system.release', return_value=kernel):
                    plan = antigravity.plan(setup, home.roots, platform)
                    by_name = {item.name: item for item in plan.configurations}
                    mcp = by_name['antigravity-mcp']
                    self.assertEqual(mcp.destination, home.roots.home / '.gemini/config/mcp_config.json')
                    cua = mcp.fields[('mcpServers', 'cua-driver')]
                    self.assertEqual(cua['command'], 'cua-driver')
                    self.assertEqual(cua['args'], ['mcp'])
                    self.assertEqual(cua['env'], {
                        'CUA_DRIVER_PERMISSION_MODE': 'unrestricted',
                        'CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS': '1',
                        'CUA_TELEMETRY_ENABLED': 'false',
                        **extra,
                    })
                    self.assertEqual(cua['disabledTools'], ['browser_click', 'get_browser_state', 'page'])
                    cli = by_name['antigravity-cli-settings']
                    self.assertEqual(cli.destination, home.roots.home / '.gemini/antigravity-cli/settings.json')
                    self.assertEqual(cli.fields, {('toolPermission',): 'always-proceed',
                                                  ('allowNonWorkspaceAccess',): True})
                    adapter = by_name['antigravity-hook-adapter']
                    self.assertEqual(adapter.destination, home.roots.home / '.gemini/config/hook-adapter.py')
                    hooks = by_name['antigravity-hooks']
                    self.assertEqual(hooks.destination, home.roots.home / '.gemini/config/hooks.json')
                    cmd = hooks.fields[('credential-guard',)]['PreToolUse'][0]['hooks'][0]['command']
                    self.assertEqual(cmd, f'python3 -I "{adapter.destination.as_posix()}" pre-tool-use')
            with patch('antigravity.shutil.which', return_value=None):
                with self.assertRaisesRegex(ValueError, 'installed cua-driver'):
                    antigravity.plan(setup, home.roots, Platform.LINUX)

    def test_invalid_settings_stale_locks_and_native_collisions_are_rejected(self):
        with Home() as home:
            setup, builder, _ = self.fixture(home)
            with self.assertRaisesRegex(ValueError, 'current setup locks'):
                antigravity.plan(replace(setup, lock={}), home.roots, Platform.LINUX)
            path = setup.roots.private.parent / 'config/antigravity/setup.toml'
            for bad in ('enabled = true\nbaseline = "claude"\n',
                        'enabled = "yes"\nbaseline = "codex"\n',
                        'enabled = true\nbaseline = "codex"\nmcp = ["t3-code"]\n',
                        'enabled = true\nbaseline = "codex"\nhooks = "yes"\n',
                        'enabled = true\nbaseline = "codex"\nhooks = true\n'):
                home.write(path, bad)
                with self.assertRaises(ValueError):
                    antigravity.plan(setup, home.roots, Platform.LINUX)
            home.write(path, 'enabled = false\nbaseline = "codex"\n')
            self.assertIsNone(antigravity.plan(setup, home.roots, Platform.LINUX))
            home.write(path, 'enabled = true\nbaseline = "codex"\n')
            for sub in ('.gemini/config/skills/kit-auto/SKILL.md', '.gemini/antigravity-cli/skills/kit-auto.md'):
                with self.subTest(sub=sub), Home() as isolated:
                    iso_setup, iso_builder, _ = self.fixture(isolated)
                    isolated.write(isolated.roots.home / sub, 'Native skill.\n')
                    plan = antigravity.plan(iso_setup, isolated.roots, Platform.LINUX)
                    with self.assertRaisesRegex(BuildError, 'collision'):
                        antigravity.materialise(plan, iso_builder, isolated.roots.home / 'out')

    def test_kept_current_disabled_and_application_owned_packages_are_excluded(self):
        with Home() as home:
            setup, _, _ = self.fixture(home)
            package = setup.packages['kit']
            for excluded in (replace(package, codex=CodexPlacement((EngineInstall(False, keep_current=True),))),
                             replace(package, platforms=frozenset({Platform.WINDOWS}))):
                candidate = replace(setup, packages={'kit': excluded},
                                    lock={'kit': replace(setup.lock['kit'], intent=intent(excluded, {}))})
                plan = antigravity.plan(candidate, home.roots, Platform.LINUX)
                self.assertEqual(plan.packages, ())
            application = replace(package, source=None)
            candidate = replace(setup, packages={'kit': application}, lock={})
            plan = antigravity.plan(candidate, home.roots, Platform.LINUX)
            self.assertEqual(plan.packages, ())
            self.assertIn('application-owned', plan.unsupported[0])

    def test_payload_outside_links_and_existing_stage_are_rejected(self):
        with Home() as home:
            setup, builder, _ = self.fixture(home)
            plan = antigravity.plan(setup, home.roots, Platform.LINUX)
            existing = home.roots.home / 'existing'
            existing.mkdir()
            home.write(existing / 'keep', 'Keep.\n')
            with self.assertRaisesRegex(BuildError, 'already exists'):
                antigravity.materialise(plan, builder, existing)
            outside = home.write(home.roots.home / 'outside', 'Secret.\n')
            source = home.roots.home / 'linked'
            source.mkdir()
            try:
                (source / 'escape').symlink_to(outside)
            except OSError:
                self.skipTest('This Windows account cannot create symlinks')
            with self.assertRaisesRegex(BuildError, 'not a contained file'):
                antigravity._copy(source, home.roots.home / 'escaped')


if __name__ == '__main__':
    unittest.main()
