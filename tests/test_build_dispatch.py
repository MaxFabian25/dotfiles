"""Builder.codex and Builder.claude send each recipe type to its own builder."""
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from description import (BuildSource, ClaudePlacement, CodexPlacement, CourseRecipe, EngineInstall,  # noqa: E402
                         GenericRecipe, LocalSource, Package, PublicEmilRecipe, Repo, RepoSource, Roots, Setup, SnapshotRecipe, ToolNotes, Visibility)
from homes import hold_built_trees_once  # noqa: E402
from host_state import DeviceRoots  # noqa: E402
from portable import GIT, read_utf8  # noqa: E402
from sync_sources import Builder, BuildError, Sources  # noqa: E402

URL = 'https://example.invalid/upstream.git'
ISOLATED_GIT_ENV = {'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid', 'GIT_COMMITTER_NAME': 'Test',
                    'GIT_COMMITTER_EMAIL': 'test@example.invalid', 'GIT_CONFIG_GLOBAL': os.devnull,
                    'GIT_CONFIG_NOSYSTEM': '1'}
SCHEMA = 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json'
RECEIPTS = ('.codex-plugin/upstream.json', '.claude-plugin/upstream.json')


def git(repository, *arguments):
    return subprocess.run([*GIT, '-C', str(repository), *arguments], check=True, capture_output=True, text=True,
                          env={**os.environ, **ISOLATED_GIT_ENV}).stdout.strip()


def sha(data):
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def in_path_order(hashes):
    return dict(sorted(hashes.items(), key=lambda item: PurePosixPath(item[0]).parts))


def fixture_hashes(files, folder=''):
    return in_path_order({path.removeprefix(folder): sha(content) for path, content in files.items()
                          if path.startswith(folder)})


def snapshot_skill(name):
    return f'---\nname: {name}\ndescription: {name.title()}.\n---\nBody.\n'


@dataclass(frozen=True)
class RetiredRecipe:
    repository: str
    commit: str


class BuildDispatch(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.upstream = self.root / 'upstream'
        self.held = hold_built_trees_once(self)

    def write(self, root, files):
        for relative, text in files.items():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_bytes(text.encode())

    def commit(self, files, later):
        """Commits the fixture and then `later` on top of it, and returns the fixture's commit."""
        self.write(self.upstream, files)
        git(self.upstream, 'init', '-q', '-b', 'main')
        git(self.upstream, 'remote', 'add', 'origin', URL)
        git(self.upstream, 'add', '.')
        git(self.upstream, 'commit', '-qm', 'Fixture')
        pinned = git(self.upstream, 'rev-parse', 'HEAD')
        self.write(self.upstream, later)
        git(self.upstream, 'add', '.')
        git(self.upstream, 'commit', '-q', '--allow-empty', '-m', 'Later')
        return pinned

    def assertRebuildKeepsTheRelease(self, source, release):
        """A rebuild finds the release it built before, and refuses one edited since rather than replacing it."""
        self.assertEqual(self.build(source), release)
        (release / '.codex-plugin/plugin.json').write_text('{}\n')
        with self.assertRaisesRegex(RuntimeError, f'(?i)^(existing )?immutable release differs: {re.escape(str(release))}$'):
            self.build(source)

    def built_json(self, path):
        raw = read_utf8(path)
        data = json.loads(raw)
        self.assertEqual(raw, json.dumps(data, indent=2) + '\n', f'{path} is not laid out as the builders write JSON')
        return data

    def assertEqualInOrder(self, actual, expected):
        self.assertEqual(json.dumps(actual, indent=2), json.dumps(expected, indent=2))

    def assertFields(self, data, expected):
        self.assertEqualInOrder({key: data.get(key, '<absent>') for key in expected}, expected)

    def packaged(self, release, paths):
        hashes = in_path_order({path.relative_to(release).as_posix(): sha(path.read_bytes()) for path in release.rglob('*')
                                if path.is_file() and path.relative_to(release).as_posix() not in RECEIPTS})
        self.assertEqual(list(hashes), paths, f'{release} does not hold exactly the files its recipe builds')
        return hashes

    def builder(self, package, *, private=True, repos=None):
        setup = Setup(Roots(self.root / 'public/setup', self.root / 'private/setup' if private else None),
                      {package.name: package}, repos or {}, {}, {}, None, (), frozenset(), {})
        roots = replace(DeviceRoots.current(self.root), engine=self.root / 'engine')
        return Builder(setup, roots, roots.engine, sources=Sources(self.root / 'sources', seeds=(self.upstream,)))

    def build(self, source, *, private=True):
        package = Package('kit', Visibility.PUBLIC, '', source, {}, ToolNotes.AUDIT,
                          CodexPlacement((EngineInstall(False),)), None)
        return self.builder(package, private=private).codex(package, verify=False).root

    def snapshot(self, *skills):
        from standalone_package import snapshot
        for skill in skills or ('one',):
            self.write(self.root / 'input', {f'{skill}/SKILL.md': snapshot_skill(skill)})
        return snapshot(self.root / 'input', {'commit': None}, self.root / 'private/setup/snapshots')

    def test_a_generic_recipe_builds_its_subdirectory_manifest_and_selection(self):
        files = {
            'nested/.cursor-plugin/plugin.json': json.dumps({'version': '2.0.0', 'author': {'name': 'Upstream'}}),
            'nested/skills/keep/SKILL.md': '---\nname: keep\ndescription: Kept.\n---\nBody.\n',
            'nested/skills/drop/SKILL.md': '---\nname: drop\ndescription: Dropped.\n---\nBody.\n'}
        commit = self.commit(files, later={
            'nested/.cursor-plugin/plugin.json': json.dumps({'version': '3.0.0', 'author': {'name': 'Later'}}),
            'nested/skills/keep/SKILL.md': '---\nname: keep\ndescription: Kept.\n---\nLater.\n'})
        recipe = GenericRecipe(URL, commit, 'nested', '.cursor-plugin/plugin.json', ('./skills/keep',))
        release = self.build(BuildSource(recipe, 5, 'Kit', metadata={'url': 'https://example.invalid/kit'}))
        version = f'2.0.0+upstream.{commit[:12]}.codex5'
        self.assertEqual(release.name, version)
        self.assertIn(release, self.held)
        self.assertEqualInOrder(self.built_json(release / '.codex-plugin/plugin.json'), {
            'author': {'name': 'Upstream'}, 'name': 'kit', 'version': version, 'skills': ['./skills/'],
            'interface': {'displayName': 'Kit', 'shortDescription': 'Original upstream skills', 'developerName': 'Upstream',
                          'category': 'Developer Tools', 'websiteURL': 'https://example.invalid/kit'}})
        self.assertEqualInOrder(self.built_json(release / '.codex-plugin/upstream.json'), {
            'repository': 'https://example.invalid/kit', 'commit': commit, 'subdirectory': 'nested', 'version': version,
            'selectedSkillRoots': ['./skills/keep'], 'upstreamFileHashes': fixture_hashes(files, 'nested/'),
            'packagingOnlyFiles': ['.codex-plugin/plugin.json'], 'omittedUpstreamFiles': ['skills/drop/SKILL.md']})

    def test_a_generic_recipe_without_a_subdirectory_or_selection_builds_every_skill_and_copies_the_manifest(self):
        files = {
            '.cursor-plugin/plugin.json': json.dumps({
                'name': 'upstream-kit', 'version': '1.2.0', 'description': 'Upstream kit.', 'author': {'name': 'Upstream'},
                'homepage': 'https://example.invalid/home', 'license': 'MIT', 'keywords': ['kit']}),
            'skills/one/SKILL.md': '---\nname: one\ndescription: One.\ndisable-model-invocation: true\n---\nBody.\n',
            'skills/two/SKILL.md': '---\nname: two\ndescription: Two.\n---\nBody.\n'}
        commit = self.commit(files, later={'skills/three/SKILL.md': '---\nname: three\ndescription: Three.\n---\nBody.\n'})
        recipe = GenericRecipe(URL, commit, None, '.cursor-plugin/plugin.json', ())
        release = self.build(BuildSource(recipe, 1, 'Kit', metadata={'url': 'https://example.invalid/kit'}))
        self.assertEqual(sorted(p.name for p in (release / 'skills').iterdir()), ['one', 'two'])
        version = f'1.2.0+upstream.{commit[:12]}.codex1'
        self.assertEqualInOrder(self.built_json(release / '.codex-plugin/plugin.json'), {
            'description': 'Upstream kit.', 'author': {'name': 'Upstream'}, 'homepage': 'https://example.invalid/home',
            'license': 'MIT', 'keywords': ['kit'], 'name': 'kit', 'version': version, 'skills': ['./skills/'],
            'interface': {'displayName': 'Kit', 'shortDescription': 'Original upstream skills', 'developerName': 'Upstream',
                          'category': 'Developer Tools', 'websiteURL': 'https://example.invalid/kit'}})
        self.assertEqualInOrder(self.built_json(release / '.codex-plugin/upstream.json'), {
            'repository': 'https://example.invalid/kit', 'commit': commit, 'subdirectory': '', 'version': version,
            'selectedSkillRoots': ['./skills/'], 'upstreamFileHashes': fixture_hashes(files),
            'packagingOnlyFiles': ['.codex-plugin/plugin.json', 'skills/one/agents/openai.yaml']})

    def test_a_course_recipe_builds_prefixed_trimmed_skills_with_their_metadata(self):
        files = {
            'LICENSE': 'License.\n',
            'skills/alpha/SKILL.md': '---\nname: alpha\ndescription: "Alpha work. Triggers on: alpha"\n---\nBody.\n',
            'skills/beta/SKILL.md': '---\nname: beta\ndescription: "Beta work. Triggers on: beta"\n---\nBody.\n---\nMore.\n'}
        commit = self.commit(files, later={
            'skills/alpha/SKILL.md': '---\nname: alpha\ndescription: "Alpha work. Triggers on: alpha"\n---\nLater.\n'})
        recipe = CourseRecipe(URL, commit, ('skills/alpha', 'skills/beta'), 'course-', ('course-alpha',))
        source = BuildSource(recipe, 3, 'Course', metadata={
            'url': 'https://example.invalid/course', 'description': 'Course skills.', 'repository': 'example/course',
            'collection': 'course', 'branch': 'main',
            'skillMetadata': {'course-alpha': {'normalizations': ['trimmed'], 'allowImplicitInvocation': True},
                              'course-beta': {'normalizations': []}}})
        release = self.build(source)
        version = f'0.1.0+upstream.{commit[:12]}.codex3'
        self.assertEqual(release.name, version)
        self.assertIn(release, self.held)
        skills = release / 'skills'
        self.assertEqual(sorted(p.name for p in skills.iterdir()), ['course-alpha', 'course-beta'])
        self.assertEqual({name: [read_utf8(skills / name / file) for file in ('SKILL.md', 'agents/openai.yaml')]
                          for name in ('course-alpha', 'course-beta')}, {
            'course-alpha': ['---\nname: course-alpha\ndescription: Alpha work.\n---\nBody.\n',
                             'interface:\n  default_prompt: Use $course-alpha for this task.\n'
                             'policy:\n  allow_implicit_invocation: true\n'],
            'course-beta': ["---\nname: course-beta\ndescription: 'Beta work. Triggers on: beta'\n---\nBody.\n---\nMore.\n",
                            'interface:\n  default_prompt: Use $course-beta for this task.\n']})
        self.assertEqual(read_utf8(release / 'LICENSE'), 'License.\n')
        interface = {'displayName': 'Course', 'shortDescription': 'Course skills.', 'developerName': 'example/course',
                     'category': 'Developer Tools', 'websiteURL': 'https://example.invalid/course'}
        self.assertEqualInOrder(self.built_json(release / 'plugin.json'), {
            '$schema': SCHEMA, 'name': 'kit', 'version': version, 'description': 'Course skills.',
            'repository': 'https://example.invalid/course', 'extensions': {'com.openai': {'interface': interface}}})
        self.assertEqualInOrder(self.built_json(release / '.codex-plugin/plugin.json'), {
            'name': 'kit', 'version': version, 'description': 'Course skills.', 'repository': 'https://example.invalid/course',
            'skills': './skills/', 'interface': interface})
        self.assertEqualInOrder(self.built_json(release / '.codex-plugin/upstream.json'), {
            'repository': 'https://example.invalid/course', 'branch': 'main', 'commit': commit, 'version': version,
            'selectedSkills': [
                {'sourcePath': 'skills/alpha', 'sourceName': 'alpha', 'installedName': 'course-alpha',
                 'normalizations': ['trimmed'], 'allowImplicitInvocation': True},
                {'sourcePath': 'skills/beta', 'sourceName': 'beta', 'installedName': 'course-beta', 'normalizations': []}],
            'upstreamFileHashes': {'course-alpha': fixture_hashes(files, 'skills/alpha/'),
                                   'course-beta': fixture_hashes(files, 'skills/beta/')},
            'compatibilityChangedFiles': {'course-alpha': ['SKILL.md', 'agents/openai.yaml'],
                                          'course-beta': ['SKILL.md', 'agents/openai.yaml']},
            'packagedFileHashes': self.packaged(release, [
                '.codex-plugin/plugin.json', 'LICENSE', 'plugin.json', 'skills/course-alpha/.course-source.json',
                'skills/course-alpha/SKILL.md', 'skills/course-alpha/agents/openai.yaml',
                'skills/course-beta/.course-source.json', 'skills/course-beta/SKILL.md',
                'skills/course-beta/agents/openai.yaml'])})
        for name, normalizations in (('alpha', ['trimmed']), ('beta', [])):
            self.assertEqualInOrder(self.built_json(skills / f'course-{name}/.course-source.json'), {
                'repo': 'example/course', 'commit': commit, 'collection': 'course', 'sourcePath': f'skills/{name}',
                'sourceName': name, 'installedName': f'course-{name}', 'normalizations': normalizations,
                'sourceFiles': fixture_hashes(files, f'skills/{name}/')})
        self.assertRebuildKeepsTheRelease(source, release)

    def test_a_course_recipe_applies_its_replacements_display_name_prefix_and_provenance_file(self):
        files = {'skills/alpha/SKILL.md': '---\nname: alpha\ndescription: Alpha work.\n---\nRead old/guide.md.\n',
                 'skills/alpha/guide.md': 'See old/guide.md.\n', 'skills/alpha/data.txt': 'old/guide.md\n'}
        commit = self.commit(files, later={'skills/alpha/guide.md': 'Later.\n'})
        recipe = CourseRecipe(URL, commit, ('skills/alpha',), 'course-')
        release = self.build(BuildSource(recipe, 1, 'Course', metadata={
            'url': 'https://example.invalid/course', 'description': 'Course skills.', 'repository': 'example/course',
            'collection': 'course', 'branch': 'main', 'markdownReplacements': [['old/', 'new/']],
            'skillDisplayNamePrefix': 'Course: ', 'provenanceFile': '.untangle-source.json',
            'skillMetadata': {'course-alpha': {'normalizations': []}}}))
        skill = release / 'skills/course-alpha'
        self.assertEqual([read_utf8(skill / name) for name in ('SKILL.md', 'guide.md', 'data.txt')],
                         ['---\nname: course-alpha\ndescription: Alpha work.\n---\nRead new/guide.md.\n',
                          'See new/guide.md.\n', 'old/guide.md\n'])
        self.assertEqual(read_utf8(skill / 'agents/openai.yaml'),
                         "interface:\n  display_name: 'Course: alpha'\n  default_prompt: Use $course-alpha for this task.\n")
        self.assertFalse((skill / '.course-source.json').exists())
        self.assertFields(self.built_json(skill / '.untangle-source.json'), {'sourceFiles': fixture_hashes(files, 'skills/alpha/')})
        self.assertFields(self.built_json(release / '.codex-plugin/upstream.json'), {
            'upstreamFileHashes': {'course-alpha': fixture_hashes(files, 'skills/alpha/')},
            'compatibilityChangedFiles': {'course-alpha': ['SKILL.md', 'agents/openai.yaml', 'guide.md']},
            'packagedFileHashes': self.packaged(release, [
                '.codex-plugin/plugin.json', 'plugin.json', 'skills/course-alpha/.untangle-source.json',
                'skills/course-alpha/SKILL.md', 'skills/course-alpha/agents/openai.yaml', 'skills/course-alpha/data.txt',
                'skills/course-alpha/guide.md'])})

    def test_a_public_emil_recipe_builds_renamed_licensed_skills_with_their_metadata(self):
        files = {'LICENSE': 'MIT License.\n', 'skills/motion/SKILL.md':
                 '---\nname: motion\ndescription: Motion.\ndisable-model-invocation: true\n---\nBody.\n---\nMore.\n'}
        commit = self.commit(files, later={'skills/motion/SKILL.md': '---\nname: motion\ndescription: Motion.\n---\nLater.\n'})
        sidecar = 'policy:\n  allow_implicit_invocation: false\n'
        recipe = PublicEmilRecipe(URL, commit, ('skills/motion',), {'motion': 'emil-motion'})
        source = BuildSource(recipe, 2, 'Motion', metadata={
            'url': 'https://example.invalid/emil', 'description': 'Motion skills.', 'branch': 'main',
            'skillMetadata': {'emil-motion': {'removeDisableModelInvocation': True, 'openaiYaml': sidecar,
                                              'legacyProvenance': True}}})
        release = self.build(source)
        version = f'0.1.0+upstream.{commit[:12]}.codex2'
        self.assertEqual(release.name, version)
        self.assertIn(release, self.held)
        skill = release / 'skills/emil-motion'
        self.assertEqual([read_utf8(skill / name) for name in ('SKILL.md', 'LICENSE.txt', 'agents/openai.yaml')],
                         ['---\nname: emil-motion\ndescription: Motion.\n---\nBody.\n---\nMore.\n', 'MIT License.\n', sidecar])
        self.assertEqual(read_utf8(release / 'LICENSE'), 'MIT License.\n')
        self.assertEqualInOrder(self.built_json(skill / '.emil-upstream.json'), {
            'source': 'https://example.invalid/emil.git', 'ref': 'main', 'commit': commit, 'sourceDirectory': 'skills/motion',
            'licenseSource': 'LICENSE', 'sourceName': 'motion', 'localName': 'emil-motion',
            'normalizations': {'removedFrontmatter': ['disable-model-invocation'], 'codexInvocationPolicy': 'explicit-only'},
            'files': ['LICENSE.txt', 'SKILL.md', 'agents/openai.yaml']})
        interface = {'displayName': 'Motion', 'shortDescription': 'Motion skills.',
                     'developerName': 'Emil Kowalski (upstream); personal Codex packaging', 'category': 'Developer Tools',
                     'websiteURL': 'https://example.invalid/emil'}
        self.assertEqualInOrder(self.built_json(release / 'plugin.json'), {
            '$schema': SCHEMA, 'name': 'kit', 'version': version, 'description': 'Motion skills.',
            'repository': 'https://example.invalid/emil', 'license': 'MIT', 'extensions': {'com.openai': {'interface': interface}}})
        self.assertEqualInOrder(self.built_json(release / '.codex-plugin/plugin.json'), {
            'name': 'kit', 'version': version, 'description': 'Motion skills.', 'repository': 'https://example.invalid/emil',
            'license': 'MIT', 'skills': './skills/', 'interface': interface})
        self.assertEqualInOrder(self.built_json(release / '.codex-plugin/upstream.json'), {
            'repository': 'https://example.invalid/emil', 'branch': 'main', 'commit': commit, 'version': version,
            'selectedSkills': [{'sourcePath': 'skills/motion', 'sourceName': 'motion', 'installedName': 'emil-motion',
                                'removeDisableModelInvocation': True, 'openaiYaml': sidecar, 'legacyProvenance': True}],
            'upstreamFileHashes': {'emil-motion': fixture_hashes(files, 'skills/motion/')},
            'compatibilityChangedFiles': {'emil-motion': ['.emil-upstream.json', 'LICENSE.txt', 'SKILL.md', 'agents/openai.yaml']},
            'packagedFileHashes': self.packaged(release, [
                '.codex-plugin/plugin.json', 'LICENSE', 'plugin.json', 'skills/emil-motion/.emil-upstream.json',
                'skills/emil-motion/LICENSE.txt', 'skills/emil-motion/SKILL.md', 'skills/emil-motion/agents/openai.yaml'])})
        self.assertRebuildKeepsTheRelease(source, release)

    def test_a_public_emil_skill_keeps_its_upstream_files_and_refuses_other_upstream_invocation_metadata(self):
        sidecar = 'policy:\n  allow_implicit_invocation: false\n'
        for shipped, refused in ((sidecar, False), ('policy:\n  allow_implicit_invocation: true\n', True)):
            with self.subTest(refused=refused):
                self.upstream = self.root / f'upstream-{refused}'
                commit = self.commit({'LICENSE': 'MIT License.\n', 'skills/motion/agents/openai.yaml': shipped,
                                      'skills/motion/notes.md': 'Notes.\n',
                                      'skills/motion/SKILL.md': '---\nname: motion\ndescription: Motion.\n---\nBody.\n'},
                                     later={})
                source = BuildSource(PublicEmilRecipe(URL, commit, ('skills/motion',), {'motion': 'motion'}), 2, 'Motion',
                                     metadata={'url': 'https://example.invalid/emil', 'description': 'Motion skills.',
                                               'branch': 'main', 'skillMetadata': {'motion': {'openaiYaml': sidecar}}})
                if refused:
                    with self.assertRaisesRegex(RuntimeError, '^Upstream added conflicting invocation metadata: motion$'):
                        self.build(source)
                else:
                    skill = self.build(source) / 'skills/motion'
                    self.assertEqual([read_utf8(skill / name) for name in ('agents/openai.yaml', 'notes.md')],
                                     [sidecar, 'Notes.\n'])

    def test_a_snapshot_recipe_builds_its_selected_skills_with_its_metadata(self):
        stored = self.snapshot()
        self.assertEqual(stored.name, 'f6ddf80e68af7e3d7e5ee7b90869911afd514d8d4a641f3f571878122c8b0d0b',
                         'composed_package.py recomputes a snapshot\'s name from its files and refuses one that differs, '
                         'so the same files must get the same name on every machine')
        recipe = SnapshotRecipe('sha256:' + stored.name, {'one': None})
        source = BuildSource(recipe, 7, 'Snap', metadata={
            'description': 'Snapshot skills.', 'sourceKind': 'personal', 'repository': 'example/snap'})
        release = self.build(source)
        version = f'0.1.0+snapshot.{stored.name[:12]}.personal7'
        self.assertEqual(release.name, version)
        self.assertEqual(self.held, [stored, release])
        self.assertEqual(read_utf8(release / 'skills/one/SKILL.md'), snapshot_skill('one'))
        self.assertEqualInOrder(self.built_json(release / '.codex-plugin/plugin.json'), {
            'name': 'kit', 'version': version, 'description': 'Snapshot skills.', 'author': {'name': 'Personal packaging'},
            'skills': './skills/',
            'interface': {'displayName': 'Snap', 'shortDescription': 'Snapshot skills.', 'developerName': 'example/snap',
                          'category': 'Developer Tools', 'websiteURL': 'https://github.com/example/snap'},
            'repository': 'https://github.com/example/snap'})
        self.assertEqualInOrder(self.built_json(release / '.codex-plugin/upstream.json'), {
            'repository': 'https://github.com/example/snap', 'commit': None, 'sourceKind': 'personal', 'version': version,
            'selectedSkills': ['one'], 'selectedSources': {},
            'sourceSnapshot': str(self.root / 'engine/standalone-snapshots/kit' / stored.name),
            'snapshotSha256': stored.name, 'sourceFileHashes': {'one/SKILL.md': sha(snapshot_skill('one'))},
            'portabilityChanges': [],
            'packagedFileHashes': self.packaged(release, ['.codex-plugin/plugin.json', 'skills/one/SKILL.md'])})
        self.assertRebuildKeepsTheRelease(source, release)

    def test_a_claude_collection_takes_its_packaging_and_description_from_the_recipe_metadata(self):
        stored = self.snapshot()
        source = BuildSource(SnapshotRecipe('sha256:' + stored.name, {'one': None}), 1, 'Snap', metadata={
            'description': 'Snapshot skills.', 'sourceKind': 'personal', 'claudePackaging': 3,
            'claudeDescriptions': {'kit': 'Claude snapshot skills.'}})
        package = Package('kit', Visibility.PRIVATE, '', source, {}, ToolNotes.AUDIT,
                          CodexPlacement((EngineInstall(False),)), ClaudePlacement((EngineInstall(True),)))
        builder = self.builder(package)
        codex = builder.codex(package, verify=False)
        [claude] = builder.claude(package, codex, verify=False).values()
        version = f'0.1.0+snapshot.{stored.name[:12]}.personal1.claude3'
        self.assertEqual(claude.version, version)
        self.assertIn(claude.root, self.held)
        self.assertEqual(read_utf8(claude.root / 'skills/one/SKILL.md'), snapshot_skill('one'))
        self.assertEqualInOrder(self.built_json(claude.root / '.claude-plugin/plugin.json'), {
            'name': 'kit', 'version': version, 'description': 'Claude snapshot skills.',
            'author': {'name': 'Personal packaging; content from Personal local files'},
            'repository': 'Personal local files', 'skills': './skills/'})
        self.assertEqualInOrder(self.built_json(claude.root / '.claude-plugin/upstream.json'), {
            'collection': 'kit', 'repository': 'Personal local files', 'commit': None, 'version': version,
            'codexRelease': str(codex.root), 'selectedSkills': ['one'],
            'sourceFileHashes': {'one': {'SKILL.md': sha(snapshot_skill('one'))}}, 'explicitInvocationHeadersAdded': [],
            'instructionBodiesPreserved': True, 'supportingFilesPreserved': True,
            'invocationContract': {'one': {'disable-model-invocation': False, 'user-invocable': True, 'allowed-tools': None}},
            'packagedFileHashes': self.packaged(claude.root, ['.claude-plugin/plugin.json', 'skills/one/SKILL.md'])})

    def test_a_claude_collection_edited_after_it_was_built_is_refused_rather_than_replaced(self):
        stored = self.snapshot()
        source = BuildSource(SnapshotRecipe('sha256:' + stored.name, {'one': None}), 1, 'Snap', metadata={
            'description': 'Snapshot skills.', 'sourceKind': 'personal', 'claudePackaging': 1,
            'claudeDescriptions': {'kit': 'Claude snapshot skills.'}})
        package = Package('kit', Visibility.PRIVATE, '', source, {}, ToolNotes.AUDIT,
                          CodexPlacement((EngineInstall(False),)), ClaudePlacement((EngineInstall(True),)))
        builder = self.builder(package)
        codex = builder.codex(package, verify=False)
        [claude] = builder.claude(package, codex, verify=False).values()
        self.assertEqual(list(builder.claude(package, codex, verify=False).values()), [claude])
        (claude.root / '.claude-plugin/plugin.json').write_text('{}\n')
        with self.assertRaisesRegex(RuntimeError, f'^Immutable Claude release differs: {re.escape(str(claude.root))}$'):
            builder.claude(package, codex, verify=False)

    def test_a_claude_collection_split_in_two_builds_each_plugin_at_the_default_packaging(self):
        stored = self.snapshot('one', 'two')
        source = BuildSource(SnapshotRecipe('sha256:' + stored.name, {'one': None, 'two': None}), 1, 'Snap', metadata={
            'description': 'Snapshot skills.', 'sourceKind': 'personal',
            'claudeDescriptions': {'kit': 'Claude one.', 'kit-extra': 'Claude two.'}})
        package = Package('kit', Visibility.PRIVATE, '', source, {}, ToolNotes.AUDIT, CodexPlacement((EngineInstall(False),)),
                          ClaudePlacement((EngineInstall(True, 'kit-extra', ('two',)), EngineInstall(True, skills=('one',)))))
        builder = self.builder(package)
        codex = builder.codex(package, verify=False)
        built = builder.claude(package, codex, verify=False)
        self.assertEqual(sorted(built), ['kit-extra@personal-collections', 'kit@personal-collections'])
        version = f'0.1.0+snapshot.{stored.name[:12]}.personal1.claude1'
        for name, description, skill in (('kit', 'Claude one.', 'one'), ('kit-extra', 'Claude two.', 'two')):
            with self.subTest(name):
                root = built[f'{name}@personal-collections'].root
                self.assertFields(self.built_json(root / '.claude-plugin/plugin.json'),
                                  {'name': name, 'version': version, 'description': description})
                self.assertFields(self.built_json(root / '.claude-plugin/upstream.json'), {
                    'collection': 'kit', 'version': version, 'codexRelease': str(codex.root), 'selectedSkills': [skill],
                    'sourceFileHashes': {skill: {'SKILL.md': sha(snapshot_skill(skill))}},
                    'invocationContract': {skill: {'disable-model-invocation': False, 'user-invocable': True,
                                                   'allowed-tools': None}},
                    'packagedFileHashes': self.packaged(root, ['.claude-plugin/plugin.json', f'skills/{skill}/SKILL.md'])})

    def test_a_recipe_without_its_build_metadata_names_the_migration(self):
        commit = self.commit({'skills/one/SKILL.md': '---\nname: one\ndescription: One.\n---\nBody.\n'}, later={})
        with self.assertRaisesRegex(BuildError, '^kit: missing build metadata; migrate its recipe first$'):
            self.build(BuildSource(CourseRecipe(URL, commit, ('skills/one',), ''), 1, 'Kit'))
        (self.root / 'private/setup/snapshots' / ('a' * 64)).mkdir(parents=True)
        for metadata in ({'description': 'D'}, {'sourceKind': 'personal'}):
            with self.subTest(metadata), \
                    self.assertRaisesRegex(BuildError, '^kit: missing snapshot build metadata; migrate its recipe first$'):
                self.build(BuildSource(SnapshotRecipe('sha256:' + 'a' * 64, {}), 1, 'Kit', metadata=metadata))

    def test_a_repository_source_without_a_path_builds_the_folder_its_marketplace_lists_for_the_plugin(self):
        skill = '---\nname: {0}\ndescription: {0}.\n---\nBody.\n'
        commit = self.commit({
            '.claude-plugin/marketplace.json': json.dumps({'plugins': [
                {'name': 'kit', 'source': './plugins/kit'},
                {'name': 'other', 'source': {'source': 'local', 'path': './plugins/other'}}]}),
            'plugins/kit/.claude-plugin/plugin.json': json.dumps({'name': 'kit', 'version': '1.0.0'}),
            'plugins/kit/skills/one/SKILL.md': skill.format('one'),
            'plugins/other/.claude-plugin/plugin.json': json.dumps({'name': 'other', 'version': '2.0.0'}),
            'plugins/other/skills/two/SKILL.md': skill.format('two')}, {})
        repos = {'upstream': Repo('upstream', Visibility.PUBLIC, '', URL, commit, None, None)}
        for name, kept in (('kit', 'one'), ('other', 'two')):
            with self.subTest(name=name):
                package = Package(name, Visibility.PUBLIC, '', RepoSource('upstream'), {}, ToolNotes.AUDIT,
                                  CodexPlacement((EngineInstall(False),)), None)
                root = self.builder(package, repos=repos).codex(package, verify=False).root
                self.assertEqual([p.name for p in (root / 'skills').iterdir()], [kept])

    def test_an_owned_source_edited_after_it_was_built_is_refused_rather_than_built_from_its_old_copy(self):
        owned = self.root / 'public/owned/kit'
        self.write(owned, {'.codex-plugin/plugin.json': json.dumps({'version': '1.0.0'}),
                           'skills/one/SKILL.md': '---\nname: one\ndescription: One.\n---\nBody.\n'})
        source = LocalSource('owned/kit', DeviceRoots.current(self.root).tree_digest(owned))
        built = self.build(source)
        self.assertEqual(read_utf8(built / 'skills/one/SKILL.md'), '---\nname: one\ndescription: One.\n---\nBody.\n')
        self.write(owned, {'skills/one/SKILL.md': '---\nname: one\ndescription: One.\n---\nEdited.\n'})
        with self.assertRaisesRegex(BuildError, 'Owned source owned/kit does not match its declared tree'):
            self.build(source)

    def test_a_snapshot_recipe_without_its_private_repository_or_its_snapshot_is_refused(self):
        source = BuildSource(SnapshotRecipe('sha256:' + 'b' * 64, {}), 1, 'Kit',
                             metadata={'description': 'D', 'sourceKind': 'personal'})
        with self.assertRaisesRegex(BuildError, '^kit: its source snapshot needs the private repository$'):
            self.build(source, private=False)
        with self.assertRaisesRegex(BuildError, f'^kit: missing source snapshot sha256:{"b" * 64}$'):
            self.build(source)

    def test_a_recipe_of_no_known_kind_names_the_package_and_its_type(self):
        with self.assertRaisesRegex(BuildError, '^kit: no builder for RetiredRecipe$'):
            self.build(BuildSource(RetiredRecipe(URL, 'a' * 40), 1, 'Kit', metadata={'url': 'https://example.invalid/kit'}))


if __name__ == '__main__':
    unittest.main()
