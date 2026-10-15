import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


spec = importlib.util.spec_from_file_location('alignment', Path(__file__).resolve().parents[1] / 'align_with_codex.py')
alignment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alignment)


class AgentSelectionTests(unittest.TestCase):
    def write_agents(self, root, *names):
        (root / 'agents').mkdir()
        for name in names:
            (root / 'agents' / f'{name}.md').write_text(f'---\nname: {name}\ndescription: Test agent.\n---\nBody\n')

    def test_selected_plugin_keeps_only_listed_agents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_agents(root, 'ci-watcher', 'reviewer')
            self.assertEqual(alignment.drop_unselected_agents(root, 'cursor-team-kit', set()), ['ci-watcher.md', 'reviewer.md'])
            self.assertEqual(alignment.package_agents(root), {})

    def test_unlisted_plugin_keeps_every_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_agents(root, 'poteto-agent')
            self.assertEqual(alignment.drop_unselected_agents(root, 'pstack-plugin'), [])
            self.assertEqual(list(alignment.package_agents(root)), ['poteto-agent'])


class ClaudeSkillTests(unittest.TestCase):
    def write_skills(self, root, *names):
        for name in names:
            (root / 'skills' / name).mkdir(parents=True)
            (root / 'skills' / name / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Test skill.\n---\nBody\n')

    def test_a_claude_skill_stays_in_the_copy_when_codex_has_it_off(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.write_skills(root, 'claude-handoff', 'handoff', 'tdd')
            disabled = {'mattpocock-skills:claude-handoff', 'mattpocock-skills:handoff'}
            self.assertEqual([str(path) for path in alignment.selected_skills(root, {}, disabled, 'mattpocock-skills', {'claude-handoff'})],
                             ['skills/claude-handoff/SKILL.md', 'skills/tdd/SKILL.md'])

    def test_a_claude_skill_is_not_listed_as_excluded(self):
        disabled = {'mattpocock-skills:claude-handoff', 'mattpocock-skills:handoff', 'cursor-team-kit:fix-ci'}
        self.assertEqual(alignment.excluded_skill_names(disabled, 'mattpocock-skills', {'claude-handoff'}), ['mattpocock-skills:handoff'])
        self.assertEqual(alignment.excluded_skill_names(disabled, 'cursor-team-kit'), ['cursor-team-kit:fix-ci'])


class DevelopmentManifestTests(unittest.TestCase):
    def source(self, root):
        source = root / 'source'
        (source / '.codex-plugin').mkdir(parents=True)
        (source / '.codex-plugin/plugin.json').write_text(json.dumps({'name': 'example', 'version': '1.0.0'}))
        ClaudeSkillTests().write_skills(source, 'example')
        (source / 'package.json').write_bytes(b'{"devDependencies":{"release-tool":"1"}}\r\n')
        (source / 'package-lock.json').write_bytes(b'{"lockfileVersion":3}\n')
        return source

    def test_relocation_preserves_bytes_changes_version_and_is_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.source(root)
            before = {path.relative_to(source): path.read_bytes() for path in source.rglob('*') if path.is_file()}
            plain = alignment.build_from('example', source, set(), root / 'catalog')
            adapted = alignment.build_from('example', source, set(), root / 'catalog',
                                           development_manifests=('package.json', 'package-lock.json'))
            self.assertNotEqual(plain['version'], adapted['version'])
            built = root / 'catalog' / adapted['source']
            receipt = alignment.read_json(built / '.claude-plugin/codex-alignment.json')
            self.assertEqual(len(receipt['developmentManifests']), 2)
            for entry in receipt['developmentManifests']:
                original = before[Path(entry['source'])]
                self.assertEqual((built / entry['destination']).read_bytes(), original)
                self.assertEqual(entry['sha256'], hashlib.sha256(original).hexdigest())
                self.assertFalse((built / entry['source']).exists())
                self.assertEqual((root / 'catalog' / plain['source'] / entry['source']).read_bytes(), original)
            self.assertEqual((built / 'skills/example/SKILL.md').read_bytes(), before[Path('skills/example/SKILL.md')])
            self.assertEqual(before, {path.relative_to(source): path.read_bytes() for path in source.rglob('*') if path.is_file()})
            self.assertEqual(adapted, alignment.build_from('example', source, set(), root / 'catalog',
                             development_manifests=('package-lock.json', 'package.json')))

    def test_invalid_missing_or_occupied_manifests_are_rejected(self):
        cases = ('unexpected', 'duplicate', 'missing', 'occupied', 'parent-file')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = self.source(root)
                selected = ('package.json', 'package-lock.json')
                if case == 'unexpected':
                    selected = ('../package.json', 'package-lock.json')
                elif case == 'duplicate':
                    selected += ('package.json',)
                elif case == 'missing':
                    (source / 'package.json').unlink()
                elif case == 'occupied':
                    (source / 'provenance/development').mkdir(parents=True)
                    (source / 'provenance/development/package.json').write_bytes(b'preserve me')
                else:
                    (source / 'provenance').write_bytes(b'preserve me')
                with self.assertRaises(RuntimeError):
                    alignment.build_from('example', source, set(), root / 'catalog', development_manifests=selected)
                self.assertFalse((root / 'catalog').exists())

    @unittest.skipIf(sys.platform == 'win32', 'Windows links require privileges')
    def test_linked_manifest_is_rejected_without_following_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.source(root)
            (source / 'package.json').unlink()
            (source / 'package.json').symlink_to(source / 'package-lock.json')
            with self.assertRaisesRegex(RuntimeError, 'linked'):
                alignment.build_from('example', source, set(), root / 'catalog',
                                     development_manifests=('package.json', 'package-lock.json'))


if __name__ == '__main__':
    unittest.main()
