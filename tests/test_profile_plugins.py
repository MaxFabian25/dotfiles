import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from homes import Home
from accepted_installations import image
from description import CodexPlacement, EngineInstall, Hooks, LocalSource, Lock, LockedTree, Package, Roots, Setup
from description import ToolNotes, Visibility
from host_observation import Unread
from host_state import Host
import profile_plugins
from sync_files import ConcurrentEdit, Files
import sync_hosts

VERSION = '2.0.0+explicit.abc'
MARKETPLACE = '[marketplaces.personal]\nsource_type = "local"\nsource = "/home/user"\n'
HOOK = 'kit@personal:hooks/hooks.json:session_start:0:0'


class Fixture:
    """A ~/.codex with an accepted kit copy, the catalog export Codex installs from, and one Orca account home."""

    def __init__(self, home, *, hooks=Hooks.OFF):
        self.home = home
        roots = home.roots
        self.native = roots.home / 'dotfiles/codex-exports/kit' / VERSION
        home.write(self.native / '.codex-plugin/plugin.json', json.dumps({'name': 'kit', 'version': VERSION}))
        home.write(self.native / 'skills/publish/SKILL.md', '---\nname: publish\ndescription: Upstream.\n---\nBody.\n')
        home.write(self.native / 'skills/publish/agents/openai.yaml', 'policy:\n  allow_implicit_invocation: false\n')
        if hooks == Hooks.TRUSTED:
            home.write(self.native / 'hooks/hooks.json',
                       json.dumps({'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'true'}]}]}}))
        home.write(roots.agents_home / 'plugins/marketplace.json', json.dumps({'name': 'personal', 'plugins': [
            {'name': 'kit', 'source': {'source': 'local', 'path': './dotfiles/codex-exports/kit/' + VERSION}}]}))
        self.canonical = roots.codex_home / 'plugins/cache/personal/kit' / VERSION
        shutil.copytree(self.native, self.canonical)
        home.write(self.canonical / 'skills/publish/SKILL.md', '---\nname: publish\ndescription: Adapted.\n---\nBody.\n')
        home.write(roots.codex_home / 'config.toml', MARKETPLACE)
        self.record = {'path': str(self.canonical), 'image': image(self.canonical)}
        package = Package('kit', Visibility.PUBLIC, '', LocalSource('kit', roots.tree_digest(self.native)), {},
                          ToolNotes.AUDIT, CodexPlacement((EngineInstall(True),), hooks=hooks), None)
        self.setup = Setup(Roots(roots.home / 'setup', None), {'kit': package}, {}, {}, {}, None, (), frozenset(),
                           {'kit': Lock('test', {Host.CODEX: LockedTree(VERSION, roots.tree_digest(self.native))})})
        self.profile = roots.home / '.local/share/orca/codex-accounts/account/home'
        self.config = home.write(self.profile / 'config.toml', '[plugins."kit@personal"]\nenabled = true\n\n' + MARKETPLACE)
        self.cache = self.profile / 'plugins/cache/personal/kit'
        self.target = self.cache / VERSION
        self.trusted = frozenset()

    def accepted(self, identity):
        return self.record if identity == 'plugin:codex:kit@personal' else None

    def approved(self, profile):
        if isinstance(self.trusted, Exception):
            raise self.trusted
        return self.trusted

    def install(self, version='1.0.0', source=None):
        shutil.copytree(source or self.native, self.cache / version)
        return self.cache / version

    def inspect(self, *, hooks=False):
        return profile_plugins.inspect(self.setup, self.home.roots, self.accepted, self.approved if hooks else None)

    def apply(self, mirrors, backup='backup'):
        files = Files(self.home.roots.home / backup)
        profile_plugins.apply(mirrors, self.home.roots, self.accepted, files)
        return files


class ProfilePluginsTest(unittest.TestCase):
    def test_older_version_becomes_the_accepted_copy_and_moves_to_the_backup(self):
        with Home() as home:
            kit = Fixture(home)
            old = kit.install('1.0.0')
            home.write(old / 'my-note.txt', 'an edit in a version Codex no longer loads')
            settings = kit.config.read_bytes()
            mirrors, differences, notes = kit.inspect()
            self.assertEqual((len(mirrors), [d.kind.value for d in differences], notes), (1, ['tree'], ()))
            files = kit.apply(mirrors)
            self.assertEqual(image(kit.target), kit.record['image'])
            self.assertFalse(old.exists())
            saved = files.backup / 'account-homes/account/personal/kit/1.0.0'
            self.assertEqual((saved / 'my-note.txt').read_text(), 'an edit in a version Codex no longer loads')
            self.assertEqual(kit.config.read_bytes(), settings)
            self.assertEqual([p.name for p in kit.cache.iterdir()], [VERSION])
            self.assertEqual(kit.inspect(), ((), (), ()))

    def test_native_refresh_and_partly_overlaid_copies_are_replaced_but_edits_are_preserved(self):
        for case in ('native', 'partly overlaid', 'edited', 'extra file'):
            with self.subTest(case), Home() as home:
                kit = Fixture(home)
                target = kit.install(VERSION)
                if case == 'partly overlaid':
                    shutil.copy(kit.canonical / 'skills/publish/SKILL.md', target / 'skills/publish/SKILL.md')
                if case == 'edited':
                    home.write(target / 'skills/publish/SKILL.md', 'my edit')
                if case == 'extra file':
                    home.write(target / 'skills/publish/notes.md', 'mine')
                before = image(target)
                mirrors, differences, _ = kit.inspect()
                if case in ('edited', 'extra file'):
                    self.assertEqual((mirrors, differences[0].kind.value), ((), 'local-edit'))
                    self.assertIn('preserved for review', differences[0].detail)
                    self.assertEqual(image(target), before)
                    continue
                kit.apply(mirrors)
                self.assertEqual(image(kit.target), kit.record['image'])
                self.assertEqual(kit.inspect(), ((), (), ()))

    def test_its_own_earlier_copy_is_replaced_when_the_accepted_copy_changes_at_the_same_version(self):
        with Home() as home:
            kit = Fixture(home)
            kit.install('1.0.0')
            kit.apply(kit.inspect()[0])
            home.write(kit.canonical / 'skills/publish/SKILL.md', '---\nname: publish\ndescription: Readapted.\n---\nBody.\n')
            kit.record = {'path': str(kit.canonical), 'image': image(kit.canonical)}
            mirrors, differences, _ = kit.inspect()
            self.assertEqual([d.kind.value for d in differences], ['tree'])
            kit.apply(mirrors, 'second-backup')
            self.assertIn('Readapted.', (kit.target / 'skills/publish/SKILL.md').read_text())

    def test_a_release_that_changes_the_accepted_copy_reaches_the_home_in_the_same_sync(self):
        with Home() as home:
            kit = Fixture(home)
            kit.install('1.0.0')
            kit.apply(kit.inspect()[0])
            planned = kit.inspect()[0]
            self.assertEqual(planned, ())

            def release(artifact, files, native):
                home.write(kit.canonical / 'skills/publish/SKILL.md', '---\nname: publish\ndescription: Released.\n---\nBody.\n')
                kit.record = {'path': str(kit.canonical), 'image': image(kit.canonical)}

            operation = sync_hosts.Apply.__new__(sync_hosts.Apply)
            operation.setup, operation.roots = kit.setup, home.roots
            operation.current = SimpleNamespace(profile_mirrors=planned)
            operation.files = Files(home.roots.home / 'backup')
            operation.owner = SimpleNamespace(preflight=lambda prepared: None, apply=release, accepted=kit.accepted)
            operation.prepared = SimpleNamespace(artifacts=('kit',))
            for step in ('_marketplaces', '_runtime', '_catalogs', '_plugins', '_skills'):
                setattr(operation, step, lambda: None)
            operation.run()
            self.assertIn('Released.', (kit.target / 'skills/publish/SKILL.md').read_text())
            self.assertEqual(kit.inspect(), ((), (), ()))

    def test_homes_that_disable_lack_or_configure_the_plugin_differently_are_left_alone(self):
        with Home() as home:
            kit = Fixture(home)
            self.assertEqual(kit.inspect(), ((), (), ()))
            old = kit.install('1.0.0')
            kit.config.write_text('[plugins."kit@personal"]\nenabled = false\n\n' + MARKETPLACE)
            self.assertEqual(kit.inspect(), ((), (), ()))
            kit.config.write_text('[plugins."kit@personal"]\nenabled = true\n\n' + MARKETPLACE.replace('/home/user', '/elsewhere'))
            mirrors, differences, notes = kit.inspect()
            self.assertEqual((mirrors, differences), ((), ()))
            self.assertIn('configures the personal marketplace differently', notes[0].detail)
            self.assertTrue(old.is_dir())

    def test_a_cache_shared_with_codex_home_is_skipped_and_other_links_are_local_edits(self):
        for canonical in (True, False):
            with self.subTest(canonical=canonical), Home() as home:
                kit = Fixture(home)
                elsewhere = home.roots.home / 'other-cache'
                shutil.copytree(home.roots.codex_home / 'plugins/cache', elsewhere)
                (kit.profile / 'plugins').mkdir()
                (kit.profile / 'plugins/cache').symlink_to(home.roots.codex_home / 'plugins/cache' if canonical else elsewhere)
                mirrors, differences, _ = kit.inspect()
                self.assertEqual(mirrors, ())
                if canonical:
                    self.assertEqual(differences, ())
                else:
                    self.assertEqual(differences[0].kind.value, 'local-edit')
                    self.assertIn('Path leaves its source', differences[0].detail)

    def test_changes_after_planning_and_an_unverified_codex_home_copy_stop_before_any_move(self):
        for change in ('settings', 'home copy', 'codex home copy'):
            with self.subTest(change), Home() as home:
                kit = Fixture(home)
                old = kit.install('1.0.0')
                mirrors, _, _ = kit.inspect()
                if change == 'settings':
                    kit.config.write_text('[plugins."kit@personal"]\nenabled = false\n\n' + MARKETPLACE)
                elif change == 'home copy':
                    home.write(old / 'late.txt', 'written during sync')
                else:
                    home.write(kit.canonical / 'skills/publish/SKILL.md', 'edited in ~/.codex')
                with self.assertRaises(ConcurrentEdit):
                    kit.apply(mirrors)
                self.assertTrue(old.is_dir())
                self.assertFalse(kit.target.exists())

    def test_trusted_hooks_need_approval_in_each_home(self):
        with Home() as home:
            kit = Fixture(home, hooks=Hooks.TRUSTED)
            kit.install('1.0.0')
            kit.apply(kit.inspect(hooks=True)[0])
            _, differences, _ = kit.inspect(hooks=True)
            self.assertEqual([d.kind.value for d in differences], ['approval'])
            self.assertIn('approve 1 kit@personal hooks in this Orca home', differences[0].detail)
            kit.trusted = frozenset({HOOK})
            self.assertEqual(kit.inspect(hooks=True), ((), (), ()))
            kit.trusted = Unread('Native hooks/list timed out')
            self.assertEqual([d.kind.value for d in kit.inspect(hooks=True)[1]], ['incomplete'])


if __name__ == '__main__':
    unittest.main()
