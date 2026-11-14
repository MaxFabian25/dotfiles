import errno
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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
        self.assertIsNone(impeccable.codex.roles)
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

    def test_a_retired_build_kind_fails_as_unknown(self):
        with Roots() as roots:
            path = roots.private / 'packages/example-course.toml'
            path.write_text(path.read_text().replace('build = "course"', 'build = "academic"'))
            self.assertIn(f"{path}:source.build: unknown build 'academic'; expected one of "
                          "['course', 'generic', 'public-emil', 'snapshot']", roots.problems())


def write(root, files):
    for relative, data in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(data)


class RenderTest(unittest.TestCase):
    def test_private_composition_pins_round_trip_and_change_intent(self):
        with Roots() as roots:
            path = roots.private / 'packages/combined.toml'
            path.write_text('[source]\nrepo = "impeccable"\nimports = { extra = "sha256:' + 'a' * 64 +
                            '" }\n[skills]\non = ["variant"]\n[codex]\ninstall = [{ via = "personal", enabled = true }]\n')
            setup = description_toml.load(roots.roots())
            package = setup.packages['combined']
            self.assertEqual(package.source.imports, {'extra': 'sha256:' + 'a' * 64})
            files = description_toml.render_root(setup, description.Visibility.PRIVATE, None)
            path.write_bytes(files['packages/combined.toml'])
            self.assertEqual(description_toml.load(roots.roots()).packages['combined'], package)
            path.write_text(path.read_text().replace('a' * 64, 'b' * 64))
            changed = description_toml.load(roots.roots()).packages['combined']
            self.assertNotEqual(description.intent(package, setup.repos), description.intent(changed, setup.repos))

    def test_a_windows_hook_command_round_trips_and_changes_intent(self):
        with Roots() as roots:
            path = roots.public / 'packages/hooked.toml'
            command = r'cmd /c if exist "${CLAUDE_PLUGIN_ROOT}\skills\run.cmd" "${CLAUDE_PLUGIN_ROOT}\skills\run.cmd" hook'
            path.write_text('public = true\n[source]\nrepo = "impeccable"\n[codex]\ninstall = [{ via = "personal", enabled = true }]\n'
                            f"windows_hook_command = '{command}'\n"
                            f"windows_replace = [{{ path = 'a/run.cmd', find = '\"%~1\" engine-probe >', replace = 'call \"%~1\" engine-probe >' }}]\n")
            setup = description_toml.load(roots.roots())
            package = setup.packages['hooked']
            self.assertEqual(package.codex.windows_hook_command, command)
            self.assertEqual(package.codex.windows_replace, (description.WindowsReplace('a/run.cmd', '"%~1" engine-probe >', 'call "%~1" engine-probe >'),))
            # A lock's intent holds this name, so another name would make every Windows-patched package's lock stale.
            self.assertEqual(type(package.codex.windows_replace[0]).__name__, 'WindowsReplace')
            files = description_toml.render_root(setup, description.Visibility.PUBLIC, None)
            path.write_bytes(files['packages/hooked.toml'])
            self.assertEqual(description_toml.load(roots.roots()).packages['hooked'], package)
            path.write_text(path.read_text().replace('run.cmd', 'other.cmd'))
            changed = description_toml.load(roots.roots()).packages['hooked']
            self.assertNotEqual(description.intent(package, setup.repos), description.intent(changed, setup.repos))

    def test_a_text_replace_round_trips_changes_intent_and_needs_an_aligned_claude_copy(self):
        block = ("\n[[codex.replace]]\npath = 'skills/deslop/SKILL.md'\nfind = 'Hand it over.'\n"
                 "replace = '''Read its files.\nThen rank.'''\n")
        with Roots() as roots:
            path = roots.public / 'packages/cursor-team-kit.toml'
            before = description_toml.load(roots.roots()).packages['cursor-team-kit']
            path.write_text(path.read_text().replace('\n[claude]\n', block + '\n[claude]\n'))
            setup = description_toml.load(roots.roots())
            package = setup.packages['cursor-team-kit']
            self.assertEqual(package.codex.replace,
                             (description.TextReplace('skills/deslop/SKILL.md', 'Hand it over.', 'Read its files.\nThen rank.'),))
            self.assertEqual(package.codex.windows_replace, ())
            self.assertNotEqual(description.intent(before, setup.repos), description.intent(package, setup.repos))
            files = description_toml.render_root(setup, description.Visibility.PUBLIC, None)
            path.write_bytes(files['packages/cursor-team-kit.toml'])
            self.assertEqual(description_toml.load(roots.roots()).packages['cursor-team-kit'], package)
            aligned = 'via = "codex-aligned", enabled = true, agents = []'
            path.write_text(path.read_text().replace(aligned, 'via = "personal-collections", enabled = false')
                            .replace('[claude.skills]\nexplicit = ["fix-ci"]\n', ''))
            self.assertEqual(description_toml.load(roots.roots()).packages['cursor-team-kit'].codex.replace, package.codex.replace)
            disabled = 'via = "personal-collections", enabled = false'
            for managed in ('enabled = true', 'enabled = false, keep_current = true'):
                with self.subTest(managed=managed):
                    path.write_text(path.read_text().replace(disabled, f'via = "personal-collections", {managed}'))
                    with self.assertRaisesRegex(description.DescriptionError, r'claude\.install\[0\]: codex\.replace '
                                                r'reaches Claude only through an aligned install'):
                        description_toml.load(roots.roots())
                    disabled = f'via = "personal-collections", {managed}'

    def test_a_malformed_text_replace_names_its_own_key(self):
        with Roots() as roots:
            path = roots.public / 'packages/cursor-team-kit.toml'
            path.write_text(path.read_text().replace('\n[claude]\n', "\n[[codex.replace]]\npath = 'a'\nfind = 'b'\n\n[claude]\n"))
            self.assertEqual(roots.problems(), [f'{path}:codex.replace[0]: expected path, find and replace strings'])

    def test_forbidden_skill_text_round_trips_changes_intent_and_must_compile(self):
        with Roots() as roots:
            path = roots.public / 'packages/impeccable.toml'
            before = description_toml.load(roots.roots()).packages['impeccable']
            self.assertEqual(before.forbid, ())
            path.write_text(path.read_text().replace('tool_notes = "apply"\n',
                                                     "tool_notes = \"apply\"\nforbid = ['\\bshipped reviewer\\b', 'old-agent']\n"))
            setup = description_toml.load(roots.roots())
            package = setup.packages['impeccable']
            self.assertEqual(package.forbid, (r'\bshipped reviewer\b', 'old-agent'))
            self.assertNotEqual(description.intent(before, setup.repos), description.intent(package, setup.repos))
            files = description_toml.render_root(setup, description.Visibility.PUBLIC, None)
            path.write_bytes(files['packages/impeccable.toml'])
            self.assertEqual(description_toml.load(roots.roots()).packages['impeccable'], package)
            path.write_text(path.read_text().replace('old-agent', 'old-(agent'))
            problems = roots.problems()
            self.assertEqual(len(problems), 1)
            self.assertRegex(problems[0], r"skills\.forbid: 'old-\(agent' is not a regular expression")
            path.write_text(path.read_text().replace('old-(agent', 'old-agent')
                            .replace('[source]\nrepo = "impeccable"\npath = "plugin"\n', ''))
            self.assertIn(f'{path}:skills.forbid: needs a [source], because only a package the engine builds is checked',
                          roots.problems())

    def test_a_role_source_other_than_plugin_is_refused(self):
        # "generated" left on 2026-11-14 with the generator, and "repo" with Impeccable's role files.
        for source in ('repo', 'generated'):
            with self.subTest(source=source), Roots() as roots:
                path = roots.public / 'packages/impeccable.toml'
                path.write_text(path.read_text() + f'\n[codex.roles]\nfrom = "{source}"\n')
                self.assertIn(f'{path}:codex.roles.from: expected plugin', roots.problems())

    def test_a_kept_current_install_round_trips_and_changes_intent(self):
        with Roots() as roots:
            path = roots.public / 'packages/cursor-team-kit.toml'
            before = description_toml.load(roots.roots()).packages['cursor-team-kit']
            text = path.read_text()
            path.write_text(text.replace('install = [{ via = "personal", enabled = true }]',
                                         'install = [{ via = "personal", enabled = false, keep_current = true }]')
                                .replace('via = "codex-aligned", enabled = true, agents = []',
                                         'via = "codex-aligned", enabled = false, keep_current = true, agents = []'))
            setup = description_toml.load(roots.roots())
            kit = setup.packages['cursor-team-kit']
            self.assertEqual(kit.codex.installs, (description.EngineInstall(False, keep_current=True),))
            self.assertEqual(kit.claude.installs, (description.AlignedInstall(False, frozenset(), keep_current=True),))
            self.assertEqual(setup.stale(), frozenset({'cursor-team-kit'}), 'keep_current is intent, so the old lock is stale')
            files = description_toml.render_root(setup, description.Visibility.PUBLIC, None)
            self.assertIn(b'install = [{ via = "personal", enabled = false, keep_current = true }]',
                          files['packages/cursor-team-kit.toml'])
            path.write_bytes(files['packages/cursor-team-kit.toml'])
            self.assertEqual(description_toml.load(roots.roots()).packages['cursor-team-kit'], kit)
        self.assertNotEqual(description.intent(kit, setup.repos), description.intent(before, setup.repos))

    def test_each_recipe_kind_is_written_with_every_key_it_holds_in_one_order(self):
        commit = 'a' * 40

        def written(recipe, **source):
            package = description.Package(
                'kit', description.Visibility.PRIVATE, '', description.BuildSource(recipe, 2, 'Kit', **source), {},
                description.ToolNotes.AUDIT, description.CodexPlacement((description.EngineInstall(False),)), None)
            return description_toml.render_entry(package).partition('\n[codex]\n')[0]

        self.assertEqual(written(description.CourseRecipe('example/course', commit, ('skills/one',), 'pre-', ('one',)),
                                 metadata={'url': 'https://example.invalid/kit'}), f'''\
[source]
build = "course"
repository = "example/course"
commit = "{commit}"
packaging = 2
display_name = "Kit"
prefix = "pre-"
trim_descriptions = ["one"]
select = ["skills/one"]

[source.metadata]
url = "https://example.invalid/kit"
''')
        self.assertEqual(written(description.GenericRecipe('https://example.invalid/kit.git', commit, 'nested',
                                                           '.cursor-plugin/plugin.json', ('./skills/keep',))), f'''\
[source]
build = "generic"
repository = "https://example.invalid/kit.git"
commit = "{commit}"
subdirectory = "nested"
manifest = ".cursor-plugin/plugin.json"
packaging = 2
display_name = "Kit"
select = ["./skills/keep"]
''')
        self.assertEqual(written(description.PublicEmilRecipe('example/emil', commit, ('skills/one', 'skills/two'),
                                                              {'two': 'second', 'one': 'first'}),
                                 frozen=description.Frozen('2026-01-01', 'b' * 40, 'Upstream changed its licence.')), f'''\
[source]
build = "public-emil"
repository = "example/emil"
commit = "{commit}"
packaging = 2
display_name = "Kit"
select = ["skills/one", "skills/two"]
rename = {{ one = "first", two = "second" }}
frozen = {{ since = "2026-01-01", upstream = "{'b' * 40}", reason = "Upstream changed its licence." }}
''')
        self.assertEqual(written(description.SnapshotRecipe('sha256:' + 'c' * 64, {
            'one': description.Origin('example/one', 'skills/one', None),
            'two': description.Origin('example/two', 'skills/two', commit)}),
                                 metadata={'description': 'Snapshot skills.'}), f'''\
[source]
build = "snapshot"
snapshot = "sha256:{'c' * 64}"
packaging = 2
display_name = "Kit"

[source.origins]
one = {{ repository = "example/one", path = "skills/one" }}
two = {{ repository = "example/two", path = "skills/two", commit = "{commit}" }}

[source.metadata]
description = "Snapshot skills."
''')

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


class PlacementTest(unittest.TestCase):
    def setUp(self):
        environment = {name: value for name, value in os.environ.items() if not name.startswith('GIT_')}
        environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, LC_ALL='C', LANGUAGE='C')
        self.enterContext(mock.patch.dict(os.environ, environment, clear=True))

    def git(self, *arguments):
        return subprocess.run([*description_checks.portable.GIT, *map(str, arguments)],
                              check=True, capture_output=True, timeout=30)

    def assert_problem(self, roots, *fragments):
        problems = description_checks.check_placement(roots)
        self.assertTrue(problems)
        for fragment in fragments:
            self.assertIn(fragment, '\n'.join(problems))

    def test_ownership_refusal_stops_loading_before_private_content(self):
        for refused in ('public', 'private'):
            with self.subTest(refused=refused), Roots() as roots:
                for root in (roots.public, roots.private):
                    subprocess.run(['git', 'init', '-q', str(root.parent)], check=True, capture_output=True, timeout=30)
                allowed = roots.private if refused == 'public' else roots.public
                command = (*description_checks.portable.GIT, '-c', 'safe.directory=',
                           '-c', f'safe.directory={allowed.parent.resolve().as_posix()}')
                with mock.patch.object(description_checks.portable, 'GIT', command), \
                        mock.patch.dict(os.environ, {'GIT_TEST_ASSUME_DIFFERENT_OWNER': '1'}):
                    result = subprocess.run([*command, '-C', str(getattr(roots, refused)), 'rev-parse', '--show-toplevel'],
                                            capture_output=True, timeout=30)
                    self.assertEqual(result.returncode, 128)
                    self.assertIn(b'dubious ownership', result.stderr)
                    with mock.patch.object(description_toml.portable, 'read_utf8',
                                           wraps=description_toml.portable.read_utf8) as reads:
                        with self.assertRaises(description.DescriptionError) as caught:
                            description_toml.load(roots.roots())
                problem = '\n'.join(caught.exception.problems)
                self.assertIn('dubious ownership', problem)
                self.assertIn(str(getattr(roots, refused)), problem)
                paths_read = [Path(call.args[0]).resolve() for call in reads.call_args_list]
                self.assertTrue(any(path.is_relative_to(roots.public.resolve()) for path in paths_read))
                self.assertFalse(any(path.is_relative_to(roots.private.resolve()) for path in paths_read))

    def test_public_only_does_not_start_git(self):
        with Roots() as roots, mock.patch.object(description_checks.subprocess, 'run') as run:
            self.assertEqual(description_checks.check_placement(roots.roots(private=False)), [])
            self.assertTrue(description_toml.load(roots.roots(private=False)).packages)
        run.assert_not_called()

    def test_plain_siblings_and_missing_leaves_are_supported(self):
        with Roots() as roots:
            for public, private in ((roots.public, roots.private),
                                    (roots.public / 'missing', roots.private / 'missing'),
                                    (roots.public / 'missing/a', roots.public / 'missing/b')):
                with self.subTest(public=public, private=private):
                    self.assertEqual(description_checks.check_placement(description.Roots(public, private)), [])

    def test_nested_plain_roots_fail_in_either_direction(self):
        with Roots() as roots:
            nested = roots.public / 'nested'
            nested.mkdir()
            for outer, inner in ((roots.public, nested), (nested / 'missing', nested / 'missing/inside')):
                for public, private in ((outer, inner), (inner, outer)):
                    with self.subTest(public=public, private=private):
                        self.assert_problem(description.Roots(public, private), 'may not sit inside')

    def test_launch_failures_and_timeouts_are_description_errors(self):
        cases = (
            (FileNotFoundError('git executable is missing'), 'git executable is missing'),
            (PermissionError('git executable is denied'), 'git executable is denied'),
            (subprocess.TimeoutExpired(['git'], 30, output=b'partial output', stderr=b'blocked\xff'), 'timed out'),
            (subprocess.TimeoutExpired(['git'], 30), 'timed out'),
        )
        for failure, diagnostic in cases:
            with self.subTest(failure=failure), Roots() as roots, \
                    mock.patch.object(description_checks.subprocess, 'run', side_effect=failure) as run:
                with self.assertRaises(description.DescriptionError) as caught:
                    description_toml.load(roots.roots())
                problem = '\n'.join(caught.exception.problems)
                self.assertIn(str(roots.public), problem)
                self.assertIn(diagnostic, problem)
                self.assertEqual(run.call_args.kwargs['timeout'], 30)
                if isinstance(failure, subprocess.TimeoutExpired) and failure.stdout:
                    self.assertIn('partial output', problem)
                    self.assertIn('blocked', problem)
                    self.assertIn('xff', problem)

    def test_unrecognised_git_errors_never_mean_no_repository(self):
        absent = b'fatal: not a git repository (or any of the parent directories): .git\n'
        cases = (
            (1, b'', absent, 'git exited 1'),
            (128, b'', b'fatal: permission denied\n', 'permission denied'),
            (128, b'', b'\xffbad diagnostic\n', '\\xffbad diagnostic'),
            (128, b'', b'', 'no diagnostic'),
            (128, b'', absent + b'extra\n', 'extra'),
            (128, b'', absent[:-1], 'git exited 128'),
            (128, b'unexpected output', absent, 'unexpected output'),
        )
        with Roots() as roots:
            for status, output, diagnostic, expected in cases:
                result = subprocess.CompletedProcess(['git'], status, output, diagnostic)
                with self.subTest(status=status, output=output, diagnostic=diagnostic), \
                        mock.patch.object(description_checks.subprocess, 'run', return_value=result):
                    self.assert_problem(roots.roots(), str(roots.public), expected)

    def test_successful_git_output_must_name_an_existing_containing_directory(self):
        with Roots() as roots:
            encoded = os.fsencode(roots.public.parent.resolve())
            cases = [b'', b'\n', b'relative\n', encoded, encoded + b'X', encoded + b'\nextra\n', encoded + b'\0\n',
                     os.fsencode(roots.public / 'missing') + b'\n',
                     os.fsencode(roots.public / 'setup.toml') + b'\n',
                     os.fsencode(roots.private.resolve()) + b'\n']
            if roots.public.resolve().anchor == Path.cwd().anchor:
                cases.append(os.fsencode(os.path.relpath(roots.public.parent.resolve())) + b'\n')
            private = subprocess.CompletedProcess(['git'], 0, os.fsencode(roots.private.parent.resolve()) + b'\n', b'')
            for output in cases:
                result = subprocess.CompletedProcess(['git'], 0, output, b'')
                with self.subTest(output=output), \
                        mock.patch.object(description_checks.subprocess, 'run', side_effect=[result, private]):
                    self.assert_problem(roots.roots(), str(roots.public), 'cannot discover Git worktree')

    def test_native_git_line_endings(self):
        with Roots() as roots:
            for ending in (b'\n', b'\r\n'):
                for status in (0, 128):
                    results = [subprocess.CompletedProcess(
                        ['git'], status, os.fsencode(root.parent.resolve()) + ending if status == 0 else b'',
                        b'fatal: not a git repository (or any of the parent directories): .git' + ending if status else b'')
                        for root in (roots.public, roots.private)]
                    with self.subTest(ending=ending, status=status), \
                            mock.patch.object(description_checks.subprocess, 'run', side_effect=results):
                        if ending == b'\r\n' and os.name != 'nt':
                            self.assert_problem(roots.roots(), 'cannot discover Git worktree')
                        else:
                            self.assertEqual(description_checks.check_placement(roots.roots()), [])

    def test_existing_file_is_not_a_discovery_directory(self):
        with Roots() as roots:
            file = roots.public / 'setup.toml'
            self.assert_problem(description.Roots(file, roots.private), str(file), 'not a directory')

    def test_unreadable_discovery_directory_fails(self):
        with Roots() as roots:
            directory = roots.public.resolve()
            original = Path.stat

            def stat(path, *args, **kwargs):
                if path == directory:
                    raise PermissionError('discovery directory is unreadable')
                return original(path, *args, **kwargs)

            with mock.patch.object(Path, 'stat', stat):
                self.assert_problem(roots.roots(), str(roots.public), 'discovery directory is unreadable')

    def test_unreadable_git_marker_fails(self):
        with Roots() as roots:
            marker = roots.public.parent.resolve() / '.git'
            original = Path.lstat

            def lstat(path, *args, **kwargs):
                if path == marker:
                    raise PermissionError(f'cannot inspect {marker}')
                return original(path, *args, **kwargs)

            with mock.patch.object(Path, 'lstat', lstat):
                self.assert_problem(roots.roots(), str(roots.public), f'cannot inspect {marker}')

    def test_root_resolution_failure_is_a_placement_error(self):
        with Roots() as roots, mock.patch.object(Path, 'resolve', side_effect=RuntimeError('symlink loop')):
            self.assert_problem(roots.roots(), str(roots.public), 'symlink loop')

    def test_damaged_git_metadata_never_means_no_repository(self):
        for damaged in ('public', 'private'):
            for damage in ('empty directory', 'missing HEAD', 'invalid HEAD'):
                with self.subTest(damaged=damaged, damage=damage), Roots() as roots:
                    root = getattr(roots, damaged)
                    marker = root.parent / '.git'
                    if damage == 'empty directory':
                        marker.mkdir()
                    else:
                        self.git('init', '-q', root.parent)
                        if damage == 'missing HEAD':
                            (marker / 'HEAD').unlink()
                        else:
                            (marker / 'HEAD').write_bytes(b'not a valid HEAD\n')
                    self.assert_problem(roots.roots(), str(root), str(marker.resolve()), 'found Git metadata')

    @unittest.skipIf(os.name == 'nt', 'creating symbolic links requires a Windows privilege')
    def test_broken_git_symlink_never_means_no_repository(self):
        with Roots() as roots:
            marker = roots.public.parent.resolve() / '.git'
            marker.symlink_to(marker.parent / 'missing-metadata', target_is_directory=True)
            self.assert_problem(roots.roots(), str(marker), 'found Git metadata')

    def test_invalid_gitdir_and_bare_repositories_fail(self):
        for kind in ('invalid gitdir', 'bare repository'):
            with self.subTest(kind=kind), Roots() as roots:
                if kind == 'invalid gitdir':
                    (roots.public.parent / '.git').write_bytes(b'gitdir: missing\n')
                else:
                    self.git('init', '--bare', '-q', roots.public)
                self.assert_problem(roots.roots(), str(roots.public), 'git exited 128')

    def test_discovery_overrides_cannot_hide_or_redirect_a_containing_worktree(self):
        with Roots() as roots:
            repository = roots.public.parent.resolve()
            inside = repository / 'private/setup'
            inside.mkdir(parents=True)
            self.git('init', '-q', repository)
            self.git('init', '-q', roots.private.parent)
            elsewhere = roots.private.parent.resolve()
            cases = (
                {'GIT_CEILING_DIRECTORIES': str(repository)},
                {'GIT_DIR': str(elsewhere / '.git')},
                {'GIT_WORK_TREE': str(elsewhere)},
                {'GIT_COMMON_DIR': str(elsewhere / '.git')},
                {'GIT_CONFIG_PARAMETERS': "'core.bare=true'"},
                {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.bare', 'GIT_CONFIG_VALUE_0': 'true'},
                {'GIT_DISCOVERY_ACROSS_FILESYSTEM': '0', 'LC_ALL': 'fr_FR.UTF-8', 'LANGUAGE': 'fr'},
            )
            for overrides in cases:
                with self.subTest(overrides=overrides), mock.patch.dict(os.environ, overrides), \
                        mock.patch.object(description_checks.subprocess, 'run', wraps=subprocess.run) as run:
                    self.assert_problem(description.Roots(roots.public, inside), 'may not sit inside')
                    environment = run.call_args.kwargs['env']
                    for name, value in overrides.items():
                        self.assertEqual(os.environ[name], value)
                        if name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_CEILING_DIRECTORIES',
                                    'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT'):
                            self.assertNotIn(name, environment)
                    self.assertEqual(environment['GIT_DISCOVERY_ACROSS_FILESYSTEM'], '1')
                    self.assertEqual(environment['LC_ALL'], 'C')
                    self.assertEqual(environment['LANGUAGE'], 'C')

    def test_linked_worktrees_use_their_own_containment_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            main, linked = base / 'main', base / 'linked'
            self.git('init', '-q', main)
            self.git('-C', main, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                     'commit', '--allow-empty', '-qm', 'Initial fixture')
            self.git('-C', main, 'worktree', 'add', '--detach', '-q', linked)
            public, private = main / 'setup', linked / 'setup'
            public.mkdir()
            private.mkdir()
            self.assertEqual(description_checks.check_placement(description.Roots(public, private)), [])
            nested = linked / 'private/setup'
            nested.mkdir(parents=True)
            self.assert_problem(description.Roots(private, nested), 'may not sit inside')

    def assert_named_worktree_containment(self, name):
        with tempfile.TemporaryDirectory() as folder:
            repository = Path(folder).resolve() / name
            repository.mkdir()
            self.git('init', '-q', repository)
            public, private = repository / 'public/setup', repository / 'private/setup'
            public.mkdir(parents=True)
            private.mkdir(parents=True)
            self.assert_problem(description.Roots(public, private), 'may not sit inside')

    def test_non_ascii_worktree_paths_preserve_containment(self):
        self.assert_named_worktree_containment('r\u00e9pertoire-\u6771')

    @unittest.skipIf(os.name == 'nt', 'Windows does not preserve ordinary trailing-space directory names')
    def test_trailing_spaces_in_worktree_paths_preserve_containment(self):
        self.assert_named_worktree_containment('repository  ')

    @unittest.skipIf(os.name == 'nt', 'Windows does not permit newlines in ordinary directory names')
    def test_newline_worktree_paths_preserve_loading_and_containment(self):
        for name in ('repository\nextra', 'repository\n', 'repository\r\n'):
            with self.subTest(name=name), Roots() as roots:
                repository = roots.public.parent / name
                repository.mkdir()
                self.git('init', '-q', repository)
                roots.public = Path(shutil.move(roots.public, repository / 'setup'))
                self.assertTrue(description_toml.load(roots.roots()).packages)
                nested = repository / 'private/setup'
                nested.mkdir(parents=True)
                self.assert_problem(description.Roots(roots.public, nested), 'may not sit inside')

    @unittest.skipIf(os.name == 'nt', 'raw undecodable filename bytes are a POSIX filesystem feature')
    def test_raw_posix_worktree_paths_preserve_containment(self):
        try:
            self.assert_named_worktree_containment(os.fsdecode(b'repository-\xff'))
        except OSError as error:
            if error.errno in (errno.EILSEQ, errno.EINVAL):
                self.skipTest(f'the filesystem does not support raw pathname bytes: {error}')
            raise


class LeakTest(unittest.TestCase):
    def test_private_imports_cannot_be_published_or_bypass_the_engine(self):
        with Roots() as roots:
            path = roots.public / 'packages/impeccable.toml'
            original = path.read_text()
            path.write_text(original.replace('repo = "impeccable"',
                            'repo = "impeccable"\nimports = { extra = "sha256:' + 'a' * 64 + '" }'))
            problems = roots.problems()
            self.assertIn(f'{path}:source.imports: source snapshots are private', problems)
            private = roots.private / 'packages/combined.toml'
            private.write_text(path.read_text().replace('public = true\n', ''))
            path.write_text(original)
            self.assertIn(f'{private}:source.imports: composed packages install through an engine catalog', roots.problems())

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
                '[source.origins]\ny = { repository = "https://github.com/example/y.git", path = "y" }\n'
                '[codex]\ninstall = [{ via = "personal", enabled = true }]\n')
            shutil.copytree(roots.private / 'hosts', roots.public / 'hosts')
            (roots.public / 'rules').mkdir()
            (roots.public / 'rules/AGENTS.md').write_text('')
            problems = roots.problems(private=False)
            for problem in (f'{roots.public / "skills/x.toml"}:source.snapshot: personal snapshots are private',
                            f'{roots.public / "packages/y.toml"}:source.build: snapshots are private',
                            f'{roots.public / "hosts/codex.toml"}:: host toggles are private',
                            f'{roots.public / "rules/AGENTS.md"}:: shared rules are private'):
                self.assertIn(problem, problems)

    def test_a_loose_skill_takes_a_snapshot_or_a_local_source_and_nothing_else(self):
        for source, extra in (('repo = "example-skills"\npath = "example-skill"\n', 'source.repo: unknown key'), ('', None)):
            with self.subTest(source=source), Roots() as roots:
                path = roots.private / 'skills/example-skill.toml'
                path.write_text(f'[source]\n{source}[place]\nclaude = true\n')
                problems = roots.problems()
                self.assertIn(f'{path}:source: expected snapshot or local', problems)
                if extra:
                    self.assertIn(f'{path}:{extra}', problems)

    def test_without_flags_the_private_root_is_the_sibling_dotfiles_private_checkout_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(description, 'ENGINE', Path(folder) / 'dotfiles'), \
                mock.patch.dict(os.environ):
            for name in ('AGENT_SETUP_PUBLIC', 'AGENT_SETUP_PRIVATE'):
                os.environ.pop(name, None)
            base = Path(folder)
            (base / 'setup-private/setup').mkdir(parents=True)
            self.assertIsNone(description.Roots.resolve().private)
            (base / 'dotfiles-private/setup').mkdir(parents=True)
            self.assertEqual(description.Roots.resolve().private, base / 'dotfiles-private/setup')

    def test_every_snapshot_origin_names_its_repository_and_path(self):
        with Roots() as roots:
            path = roots.private / 'packages/y.toml'
            path.write_text(f'[source]\nbuild = "snapshot"\nsnapshot = "sha256:{"a" * 64}"\npackaging = 1\ndisplay_name = "Y"\n'
                            '[source.origins]\ny = {}\n[codex]\ninstall = [{ via = "personal", enabled = true }]\n')
            problems = roots.problems()
        self.assertIn(f'{path}:source.origins.y.repository: missing', problems)
        self.assertIn(f'{path}:source.origins.y.path: missing', problems)

    def test_a_device_list_loads_from_the_private_root_only(self):
        with Roots() as roots:
            (roots.private / 'devices').mkdir()
            (roots.private / 'devices/macos.toml').write_text(
                'account_homes = [".local/share/orca/codex-accounts/*/home"]\n'
                '[codex]\nplugin = ["chrome@openai-bundled"]\n[claude]\nloose_skill = ["{claude_home}/skills/orca-cli"]\n')
            setup = description_toml.load(roots.roots())
            self.assertEqual(setup.account_homes, {host_state.DeviceList.MACOS: ('.local/share/orca/codex-accounts/*/home',)})
            listed = setup.expected_unmanaged
            self.assertEqual(listed, {host_state.DeviceList.MACOS: frozenset({
                host_state.FactKey(H.CODEX, host_state.Surface.PLUGIN, 'chrome@openai-bundled'),
                host_state.FactKey(H.CLAUDE, host_state.Surface.LOOSE_SKILL, '{claude_home}/skills/orca-cli')})})
            for outside in ('/home/x/*/home', 'C:/Users/x/*/home', '~/x/*/home', '../x/*/home', 'x/../../y', '', '.'):
                (roots.private / 'devices/linux.toml').write_text(f'account_homes = ["{outside}"]\n')
                self.assertEqual(roots.problems(), [f'{roots.private / "devices/linux.toml"}:account_homes: expected a '
                                                    f'glob under the home folder, not {outside!r}'])
            (roots.private / 'devices/linux.toml').write_text('[codex]\nplugins = ["x"]\n')
            self.assertEqual(roots.problems(), [f'{roots.private / "devices/linux.toml"}:codex.plugins: unknown key'])
            (roots.private / 'devices/linux.toml').unlink()
            (roots.private / 'devices/wsl.toml').write_text('[claude]\nloose_skill = ["{claude_home}/skills/cua-driver"]\n')
            self.assertEqual(description_toml.load(roots.roots()).expected_unmanaged[host_state.DeviceList.WSL], frozenset(
                {host_state.FactKey(H.CLAUDE, host_state.Surface.LOOSE_SKILL, '{claude_home}/skills/cua-driver')}))
            (roots.private / 'devices/omarchy.toml').write_text('[claude]\nloose_skill = ["{claude_home}/skills/omarchy"]\n')
            self.assertEqual(roots.problems(), [f'{roots.private / "devices/omarchy.toml"}:: no device reads this list; '
                                                'name it macos, wsl, linux, windows'])
            (roots.private / 'devices/omarchy.toml').unlink()
            (roots.private / 'devices/wsl.toml').unlink()
            shutil.move(roots.private / 'devices', roots.public / 'devices')
            self.assertIn(f'{roots.public / "devices/macos.toml"}:: device lists are private',
                          roots.problems(private=False))

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
            self.assertEqual(roots.problems(), [f'{path}:claude.install: at most one enabled or kept-current install per host'])

    def test_an_enabled_aligned_copy_needs_an_enabled_codex_install(self):
        with Roots() as roots:
            path = self.edit(roots, 'packages/cursor-team-kit.toml', 'install = [{ via = "personal", enabled = true }]',
                             'install = [{ via = "personal", enabled = false }]')
            self.assertEqual(roots.problems(), [f'{path}:claude.install[0]: an enabled or kept-current aligned copy needs an '
                                                'enabled or kept-current Codex install'])

    def test_a_kept_current_codex_install_can_build_an_aligned_copy(self):
        with Roots() as roots:
            self.edit(roots, 'packages/cursor-team-kit.toml', 'install = [{ via = "personal", enabled = true }]',
                      'install = [{ via = "personal", enabled = false, keep_current = true }]')
            kit = description_toml.load(roots.roots()).packages['cursor-team-kit']
        self.assertEqual(kit.codex.installs, (description.EngineInstall(False, keep_current=True),))

    def test_keep_current_is_only_for_a_switched_off_engine_or_aligned_install(self):
        cases = {
            ('cursor-team-kit', 'install = [{ via = "personal", enabled = true }]',
             'install = [{ via = "personal", enabled = true, keep_current = true }]'):
                'codex.install[0].keep_current: applies only with enabled = false; an enabled install is kept current already',
            ('impeccable', 'install = [{ via = "impeccable", enabled = true }]',
             'install = [{ via = "impeccable", enabled = false, keep_current = true }]'):
                'claude.install[0].keep_current: needs an engine or aligned install, not impeccable',
        }
        for (name, old, new), problem in cases.items():
            with Roots() as roots, self.subTest(new):
                path = self.edit(roots, f'packages/{name}.toml', old, new)
                self.assertEqual(roots.problems(), [f'{path}:{problem}'])

    def test_a_kept_current_codex_install_takes_neither_roles_nor_trusted_hooks(self):
        roles = '[codex.roles]\nfrom = "plugin"\ndirectory = "agents/codex"\n\n'
        for keeps in ('roles', 'hooks'):
            with Roots() as roots, self.subTest(keeps=keeps):
                path = self.edit(roots, 'packages/impeccable.toml', 'install = [{ via = "personal", enabled = true }]',
                                 'install = [{ via = "personal", enabled = false, keep_current = true }]')
                text = path.read_text()
                if keeps == 'roles':
                    text = text.replace('hooks = "trusted"\n', '').replace('[claude]\n', roles + '[claude]\n', 1)
                path.write_text(text)
                self.assertEqual(roots.problems(), [f'{path}:codex.install[0].keep_current: a kept-current install gets no '
                                                    'Codex roles or hook approval; enable it, or drop its roles and trusted hooks'])

    def test_a_kept_current_install_meets_the_rules_an_enabled_one_meets(self):
        impeccable = 'install = [{ via = "impeccable", enabled = true }]'
        cases = {
            'claude.install: at most one enabled or kept-current install per host':
                ('impeccable', [(impeccable, impeccable[:-1] + ', { via = "codex-aligned", enabled = false, keep_current = true }]')]),
            'claude.install[0]: Claude Code runs this install from the catalog, where tool notes do not reach; '
            'give it an aligned Claude install':
                ('impeccable', [(impeccable, 'install = [{ via = "personal-collections", enabled = false, keep_current = true }]')]),
            'claude.install[0]: Windows patches require a native marketplace install':
                ('impeccable', [(impeccable, 'install = [{ via = "codex-aligned", enabled = false, keep_current = true }]\n'
                                             'windows_replace = [{ path = "hooks/hooks.json", find = "a", replace = "b" }]')]),
            'claude.install[0]: an enabled or kept-current aligned copy needs an enabled or kept-current Codex install':
                ('cursor-team-kit', [('install = [{ via = "personal", enabled = true }]', 'install = [{ via = "personal", enabled = false }]'),
                                     ('enabled = true, agents = []', 'enabled = false, keep_current = true, agents = []')]),
            'skills.off: check-compiler-errors, fix-ci, fix-merge-conflicts, get-pr-comments, loop-on-ci, new-branch-and-pr, '
            'review-and-ship, run-smoke-tests, workflow-from-chats stay offered in Claude Code, which ignores skillOverrides for a '
            'personal-collections install; remove them from source.select and raise source.packaging':
                ('cursor-team-kit', [('install = [{ via = "codex-aligned", enabled = true, agents = [] }]',
                                      'install = [{ via = "personal-collections", enabled = false, keep_current = true }]'),
                                     ('[claude.skills]\nexplicit = ["fix-ci"]\n', '')]),
        }
        for problem, (name, edits) in cases.items():
            with Roots() as roots, self.subTest(problem):
                for old, new in edits:
                    path = self.edit(roots, f'packages/{name}.toml', old, new)
                self.assertEqual(roots.problems(), [f'{path}:{problem}'])

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
            ('via = "codex-aligned", enabled = true, agents = []', 'via = "personal-collections", enabled = false'):
                'claude.skills: a Claude mode needs an aligned Claude install',
        }
        for (old, new), problem in cases.items():
            with Roots() as roots, self.subTest(new):
                path = self.edit(roots, 'packages/cursor-team-kit.toml', old, new)
                self.assertEqual(roots.problems(), [f'{path}:{problem}'])

    def engine_install(self, roots, enabled):
        self.edit(roots, 'packages/cursor-team-kit.toml', '\n[claude.skills]\nexplicit = ["fix-ci"]\n', '')
        return self.edit(roots, 'packages/cursor-team-kit.toml', 'via = "codex-aligned", enabled = true, agents = []',
                         f'via = "personal-collections", enabled = {enabled}')

    def test_an_off_skill_is_rejected_in_an_enabled_claude_engine_install(self):
        with Roots() as roots:
            path = self.engine_install(roots, 'true')
            [problem] = roots.problems()
        self.assertTrue(problem.startswith(f'{path}:skills.off: check-compiler-errors, fix-ci, '), problem)
        self.assertTrue(problem.endswith('remove them from source.select and raise source.packaging'), problem)

    def test_an_off_skill_stays_allowed_where_it_takes_effect(self):
        with Roots() as roots:
            description_toml.load(roots.roots())  # the fixture's enabled aligned install
        with Roots() as roots:
            self.engine_install(roots, 'false')
            description_toml.load(roots.roots())

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
