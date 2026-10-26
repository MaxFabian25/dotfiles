from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ENGINE = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / 'fixtures/setup'
sys.path.insert(0, str(ENGINE))


import host_state
import description
import description_checks
import description_toml
import runtime_pin
H, M = host_state.Host, host_state.SkillMode


class Roots:
    """Copies of the design's excerpts in two separate folders, as two repositories would hold them."""

    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.public, self.private = base / 'public-repo/setup', base / 'private-repo/setup'
        shutil.copytree(FIXTURES / 'public', self.public)
        shutil.copytree(FIXTURES / 'private', self.private)
        return self

    def __exit__(self, *exc):
        self.directory.cleanup()

    def roots(self, private=True):
        return description.Roots(self.public, self.private if private else None)

    def problems(self, private=True):
        with self.assert_raises() as caught:
            description_toml.load(self.roots(private))
        return caught.exception.problems

    def assert_raises(self):
        return unittest.TestCase().assertRaises(description.DescriptionError)


class LoadTest(unittest.TestCase):
    def test_excerpts_load_into_typed_entries(self):
        with Roots() as roots:
            setup = description_toml.load(roots.roots())
        kit = setup.packages['cursor-team-kit']
        self.assertEqual(kit.visibility, description.Visibility.PUBLIC)
        self.assertEqual(kit.modes['fix-ci'], M.OFF)
        self.assertEqual(kit.modes['deslop'], M.EXPLICIT)
        self.assertNotIn('weekly-review', kit.modes)
        self.assertEqual(kit.source.recipe.commit, '1e5c90a4b6f249917bd21fe361b3a2a7deb741b8')
        self.assertEqual(kit.codex.installs, (description.EngineInstall(enabled=True),))
        self.assertEqual(kit.claude.installs, (description.AlignedInstall(enabled=True, agents=frozenset()),))
        self.assertEqual(kit.claude.modes, {'fix-ci': M.EXPLICIT})
        self.assertEqual(setup.packages['impeccable'].claude.modes, {})
        impeccable = setup.packages['impeccable']
        self.assertEqual(impeccable.source, description.RepoSource('impeccable', 'plugin'))
        self.assertEqual(impeccable.codex.hooks, description.Hooks.TRUSTED)
        self.assertEqual(impeccable.codex.roles.rewrite[1], ('](../reference/', ']({skill}/reference/'))
        self.assertEqual(impeccable.tool_notes, description.ToolNotes.APPLY)
        self.assertEqual(setup.repos['impeccable'].claude,
                         description.HostMarketplace('impeccable', sparse=('.claude-plugin', 'plugin')))
        connector = setup.packages['example-connector']
        self.assertEqual((connector.visibility, connector.source), (description.Visibility.PRIVATE, None))
        self.assertEqual(connector.codex.installs, (description.MarketplaceInstall('openai-curated-remote', True),))
        course = setup.packages['example-course']
        self.assertEqual(course.source.recipe.skills()[:2], ('coursedev-animate', 'coursedev-accessibility'))
        self.assertEqual(course.source.frozen.reason, 'upstream deleted')
        self.assertEqual(setup.loose['example-skill'].place,
                         {description.SkillRoot.CLAUDE: True, description.SkillRoot.AGENTS: True})
        self.assertEqual(setup.host_skills[H.CODEX], {'plugin-creator': M.OFF, 'skill-creator': M.OFF, 'documents': M.OFF})
        self.assertEqual(setup.note_rules[1], description.NoteRule(r'\bTodoWrite\b', ' (Codex: `update_plan`)'))
        self.assertEqual(setup.lock['impeccable'].hooks, 3)
        self.assertEqual(setup.stale(), frozenset())

    def test_public_root_loads_alone(self):
        with Roots() as roots:
            setup = description_toml.load(roots.roots(private=False))
        self.assertEqual(sorted(setup.packages), ['cursor-team-kit', 'impeccable'])
        self.assertEqual(setup.host_skills, {})

    def test_a_new_repo_commit_stales_the_packages_that_use_it(self):
        with Roots() as roots:
            path = roots.public / 'repos/impeccable.toml'
            path.write_text(path.read_text().replace('d631a8827f99414d2b6daba4ef08b7f8701751d7', 'b' * 40))
            self.assertEqual(description_toml.load(roots.roots()).stale(), frozenset({'impeccable'}))

    def test_a_lock_without_a_record_its_entry_needs_is_stale(self):
        cases = {
            'cursor-team-kit': ('public', 'codex = { version = "1.2.0+upstream.1e5c90a4b6f2.codex2", '
                                          'tree = "sha256:681957e1e0ef56d52dff37f6f7697406bc633d5ecb517e1991ec2db5f0241401" }\n'),
            'impeccable': ('public', 'hooks = 3\n'),
            'example-skill': ('private', ', agents = "sha256:1111111111111111111111111111111111111111111111111111111111111111"'),
        }
        for name, (root, record) in cases.items():
            with Roots() as roots, self.subTest(name):
                lock = getattr(roots, root) / 'lock.toml'
                self.assertIn(record, lock.read_text())
                lock.write_text(lock.read_text().replace(record, ''))
                self.assertEqual(description_toml.load(roots.roots()).stale(), frozenset({name}))

    def test_an_edited_entry_reads_as_stale(self):
        with Roots() as roots:
            path = roots.public / 'packages/cursor-team-kit.toml'
            path.write_text(path.read_text().replace('"control-cli", "control-ui"', '"control-cli", "weekly-review", "control-ui"'))
            self.assertEqual(description_toml.load(roots.roots()).stale(), frozenset({'cursor-team-kit'}))

    def test_a_changed_claude_mode_reads_as_stale(self):
        with Roots() as roots:
            path = roots.public / 'packages/cursor-team-kit.toml'
            path.write_text(path.read_text().replace('explicit = ["fix-ci"]', 'on = ["fix-ci"]'))
            setup = description_toml.load(roots.roots())
        self.assertEqual(setup.packages['cursor-team-kit'].claude.modes, {'fix-ci': M.ON})
        self.assertEqual(setup.stale(), frozenset({'cursor-team-kit'}))


def write(root, files):
    for relative, data in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(data)


class RenderTest(unittest.TestCase):
    def test_an_unchanged_entry_keeps_its_bytes_comments_and_all(self):
        with Roots() as roots:
            setup = description_toml.load(roots.roots())
            for root, visibility in ((roots.public, description.Visibility.PUBLIC), (roots.private, description.Visibility.PRIVATE)):
                files = description_toml.render_root(setup, visibility, root)
                for relative, data in files.items():
                    if relative != 'lock.toml':
                        self.assertEqual(data, (root / relative).read_bytes(), relative)
            self.assertIn(b'# six more, moved verbatim', description_toml.render_root(setup, description.Visibility.PUBLIC,
                                                                                      roots.public)['setup.toml'])

    def test_rendered_files_load_back_equal(self):
        with Roots() as roots, tempfile.TemporaryDirectory() as directory:
            setup = description_toml.load(roots.roots())
            fresh = description.Roots(Path(directory) / 'a/setup', Path(directory) / 'b/setup')
            copy = description.Setup(**{**setup.__dict__, 'roots': fresh})
            for visibility in description.Visibility:
                write(fresh.of(visibility), description_toml.render_root(copy, visibility, None))
            self.assertEqual(description_toml.load(fresh), copy)
            self.assertTrue((fresh.public / 'packages/cursor-team-kit.toml').read_text().endswith(
                '[claude]\ninstall = [{ via = "codex-aligned", enabled = true, agents = [] }]\n\n'
                '[claude.skills]\nexplicit = ["fix-ci"]\n'))
            self.assertEqual(sorted(description_toml.render_root(copy, description.Visibility.PRIVATE, None)),
                             ['hosts/codex.toml', 'lock.toml', 'packages/example-connector.toml', 'packages/example-course.toml',
                              'repos/example-skills.toml', 'skills/example-skill.toml'])


class LeakTest(unittest.TestCase):
    def test_a_public_reference_to_a_private_name_is_unknown(self):
        with Roots() as roots:
            path = roots.public / 'packages/impeccable.toml'
            path.write_text(path.read_text().replace('repo = "impeccable"', 'repo = "example-skills"'))
            self.assertIn(f'{path}:source.repo: unknown repo example-skills', roots.problems(private=False))
            self.assertIn(f'{path}:source.repo: unknown repo example-skills', roots.problems())

    def test_a_private_file_in_the_public_root_fails(self):
        with Roots() as roots:
            path = roots.public / 'packages/example-connector.toml'
            shutil.move(roots.private / 'packages/example-connector.toml', path)
            self.assertEqual(roots.problems(), [f'{path}:public: a public file must say public = true'])

    def test_a_public_example_in_the_private_root_fails(self):
        with Roots() as roots:
            path = roots.private / 'repos/pbakaus-impeccable.toml'
            shutil.copy(roots.public / 'repos/impeccable.toml', path)
            self.assertEqual(roots.problems(), [f'{path}:public: a private file must not carry the public key; '
                                                'move it to the public root to publish it'])

    def test_a_private_root_inside_the_public_worktree_fails(self):
        with Roots() as roots:
            repository = roots.public.parent
            subprocess.run(['git', 'init', '-q', str(repository)], check=True)
            inside = repository / 'private/setup'
            shutil.copytree(roots.private, inside)
            with roots.assert_raises() as caught:
                description_toml.load(description.Roots(roots.public, inside))
            self.assertEqual(caught.exception.problems, [f'{inside}:: the private root may not sit inside the public '
                                                         "root's worktree, or the reverse"])

    def test_a_name_defined_in_both_roots_fails(self):
        with Roots() as roots:
            path = roots.private / 'packages/impeccable.toml'
            shutil.copy(roots.private / 'packages/example-connector.toml', path)
            self.assertIn(f'{path}:: impeccable is already defined in the public root', roots.problems())

    def test_private_kinds_fail_in_the_public_root_whatever_the_flag(self):
        cases = {
            'packages/example-course.toml': 'source.build: courses are private',
            'packages/example-connector.toml': 'source: catalog and account toggles are private',
            'repos/example-skills.toml': 'url: https://github.com/example/skills.git is not on public_repositories in setup.toml',
        }
        for relative, problem in cases.items():
            with Roots() as roots, self.subTest(relative):
                path = roots.public / relative
                path.write_text('public = true\n' + (roots.private / relative).read_text())
                (roots.private / relative).unlink()
                self.assertIn(f'{path}:{problem}', roots.problems())

    def test_snapshots_host_toggles_and_rules_fail_in_the_public_root(self):
        with Roots() as roots:
            snapshot = 'sha256:' + 'a' * 64
            (roots.public / 'skills').mkdir()
            (roots.public / 'skills/x.toml').write_text(f'public = true\n[source]\nsnapshot = {{ claude = "{snapshot}" }}\n[place]\nclaude = true\n')
            (roots.public / 'packages/y.toml').write_text(
                f'public = true\n[source]\nbuild = "snapshot"\nsnapshot = "{snapshot}"\npackaging = 1\ndisplay_name = "Y"\n'
                '[source.origins]\ny = {}\n[codex]\ninstall = [{ via = "personal", enabled = true }]\n')
            shutil.copytree(roots.private / 'hosts', roots.public / 'hosts')
            (roots.public / 'rules').mkdir()
            (roots.public / 'rules/AGENTS.md').write_text('')
            problems = roots.problems(private=False)
            for problem in (f'{roots.public}/skills/x.toml:source.snapshot: personal snapshots are private',
                            f'{roots.public}/packages/y.toml:source.build: snapshots are private',
                            f'{roots.public}/hosts/codex.toml:: host toggles are private',
                            f'{roots.public}/rules/AGENTS.md:: shared rules are private'):
                self.assertIn(problem, problems)

    def test_a_device_list_loads_from_the_private_root_only(self):
        with Roots() as roots:
            (roots.private / 'devices').mkdir()
            (roots.private / 'devices/macos.toml').write_text(
                '[codex]\nplugin = ["chrome@openai-bundled"]\n[claude]\nloose_skill = ["{claude_home}/skills/orca-cli"]\n')
            listed = description_toml.load(roots.roots()).expected_unmanaged
            self.assertEqual(listed, {host_state.Platform.MACOS: frozenset({
                host_state.FactKey(H.CODEX, host_state.Surface.PLUGIN, 'chrome@openai-bundled'),
                host_state.FactKey(H.CLAUDE, host_state.Surface.LOOSE_SKILL, '{claude_home}/skills/orca-cli')})})
            (roots.private / 'devices/linux.toml').write_text('[codex]\nplugins = ["x"]\n')
            self.assertEqual(roots.problems(), [f'{roots.private}/devices/linux.toml:codex.plugins: unknown key'])
            (roots.private / 'devices/linux.toml').unlink()
            shutil.move(roots.private / 'devices', roots.public / 'devices')
            self.assertIn(f'{roots.public}/devices/macos.toml:: device lists are private', roots.problems(private=False))

    def test_a_mistyped_private_value_is_a_problem_not_a_crash(self):
        with Roots() as roots:
            path = roots.private / 'packages/example-connector.toml'
            path.write_text(path.read_text().replace('enabled = true', 'enabled = "yes"'))
            self.assertEqual(roots.problems(), [f'{path}:codex.install[0].enabled: expected bool'])

    def test_a_lock_keys_only_entries_of_its_own_root(self):
        with Roots() as roots:
            lock = roots.private / 'lock.toml'
            lock.write_text(lock.read_text() + '\n[impeccable]\nintent = "sha256:' + 'b' * 64 + '"\n')
            self.assertEqual(roots.problems(), [f'{lock}:impeccable: locks only entries of its own root'])


class RuntimePinTest(unittest.TestCase):
    PIN = runtime_pin.RuntimePin(
        '26.1025.11240', f'{runtime_pin.ORIGIN}26.1025.11240/codex-primary-runtime-linux-x64-26.1025.11240.tar.xz',
        'sha256:' + 'a' * 64, 387299468, 'sha256:' + 'b' * 64, 1836123456, ('pdf', 'presentations'))

    def test_the_runtime_pin_loads_from_the_public_root_and_not_the_private_one(self):
        with Roots() as roots:
            (roots.public / runtime_pin.PIN).write_text(self.PIN.render())
            self.assertEqual(description_toml.load(roots.roots()).runtime, self.PIN)
            moved = roots.private / runtime_pin.PIN
            shutil.move(roots.public / runtime_pin.PIN, moved)
            self.assertEqual(roots.problems(), [f'{moved}:: the runtime pin belongs to the public root'])

    def test_an_enabled_runtime_package_must_name_a_plugin_the_pin_offers(self):
        with Roots() as roots:
            (roots.public / runtime_pin.PIN).write_text(self.PIN.render())
            path = roots.private / 'packages/documents.toml'
            path.write_text('[codex]\ninstall = [{ via = "openai-primary-runtime", enabled = true }]\n')
            self.assertEqual(roots.problems(), [
                f'{path}:codex.install[0].via: the pinned linux runtime 26.1025.11240 offers no documents; '
                'set platforms to keep it off linux, or pin a runtime that has it'])
            path.write_text('platforms = ["macos"]\n' + path.read_text())
            self.assertIn('documents', description_toml.load(roots.roots()).packages)

    def test_for_platform_keeps_the_runtime_only_on_linux(self):
        with Roots() as roots:
            (roots.public / runtime_pin.PIN).write_text(self.PIN.render())
            setup = description_toml.load(roots.roots())
        self.assertEqual({platform: setup.for_platform(platform).runtime for platform in host_state.Platform},
                         {host_state.Platform.LINUX: self.PIN, host_state.Platform.MACOS: None,
                          host_state.Platform.WINDOWS: None})


class InvariantTest(unittest.TestCase):
    def edit(self, roots, relative, old, new):
        path = roots.public / relative
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new))
        return path

    def test_a_branch_never_passes_for_a_pin(self):
        with Roots() as roots:
            path = self.edit(roots, 'repos/impeccable.toml', 'd631a8827f99414d2b6daba4ef08b7f8701751d7', 'main')
            self.assertEqual(roots.problems(), [f"{path}:commit: not a full commit: 'main'"])

    def test_one_enabled_install_per_host(self):
        with Roots() as roots:
            path = self.edit(roots, 'packages/impeccable.toml', 'install = [{ via = "impeccable", enabled = true }]',
                             'install = [{ via = "impeccable", enabled = true }, { via = "codex-aligned", enabled = true }]')
            self.assertEqual(roots.problems(), [f'{path}:claude.install: at most one enabled install per host'])

    def test_an_enabled_aligned_copy_needs_an_enabled_codex_install(self):
        with Roots() as roots:
            path = self.edit(roots, 'packages/cursor-team-kit.toml', 'install = [{ via = "personal", enabled = true }]',
                             'install = [{ via = "personal", enabled = false }]')
            self.assertEqual(roots.problems(), [f'{path}:claude.install[0]: an enabled aligned copy needs an enabled Codex install'])

    def test_a_skill_takes_one_mode(self):
        with Roots() as roots:
            path = self.edit(roots, 'packages/cursor-team-kit.toml', '"control-cli", "control-ui", "deslop"',
                             '"control-cli", "control-ui", "deslop", "fix-ci"')
            self.assertEqual(roots.problems(), [f'{path}:skills.explicit: fix-ci already has mode off'])

    def test_a_claude_mode_overrides_only_a_selected_skill_of_an_aligned_install(self):
        cases = {
            ('explicit = ["fix-ci"]', 'explicit = ["deslop"]'):
                'claude.skills.explicit: deslop is explicit for the package; a Claude mode needs an off skill, or is on for an explicit one',
            ('explicit = ["fix-ci"]', 'on = ["control-tower"]'): 'claude.skills.on: control-tower is not a selected skill',
            ('explicit = ["fix-ci"]', 'off = ["fix-ci"]'): 'claude.skills.off: a Claude mode is explicit or on',
            ('via = "codex-aligned", enabled = true, agents = []', 'via = "personal-collections", enabled = true'):
                'claude.skills: a Claude mode needs an aligned Claude install',
        }
        for (old, new), problem in cases.items():
            with Roots() as roots, self.subTest(new):
                path = self.edit(roots, 'packages/cursor-team-kit.toml', old, new)
                self.assertEqual(roots.problems(), [f'{path}:{problem}'])

    def test_a_claude_on_mode_takes_a_package_explicit_skill(self):
        with Roots() as roots:
            self.edit(roots, 'packages/cursor-team-kit.toml', 'explicit = ["fix-ci"]', 'explicit = ["fix-ci"]\non = ["deslop"]')
            kit = description_toml.load(roots.roots()).packages['cursor-team-kit']
        self.assertEqual(kit.modes['deslop'], M.EXPLICIT)
        self.assertEqual(kit.claude.modes, {'fix-ci': M.EXPLICIT, 'deslop': M.ON})

    def test_host_toggles_name_only_what_no_entry_owns(self):
        cases = {
            ('codex', '"cursor-team-kit:fix-ci" = "off"'): 'skills.cursor-team-kit:fix-ci: a plugin skill belongs to its package',
            ('codex', 'example-skill = "off"'): 'skills.example-skill: owned by the loose skill example-skill',
            ('codex', '"{agents_home}/skills/example-skill/SKILL.md" = "off"'):
                'skills.{agents_home}/skills/example-skill/SKILL.md: owned by the loose skill example-skill',
            ('claude', 'example-skill = "on"'): 'skills.example-skill: owned by the loose skill example-skill',
            ('codex', '"anthropic-skills:chrome-browser" = "off"'):
                'skills.anthropic-skills:chrome-browser: a plugin skill belongs to its package',
        }
        for (host, line), problem in cases.items():
            with Roots() as roots, self.subTest(line):
                path = roots.private / f'hosts/{host}.toml'
                path.write_text((path.read_text() if path.is_file() else '[skills]\n') + line + '\n')
                self.assertEqual(roots.problems(), [f'{path}:{problem}'])

    def test_tool_notes_cannot_target_a_claude_install_run_from_the_catalog(self):
        with Roots() as roots:
            path = roots.private / 'packages/example-course.toml'
            path.write_text(path.read_text().replace('[skills]\n', '[skills]\ntool_notes = "apply"\n'))
            self.assertEqual(roots.problems(), [f'{path}:claude.install[0]: Claude Code runs this install from the catalog, '
                                                'where tool notes do not reach; give it an aligned Claude install'])

    def test_a_claude_toggle_can_name_one_of_anthropics_own_skills(self):
        with Roots() as roots:
            path = roots.private / 'hosts/claude.toml'
            path.parent.mkdir(exist_ok=True)
            path.write_text('[skills]\n"anthropic-skills:chrome-browser" = "off"\n')
            setup = description_toml.load(roots.roots())
        self.assertEqual(setup.host_skills[H.CLAUDE], {'anthropic-skills:chrome-browser': M.OFF})

    def test_a_repo_must_register_the_marketplace_an_install_names(self):
        with Roots() as roots:
            path = self.edit(roots, 'packages/impeccable.toml', 'via = "impeccable"', 'via = "pbakaus"')
            self.assertEqual(roots.problems(), [f'{path}:claude.install[0].via: repo impeccable does not register pbakaus in claude'])


if __name__ == '__main__':
    unittest.main()
