import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('invocation_overrides', Path(__file__).resolve().parents[1] / 'invocation_overrides.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

RULES = [
    {'match': r'\bAskUserQuestion tool\b', 'note': ' (Codex: `request_user_input`)'},
    {'match': r'\bSkill tool\b', 'note': " (Codex: open the named skill's `SKILL.md`)"},
    {'match': r'\bTask tool\b', 'note': ' (Codex: `spawn_agent`)'},
    {'match': r'\bAgent\(\) or Task\(\)', 'note': ' (Codex: `spawn_agent`)'},
    {'match': r'\bExplore agent(?:/tool)?', 'note': ' (Codex: `spawn_agent` with `agent_type` `explorer`)'},
    {'match': r'\bTodoWrite\b', 'note': ' (Codex: `update_plan`)'},
    {'match': r'\bWebSearch\b', 'note': " (Codex: the web tool's `search_query`)"},
    {'match': r'\bWebFetch\b', 'note': " (Codex: the web tool's `open`)"},
]
ADAPTATIONS = {'plugins': {}, 'toolNotes': {'plugins': ['example-tools'],
                                         'inheritingMarketplaces': ['codex-aligned'], 'rules': RULES}}


class ToolNotesTest(unittest.TestCase):
    def test_notes_follow_claude_tool_names_outside_frontmatter_once(self):
        text = ('---\nname: x\ndescription: Use WebFetch.\ntools: WebSearch, WebFetch\n---\n'
                'STOP and call the AskUserQuestion tool to clarify. Call the Skill tool with "tdd". '
                'Use TodoWrite, WebSearch and WebFetch. If an Explore agent/tool is available, use it. '
                'Spawn them whenever a sub-agent/Task tool is exposed, or an Agent() or Task() tool.\n')
        noted = module.add_tool_notes(text, RULES)
        self.assertTrue(noted.startswith('---\nname: x\ndescription: Use WebFetch.\ntools: WebSearch, WebFetch\n---\n'))
        for expected in ['the AskUserQuestion tool (Codex: `request_user_input`) to clarify',
                         "the Skill tool (Codex: open the named skill's `SKILL.md`) with",
                         'TodoWrite (Codex: `update_plan`)',
                         "WebSearch (Codex: the web tool's `search_query`) and WebFetch (Codex: the web tool's `open`).",
                         'Explore agent/tool (Codex: `spawn_agent` with `agent_type` `explorer`) is',
                         'sub-agent/Task tool (Codex: `spawn_agent`) is',
                         'Agent() or Task() (Codex: `spawn_agent`) tool']:
            self.assertIn(expected, noted)
        self.assertEqual(module.add_tool_notes(noted, RULES), noted)

    def test_unrelated_words_stay_untouched(self):
        text = 'A general-purpose module, a Composer component, an asyncio Task(coro) and a task tool belt.\n'
        self.assertEqual(module.add_tool_notes(text, RULES), text)

    def test_listed_plugins_are_noted_in_both_hosts_except_inheriting_copies(self):
        self.assertEqual(module.noted_here(ADAPTATIONS, 'example-tools@personal'), RULES)
        self.assertEqual(module.noted_here(ADAPTATIONS, 'example-tools@example-marketplace'), RULES)
        self.assertIsNone(module.noted_here(ADAPTATIONS, 'example-tools@codex-aligned'))
        self.assertIsNone(module.noted_here(ADAPTATIONS, 'unlisted@personal'))

    def test_plan_notes_reference_files_and_restores_from_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            root = home / '.codex/plugins/cache/personal/example-tools/1.0.0'
            skill = root / 'skills/example'
            (skill / 'references').mkdir(parents=True)
            (skill / 'SKILL.md').write_text('---\nname: example\ndescription: Research.\n---\nTrack with TodoWrite.\n')
            original = 'Use WebFetch to read it.\n'
            (skill / 'references/process.md').write_text(original)
            registry = {'targets': {'Codex': {}, 'Claude': {}}, 'explicitTargets': {'Codex': {}, 'Claude': {}}}
            installs = [('Codex', 'example-tools@personal', root)]
            overlay = module.Overlay(home / 'recovery')
            writes, _ = module.plan(registry, installs, overlay, ADAPTATIONS)
            self.assertEqual(overlay.apply(writes), 2)
            self.assertIn("WebFetch (Codex: the web tool's `open`)", (skill / 'references/process.md').read_text())
            writes, _ = module.plan(registry, installs, overlay, ADAPTATIONS)
            self.assertEqual(overlay.apply(writes, check=True), 0)
            self.assertEqual(overlay.restore(installs), 2)
            self.assertEqual((skill / 'references/process.md').read_text(), original)


if __name__ == '__main__':
    unittest.main()
