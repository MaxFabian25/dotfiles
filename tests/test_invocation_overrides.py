import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('invocation_overrides', Path(__file__).resolve().parents[1] / 'invocation_overrides.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class InvocationOverridesTest(unittest.TestCase):
    def test_managed_system_skill_is_discovered_and_replayed_without_changing_body(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            root = home / '.codex/skills/.system/review-agent'
            root.mkdir(parents=True)
            original = '---\nname: review-agent\ndescription: Review code changes.\n---\nRead-only; do not modify code.\n'
            (root / 'SKILL.md').write_text(original)
            (root / 'agents').mkdir()
            (root / 'agents/openai.yaml').write_text('policy:\n  allow_implicit_invocation: false\n')
            registry = {'targets': {'Codex': {'review-agent@system': ['review-agent']}, 'Claude': {}},
                        'standaloneRoots': {'Codex': {'review-agent@system': str(root)}}}
            with patch.object(module, 'HOME', home), patch.object(module.subprocess, 'check_output', return_value='{"installed": []}'):
                installs = module.installations(registry)
                writes, counts = module.plan(registry, installs)
                self.assertEqual(counts, {'Codex': 1, 'Claude': 0})
                overlay = module.Overlay(home / 'recovery')
                self.assertEqual(overlay.apply(writes), 1)
                self.assertEqual((root / 'SKILL.md').read_text(), original)
                self.assertEqual(module.installations(registry, ['unrelated-plugin']), [])
                overlay.restore(installs)
                self.assertIn('false', (root / 'agents/openai.yaml').read_text())
                (root / 'SKILL.md').unlink()
                self.assertEqual(module.installations(registry), [])

    def test_standalone_override_rejects_paths_outside_the_skill_root(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            registry = {'targets': {'Codex': {'x@system': ['x']}, 'Claude': {}},
                        'standaloneRoots': {'Codex': {'x@system': str(home / 'other')}}}
            with patch.object(module, 'HOME', home), patch.object(module.subprocess, 'check_output', return_value='{"installed": []}'):
                with self.assertRaises(ValueError):
                    module.installations(registry)

    def test_only_requested_frontmatter_changes(self):
        original = '---\nname: sample\ndescription: >-\n  A task.\ndisable-model-invocation: true\nallowed-tools: [Read, Write]\nuser-invocable: true\n---\n\nAsk for approval before publishing.\n'
        result = module.transform_skill(original, 'plugin:sample', {})
        self.assertEqual(result, original.replace('disable-model-invocation: true', 'disable-model-invocation: false'))
        self.assertEqual(module.transform_skill(result, 'plugin:sample', {}), result)

    def test_description_override_handles_folded_yaml_without_changing_body(self):
        source = '---\nname: sample\ndescription: >-\n  Only by name.\nallowed-tools: [Read]\n---\nOnly publish with permission.\n'
        registry = {'descriptionReplacements': {'p:sample': {'from': 'Only by name.', 'to': 'Use for the task.'}}}
        changed = module.transform_skill(source, 'p:sample', registry)
        _, front = module.frontmatter(changed)
        self.assertEqual(front['description'], 'Use for the task.')
        self.assertEqual(front['allowed-tools'], ['Read'])
        self.assertTrue(changed.endswith('Only publish with permission.\n'))

    def test_changed_explicit_description_requires_review(self):
        source = '---\nname: sample\ndescription: Only explicitly invoke.\n---\nBody\n'
        registry = {'descriptionReplacements': {'p:sample': {'from': 'Only by name.', 'to': 'Use for the task.'}}}
        with self.assertRaises(ValueError):
            module.transform_skill(source, 'p:sample', registry)

    def test_sidecar_preserves_other_policy_and_interface_fields(self):
        source = 'interface:\n  display_name: Sample\npolicy:\n  products: [codex]\n  allow_implicit_invocation: false # user-selected\n'
        changed = module.transform_policy(source)
        self.assertEqual(changed, source.replace('false #', 'true #'))
        self.assertEqual(module.transform_policy(changed), changed)
        self.assertEqual(module.transform_policy(None), 'policy:\n  allow_implicit_invocation: true\n')

    def test_receipt_restores_original_bytes_and_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'plugin'
            root.mkdir()
            path = root / 'SKILL.md'
            path.write_text('before\n')
            overlay = module.Overlay(Path(directory) / 'state')
            writes = [(path, 'before\n', 'after\n', 'Codex', 'p@m')]
            self.assertEqual(overlay.apply(writes), 1)
            self.assertEqual(overlay.restore([('Codex', 'p@m', root)]), 1)
            self.assertEqual(path.read_text(), 'before\n')
            self.assertEqual(overlay.apply(writes), 1)
            self.assertEqual(overlay.apply([(path, 'after\n', 'after\n', 'Codex', 'p@m')]), 0)
            self.assertEqual(overlay.state['files'][str(path)]['base'], 'before\n')

    def test_refuses_unrecognized_local_changes_and_preserves_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'plugin'
            root.mkdir()
            path = root / 'SKILL.md'
            path.write_text('base')
            overlay = module.Overlay(Path(directory) / 'state')
            overlay.apply([(path, 'base', 'effective', 'Claude', 'p@m')])
            path.write_text('manual edit')
            with self.assertRaises(ValueError):
                overlay.restore([('Claude', 'p@m', root)])
            with self.assertRaises(ValueError):
                overlay.apply([(path, 'manual edit', 'effective', 'Claude', 'p@m')])
            self.assertEqual(path.read_text(), 'manual edit')

    def test_restore_removes_a_sidecar_created_by_the_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'plugin'
            root.mkdir()
            path = root / 'agents/openai.yaml'
            overlay = module.Overlay(Path(directory) / 'state')
            overlay.apply([(path, None, 'policy:\n  allow_implicit_invocation: true\n', 'Codex', 'p@m')])
            overlay.restore([('Codex', 'p@m', root)])
            self.assertFalse(path.exists())

    def test_adaptation_merges_upstream_edits_and_stops_on_conflicts(self):
        base = 'one\ntwo\nthree\nfour\nfive\n'
        adapted = 'one\nTWO\nthree\nfour\nfive\n'
        self.assertEqual(module.merge_adaptation(base, base, adapted, 'x'), adapted)
        self.assertEqual(module.merge_adaptation(adapted, base, adapted, 'x'), adapted)
        upstream = 'one\ntwo\nthree\nfour\nFIVE\n'
        self.assertEqual(module.merge_adaptation(upstream, base, adapted, 'x'), 'one\nTWO\nthree\nfour\nFIVE\n')
        with self.assertRaises(ValueError):
            module.merge_adaptation('one\n2\nthree\nfour\nfive\n', base, adapted, 'x')
        self.assertEqual(module.merge_adaptation(None, None, 'new\n', 'x'), 'new\n')
        with self.assertRaises(ValueError):
            module.merge_adaptation('upstream added\n', None, 'new\n', 'x')

    def test_adaptation_and_invocation_overlays_compose_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            repository = directory / 'adaptation'
            skill = repository / 'skills/sample/SKILL.md'
            skill.parent.mkdir(parents=True)
            original = '---\nname: sample\ndescription: Use for samples.\ndisable-model-invocation: true\n---\nSpawn a Task with readonly: true.\n'
            skill.write_text(original)
            git = ['git', '-C', str(repository), '-c', 'user.name=t', '-c', 'user.email=t@t']
            module.subprocess.run([*git[:3], 'init', '-q', '-b', 'main'], check=True)
            module.subprocess.run([*git, 'add', '-A'], check=True)
            module.subprocess.run([*git, 'commit', '-qm', 'upstream'], check=True)
            module.subprocess.run([*git, 'branch', 'upstream'], check=True)
            adapted_body = 'Spawn a read-only agent per harnesses.md.\n'
            skill.write_text(original.replace('Spawn a Task with readonly: true.\n', adapted_body))
            (repository / 'skills/sample/references').mkdir()
            (repository / 'skills/sample/references/harnesses.md').write_text('map\n')
            module.subprocess.run([*git, 'add', '-A'], check=True)
            module.subprocess.run([*git, 'commit', '-qm', 'adapt'], check=True)

            root = directory / 'cache/plugin/1.0'
            (root / 'skills/sample').mkdir(parents=True)
            (root / 'skills/sample/SKILL.md').write_text(original)
            adaptations = {'plugins': {'plugin': {'repository': str(repository), 'baseRef': 'upstream',
                                                   'adaptedRef': 'main', 'hosts': ['Codex']}}}
            registry = {'targets': {'Codex': {'plugin@m': ['sample']}, 'Claude': {}}}
            installs = [('Codex', 'plugin@m', root)]
            overlay = module.Overlay(directory / 'state')
            writes, _ = module.plan(registry, installs, overlay, adaptations)
            self.assertEqual(overlay.apply(writes), 3)
            effective = (root / 'skills/sample/SKILL.md').read_text()
            self.assertIn('disable-model-invocation: false', effective)
            self.assertTrue(effective.endswith(adapted_body))
            self.assertEqual((root / 'skills/sample/references/harnesses.md').read_text(), 'map\n')

            writes, _ = module.plan(registry, installs, overlay, adaptations)
            self.assertEqual(overlay.apply(writes), 0)

            self.assertEqual(overlay.restore(installs), 3)
            self.assertEqual((root / 'skills/sample/SKILL.md').read_text(), original)
            self.assertFalse((root / 'skills/sample/references/harnesses.md').exists())
            self.assertFalse((root / 'skills/sample/agents/openai.yaml').exists())

    def test_interrupted_transition_between_effective_versions_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'plugin'
            root.mkdir()
            first, second = root / 'a.md', root / 'b.md'
            first.write_text('base a\n')
            second.write_text('base b\n')
            overlay = module.Overlay(Path(directory) / 'state')
            overlay.apply([(first, 'base a\n', 'v1 a\n', 'Codex', 'p@m'), (second, 'base b\n', 'v1 b\n', 'Codex', 'p@m')])
            real_write = module.atomic_write
            calls = []

            def crash_after_first(path, value):
                if calls and path in (first, second):
                    raise KeyboardInterrupt
                if path in (first, second):
                    calls.append(path)
                real_write(path, value)

            with patch.object(module, 'atomic_write', crash_after_first):
                with self.assertRaises(KeyboardInterrupt):
                    overlay.apply([(first, 'v1 a\n', 'v2 a\n', 'Codex', 'p@m'), (second, 'v1 b\n', 'v2 b\n', 'Codex', 'p@m')])
            self.assertEqual((first.read_text(), second.read_text()), ('v2 a\n', 'v1 b\n'))

            reloaded = module.Overlay(Path(directory) / 'state')
            self.assertEqual(reloaded.pristine(second, 'v1 b\n'), 'base b\n')
            self.assertEqual(reloaded.apply([(second, 'v1 b\n', 'v2 b\n', 'Codex', 'p@m')]), 1)
            self.assertEqual(second.read_text(), 'v2 b\n')
            self.assertEqual(reloaded.restore([('Codex', 'p@m', root)]), 2)
            self.assertEqual((first.read_text(), second.read_text()), ('base a\n', 'base b\n'))

    def test_interrupted_transition_can_still_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'plugin'
            root.mkdir()
            path = root / 'a.md'
            path.write_text('base\n')
            overlay = module.Overlay(Path(directory) / 'state')
            overlay.apply([(path, 'base\n', 'v1\n', 'Codex', 'p@m')])
            record = overlay.state['files'][str(path)]
            record['previous'], record['effective'] = record['effective'], 'v2\n'
            self.assertEqual(overlay.restore([('Codex', 'p@m', root)]), 1)
            self.assertEqual(path.read_text(), 'base\n')

    def _adaptation_repository(self, directory):
        repository = directory / 'adaptation'
        (repository / 'docs').mkdir(parents=True)
        (repository / 'docs/a.md').write_text('one\ntwo\nthree\nfour\nfive\n')
        git = ['git', '-C', str(repository), '-c', 'user.name=t', '-c', 'user.email=t@t']
        module.subprocess.run([*git[:3], 'init', '-q', '-b', 'main'], check=True)
        module.subprocess.run([*git, 'add', '-A'], check=True)
        module.subprocess.run([*git, 'commit', '-qm', 'upstream 1'], check=True)
        module.subprocess.run([*git, 'branch', 'upstream'], check=True)
        (repository / 'docs/a.md').write_text('one\nTWO\nthree\nfour\nfive\n')
        module.subprocess.run([*git, 'commit', '-qam', 'adapt'], check=True)
        return repository, git

    def test_newer_upstream_on_base_ref_is_merged_not_reverted(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            repository, git = self._adaptation_repository(directory)
            module.subprocess.run([*git, 'checkout', '-q', 'upstream'], check=True)
            (repository / 'docs/a.md').write_text('one\ntwo\nthree\nfour\nFIVE\n')
            (repository / 'docs/new.md').write_text('added upstream\n')
            module.subprocess.run([*git, 'add', '-A'], check=True)
            module.subprocess.run([*git, 'commit', '-qm', 'upstream 2'], check=True)
            module.subprocess.run([*git, 'checkout', '-q', 'main'], check=True)
            root = directory / 'cache'
            (root / 'docs').mkdir(parents=True)
            (root / 'docs/a.md').write_text('one\ntwo\nthree\nfour\nFIVE\n')
            (root / 'docs/new.md').write_text('added upstream\n')
            adaptations = {'plugins': {'plugin': {'repository': str(repository), 'baseRef': 'upstream',
                                                   'adaptedRef': 'main', 'hosts': ['Codex']}}}
            registry = {'targets': {'Codex': {'plugin@m': []}, 'Claude': {}}}
            overlay = module.Overlay(directory / 'state')
            writes, _ = module.plan(registry, [('Codex', 'plugin@m', root)], overlay, adaptations)
            overlay.apply(writes)
            self.assertEqual((root / 'docs/a.md').read_text(), 'one\nTWO\nthree\nfour\nFIVE\n')
            self.assertEqual((root / 'docs/new.md').read_text(), 'added upstream\n')

    def test_retired_adaptation_returns_to_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            repository, git = self._adaptation_repository(directory)
            root = directory / 'cache'
            (root / 'docs').mkdir(parents=True)
            (root / 'docs/a.md').write_text('one\ntwo\nthree\nfour\nfive\n')
            adaptations = {'plugins': {'plugin': {'repository': str(repository), 'baseRef': 'upstream',
                                                   'adaptedRef': 'main', 'hosts': ['Codex']}}}
            registry = {'targets': {'Codex': {'plugin@m': []}, 'Claude': {}}}
            installs = [('Codex', 'plugin@m', root)]
            overlay = module.Overlay(directory / 'state')
            overlay.apply(module.plan(registry, installs, overlay, adaptations)[0])
            self.assertIn('TWO', (root / 'docs/a.md').read_text())
            module.subprocess.run([*git, 'revert', '--no-edit', 'HEAD'], check=True)
            self.assertEqual(overlay.apply(module.plan(registry, installs, overlay, adaptations)[0]), 1)
            self.assertEqual((root / 'docs/a.md').read_text(), 'one\ntwo\nthree\nfour\nfive\n')
            self.assertEqual(overlay.apply(module.plan(registry, installs, overlay, adaptations)[0]), 0)

    def test_adaptation_may_not_add_or_rename_a_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            repository, git = self._adaptation_repository(directory)
            skill = repository / 'skills/new/SKILL.md'
            skill.parent.mkdir(parents=True)
            skill.write_text('---\nname: new\ndescription: d\n---\nbody\n')
            module.subprocess.run([*git, 'add', '-A'], check=True)
            module.subprocess.run([*git, 'commit', '-qm', 'add skill'], check=True)
            adaptation = {'repository': str(repository), 'baseRef': 'upstream', 'adaptedRef': 'main', 'hosts': ['Codex']}
            with self.assertRaisesRegex(ValueError, 'add, remove or rename'):
                module.adaptation_files(adaptation)

    def test_adaptation_only_plugin_is_an_installation_and_identities_scope_it(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            inventory = '{"installed": [{"pluginId": "plugin@a", "name": "plugin", "marketplaceName": "a", "version": "1"},' \
                        ' {"pluginId": "plugin@b", "name": "plugin", "marketplaceName": "b", "version": "1"}]}'
            registry = {'targets': {'Codex': {}, 'Claude': {}}}
            adaptations = {'plugins': {'plugin': {'repository': '/nowhere', 'baseRef': 'upstream', 'adaptedRef': 'main',
                                                   'hosts': ['Codex'], 'identities': ['plugin@a']}}}
            with patch.object(module, 'HOME', home), patch.object(module.subprocess, 'check_output', return_value=inventory):
                found = module.installations(registry, adaptations=adaptations)
            self.assertEqual([(host, identity) for host, identity, _ in found], [('Codex', 'plugin@a')])

    def test_plan_without_adaptations_reads_no_registry_file(self):
        with patch.object(module, 'ADAPTATIONS', Path('/nonexistent/should-not-be-read.json')), \
             patch.object(module, 'read_json', side_effect=AssertionError('read a registry file')):
            self.assertEqual(module.plan({'targets': {'Codex': {}, 'Claude': {}}}, []), ([], {'Codex': 0, 'Claude': 0}))

    def test_overlay_refuses_paths_that_escape_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            repository, _ = self._adaptation_repository(directory)
            outside = directory / 'outside'
            outside.mkdir()
            (outside / 'a.md').write_text('one\ntwo\nthree\nfour\nfive\n')
            root = directory / 'cache'
            root.mkdir()
            (root / 'docs').symlink_to(outside)
            adaptations = {'plugins': {'plugin': {'repository': str(repository), 'baseRef': 'upstream',
                                                   'adaptedRef': 'main', 'hosts': ['Codex']}}}
            registry = {'targets': {'Codex': {'plugin@m': []}, 'Claude': {}}}
            with self.assertRaises(ValueError):
                module.plan(registry, [('Codex', 'plugin@m', root)], module.Overlay(directory / 'state'), adaptations)
            self.assertEqual((outside / 'a.md').read_text(), 'one\ntwo\nthree\nfour\nfive\n')

    def test_explicit_target_sets_both_host_flags_and_restores(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            root = directory / 'cache/plugin/1.0'
            skill = root / 'skills/sample/SKILL.md'
            skill.parent.mkdir(parents=True)
            original = '---\nname: sample\ndescription: Only use when the user explicitly invokes this skill by name.\n---\nPublish only with approval.\n'
            skill.write_text(original)
            registry = {'targets': {'Codex': {}, 'Claude': {}}, 'explicitTargets': {'Codex': {'plugin@m': ['sample']}}}
            installs = [('Codex', 'plugin@m', root)]
            overlay = module.Overlay(directory / 'state')
            writes, counts = module.plan(registry, installs, overlay)
            self.assertEqual(counts, {'Codex': 1, 'Claude': 0})
            self.assertEqual(overlay.apply(writes), 2)
            _, front = module.frontmatter(skill.read_text())
            self.assertIs(front['disable-model-invocation'], True)
            self.assertEqual(front['description'], 'Only use when the user explicitly invokes this skill by name.')
            self.assertTrue(skill.read_text().endswith('---\nPublish only with approval.\n'))
            self.assertEqual((skill.parent / 'agents/openai.yaml').read_text(), 'policy:\n  allow_implicit_invocation: false\n')
            self.assertEqual(overlay.apply(module.plan(registry, installs, overlay)[0]), 0)
            self.assertEqual(overlay.restore(installs), 2)
            self.assertEqual(skill.read_text(), original)
            self.assertFalse((skill.parent / 'agents/openai.yaml').exists())

    def test_explicit_flag_replaces_a_false_value_only(self):
        original = '---\nname: sample\ndescription: A task.\ndisable-model-invocation: false\nallowed-tools: [Read]\n---\nBody\n'
        result = module.transform_skill(original, 'p:sample', {}, selected=False, explicit=True)
        self.assertEqual(result, original.replace('disable-model-invocation: false', 'disable-model-invocation: true'))
        self.assertEqual(module.transform_skill(result, 'p:sample', {}, selected=False, explicit=True), result)

    def test_sidecar_can_be_made_explicit_preserving_other_fields(self):
        source = 'interface:\n  display_name: Sample\npolicy:\n  allow_implicit_invocation: true # upstream\n'
        self.assertEqual(module.transform_policy(source, allow=False), source.replace('true #', 'false #'))
        self.assertEqual(module.transform_policy(None, allow=False), 'policy:\n  allow_implicit_invocation: false\n')

    def test_skill_cannot_be_both_implicit_and_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / 'plugin'
            (root / 'skills/sample').mkdir(parents=True)
            (root / 'skills/sample/SKILL.md').write_text('---\nname: sample\ndescription: A task.\n---\nBody\n')
            registry = {'targets': {'Codex': {'plugin@m': ['sample']}, 'Claude': {}},
                        'explicitTargets': {'Codex': {'plugin@m': ['sample']}}}
            with self.assertRaises(ValueError):
                module.plan(registry, [('Codex', 'plugin@m', root)])


if __name__ == '__main__':
    unittest.main()
