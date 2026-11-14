import importlib.util
from pathlib import Path
import tempfile
import unittest

ENGINE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('codex_agents', ENGINE / 'codex_agents.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# The rules Impeccable's package gave its repository roles until 2026-11-14, when its agents moved to built-in spawns.
RULES = [('.agents/skills/impeccable/', '{skill}/'), ('](../reference/', ']({skill}/reference/')]


class CodexAgentsTest(unittest.TestCase):
    def test_project_local_paths_point_at_the_installed_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            skill = Path(directory) / 'skills/impeccable'
            (skill / 'scripts').mkdir(parents=True)
            (skill / 'scripts/impeccable').write_text('')
            (skill / 'reference').mkdir()
            (skill / 'reference/component-review.md').write_text('')
            text = 'Run `.agents/skills/impeccable/scripts/impeccable comp-spec`, then see [review](../reference/component-review.md).'
            result = module.rewrite(text, RULES, skill)
            self.assertIn(f'`{skill}/scripts/impeccable comp-spec`', result)
            self.assertIn(f']({skill}/reference/component-review.md)', result)

    def test_rewrite_refuses_paths_missing_from_the_installed_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'does not exist'):
                module.rewrite('Run `.agents/skills/impeccable/scripts/impeccable`.', RULES, Path(directory))

    def test_rewrite_checks_paths_in_a_local_copy_before_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory) / 'cache/impeccable/4.7.0/skills/impeccable'
            local = Path(directory) / 'build/skills/impeccable'
            (local / 'reference').mkdir(parents=True)
            (local / 'reference/component-review.md').write_text('')
            result = module.rewrite('See [review](../reference/component-review.md).', RULES, installed, local)
            self.assertIn(f']({installed}/reference/component-review.md)', result)
            with self.assertRaisesRegex(ValueError, 'does not exist'):
                module.rewrite('See [review](../reference/missing.md).', RULES, installed, local)


if __name__ == '__main__':
    unittest.main()
