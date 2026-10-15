from pathlib import Path
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import package


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
            profile = {'checkout': str(source), 'subdirectory': '',
                       'manifest': '.cursor-plugin/plugin.json', 'displayName': 'Example',
                       'url': 'https://example.invalid/source'}
            (packages / 'sources.json').write_text(json.dumps({'local-name': profile}))
            with patch.object(package, 'ROOT', packages):
                release, receipt = package.package('local-name')
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
                repeated, repeated_receipt = package.package('local-name')
                self.assertEqual(repeated, release)
                self.assertEqual(repeated_receipt, receipt)


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




if __name__ == '__main__':
    unittest.main()
