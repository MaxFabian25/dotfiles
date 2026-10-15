import argparse
import base64
import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import standalone_package as sp
from verify_candidate import validate


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + '\n')


class StandalonePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.root = self.home / 'packages'
        self.root.mkdir(parents=True)
        patcher = patch.object(sp, 'ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.definition = {'displayName': 'Sample', 'description': 'Selected sample skill.', 'sourceKind': 'github',
                           'repository': 'example/skills', 'packagingRevision': 1,
                           'skills': {'workflow': {'sourcePath': 'skills/workflow'}}}
        self.body = b'---\nname: workflow\ndescription: Sample\nallowed-tools: Read\n---\nOriginal source.\n'
        self.metadata = 'policy:\n  allow_implicit_invocation: false\n'
        source = self.root / 'input/workflow'
        source.mkdir(parents=True)
        (source / 'SKILL.md').write_bytes(self.body)
        (source / 'agents').mkdir()
        (source / 'agents/openai.yaml').write_text(self.metadata)
        self.snapshot = sp.snapshot(source.parent, {'commit': None, 'selectedSources': {}}, self.root / 'snapshots')
        self.release, _ = sp.build_snapshot('sample', self.definition, self.snapshot)

    def test_same_content_with_new_provenance_has_a_distinct_snapshot(self):
        first = sp.snapshot(self.root / 'input', {'commit': 'old'}, self.root / 'snapshots')
        second = sp.snapshot(self.root / 'input', {'commit': 'new'}, self.root / 'snapshots')
        self.assertNotEqual(first, second)
        self.assertEqual(sp.read(second / 'source.json')['commit'], 'new')

    def test_incoming_source_cannot_override_user_invocation_metadata(self):
        text = self.body.replace(b'Original source.', b'Updated source.')
        upstream_policy = b'policy:\n  allow_implicit_invocation: true\n'
        data = {sp.blob_hash(text): text, sp.blob_hash(upstream_policy): upstream_policy}
        sources = {'workflow': {'repository': 'example/skills', 'tree': 'new-tree',
                   'blobHashes': {'SKILL.md': sp.blob_hash(text), 'agents/openai.yaml': sp.blob_hash(upstream_policy)},
                   'modes': {'SKILL.md': '100644', 'agents/openai.yaml': '100644'}}}
        def api(_repo, endpoint):
            return {'encoding': 'base64', 'content': base64.b64encode(data[endpoint.split('/')[-1]]).decode()}
        with patch.object(sp, 'api', side_effect=api):
            candidate, _ = sp.prepare_candidate('sample', self.definition, self.release, 'new', sources)
        self.assertEqual((candidate / 'skills/workflow/SKILL.md').read_bytes(), text)
        self.assertEqual((candidate / 'skills/workflow/agents/openai.yaml').read_text(), self.metadata)
        self.assertTrue(validate(self.release, candidate)['invocationPreserved'])

    def follow_upstream_candidate(self, upstream_sidecar):
        definition = {**self.definition, 'metadataFollowsUpstream': True}
        data = {sp.blob_hash(self.body): self.body}
        blobs = {'SKILL.md': sp.blob_hash(self.body)}
        if upstream_sidecar is not None:
            data[sp.blob_hash(upstream_sidecar)] = upstream_sidecar
            blobs['agents/openai.yaml'] = sp.blob_hash(upstream_sidecar)
        sources = {'workflow': {'repository': 'example/skills', 'tree': 'same-tree', 'blobHashes': blobs,
                                'modes': {path: '100644' for path in blobs}}}
        def api(_repo, endpoint):
            return {'encoding': 'base64', 'content': base64.b64encode(data[endpoint.split('/')[-1]]).decode()}
        with patch.object(sp, 'api', side_effect=api):
            candidate, _ = sp.prepare_candidate('sample', definition, self.release, 'same', sources)
        return candidate

    def test_follow_upstream_collection_uses_the_upstream_sidecar(self):
        upstream = b'interface:\n  display_name: "Workflow"\n'
        candidate = self.follow_upstream_candidate(upstream)
        self.assertEqual((candidate / 'skills/workflow/agents/openai.yaml').read_bytes(), upstream)
        self.assertEqual((candidate / 'skills/workflow/SKILL.md').read_bytes(), self.body)
        with self.assertRaises(ValueError):
            validate(self.release, candidate)
        result = validate(self.release, candidate, upstream_invocation_skills={'workflow'})
        self.assertEqual(result['invocationFollowsUpstream'], ['workflow'])

    def test_follow_upstream_collection_drops_a_local_only_sidecar(self):
        candidate = self.follow_upstream_candidate(None)
        self.assertFalse((candidate / 'skills/workflow/agents/openai.yaml').exists())
        self.assertEqual(self.follow_upstream_candidate(None), candidate)

    def test_disabled_codex_publication_and_selective_rollback(self):
        config = self.home / '.codex/config.toml'
        config.parent.mkdir(parents=True)
        config.write_text('[plugins."sample@personal"]\nenabled = false\n')
        market = self.home / '.agents/plugins/marketplace.json'
        write_json(market, {'name': 'personal', 'plugins': [{'name': 'unrelated', 'source': {'path': './unchanged'}}]})
        before = config.read_bytes()
        with patch.object(Path, 'home', return_value=self.home):
            transaction = sp.publish_codex('sample', self.release, self.root / 'backup')
            self.assertEqual(config.read_bytes(), before)
            current = sp.read(market)
            current['plugins'].append({'name': 'concurrent', 'source': {'path': './new'}})
            write_json(market, current)
            sp.restore_codex(transaction)
        self.assertEqual({x['name'] for x in sp.read(market)['plugins']}, {'unrelated', 'concurrent'})
        self.assertEqual(config.read_bytes(), before)

    def test_edited_source_snapshot_is_never_silently_rebuilt(self):
        (self.snapshot / 'skills/workflow/SKILL.md').write_text('User changed the workflow')
        with self.assertRaisesRegex(RuntimeError, 'differs from its receipt'):
            sp.build_snapshot('sample', self.definition, self.snapshot)

    def test_updated_cache_has_one_active_version_and_rollback_restores_previous(self):
        config = self.home / '.codex/config.toml'
        config.parent.mkdir(parents=True)
        config.write_text('[plugins."sample@personal"]\nenabled = false\n')
        market = self.home / '.agents/plugins/marketplace.json'
        write_json(market, {'name': 'personal', 'plugins': []})
        with patch.object(Path, 'home', return_value=self.home):
            sp.publish_codex('sample', self.release, self.root / 'first-backup')
            original = self.home / '.codex/plugins/cache/personal/sample' / self.release.name
            (self.root / 'input/workflow/SKILL.md').write_bytes(self.body.replace(b'Original', b'Updated'))
            source = sp.snapshot(self.root / 'input', {'commit': 'new'}, self.root / 'snapshots')
            candidate, _ = sp.build_snapshot('sample', self.definition, source)
            transaction = sp.publish_codex('sample', candidate, self.root / 'second-backup')
            cache = original.parent / candidate.name
            self.assertTrue(cache.is_dir())
            self.assertFalse(original.exists())
            sp.restore_codex(transaction)
            self.assertTrue(original.is_dir())
            self.assertFalse(cache.exists())

    def test_retired_source_never_uses_a_current_upstream_replacement(self):
        definition = dict(self.definition, sourceKind='retired-source')
        write_json(self.root / 'standalone-packages.json', {'collections': {'sample': definition}})
        args = argparse.Namespace(plugin='sample', check=True)
        with patch.object(sp, 'verify_installed', return_value=(self.release, {})), \
             patch.object(sp, 'selected_sources', side_effect=AssertionError('Retired source must not be replaced')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            sp.update(args)
        self.assertFalse(json.loads(output.getvalue())['updateAvailable'])

    def discovery_api(self, names, truncated=False):
        tree = [{'path': 'skills/workflow/SKILL.md', 'type': 'blob', 'mode': '100644', 'sha': sp.blob_hash(self.body)}]
        blobs = {}
        for folder, name in names:
            content = ('---\nname: ' + name + '\ndescription: Newly published workflow\n---\nNew source.\n').encode()
            sha = sp.blob_hash(content)
            blobs[sha] = content
            tree.append({'path': 'skills/' + folder + '/SKILL.md', 'type': 'blob', 'mode': '100644', 'sha': sha})
        def api(_repository, endpoint):
            if endpoint == 'git/trees/new-commit?recursive=1':
                return {'tree': tree, 'truncated': truncated}
            data = blobs[endpoint.removeprefix('git/blobs/')]
            return {'encoding': 'base64', 'content': base64.b64encode(data).decode()}
        return api

    def test_discovery_uses_upstream_names_and_preserves_existing_selection(self):
        definition = dict(self.definition, autoDiscoverSkills={'root': 'skills'})
        with patch.object(sp, 'api', side_effect=self.discovery_api([('new-directory', 'new-workflow')])):
            result = sp.discover_definition(definition, 'new-commit')
        self.assertEqual(result['skills'], {'workflow': {'sourcePath': 'skills/workflow'},
                                           'new-workflow': {'sourcePath': 'skills/new-directory'}})
        self.assertEqual(set(definition['skills']), {'workflow'})

    def test_new_upstream_skill_is_reported_by_the_update_check(self):
        definition = dict(self.definition, autoDiscoverSkills={'root': 'skills'})
        write_json(self.root / 'standalone-packages.json', {'collections': {'sample': definition}})
        previous = {'selectedSources': {'workflow': {'tree': 'unchanged'}}}
        args = argparse.Namespace(plugin='sample', check=True)
        def selected(definition, commit):
            self.assertEqual(set(definition['skills']), {'workflow', 'new-workflow'})
            return commit, {'workflow': {'tree': 'unchanged'}, 'new-workflow': {'tree': 'new'}}
        with patch.object(sp, 'verify_installed', return_value=(self.release, previous)), \
             patch.object(sp, 'upstream_revision', return_value='new-commit'), \
             patch.object(sp, 'api', side_effect=self.discovery_api([('new-directory', 'new-workflow')])), \
             patch.object(sp, 'selected_sources', side_effect=selected), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            sp.update(args)
        result = json.loads(output.getvalue())
        self.assertTrue(result['updateAvailable'])
        self.assertEqual(result['newSkills'], ['new-workflow'])

    def test_other_collections_keep_their_explicit_selection(self):
        with patch.object(sp, 'api', side_effect=AssertionError('No repository-wide discovery')):
            result = sp.discover_definition(self.definition, 'new-commit')
        self.assertEqual(result, self.definition)

    def test_added_skills_extend_the_one_claude_package_or_skip_a_collection_claude_aligns(self):
        config = {'collections': {'kit': [{'name': 'kit', 'skills': ['a']}], 'split': [{'skills': ['a']}, {'skills': ['b']}]}}
        extended = sp.claude_additions(config, 'kit', ['c'])
        self.assertEqual(extended['collections']['kit'][0]['skills'], ['a', 'c'])
        self.assertEqual(config['collections']['kit'][0]['skills'], ['a'])
        self.assertIsNone(sp.claude_additions(config, 'aligned-only', ['c']))
        self.assertIsNone(sp.claude_additions(config, 'kit', []))
        with self.assertRaisesRegex(RuntimeError, 'one existing Claude package'):
            sp.claude_additions(config, 'split', ['c'])

    def test_discovery_rejects_name_collisions(self):
        definition = dict(self.definition, autoDiscoverSkills={'root': 'skills'})
        with patch.object(sp, 'api', side_effect=self.discovery_api([('different-folder', 'workflow')])):
            with self.assertRaisesRegex(RuntimeError, 'collides'):
                sp.discover_definition(definition, 'new-commit')

    def test_discovery_rejects_incomplete_and_removed_sources(self):
        definition = dict(self.definition, autoDiscoverSkills={'root': 'skills'})
        with patch.object(sp, 'api', side_effect=self.discovery_api([], truncated=True)):
            with self.assertRaisesRegex(RuntimeError, 'Incomplete'):
                sp.discover_definition(definition, 'new-commit')
        with patch.object(sp, 'api', return_value={'tree': [], 'truncated': False}):
            with self.assertRaisesRegex(RuntimeError, 'removed or moved'):
                sp.discover_definition(definition, 'new-commit')

    def test_duplicate_must_be_disabled_before_publication(self):
        definition = dict(self.definition, duplicateSkills={'workflow': {'duplicateOf': 'publisher:workflow'}})
        config = self.home / '.codex/config.toml'
        config.parent.mkdir(parents=True)
        config.write_text('[[skills.config]]\nname = "sample:workflow"\nenabled = true\n')
        with patch.object(Path, 'home', return_value=self.home):
            with self.assertRaisesRegex(RuntimeError, 'Disable the duplicate'):
                sp.require_disabled_duplicates('sample', definition)
            config.write_text('[[skills.config]]\nname = "sample:workflow"\nenabled = false\n')
            sp.require_disabled_duplicates('sample', definition)

    def test_source_record_updates_preserve_other_collections(self):
        path = self.root / 'standalone-packages.json'
        write_json(path, {'collections': {'sample': {'skills': ['old']}, 'unrelated': {'skills': ['keep']}}})
        sp.replace_record(path, 'collections', 'sample', {'skills': ['old']}, {'skills': ['old', 'new']})
        self.assertEqual(sp.read(path)['collections']['unrelated'], {'skills': ['keep']})
        with self.assertRaisesRegex(RuntimeError, 'Concurrent source record change'):
            sp.replace_record(path, 'collections', 'sample', {'skills': ['old']}, {'skills': []})


if __name__ == '__main__':
    unittest.main()
