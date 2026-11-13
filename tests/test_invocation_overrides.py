import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homes import isolate_git


def setUpModule():
    isolate_git()


spec = importlib.util.spec_from_file_location('invocation_overrides', Path(__file__).resolve().parents[1] / 'invocation_overrides.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def planned(writes, root):
    return {path.relative_to(root).as_posix(): new for path, _, new, _, _ in writes}


def changed(writes, root):
    return {path.relative_to(root).as_posix(): new for path, old, new, _, _ in writes if old != new}


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
            (root / 'SKILL.md').write_text(original, newline='\n')
            (root / 'agents').mkdir()
            (root / 'agents/openai.yaml').write_text('policy:\n  allow_implicit_invocation: false\n', newline='\n')
            registry = {'targets': {'Codex': {'review-agent@system': ['review-agent']}, 'Claude': {}}}
            installs = [('Codex', 'review-agent@system', root)]
            writes, counts = module.plan(registry, installs)
            self.assertEqual(counts, {'Codex': 1, 'Claude': 0})
            self.assertEqual(planned(writes, root)['SKILL.md'], original)
            self.assertEqual(changed(writes, root), {'agents/openai.yaml': 'policy:\n  allow_implicit_invocation: true\n'})

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
        self.assertEqual(module.transform_policy('policy:\n  products: [codex]\n'),
                         'policy:\n  allow_implicit_invocation: true\n  products: [codex]\n')

    def test_a_text_replacement_applies_once_and_an_ambiguous_one_stops(self):
        registry = {'textReplacements': {'p:sample': [{'from': 'Use Task', 'to': 'Use a subagent'}]}}
        skill = '---\nname: sample\ndescription: A task.\n---\nUse Task here.\n'
        self.assertEqual(module.transform_skill(skill, 'p:sample', registry), skill.replace('Use Task', 'Use a subagent'))
        with self.assertRaisesRegex(ValueError, 'Ambiguous text replacement: p:sample'):
            module.transform_skill(skill + 'Use Task there.\n', 'p:sample', registry)

    def test_skills_are_not_collected_from_reference_example_or_test_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for folder in ('skills/kept', 'skills/group/nested', 'skills/group/tests/fixture', 'skills/references/sample'):
                (root / folder).mkdir(parents=True)
                (root / folder / 'SKILL.md').write_text(f'---\nname: {Path(folder).name}\n---\nA skill.\n')
            self.assertEqual(sorted(module.skill_paths(root)), ['kept', 'nested'])

    def test_a_policy_export_sets_each_exported_skills_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ('open', 'closed'):
                (root / 'skills' / name).mkdir(parents=True)
                (root / 'skills' / name / 'SKILL.md').write_text(f'---\nname: {name}\n---\nA skill.\n')
            (root / '.dotfiles-policy.json').write_text(json.dumps({'implicit': ['open'], 'explicit': ['closed']}))
            policies = module.exported_policies(root)
            self.assertEqual(sorted(policies), ['skills/closed/agents/openai.yaml', 'skills/open/SKILL.md',
                                                'skills/open/agents/openai.yaml'])
            self.assertEqual(policies['skills/closed/agents/openai.yaml'](None), 'policy:\n  allow_implicit_invocation: false\n')
            self.assertEqual(policies['skills/open/agents/openai.yaml'](None), 'policy:\n  allow_implicit_invocation: true\n')

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

    def test_a_crlf_copy_merges_as_text_and_keeps_its_line_ends(self):
        base = 'one\ntwo\nthree\nfour\nfive\n'
        adapted = 'one\nTWO\nthree\nfour\nfive\n'
        self.assertEqual(module.merge_adaptation('one\r\ntwo\r\nthree\r\nfour\r\nfive\r\n', base, adapted, 'x'),
                         'one\r\nTWO\r\nthree\r\nfour\r\nfive\r\n')
        self.assertEqual(module.merge_adaptation('one\r\ntwo\r\nthree\r\nfour\r\nFIVE\r\n', base, adapted, 'x'),
                         'one\r\nTWO\r\nthree\r\nfour\r\nFIVE\r\n')
        with self.assertRaisesRegex(ValueError, 'conflict'):
            module.merge_adaptation('one\r\n2\r\nthree\r\nfour\r\nfive\r\n', base, adapted, 'x')
        self.assertIsNone(module.merge_adaptation('one\r\ntwo\r\nthree\r\nfour\r\nfive\r\n', base, None, 'x'))
        self.assertEqual(module.merge_adaptation('one\r\ntwo\r\nthree\r\nfour\r\r\nfive\r\n', base, adapted, 'x'),
                         'one\r\nTWO\r\nthree\r\nfour\r\r\nfive\r\n')
        self.assertEqual(module.merge_adaptation('one\ntwo\nthree\nfour\nfive\r\n', base, adapted, 'x'),
                         'one\nTWO\nthree\nfour\nfive\r\n')
        self.assertEqual(module.merge_adaptation('one', 'one', 'one\ntwo\n', 'x'), 'one\ntwo\n')
        with self.assertRaisesRegex(ValueError, 'conflict'):
            module.merge_adaptation('one\r\ntwo\r\n', 'one\ntwo\n', 'one\r\nTWO\n', 'x')
        script = '#!/bin/sh\r\necho hi\r\n'
        self.assertEqual(module.merge_adaptation(script, script, script.replace('\r\n', '\n'), 'x'),
                         '#!/bin/sh\necho hi\n')

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
            skill.write_text(original, newline='\n')
            git = ['git', '-C', str(repository), '-c', 'user.name=t', '-c', 'user.email=t@t']
            module.subprocess.run([*git[:3], 'init', '-q', '-b', 'main'], check=True)
            module.subprocess.run([*git, 'add', '-A'], check=True)
            module.subprocess.run([*git, 'commit', '-qm', 'upstream'], check=True)
            module.subprocess.run([*git, 'branch', 'upstream'], check=True)
            adapted_body = 'Spawn a read-only agent per harnesses.md.\n'
            skill.write_text(original.replace('Spawn a Task with readonly: true.\n', adapted_body), newline='\n')
            (repository / 'skills/sample/references').mkdir()
            (repository / 'skills/sample/references/harnesses.md').write_text('map\n', newline='\n')
            module.subprocess.run([*git, 'add', '-A'], check=True)
            module.subprocess.run([*git, 'commit', '-qm', 'adapt'], check=True)

            root = directory / 'cache/plugin/1.0'
            (root / 'skills/sample').mkdir(parents=True)
            (root / 'skills/sample/SKILL.md').write_text(original, newline='\n')
            adaptations = {'plugins': {'plugin': {'repository': str(repository), 'baseRef': 'upstream',
                                                   'adaptedRef': 'main', 'hosts': ['Codex']}}}
            registry = {'targets': {'Codex': {'plugin@m': ['sample']}, 'Claude': {}}}
            installs = [('Codex', 'plugin@m', root)]
            writes, _ = module.plan(registry, installs, adaptations)
            effective = changed(writes, root)
            self.assertEqual(set(effective), {'skills/sample/SKILL.md', 'skills/sample/agents/openai.yaml',
                                              'skills/sample/references/harnesses.md'})
            self.assertIn('disable-model-invocation: false', effective['skills/sample/SKILL.md'])
            self.assertTrue(effective['skills/sample/SKILL.md'].endswith(adapted_body))
            self.assertEqual(effective['skills/sample/references/harnesses.md'], 'map\n')

            for path, _, new, _, _ in writes:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(new, newline='\n')
            self.assertEqual(changed(module.plan(registry, installs, adaptations)[0], root), {})

    def _adaptation_repository(self, directory):
        repository = directory / 'adaptation'
        (repository / 'docs').mkdir(parents=True)
        (repository / 'docs/a.md').write_text('one\ntwo\nthree\nfour\nfive\n', newline='\n')
        git = ['git', '-C', str(repository), '-c', 'user.name=t', '-c', 'user.email=t@t']
        module.subprocess.run([*git[:3], 'init', '-q', '-b', 'main'], check=True)
        module.subprocess.run([*git, 'add', '-A'], check=True)
        module.subprocess.run([*git, 'commit', '-qm', 'upstream 1'], check=True)
        module.subprocess.run([*git, 'branch', 'upstream'], check=True)
        (repository / 'docs/a.md').write_text('one\nTWO\nthree\nfour\nfive\n', newline='\n')
        module.subprocess.run([*git, 'commit', '-qam', 'adapt'], check=True)
        return repository, git

    def test_newer_upstream_on_base_ref_is_merged_not_reverted(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            repository, git = self._adaptation_repository(directory)
            module.subprocess.run([*git, 'checkout', '-q', 'upstream'], check=True)
            (repository / 'docs/a.md').write_text('one\ntwo\nthree\nfour\nFIVE\n', newline='\n')
            (repository / 'docs/new.md').write_text('added upstream\n', newline='\n')
            module.subprocess.run([*git, 'add', '-A'], check=True)
            module.subprocess.run([*git, 'commit', '-qm', 'upstream 2'], check=True)
            module.subprocess.run([*git, 'checkout', '-q', 'main'], check=True)
            root = directory / 'cache'
            (root / 'docs').mkdir(parents=True)
            (root / 'docs/a.md').write_text('one\ntwo\nthree\nfour\nFIVE\n', newline='\n')
            (root / 'docs/new.md').write_text('added upstream\n', newline='\n')
            adaptations = {'plugins': {'plugin': {'repository': str(repository), 'baseRef': 'upstream',
                                                   'adaptedRef': 'main', 'hosts': ['Codex']}}}
            registry = {'targets': {'Codex': {'plugin@m': []}, 'Claude': {}}}
            writes, _ = module.plan(registry, [('Codex', 'plugin@m', root)], adaptations)
            self.assertEqual(planned(writes, root), {'docs/a.md': 'one\nTWO\nthree\nfour\nFIVE\n'})

    def test_a_crlf_copy_gets_the_adaptation_in_crlf_and_replays_to_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            repository, _ = self._adaptation_repository(directory)
            root = directory / 'cache'
            (root / 'docs').mkdir(parents=True)
            (root / 'docs/a.md').write_bytes(b'one\r\ntwo\r\nthree\r\nfour\r\nFIVE\r\n')
            adaptations = {'plugins': {'plugin': {'repository': str(repository), 'baseRef': 'upstream',
                                                   'adaptedRef': 'main', 'hosts': ['Codex']}}}
            registry = {'targets': {'Codex': {'plugin@m': []}, 'Claude': {}}}
            installs = [('Codex', 'plugin@m', root)]
            writes, _ = module.plan(registry, installs, adaptations)
            self.assertEqual(changed(writes, root), {'docs/a.md': 'one\r\nTWO\r\nthree\r\nfour\r\nFIVE\r\n'})
            for path, _, new, _, _ in writes:
                path.write_bytes(new.encode('utf-8'))
            self.assertEqual(changed(module.plan(registry, installs, adaptations)[0], root), {})

    def test_adaptation_merges_beside_a_flag_the_policy_export_flipped(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            repository = directory / 'adaptation'
            skill = repository / 'skills/sample/SKILL.md'
            skill.parent.mkdir(parents=True)
            original = '---\nname: sample\ndescription: Use for samples.\ndisable-model-invocation: true\n---\nBody.\n'
            skill.write_text(original, newline='\n')
            git = ['git', '-C', str(repository), '-c', 'user.name=t', '-c', 'user.email=t@t']
            module.subprocess.run([*git[:3], 'init', '-q', '-b', 'main'], check=True)
            module.subprocess.run([*git, 'add', '-A'], check=True)
            module.subprocess.run([*git, 'commit', '-qm', 'upstream'], check=True)
            module.subprocess.run([*git, 'branch', 'upstream'], check=True)
            skill.write_text(original.replace('Use for samples.', 'Use when asked for samples by name.'), newline='\n')
            module.subprocess.run([*git, 'commit', '-qam', 'adapt'], check=True)

            root = directory / 'export'
            (root / 'skills/sample/agents').mkdir(parents=True)
            (root / 'skills/sample/SKILL.md').write_text(original.replace('invocation: true', 'invocation: false'),
                                                         newline='\n')
            (root / 'skills/sample/agents/openai.yaml').write_text('policy:\n  allow_implicit_invocation: true\n',
                                                                   newline='\n')
            (root / '.dotfiles-policy.json').write_text('{"schema": 2, "explicit": [], "implicit": ["sample"]}\n')
            adaptations = {'plugins': {'plugin': {'repository': str(repository), 'baseRef': 'upstream',
                                                   'adaptedRef': 'main', 'hosts': ['Codex']}}}
            registry = {'targets': {'Codex': {'plugin@m': ['sample']}, 'Claude': {}}}
            writes, _ = module.plan(registry, [('Codex', 'plugin@m', root)], adaptations)
            self.assertEqual(planned(writes, root)['skills/sample/SKILL.md'],
                             '---\nname: sample\ndescription: Use when asked for samples by name.\n'
                             'disable-model-invocation: false\n---\nBody.\n')

    def test_adaptation_may_not_add_remove_or_rename_a_skill(self):
        original = '---\nname: sample\ndescription: d\n---\nbody\n'
        cases = {'add': ('new', '---\nname: new\ndescription: d\n---\nbody\n'),
                 'rename': ('sample', original.replace('name: sample', 'name: renamed')),
                 'remove': ('sample', None)}
        for case, (name, adapted) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                repository = Path(directory).resolve() / 'adaptation'
                (repository / 'skills/sample').mkdir(parents=True)
                (repository / 'skills/sample/SKILL.md').write_text(original)
                git = ['git', '-C', str(repository), '-c', 'user.name=t', '-c', 'user.email=t@t']
                module.subprocess.run([*git[:3], 'init', '-q', '-b', 'main'], check=True)
                module.subprocess.run([*git, 'add', '-A'], check=True)
                module.subprocess.run([*git, 'commit', '-qm', 'upstream'], check=True)
                module.subprocess.run([*git, 'branch', 'upstream'], check=True)
                skill = repository / 'skills' / name / 'SKILL.md'
                if adapted is None:
                    skill.unlink()
                else:
                    skill.parent.mkdir(exist_ok=True)
                    skill.write_text(adapted)
                module.subprocess.run([*git, 'add', '-A'], check=True)
                module.subprocess.run([*git, 'commit', '-qm', case], check=True)
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
                (root / 'docs/a.md').write_text('one\ntwo\nthree\nfour\nfive\n', newline='\n')
                installs.append(('Codex', f'plugin@{marketplace}', root))
            registry = {'targets': {'Codex': {}, 'Claude': {}}}
            adaptations = {'plugins': {'plugin': {'repository': str(repository), 'baseRef': 'upstream', 'adaptedRef': 'main',
                                                   'hosts': ['Codex'], 'identities': ['plugin@a']}}}
            writes, _ = module.plan(registry, installs, adaptations=adaptations)
            changed = [(identity, path.relative_to(directory).as_posix(), new)
                       for path, old, new, _, identity in writes if old != new]
            self.assertEqual(changed, [('plugin@a', 'cache/a/plugin/1/docs/a.md', 'one\nTWO\nthree\nfour\nfive\n')])

    def test_a_file_git_cannot_show_raises_with_gits_own_words(self):
        with tempfile.TemporaryDirectory() as directory:
            said = module.subprocess.run(['git', '-C', directory, 'show', 'main:SKILL.md'], capture_output=True,
                                         text=True).stderr.splitlines()[-1].strip()
            with self.assertRaises(module.subprocess.CalledProcessError) as failed:
                module.git_text(Path(directory), 'main', 'SKILL.md')
        self.assertTrue(str(failed.exception).startswith(f'{said} (exit 128 from git '), str(failed.exception))

    def test_an_adaptation_whose_tree_git_cannot_read_raises_with_gits_own_words(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, git = self._adaptation_repository(Path(directory).resolve())
            tree = module.subprocess.run([*git, 'rev-parse', 'main^{tree}'], capture_output=True,
                                         text=True).stdout.strip()
            lost = repository / '.git/objects' / tree[:2] / tree[2:]
            lost.chmod(0o644)  # git writes its objects read-only, and Windows deletes no read-only file
            lost.unlink()
            said = module.subprocess.run([*git, 'diff', '--no-renames', '--name-status', '-z', 'upstream', 'main'],
                                         capture_output=True, text=True).stderr.splitlines()[-1].strip()
            with self.assertRaises(module.subprocess.CalledProcessError) as failed:
                module.adaptation_files({'repository': str(repository), 'baseRef': 'upstream', 'adaptedRef': 'main'})
        self.assertTrue(str(failed.exception).startswith(f'{said} (exit 128 from git '), str(failed.exception))

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
            skill.write_text(original, newline='\n')
            registry = {'targets': {'Codex': {}, 'Claude': {}}, 'explicitTargets': {'Codex': {'plugin@m': ['sample']}}}
            installs = [('Codex', 'plugin@m', root)]
            writes, counts = module.plan(registry, installs)
            self.assertEqual(counts, {'Codex': 1, 'Claude': 0})
            effective = changed(writes, root)
            self.assertEqual(set(effective), {'skills/sample/SKILL.md', 'skills/sample/agents/openai.yaml'})
            _, front = module.frontmatter(effective['skills/sample/SKILL.md'])
            self.assertIs(front['disable-model-invocation'], True)
            self.assertEqual(front['description'], 'Only use when the user explicitly invokes this skill by name.')
            self.assertTrue(effective['skills/sample/SKILL.md'].endswith('---\nPublish only with approval.\n'))
            self.assertEqual(effective['skills/sample/agents/openai.yaml'], 'policy:\n  allow_implicit_invocation: false\n')

    def test_explicit_flag_replaces_a_false_value_only(self):
        original = '---\nname: sample\ndescription: A task.\ndisable-model-invocation: false\nallowed-tools: [Read]\n---\nBody\n'
        result = module.transform_skill(original, 'p:sample', {}, selected=False, explicit=True)
        self.assertEqual(result, original.replace('disable-model-invocation: false', 'disable-model-invocation: true'))
        self.assertEqual(module.transform_skill(result, 'p:sample', {}, selected=False, explicit=True), result)

    def test_explicit_flag_ends_the_header_unless_a_block_scalar_does(self):
        plain = '---\nname: sample\ndescription: A task.\n---\nBody\n'
        self.assertEqual(module.transform_skill(plain, 'p:sample', {}, selected=False, explicit=True),
                         plain.replace('A task.\n', 'A task.\ndisable-model-invocation: true\n'))
        block = '---\nname: sample\ndescription: |\n  First line.\n  Second line.\n---\nBody\n'
        result = module.transform_skill(block, 'p:sample', {}, selected=False, explicit=True)
        self.assertEqual(module.frontmatter(result)[1], {'name': 'sample', 'description': 'First line.\nSecond line.',
                                                         'disable-model-invocation': True})
        self.assertTrue(result.endswith('\n---\nBody\n'))
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
