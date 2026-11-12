"""Builder.codex and Builder.claude send each recipe type to its own builder. Each test compares the whole text of the
receipt and plugin manifests the build writes, so every field and its order count, and each upstream recipe is built
at a commit that is not its checkout's HEAD."""
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from description import (BuildSource, ClaudePlacement, CodexPlacement, CourseRecipe, EngineInstall,  # noqa: E402
                         GenericRecipe, Package, PublicEmilRecipe, Roots, Setup, SnapshotRecipe, ToolNotes, Visibility)
from host_state import DeviceRoots  # noqa: E402
from portable import GIT  # noqa: E402
from sync_sources import Builder, BuildError, Sources  # noqa: E402

URL = 'https://example.invalid/upstream.git'
# Fixed identity, dates and no user or system config, so a fixture's commit, and every hash of a file that names it,
# is the same on every machine.
COMMITTER = {'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid', 'GIT_AUTHOR_DATE': '2026-01-01T00:00:00Z',
             'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid',
             'GIT_COMMITTER_DATE': '2026-01-01T00:00:00Z', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}


def git(repository, *arguments):
    return subprocess.run([*GIT, '-C', str(repository), *arguments], check=True, capture_output=True, text=True,
                          env={**os.environ, **COMMITTER}).stdout.strip()


def read(path):
    return json.loads(path.read_text())


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

    def assertText(self, path, expected, **values):
        """Each value is one the fixture cannot know in advance, written in the expected text as {name}."""
        text = path.read_bytes().decode()
        for name, value in values.items():
            text = text.replace(json.dumps(str(value))[1:-1], '{' + name + '}')
        self.assertEqual(text, expected)

    def builder(self, package):
        setup = Setup(Roots(self.root / 'public/setup', self.root / 'private/setup'), {'kit': package}, {}, {}, {},
                      None, (), frozenset(), {})
        roots = replace(DeviceRoots.current(self.root), engine=self.root / 'engine')
        return Builder(setup, roots, roots.engine, sources=Sources(self.root / 'sources', seeds=(self.upstream,)))

    def build(self, source):
        package = Package('kit', Visibility.PUBLIC, '', source, {}, ToolNotes.AUDIT,
                          CodexPlacement((EngineInstall(False),)), None)
        return self.builder(package).codex(package, verify=False).root

    def snapshot(self, *skills):
        from standalone_package import snapshot
        for skill in skills or ('one',):
            self.write(self.root / 'input', {f'{skill}/SKILL.md': f'---\nname: {skill}\ndescription: {skill.title()}.\n---\nBody.\n'})
        return snapshot(self.root / 'input', {'commit': None}, self.root / 'private/setup/snapshots')

    def test_a_generic_recipe_builds_its_subdirectory_manifest_and_selection(self):
        commit = self.commit({
            'nested/.cursor-plugin/plugin.json': json.dumps({'version': '2.0.0', 'author': {'name': 'Upstream'}}),
            'nested/skills/keep/SKILL.md': '---\nname: keep\ndescription: Kept.\n---\nBody.\n',
            'nested/skills/drop/SKILL.md': '---\nname: drop\ndescription: Dropped.\n---\nBody.\n'}, later={
            'nested/.cursor-plugin/plugin.json': json.dumps({'version': '3.0.0', 'author': {'name': 'Later'}}),
            'nested/skills/keep/SKILL.md': '---\nname: keep\ndescription: Kept.\n---\nLater.\n'})
        recipe = GenericRecipe(URL, commit, 'nested', '.cursor-plugin/plugin.json', ('./skills/keep',))
        release = self.build(BuildSource(recipe, 5, 'Kit', metadata={'url': 'https://example.invalid/kit'}))
        self.assertEqual(release.name, f'2.0.0+upstream.{commit[:12]}.codex5')
        manifest = read(release / '.codex-plugin/plugin.json')
        self.assertEqual((manifest['interface']['displayName'], manifest['interface']['websiteURL']),
                         ('Kit', 'https://example.invalid/kit'))
        receipt = read(release / '.codex-plugin/upstream.json')
        self.assertEqual((receipt['subdirectory'], receipt['selectedSkillRoots'], receipt['omittedUpstreamFiles']),
                         ('nested', ['./skills/keep'], ['skills/drop/SKILL.md']))
        self.assertText(release / '.codex-plugin/plugin.json', '''\
{
  "author": {
    "name": "Upstream"
  },
  "name": "kit",
  "version": "2.0.0+upstream.774004acb52b.codex5",
  "skills": [
    "./skills/"
  ],
  "interface": {
    "displayName": "Kit",
    "shortDescription": "Original upstream skills",
    "developerName": "Upstream",
    "category": "Developer Tools",
    "websiteURL": "https://example.invalid/kit"
  }
}
''')
        self.assertText(release / '.codex-plugin/upstream.json', '''\
{
  "repository": "https://example.invalid/kit",
  "commit": "774004acb52b02b0c1ab0e6fc8356aba8b69dbc5",
  "subdirectory": "nested",
  "version": "2.0.0+upstream.774004acb52b.codex5",
  "selectedSkillRoots": [
    "./skills/keep"
  ],
  "upstreamFileHashes": {
    ".cursor-plugin/plugin.json": "02bd484aa647305c5e4cb33314a55a338d6a82aa388bc59fa425ccc3c83dfadc",
    "skills/drop/SKILL.md": "e797eb2b1909e1c5af06ca60c5fd089d96c1af96c2c7bd86c540f8fec5f9f59b",
    "skills/keep/SKILL.md": "c222562bd864ffdc59db5cc7c9d6927d31dc91278e9e0265d9e033775950d28a"
  },
  "packagingOnlyFiles": [
    ".codex-plugin/plugin.json"
  ],
  "omittedUpstreamFiles": [
    "skills/drop/SKILL.md"
  ]
}
''')

    def test_a_generic_recipe_without_a_subdirectory_or_selection_builds_every_skill_and_copies_the_manifest(self):
        commit = self.commit({
            '.cursor-plugin/plugin.json': json.dumps({
                'name': 'upstream-kit', 'version': '1.2.0', 'description': 'Upstream kit.', 'author': {'name': 'Upstream'},
                'homepage': 'https://example.invalid/home', 'license': 'MIT', 'keywords': ['kit']}),
            'skills/one/SKILL.md': '---\nname: one\ndescription: One.\ndisable-model-invocation: true\n---\nBody.\n',
            'skills/two/SKILL.md': '---\nname: two\ndescription: Two.\n---\nBody.\n'}, later={
            'skills/three/SKILL.md': '---\nname: three\ndescription: Three.\n---\nBody.\n'})
        recipe = GenericRecipe(URL, commit, None, '.cursor-plugin/plugin.json', ())
        release = self.build(BuildSource(recipe, 1, 'Kit', metadata={'url': 'https://example.invalid/kit'}))
        self.assertEqual(sorted(p.name for p in (release / 'skills').iterdir()), ['one', 'two'])
        self.assertText(release / '.codex-plugin/plugin.json', '''\
{
  "description": "Upstream kit.",
  "author": {
    "name": "Upstream"
  },
  "homepage": "https://example.invalid/home",
  "license": "MIT",
  "keywords": [
    "kit"
  ],
  "name": "kit",
  "version": "1.2.0+upstream.27908f4e7ae4.codex1",
  "skills": [
    "./skills/"
  ],
  "interface": {
    "displayName": "Kit",
    "shortDescription": "Original upstream skills",
    "developerName": "Upstream",
    "category": "Developer Tools",
    "websiteURL": "https://example.invalid/kit"
  }
}
''')
        self.assertText(release / '.codex-plugin/upstream.json', '''\
{
  "repository": "https://example.invalid/kit",
  "commit": "27908f4e7ae4fc279239b838e4c585c2747222b0",
  "subdirectory": "",
  "version": "1.2.0+upstream.27908f4e7ae4.codex1",
  "selectedSkillRoots": [
    "./skills/"
  ],
  "upstreamFileHashes": {
    ".cursor-plugin/plugin.json": "2fc4470b559899cfdf6e0b56d98eaace3d780fc9446e6dc8274b8edf4aee631a",
    "skills/one/SKILL.md": "76fdba72a13a12dee8f9a9327676c8f81345013f42b8dde4855113338f8afb0f",
    "skills/two/SKILL.md": "c6d36d87650bd724860e8de744b1692dd1d69beb1ae973f51e06c72df0e61e4d"
  },
  "packagingOnlyFiles": [
    ".codex-plugin/plugin.json",
    "skills/one/agents/openai.yaml"
  ]
}
''')

    def test_a_course_recipe_builds_prefixed_trimmed_skills_with_their_metadata(self):
        commit = self.commit({
            'LICENSE': 'License.\n',
            'skills/alpha/SKILL.md': '---\nname: alpha\ndescription: "Alpha work. Triggers on: alpha"\n---\nBody.\n',
            'skills/beta/SKILL.md': '---\nname: beta\ndescription: "Beta work. Triggers on: beta"\n---\nBody.\n'}, later={
            'skills/alpha/SKILL.md': '---\nname: alpha\ndescription: "Alpha work. Triggers on: alpha"\n---\nLater.\n'})
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
        self.assertText(release / 'plugin.json', '''\
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "kit",
  "version": "0.1.0+upstream.17ce30aa6cf9.codex3",
  "description": "Course skills.",
  "repository": "https://example.invalid/course",
  "extensions": {
    "com.openai": {
      "interface": {
        "displayName": "Course",
        "shortDescription": "Course skills.",
        "developerName": "example/course",
        "category": "Developer Tools",
        "websiteURL": "https://example.invalid/course"
      }
    }
  }
}
''')
        self.assertText(release / '.codex-plugin/plugin.json', '''\
{
  "name": "kit",
  "version": "0.1.0+upstream.17ce30aa6cf9.codex3",
  "description": "Course skills.",
  "repository": "https://example.invalid/course",
  "skills": "./skills/",
  "interface": {
    "displayName": "Course",
    "shortDescription": "Course skills.",
    "developerName": "example/course",
    "category": "Developer Tools",
    "websiteURL": "https://example.invalid/course"
  }
}
''')
        self.assertText(release / '.codex-plugin/upstream.json', '''\
{
  "repository": "https://example.invalid/course",
  "branch": "main",
  "commit": "17ce30aa6cf9bf0d8e02b95ad4acafae1e85dc4b",
  "version": "0.1.0+upstream.17ce30aa6cf9.codex3",
  "selectedSkills": [
    {
      "sourcePath": "skills/alpha",
      "sourceName": "alpha",
      "installedName": "course-alpha",
      "normalizations": [
        "trimmed"
      ],
      "allowImplicitInvocation": true
    },
    {
      "sourcePath": "skills/beta",
      "sourceName": "beta",
      "installedName": "course-beta",
      "normalizations": []
    }
  ],
  "upstreamFileHashes": {
    "course-alpha": {
      "SKILL.md": "7aec8e84bdc27eafe39688d2499416238eecacab6641c5d20fdf8fee3d74dcaa"
    },
    "course-beta": {
      "SKILL.md": "061b92b7538af86dabaa486057060a1ab017418a3c83ecf93e64986105adeb7d"
    }
  },
  "compatibilityChangedFiles": {
    "course-alpha": [
      "SKILL.md",
      "agents/openai.yaml"
    ],
    "course-beta": [
      "SKILL.md",
      "agents/openai.yaml"
    ]
  },
  "packagedFileHashes": {
    ".codex-plugin/plugin.json": "91ba895e532f5a0edfc136fb020cfc695afca7516eff90adb35589cdeef395e8",
    "LICENSE": "b4e18e9cacee1924815525f824ed5eeb98778a4fbecac79ee79e9c92f480defc",
    "plugin.json": "af7f94b382c4bf17ff203852f1045fd56e400ed0883ecaf7af9569e86d695d80",
    "skills/course-alpha/.course-source.json": "4329edc1779f2675afd1a9d3e159206d7c1b16121f6ab5077e3525d878f0d760",
    "skills/course-alpha/SKILL.md": "65ffe0f81e6f6b85aad254b604236b2d496121a9b39159163a408b9670a3ccf6",
    "skills/course-alpha/agents/openai.yaml": "e7faa81d7fb0008e73246daf553e3750481bf05df0cba6ff3a39249d1c39b3da",
    "skills/course-beta/.course-source.json": "1274d0dea417c329a76c9f8f6eff0bf0628fea5ea7e1339c16b4ceed08235dd6",
    "skills/course-beta/SKILL.md": "15a1fc1bd74cc65a92c068bc28268322abeaa4af2e3411a83364b9e19d10fb07",
    "skills/course-beta/agents/openai.yaml": "691a6870d47cea8b0d8c73e0e27de653bdf302168b9d288b050d029a90ed7d1e"
  }
}
''')
        self.assertText(release / 'skills/course-alpha/.course-source.json', '''\
{
  "repo": "example/course",
  "commit": "17ce30aa6cf9bf0d8e02b95ad4acafae1e85dc4b",
  "collection": "course",
  "sourcePath": "skills/alpha",
  "sourceName": "alpha",
  "installedName": "course-alpha",
  "normalizations": [
    "trimmed"
  ],
  "sourceFiles": {
    "SKILL.md": "7aec8e84bdc27eafe39688d2499416238eecacab6641c5d20fdf8fee3d74dcaa"
  }
}
''')

    def test_a_course_recipe_applies_its_replacements_display_name_prefix_and_provenance_file(self):
        commit = self.commit({
            'skills/alpha/SKILL.md': '---\nname: alpha\ndescription: Alpha work.\n---\nRead old/guide.md.\n',
            'skills/alpha/guide.md': 'See old/guide.md.\n', 'skills/alpha/data.txt': 'old/guide.md\n'}, later={
            'skills/alpha/guide.md': 'Later.\n'})
        recipe = CourseRecipe(URL, commit, ('skills/alpha',), 'course-')
        release = self.build(BuildSource(recipe, 1, 'Course', metadata={
            'url': 'https://example.invalid/course', 'description': 'Course skills.', 'repository': 'example/course',
            'collection': 'course', 'branch': 'main', 'markdownReplacements': [['old/', 'new/']],
            'skillDisplayNamePrefix': 'Course: ', 'provenanceFile': '.untangle-source.json',
            'skillMetadata': {'course-alpha': {'normalizations': []}}}))
        skill = release / 'skills/course-alpha'
        self.assertEqual(((skill / 'guide.md').read_text(), (skill / 'data.txt').read_text()),
                         ('See new/guide.md.\n', 'old/guide.md\n'))
        self.assertEqual(yaml.safe_load((skill / 'agents/openai.yaml').read_text())['interface']['display_name'],
                         'Course: alpha')
        self.assertText(skill / '.untangle-source.json', '''\
{
  "repo": "example/course",
  "commit": "c3e0701baa4a08d6ffe57cf31c785f5cbfd727c1",
  "collection": "course",
  "sourcePath": "skills/alpha",
  "sourceName": "alpha",
  "installedName": "course-alpha",
  "normalizations": [],
  "sourceFiles": {
    "SKILL.md": "f5fd3b075ae48e3236775c07e1e3b722e6a8f09feddb9ba20a7c4caf0f156326",
    "data.txt": "239497b2f75183ebdcef353cbdaaa077595405bad1273965b70d56e8fcb22000",
    "guide.md": "e1d384c5b83a6aa8ebb1a58a656f1f0b21c7754616474e974e7770adbc85f105"
  }
}
''')
        self.assertText(release / '.codex-plugin/upstream.json', '''\
{
  "repository": "https://example.invalid/course",
  "branch": "main",
  "commit": "c3e0701baa4a08d6ffe57cf31c785f5cbfd727c1",
  "version": "0.1.0+upstream.c3e0701baa4a.codex1",
  "selectedSkills": [
    {
      "sourcePath": "skills/alpha",
      "sourceName": "alpha",
      "installedName": "course-alpha",
      "normalizations": []
    }
  ],
  "upstreamFileHashes": {
    "course-alpha": {
      "SKILL.md": "f5fd3b075ae48e3236775c07e1e3b722e6a8f09feddb9ba20a7c4caf0f156326",
      "data.txt": "239497b2f75183ebdcef353cbdaaa077595405bad1273965b70d56e8fcb22000",
      "guide.md": "e1d384c5b83a6aa8ebb1a58a656f1f0b21c7754616474e974e7770adbc85f105"
    }
  },
  "compatibilityChangedFiles": {
    "course-alpha": [
      "SKILL.md",
      "agents/openai.yaml",
      "guide.md"
    ]
  },
  "packagedFileHashes": {
    ".codex-plugin/plugin.json": "4d5c458c15b8a88c2c2fa1657cacfd75031613b52da89f8f65c12d241bdd9ddd",
    "plugin.json": "73fc69eca4081a8504cc36b2ee40409e181098c40533804eb27c69982e596b49",
    "skills/course-alpha/.untangle-source.json": "9216bb9cecffb2f0fb1d60b176ff28538a1293ec0cf471beed14655c6985512a",
    "skills/course-alpha/SKILL.md": "3d8d3c9368729742364ad2dab8b4a55540962e025298eb6d2979d353563c75a8",
    "skills/course-alpha/agents/openai.yaml": "a3bd3079ed93f905c22fdd4b41ad210948886bbbe0a1f09e0f5671f809706e85",
    "skills/course-alpha/data.txt": "239497b2f75183ebdcef353cbdaaa077595405bad1273965b70d56e8fcb22000",
    "skills/course-alpha/guide.md": "6b561ccf745cf3b20b71169929dcebfd5712575d9c807262a4d9417137876908"
  }
}
''')

    def test_a_public_emil_recipe_builds_renamed_licensed_skills_with_their_metadata(self):
        commit = self.commit({
            'LICENSE': 'MIT License.\n',
            'skills/motion/SKILL.md': '---\nname: motion\ndescription: Motion.\ndisable-model-invocation: true\n---\nBody.\n'},
            later={'skills/motion/SKILL.md': '---\nname: motion\ndescription: Motion.\n---\nLater.\n'})
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
        self.assertText(release / 'plugin.json', '''\
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "kit",
  "version": "0.1.0+upstream.a4d715364f61.codex2",
  "description": "Motion skills.",
  "repository": "https://example.invalid/emil",
  "license": "MIT",
  "extensions": {
    "com.openai": {
      "interface": {
        "displayName": "Motion",
        "shortDescription": "Motion skills.",
        "developerName": "Emil Kowalski (upstream); personal Codex packaging",
        "category": "Developer Tools",
        "websiteURL": "https://example.invalid/emil"
      }
    }
  }
}
''')
        self.assertText(release / '.codex-plugin/plugin.json', '''\
{
  "name": "kit",
  "version": "0.1.0+upstream.a4d715364f61.codex2",
  "description": "Motion skills.",
  "repository": "https://example.invalid/emil",
  "license": "MIT",
  "skills": "./skills/",
  "interface": {
    "displayName": "Motion",
    "shortDescription": "Motion skills.",
    "developerName": "Emil Kowalski (upstream); personal Codex packaging",
    "category": "Developer Tools",
    "websiteURL": "https://example.invalid/emil"
  }
}
''')
        self.assertText(release / '.codex-plugin/upstream.json', '''\
{
  "repository": "https://example.invalid/emil",
  "branch": "main",
  "commit": "a4d715364f61d7b8af3d99631a1d6dda66966021",
  "version": "0.1.0+upstream.a4d715364f61.codex2",
  "selectedSkills": [
    {
      "sourcePath": "skills/motion",
      "sourceName": "motion",
      "installedName": "emil-motion",
      "removeDisableModelInvocation": true,
      "openaiYaml": "policy:\\n  allow_implicit_invocation: false\\n",
      "legacyProvenance": true
    }
  ],
  "upstreamFileHashes": {
    "emil-motion": {
      "SKILL.md": "ac44d4072b4d1e28da4f2580b24511350a40d3878a72711d44daa9cb9be59ace"
    }
  },
  "compatibilityChangedFiles": {
    "emil-motion": [
      ".emil-upstream.json",
      "LICENSE.txt",
      "SKILL.md",
      "agents/openai.yaml"
    ]
  },
  "packagedFileHashes": {
    ".codex-plugin/plugin.json": "dc55f3eee565609a12ce8c9b15792c9d4eea9087cccf6036c281055945936feb",
    "LICENSE": "a87cb046863d7850bd6d389fa5a0bb1e8fc7edd9cf4baa1d96e478049bc421aa",
    "plugin.json": "0d34f9aa4d34bf77f4113014e7c5473793685c1d7dd1479c14675574de34da27",
    "skills/emil-motion/.emil-upstream.json": "f8411c157f1ddcfdf0f8b4f6262279b29ccde2c6f2d7dbfb389b54134de27fdf",
    "skills/emil-motion/LICENSE.txt": "a87cb046863d7850bd6d389fa5a0bb1e8fc7edd9cf4baa1d96e478049bc421aa",
    "skills/emil-motion/SKILL.md": "5ecfcd19e3e5596a14125b56105d8de4b1732b629bd5915790b0597d131b6f01",
    "skills/emil-motion/agents/openai.yaml": "a1499d95abd8447558c535fe5554adcc3c9b988a0a39264a6283d430effe1e94"
  }
}
''')
        self.assertText(skill / '.emil-upstream.json', '''\
{
  "source": "https://example.invalid/emil.git",
  "ref": "main",
  "commit": "a4d715364f61d7b8af3d99631a1d6dda66966021",
  "sourceDirectory": "skills/motion",
  "licenseSource": "LICENSE",
  "sourceName": "motion",
  "localName": "emil-motion",
  "normalizations": {
    "removedFrontmatter": [
      "disable-model-invocation"
    ],
    "codexInvocationPolicy": "explicit-only"
  },
  "files": [
    "LICENSE.txt",
    "SKILL.md",
    "agents/openai.yaml"
  ]
}
''')

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
        self.assertText(release / '.codex-plugin/plugin.json', '''\
{
  "name": "kit",
  "version": "0.1.0+snapshot.f6ddf80e68af.personal7",
  "description": "Snapshot skills.",
  "author": {
    "name": "Personal packaging"
  },
  "skills": "./skills/",
  "interface": {
    "displayName": "Snap",
    "shortDescription": "Snapshot skills.",
    "developerName": "example/snap",
    "category": "Developer Tools",
    "websiteURL": "https://github.com/example/snap"
  },
  "repository": "https://github.com/example/snap"
}
''')
        self.assertText(release / '.codex-plugin/upstream.json', '''\
{
  "repository": "https://github.com/example/snap",
  "commit": null,
  "sourceKind": "personal",
  "version": "0.1.0+snapshot.f6ddf80e68af.personal7",
  "selectedSkills": [
    "one"
  ],
  "selectedSources": {},
  "sourceSnapshot": "{snapshot}",
  "snapshotSha256": "f6ddf80e68af7e3d7e5ee7b90869911afd514d8d4a641f3f571878122c8b0d0b",
  "sourceFileHashes": {
    "one/SKILL.md": "93be8a328cadef422f448b384da9958ac2eed32f45f41016303b28dbc81aac86"
  },
  "portabilityChanges": [],
  "packagedFileHashes": {
    ".codex-plugin/plugin.json": "1f3b4959e1850133139285c1a42289433466d6876f5bb725243ea47d229b26d2",
    "skills/one/SKILL.md": "93be8a328cadef422f448b384da9958ac2eed32f45f41016303b28dbc81aac86"
  }
}
''',
                        snapshot=self.root / 'engine/standalone-snapshots/kit' / stored.name)

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
        self.assertEqual(claude.version, f'0.1.0+snapshot.{stored.name[:12]}.personal1.claude3')
        manifest = read(claude.root / '.claude-plugin/plugin.json')
        self.assertEqual((manifest['name'], manifest['description']), ('kit', 'Claude snapshot skills.'))
        self.assertEqual(read(claude.root / '.claude-plugin/upstream.json')['selectedSkills'], ['one'])
        self.assertText(claude.root / '.claude-plugin/plugin.json', '''\
{
  "name": "kit",
  "version": "0.1.0+snapshot.f6ddf80e68af.personal1.claude3",
  "description": "Claude snapshot skills.",
  "author": {
    "name": "Personal packaging; content from Personal local files"
  },
  "repository": "Personal local files",
  "skills": "./skills/"
}
''')
        self.assertText(claude.root / '.claude-plugin/upstream.json', '''\
{
  "collection": "kit",
  "repository": "Personal local files",
  "commit": null,
  "version": "0.1.0+snapshot.f6ddf80e68af.personal1.claude3",
  "codexRelease": "{codex}",
  "selectedSkills": [
    "one"
  ],
  "sourceFileHashes": {
    "one": {
      "SKILL.md": "93be8a328cadef422f448b384da9958ac2eed32f45f41016303b28dbc81aac86"
    }
  },
  "explicitInvocationHeadersAdded": [],
  "instructionBodiesPreserved": true,
  "supportingFilesPreserved": true,
  "invocationContract": {
    "one": {
      "disable-model-invocation": false,
      "user-invocable": true,
      "allowed-tools": null
    }
  },
  "packagedFileHashes": {
    ".claude-plugin/plugin.json": "4069bfb22298e24da3053bdb1faf62ab8ce1f93b1ada1689d856b651b45121ec",
    "skills/one/SKILL.md": "93be8a328cadef422f448b384da9958ac2eed32f45f41016303b28dbc81aac86"
  }
}
''', codex=codex.root)

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
        extra, main = built['kit-extra@personal-collections'].root, built['kit@personal-collections'].root
        self.assertText(main / '.claude-plugin/plugin.json', '''\
{
  "name": "kit",
  "version": "0.1.0+snapshot.a9735dba9def.personal1.claude1",
  "description": "Claude one.",
  "author": {
    "name": "Personal packaging; content from Personal local files"
  },
  "repository": "Personal local files",
  "skills": "./skills/"
}
''')
        self.assertText(main / '.claude-plugin/upstream.json', '''\
{
  "collection": "kit",
  "repository": "Personal local files",
  "commit": null,
  "version": "0.1.0+snapshot.a9735dba9def.personal1.claude1",
  "codexRelease": "{codex}",
  "selectedSkills": [
    "one"
  ],
  "sourceFileHashes": {
    "one": {
      "SKILL.md": "93be8a328cadef422f448b384da9958ac2eed32f45f41016303b28dbc81aac86"
    }
  },
  "explicitInvocationHeadersAdded": [],
  "instructionBodiesPreserved": true,
  "supportingFilesPreserved": true,
  "invocationContract": {
    "one": {
      "disable-model-invocation": false,
      "user-invocable": true,
      "allowed-tools": null
    }
  },
  "packagedFileHashes": {
    ".claude-plugin/plugin.json": "8b0d7122eacfaa8f559543e83655c0cbe7aa95d1a8209b604c774aa21541ca81",
    "skills/one/SKILL.md": "93be8a328cadef422f448b384da9958ac2eed32f45f41016303b28dbc81aac86"
  }
}
''', codex=codex.root)
        self.assertText(extra / '.claude-plugin/plugin.json', '''\
{
  "name": "kit-extra",
  "version": "0.1.0+snapshot.a9735dba9def.personal1.claude1",
  "description": "Claude two.",
  "author": {
    "name": "Personal packaging; content from Personal local files"
  },
  "repository": "Personal local files",
  "skills": "./skills/"
}
''')
        self.assertText(extra / '.claude-plugin/upstream.json', '''\
{
  "collection": "kit",
  "repository": "Personal local files",
  "commit": null,
  "version": "0.1.0+snapshot.a9735dba9def.personal1.claude1",
  "codexRelease": "{codex}",
  "selectedSkills": [
    "two"
  ],
  "sourceFileHashes": {
    "two": {
      "SKILL.md": "c6d36d87650bd724860e8de744b1692dd1d69beb1ae973f51e06c72df0e61e4d"
    }
  },
  "explicitInvocationHeadersAdded": [],
  "instructionBodiesPreserved": true,
  "supportingFilesPreserved": true,
  "invocationContract": {
    "two": {
      "disable-model-invocation": false,
      "user-invocable": true,
      "allowed-tools": null
    }
  },
  "packagedFileHashes": {
    ".claude-plugin/plugin.json": "4e704b71077e8f82174e3956afe00e67f6ad97d79612e17f3c2b9f69a0e278b7",
    "skills/two/SKILL.md": "c6d36d87650bd724860e8de744b1692dd1d69beb1ae973f51e06c72df0e61e4d"
  }
}
''', codex=codex.root)

    def test_a_recipe_without_its_build_metadata_names_the_migration(self):
        commit = self.commit({'skills/one/SKILL.md': '---\nname: one\ndescription: One.\n---\nBody.\n'}, later={})
        with self.assertRaisesRegex(BuildError, '^kit: missing build metadata; migrate its recipe first$'):
            self.build(BuildSource(CourseRecipe(URL, commit, ('skills/one',), ''), 1, 'Kit'))
        (self.root / 'private/setup/snapshots' / ('a' * 64)).mkdir(parents=True)
        for metadata in ({'description': 'D'}, {'sourceKind': 'personal'}):
            with self.subTest(metadata), \
                    self.assertRaisesRegex(BuildError, '^kit: missing snapshot build metadata; migrate its recipe first$'):
                self.build(BuildSource(SnapshotRecipe('sha256:' + 'a' * 64, {}), 1, 'Kit', metadata=metadata))

    def test_a_recipe_of_no_known_kind_names_the_package_and_its_type(self):
        with self.assertRaisesRegex(BuildError, '^kit: no builder for RetiredRecipe$'):
            self.build(BuildSource(RetiredRecipe(URL, 'a' * 40), 1, 'Kit', metadata={'url': 'https://example.invalid/kit'}))


if __name__ == '__main__':
    unittest.main()
