import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('invocation_overrides', Path(__file__).resolve().parents[1] / 'invocation_overrides.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def planned(writes):
    return {path.name: new for path, _, new, _, _ in writes}


def changed(writes):
    return {path.name: new for path, old, new, _, _ in writes if old != new}


class InvocationOverridesTest(unittest.TestCase):
    def test_native_manifest_selection_excludes_unselected_skill_directories(self):
        for manifest in ('plugin.json', '.codex-plugin/plugin.json', '.claude-plugin/plugin.json'):
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ('selected', 'unselected'):
                    path = root / 'skills' / name / 'SKILL.md'
                    path.parent.mkdir(parents=True)
                    path.write_text(f'---\nname: {name}\n---\nA skill.\n')
                metadata = root / manifest
                metadata.parent.mkdir(parents=True, exist_ok=True)
                metadata.write_text('{"name":"kit","skills":["./skills/selected"]}')
                self.assertEqual(set(module.skill_paths(root)), {'selected'})

    def test_managed_system_skill_is_replayed_without_changing_body(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            root = home / '.codex/skills/.system/review-agent'
            root.mkdir(parents=True)
            original = '---\nname: review-agent\ndescription: Review code changes.\n---\nRead-only; do not modify code.\n'
            (root / 'SKILL.md').write_text(original)
            (root / 'agents').mkdir()
            (root / 'agents/openai.yaml').write_text('policy:\n  allow_implicit_invocation: false\n')
            registry = {'targets': {'Codex': {'review-agent@system': ['review-agent']}, 'Claude': {}}}
            installs = [('Codex', 'review-agent@system', root)]
            writes, counts = module.plan(registry, installs)
            self.assertEqual(counts, {'Codex': 1, 'Claude': 0})
            self.assertEqual(planned(writes)['SKILL.md'], original)
            self.assertEqual(changed(writes), {'openai.yaml': 'policy:\n  allow_implicit_invocation: true\n'})

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

    def test_harness_gaps_name_skills_that_use_cursor_names_without_the_harness_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skills = {'mode': 'Spawn a `Task` subagent per [the map](references/harnesses.md#subagents).\n',
                      'spawner': 'Spawn a `Task` subagent.\n',
                      'router': 'Then run the **mode** skill.\n',
                      'linked': 'Read [mode](../mode/SKILL.md), the **mode** skill.\n',
                      'pointed': 'Run the **mode** skill per [harnesses.md](../mode/references/harnesses.md).\n',
                      'deep': 'See the notes.\n',
                      'plain': 'Write clearly.\n'}
            for name, body in skills.items():
                (root / 'skills' / name).mkdir(parents=True)
                (root / 'skills' / name / 'SKILL.md').write_text(f'---\nname: {name}\n---\n# {name}\n\n{body}')
            (root / 'skills/deep/references').mkdir()
            (root / 'skills/deep/references/notes.md').write_text('Ask with AskQuestion.\n')
            self.assertEqual(module.harness_gaps(root, 'skills/mode/references/harnesses.md'), ['deep', 'router', 'spawner'])

    def test_adaptation_and_invocation_overlays_compose_and_replay_to_nothing(self):
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
            writes, _ = module.plan(registry, installs, adaptations)
            effective = changed(writes)
            self.assertEqual(set(effective), {'SKILL.md', 'openai.yaml', 'harnesses.md'})
            self.assertIn('disable-model-invocation: false', effective['SKILL.md'])
            self.assertTrue(effective['SKILL.md'].endswith(adapted_body))
            self.assertEqual(effective['harnesses.md'], 'map\n')

            for path, _, new, _, _ in writes:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(new)
            self.assertEqual(changed(module.plan(registry, installs, adaptations)[0]), {})

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
            writes, _ = module.plan(registry, [('Codex', 'plugin@m', root)], adaptations)
            self.assertEqual(planned(writes), {'a.md': 'one\nTWO\nthree\nfour\nFIVE\n'})

    def test_adaptation_merges_beside_a_flag_the_policy_export_flipped(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            repository = directory / 'adaptation'
            skill = repository / 'skills/sample/SKILL.md'
            skill.parent.mkdir(parents=True)
            original = '---\nname: sample\ndescription: Use for samples.\ndisable-model-invocation: true\n---\nBody.\n'
            skill.write_text(original)
            git = ['git', '-C', str(repository), '-c', 'user.name=t', '-c', 'user.email=t@t']
            module.subprocess.run([*git[:3], 'init', '-q', '-b', 'main'], check=True)
            module.subprocess.run([*git, 'add', '-A'], check=True)
            module.subprocess.run([*git, 'commit', '-qm', 'upstream'], check=True)
            module.subprocess.run([*git, 'branch', 'upstream'], check=True)
            skill.write_text(original.replace('Use for samples.', 'Use when asked for samples by name.'))
            module.subprocess.run([*git, 'commit', '-qam', 'adapt'], check=True)

            root = directory / 'export'
            (root / 'skills/sample/agents').mkdir(parents=True)
            (root / 'skills/sample/SKILL.md').write_text(original.replace('invocation: true', 'invocation: false'))
            (root / 'skills/sample/agents/openai.yaml').write_text('policy:\n  allow_implicit_invocation: true\n')
            (root / '.dotfiles-policy.json').write_text('{"schema": 2, "explicit": [], "implicit": ["sample"]}\n')
            adaptations = {'plugins': {'plugin': {'repository': str(repository), 'baseRef': 'upstream',
                                                   'adaptedRef': 'main', 'hosts': ['Codex']}}}
            registry = {'targets': {'Codex': {'plugin@m': ['sample']}, 'Claude': {}}}
            writes, _ = module.plan(registry, [('Codex', 'plugin@m', root)], adaptations)
            effective = {path.name: after for path, _, after, _, _ in writes}
            self.assertEqual(effective['SKILL.md'], '---\nname: sample\ndescription: Use when asked for samples by name.\n'
                                                    'disable-model-invocation: false\n---\nBody.\n')

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

    def test_identities_scope_an_adaptation_to_the_installs_they_name(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            repository, _ = self._adaptation_repository(directory)
            installs = []
            for marketplace in ('a', 'b'):
                root = directory / 'cache' / marketplace / 'plugin/1'
                (root / 'docs').mkdir(parents=True)
                (root / 'docs/a.md').write_text('one\ntwo\nthree\nfour\nfive\n')
                installs.append(('Codex', f'plugin@{marketplace}', root))
            registry = {'targets': {'Codex': {}, 'Claude': {}}}
            adaptations = {'plugins': {'plugin': {'repository': str(repository), 'baseRef': 'upstream', 'adaptedRef': 'main',
                                                   'hosts': ['Codex'], 'identities': ['plugin@a']}}}
            writes, _ = module.plan(registry, installs, adaptations=adaptations)
            changed = [(identity, path.relative_to(directory).as_posix(), new)
                       for path, old, new, _, identity in writes if old != new]
            self.assertEqual(changed, [('plugin@a', 'cache/a/plugin/1/docs/a.md', 'one\nTWO\nthree\nfour\nfive\n')])

    def test_plan_without_adaptations_reads_no_registry_file(self):
        with patch.object(module, 'read_json', side_effect=AssertionError('read a registry file')):
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
                module.plan(registry, [('Codex', 'plugin@m', root)], adaptations)
            self.assertEqual((outside / 'a.md').read_text(), 'one\ntwo\nthree\nfour\nfive\n')

    def test_explicit_target_sets_both_host_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            root = directory / 'cache/plugin/1.0'
            skill = root / 'skills/sample/SKILL.md'
            skill.parent.mkdir(parents=True)
            original = '---\nname: sample\ndescription: Only use when the user explicitly invokes this skill by name.\n---\nPublish only with approval.\n'
            skill.write_text(original)
            registry = {'targets': {'Codex': {}, 'Claude': {}}, 'explicitTargets': {'Codex': {'plugin@m': ['sample']}}}
            installs = [('Codex', 'plugin@m', root)]
            writes, counts = module.plan(registry, installs)
            self.assertEqual(counts, {'Codex': 1, 'Claude': 0})
            effective = changed(writes)
            self.assertEqual(set(effective), {'SKILL.md', 'openai.yaml'})
            _, front = module.frontmatter(effective['SKILL.md'])
            self.assertIs(front['disable-model-invocation'], True)
            self.assertEqual(front['description'], 'Only use when the user explicitly invokes this skill by name.')
            self.assertTrue(effective['SKILL.md'].endswith('---\nPublish only with approval.\n'))
            self.assertEqual(effective['openai.yaml'], 'policy:\n  allow_implicit_invocation: false\n')

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
