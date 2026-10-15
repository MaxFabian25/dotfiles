import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import align_with_codex
import course_package
import package
from portable import GIT


class BuildBytesTests(unittest.TestCase):
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
            generic = {'checkout': str(source), 'subdirectory': '', 'manifest': '.cursor-plugin/plugin.json',
                       'displayName': 'Example', 'url': 'https://example.invalid/example'}
            course = {'checkout': str(source), 'packagingRevision': 1, 'repository': 'example/course',
                      'collection': 'example', 'branch': 'main', 'displayName': 'Example',
                      'description': 'Example workflows.', 'url': 'https://example.invalid/course',
                      'selectedSkills': [{'sourcePath': 'skills/example', 'sourceName': 'example',
                                          'installedName': 'example', 'normalizations': []}]}
            for kind in ('generic', 'course'):
                artifacts = []
                for flavor in (PurePosixPath, PureWindowsPath):
                    def compare(left, right, flavor=flavor):
                        return flavor(*left.parts) < flavor(*right.parts)

                    output = root / kind / flavor.__name__
                    with patch.object(type(root), '__lt__', compare):
                        if kind == 'generic':
                            release, receipt = package.package('example', config=generic, output_root=output)
                            self.assertEqual(list(receipt['upstreamFileHashes']), [
                                '.cursor-plugin/plugin.json', 'LICENSE', 'skills/example/SKILL.md',
                                'skills/example/agents/openai.yaml', 'skills/example/guide.md'])
                        else:
                            release, receipt = course_package.build('example', course, output_root=output)
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
