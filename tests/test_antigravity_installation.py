from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from accepted_installations import AcceptedInstallations
from device import Device
from drift import Kind
from homes import Home
from host_state import Host, Platform, SkillMode
from sync_files import ConcurrentEdit, Files
from sync_sources import Builder
import test_antigravity


class AntigravityInstallerTests(unittest.TestCase):
    def apply(self, setup, home):
        owner = AcceptedInstallations(setup, Device(Platform.LINUX, home.roots, (), ()))
        self.addCleanup(owner.staging.close)
        prepared = owner.prepare({}, Builder(setup, home.roots, home.roots.engine))
        files = Files(home.roots.home / 'backups')
        for token in prepared.artifacts:
            owner.apply(token, files)
        return owner, prepared

    def test_receipted_projection_is_idempotent_handles_empty_mcp_and_preserves_local_json(self):
        home = self.enterContext(Home())
        setup, _, _ = test_antigravity.AntigravityTests().fixture(home)
        source = setup.roots.private.parent / 'config/antigravity'
        home.write(source / 'setup.toml',
                   'enabled = true\nbaseline = "codex"\nmcp = ["cua-driver"]\nhooks = true\n'
                   '[cli_settings]\ntoolPermission = "always-proceed"\n')
        home.write(setup.roots.private.parent / 'config/mcp-tool-filters/filters.json',
                   '{"cua_browser_tools":["browser_click"]}\n')
        home.write(source / 'hooks.json', json.dumps({
            'credential-guard': {
                'PreToolUse': [{'matcher': '*', 'hooks': [{'type': 'command', 'command': '{python} -I "{adapter}" pre-tool-use'}]}]
            }
        }))
        home.write(source / 'hook_adapter.py', 'print("{}")\n')
        gemini = home.roots.home / '.gemini'
        # Antigravity App creates an empty 0-byte mcp_config.json and {} hooks.json on first run.
        (gemini / 'config').mkdir(parents=True)
        (gemini / 'config/mcp_config.json').write_bytes(b'')
        (gemini / 'config/hooks.json').write_bytes(b'{}\n')
        home.write(gemini / 'config/config.json', '{"userSettings":{"permissionPreset":"TURBO"}}\n')
        with patch('antigravity.shutil.which', return_value='cua-driver'):
            owner, prepared = self.apply(setup, home)
        self.assertGreater(len(prepared.artifacts), 0)
        self.assertTrue((gemini / 'config/dotfiles/skills/kit-auto/SKILL.md').is_file())
        self.assertEqual((gemini / 'AGENTS.md').read_bytes(), b'Shared instructions.\n')
        self.assertEqual(json.loads((gemini / 'config/skills.json').read_bytes())['entries'],
                         [{'path': str(gemini / 'config/dotfiles/skills')}])
        mcp = json.loads((gemini / 'config/mcp_config.json').read_bytes())
        self.assertEqual(mcp['mcpServers']['cua-driver']['disabledTools'], ['browser_click'])
        self.assertEqual(json.loads((gemini / 'antigravity-cli/settings.json').read_bytes())['toolPermission'],
                         'always-proceed')
        hooks = json.loads((gemini / 'config/hooks.json').read_bytes())
        self.assertIn('credential-guard', hooks)
        # Preserve unrelated keys in mcp_config.json, hooks.json, settings.json, and config.json.
        mcp['mcpServers']['local-server'] = {'command': 'echo'}
        (gemini / 'config/mcp_config.json').write_bytes(json.dumps(mcp).encode())
        hooks['local-user-hook'] = {'Stop': [{'type': 'command', 'command': 'true'}]}
        (gemini / 'config/hooks.json').write_bytes(json.dumps(hooks).encode())
        cli = json.loads((gemini / 'antigravity-cli/settings.json').read_bytes())
        cli['model'] = 'Gemini 4 Carbon'
        (gemini / 'antigravity-cli/settings.json').write_bytes(json.dumps(cli).encode())
        with patch('antigravity.shutil.which', return_value='cua-driver'):
            second, again = self.apply(setup, home)
        self.assertEqual(again.artifacts, ())
        self.assertEqual(second.inspect({})[1], ())
        self.assertIn('local-server', json.loads((gemini / 'config/mcp_config.json').read_bytes())['mcpServers'])
        self.assertIn('local-user-hook', json.loads((gemini / 'config/hooks.json').read_bytes()))
        self.assertEqual(json.loads((gemini / 'antigravity-cli/settings.json').read_bytes())['model'],
                         'Gemini 4 Carbon')
        self.assertEqual((gemini / 'config/config.json').read_text(), '{"userSettings":{"permissionPreset":"TURBO"}}\n')

    def test_local_edit_blocks_sync_and_selection_changes_update_projection(self):
        home = self.enterContext(Home())
        setup, _, _ = test_antigravity.AntigravityTests().fixture(home)
        self.apply(setup, home)
        updated = replace(setup, host_skills={Host.CODEX: {'kit:auto': SkillMode.OFF}})
        self.apply(updated, home)
        native = home.roots.home / '.gemini/config/dotfiles'
        self.assertFalse((native / 'skills/kit-auto').exists())
        wrapper = native / 'skills/kit-manual/SKILL.md'
        wrapper.write_bytes(b'User edit that must remain.\n')
        owner = AcceptedInstallations(updated, Device(Platform.LINUX, home.roots, (), ()))
        self.addCleanup(owner.staging.close)
        self.assertIn(Kind.LOCAL_EDIT, {difference.kind for difference in owner.inspect({})[1]})
        with self.assertRaises(ConcurrentEdit):
            owner.prepare({}, Builder(updated, home.roots, home.roots.engine))
        self.assertEqual(wrapper.read_bytes(), b'User edit that must remain.\n')

    def test_pending_tree_swap_recovers_after_interrupted_acceptance(self):
        home = self.enterContext(Home())
        setup, _, _ = test_antigravity.AntigravityTests().fixture(home)
        owner = AcceptedInstallations(setup, Device(Platform.LINUX, home.roots, (), ()))
        self.addCleanup(owner.staging.close)
        prepared = owner.prepare({}, Builder(setup, home.roots, home.roots.engine))
        original = owner._save

        def interrupted(artifact, accepted, pending=None):
            if artifact.identity == 'antigravity:projection' and pending is None:
                raise RuntimeError('Synthetic crash before projection acceptance')
            return original(artifact, accepted, pending)

        with patch.object(owner, '_save', interrupted):
            with self.assertRaisesRegex(RuntimeError, 'Synthetic crash'):
                for token in prepared.artifacts:
                    owner.apply(token, Files(home.roots.home / 'backups'))
        receipt = next(json.loads(path.read_bytes()) for path in owner.directory.glob('*.json')
                       if json.loads(path.read_bytes())['identity'] == 'antigravity:projection')
        self.assertIsNotNone(receipt['pending'])
        recovered, _ = self.apply(setup, home)
        self.assertEqual(recovered.inspect({})[1], ())
        self.assertTrue((home.roots.home / '.gemini/config/dotfiles/skills/kit-auto/SKILL.md').is_file())


if __name__ == '__main__':
    unittest.main()
