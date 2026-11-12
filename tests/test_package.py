from pathlib import Path
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from description import BuildSource, GenericRecipe
import package


def head(repository):
    return subprocess.run(['git', '-C', str(repository), 'rev-parse', 'HEAD'], check=True, capture_output=True,
                          text=True).stdout.strip()


class PublisherRootManifest(unittest.TestCase):
    def test_generated_identity_preserves_the_publisher_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, packages = root / 'source', root / 'packages'
            (source / '.cursor-plugin').mkdir(parents=True)
            packages.mkdir()
            publisher = {'name': 'publisher-name', 'version': '1.0.0',
                         'author': {'name': 'Publisher'}}
            original = json.dumps(publisher).encode()
            (source / 'plugin.json').write_bytes(original)
            (source / '.cursor-plugin/plugin.json').write_bytes(original)
            skill = source / 'skills/example'
            skill.mkdir(parents=True)
            (skill / 'SKILL.md').write_text('---\nname: example\ndescription: Example workflow.\ndisable-model-invocation: true\n---\nRead [guide](guide.md).\n')
            (skill / 'guide.md').write_text('Supporting guidance.\n')
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            subprocess.run(['git', 'add', '.'], cwd=source, check=True)
            subprocess.run(['git', '-c', 'user.name=Package Test', '-c',
                            'user.email=package-test@example.invalid', 'commit', '-qm', 'Fixture'],
                           cwd=source, check=True)
            recipe = GenericRecipe('https://example.invalid/source.git', head(source), None, '.cursor-plugin/plugin.json', ())
            profile = BuildSource(recipe, 0, 'Example', metadata={'url': 'https://example.invalid/source'})
            release, receipt = package.package('local-name', recipe, profile, source, output_root=packages)
            self.assertEqual((release.name, receipt['version']), ('1.0.0+upstream.' + recipe.commit[:12],) * 2)
            manifest = json.loads((release / 'plugin.json').read_text())
            self.assertEqual(manifest['name'], 'local-name')
            self.assertEqual(manifest, json.loads((release / '.codex-plugin/plugin.json').read_text()))
            preserved = receipt['preservedUpstreamFiles']['plugin.json']
            self.assertEqual((release / preserved).read_bytes(), original)
            for path, digest in receipt['upstreamFileHashes'].items():
                actual = release / receipt['preservedUpstreamFiles'].get(path, path)
                self.assertEqual(hashlib.sha256(actual.read_bytes()).hexdigest(), digest)
            self.assertEqual((release / 'skills/example/SKILL.md').read_bytes(), (skill / 'SKILL.md').read_bytes())
            self.assertIn('allow_implicit_invocation: false', (release / 'skills/example/agents/openai.yaml').read_text())
            repeated, repeated_receipt = package.package('local-name', recipe, profile, source, output_root=packages)
            self.assertEqual(repeated, release)
            self.assertEqual(repeated_receipt, receipt)


class SharedSkillRoot(unittest.TestCase):
    def build(self, skills, *folders):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source, packages = root / 'source', root / 'packages'
        packages.mkdir()
        (source / '.cursor-plugin').mkdir(parents=True)
        (source / '.cursor-plugin/plugin.json').write_text(json.dumps({'version': '1.0.0', 'author': {'name': 'P'}}))
        for folder in folders:
            (source / folder).mkdir(parents=True)
            (source / folder / 'SKILL.md').write_text(f'---\nname: {Path(folder).name}\ndescription: D.\n---\nBody.\n')
        subprocess.run(['git', 'init', '-q', str(source)], check=True)
        subprocess.run(['git', 'add', '.'], cwd=source, check=True)
        subprocess.run(['git', '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'F'], cwd=source, check=True)
        recipe = GenericRecipe('https://example.invalid/s.git', head(source), None, '.cursor-plugin/plugin.json', tuple(skills))
        profile = BuildSource(recipe, 0, 'E', metadata={'url': 'https://example.invalid/s'})
        release, receipt = package.package('kit', recipe, profile, source, output_root=packages)
        return release, receipt, json.loads((release / '.codex-plugin/plugin.json').read_text())

    def test_a_selection_exports_one_root_and_only_the_selected_skills(self):
        release, receipt, manifest = self.build(['./skills/group/keep', './skills/flat'],
                                                'skills/group/keep', 'skills/group/drop', 'skills/flat', 'skills/other')
        self.assertEqual(manifest['skills'], ['./skills/'])
        self.assertEqual(sorted(p.parent.relative_to(release).as_posix() for p in release.rglob('SKILL.md')),
                         ['skills/flat', 'skills/group/keep'])
        self.assertEqual(receipt['selectedSkillRoots'], ['./skills/group/keep', './skills/flat'])
        self.assertEqual(receipt['omittedUpstreamFiles'], ['skills/group/drop/SKILL.md', 'skills/other/SKILL.md'])

    def test_a_selection_outside_skills_stays_as_named(self):
        release, receipt, manifest = self.build(['./tool-a'], 'tool-a', 'tool-b')
        self.assertEqual(manifest['skills'], ['./tool-a'])
        self.assertTrue((release / 'tool-b/SKILL.md').is_file())
        self.assertNotIn('omittedUpstreamFiles', receipt)


class RuntimeDependencies(unittest.TestCase):
    def test_installed_node_modules_are_not_package_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'skills/x/scripts/node_modules/commander').mkdir(parents=True)
            (root / 'skills/x/scripts/run.ts').write_text('code')
            (root / 'skills/x/scripts/node_modules/commander/package.json').write_text('{}')
            self.assertEqual(list(package.files(root)), ['skills/x/scripts/run.ts'])

    def test_claude_runtime_markers_are_not_package_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.in_use').mkdir()
            (root / '.in_use/12345').write_text('')
            (root / '.orphaned_at').write_text('1')
            (root / 'SKILL.md').write_text('skill')
            self.assertEqual(list(package.files(root)), ['SKILL.md'])

    def test_python_caches_are_not_package_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts/__pycache__').mkdir(parents=True)
            (root / 'scripts/tool.py').write_text('code')
            (root / 'scripts/__pycache__/tool.cpython-314.pyc').write_bytes(b'compiled')
            self.assertEqual(list(package.files(root)), ['scripts/tool.py'])




if __name__ == '__main__':
    unittest.main()
