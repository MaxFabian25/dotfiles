"""Size checks on the skills this repository owns, under skills/ and packages/.

A session holds every skill's description all the time, and reads a skill's Markdown files whole when the skill
points at them. The user's global rules read a file whole only under 600 lines and 30 KB, so a file past either has
to be read in sections, and a description past 1,024 characters breaks the Agent Skills limit. The private
repository's setup/test_skill_sizes.py holds its own skills to the same limits."""
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

import yaml

REPO = Path(__file__).resolve().parents[1]
ROOTS = ('skills', 'packages')
DESCRIPTION_LIMIT = 1024
EXPLICIT_DESCRIPTION_LIMIT = 200
LINE_LIMIT = 600
BYTE_LIMIT = 30_000
EXPLICIT_TRIGGER = re.compile(
    r'\b[Uu]se\s+(?:when|for|only\s+when|to|also|before|after)\b|\b[Tt]rigger(?:s|ed)?\s+(?:on|when)\b')
EXPLICIT_FIX = ('An explicit-only skill (`disable-model-invocation: true`) takes a short human-facing summary '
                'without model trigger lists.')
# Bytes on 2026-11-12 of the one file that was already past BYTE_LIMIT. The number is a cap on that file. An edit
# that shrinks the file may leave the number or lower it, and one that brings it under BYTE_LIMIT deletes its entry.
# Add no entry.
CEILINGS = {}
# A SKILL.md that was split by branch, and the bytes it may reach, so that it does not grow back. Raise a number
# only with the user's yes.
BUDGETS = {
    'skills/browser-and-computer-use/SKILL.md': 15_500,
    'skills/excel-com-wsl/SKILL.md': 10_200,
    'skills/powerpoint-com-wsl/SKILL.md': 3_000,
    'skills/planted-error-checks/SKILL.md': 7_200,
}
FIX = 'Move a part that only some tasks reach into its own file behind a one-line pointer, or split the file by branch.'
DATED = re.compile(r'\b20\d\d\b|\b[Oo]n \d{1,2}(?:(?: and | to |-|–)\d{1,2})?\s+(?:January|February|March|April|May|June|July|August|September|'
                   r'October|November|December)\b')
UNDATED = ('A session reads every description on every turn. Move the dated test or story into the skill\'s body or '
           'its references/incidents.md and keep the rule.')
# Folders .gitignore leaves out. A checkout that has run a package's own tools holds other projects' Markdown there.
INSTALLED = {'node_modules', '.venv', '.pytest_cache', 'dist', 'build'}


def frontmatter(path):
    lines = path.read_text(encoding='utf-8').split('\n')
    if lines[0] != '---' or '---' not in lines[1:]:
        return {}
    return yaml.safe_load('\n'.join(lines[1:lines.index('---', 1)])) or {}


def owned(pattern):
    return sorted(path for root in ROOTS for path in (REPO / root).rglob(pattern)
                  if path.is_file() and not INSTALLED.intersection(path.relative_to(REPO).parts))


def unreachable_markdown(skill_md):
    """Markdown files inside one skill's folder that no link or filename from SKILL.md (or its CSV indexes) reaches."""
    folder = skill_md.parent.resolve()
    docs = {path.resolve() for path in folder.rglob('*.md')
            if path.is_file() and not INSTALLED.intersection(path.relative_to(folder).parts)}
    index_text = '\n'.join(
        path.read_text(encoding='utf-8', errors='ignore')
        for path in folder.rglob('*.csv')
        if path.is_file() and not INSTALLED.intersection(path.relative_to(folder).parts)
    )
    visited = {skill_md.resolve()}
    queue = [skill_md.resolve()]
    while queue:
        current = queue.pop(0)
        text = current.read_text(encoding='utf-8') + '\n' + index_text
        for candidate in list(docs - visited):
            rel_root = candidate.relative_to(folder).as_posix()
            try:
                rel_current = candidate.relative_to(current.parent).as_posix()
            except ValueError:
                rel_current = None
            if rel_root in text or (rel_current and rel_current in text) or candidate.name in text:
                visited.add(candidate)
                queue.append(candidate)
    return sorted(path.relative_to(folder).as_posix() for path in (docs - visited))


class SkillSizes(unittest.TestCase):
    def test_every_skill_has_a_description_within_the_limit(self):
        skills = owned('SKILL.md')
        self.assertTrue(skills, f'no SKILL.md under {ROOTS}; has the layout changed?')
        for path in skills:
            name = path.relative_to(REPO).as_posix()
            lines = path.read_text(encoding='utf-8').split('\n')
            with self.subTest(skill=name):
                self.assertEqual(lines[0], '---', f'{name} needs frontmatter')
                # As a YAML reader takes it, so a description folded or quoted over several lines counts whole.
                loaded = yaml.safe_load('\n'.join(lines[1:lines.index('---', 1)])) or {}
                self.assertIsInstance(loaded, dict, f'{name} needs frontmatter that is a YAML mapping')
                description = loaded.get('description')
                self.assertIsInstance(description, str, f'{name} needs a `description:` in its frontmatter')
                self.assertLessEqual(len(description), DESCRIPTION_LIMIT,
                                     f'{name} has a description of {len(description)} characters and the limit is '
                                     f'{DESCRIPTION_LIMIT}. Keep what the skill is and one trigger per branch, and '
                                     'move the rest into the body.')

    def test_no_description_holds_a_date(self):
        for path in owned('SKILL.md'):
            name = path.relative_to(REPO).as_posix()
            with self.subTest(skill=name):
                loaded = frontmatter(path)
                description = str(loaded.get('description') or '') if isinstance(loaded, dict) else ''
                found = DATED.search(description)
                self.assertIsNone(found, f'{name} has a date in its description ("{found and found.group()}"). '
                                         f'{UNDATED}')

    def test_every_markdown_file_fits_one_whole_read(self):
        files = owned('*.md')
        self.assertTrue(files)
        for path in files:
            name = path.relative_to(REPO).as_posix()
            data = path.read_bytes()
            lines, size = len(data.splitlines()), len(data)
            with self.subTest(file=name):
                self.assertLess(lines, LINE_LIMIT, f'{name} has {lines} lines and a whole read takes under {LINE_LIMIT}. {FIX}')
                if name in BUDGETS:
                    self.assertLessEqual(size, BUDGETS[name],
                                         f'{name} is {size} bytes and its budget is {BUDGETS[name]}. {FIX}')
                if name in CEILINGS:
                    self.assertLessEqual(size, CEILINGS[name],
                                         f'{name} is {size} bytes, over its cap of {CEILINGS[name]}, which is its size on '
                                         f'the day the limit of {BYTE_LIMIT} came. {FIX} Cutting as many bytes elsewhere '
                                         'in the file also works. Leave the number in CEILINGS as it is.')
                else:
                    self.assertLess(size, BYTE_LIMIT, f'{name} is {size} bytes and a whole read takes under {BYTE_LIMIT}. {FIX}')

    def test_every_capped_file_still_exists_and_is_still_over_the_limit(self):
        for name in CEILINGS:
            with self.subTest(file=name):
                self.assertGreaterEqual((REPO / name).stat().st_size, BYTE_LIMIT,
                                        f'{name} now fits {BYTE_LIMIT} bytes; delete its entry from CEILINGS')

    def test_a_packages_installed_dependencies_are_not_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            # Under a folder named node_modules, so a filter that reads the whole path instead of the path inside
            # the repository counts nothing.
            root = Path(directory, 'node_modules', 'checkout')
            with mock.patch(f'{__name__}.REPO', root):
                for name in ('skills/one/SKILL.md', 'packages/kit/node_modules/dep/README.md',
                             'packages/kit/.venv/lib/dep/README.md', 'packages/kit/.pytest_cache/README.md',
                             'packages/kit/dist/guide.md', 'packages/kit/build/guide.md',
                             'packages/kit/eval/node_modules/dep/README.md', 'packages/kit/skills/two/SKILL.md'):
                    (root / name).parent.mkdir(parents=True, exist_ok=True)
                    (root / name).write_text('text\n')
                self.assertEqual([path.relative_to(root).as_posix() for path in owned('*.md')],
                                 ['packages/kit/skills/two/SKILL.md', 'skills/one/SKILL.md'])

    def test_a_skill_whose_frontmatter_is_no_mapping_is_told_so(self):
        for text in ('---\njust a line\n---\nbody\n', '---\n- one\n- two\n---\nbody\n'):
            with tempfile.TemporaryDirectory() as directory, mock.patch(f'{__name__}.REPO', Path(directory)):
                (Path(directory) / 'skills/one').mkdir(parents=True)
                (Path(directory) / 'skills/one/SKILL.md').write_text(text)
                result = unittest.TestResult()
                SkillSizes('test_every_skill_has_a_description_within_the_limit').run(result)
            self.assertEqual(result.errors, [], text)
            [(_, failure)] = result.failures
            self.assertIn('skills/one/SKILL.md needs frontmatter that is a YAML mapping', failure)

    def test_a_skill_with_empty_frontmatter_is_told_what_it_lacks(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(f'{__name__}.REPO', Path(directory)):
            (Path(directory) / 'skills/one').mkdir(parents=True)
            (Path(directory) / 'skills/one/SKILL.md').write_text('---\n---\nbody\n')
            result = unittest.TestResult()
            SkillSizes('test_every_skill_has_a_description_within_the_limit').run(result)
        self.assertEqual(result.errors, [])
        [(_, failure)] = result.failures
        self.assertIn('skills/one/SKILL.md needs a `description:` in its frontmatter', failure)

    def test_a_dated_description_is_named_and_others_pass(self):
        def failures(text):
            with tempfile.TemporaryDirectory() as directory, mock.patch(f'{__name__}.REPO', Path(directory)):
                (Path(directory) / 'skills/one').mkdir(parents=True)
                (Path(directory) / 'skills/one/SKILL.md').write_text(text)
                result = unittest.TestResult()
                SkillSizes('test_no_description_holds_a_date').run(result)
            self.assertEqual(result.errors, [], text)
            return [failure for _, failure in result.failures]
        for dated, found in (('Use it. On 4 November its check refused a browser.', 'On 4 November'),
                             ('Use it, as on 4 November its check refused a browser.', 'on 4 November'),
                             ('Use it. On 12 and 13 November its check refused a browser.', 'On 12 and 13 November'),
                             ('>-\n  Use it.\n  On 4 November its check refused.', 'On 4 November'),
                             ('|\n  Use it. On 4\n  November its check refused.', 'On 4\nNovember'),
                             ('Use it, because its login changed in 2026.', '2026')):
            [failure] = failures(f'---\ndescription: {dated}\n---\nbody\n')
            self.assertIn(f'skills/one/SKILL.md has a date in its description ("{found}")', failure)
        for text in ('---\ndescription: Use it for 4 accounts on Monday, before 20 sign-ins.\n---\nbody\n',
                     '---\ndescription: Use it for ES2020 modules and 12026 rows.\n---\nbody\n',
                     'On 4 November 2026, a body with no frontmatter.\n', '---\n- On 4 November 2026\n---\nbody\n'):
            self.assertEqual(failures(text), [], text)

    def test_explicit_only_skills_have_concise_human_descriptions(self):
        for path in owned('SKILL.md'):
            name = path.relative_to(REPO).as_posix()
            loaded = frontmatter(path)
            if not isinstance(loaded, dict) or loaded.get('disable-model-invocation') is not True:
                continue
            description = str(loaded.get('description') or '')
            with self.subTest(skill=name):
                self.assertLessEqual(
                    len(description), EXPLICIT_DESCRIPTION_LIMIT,
                    f'{name} is explicit-only with a {len(description)}-character description '
                    f'(limit {EXPLICIT_DESCRIPTION_LIMIT}). {EXPLICIT_FIX}')
                found = EXPLICIT_TRIGGER.search(description)
                self.assertIsNone(
                    found,
                    f'{name} is explicit-only and keeps a trigger phrase ("{found and found.group()}"). {EXPLICIT_FIX}')

    def test_every_skill_markdown_file_is_reachable_from_skill_md(self):
        for path in owned('SKILL.md'):
            name = path.relative_to(REPO).as_posix()
            with self.subTest(skill=name):
                missing = unreachable_markdown(path)
                self.assertEqual(
                    missing, [],
                    f'{name} leaves Markdown files unreachable from SKILL.md: {", ".join(missing)}. '
                    'Add a pointer with its trigger condition or delete the unused file.')

    def test_explicit_description_and_reachability_checks_catch_regressions(self):
        def run(files, test_name):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with mock.patch(f'{__name__}.REPO', root):
                    for rel, text in files.items():
                        (root / rel).parent.mkdir(parents=True, exist_ok=True)
                        (root / rel).write_text(text, encoding='utf-8')
                    result = unittest.TestResult()
                    SkillSizes(test_name).run(result)
            self.assertEqual(result.errors, [], files)
            return [failure for _, failure in result.failures]

        explicit = 'test_explicit_only_skills_have_concise_human_descriptions'
        self.assertEqual(run({'skills/one/SKILL.md': '---\ndisable-model-invocation: true\ndescription: Summarize weekly notes.\n---\n'}, explicit), [])
        self.assertEqual(run({'skills/one/SKILL.md': '---\ndescription: Use when the user asks for summaries.\n---\n'}, explicit), [])
        [trigger_fail] = run({'skills/one/SKILL.md': '---\ndisable-model-invocation: true\ndescription: Summarize notes. Use when asked.\n---\n'}, explicit)
        self.assertIn('Use when', trigger_fail)
        [length_fail] = run({'skills/one/SKILL.md': f'---\ndisable-model-invocation: true\ndescription: {"a" * (EXPLICIT_DESCRIPTION_LIMIT + 1)}\n---\n'}, explicit)
        self.assertIn(f'limit {EXPLICIT_DESCRIPTION_LIMIT}', length_fail)

        reach = 'test_every_skill_markdown_file_is_reachable_from_skill_md'
        self.assertEqual(run({
            'skills/one/SKILL.md': '---\ndescription: One.\n---\nRead [a](references/a.md).\n',
            'skills/one/references/a.md': 'See [b](b.md).\n',
            'skills/one/references/b.md': 'Details.\n',
        }, reach), [])
        [orphan_fail] = run({
            'skills/one/SKILL.md': '---\ndescription: One.\n---\nBody.\n',
            'skills/one/references/incidents.md': 'Unlinked.\n',
        }, reach)
        self.assertIn('references/incidents.md', orphan_fail)


if __name__ == '__main__':
    unittest.main()
