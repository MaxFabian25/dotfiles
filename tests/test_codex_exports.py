from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from homes import Home
from description import (CodexPlacement, EngineInstall, LocalSource, Lock, LockedTree, MarketplaceInstall,
                         Package, RepoSource, Roots, Setup, ToolNotes, Visibility)
from host_state import Host, SkillMode
import profile_policies
from sync_files import ConcurrentEdit, Files
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

    def test_native_marketplace_source_keeps_publisher_version(self):
        with Home() as home:
            _, original, package, builder = self.fixture(home)
            native = replace(package, codex=CodexPlacement((MarketplaceInstall('publisher', True),)))
            built = builder.codex(native, verify=False)
            self.assertEqual(built.version, '1.0.0')
            self.assertEqual((built.root / 'skills/publish/agents/openai.yaml').read_text(), original)


class ProfilePolicyTest(unittest.TestCase):
    def fixture(self, home):
        from types import SimpleNamespace

        source = home.roots.engine / 'native/kit' / ('a' * 40)
        home.skill(source, 'publish')
        home.write(source / '.codex-plugin/plugin.json', '{"name":"kit","version":"1.0.0"}\n')
        package = Package('kit', Visibility.PUBLIC, '', RepoSource('publisher'), {'publish': SkillMode.EXPLICIT},
                          ToolNotes.AUDIT, CodexPlacement((MarketplaceInstall('publisher', True),)), None)
        lock = Lock('test', {Host.CODEX: LockedTree('1.0.0', home.roots.tree_digest(source))})
        setup = Setup(Roots(home.roots.home / 'setup', None), {'kit': package},
                      {'publisher': SimpleNamespace(commit='a' * 40)}, {}, {}, None, (), frozenset(), {'kit': lock})
        profile = home.roots.home / '.local/share/orca/codex-accounts/example/home'
        config = home.write(profile / 'config.toml', '[plugins."kit@publisher"]\nenabled = true\n')
        target = profile / 'plugins/cache/publisher/kit/1.0.0'
        shutil.copytree(source, target)
        return setup, target, config

    def test_repair_is_narrow_backed_up_and_survives_cache_rehydration(self):
        with Home() as home:
            setup, target, config = self.fixture(home)
            original_config = config.read_bytes()
            home.write(target / 'unrelated.txt', 'keep my file')
            repairs, differences = profile_policies.inspect(setup, home.roots)
            self.assertEqual((len(repairs), len(differences)), (1, 1))
            files = Files(home.roots.home / 'backups')
            profile_policies.apply(repairs, files)
            self.assertEqual(files.changed, [target / 'skills/publish/agents/openai.yaml'])
            self.assertEqual(config.read_bytes(), original_config)
            self.assertEqual((target / 'unrelated.txt').read_text(), 'keep my file')
            self.assertTrue((files.backup / 'files.json').is_file())
            self.assertEqual(profile_policies.inspect(setup, home.roots), ((), ()))
            repairs[0].path.unlink()
            self.assertEqual(len(profile_policies.inspect(setup, home.roots)[0]), 1)

    def test_disabled_and_unknown_version_are_not_installed_or_adopted(self):
        with Home() as home:
            setup, target, config = self.fixture(home)
            config.write_text('[plugins."kit@publisher"]\nenabled = false\n')
            self.assertEqual(profile_policies.inspect(setup, home.roots), ((), ()))
            config.write_text('[plugins."kit@publisher"]\nenabled = true\n')
            target.rename(target.with_name('0.9.0'))
            self.assertEqual(profile_policies.inspect(setup, home.roots), ((), ()))

    def test_user_edits_and_concurrent_config_changes_are_preserved(self):
        with Home() as home:
            setup, target, config = self.fixture(home)
            repairs, _ = profile_policies.inspect(setup, home.roots)
            config.write_text('[plugins."kit@publisher"]\nenabled = false\n')
            with self.assertRaises(ConcurrentEdit):
                profile_policies.apply(repairs, Files(home.roots.home / 'backups'))
            config.write_text('[plugins."kit@publisher"]\nenabled = true\n')
            home.write(target / 'skills/publish/agents/openai.yaml', 'interface:\n  display_name: My edit\n')
            repairs, differences = profile_policies.inspect(setup, home.roots)
            self.assertFalse(repairs)
            self.assertEqual(differences[0].kind.value, 'local-edit')

    def test_source_or_skill_edits_block_repair(self):
        with Home() as home:
            setup, target, _ = self.fixture(home)
            (target / 'skills/publish/SKILL.md').write_text('local edit')
            repairs, differences = profile_policies.inspect(setup, home.roots)
            self.assertFalse(repairs)
            self.assertIn('local edits', differences[0].detail)


if __name__ == '__main__':
    unittest.main()
