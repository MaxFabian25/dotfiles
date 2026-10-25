import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('codex_agents', Path(__file__).resolve().parents[1] / 'codex_agents.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

RULES = module.PROFILES['impeccable@personal']['rewrites']


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

    def test_claude_agents_become_codex_roles_with_sandbox_from_their_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'rpi/1.0.0'
            (cache / 'agents').mkdir(parents=True)
            agents = {'locator': 'Grep, Glob, Bash', 'implementer': 'Read, Edit, Bash', 'researcher': 'WebSearch, WebFetch, Read'}
            for name, tools in agents.items():
                (cache / f'agents/{name}.md').write_text(
                    f'---\nname: {name}\ndescription: "Finds \\"things\\"."\ntools: {tools}\nmodel: sonnet\n---\n\nBody of {name}.\n')
            roles, ref = module.generated('rpi@skills', {'directory': 'agents'}, cache)
            self.assertEqual(ref, 'rpi@skills:1.0.0')
            parsed = {name: tomllib.loads(text) for name, text in roles.items()}
            self.assertEqual(parsed['locator.toml']['sandbox_mode'], 'read-only')
            self.assertNotIn('sandbox_mode', parsed['implementer.toml'])
            self.assertNotIn('sandbox_mode', parsed['researcher.toml'])
            self.assertEqual(parsed['locator.toml']['description'], 'Finds "things".')
            self.assertEqual(parsed['locator.toml']['developer_instructions'], 'Body of locator.\n')
            self.assertNotIn('model', parsed['locator.toml'])

    def test_sync_never_overwrites_an_unmanaged_role_and_removes_retired_ones(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            agents = home / '.codex/agents'
            agents.mkdir(parents=True)
            (agents / 'mine.toml').write_text('name = "mine"\n')
            receipts = home / '.codex/skill-updates/codex-agents'
            receipts.mkdir(parents=True)
            (agents / 'old.toml').write_text('old\n')
            (receipts / 'x.json').write_text(json.dumps({'ref': 'a', 'files': {'old.toml': 'hash'}}))
            profiles = {'x@m': {'kind': 'claude-agents'}}
            with patch.object(module, 'HOME', home), patch.object(module, 'PROFILES', profiles), \
                    patch.object(module, 'installed', return_value=home), \
                    patch.object(module, 'desired', return_value=({'new.toml': 'new\n'}, 'b')):
                report = module.sync()
                self.assertEqual(report['x@m']['writes'], ['new.toml'])
                self.assertEqual(report['x@m']['removals'], ['old.toml'])
                self.assertFalse((agents / 'old.toml').exists())
                self.assertEqual((agents / 'new.toml').read_text(), 'new\n')
            with patch.object(module, 'HOME', home), patch.object(module, 'PROFILES', profiles), \
                    patch.object(module, 'installed', return_value=home), \
                    patch.object(module, 'desired', return_value=({'mine.toml': 'other\n'}, 'c')):
                with self.assertRaisesRegex(ValueError, 'unmanaged'):
                    module.sync()
            self.assertEqual((agents / 'mine.toml').read_text(), 'name = "mine"\n')


if __name__ == '__main__':
    unittest.main()
