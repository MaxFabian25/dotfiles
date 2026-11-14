"""A skill's link to another skill's SKILL.md: the overlay repoints ../NAME/SKILL.md at a skill the plugin keeps in
another category folder, leaves every other link as it is, and refuses a built skill whose link does not resolve."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homes import Home
from description import (AlignedInstall, ClaudePlacement, CodexPlacement, EngineInstall, LocalSource, Package, Roots,
                         Setup, ToolNotes, Visibility)
from host_state import SkillMode
import invocation_overrides
from sync_sources import Builder

NO_TARGETS = {'targets': {'Codex': {}, 'Claude': {}}}
CHIEF = ('---\nname: chief-of-staff\ndescription: Run the team.\n---\n'
         'Run a [retro](../retro/SKILL.md#steps), then write a spec with `../to-spec/SKILL.md`.\n'
         'Hand off with ../claude-handoff/SKILL.md, or draft with ../wayfinder/SKILL.md.\n'
         'Upstream keeps it at https://example.com/skills/in-progress/../retro/SKILL.md.\n')
RELINKED = CHIEF.replace('(../retro/SKILL.md#', '(../../engineering/retro/SKILL.md#').replace(
    '`../to-spec/', '`../../engineering/to-spec/')


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8', newline='\n')


def skill(root, folder, body='Use it.\n', name=None):
    write(root / 'skills' / folder / 'SKILL.md',
          f'---\nname: {name or Path(folder).name}\ndescription: A skill.\n---\n{body}')


def categories(root, chief=CHIEF):
    # Matt Pocock's layout: the build keeps upstream's category folders, while upstream links skills as siblings.
    for folder in ('engineering/retro', 'engineering/to-spec', 'engineering/wayfinder', 'in-progress/claude-handoff'):
        skill(root, folder)
    # A folder whose name a link uses for a skill that lives elsewhere: the link resolves, so it stays.
    skill(root, 'in-progress/wayfinder', name='wayfinder-draft')
    write(root / 'skills/in-progress/chief-of-staff/SKILL.md', chief)


def changed(writes, root):
    return {path.relative_to(root).as_posix(): new for path, old, new, _, _ in writes if old != new}


class OverlayTest(unittest.TestCase):
    def test_a_sibling_link_into_another_category_folder_is_repointed_and_replays_to_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / 'kit'
            categories(root)
            installs = [('Codex', 'kit@personal', root)]
            writes = invocation_overrides.plan(NO_TARGETS, installs)
            self.assertEqual(changed(writes, root), {'skills/in-progress/chief-of-staff/SKILL.md': RELINKED})
            for path, _, new, _, _ in writes:
                path.write_text(new, encoding='utf-8', newline='\n')
            self.assertEqual(invocation_overrides.plan(NO_TARGETS, installs), [])

    def test_the_link_is_repointed_after_the_invocation_edit_in_the_same_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / 'kit'
            categories(root)
            registry = {'targets': {'Claude': {}}, 'explicitTargets': {'Claude': {'kit@personal-collections': ['chief-of-staff']}}}
            writes = invocation_overrides.plan(registry, [('Claude', 'kit@personal-collections', root)])
            self.assertEqual(changed(writes, root)['skills/in-progress/chief-of-staff/SKILL.md'],
                             RELINKED.replace('description: Run the team.\n', 'description: Run the team.\n'
                                              'disable-model-invocation: true\n'))

    def test_a_plugin_whose_links_resolve_plans_no_write(self):
        cases = {'side by side': lambda root: (skill(root, 'grill', 'Then run `../tdd/SKILL.md`.\n'), skill(root, 'tdd')),
                 'repointed already': lambda root: categories(root, RELINKED)}
        for case, make in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve() / 'kit'
                make(root)
                self.assertEqual(invocation_overrides.plan(NO_TARGETS, [('Codex', 'kit@personal', root)]), [])

    def test_a_link_to_a_skill_the_plugin_lacks_stops_the_overlay_and_names_the_plugin_file_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / 'kit'
            categories(root, CHIEF + 'Film it with `../ketchup-video/SKILL.md` per ../../engineering/gone/SKILL.md.\n')
            with self.assertRaises(ValueError) as stop:
                invocation_overrides.plan(NO_TARGETS, [('Codex', 'kit@personal', root)])
            for named in ('kit@personal', 'skills/in-progress/chief-of-staff/SKILL.md', '../ketchup-video/SKILL.md',
                          '../../engineering/gone/SKILL.md'):
                self.assertIn(named, str(stop.exception))
            self.assertNotIn('retro', str(stop.exception))


class AlignedCopyTest(unittest.TestCase):
    def build(self, home, modes, write_source):
        source = home.roots.home / 'source'
        write_source(source)
        home.write(source / '.codex-plugin/plugin.json', '{"name":"kit","version":"1.0.0"}\n')
        package = Package('kit', Visibility.PUBLIC, '', LocalSource('source', home.roots.tree_digest(source)), modes,
                          ToolNotes.AUDIT, CodexPlacement((EngineInstall(True),)), ClaudePlacement((AlignedInstall(True),)))
        setup = Setup(Roots(home.roots.home / 'setup', None), {'kit': package}, {}, {}, {}, None, (), frozenset(), {})
        builder = Builder(setup, home.roots, home.roots.engine)
        codex = builder.codex(package, verify=False)
        return builder, package, codex

    def test_both_hosts_get_the_repointed_link_and_the_locked_codex_tree_keeps_upstreams(self):
        with Home() as home:
            builder, package, codex = self.build(home, {}, categories)
            chief = 'skills/in-progress/chief-of-staff/SKILL.md'
            aligned = builder.claude(package, codex, verify=False)['kit@codex-aligned'].root
            self.assertEqual((aligned / chief).read_text(encoding='utf-8'), RELINKED)
            self.assertEqual((builder.effective(package, codex) / chief).read_text(encoding='utf-8'), RELINKED)
            self.assertEqual((codex.root / chief).read_text(encoding='utf-8'), CHIEF)

    def test_a_link_to_a_skill_claude_leaves_out_stops_the_aligned_copy(self):
        def plugin(source):
            skill(source, 'kept', 'Then run ../dropped/SKILL.md.\n')
            skill(source, 'dropped')
        with Home() as home:
            builder, package, codex = self.build(home, {}, plugin)
            self.assertEqual(list(builder.claude(package, codex, verify=False)), ['kit@codex-aligned'])
        with Home() as home:
            builder, package, codex = self.build(home, {'dropped': SkillMode.OFF}, plugin)
            with self.assertRaises(ValueError) as stop:
                builder.claude(package, codex, verify=False)
            for named in ('kit@codex-aligned', 'skills/kept/SKILL.md', '../dropped/SKILL.md'):
                self.assertIn(named, str(stop.exception))


if __name__ == '__main__':
    unittest.main()
