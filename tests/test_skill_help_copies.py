"""No skill copies the help of a script it bundles into its SKILL.md or references, as skill_help_copies.py finds
such copies. The private repository's setup/test_skill_help_copies.py loads the same checker for its own skills."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from skill_help_copies import COPIED_WORDS, copies, describe

REPO = Path(__file__).resolve().parents[1]
ROOTS = ('skills', 'packages')
# Copies found on 2026-11-14 that wait for issue 238. Delete an entry once its copy is gone, and add none: fix a new
# copy instead.
ALLOWED = {
    ('skills/excel-com-wsl', 'scripts/office-logs.ps1', 'references/office-logs.md'):
        'not in the review: restates the script\'s header in a reference read only when a log is left; on issue 238',
}


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8', newline='\n')


def words(tag, count=COPIED_WORDS + 5):
    """A sentence of count words that no other call with another tag shares."""
    return ' '.join(f'{tag}{index}' for index in range(count)) + '.'


class HelpCopies(unittest.TestCase):
    def found(self):
        return [copy for root in ROOTS for copy in copies(REPO / root)]

    def test_no_skill_copies_a_scripts_help(self):
        found = self.found()
        self.assertTrue(any(copy.key in ALLOWED for copy in found), 'no allowed copy found; has the layout changed?')
        self.assertEqual([describe(copy) for copy in found if copy.key not in ALLOWED], [])

    def test_every_allowed_copy_still_occurs(self):
        found = {copy.key for copy in self.found()}
        self.assertEqual([' '.join(key) for key in ALLOWED if key not in found], [],
                         'these copies are gone; delete their entries from ALLOWED')

    def test_a_pasted_help_is_found_in_each_kind_of_script_without_running_it(self):
        with tempfile.TemporaryDirectory() as directory:
            skills = Path(directory, 'skills')
            scripts = skills / 'one' / 'scripts'
            write(scripts / 'tool.py', f'"""{words("doc")}"""\nimport argparse\nfrom pathlib import Path\n'
                                       "Path(__file__).with_name('ran').write_text('ran')\n"
                                       f'argparse.ArgumentParser().add_argument("-x", help=f"{words("arg")} {{x}}")\n')
            write(scripts / 'tool.mjs', f'#!/usr/bin/env node\n// {words("mjs")}\nconsole.log(1)\n// {words("late")}\n')
            write(scripts / 'tool.ps1', f'<#\n{words("ps")}\n#>\nWrite-Output 1\n')
            write(scripts / 'tool.sh', f'#!/bin/sh\n\n# {words("sh")}\necho 1\n')
            write(skills / 'one' / 'SKILL.md', '\n\n'.join(words(tag) for tag in ('doc', 'mjs', 'late', 'ps', 'sh')))
            write(skills / 'one' / 'references' / 'notes.md', words('arg'))
            found = {copy.key: copy.words for copy in copies(skills)}
            self.assertFalse((scripts / 'ran').exists())
        self.assertEqual(found, {('skills/one', 'scripts/tool.py', 'SKILL.md'): COPIED_WORDS + 5,
                                 ('skills/one', 'scripts/tool.py', 'references/notes.md'): COPIED_WORDS + 5,
                                 ('skills/one', 'scripts/tool.mjs', 'SKILL.md'): COPIED_WORDS + 5,
                                 ('skills/one', 'scripts/tool.ps1', 'SKILL.md'): COPIED_WORDS + 5,
                                 ('skills/one', 'scripts/tool.sh', 'SKILL.md'): COPIED_WORDS + 5})

    def test_code_short_runs_and_installed_files_are_not_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            skills = Path(directory, 'skills')
            write(skills / 'one' / 'scripts' / 'tool.py', f'"""{words("doc")}\n\n{words("cmd")}\n{words("short")}"""\n')
            write(skills / 'one' / 'scripts' / 'node_modules' / 'dep' / 'index.js', f'// {words("dep")}\n')
            # A fenced block, a code span, and all of a help in runs of 5 words with a word between them.
            short = words('short').rstrip('.').split()
            broken = ' x '.join(' '.join(short[index:index + 5]) for index in range(0, len(short), 5))
            write(skills / 'one' / 'SKILL.md', f'```\n{words("doc")}\n```\n\n`{words("cmd")}`\n\n{broken}\n\n'
                                               f'{words("dep")}\n')
            write(skills / 'two' / 'SKILL.md', words('doc'))
            self.assertEqual(copies(skills), [])

    def test_a_copy_starts_at_the_threshold_and_a_byte_order_mark_hides_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            skills = Path(directory, 'skills')
            for name, count in (('at', COPIED_WORDS), ('under', COPIED_WORDS - 1)):
                write(skills / name / 'scripts' / 'tool.py', f'﻿"""{words(name, count)}"""\n')
                write(skills / name / 'SKILL.md', '﻿' + words(name, count))
            found = {copy.key: copy.words for copy in copies(skills)}
        self.assertEqual(found, {('skills/at', 'scripts/tool.py', 'SKILL.md'): COPIED_WORDS})


if __name__ == '__main__':
    unittest.main()
