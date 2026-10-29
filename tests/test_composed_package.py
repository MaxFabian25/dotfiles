import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from homes import Home
from composed_package import compose
from description import CodexPlacement, EngineInstall, Package, RepoSource, ToolNotes, Visibility
from standalone_package import snapshot
from sync_sources import BuildError, Built


class CompositionTest(unittest.TestCase):
    def fixture(self, home):
        base = home.roots.home / 'base'
        home.skill(base, 'original')
        home.write(base / 'references/keep.md', 'Preserve this reference.\n')
        home.write(base / '.codex-plugin/plugin.json',
                   '{"name":"kit","version":"1.0.0","skills":["./skills/original"]}\n')
        extra = home.roots.home / 'extra'
        home.skill(extra, 'addition')
        home.write(extra / 'skills/addition/reference.md', 'Preserve this addition.\n')
        private = home.roots.home / 'private/setup'
        stored = snapshot(extra / 'skills', {'commit': 'b' * 40, 'selectedSources': {}}, private / 'snapshots')
        package = Package('kit', Visibility.PRIVATE, '', RepoSource('upstream', imports={'addition': 'sha256:' + stored.name}),
                          {}, ToolNotes.AUDIT, CodexPlacement((EngineInstall(True),)), None)
        built = Built('kit', base, '1.0.0', home.roots.tree_digest(base))
        return package, built, private, stored

    def test_composition_preserves_sources_and_carries_discovery_and_provenance(self):
        with Home() as home:
            package, base, private, stored = self.fixture(home)
            source_digest = home.roots.tree_digest(stored)
            result = compose(package, base, private, home.roots.engine, home.roots)
            self.assertEqual(home.roots.tree_digest(base.root), base.digest)
            self.assertEqual(home.roots.tree_digest(stored), source_digest)
            self.assertEqual((result.root / 'skills/original/SKILL.md').read_bytes(),
                             (base.root / 'skills/original/SKILL.md').read_bytes())
            self.assertEqual((result.root / 'references/keep.md').read_bytes(),
                             (base.root / 'references/keep.md').read_bytes())
            self.assertEqual((result.root / 'skills/addition/reference.md').read_bytes(),
                             (stored / 'skills/addition/reference.md').read_bytes())
            manifest = json.loads((result.root / '.codex-plugin/plugin.json').read_bytes())
            self.assertEqual(manifest['skills'], ['./skills/original', './skills/addition'])
            receipt = json.loads((result.root / '.dotfiles-composition.json').read_bytes())
            self.assertEqual(receipt['imports']['addition']['snapshot'], package.source.imports['addition'])
            self.assertEqual(compose(package, base, private, home.roots.engine, home.roots), result)

    def test_modified_snapshot_or_receipt_cannot_be_imported(self):
        for change_receipt in (False, True):
            with self.subTest(receipt=change_receipt), Home() as home:
                package, base, private, stored = self.fixture(home)
                if change_receipt:
                    record = json.loads((stored / 'source.json').read_bytes())
                    record['commit'] = 'c' * 40
                    (stored / 'source.json').write_text(json.dumps(record))
                else:
                    (stored / 'skills/addition/reference.md').write_text('Changed source')
                with self.assertRaisesRegex(BuildError, 'differs from its pin'):
                    compose(package, base, private, home.roots.engine, home.roots)

    def test_existing_skill_and_edited_composition_are_never_overwritten(self):
        with Home() as home:
            package, base, private, _ = self.fixture(home)
            result = compose(package, base, private, home.roots.engine, home.roots)
            (result.root / 'references/keep.md').write_text('User edit')
            with self.assertRaisesRegex(BuildError, 'immutable composed export was edited'):
                compose(package, base, private, home.roots.engine, home.roots)
            home.skill(base.root, 'addition')
            with self.assertRaisesRegex(BuildError, 'replace existing content'):
                compose(package, base, private, home.roots.engine, home.roots)


if __name__ == '__main__':
    unittest.main()
