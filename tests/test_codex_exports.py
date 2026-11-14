from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homes import Home, hold_built_trees_once
from description import (AlignedInstall, ClaudePlacement, CodexPlacement, EngineInstall, LocalSource, MarketplaceInstall,
                         Package, Roots, Setup, TextReplace, ToolNotes, Visibility, WindowsReplace, plugin_id)
from host_state import Host, SkillMode
from sync_sources import Builder, BuildError, Built, forbidden_text


class PolicyExportTest(unittest.TestCase):
    def fixture(self, home):
        source = home.roots.home / 'source'
        home.skill(source, 'publish')
        home.skill(source, 'ordinary')
        home.write(source / '.codex-plugin/plugin.json', '{"name":"kit","version":"1.0.0"}\n')
        original = 'interface:\n  display_name: Publish\npolicy:\n  allow_implicit_invocation: true\n'
        home.write(source / 'skills/publish/agents/openai.yaml', original)
        package = Package('kit', Visibility.PUBLIC, '', LocalSource('source', home.roots.tree_digest(source)),
                          {'publish': SkillMode.EXPLICIT}, ToolNotes.AUDIT,
                          CodexPlacement((EngineInstall(True),)), None)
        setup = Setup(Roots(home.roots.home / 'setup', None), {'kit': package}, {}, {}, {}, None, (), frozenset(), {})
        return source, original, package, Builder(setup, home.roots, home.roots.engine)

    def test_fresh_catalog_copy_contains_policy_and_preserves_pristine_source(self):
        with Home() as home:
            source, original, package, builder = self.fixture(home)
            held = hold_built_trees_once(self)
            built = builder.codex(package, verify=False)
            self.assertEqual(held, [built.root])
            self.assertIn('+explicit.', built.version)
            self.assertEqual(json.loads((built.root / '.codex-plugin/plugin.json').read_bytes())['version'], built.version)
            fresh = home.roots.home / 'fresh-profile/cache'
            shutil.copytree(built.root, fresh)
            self.assertIn('allow_implicit_invocation: false', (fresh / 'skills/publish/agents/openai.yaml').read_text())
            self.assertEqual((fresh / 'skills/publish/SKILL.md').read_bytes(), (source / 'skills/publish/SKILL.md').read_bytes())
            effective = builder.effective(package, built)
            self.assertIn('disable-model-invocation: true', (effective / 'skills/publish/SKILL.md').read_text())
            self.assertFalse((fresh / 'skills/ordinary/agents/openai.yaml').exists())
            self.assertEqual((source / 'skills/publish/agents/openai.yaml').read_text(), original)
            self.assertEqual(builder.codex(package, verify=False), built)
            changed = replace(package, modes={})
            plain = builder.codex(changed, verify=False)
            self.assertEqual(plain.version, '1.0.0')
            self.assertEqual((plain.root / 'skills/publish/agents/openai.yaml').read_text(), original)

    def test_edited_export_is_rejected_and_policy_changes_get_new_versions(self):
        with Home() as home:
            _, _, package, builder = self.fixture(home)
            built = builder.codex(package, verify=False)
            extra = builder.codex(replace(package, modes={**package.modes, 'ordinary': SkillMode.EXPLICIT}), verify=False)
            self.assertNotEqual(built.version, extra.version)
            (built.root / 'skills/publish/SKILL.md').write_text('local edit')
            with self.assertRaisesRegex(BuildError, 'immutable policy export was edited'):
                builder.codex(package, verify=False)

    def test_all_native_manifest_variants_use_the_export_version(self):
        with Home() as home:
            source, _, package, builder = self.fixture(home)
            manifests = ('plugin.json', '.codex-plugin/plugin.json', '.claude-plugin/plugin.json', '.cursor-plugin/plugin.json')
            original = b'{"name":"kit","version":"1.0.0"}\n'
            for name in manifests:
                home.write(source / name, original.decode())
            package = replace(package, source=LocalSource('source', home.roots.tree_digest(source)))
            built = builder.codex(package, verify=False)
            for name in manifests:
                self.assertEqual(json.loads((built.root / name).read_bytes())['version'], built.version)
                self.assertEqual((source / name).read_bytes(), original)

    def test_a_kept_current_install_builds_what_an_enabled_one_builds(self):
        with Home() as home:
            _, _, package, builder = self.fixture(home)
            package = replace(package, claude=ClaudePlacement((AlignedInstall(True),)))
            enabled = builder.codex(package, verify=False)
            aligned = builder.claude(package, enabled, verify=False)['kit@codex-aligned']
            self.assertIn('disable-model-invocation: true', (aligned.root / 'skills/publish/SKILL.md').read_text())
            enabled_aligned = aligned.digest
            kept = replace(package, codex=CodexPlacement((EngineInstall(False, keep_current=True),)),
                           claude=ClaudePlacement((AlignedInstall(False, keep_current=True),)))
            built = builder.codex(kept, verify=False)
            self.assertEqual((built.version, built.digest), (enabled.version, enabled.digest))
            self.assertIn('+explicit.', built.version)
            claude = builder.claude(kept, built, verify=False)
            self.assertEqual(list(claude), ['kit@codex-aligned'])
            self.assertEqual(claude['kit@codex-aligned'].digest, enabled_aligned, 'the aligned copy carries the Codex overlay')
            dormant = replace(kept, codex=CodexPlacement((EngineInstall(False),)), claude=ClaudePlacement((AlignedInstall(False),)))
            self.assertEqual(builder.codex(dormant, verify=False).version, '1.0.0')
            self.assertEqual(builder.claude(dormant, built, verify=False), {})

    def test_a_text_replace_reaches_the_codex_install_and_the_aligned_copy_on_every_platform(self):
        with Home() as home:
            source, _, package, builder = self.fixture(home)
            home.skill(source, 'ordinary', body='Hand it over.\nThen stop.\n')
            edit = TextReplace('skills/ordinary/SKILL.md', 'Hand it over.', 'Read its files.')
            package = replace(package, source=LocalSource('source', home.roots.tree_digest(source)),
                              codex=CodexPlacement((EngineInstall(True),), replace=(edit,)),
                              claude=ClaudePlacement((AlignedInstall(True),)))
            built = builder.codex(package, verify=False)
            self.assertIn('Hand it over.', (built.root / 'skills/ordinary/SKILL.md').read_text(), 'the release stays pristine')
            patched = '---\nname: ordinary\ndescription: ordinary\n---\nRead its files.\nThen stop.\n'
            install = home.roots.home / 'install'
            shutil.copytree(built.root, install)
            with patch('sync_sources.WINDOWS', False):
                writes = builder.overlay(package, Host.CODEX, 'kit@personal', install)
                self.assertEqual([new for path, _, new, *_ in writes if path.name == 'SKILL.md' and path.parent.name == 'ordinary'],
                                 [patched])
                aligned = builder.claude(package, built, verify=False)['kit@codex-aligned'].root
            self.assertEqual((aligned / 'skills/ordinary/SKILL.md').read_text(), patched)
            (install / 'skills/ordinary/SKILL.md').write_text(patched)
            again = builder.overlay(package, Host.CODEX, 'kit@personal', install)
            self.assertEqual([old for path, old, new, *_ in again if path.parent.name == 'ordinary' and old != new], [])
            (install / 'skills/ordinary/SKILL.md').write_text('---\nname: ordinary\ndescription: ordinary\n---\nUpstream rewrote it.\n')
            with self.assertRaisesRegex(ValueError, 'Review upstream change to an adapted file: kit@personal:skills/ordinary/SKILL.md'):
                builder.overlay(package, Host.CODEX, 'kit@personal', install)

    def test_a_text_replace_whose_passage_or_file_is_gone_stops_the_build_without_a_claude_copy(self):
        with Home() as home:
            source, _, package, builder = self.fixture(home)
            home.skill(source, 'ordinary', body='Upstream rewrote it.\n')
            package = replace(package, source=LocalSource('source', home.roots.tree_digest(source)))
            for path in ('skills/ordinary/SKILL.md', 'skills/ordinary/gone.md'):
                with self.subTest(path=path):
                    edit = TextReplace(path, 'Hand it over.', 'Read its files.')
                    codex_only = replace(package, codex=CodexPlacement((EngineInstall(True),), replace=(edit,)), claude=None)
                    with self.assertRaisesRegex(ValueError, f'Review upstream change to an adapted file: kit@personal:{path}'):
                        builder.codex(codex_only, verify=False)

    def test_forbidden_text_stops_the_build_only_where_the_edits_leave_it_in_a_skill(self):
        with Home() as home:
            source, _, package, builder = self.fixture(home)
            home.skill(source, 'ordinary', body='Spawn old-agent.\nThen stop.\n')
            home.write(source / 'agents/old-agent.md', 'The agent old-agent lives outside skills/.\n')
            (source / 'skills/ordinary/icon.png').write_bytes(b'\xff\xd8\x00image\xff')
            edit = TextReplace('skills/ordinary/SKILL.md', 'Spawn old-agent.', 'Read the agent file.')
            package = replace(package, source=LocalSource('source', home.roots.tree_digest(source)),
                              forbid=('never-written', 'old-agent'))
            edited = replace(package, codex=CodexPlacement((EngineInstall(True),), replace=(edit,)))
            builder.check_overlays(edited, builder.codex(edited, verify=False), {})
            with self.assertRaisesRegex(BuildError, r"kit@personal: skills/ordinary/SKILL\.md:5 still holds 'old-agent'"):
                builder.check_overlays(package, builder.codex(package, verify=False), {})

    def test_forbidden_text_reads_the_files_an_overlay_creates_and_skips_the_ones_it_deletes(self):
        with Home() as home:
            source, _, package, _ = self.fixture(home)
            package = replace(package, forbid=('old-agent',))
            on_disk = home.write(source / 'skills/ordinary/gone.md', 'Spawn old-agent.\n')
            forbidden_text(package, 'kit@personal', source, [(on_disk, 'Spawn old-agent.\n', None, Host.CODEX, 'kit@personal')])
            created = source / 'skills/ordinary/new.md'
            with self.assertRaisesRegex(BuildError, r'skills/ordinary/new\.md:2 still holds'):
                forbidden_text(package, 'kit@personal', source, [(created, None, 'First.\nold-agent\n', Host.CODEX, 'kit@personal'),
                                                                 (on_disk, 'Spawn old-agent.\n', None, Host.CODEX, 'kit@personal')])

    def test_forbidden_text_reads_every_skill_folder_and_stops_where_it_cannot_read(self):
        with Home() as home:
            _, _, package, _ = self.fixture(home)
            package = replace(package, forbid=('old-agent',))
            declared = home.roots.home / 'declared'
            home.write(declared / '.codex-plugin/plugin.json', '{"name":"kit","skills":"./skill-src/"}\n')
            home.write(declared / 'skill-src/hidden/SKILL.md', '---\nname: hidden\ndescription: hidden\n---\nSpawn old-agent.\n')
            with self.assertRaisesRegex(BuildError, r'kit@personal: skill-src/hidden/SKILL\.md:5 still holds'):
                forbidden_text(package, 'kit@personal', declared, [])
            latin = home.roots.home / 'latin'
            home.skill(latin, 'plain')
            (latin / 'skills/plain/notes.md').write_bytes(b'Caf\xe9: spawn old-agent.\n')
            with self.assertRaisesRegex(BuildError, r'skills/plain/notes\.md:1 still holds'):
                forbidden_text(package, 'kit@personal', latin, [])
            empty = home.roots.home / 'empty'
            home.write(empty / 'README.md', 'old-agent\n')
            with self.assertRaisesRegex(BuildError, 'kit@personal: no skill file to check'):
                forbidden_text(package, 'kit@personal', empty, [])
            linked = home.roots.home / 'linked'
            home.skill(linked, 'plain')
            home.write(home.roots.home / 'outside/SKILL.md', '---\nname: elsewhere\ndescription: elsewhere\n---\nSpawn old-agent.\n')
            try:
                (linked / 'skills/elsewhere').symlink_to(home.roots.home / 'outside', target_is_directory=True)
            except OSError:
                self.skipTest('This Windows account cannot create symlinks')
            with self.assertRaisesRegex(BuildError, 'kit@personal: skills/elsewhere is a symlinked folder'):
                forbidden_text(package, 'kit@personal', linked, [])

    def test_forbidden_text_is_checked_on_each_host_and_on_each_platform_overlay(self):
        with Home() as home:
            source, _, package, builder = self.fixture(home)
            package = replace(package, forbid=('old-agent',))
            for body, edit in (('Then stop.\n', WindowsReplace('skills/ordinary/SKILL.md', 'Then stop.', 'Spawn old-agent.')),
                               ('Spawn old-agent.\n', WindowsReplace('skills/ordinary/SKILL.md', 'Spawn old-agent.', 'Then stop.'))):
                with self.subTest(body=body):
                    home.skill(source, 'ordinary', body=body)
                    windows = replace(package, source=LocalSource('source', home.roots.tree_digest(source)),
                                      codex=CodexPlacement((EngineInstall(True),), windows_replace=(edit,)))
                    with self.assertRaisesRegex(BuildError, r'kit@personal: skills/ordinary/SKILL\.md:5 still holds'):
                        builder.check_overlays(windows, builder.codex(windows, verify=False), {})
            home.skill(source, 'ordinary')
            install = MarketplaceInstall('publisher', True)
            native = replace(package, source=LocalSource('source', home.roots.tree_digest(source)),
                             claude=ClaudePlacement((install,)))
            codex = builder.codex(native, verify=False)
            claude_root = home.roots.home / 'claude-tree'
            shutil.copytree(source, claude_root)
            home.skill(claude_root, 'ordinary', body='Spawn old-agent.\n')
            identity = plugin_id('kit', install, Host.CLAUDE)
            claude = {identity: Built('kit', claude_root, '1.0.0', 'sha256:' + '0' * 64)}
            with self.assertRaisesRegex(BuildError, f'{identity}: skills/ordinary/SKILL\\.md:5 still holds'):
                builder.check_overlays(native, codex, claude)
            edit = TextReplace('skills/ordinary/SKILL.md', 'Spawn old-agent.', 'Use it.')
            builder.check_overlays(replace(native, claude=ClaudePlacement((install,), replace=(edit,))), codex, claude)

    def test_the_aligned_copy_keeps_claudes_own_skill_and_only_the_agents_its_install_lists(self):
        with Home() as home:
            source, _, package, builder = self.fixture(home)
            for name in ('reviewer', 'watcher'):
                home.write(source / f'agents/{name}.md', f'---\nname: {name}\ndescription: Test agent.\n---\nBody\n')
            package = replace(package, source=LocalSource('source', home.roots.tree_digest(source)), modes={'publish': SkillMode.OFF},
                              claude=ClaudePlacement((AlignedInstall(True, frozenset({'reviewer'})),), {'publish': SkillMode.EXPLICIT}))
            aligned = builder.claude(package, builder.codex(package, verify=False), verify=False)['kit@codex-aligned'].root
            self.assertEqual(sorted(path.name for path in (aligned / 'skills').iterdir()), ['ordinary', 'publish'])
            self.assertEqual(sorted(path.name for path in (aligned / 'agents').iterdir()), ['reviewer.md'])

    def test_native_marketplace_source_keeps_publisher_version(self):
        with Home() as home:
            _, original, package, builder = self.fixture(home)
            native = replace(package, codex=CodexPlacement((MarketplaceInstall('publisher', True),)))
            built = builder.codex(native, verify=False)
            self.assertEqual(built.version, '1.0.0')
            self.assertEqual((built.root / 'skills/publish/agents/openai.yaml').read_text(), original)

    def test_implicit_export_overrides_both_flags_without_changing_source_or_other_skills(self):
        with Home() as home:
            source, _, package, builder = self.fixture(home)
            skill = source / 'skills/publish/SKILL.md'
            skill.write_text('---\nname: publish\ndescription: Publish\ndisable-model-invocation: true\n---\nKeep body.\n')
            sidecar = source / 'skills/publish/agents/openai.yaml'
            sidecar.write_text('interface:\n  display_name: Publish\npolicy:\n  allow_implicit_invocation: false\n')
            before = home.roots.tree_digest(source)
            package = replace(package, source=LocalSource('source', before), modes={'publish': SkillMode.ON})
            built = builder.codex(package, verify=False)
            self.assertIn('disable-model-invocation: false', (built.root / 'skills/publish/SKILL.md').read_text())
            self.assertIn('allow_implicit_invocation: true', (built.root / 'skills/publish/agents/openai.yaml').read_text())
            self.assertIn('display_name: Publish', (built.root / 'skills/publish/agents/openai.yaml').read_text())
            self.assertEqual(home.roots.tree_digest(source), before)
            self.assertEqual((built.root / 'skills/ordinary/SKILL.md').read_bytes(),
                             (source / 'skills/ordinary/SKILL.md').read_bytes())
            self.assertEqual(builder.codex(package, verify=False), built)
            native = replace(package, codex=CodexPlacement((MarketplaceInstall('publisher', True),)))
            pristine = builder.codex(native, verify=False)
            self.assertEqual(pristine.version, '1.0.0')
            effective = builder.effective(native, pristine)
            self.assertIn('disable-model-invocation: false', (effective / 'skills/publish/SKILL.md').read_text())
            self.assertIn('allow_implicit_invocation: true', (effective / 'skills/publish/agents/openai.yaml').read_text())

    def test_a_skill_upstream_already_ships_explicit_only_keeps_its_upstream_text(self):
        with Home() as home:
            source, _, package, builder = self.fixture(home)
            sidecar = source / 'skills/publish/agents/openai.yaml'
            sidecar.write_text('interface:\n  display_name: Publish\npolicy:\n  allow_implicit_invocation: false\n')
            native = replace(package, source=LocalSource('source', home.roots.tree_digest(source)),
                             codex=CodexPlacement((MarketplaceInstall('publisher', True),)))
            effective = builder.effective(native, builder.codex(native, verify=False))
            self.assertEqual((effective / 'skills/publish/SKILL.md').read_bytes(), (source / 'skills/publish/SKILL.md').read_bytes())
            self.assertEqual((effective / 'skills/publish/agents/openai.yaml').read_bytes(), sidecar.read_bytes())


if __name__ == '__main__':
    unittest.main()
