"""Size checks on the skills this repository owns, under skills/ and packages/.

A session holds every skill's description all the time, and reads a skill's Markdown files whole when the skill
points at them. The user's global rules read a file whole only under 600 lines and 30 KB, so a file past either has
to be read in sections, and a description past 1,024 characters breaks the Agent Skills limit. The private
repository's setup/test_skill_sizes.py holds its own skills to the same limits."""
from pathlib import Path
import unittest

REPO = Path(__file__).resolve().parents[1]
ROOTS = ('skills', 'packages')
DESCRIPTION_LIMIT = 1024
LINE_LIMIT = 600
BYTE_LIMIT = 30_000
# Bytes on 2026-11-12 of the one file that was already past BYTE_LIMIT. The number is a cap on that file. An edit
# that shrinks the file may leave the number or lower it. Add no entry.
CEILINGS = {'skills/browser-and-computer-use/SKILL.md': 36_596}
FIX = 'Move a part that only some tasks reach into its own file behind a one-line pointer, or split the file by branch.'


def owned(pattern):
    return sorted(path for root in ROOTS for path in (REPO / root).rglob(pattern) if path.is_file())


class SkillSizes(unittest.TestCase):
    def test_every_skill_has_a_one_line_description_within_the_limit(self):
        skills = owned('SKILL.md')
        self.assertTrue(skills, f'no SKILL.md under {ROOTS}; has the layout changed?')
        for path in skills:
            name = path.relative_to(REPO).as_posix()
            lines = path.read_text(encoding='utf-8').split('\n')
            with self.subTest(skill=name):
                self.assertEqual(lines[0], '---', f'{name} needs frontmatter')
                front = lines[1:lines.index('---', 1)]
                found = [index for index, line in enumerate(front) if line.startswith('description:')]
                self.assertEqual(len(found), 1, f'{name} needs one `description:` line in its frontmatter')
                description = front[found[0]].removeprefix('description:').strip()
                following = front[found[0] + 1:found[0] + 2]
                self.assertFalse(description in ('>', '|', '>-', '|-', '>+', '|+', '') or
                                 any(line[:1] in (' ', '\t') for line in following),
                                 f'{name} runs its description over several lines; write it on one line so that '
                                 'this test can count it')
                self.assertLessEqual(len(description), DESCRIPTION_LIMIT,
                                     f'{name} has a description of {len(description)} characters and the limit is '
                                     f'{DESCRIPTION_LIMIT}. Keep what the skill is and one trigger per branch, and '
                                     'move the rest into the body.')

    def test_every_markdown_file_fits_one_whole_read(self):
        files = owned('*.md')
        self.assertTrue(files)
        for path in files:
            name = path.relative_to(REPO).as_posix()
            data = path.read_bytes()
            lines, size = len(data.splitlines()), len(data)
            with self.subTest(file=name):
                self.assertLess(lines, LINE_LIMIT, f'{name} has {lines} lines and a whole read takes under {LINE_LIMIT}. {FIX}')
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


if __name__ == '__main__':
    unittest.main()
