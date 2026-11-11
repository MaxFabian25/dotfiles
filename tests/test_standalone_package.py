from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import standalone_package as sp


class StandalonePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'packages'
        self.root.mkdir(parents=True)
        patcher = patch.object(sp, 'ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.definition = {'displayName': 'Sample', 'description': 'Selected sample skill.', 'sourceKind': 'github',
                           'repository': 'example/skills', 'packagingRevision': 1,
                           'skills': {'workflow': {'sourcePath': 'skills/workflow'}}}
        source = self.root / 'input/workflow'
        source.mkdir(parents=True)
        (source / 'SKILL.md').write_bytes(b'---\nname: workflow\ndescription: Sample\nallowed-tools: Read\n---\nOriginal source.\n')
        (source / 'agents').mkdir()
        (source / 'agents/openai.yaml').write_text('policy:\n  allow_implicit_invocation: false\n')
        self.snapshot = sp.snapshot(source.parent, {'commit': None, 'selectedSources': {}}, self.root / 'snapshots')
        self.release, _ = sp.build_snapshot('sample', self.definition, self.snapshot)

    def test_same_content_with_new_provenance_has_a_distinct_snapshot(self):
        first = sp.snapshot(self.root / 'input', {'commit': 'old'}, self.root / 'snapshots')
        second = sp.snapshot(self.root / 'input', {'commit': 'new'}, self.root / 'snapshots')
        self.assertNotEqual(first, second)
        self.assertEqual(sp.read(second / 'source.json')['commit'], 'new')

    def test_a_snapshot_that_no_longer_holds_the_selected_skills_is_not_built(self):
        with self.assertRaisesRegex(RuntimeError, 'Selected skills changed'):
            sp.build_snapshot('sample', {**self.definition, 'skills': {}}, self.snapshot)

    def test_edited_source_snapshot_is_never_silently_rebuilt(self):
        (self.snapshot / 'skills/workflow/SKILL.md').write_text('User changed the workflow')
        with self.assertRaisesRegex(RuntimeError, 'differs from its receipt'):
            sp.build_snapshot('sample', self.definition, self.snapshot)
        with self.assertRaisesRegex(RuntimeError, 'Edited source snapshot'):
            sp.snapshot(self.root / 'input', {'commit': None, 'selectedSources': {}}, self.root / 'snapshots')


if __name__ == '__main__':
    unittest.main()
