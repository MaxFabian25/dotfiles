"""Regression checks for disabled native publication and portable invocation."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import claude_package as cp


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + '\n')


class ClaudePublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / 'home'
        self.catalog = self.root / 'catalog'
        self.settings = self.home / '.claude/settings.json'
        self.registry = self.home / '.claude/plugins/installed_plugins.json'
        self.market = self.catalog / '.claude-plugin/marketplace.json'
        write_json(self.settings, {'enabledPlugins': {'sample@personal-collections': False, 'other@market': True}})
        write_json(self.registry, {'version': 2, 'plugins': {'other@market': [{'version': 'unchanged'}]}})
        write_json(self.market, {'name': cp.MARKETPLACE, 'owner': {'name': 'Test'}, 'plugins': []})
        self.config = {'packagingRevision': 1, 'collections': {'sample': [
            {'name': 'sample', 'skills': ['workflow'], 'description': 'Test workflow'}]}}

    def build(self, version='1.0.0', body='Original instructions.\n'):
        source = self.root / ('source-' + version)
        skill = source / 'skills/workflow'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: workflow\ndescription: Test\nallowed-tools: Read\n---\n' + body)
        (skill / 'references').mkdir()
        (skill / 'references/reference.md').write_text('Complete course resource.\n')
        (skill / 'agents').mkdir()
        (skill / 'agents/openai.yaml').write_text('policy:\n  allow_implicit_invocation: false\n')
        write_json(source / '.codex-plugin/upstream.json', {'repository': 'https://github.com/example/skills',
                   'commit': version, 'version': version})
        return cp.build('sample', source, self.config, self.catalog)

    def publish(self, packages, bootstrap=False):
        return cp.publish(packages, self.root / 'backup', self.home, self.catalog, bootstrap)

    def test_preserves_body_resources_and_explicit_invocation(self):
        packages = self.build()
        release = Path(packages[0]['release'])
        self.assertTrue(cp.contract(release)['workflow']['disable-model-invocation'])
        self.assertEqual(cp.contract(release)['workflow']['allowed-tools'], 'Read')
        self.assertEqual((release / 'skills/workflow/SKILL.md').read_text().split('---', 2)[2], '\nOriginal instructions.\n')
        self.assertEqual((release / 'skills/workflow/references/reference.md').read_text(), 'Complete course resource.\n')

    def test_update_preserves_disabled_settings_and_rollback_preserves_unrelated_changes(self):
        first = self.build()
        self.publish(first, True)
        settings_before = self.settings.read_bytes()
        second = self.build('1.1.0', 'Updated upstream instructions.\n')
        transaction = self.publish(second)
        self.assertEqual(self.settings.read_bytes(), settings_before)
        cp.update_json(self.registry, lambda data: data['plugins'].update({'concurrent@market': [{'version': 'new'}]}))
        cp.restore_publication(transaction, self.home, self.catalog)
        installed = cp.read(self.registry)['plugins']
        self.assertEqual(installed['sample@personal-collections'][0]['version'], first[0]['version'])
        self.assertEqual(installed['concurrent@market'], [{'version': 'new'}])
        self.assertEqual(installed['other@market'], [{'version': 'unchanged'}])

    def test_edited_installation_blocks_update(self):
        first = self.build()
        self.publish(first, True)
        cache = Path(cp.read(self.registry)['plugins']['sample@personal-collections'][0]['installPath'])
        (cache / 'skills/workflow/references/reference.md').write_text('User edit.\n')
        with self.assertRaisesRegex(RuntimeError, 'Edited Claude cache'):
            self.publish(self.build('1.1.0'))

    def test_uninstalled_plugin_is_not_reinstalled(self):
        packages = self.build()
        transaction = self.publish(packages)
        self.assertEqual(transaction['publishedRecords'], {})
        self.assertNotIn('sample@personal-collections', cp.read(self.registry)['plugins'])

    def test_reviewed_addition_preserves_existing_contract_and_disabled_state(self):
        first = self.build()
        self.publish(first, True)
        second = self.build('1.1.0')
        release = Path(second[0]['release'])
        added = release / 'skills/new-workflow'
        added.mkdir()
        (added / 'SKILL.md').write_text('---\nname: new-workflow\ndescription: New workflow\n---\nNew instructions.\n')
        with self.assertRaisesRegex(RuntimeError, 'invocation or selection changed'):
            cp.current_record(second[0], self.home)
        second[0]['allowedAddedSkills'] = ['new-workflow']
        cp.current_record(second[0], self.home)
        existing = release / 'skills/workflow/SKILL.md'
        existing.write_text(existing.read_text().replace('allowed-tools: Read', 'allowed-tools: Bash'))
        with self.assertRaisesRegex(RuntimeError, 'invocation or selection changed'):
            cp.current_record(second[0], self.home)
        self.assertFalse(cp.read(self.settings)['enabledPlugins']['sample@personal-collections'])

    def test_follow_upstream_allows_only_the_model_invocation_flag_to_change(self):
        first = self.build()
        self.publish(first, True)
        second = self.build('1.1.0')
        release = Path(second[0]['release'])
        skill = release / 'skills/workflow/SKILL.md'
        skill.write_text(skill.read_text().replace('disable-model-invocation: true\n', ''))
        with self.assertRaisesRegex(RuntimeError, 'invocation or selection changed'):
            cp.current_record(second[0], self.home)
        second[0]['upstreamInvocationSkills'] = ['workflow']
        cp.current_record(second[0], self.home)
        skill.write_text(skill.read_text().replace('allowed-tools: Read', 'allowed-tools: Bash'))
        with self.assertRaisesRegex(RuntimeError, 'invocation or selection changed'):
            cp.current_record(second[0], self.home)

    def test_failed_registry_publication_restores_catalog(self):
        packages = self.build()
        original = cp.update_json
        failed = False
        def fail_once(path, change):
            nonlocal failed
            if path == self.registry and not failed:
                failed = True
                raise OSError('Simulated registry write failure')
            return original(path, change)
        with patch.object(cp, 'update_json', side_effect=fail_once):
            with self.assertRaisesRegex(OSError, 'Simulated registry'):
                self.publish(packages, True)
        self.assertEqual(cp.read(self.market)['plugins'], [])
        self.assertNotIn('sample@personal-collections', cp.read(self.registry)['plugins'])



if __name__ == '__main__':
    unittest.main()
