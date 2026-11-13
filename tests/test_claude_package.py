"""Regression checks for portable invocation in Claude releases built from Codex collections."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

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
        self.catalog = self.root / 'catalog'
        self.definitions = [{'name': 'sample', 'skills': ['workflow'], 'description': 'Test workflow'}]

    def build(self, version='1.0.0', body='Original instructions.\n',
              sidecar='policy:\n  allow_implicit_invocation: false\n', explicit=frozenset()):
        source = self.root / ('source-' + version)
        skill = source / 'skills/workflow'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: workflow\ndescription: Test\nallowed-tools: Read\n---\n' + body)
        (skill / 'references').mkdir()
        (skill / 'references/reference.md').write_text('Complete course resource.\n')
        (skill / 'agents').mkdir()
        (skill / 'agents/openai.yaml').write_text(sidecar)
        write_json(source / '.codex-plugin/upstream.json', {'repository': 'https://github.com/example/skills',
                   'commit': version, 'version': version})
        return cp.build('sample', source, self.definitions, 1, self.catalog, explicit)

    def test_preserves_body_resources_and_explicit_invocation(self):
        packages = self.build()
        release = Path(packages[0]['release'])
        self.assertTrue(cp.contract(release)['workflow']['disable-model-invocation'])
        self.assertEqual(cp.contract(release)['workflow']['allowed-tools'], 'Read')
        self.assertEqual((release / 'skills/workflow/SKILL.md').read_text().split('---', 2)[2], '\nOriginal instructions.\n')
        self.assertEqual((release / 'skills/workflow/references/reference.md').read_text(), 'Complete course resource.\n')

    def test_a_skill_the_setup_makes_explicit_is_explicit_in_the_release_and_its_version(self):
        implicit = 'interface:\n  display_name: Workflow\n'
        plain = Path(self.build('2.0.0', sidecar=implicit)[0]['release'])
        self.assertEqual(plain.name, '2.0.0.claude1')
        self.assertFalse(cp.contract(plain)['workflow']['disable-model-invocation'])
        self.assertNotIn('explicitInvocationPoliciesSet', cp.read(plain / '.claude-plugin/upstream.json'))

        chosen = self.build('2.0.1', sidecar=implicit, explicit=frozenset({'workflow'}))[0]
        release = Path(chosen['release'])
        self.assertRegex(chosen['version'], r'^2\.0\.1\.claude1\.explicit\.[0-9a-f]{12}$')
        self.assertEqual(release.name, chosen['version'])
        self.assertTrue(cp.contract(release)['workflow']['disable-model-invocation'])
        sidecar = (release / 'skills/workflow/agents/openai.yaml').read_text()
        self.assertEqual(cp.yaml.safe_load(sidecar), {'interface': {'display_name': 'Workflow'},
                                                       'policy': {'allow_implicit_invocation': False}})
        # The install overlay writes these same two changes, so it leaves the release Claude Code runs as it is.
        self.assertEqual(cp.transform_policy(sidecar, allow=False), sidecar)
        receipt = cp.read(release / '.claude-plugin/upstream.json')
        self.assertEqual((receipt['explicitInvocationHeadersAdded'], receipt['explicitInvocationPoliciesSet']),
                         (['workflow'], ['workflow']))
        self.assertEqual((release / 'skills/workflow/SKILL.md').read_text().split('---', 2)[2], '\nOriginal instructions.\n')

    def test_an_explicit_choice_upstream_already_makes_changes_neither_release_nor_version(self):
        packages = self.build(explicit=frozenset({'workflow'}))
        self.assertEqual(packages[0]['version'], '1.0.0.claude1')
        self.assertNotIn('explicitInvocationPoliciesSet', cp.read(Path(packages[0]['release']) / '.claude-plugin/upstream.json'))


if __name__ == '__main__':
    unittest.main()
