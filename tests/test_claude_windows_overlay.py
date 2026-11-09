from dataclasses import replace
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_host
import description as d
import description_toml
import homes
import host_state as hs
from sync_sources import Builder
from test_description import Roots


class ClaudeWindowsOverlayTest(unittest.TestCase):
    def test_windows_patch_cannot_silently_target_a_catalog_install(self):
        for via in ('personal-collections', 'codex-aligned'):
            with Roots() as roots, self.subTest(via=via):
                path = roots.public / 'packages/impeccable.toml'
                text = path.read_text().replace('tool_notes = "apply"', 'tool_notes = "audit"')
                text = text.replace('via = "impeccable"', f'via = "{via}"')
                path.write_text(text.replace('[claude]\n', '[claude]\nwindows_replace = [{ path = "run.cmd", '
                                             'find = "download", replace = "exit /b 1" }]\n'))
                with self.assertRaisesRegex(d.DescriptionError, 'Windows patches require a native marketplace install'):
                    description_toml.load(roots.roots())

    def test_native_windows_patch_is_locked_round_trips_and_reconciles_only_on_windows(self):
        with Roots() as roots, homes.Home() as device:
            path = roots.public / 'packages/impeccable.toml'
            original = description_toml.load(roots.roots())
            text = path.read_text()
            text = text.replace('[claude]\n', '[claude]\nwindows_replace = [{ path = "run.cmd", '
                                'find = "download", replace = "exit /b 1" }]\n')
            path.write_text(text)
            setup = description_toml.load(roots.roots())
            package = setup.packages['impeccable']
            self.assertNotEqual(d.intent(original.packages['impeccable'], setup.repos), d.intent(package, setup.repos))
            rendered = description_toml.render_root(setup, d.Visibility.PUBLIC, None)
            path.write_bytes(rendered['packages/impeccable.toml'])
            self.assertEqual(description_toml.load(roots.roots()).packages['impeccable'], package)
            package = replace(package, tool_notes=d.ToolNotes.AUDIT, modes={})
            setup = replace(setup, packages={'impeccable': package})
            identity = 'impeccable@impeccable'
            root = device.roots.claude_home / 'plugins/cache/impeccable/impeccable/1.0'
            device.write(root / 'run.cmd', 'before\ndownload\nafter\n')
            builder = Builder(setup, device.roots, device.roots.home / 'build')
            with patch('sync_sources.WINDOWS', True):
                writes = builder.overlay(package, hs.Host.CLAUDE, identity, root)
            self.assertEqual([(p.name, new) for p, old, new, *_ in writes], [('run.cmd', 'before\nexit /b 1\nafter\n')])
            with patch('sync_sources.WINDOWS', False):
                self.assertEqual(builder.overlay(package, hs.Host.CLAUDE, identity, root), [])
            facts = claude_host.ClaudeState(device.roots, 'claude').project(setup)
            key = hs.FactKey(hs.Host.CLAUDE, hs.Surface.OVERLAY, identity)
            self.assertEqual(facts[key].value.windows_replace, package.claude.windows_replace)
            disabled = replace(package, claude=replace(package.claude, installs=(d.MarketplaceInstall('impeccable', False),)))
            facts = claude_host.ClaudeState(device.roots, 'claude').project(replace(setup, packages={'impeccable': disabled}))
            self.assertNotIn(key, facts)


if __name__ == '__main__':
    unittest.main()
