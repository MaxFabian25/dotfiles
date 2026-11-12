import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import align_with_codex
import course_package
from description import BuildSource, CourseRecipe, GenericRecipe
import package
from portable import GIT


def head(repository):
    return subprocess.run([*GIT, '-C', str(repository), 'rev-parse', 'HEAD'], check=True, capture_output=True,
                          text=True).stdout.strip()


class BuildBytesTests(unittest.TestCase):
    def test_course_model_invocation_override_is_limited_to_the_selected_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            for name in ('selected', 'other'):
                skill = source / 'skills' / name
                skill.mkdir(parents=True)
                (skill / 'SKILL.md').write_text(
                    f'---\nname: {name}\ndescription: Example\ndisable-model-invocation: true\n---\nInstructions.\n')
            for arguments in (('init', '-q'), ('add', '.'),
                              ('-c', 'user.name=Build Test', '-c', 'user.email=build@example.invalid',
                               'commit', '-qm', 'Fixture')):
                subprocess.run([*GIT, '-C', str(source), *arguments], check=True, capture_output=True)
            recipe = CourseRecipe('https://example.invalid/course.git', head(source), ('skills/selected', 'skills/other'), '')
            course = BuildSource(recipe, 1, 'Example', metadata={
                'repository': 'example/course', 'collection': 'example', 'branch': 'main',
                'description': 'Example workflows.', 'url': 'https://example.invalid/course',
                'skillMetadata': {'selected': {'normalizations': [], 'allowImplicitInvocation': True},
                                  'other': {'normalizations': []}}})
            release, _ = course_package.build('example', recipe, course, source, output_root=root / 'output')
            for name, allowed in (('selected', True), ('other', False)):
                policy = yaml.safe_load((release / 'skills' / name / 'agents/openai.yaml').read_text())
                self.assertIs(policy['policy']['allow_implicit_invocation'], allowed)

    def test_receipt_bytes_do_not_depend_on_native_path_ordering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            skill = source / 'skills/example'
            skill.mkdir(parents=True)
            manifest = source / '.cursor-plugin/plugin.json'
            manifest.parent.mkdir()
            manifest.write_bytes(b'{"version":"1.0.0","author":{"name":"Example"}}\n')
            (source / 'LICENSE').write_bytes(b'Example license.\n')
            original = b'---\nname: example\ndescription: Example workflow.\n---\nOriginal instructions.\n'
            (skill / 'SKILL.md').write_bytes(original)
            (skill / 'guide.md').write_bytes(b'Original guide.\n')
            (skill / 'agents').mkdir()
            (skill / 'agents/openai.yaml').write_bytes(b'interface:\n  display_name: Example\n')
            for arguments in (('init', '-q'), ('add', '.'),
                              ('-c', 'user.name=Build Test', '-c', 'user.email=build@example.invalid',
                               'commit', '-qm', 'Fixture')):
                subprocess.run([*GIT, '-C', str(source), *arguments], check=True, capture_output=True)
            commit = head(source)
            generic_recipe = GenericRecipe('https://example.invalid/example.git', commit, None, '.cursor-plugin/plugin.json', ())
            generic = BuildSource(generic_recipe, 0, 'Example', metadata={'url': 'https://example.invalid/example'})
            course_recipe = CourseRecipe('https://example.invalid/course.git', commit, ('skills/example',), '')
            course = BuildSource(course_recipe, 1, 'Example', metadata={
                'repository': 'example/course', 'collection': 'example', 'branch': 'main',
                'description': 'Example workflows.', 'url': 'https://example.invalid/course',
                'skillMetadata': {'example': {'normalizations': []}}})
            for kind in ('generic', 'course'):
                artifacts = []
                for flavor in (PurePosixPath, PureWindowsPath):
                    def compare(left, right, flavor=flavor):
                        return flavor(*left.parts) < flavor(*right.parts)

                    output = root / kind / flavor.__name__
                    with patch.object(type(root), '__lt__', compare):
                        if kind == 'generic':
                            release, receipt = package.package('example', generic_recipe, generic, source, output_root=output)
                            self.assertEqual(list(receipt['upstreamFileHashes']), [
                                '.cursor-plugin/plugin.json', 'LICENSE', 'skills/example/SKILL.md',
                                'skills/example/agents/openai.yaml', 'skills/example/guide.md'])
                        else:
                            release, receipt = course_package.build('example', course_recipe, course, source, output_root=output)
                            self.assertEqual(list(receipt['upstreamFileHashes']['example']), [
                                'SKILL.md', 'agents/openai.yaml', 'guide.md'])
                            provenance = json.loads((release / 'skills/example/.course-source.json').read_bytes())
                            self.assertEqual(provenance['sourceFiles']['SKILL.md'], hashlib.sha256(original).hexdigest())
                        self.assertEqual((release / 'skills/example/guide.md').read_bytes(), b'Original guide.\n')
                        artifacts.append({path.relative_to(release).as_posix(): path.read_bytes()
                                          for path in release.rglob('*') if path.is_file()})
                self.assertEqual(artifacts[0], artifacts[1])

    def test_aligned_release_bytes_do_not_depend_on_native_path_ordering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            skill = source / 'skills/example/SKILL.md'
            skill.parent.mkdir(parents=True)
            skill.write_bytes(b'---\nname: example\ndescription: Example workflow.\n---\nOriginal instructions.\n')
            (source / 'plugin.json').write_bytes(b'{"name":"example","version":"1.0.0","skills":["./skills/"]}\n')
            agents = source / 'agents'
            agents.mkdir()
            for name in ('Zeta', 'alpha'):
                (agents / (name + '.md')).write_bytes(
                    f'---\nname: {name}\ndescription: Example agent.\n---\nOriginal instructions.\n'.encode())
            artifacts = []
            for flavor in (PurePosixPath, PureWindowsPath):
                def compare(left, right, flavor=flavor):
                    return flavor(*left.parts) < flavor(*right.parts)

                catalog = root / flavor.__name__
                with patch.object(type(root), '__lt__', compare):
                    entry = align_with_codex.build_from('example', source, set(), catalog)
                release = catalog / entry['source']
                manifest = json.loads((release / '.claude-plugin/plugin.json').read_bytes())
                self.assertEqual(manifest['agents'], ['./agents/Zeta.md', './agents/alpha.md'])
                artifacts.append((entry['version'], {path.relative_to(release).as_posix(): path.read_bytes()
                                                     for path in release.rglob('*') if path.is_file()}))
            self.assertEqual(artifacts[0], artifacts[1])


if __name__ == '__main__':
    unittest.main()
