from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homes import Home
from description import (CodexPlacement, EngineInstall, LocalSource, MarketplaceInstall, Package, Roots, Setup,
                         ToolNotes, Visibility)
from host_state import Host, SkillMode
from sync_sources import Builder, BuildError


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
            built = builder.codex(package, verify=False)
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


if __name__ == '__main__':
    unittest.main()
