import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('alignment', Path(__file__).resolve().parents[1] / 'align_with_codex.py')
alignment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alignment)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homes import hold_built_trees_once  # noqa: E402


def write_agents(root, *names):
    (root / 'agents').mkdir()
    for name in names:
        (root / 'agents' / f'{name}.md').write_text(f'---\nname: {name}\ndescription: Test agent.\n---\nBody\n', newline='\n')


def write_skills(root, *names):
    for name in names:
        (root / 'skills' / name).mkdir(parents=True)
        (root / 'skills' / name / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Test skill.\n---\nBody\n',
                                                       newline='\n')


def plugin_source(root, *skills):
    source = root / 'source'
    (source / '.codex-plugin').mkdir(parents=True)
    (source / '.codex-plugin/plugin.json').write_text(json.dumps({'name': 'example', 'version': '1.0.0'}))
    write_skills(source, *skills)
    return source


class AgentSelectionTests(unittest.TestCase):
    def test_selected_plugin_keeps_only_listed_agents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_agents(root, 'ci-watcher', 'reviewer')
            self.assertEqual(alignment.drop_unselected_agents(root, set()), ['ci-watcher.md', 'reviewer.md'])
            self.assertEqual(alignment.package_agents(root), {})

    def test_no_selection_keeps_every_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_agents(root, 'poteto-agent')
            self.assertEqual(alignment.drop_unselected_agents(root, None), [])
            self.assertEqual(list(alignment.package_agents(root)), ['poteto-agent'])


class ClaudeSkillTests(unittest.TestCase):
    def test_a_claude_skill_stays_in_the_copy_when_codex_has_it_off(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            write_skills(root, 'claude-handoff', 'handoff', 'tdd')
            disabled = {'mattpocock-skills:claude-handoff', 'mattpocock-skills:handoff'}
            self.assertEqual([str(path) for path in alignment.selected_skills(root, {}, disabled, 'mattpocock-skills', {'claude-handoff'})],
                             ['skills/claude-handoff/SKILL.md', 'skills/tdd/SKILL.md'])

    def test_a_claude_skill_is_not_listed_as_excluded(self):
        disabled = {'mattpocock-skills:claude-handoff', 'mattpocock-skills:handoff', 'cursor-team-kit:fix-ci'}
        self.assertEqual(alignment.excluded_skill_names(disabled, 'mattpocock-skills', {'claude-handoff'}), ['mattpocock-skills:handoff'])
        self.assertEqual(alignment.excluded_skill_names(disabled, 'cursor-team-kit'), ['cursor-team-kit:fix-ci'])

    def test_a_built_copy_keeps_the_claude_skill_that_codex_has_off(self):
        held = hold_built_trees_once(self)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = plugin_source(root, 'claude-handoff', 'handoff', 'tdd')
            disabled = {'example:claude-handoff', 'example:handoff'}
            entry = alignment.build_from('example', source, disabled, root / 'catalog', kept_skills={'claude-handoff'})
            built = root / 'catalog' / entry['source']
            self.assertEqual(held, [built])
            self.assertEqual(sorted(path.name for path in (built / 'skills').iterdir()), ['claude-handoff', 'tdd'])
            receipt = json.loads((built / '.claude-plugin/codex-alignment.json').read_text())
            self.assertEqual(receipt['excludedSkillNames'], ['example:handoff'])

    def test_each_agent_selection_is_its_own_copy_in_one_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = plugin_source(root, 'tdd')
            write_agents(source, 'ci-watcher', 'reviewer')
            versions = set()
            for agents, kept in ((None, ['ci-watcher.md', 'reviewer.md']), ({'reviewer'}, ['reviewer.md']), (set(), [])):
                entry = alignment.build_from('example', source, set(), root / 'catalog', agents=agents)
                versions.add(entry['version'])
                self.assertEqual(sorted(path.name for path in (root / 'catalog' / entry['source'] / 'agents').iterdir()), kept)
            self.assertEqual(len(versions), 3)
            invocable = alignment.build_from('example', source, set(), root / 'catalog', invocable={'reviewer'})
            self.assertNotIn(invocable['version'], versions)

    def test_a_copy_that_keeps_every_agent_keeps_the_version_it_had_without_a_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = plugin_source(root, 'tdd')
            write_agents(source, 'reviewer')
            unselected = alignment.build_from('example', source, set(), root / 'unselected')
            every = alignment.build_from('example', source, set(), root / 'every', agents=None)
            self.assertEqual(every['version'], unselected['version'])
            # What build_from gave this source at f17b0ab, before the selection counted; existing locks name such versions.
            # The fixture writes LF on every platform, so the pin holds on Windows too.
            self.assertEqual(every['version'], '1.0.0+codex-aligned.6f5a2679a652')


class InvocableSkillTests(unittest.TestCase):
    def test_an_invocable_skill_loses_the_explicit_flag_that_codex_keeps(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            (source / '.codex-plugin').mkdir(parents=True)
            (source / '.codex-plugin/plugin.json').write_text(json.dumps({'name': 'example', 'version': '1.0.0'}))
            for skill in ('mode', 'leaf'):
                (source / 'skills' / skill / 'agents').mkdir(parents=True)
                (source / 'skills' / skill / 'SKILL.md').write_text(
                    f'---\nname: {skill}\ndescription: Test skill.\ndisable-model-invocation: true\n---\nBody\n')
                (source / 'skills' / skill / 'agents/openai.yaml').write_text('policy:\n  allow_implicit_invocation: false\n')
            plain = alignment.build_from('example', source, set(), root / 'catalog')
            invocable = alignment.build_from('example', source, set(), root / 'catalog', invocable={'mode'})
            self.assertNotEqual(plain['version'], invocable['version'])
            built = root / 'catalog' / invocable['source']
            self.assertIn('disable-model-invocation: false', (built / 'skills/mode/SKILL.md').read_text())
            self.assertIn('disable-model-invocation: true', (built / 'skills/leaf/SKILL.md').read_text())
            self.assertIn('disable-model-invocation: true', (root / 'catalog' / plain['source'] / 'skills/mode/SKILL.md').read_text())
            self.assertIn('disable-model-invocation: true', (source / 'skills/mode/SKILL.md').read_text())

    def test_a_skill_codex_keeps_explicit_only_by_its_sidecar_is_explicit_only_in_the_claude_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = plugin_source(root, 'mode', 'leaf')
            (source / 'skills/mode/agents').mkdir()
            (source / 'skills/mode/agents/openai.yaml').write_text('policy:\n  allow_implicit_invocation: false\n')
            built = root / 'catalog' / alignment.build_from('example', source, set(), root / 'catalog')['source']
            self.assertIn('disable-model-invocation: true', (built / 'skills/mode/SKILL.md').read_text())
            self.assertNotIn('disable-model-invocation', (built / 'skills/leaf/SKILL.md').read_text())


class DevelopmentManifestTests(unittest.TestCase):
    def source(self, root):
        source = plugin_source(root, 'example')
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
