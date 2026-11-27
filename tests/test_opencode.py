from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from homes import Home
from configuration import BlockedConfiguration, patch_fields
from description import (CodexPlacement, EngineInstall, LocalSource, Lock, LockedTree, LooseSkill,
                         Package, Roots, Rules, Setup, SkillRoot, SnapshotSource, ToolNotes, Visibility, intent)
from host_state import Host, Platform, SkillMode
from invocation_overrides import frontmatter
import opencode
from sync_sources import Builder, BuildError


class OpenCodeTests(unittest.TestCase):
    def fixture(self, home, *, modes=None, names=('auto', 'manual', 'hidden', 'forced'), package_name='kit', extra=None):
        source = home.roots.home / 'source'
        for name in names:
            home.skill(source, name, explicit_sidecar=name == 'manual', frontmatter_explicit=name == 'forced')
        home.write(source / '.codex-plugin/plugin.json', '{"name":"%s","version":"1.0.0"}\n' % package_name)
        home.write(source / 'references/shared.md', 'Package-relative support.\n')
        for path, text in (extra or {}).items():
            home.write(source / path, text)
        package = Package(package_name, Visibility.PUBLIC, '', LocalSource('source', home.roots.tree_digest(source)),
                          modes or {}, ToolNotes.AUDIT, CodexPlacement((EngineInstall(True),)), None)
        setup = Setup(Roots(home.roots.home / 'setup', home.roots.home / 'private/setup'),
                      {package_name: package}, {}, {}, {}, Rules(b'Shared instructions.\n'), (), frozenset(), {})
        home.write(setup.roots.private.parent / 'config/opencode/setup.toml', 'enabled = true\nbaseline = "codex"\n')
        builder = Builder(setup, home.roots, home.roots.engine)
        built = builder.codex(package, verify=False)
        setup = replace(setup, lock={package_name: Lock(intent(package, {}),
                                                        {Host.CODEX: LockedTree(built.version, built.digest)})})
        return setup, Builder(setup, home.roots, home.roots.engine), source

    def generated(self, home, setup, builder, name='output'):
        plan = opencode.plan(setup, home.roots, Platform.LINUX)
        path = opencode.materialise(plan, builder, home.roots.home / name)
        return plan, path, json.loads((path / 'projection.json').read_bytes())

    def test_projection_preserves_payload_policy_and_unrelated_json(self):
        with Home() as home:
            setup, builder, source = self.fixture(home, modes={'hidden': SkillMode.OFF, 'forced': SkillMode.ON})
            before = home.roots.tree_digest(source)
            plan, output, receipt = self.generated(home, setup, builder)
            self.assertEqual(sorted(path.name for path in (output / 'skills').iterdir()),
                             ['kit-auto', 'kit-forced', 'kit-manual'])
            for name, implicit in [('auto', True), ('manual', False), ('forced', True)]:
                path = output / 'skills' / ('kit-' + name) / 'SKILL.md'
                _, metadata = frontmatter(path.read_text())
                self.assertEqual(metadata['disable-model-invocation'], not implicit)
                self.assertEqual(metadata['metadata']['opencode/autoinvoke'], implicit)
                record = next(record for record in receipt['skills'] if record['id'] == 'kit-' + name)
                self.assertTrue((output / record['source']).is_file())
                self.assertIn((plan.destination / record['source']).as_posix(), path.read_text())
            self.assertEqual((output / 'payload/kit/references/shared.md').read_bytes(), b'Package-relative support.\n')
            rules = next(item for item in plan.configurations if item.name == 'opencode-rules')
            self.assertEqual(rules.destination, plan.destination.parent / 'AGENTS.md')
            self.assertEqual(rules.data, b'Shared instructions.\n')
            self.assertNotIn(('instructions',), plan.configurations[0].fields)
            self.assertEqual(home.roots.tree_digest(source), before)
            fields = plan.configurations[0].fields
            self.assertEqual(fields[('update',)], 'disable')
            self.assertIn({'action': 'skill.use', 'resource': 'kit-hidden', 'effect': 'deny'}, fields[('policies',)])
            original = b'{"theme":"user-choice","accounts":{"synthetic":"preserve"}}\n'
            configured = patch_fields(original, fields, 'json')
            self.assertEqual(json.loads(configured)['accounts'], {'synthetic': 'preserve'})
            self.assertEqual(patch_fields(configured, fields, 'json'), configured)
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
            self.assertFalse(header['disable-model-invocation'])

    def test_selected_manifest_roots_keep_support_outside_the_scanned_root(self):
        with Home() as home:
            setup, _, source = self.fixture(home)
            home.write(source / '.codex-plugin/plugin.json', '{"name":"kit","version":"1.0.0","skills":["./catalog"]}\n')
            for name in ('first', 'second'):
                home.write(source / f'catalog/category/{name}/SKILL.md',
                           f'---\nname: {name}\ndescription: Selectable.\n---\nRead ../../../references/shared.md.\n')
            package = replace(setup.packages['kit'], source=LocalSource('source', home.roots.tree_digest(source)),
                              codex=CodexPlacement((EngineInstall(True),)))
            setup = replace(setup, packages={'kit': package}, lock={})
            builder = Builder(setup, home.roots, home.roots.engine)
            built = builder.codex(package, verify=False)
            setup = replace(setup, lock={'kit': Lock(intent(package, {}), {Host.CODEX: LockedTree(built.version, built.digest)})})
            _, output, receipt = self.generated(home, setup, Builder(setup, home.roots, home.roots.engine))
            self.assertEqual([record['id'] for record in receipt['skills']], ['kit-first', 'kit-second'])
            self.assertTrue((output / 'payload/kit/catalog/category/second/SKILL.md').is_file())
            self.assertTrue((output / 'payload/kit/references/shared.md').is_file())

    def test_kept_current_disabled_and_other_platform_packages_are_not_exposed(self):
        with Home() as home:
            setup, _, _ = self.fixture(home)
            package = setup.packages['kit']
            for excluded in (replace(package, codex=CodexPlacement((EngineInstall(False, keep_current=True),))),
                             replace(package, platforms=frozenset({Platform.WINDOWS}))):
                candidate = replace(setup, packages={'kit': excluded},
                                    lock={'kit': replace(setup.lock['kit'], intent=intent(excluded, {}))})
                plan = opencode.plan(candidate, home.roots, Platform.LINUX)
                self.assertEqual(plan.packages, ())
            application = replace(package, source=None)
            candidate = replace(setup, packages={'kit': application}, lock={})
            plan = opencode.plan(candidate, home.roots, Platform.LINUX)
            self.assertEqual(plan.packages, ())
            self.assertIn('application-owned', plan.unsupported[0])

    def test_initial_array_and_jsonc_conflicts_preserve_existing_files(self):
        with Home() as home:
            setup, builder, _ = self.fixture(home)
            path = home.roots.home / '.config/opencode/opencode.json'
            before = b'{"skills":["/reviewed/local-skills"],"theme":"keep"}\n'
            path.parent.mkdir(parents=True)
            path.write_bytes(before)
            plan = opencode.plan(setup, home.roots, Platform.LINUX)
            self.assertTrue(any(isinstance(item, BlockedConfiguration) for item in plan.configurations))
            with self.assertRaisesRegex(BuildError, 'adoption is blocked'):
                opencode.materialise(plan, builder, home.roots.home / 'blocked')
            self.assertFalse((home.roots.home / 'blocked').exists())
            self.assertEqual(path.read_bytes(), before)
            home.write(setup.roots.private.parent / 'config/opencode/setup.toml',
                       'enabled = true\nbaseline = "codex"\n[retain]\nskills = ["/reviewed/local-skills"]\n')
            plan = opencode.plan(setup, home.roots, Platform.LINUX)
            self.assertFalse(any(isinstance(item, BlockedConfiguration) for item in plan.configurations))
            retained = json.loads(patch_fields(before, opencode.plan(setup, home.roots, Platform.LINUX, adopted=True)
                                               .configurations[0].fields, 'json'))
            self.assertEqual(retained['skills'][0], '/reviewed/local-skills')
            jsonc = path.with_suffix('.jsonc')
            jsonc.write_bytes(b'{ // preserve comments\n}\n')
            plan = opencode.plan(setup, home.roots, Platform.LINUX, adopted=True)
            self.assertTrue(any(isinstance(item, BlockedConfiguration) for item in plan.configurations))
            self.assertEqual(jsonc.read_bytes(), b'{ // preserve comments\n}\n')

    def test_colliding_ids_fail_before_writing_the_projection(self):
        with Home() as home:
            setup, builder, _ = self.fixture(home)
            first = replace(setup.packages['kit'], codex=CodexPlacement((EngineInstall(True),)))
            collision = replace(first, name='KIT')
            setup = replace(setup, packages={'kit': first, 'KIT': collision},
                            lock={'kit': replace(setup.lock['kit'], intent=intent(first, {})),
                                  'KIT': replace(setup.lock['kit'], intent=intent(collision, {}))})
            builder = Builder(setup, home.roots, home.roots.engine)
            plan = opencode.plan(setup, home.roots, Platform.LINUX)
            with self.assertRaisesRegex(BuildError, 'collision'):
                opencode.materialise(plan, builder, home.roots.home / 'output')
            self.assertFalse((home.roots.home / 'output').exists())

    def test_loose_off_manual_sidecar_and_codex_only_projection_from_local_and_snapshot_sources(self):
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
            plan, output, receipt = self.generated(home, setup, builder)
            self.assertEqual(sorted(path.name for path in (output / 'skills').iterdir()), ['codex-only', 'manual'])
            manual = frontmatter((output / 'skills/manual/SKILL.md').read_text())[1]
            self.assertTrue(manual['disable-model-invocation'])
            self.assertFalse(manual['metadata']['opencode/autoinvoke'])
            self.assertIn('native', next(record for record in receipt['skills'] if record['id'] == 'ordinary'))
            self.assertIn({'action': 'skill.use', 'resource': 'hidden', 'effect': 'deny'},
                          plan.configurations[0].fields[('policies',)])
            self.assertFalse((output / 'payload/loose/ordinary').exists())

    def test_skills_only_claude_code_reads_and_the_desktop_browser_are_removed_by_policy(self):
        with Home() as home:
            setup, _, _ = self.fixture(home, modes={'hidden': SkillMode.OFF})
            claude = home.roots.claude_home / 'skills'
            for path in ('claude-only/SKILL.md', 'claude-only/nested/SKILL.md', 'synced/account/docx/SKILL.md',
                         'shared/SKILL.md', '.sync-account/dotted/SKILL.md',
                         'node_modules/vendored/SKILL.md', 'note.md'):
                home.write(claude / path, '---\nname: synthetic\ndescription: synthetic\n---\n')
            home.skill(home.roots.agents_home, 'shared')
            fields = opencode.plan(setup, home.roots, Platform.LINUX).configurations[0].fields
            denied = {rule['resource'] for rule in fields[('policies',)] if rule['action'] == 'skill.use'}
            self.assertEqual(denied, {'kit-hidden', 'claude-only', 'docx', 'note'})
            self.assertFalse(any(rule['action'] == 'skill' for rule in fields[('permissions',)]))
            self.assertEqual(fields[('plugins',)], ['-opencode.browser'])

    def test_cua_transport_and_browser_permissions_are_platform_native(self):
        with Home() as home:
            setup, _, _ = self.fixture(home)
            home.write(setup.roots.private.parent / 'config/opencode/setup.toml',
                       'enabled = true\nbaseline = "codex"\nmcp = ["cua-driver"]\n')
            home.write(setup.roots.private.parent / 'config/mcp-tool-filters/filters.json', json.dumps({
                'claude_deny_rules': ['mcp__cua-driver__browser_*', 'mcp__cua-driver__page', 'mcp__t3-code__device_*'],
                'cua_browser_tools': ['browser_click']}))
            for platform, kernel, extra in [(Platform.LINUX, 'microsoft-standard-WSL2', {'DISPLAY': ':99'}),
                                             (Platform.LINUX, 'arch-linux', {'CUA_DRIVER_RS_ENABLE_WAYLAND': '1'}),
                                             (Platform.WINDOWS, 'windows', {})]:
                with self.subTest(platform=platform, kernel=kernel), patch('opencode.shutil.which', return_value='cua-driver'), \
                        patch('opencode.operating_system.release', return_value=kernel):
                    fields = opencode.plan(setup, home.roots, platform).configurations[0].fields
                    transport = fields[('mcp', 'servers', 'cua-driver')]
                    self.assertEqual(transport['command'], ['cua-driver', 'mcp'])
                    self.assertEqual(transport['environment'], {'CUA_DRIVER_PERMISSION_MODE': 'unrestricted',
                                     'CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS': '1', 'CUA_TELEMETRY_ENABLED': 'false', **extra})
                    actions = {rule['action'] for rule in fields[('permissions',)] if rule['effect'] == 'deny'}
                    self.assertEqual(actions, {'cua-driver_browser_*', 'cua-driver_page'})
            # Once the pull has delivered cua-driver-defaults, Linux runs the driver through it.
            home.write(home.roots.home / '.local/bin/cua-driver-defaults', '#!/usr/bin/env python3\n')
            for platform, kernel, runs_wrapper in [(Platform.LINUX, 'microsoft-standard-WSL2', True),
                                                    (Platform.LINUX, 'arch-linux', True),
                                                    (Platform.WINDOWS, 'windows', False)]:
                with self.subTest(platform=platform, kernel=kernel, wrapper=True), \
                        patch('opencode.shutil.which', return_value='cua-driver'), \
                        patch('opencode.operating_system.release', return_value=kernel):
                    transport = opencode.plan(setup, home.roots, platform).configurations[0].fields[
                        ('mcp', 'servers', 'cua-driver')]
                    wrapper = str(home.roots.home / '.local/bin/cua-driver-defaults')
                    self.assertEqual(transport['command'], [wrapper if runs_wrapper else 'cua-driver', 'mcp'])
                    self.assertEqual(transport['environment'].get('CUA_DRIVER_BIN'),
                                     'cua-driver' if runs_wrapper else None)
            with patch('opencode.shutil.which', return_value=None):
                with self.assertRaisesRegex(ValueError, 'installed cua-driver'):
                    opencode.plan(setup, home.roots, Platform.LINUX)

    def test_invalid_settings_and_stale_locks_fail_without_an_output(self):
        with Home() as home:
            setup, _, _ = self.fixture(home)
            with self.assertRaisesRegex(ValueError, 'current setup locks'):
                opencode.plan(replace(setup, lock={}), home.roots, Platform.LINUX)
            path = setup.roots.private.parent / 'config/opencode/setup.toml'
            for settings in ('enabled = true\nbaseline = "claude"\n',
                             'enabled = "yes"\nbaseline = "codex"\n',
                             'enabled = true\nbaseline = "codex"\nmcp = ["t3-code"]\n',
                             'enabled = true\nbaseline = "codex"\nhooks = "yes"\n'):
                home.write(path, settings)
                with self.assertRaises(ValueError):
                    opencode.plan(setup, home.roots, Platform.LINUX)
            home.write(path, 'enabled = false\nbaseline = "codex"\n')
            self.assertIsNone(opencode.plan(setup, home.roots, Platform.LINUX))

    def test_native_unmanaged_names_cannot_be_shadowed_by_a_package(self):
        for location in ('agents', 'claude', 'opencode'):
            for name in ('kit-auto/SKILL.md', 'kit-auto.md'):
                with self.subTest(location=location, name=name), Home() as home:
                    setup, builder, _ = self.fixture(home)
                    root = {'agents': home.roots.agents_home, 'claude': home.roots.claude_home,
                            'opencode': home.roots.home / '.config/opencode'}[location]
                    home.write(root / 'skills' / name, 'Native body.\n')
                    plan = opencode.plan(setup, home.roots, Platform.LINUX)
                    with self.assertRaisesRegex(BuildError, 'ID collision'):
                        opencode.materialise(plan, builder, home.roots.home / 'output')
                    self.assertFalse((home.roots.home / 'output').exists())

    def test_a_missing_selection_malformed_metadata_and_existing_stage_are_rejected(self):
        with Home() as home:
            setup, builder, _ = self.fixture(home)
            setup = replace(setup, host_skills={Host.CODEX: {'kit:missing': SkillMode.ON}})
            plan = opencode.plan(setup, home.roots, Platform.LINUX)
            with self.assertRaisesRegex(BuildError, r"^kit: missing selected OpenCode skills: \['missing'\]$"):
                opencode.materialise(plan, builder, home.roots.home / 'missing')
            self.assertFalse((home.roots.home / 'missing').exists())
            metadata = home.write(home.roots.home / 'malformed/SKILL.md',
                                  '---\nname: malformed\ndescription: Valid description\nmetadata: []\n---\nBody.\n')
            with self.assertRaisesRegex(ValueError, 'metadata must be a mapping'):
                opencode._metadata(metadata, None)
            metadata.write_bytes(b'---\nname: malformed\ndescription: Valid\n---\nBody.\n')
            home.write(metadata.parent / 'agents/openai.yaml', 'policy:\n  allow_implicit_invocation: "false"\n')
            with self.assertRaisesRegex(ValueError, 'policy must be boolean'):
                opencode._metadata(metadata, None)
            existing = home.roots.home / 'existing'
            existing.mkdir()
            home.write(existing / 'keep', 'Keep this stage.\n')
            with self.assertRaisesRegex(BuildError, 'already exists'):
                opencode.materialise(plan, builder, existing)
            self.assertEqual((existing / 'keep').read_bytes(), b'Keep this stage.\n')

    def test_payload_outside_links_and_copy_failures_never_leave_a_success_receipt(self):
        with Home() as home:
            setup, builder, _ = self.fixture(home)
            plan = opencode.plan(setup, home.roots, Platform.LINUX)
            with patch('opencode.shutil.copytree', side_effect=OSError('synthetic copy refused')):
                with self.assertRaisesRegex(OSError, 'synthetic copy refused'):
                    opencode.materialise(plan, builder, home.roots.home / 'copy-failed')
            self.assertFalse((home.roots.home / 'copy-failed/projection.json').exists())
            outside = home.write(home.roots.home / 'outside', 'Never copy outside content.\n')
            source = home.roots.home / 'linked'
            source.mkdir()
            try:
                (source / 'escape').symlink_to(outside)
            except OSError:
                self.skipTest('This Windows account cannot create symlinks')
            with self.assertRaisesRegex(BuildError, 'not a contained file'):
                opencode._copy(source, home.roots.home / 'escaped')
            self.assertFalse((home.roots.home / 'escaped').exists())

    def test_hooks_run_antigravitys_pre_tool_hooks_through_its_adapter(self):
        with Home() as home:
            setup, _, _ = self.fixture(home)
            config = setup.roots.private.parent / 'config'
            home.write(config / 'antigravity/hooks.json', json.dumps({
                'credential-guard': {'PreToolUse': [{'matcher': '*', 'hooks': [
                    {'type': 'command', 'command': '{python} -I "{adapter}" credential-guard'}]}]},
                'git-force-push-ask': {'PreToolUse': [{'matcher': 'run_command', 'hooks': [
                    {'type': 'command', 'command': '{python} -I "{adapter}" git-force-push-ask'}]}]},
                'session-start-context': {'SessionStart': [
                    {'type': 'command', 'command': '{python} -I "{adapter}" session-start'}]}}))
            home.write(config / 'antigravity/hook_adapter.py', 'print("adapter")\n')
            home.write(config / 'opencode/guards.js.in',
                       'const ADAPTER = __DOTFILES_HOOK_ADAPTER__\nconst HOOKS = __DOTFILES_PRE_TOOL_HOOKS__\n')
            names = {item.name for item in opencode.plan(setup, home.roots, Platform.LINUX).configurations}
            self.assertNotIn('opencode-guards-plugin', names)
            home.write(config / 'opencode/setup.toml', 'enabled = true\nbaseline = "codex"\nhooks = true\n')
            by_name = {item.name: item for item in opencode.plan(setup, home.roots, Platform.LINUX).configurations}
            adapter = by_name['opencode-hook-adapter']
            self.assertEqual(adapter.destination, home.roots.home / '.config/opencode/hook-adapter.py')
            self.assertEqual(adapter.data, b'print("adapter")\n')
            plugin = by_name['opencode-guards-plugin']
            self.assertEqual(plugin.destination, home.roots.home / '.config/opencode/plugins/guards.js')
            lines = plugin.data.decode().splitlines()
            self.assertEqual(json.loads(lines[0].split(' = ', 1)[1]), [sys.executable, '-I', str(adapter.destination)])
            self.assertEqual(json.loads(lines[1].split(' = ', 1)[1]),
                             [{'mode': 'credential-guard', 'matcher': '*'},
                              {'mode': 'git-force-push-ask', 'matcher': 'run_command'}])
            home.write(config / 'antigravity/hooks.json', json.dumps({'other': {'PreToolUse': [
                {'matcher': '*', 'hooks': [{'type': 'command', 'command': 'python3 other.py'}]}]}}))
            with self.assertRaisesRegex(ValueError, 'cannot run this Antigravity PreToolUse hook'):
                opencode.plan(setup, home.roots, Platform.LINUX)

    def test_skill_mentions_plugin_is_installed_beside_the_other_plugins(self):
        with Home() as home:
            setup, _, _ = self.fixture(home)
            home.write(setup.roots.private.parent / 'config/opencode/skill-mentions.js', 'export default {}\n')
            by_name = {item.name: item for item in opencode.plan(setup, home.roots, Platform.LINUX).configurations}
            mentions = by_name['opencode-skill-mentions-plugin']
            self.assertEqual(mentions.destination, home.roots.home / '.config/opencode/plugins/skill-mentions.js')
            self.assertEqual(mentions.data, b'export default {}\n')

    def test_tool_filter_plugin_hides_the_tools_claude_code_denies_by_server(self):
        with Home() as home:
            setup, _, _ = self.fixture(home)
            config = setup.roots.private.parent / 'config'
            home.write(config / 'mcp-tool-filters/filters.json', json.dumps({
                'claude_deny_rules': ['mcp__t3-code__preview_*', 'mcp__t3-code__t3_preview_*', 'mcp__t3-code__device_*',
                                      'mcp__cua-driver__browser_*', 'mcp__cua-driver__page'],
                't3_browser_tools': ['preview_open'], 'cua_browser_tools': ['browser_click']}))
            names = {item.name for item in opencode.plan(setup, home.roots, Platform.LINUX).configurations}
            self.assertNotIn('opencode-tool-filter-plugin', names)
            home.write(config / 'opencode/tool-filter.js.in', 'const HIDDEN = __DOTFILES_HIDDEN_TOOLS__\n')
            by_name = {item.name: item for item in opencode.plan(setup, home.roots, Platform.LINUX).configurations}
            plugin = by_name['opencode-tool-filter-plugin']
            self.assertEqual(plugin.destination, home.roots.home / '.config/opencode/plugins/tool-filter.js')
            self.assertEqual(json.loads(plugin.data.decode().split(' = ', 1)[1]),
                             {'t3-code': ['device_*', 'preview_*', 't3_preview_*'], 'cua-driver': ['browser_*', 'page']})
            for rules in (None, ['mcp__cua-driver__page', 'Bash(rm:*)'], ['mcp__cua-driver__browser click']):
                with self.subTest(rules=rules):
                    home.write(config / 'mcp-tool-filters/filters.json', json.dumps({'claude_deny_rules': rules}))
                    with self.assertRaisesRegex(ValueError, 'claude_deny_rules'):
                        opencode.plan(setup, home.roots, Platform.LINUX)

    def test_pstack_seats_become_subagents_and_the_built_in_agents_run_on_opus(self):
        with Home() as home:
            agents = {'agents/poteto-agent.md': '---\nname: poteto-agent\ndescription: Delegate.\n---\n\n# Poteto\n\n'
                                                'Read [harnesses.md](../skills/poteto-mode/references/harnesses.md).\n',
                      'agents/comment-sicko.md': '---\nname: comment-sicko\ndescription: Hater.\n---\n# Sicko\n'}
            setup, builder, _ = self.fixture(home, package_name='pstack', names=('poteto-mode',), extra=agents)
            models = setup.roots.private.parent / 'config/pstack/models.md'
            home.write(models, '# routing\n## claude-code\nbug-fix: opus@high\n## codex\nbug-fix: gpt-6.2-sol@xhigh\n')
            plan = opencode.plan(setup, home.roots, Platform.LINUX)
            self.assertFalse(any(path[0] == 'agents' for path in plan.configurations[0].fields))
            home.write(models, models.read_text() + '## opencode\nbug-fix: opus@high\njudgment and prose: opus@xhigh\n'
                       'arena runners: opus@high, codex:gpt-6.2-sol@xhigh, antigravity:gemini-4-neon@high\n'
                       'recall readers: inherit-parent@low\n## antigravity\nbug-fix: gemini-4-neon@high\n')
            plan = opencode.plan(setup, home.roots, Platform.LINUX)
            fields = plan.configurations[0].fields
            agents_path = (plan.destination / 'agents').as_posix()
            self.assertEqual({path[1] for path in fields if path[0] == 'agents'},
                             {'pstack-opus-high', 'pstack-opus-xhigh', 'pstack-gpt-6-2-sol-xhigh', 'comment-sicko',
                              'general', 'explore'})
            high = fields[('agents', 'pstack-opus-high')]
            self.assertEqual((high['mode'], high['model'], high['system']),
                             ('subagent', 'anthropic-api/claude-opus-5-5#high', '{file:' + agents_path + '/poteto-agent.md}'))
            self.assertIn('`bug-fix`, `arena runners`', high['description'])
            self.assertEqual(fields[('agents', 'pstack-gpt-6-2-sol-xhigh')]['model'], 'openai-api/gpt-6.2-sol#xhigh')
            self.assertEqual(fields[('agents', 'pstack-opus-xhigh')]['model'], 'anthropic-api/claude-opus-5-5#xhigh')
            sicko = fields[('agents', 'comment-sicko')]
            self.assertEqual((sicko['model'], sicko['system']),
                             ('anthropic-api/claude-opus-5-5#high', '{file:' + agents_path + '/comment-sicko.md}'))
            self.assertEqual(fields[('agents', 'general', 'model')], 'anthropic-api/claude-opus-5-5#high')
            self.assertEqual(fields[('agents', 'explore', 'model')], 'anthropic-api/claude-opus-5-5#high')
            mine = json.loads(patch_fields(b'{"agents":{"general":{"color":"accent"}}}', fields, 'json'))['agents']
            self.assertEqual(mine['general'], {'color': 'accent', 'model': 'anthropic-api/claude-opus-5-5#high'})
            output = opencode.materialise(plan, builder, home.roots.home / 'output')
            harnesses = (plan.destination / 'payload/pstack/skills/poteto-mode/references/harnesses.md').as_posix()
            self.assertEqual((output / 'agents/poteto-agent.md').read_text(),
                             f'# Poteto\n\nRead [harnesses.md]({harnesses}).\n')
            self.assertEqual((output / 'agents/comment-sicko.md').read_text(), '# Sicko\n')

    def test_pstack_seats_need_pstack_exported_and_its_agent_definitions(self):
        with Home() as home:
            setup, builder, _ = self.fixture(home)
            home.write(setup.roots.private.parent / 'config/pstack/models.md',
                       '## claude-code\nx: opus@high\n## codex\nx: gpt-6.2-sol@xhigh\n## opencode\nx: opus@high\n')
            with self.assertRaisesRegex(ValueError, 'pstack package exported'):
                opencode.plan(setup, home.roots, Platform.LINUX)
        with Home() as home:
            setup, builder, _ = self.fixture(home, package_name='pstack', names=('poteto-mode',),
                                             extra={'agents/poteto-agent.md': '---\nname: poteto-agent\n---\nBody.\n'})
            home.write(setup.roots.private.parent / 'config/pstack/models.md',
                       '## claude-code\nx: opus@high\n## codex\nx: gpt-6.2-sol@xhigh\n## opencode\nx: opus@high\n')
            plan = opencode.plan(setup, home.roots, Platform.LINUX)
            with self.assertRaisesRegex(BuildError, 'agents/comment-sicko.md'):
                opencode.materialise(plan, builder, home.roots.home / 'output')
            self.assertFalse((home.roots.home / 'output').exists())

    def test_runtime_support_is_receipted_on_windows_without_static_credentials_or_provider(self):
        with Home() as home:
            setup, _, _ = self.fixture(home)
            directory = setup.roots.private.parent / 'config/opencode'
            home.write(directory / 'runtime.toml', 'vault_reference="op://Agents/Fixture/credential"\n'
                       'model="anthropic-api/claude-opus-5-5#xhigh"\n')
            home.write(directory / 'runtime.py', 'print("synthetic runtime")\n')
            home.write(directory / 'credentials.js.in',
                       'const helper = __DOTFILES_CREDENTIAL_HELPER__; const model = __DOTFILES_MODEL_ID__;\n')
            home.write(directory / 'opencode-anthropic.cmd', '@echo synthetic wrapper\n')
            plan = opencode.plan(setup, home.roots, Platform.WINDOWS)
            by_name = {configuration.name: configuration for configuration in plan.configurations}
            self.assertEqual(by_name['opencode-launcher'].destination, home.roots.home / '.local/bin/opencode-anthropic.cmd')
            self.assertNotIn(('providers', 'anthropic-api'), by_name['opencode'].fields)
            fields = by_name['opencode'].fields
            self.assertEqual(fields[('model',)], 'anthropic-api/claude-opus-5-5#xhigh')
            self.assertEqual(fields[('policies',)],
                             [{'action': 'provider.use', 'resource': '*', 'effect': 'deny'},
                              {'action': 'provider.use', 'resource': 'anthropic-api', 'effect': 'allow'},
                              {'action': 'provider.use', 'resource': 'openai-api', 'effect': 'allow'}])
            plugin = by_name['opencode-credentials-plugin'].data.decode()
            self.assertNotIn('__DOTFILES_', plugin)
            self.assertIn('"claude-opus-5-5"', plugin)
            self.assertIn('"credential"', plugin)
            self.assertNotIn('sk-ant-', plugin)
            self.assertEqual(by_name['opencode-runtime'].data, b'print("synthetic runtime")\n')
            self.assertNotIn('opencode-launcher', {item.name for item in opencode.plan(setup, home.roots, Platform.LINUX).configurations})
            path = home.roots.home / '.config/opencode/opencode.json'
            original = b'{"policies":[{"action":"provider.use","resource":"opencode","effect":"allow"}],"theme":"keep"}\n'
            home.write(path, original.decode())
            self.assertTrue(any(isinstance(item, BlockedConfiguration)
                                for item in opencode.plan(setup, home.roots, Platform.LINUX).configurations))
            self.assertEqual(path.read_bytes(), original)
            home.write(directory / 'setup.toml', 'enabled = true\nbaseline = "codex"\n[retain]\n'
                       'policies = [{action="provider.use",resource="opencode",effect="allow"}]\n')
            retained = opencode.plan(setup, home.roots, Platform.LINUX)
            self.assertFalse(any(isinstance(item, BlockedConfiguration) for item in retained.configurations))
            merged = json.loads(patch_fields(original, retained.configurations[0].fields, 'json'))
            self.assertEqual(merged['theme'], 'keep')
            self.assertEqual(merged['policies'], [{'action': 'provider.use', 'resource': 'opencode', 'effect': 'allow'},
                                                *fields[('policies',)]])
            for invalid in ('policies = "not-an-array"', 'policies = ["not-an-object"]'):
                home.write(directory / 'setup.toml', 'enabled = true\nbaseline = "codex"\n[retain]\n' + invalid + '\n')
                with self.assertRaisesRegex(ValueError, 'retain'):
                    opencode.plan(setup, home.roots, Platform.LINUX)


if __name__ == '__main__':
    unittest.main()
