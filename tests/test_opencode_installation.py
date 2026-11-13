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
import test_opencode


class InstallerTests(unittest.TestCase):
    def apply(self, setup, home):
        owner = AcceptedInstallations(setup, Device(Platform.LINUX, home.roots, (), ()))
        self.addCleanup(owner.staging.close)
        prepared = owner.prepare({}, Builder(setup, home.roots, home.roots.engine))
        files = Files(home.roots.home / 'backups')
        for token in prepared.artifacts:
            owner.apply(token, files)
        return owner, prepared

    def test_receipted_projection_is_idempotent_preserves_native_state_and_local_json(self):
        home = self.enterContext(Home())
        setup, _, _ = test_opencode.OpenCodeTests().fixture(home)
        source = setup.roots.private.parent / 'config/opencode'
        home.write(source / 'runtime.toml', 'vault_reference="op://Agents/Fixture/credential"\n'
                   'model="anthropic-api/claude-opus-5-5#xhigh"\n')
        home.write(source / 'runtime.py', 'print("synthetic runtime")\n')
        home.write(source / 'credentials.js.in',
                   'const helper = __DOTFILES_CREDENTIAL_HELPER__; const model = __DOTFILES_MODEL_ID__;\n')
        native = home.roots.home / '.config/opencode'
        state = {'cli.json': b'{"ui":"preserve"}', 'service.json': b'{"disabled":false}',
                 'accounts.db': b'Synthetic account state'}
        for name, data in state.items():
            home.write(native / name, data.decode())
        owner, prepared = self.apply(setup, home)
        self.assertGreater(len(prepared.artifacts), 0)
        self.assertTrue((native / 'dotfiles/skills/kit-auto/SKILL.md').is_file())
        self.assertEqual((native / 'AGENTS.md').read_bytes(), b'Shared instructions.\n')
        path = native / 'opencode.json'
        configuration = json.loads(path.read_bytes())
        self.assertEqual(configuration['model'], 'anthropic-api/claude-opus-5-5#xhigh')
        self.assertEqual(configuration['policies'],
                         [{'action': 'provider.use', 'resource': '*', 'effect': 'deny'},
                          {'action': 'provider.use', 'resource': 'anthropic-api', 'effect': 'allow'}])
        self.assertEqual((native / 'runtime.py').read_bytes(), b'print("synthetic runtime")\n')
        self.assertEqual((native / 'runtime.toml').read_bytes(), (source / 'runtime.toml').read_bytes())
        self.assertNotIn(b'__DOTFILES_', (native / 'plugins/credentials.js').read_bytes())
        configuration['ui'] = {'theme': 'local-user-choice'}
        path.write_bytes(json.dumps(configuration).encode())
        second, again = self.apply(setup, home)
        self.assertEqual(again.artifacts, ())
        self.assertEqual(second.inspect({})[1], ())
        self.assertEqual(json.loads(path.read_bytes())['ui'], {'theme': 'local-user-choice'})
        for name, data in state.items():
            self.assertEqual((native / name).read_bytes(), data)
        receipt = next(json.loads(path.read_bytes()) for path in owner.directory.glob('*.json')
                       if json.loads(path.read_bytes())['identity'] == 'opencode:projection')
        self.assertIsNone(receipt['pending'])
        self.assertEqual(receipt['accepted']['image']['kind'], 'tree')

    def test_managed_edits_are_preserved_and_selection_changes_update_owned_arrays(self):
        home = self.enterContext(Home())
        setup, _, _ = test_opencode.OpenCodeTests().fixture(home)
        self.apply(setup, home)
        updated = replace(setup, host_skills={Host.CODEX: {'kit:auto': SkillMode.OFF}})
        self.apply(updated, home)
        native = home.roots.home / '.config/opencode'
        self.assertFalse((native / 'dotfiles/skills/kit-auto').exists())
        self.assertIn({'action': 'skill', 'resource': 'kit-auto', 'effect': 'deny'},
                      json.loads((native / 'opencode.json').read_bytes())['permissions'])
        updated = replace(setup, host_skills={Host.CODEX: {'kit:auto': SkillMode.OFF, 'kit:hidden': SkillMode.OFF}})
        self.apply(updated, home)
        self.assertIn({'action': 'skill', 'resource': 'kit-hidden', 'effect': 'deny'},
                      json.loads((native / 'opencode.json').read_bytes())['permissions'])
        wrapper = native / 'dotfiles/skills/kit-manual/SKILL.md'
        wrapper.write_bytes(b'User edit that must remain.\n')
        owner = AcceptedInstallations(updated, Device(Platform.LINUX, home.roots, (), ()))
        self.addCleanup(owner.staging.close)
        self.assertIn(Kind.LOCAL_EDIT, {difference.kind for difference in owner.inspect({})[1]})
        with self.assertRaises(ConcurrentEdit):
            owner.prepare({}, Builder(updated, home.roots, home.roots.engine))
        self.assertEqual(wrapper.read_bytes(), b'User edit that must remain.\n')

    def test_pending_tree_swap_recovers_after_interrupted_acceptance(self):
        home = self.enterContext(Home())
        setup, _, _ = test_opencode.OpenCodeTests().fixture(home)
        owner = AcceptedInstallations(setup, Device(Platform.LINUX, home.roots, (), ()))
        self.addCleanup(owner.staging.close)
        prepared = owner.prepare({}, Builder(setup, home.roots, home.roots.engine))
        original = owner._save
        def interrupted(artifact, accepted, pending=None):
            if artifact.identity == 'opencode:projection' and pending is None:
                raise RuntimeError('Synthetic crash before projection acceptance')
            return original(artifact, accepted, pending)
        with patch.object(owner, '_save', interrupted):
            with self.assertRaisesRegex(RuntimeError, 'Synthetic crash'):
                for token in prepared.artifacts:
                    owner.apply(token, Files(home.roots.home / 'backups'))
        receipt = next(json.loads(path.read_bytes()) for path in owner.directory.glob('*.json')
                       if json.loads(path.read_bytes())['identity'] == 'opencode:projection')
        self.assertIsNotNone(receipt['pending'])
        recovered, _ = self.apply(setup, home)
        self.assertEqual(recovered.inspect({})[1], ())
        self.assertTrue((home.roots.home / '.config/opencode/dotfiles/skills/kit-auto/SKILL.md').is_file())


if __name__ == '__main__':
    unittest.main()
