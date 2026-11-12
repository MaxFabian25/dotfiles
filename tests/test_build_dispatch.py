"""Builder.codex sends each recipe type to its own builder with every recipe field, checked on the built output."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from description import (AcademicRecipe, BuildSource, ClaudePlacement, CodexPlacement, CourseRecipe, EngineInstall,  # noqa: E402
                         GenericRecipe, Package, PublicEmilRecipe, Roots, Setup, SnapshotRecipe, ToolNotes, Visibility)
from host_state import DeviceRoots  # noqa: E402
from portable import GIT  # noqa: E402
from sync_sources import Builder, BuildError, Sources  # noqa: E402

URL = 'https://example.invalid/upstream.git'
INSTALLER = '''NAMES = ('one', 'two')


def prepare(source, prepared, destination, python):
    for name in NAMES:
        (prepared / name / 'references').mkdir(parents=True)
        (prepared / name / 'SKILL.md').write_text('---\\nname: ' + name + '\\ndescription: Prepared.\\n---\\nBody.\\n')
        (prepared / name / 'references/guide.md').write_text('Run .claude/skills/' + name + '/tool.py\\n')
'''


def git(repository, *arguments):
    return subprocess.run([*GIT, '-C', str(repository), *arguments], check=True, capture_output=True,
                          text=True).stdout.strip()


def read(path):
    return json.loads(path.read_text())


class BuildDispatch(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.upstream = self.root / 'upstream'

    def commit(self, files):
        for relative, text in files.items():
            (self.upstream / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.upstream / relative).write_text(text)
        git(self.upstream, 'init', '-q', '-b', 'main')
        git(self.upstream, 'remote', 'add', 'origin', URL)
        git(self.upstream, 'add', '.')
        git(self.upstream, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'Fixture')
        return git(self.upstream, 'rev-parse', 'HEAD')

    def builder(self, package):
        setup = Setup(Roots(self.root / 'public/setup', self.root / 'private/setup'), {'kit': package}, {}, {}, {},
                      None, (), frozenset(), {})
        roots = replace(DeviceRoots.current(self.root), engine=self.root / 'engine')
        return Builder(setup, roots, roots.engine, sources=Sources(self.root / 'sources', seeds=(self.upstream,)))

    def build(self, source):
        package = Package('kit', Visibility.PUBLIC, '', source, {}, ToolNotes.AUDIT,
                          CodexPlacement((EngineInstall(False),)), None)
        return self.builder(package).codex(package, verify=False).root

    def snapshot(self):
        from standalone_package import snapshot
        (self.root / 'input/one').mkdir(parents=True)
        (self.root / 'input/one/SKILL.md').write_text('---\nname: one\ndescription: One.\n---\nBody.\n')
        return snapshot(self.root / 'input', {'commit': None}, self.root / 'private/setup/snapshots')

    def test_a_generic_recipe_builds_its_subdirectory_manifest_and_selection(self):
        commit = self.commit({
            'nested/.cursor-plugin/plugin.json': json.dumps({'version': '2.0.0', 'author': {'name': 'Upstream'}}),
            'nested/skills/keep/SKILL.md': '---\nname: keep\ndescription: Kept.\n---\nBody.\n',
            'nested/skills/drop/SKILL.md': '---\nname: drop\ndescription: Dropped.\n---\nBody.\n'})
        recipe = GenericRecipe(URL, commit, 'nested', '.cursor-plugin/plugin.json', ('./skills/keep',))
        release = self.build(BuildSource(recipe, 5, 'Kit', metadata={'url': 'https://example.invalid/kit'}))
        self.assertEqual(release.name, f'2.0.0+upstream.{commit[:12]}.codex5')
        manifest = read(release / '.codex-plugin/plugin.json')
        self.assertEqual((manifest['interface']['displayName'], manifest['interface']['websiteURL']),
                         ('Kit', 'https://example.invalid/kit'))
        receipt = read(release / '.codex-plugin/upstream.json')
        self.assertEqual((receipt['subdirectory'], receipt['selectedSkillRoots'], receipt['omittedUpstreamFiles']),
                         ('nested', ['./skills/keep'], ['skills/drop/SKILL.md']))

    def test_a_course_recipe_builds_prefixed_trimmed_skills_with_their_metadata(self):
        commit = self.commit({
            'LICENSE': 'License.\n',
            'skills/alpha/SKILL.md': '---\nname: alpha\ndescription: "Alpha work. Triggers on: alpha"\n---\nBody.\n',
            'skills/beta/SKILL.md': '---\nname: beta\ndescription: "Beta work. Triggers on: beta"\n---\nBody.\n'})
        recipe = CourseRecipe(URL, commit, ('skills/alpha', 'skills/beta'), 'course-', ('course-alpha',))
        release = self.build(BuildSource(recipe, 3, 'Course', metadata={
            'url': 'https://example.invalid/course', 'description': 'Course skills.', 'repository': 'example/course',
            'collection': 'course', 'branch': 'main',
            'skillMetadata': {'course-alpha': {'normalizations': ['trimmed'], 'allowImplicitInvocation': True},
                              'course-beta': {'normalizations': []}}}))
        self.assertEqual(release.name, f'0.1.0+upstream.{commit[:12]}.codex3')
        self.assertEqual(sorted(p.name for p in (release / 'skills').iterdir()), ['course-alpha', 'course-beta'])
        descriptions = {name: yaml.safe_load((release / 'skills' / name / 'SKILL.md').read_text().split('---')[1])
                        for name in ('course-alpha', 'course-beta')}
        self.assertEqual(descriptions['course-alpha'], {'name': 'course-alpha', 'description': 'Alpha work.'})
        self.assertEqual(descriptions['course-beta']['description'], 'Beta work. Triggers on: beta')
        policy = yaml.safe_load((release / 'skills/course-alpha/agents/openai.yaml').read_text())
        self.assertIs(policy['policy']['allow_implicit_invocation'], True)
        self.assertEqual(read(release / 'skills/course-alpha/.course-source.json')['normalizations'], ['trimmed'])
        self.assertEqual(read(release / '.codex-plugin/plugin.json')['interface']['displayName'], 'Course')
        self.assertEqual(read(release / '.codex-plugin/upstream.json')['selectedSkills'][0], {
            'sourcePath': 'skills/alpha', 'sourceName': 'alpha', 'installedName': 'course-alpha',
            'normalizations': ['trimmed'], 'allowImplicitInvocation': True})

    def test_a_public_emil_recipe_builds_renamed_licensed_skills_with_their_metadata(self):
        commit = self.commit({
            'LICENSE': 'MIT License.\n',
            'skills/motion/SKILL.md': '---\nname: motion\ndescription: Motion.\ndisable-model-invocation: true\n---\nBody.\n'})
        sidecar = 'policy:\n  allow_implicit_invocation: false\n'
        recipe = PublicEmilRecipe(URL, commit, ('skills/motion',), {'motion': 'emil-motion'})
        release = self.build(BuildSource(recipe, 2, 'Motion', metadata={
            'url': 'https://example.invalid/emil', 'description': 'Motion skills.', 'branch': 'main',
            'skillMetadata': {'emil-motion': {'removeDisableModelInvocation': True, 'openaiYaml': sidecar,
                                              'legacyProvenance': True}}}))
        self.assertEqual(release.name, f'0.1.0+upstream.{commit[:12]}.codex2')
        skill = release / 'skills/emil-motion'
        self.assertEqual((skill / 'SKILL.md').read_text(), '---\nname: emil-motion\ndescription: Motion.\n---\nBody.\n')
        self.assertEqual((skill / 'LICENSE.txt').read_text(), 'MIT License.\n')
        self.assertEqual((skill / 'agents/openai.yaml').read_text(), sidecar)
        self.assertEqual(read(skill / '.emil-upstream.json')['localName'], 'emil-motion')
        manifest = read(release / '.codex-plugin/plugin.json')
        self.assertEqual((manifest['license'], manifest['interface']['displayName']), ('MIT', 'Motion'))

    def test_an_academic_recipe_builds_with_its_reviewed_installer_and_requirements(self):
        requirements = 'pyyaml==6.0.3\n'
        commit = self.commit({'LICENSE': 'License.\n', 'scripts/install-codex-skills.py': INSTALLER,
                              'scripts/codex-skills-requirements.txt': requirements})
        installer = hashlib.sha256(INSTALLER.encode()).hexdigest()
        required = hashlib.sha256(requirements.encode()).hexdigest()
        recipe = AcademicRecipe(URL, commit, ('one',), 'sha256:' + installer, 'sha256:' + required)
        policies = {'one': {'frontmatter': {'disable-model-invocation': True}, 'openaiYaml': 'policy: {}\n'}}
        source = BuildSource(recipe, 4, 'Academic', metadata={
            'url': 'https://example.invalid/academic', 'python': '/usr/bin/python3', 'invocationPolicies': policies})
        release = self.build(source)
        self.assertEqual(release.name, f'0.1.0+upstream.{commit[:12]}.codex4')
        self.assertEqual([p.name for p in (release / 'skills').iterdir()], ['one'])
        self.assertIn('disable-model-invocation: true', (release / 'skills/one/SKILL.md').read_text())
        self.assertEqual((release / 'skills/one/references/guide.md').read_text(), 'Run {skill_dir}/tool.py\n')
        receipt = read(release / '.codex-plugin/upstream.json')
        self.assertEqual((receipt['reviewedInstallerSha256'], receipt['runtimeRequirementsSha256'], receipt['python']),
                         (installer, required, '/usr/bin/python3'))
        self.assertEqual(read(release / '.codex-plugin/plugin.json')['interface']['displayName'], 'Academic')
        with self.assertRaisesRegex(RuntimeError, 'installer changed'):
            self.build(replace(source, recipe=replace(recipe, installer='sha256:' + required)))

    def test_a_snapshot_recipe_builds_its_selected_skills_with_its_metadata(self):
        stored = self.snapshot()
        recipe = SnapshotRecipe('sha256:' + stored.name, {'one': None})
        release = self.build(BuildSource(recipe, 7, 'Snap', metadata={
            'description': 'Snapshot skills.', 'sourceKind': 'personal', 'repository': 'example/snap'}))
        self.assertEqual(release.name, f'0.1.0+snapshot.{stored.name[:12]}.personal7')
        manifest = read(release / '.codex-plugin/plugin.json')
        self.assertEqual((manifest['repository'], manifest['interface']['displayName'], manifest['description']),
                         ('https://github.com/example/snap', 'Snap', 'Snapshot skills.'))
        receipt = read(release / '.codex-plugin/upstream.json')
        self.assertEqual((receipt['sourceKind'], receipt['selectedSkills']), ('personal', ['one']))

    def test_a_claude_collection_takes_its_packaging_and_description_from_the_recipe_metadata(self):
        stored = self.snapshot()
        source = BuildSource(SnapshotRecipe('sha256:' + stored.name, {'one': None}), 1, 'Snap', metadata={
            'description': 'Snapshot skills.', 'sourceKind': 'personal', 'claudePackaging': 3,
            'claudeDescriptions': {'kit': 'Claude snapshot skills.'}})
        package = Package('kit', Visibility.PRIVATE, '', source, {}, ToolNotes.AUDIT,
                          CodexPlacement((EngineInstall(False),)), ClaudePlacement((EngineInstall(True),)))
        builder = self.builder(package)
        [claude] = builder.claude(package, builder.codex(package, verify=False), verify=False).values()
        self.assertEqual(claude.version, f'0.1.0+snapshot.{stored.name[:12]}.personal1.claude3')
        manifest = read(claude.root / '.claude-plugin/plugin.json')
        self.assertEqual((manifest['name'], manifest['description']), ('kit', 'Claude snapshot skills.'))
        self.assertEqual(read(claude.root / '.claude-plugin/upstream.json')['selectedSkills'], ['one'])

    def test_a_recipe_without_its_build_metadata_names_the_migration(self):
        commit = self.commit({'skills/one/SKILL.md': '---\nname: one\ndescription: One.\n---\nBody.\n'})
        with self.assertRaisesRegex(BuildError, '^kit: missing build metadata; migrate its recipe first$'):
            self.build(BuildSource(CourseRecipe(URL, commit, ('skills/one',), ''), 1, 'Kit'))
        (self.root / 'private/setup/snapshots' / ('a' * 64)).mkdir(parents=True)
        with self.assertRaisesRegex(BuildError, '^kit: missing snapshot build metadata; migrate its recipe first$'):
            self.build(BuildSource(SnapshotRecipe('sha256:' + 'a' * 64, {}), 1, 'Kit', metadata={'description': 'D'}))


if __name__ == '__main__':
    unittest.main()
