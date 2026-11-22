from dataclasses import replace
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from description import BuildSource, SnapshotRecipe
from package import files
import standalone_package as sp


class StandalonePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'packages'
        self.root.mkdir(parents=True)
        source = self.root / 'input/workflow'
        source.mkdir(parents=True)
        (source / 'SKILL.md').write_bytes(b'---\nname: workflow\ndescription: Sample\nallowed-tools: Read\n---\nOriginal source.\n')
        (source / 'agents').mkdir()
        (source / 'agents/openai.yaml').write_text('policy:\n  allow_implicit_invocation: false\n')
        self.snapshot = sp.snapshot(source.parent, {'commit': None, 'selectedSources': {}}, self.root / 'snapshots')
        self.recipe = SnapshotRecipe('sha256:' + self.snapshot.name, {'workflow': None})
        self.source = BuildSource(self.recipe, 1, 'Sample', metadata={
            'description': 'Selected sample skill.', 'sourceKind': 'github', 'repository': 'example/skills'})
        sp.build_snapshot('sample', self.recipe, self.source, self.snapshot, output_root=self.root)

    def test_same_content_with_new_provenance_has_a_distinct_snapshot(self):
        first = sp.snapshot(self.root / 'input', {'commit': 'old'}, self.root / 'snapshots')
        second = sp.snapshot(self.root / 'input', {'commit': 'new'}, self.root / 'snapshots')
        self.assertNotEqual(first, second)
        self.assertEqual(sp.read(second / 'source.json')['commit'], 'new')

    def test_a_snapshot_that_no_longer_holds_the_selected_skills_is_not_built(self):
        with self.assertRaisesRegex(RuntimeError, 'Selected skills changed'):
            sp.build_snapshot('sample', replace(self.recipe, origins={}), self.source, self.snapshot, output_root=self.root)

    def test_two_checkouts_build_the_same_bytes(self):
        """A release staged under ~/.agents is checked from whichever checkout runs sync, worktrees included."""
        releases = []
        for name in ('dotfiles', 'worktree'):
            root = Path(self.temp.name) / name
            snapshot = root / 'standalone-snapshots/sample' / self.snapshot.name
            shutil.copytree(self.snapshot, snapshot)
            release, receipt = sp.build_snapshot('sample', self.recipe, self.source, snapshot, output_root=root)
            self.assertEqual(receipt['sourceSnapshot'], 'standalone-snapshots/sample/' + self.snapshot.name)
            releases.append(files(release))
        self.assertEqual(releases[0], releases[1])

    def test_edited_source_snapshot_is_never_silently_rebuilt(self):
        (self.snapshot / 'skills/workflow/SKILL.md').write_text('User changed the workflow')
        with self.assertRaisesRegex(RuntimeError, 'differs from its receipt'):
            sp.build_snapshot('sample', self.recipe, self.source, self.snapshot, output_root=self.root)
        with self.assertRaisesRegex(RuntimeError, 'Edited source snapshot'):
            sp.snapshot(self.root / 'input', {'commit': None, 'selectedSources': {}}, self.root / 'snapshots')


if __name__ == '__main__':
    unittest.main()
