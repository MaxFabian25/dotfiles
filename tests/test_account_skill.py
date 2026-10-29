import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from read_account_skill import select_source, windows_handoff


class SourceSelectionTest(unittest.TestCase):
    def test_reads_the_publisher_even_when_its_discovery_entry_is_disabled(self):
        source = {'name': 'example:task', 'pluginId': 'example@openai-curated-remote',
                  'path': str(Path('/publisher/skills/task/SKILL.md').absolute()), 'enabled': False}
        wrapper = {**source, 'pluginId': None, 'path': '/managed/SKILL.md', 'enabled': True}
        self.assertEqual(select_source([wrapper, source], 'example:task'), Path(source['path']))

    def test_refuses_missing_ambiguous_or_foreign_sources(self):
        source = {'name': 'example:task', 'pluginId': 'example@openai-curated-remote',
                  'path': str(Path('/publisher/skills/task/SKILL.md').absolute())}
        for skills in ([], [source, source], [{**source, 'pluginId': 'example@personal'}]):
            with self.subTest(skills=skills), self.assertRaises(RuntimeError):
                select_source(skills, 'example:task')

    def test_rejects_unqualified_names_and_non_skill_paths(self):
        with self.assertRaises(ValueError):
            select_source([], 'task')
        with self.assertRaises(RuntimeError):
            select_source([{'name': 'example:task', 'pluginId': 'example@openai-curated-remote',
                            'path': '/publisher/settings.json'}], 'example:task')

    def test_wsl_uses_windows_python_and_preserves_the_native_profile(self):
        for profile in ('C:/Users/test person/profile/home', '/mnt/c/Users/test person/profile/home'):
            with self.subTest(profile=profile), patch('portable.WINDOWS', False), patch.object(Path, 'is_file', return_value=True):
                self.assertEqual(windows_handoff(profile, 'read_account_skill.py'), [
                    '/mnt/c/Users/test person/dotfiles/.venv/Scripts/python.exe',
                    'C:/Users/test person/dotfiles/read_account_skill.py', '--profile',
                    'C:/Users/test person/profile/home'])

    def test_wsl_refuses_a_missing_windows_runtime(self):
        with patch('portable.WINDOWS', False), patch.object(Path, 'is_file', return_value=False):
            with self.assertRaises(RuntimeError):
                windows_handoff('C:/Users/test/.codex', 'read_account_skill.py')

    def test_native_profiles_need_no_handoff(self):
        with patch('portable.WINDOWS', False):
            self.assertIsNone(windows_handoff('/home/test/.codex', 'read_account_skill.py'))
        with patch('portable.WINDOWS', True):
            self.assertIsNone(windows_handoff('C:/Users/test/.codex', 'read_account_skill.py'))


if __name__ == '__main__':
    unittest.main()
