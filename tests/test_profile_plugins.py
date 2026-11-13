from dataclasses import replace
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from homes import Home, hold_built_trees_once
from accepted_installations import image
from description import CodexPlacement, EngineInstall, Hooks, LocalSource, Lock, LockedTree, Package, Roots, Setup
from description import MarketplaceInstall, ToolNotes, Visibility
from host_observation import Unread
from host_state import Host, Platform
import portable
import profile_plugins
from sync_files import ConcurrentEdit, Files
import sync_hosts

VERSION = '2.0.0+explicit.abc'
ACCOUNT_HOMES = ('.local/share/orca/codex-accounts/*/home',)
MARKETPLACE = '[marketplaces.personal]\nsource_type = "local"\nsource = "/home/user"\n'
HOOK = 'kit@personal:hooks/hooks.json:session_start:0:0'
SWITCHED_OFF = (('off', '[plugins."kit@personal"]\nenabled = false\n\n'), ('not mentioned', ''))
SAVED = 'backup/account-homes/.local/share/orca/codex-accounts/account/home/personal/kit'


def hold_moves_into_the_backup_once(case, backup, meanwhile=None):
    """hold_built_trees_once for the moves of a home's copies into backup, which its rule does not match: the first
    move of each copy is refused the way Windows refuses it while another process holds the folder. Returns the
    copies refused so far. meanwhile, given the refused copy, does what another process does during the wait."""
    refused, waiting, device, sleep, rename = [], [], portable.WINDOWS, portable.time.sleep, os.rename
    case.addCleanup(setattr, portable, 'WINDOWS', device)

    def attempt(source, target):
        source = Path(source)
        if Path(target).is_relative_to(backup) and source not in refused:
            refused.append(source)
            portable.WINDOWS = True
            waiting.append(source)
            raise PermissionError(errno.EACCES, 'Access is denied', str(source))
        return rename(source, target)

    def wait(seconds):
        if not waiting:
            return sleep(seconds)
        portable.WINDOWS = device
        source = waiting.pop()
        if meanwhile:
            meanwhile(source)

    for patcher in (patch.object(portable.time, 'sleep', wait), patch('os.rename', attempt)):
        patcher.start()
        case.addCleanup(patcher.stop)
    return refused


def refuse_moves_into(backup, code, refused):
    """A patch of os.rename that refuses every move into backup with the OSError of errno code, and records the
    copies it refused."""
    rename = os.rename

    def attempt(source, target):
        if Path(target).is_relative_to(backup):
            refused.append(Path(source))
            raise OSError(code, os.strerror(code), str(source))
        return rename(source, target)
    return patch('os.rename', attempt)


class Fixture:
    """A ~/.codex with an accepted kit copy, the catalog export Codex installs from, and one account home."""

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

    def keep_current(self, hooks=Hooks.OFF):
        package = replace(self.setup.packages['kit'],
                          codex=CodexPlacement((EngineInstall(False, keep_current=True),), hooks=hooks))
        self.setup = replace(self.setup, packages={'kit': package})

    def install(self, version='1.0.0', source=None):
        shutil.copytree(source or self.native, self.cache / version)
        return self.cache / version

    def inspect(self, *, hooks=False, patterns=ACCOUNT_HOMES):
        return profile_plugins.inspect(self.setup, self.home.roots, patterns, self.accepted,
                                       self.approved if hooks else None)

    def apply(self, mirrors, backup='backup'):
        files = Files(self.home.roots.home / backup)
        profile_plugins.apply(mirrors, self.home.roots, self.accepted, files)
        return files


class ProfilePluginsTest(unittest.TestCase):
    def test_a_home_the_device_file_does_not_list_is_left_alone(self):
        with Home() as home:
            kit = Fixture(home, hooks=Hooks.TRUSTED)
            kit.install('1.0.0')
            before = image(kit.cache)
            self.assertEqual(kit.inspect(hooks=True, patterns=()), ((), (), ()))
            self.assertEqual(kit.inspect(hooks=True, patterns=('.local/share/other/*/home',)), ((), (), ()))
            self.assertEqual(image(kit.cache), before)

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
            saved = home.roots.home / SAVED / '1.0.0'
            self.assertEqual((saved / 'my-note.txt').read_text(), 'an edit in a version Codex no longer loads')
            self.assertEqual(kit.config.read_bytes(), settings)
            self.assertEqual([p.name for p in kit.cache.iterdir()], [VERSION])
            self.assertEqual(kit.inspect(), ((), (), ()))

    def test_native_refresh_and_partly_overlaid_copies_are_replaced_but_edits_are_preserved(self):
        for case in ('native', 'partly overlaid', 'edited', 'extra file', 'extra file of a kept-current plugin set off'):
            with self.subTest(case), Home() as home:
                kit = Fixture(home)
                target = kit.install(VERSION)
                if case == 'partly overlaid':
                    shutil.copy(kit.canonical / 'skills/publish/SKILL.md', target / 'skills/publish/SKILL.md')
                if case == 'edited':
                    home.write(target / 'skills/publish/SKILL.md', 'my edit')
                if case.startswith('extra file'):
                    home.write(target / 'skills/publish/notes.md', 'mine')
                if case.endswith('set off'):
                    kit.keep_current()
                    kit.config.write_text(SWITCHED_OFF[0][1] + MARKETPLACE)
                before = image(target)
                mirrors, differences, _ = kit.inspect()
                if case.startswith(('edited', 'extra file')):
                    self.assertEqual((mirrors, [d.kind.value for d in differences]), ((), ['local-edit']))
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

    def test_a_copy_that_already_matched_is_replaced_when_a_release_replaces_the_accepted_copy_in_place(self):
        for case in ('unchanged', 'edited', 'no backup'):
            with self.subTest(case), Home() as home:
                kit = Fixture(home)
                shutil.copytree(kit.canonical, kit.target)
                self.assertEqual(kit.inspect(), ((), (), ()))
                # The release moved the copy aside, as accepted_installations records in the receipt's backup.
                displaced = kit.canonical.parent / f'.sync-{VERSION}-{"0" * 32}/displaced'
                shutil.copytree(kit.canonical, displaced)
                home.write(kit.canonical / 'skills/publish/SKILL.md', '---\nname: publish\ndescription: Released.\n---\nBody.\n')
                kit.record = {'path': str(kit.canonical), 'image': image(kit.canonical)}
                if case != 'no backup':
                    kit.record['backup'] = str(displaced)
                if case == 'edited':
                    home.write(kit.target / 'skills/publish/notes.md', 'mine')
                before = image(kit.target)
                mirrors, differences, _ = kit.inspect()
                if case != 'unchanged':
                    self.assertEqual((mirrors, differences[0].kind.value), ((), 'local-edit'))
                    self.assertIn('the copy it replaced', differences[0].detail)
                    self.assertEqual(image(kit.target), before)
                    continue
                self.assertEqual([d.kind.value for d in differences], ['tree'])
                kit.apply(mirrors)
                self.assertIn('Released.', (kit.target / 'skills/publish/SKILL.md').read_text())
                self.assertEqual(kit.inspect(), ((), (), ()))

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
            operation.setup, operation.roots = replace(kit.setup, account_homes={Platform.LINUX: ACCOUNT_HOMES}), home.roots
            operation.device = SimpleNamespace(platform=Platform.LINUX)
            operation.current = SimpleNamespace(profile_mirrors=planned)
            operation.files = Files(home.roots.home / 'backup')
            operation.owner = SimpleNamespace(preflight=lambda prepared: None, apply=release, accepted=kit.accepted)
            operation.prepared = SimpleNamespace(artifacts=('kit',))
            for step in ('_marketplaces', '_runtime', '_catalogs', '_plugins', '_skills'):
                setattr(operation, step, lambda: None)
            operation.run()
            self.assertIn('Released.', (kit.target / 'skills/publish/SKILL.md').read_text())
            self.assertEqual(kit.inspect(), ((), (), ()))

    def test_a_kept_current_plugin_a_home_has_on_is_mirrored_there(self):
        with Home() as home:
            kit = Fixture(home)
            kit.keep_current()
            kit.install('1.0.0')
            mirrors, differences, notes = kit.inspect()
            self.assertEqual((len(mirrors), [d.kind.value for d in differences], notes), (1, ['tree'], ()))
            kit.apply(mirrors)
            self.assertEqual(image(kit.target), kit.record['image'])

    def test_a_kept_current_plugin_a_home_has_off_or_does_not_mention_is_mirrored_there_and_stays_off(self):
        for case, switch in SWITCHED_OFF:
            with self.subTest(case), Home() as home:
                kit = Fixture(home)
                kit.keep_current()
                kit.config.write_text(switch + MARKETPLACE)
                kit.install('1.0.0')
                mirrors, differences, notes = kit.inspect()
                self.assertEqual((len(mirrors), [d.kind.value for d in differences], notes), (1, ['tree'], ()))
                kit.apply(mirrors)
                self.assertEqual(image(kit.target), kit.record['image'])
                self.assertEqual(kit.config.read_text(), switch + MARKETPLACE)

    def test_a_kept_current_plugin_is_not_installed_in_a_home_that_lacks_its_folder(self):
        for case, switch in SWITCHED_OFF:
            with self.subTest(case), Home() as home:
                kit = Fixture(home)
                kit.keep_current()
                kit.config.write_text(switch + MARKETPLACE)
                self.assertEqual(kit.inspect(), ((), (), ()))
                self.assertFalse(kit.cache.exists())

    def test_a_kept_current_plugin_a_home_has_off_asks_that_home_for_no_hook_approval(self):
        with Home() as home:
            kit = Fixture(home, hooks=Hooks.TRUSTED)
            kit.keep_current(Hooks.TRUSTED)
            shutil.copytree(kit.canonical, kit.target)
            kit.config.write_text(SWITCHED_OFF[0][1] + MARKETPLACE)
            self.assertEqual(kit.inspect(hooks=True), ((), (), ()))
            kit.config.write_text('[plugins."kit@personal"]\nenabled = true\n\n' + MARKETPLACE)
            self.assertEqual([d.kind.value for d in kit.inspect(hooks=True)[1]], ['approval'])

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
            marketplace = replace(kit.setup.packages['kit'], codex=CodexPlacement((MarketplaceInstall('personal', True),)))
            kit.setup = replace(kit.setup, packages={'kit': marketplace})
            kit.config.write_text('[plugins."kit@personal"]\nenabled = false\n\n' + MARKETPLACE)
            self.assertEqual(kit.inspect(), ((), (), ()))

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
                    kit.keep_current()
                    kit.config.write_text(MARKETPLACE)
                    self.assertEqual(kit.inspect(), ((), (), ()))

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

    def test_a_copy_that_fails_leaves_no_work_folder_in_the_home(self):
        with Home() as home:
            kit = Fixture(home)
            kit.install('1.0.0')
            mirrors, _, _ = kit.inspect()
            home.write(home.roots.home / 'backup/account-homes', 'a file where the backup folder goes')
            with self.assertRaises(FileExistsError if sys.platform == 'win32' else NotADirectoryError):
                kit.apply(mirrors)
            self.assertEqual([path.name for path in kit.cache.iterdir()], ['1.0.0'])

    def test_a_copy_that_fails_after_the_old_tree_moved_puts_it_back_and_leaves_no_work_folder_in_the_home(self):
        with Home() as home:
            kit = Fixture(home)
            old = kit.install('1.0.0')
            before = image(old)
            mirrors, _, _ = kit.inspect()
            rename = Path.rename

            def stop(path, target):
                if path.name == 'staged':
                    raise RuntimeError('stopped at the rename')
                return rename(path, target)

            with patch.object(Path, 'rename', stop), self.assertRaisesRegex(RuntimeError, 'stopped at the rename'):
                kit.apply(mirrors)
            self.assertEqual([path.name for path in kit.cache.iterdir()], ['1.0.0'])
            self.assertEqual(image(old), before)
            self.assertEqual(json.loads((home.roots.home / SAVED / 'moved.json').read_text()), [])
            self.assertFalse((home.roots.home / SAVED / '1.0.0').exists())
            mirrors, differences, _ = kit.inspect()
            self.assertEqual([d.kind.value for d in differences], ['tree'])
            kit.apply(mirrors, 'second-backup')
            self.assertEqual(image(kit.target), kit.record['image'])

    def test_a_staged_copy_edited_while_windows_refuses_a_move_into_the_backup_does_not_reach_the_home(self):
        with Home() as home:
            kit = Fixture(home)
            native, old = kit.install(VERSION), kit.install('1.0.0')
            before = {native: image(native), old: image(old)}
            mirrors, _, _ = kit.inspect()

            def edit(copy):
                if copy == old:
                    [stage] = kit.cache.glob('.sync-*/staged')
                    home.write(stage / 'late.txt', 'written during sync')

            refused = hold_moves_into_the_backup_once(self, home.roots.home / 'backup', meanwhile=edit)
            with self.assertRaisesRegex(ConcurrentEdit, r'^Staged copy failed verification: .*staged$'):
                kit.apply(mirrors)
            self.assertEqual(refused, [native, old])
            self.assertEqual({path: image(path) for path in before}, before)

    def test_each_move_windows_refuses_once_succeeds_on_the_retry(self):
        with Home() as home:
            kit = Fixture(home)
            native, old = kit.install(VERSION), kit.install('1.0.0')
            home.write(old / 'my-note.txt', 'an edit in a version Codex no longer loads')
            images = {VERSION: image(native), '1.0.0': image(old)}
            mirrors, _, _ = kit.inspect()
            into_place = hold_built_trees_once(self)
            into_backup = hold_moves_into_the_backup_once(self, home.roots.home / 'backup')
            kit.apply(mirrors)
            self.assertEqual((into_backup, into_place), ([kit.target, old], [kit.target]))
            self.assertEqual(image(kit.target), kit.record['image'])
            self.assertEqual([path.name for path in kit.cache.iterdir()], [VERSION])
            self.assertEqual({name: image(home.roots.home / SAVED / name) for name in images}, images)
            self.assertEqual(json.loads((home.roots.home / SAVED / 'moved.json').read_text()),
                             [{'from': str(path), 'to': str(home.roots.home / SAVED / path.name)}
                              for path in (native, old)])

    def test_an_old_copy_edited_while_windows_refuses_its_move_into_the_backup_stays_in_the_home(self):
        with Home() as home:
            kit = Fixture(home)
            old = kit.install('1.0.0')
            mirrors, _, _ = kit.inspect()
            hold_moves_into_the_backup_once(self, home.roots.home / 'backup',
                                            meanwhile=lambda copy: home.write(copy / 'late.txt', 'written during sync'))
            with self.assertRaisesRegex(ConcurrentEdit,
                                        f'^Account home plugin changed during sync: {re.escape(str(old))}$'):
                kit.apply(mirrors)
            self.assertEqual((old / 'late.txt').read_text(), 'written during sync')
            self.assertTrue((old / 'skills/publish/SKILL.md').is_file())
            self.assertFalse((home.roots.home / SAVED / '1.0.0').exists())
            self.assertFalse(kit.target.exists())

    def test_a_folder_that_appears_where_windows_refuses_the_move_into_place_is_not_replaced(self):
        with Home() as home:
            kit = Fixture(home)
            kit.install('1.0.0')
            mirrors, _, _ = kit.inspect()

            def appear(target):
                target.mkdir()
                (target / 'theirs.txt').write_text('made during the wait\n')

            held = hold_built_trees_once(self, meanwhile=appear)
            with self.assertRaisesRegex(ConcurrentEdit,
                                        f'^Account home plugin changed during sync: {re.escape(str(kit.target))}$'):
                kit.apply(mirrors)
            self.assertEqual(held, [kit.target])
            self.assertEqual([path.name for path in kit.target.iterdir()], ['theirs.txt'])

    def test_a_staged_copy_edited_while_windows_refuses_the_move_into_place_does_not_reach_the_home(self):
        with Home() as home:
            kit = Fixture(home)
            kit.install('1.0.0')
            mirrors, _, _ = kit.inspect()

            def edit(target):
                [stage] = target.parent.glob('.sync-*/staged')
                home.write(stage / 'late.txt', 'written during sync')

            hold_built_trees_once(self, meanwhile=edit)
            with self.assertRaisesRegex(ConcurrentEdit, r'^Staged copy failed verification: .*staged$'):
                kit.apply(mirrors)
            self.assertFalse(kit.target.exists())

    def test_a_later_copy_edited_while_windows_refuses_an_earlier_one_stops_the_sync_with_both_in_the_home(self):
        with Home() as home:
            kit = Fixture(home)
            native, old = kit.install(VERSION), kit.install('1.0.0')
            before = image(native)
            mirrors, _, _ = kit.inspect()
            refused = hold_moves_into_the_backup_once(
                self, home.roots.home / 'backup',
                meanwhile=lambda copy: copy == native and home.write(old / 'late.txt', 'written during sync'))
            refused.append(old)  # so that the later copy's move is tried once, on the read before it
            with self.assertRaisesRegex(ConcurrentEdit,
                                        f'^Account home plugin changed during sync: {re.escape(str(old))}$'):
                kit.apply(mirrors)
            self.assertEqual((old / 'late.txt').read_text(), 'written during sync')
            self.assertEqual(image(native), before)
            self.assertEqual(json.loads((home.roots.home / SAVED / 'moved.json').read_text()), [])

    def test_a_folder_that_appears_at_the_target_keeps_the_copy_it_displaced_in_the_backup_by_name(self):
        for case in ('during a move into the backup', 'during the move into place'):
            with self.subTest(case), Home() as home:
                kit = Fixture(home)
                native, old = kit.install(VERSION), kit.install('1.0.0')
                images = {native: image(native), old: image(old)}
                mirrors, _, _ = kit.inspect()
                saved = home.roots.home / SAVED / VERSION
                if case == 'during the move into place':
                    hold_built_trees_once(self, meanwhile=lambda target: target.mkdir())
                else:
                    hold_moves_into_the_backup_once(self, home.roots.home / 'backup',
                                                    meanwhile=lambda copy: copy == old and kit.target.mkdir())
                with (patch('sys.stderr', new_callable=io.StringIO) as stderr,
                      self.assertRaisesRegex(ConcurrentEdit,
                                             f'^Account home plugin changed during sync: {re.escape(str(kit.target))}$')):
                    kit.apply(mirrors)
                self.assertEqual(list(kit.target.iterdir()), [])
                self.assertEqual((image(old), image(saved)), (images[old], images[native]))
                self.assertEqual(json.loads((home.roots.home / SAVED / 'moved.json').read_text()),
                                 [{'from': str(native), 'to': str(saved)}])
                self.assertEqual(stderr.getvalue(), f'Account home copy left in the sync backup: {saved}, from '
                                                    f'{native}: its place in the home is taken\n')

    def moves_out_of_the_backup_refused(self, home, times, meanwhile=None):
        """A patch of os.rename that refuses that many moves out of the sync backup, on a device that counts as
        Windows and waits no time. meanwhile, given the refused copy, acts during the wait."""
        rename, left, waiting = os.rename, [times], []

        def attempt(source, target):
            if Path(source).is_relative_to(home.roots.home / 'backup') and left[0]:
                left[0] -= 1
                waiting.append(Path(source))
                raise PermissionError(errno.EACCES, 'Access is denied', str(source))
            return rename(source, target)

        def wait(seconds):
            if waiting and meanwhile:
                meanwhile(waiting.pop())

        stack = contextlib.ExitStack()
        for patcher in (patch.object(portable, 'WINDOWS', True), patch.object(portable.time, 'sleep', wait),
                        patch('os.rename', attempt)):
            stack.enter_context(patcher)
        return stack

    def stopped_at_the_move_into_place(self, kit, mirrors, error=RuntimeError):
        rename = Path.rename

        def stop(path, target):
            if path.name == 'staged':
                raise error('stopped at the rename')
            return rename(path, target)

        with patch.object(Path, 'rename', stop), self.assertRaisesRegex(error, 'stopped at the rename'):
            kit.apply(mirrors)

    def test_an_interrupt_at_the_move_into_place_puts_the_old_copy_back(self):
        with Home() as home:
            kit = Fixture(home)
            old = kit.install('1.0.0')
            before = image(old)
            self.stopped_at_the_move_into_place(kit, kit.inspect()[0], KeyboardInterrupt)
            self.assertEqual(image(old), before)

    def test_an_interrupt_as_a_move_into_the_backup_returns_puts_every_old_copy_back(self):
        for versions in (('1.0.0',), (VERSION, '1.0.0')):
            with self.subTest(versions=versions), Home() as home:
                kit = Fixture(home)
                before = {copy: image(copy) for copy in map(kit.install, versions)}
                mirrors, _, _ = kit.inspect()
                rename = os.rename

                # Python raises an interrupt that arrives during os.rename once the call returns, with the move done.
                def interrupted(source, target):
                    rename(source, target)
                    if Path(target).is_relative_to(home.roots.home / 'backup') and Path(source).name == '1.0.0':
                        raise KeyboardInterrupt

                with patch('os.rename', interrupted), self.assertRaises(KeyboardInterrupt):
                    kit.apply(mirrors)
                self.assertEqual({copy: image(copy) for copy in before}, before)
                self.assertEqual(json.loads((home.roots.home / SAVED / 'moved.json').read_text()), [])

    def test_a_move_back_that_windows_refuses_once_succeeds_on_the_retry(self):
        with Home() as home:
            kit = Fixture(home)
            old = kit.install('1.0.0')
            before = image(old)
            mirrors, _, _ = kit.inspect()
            with self.moves_out_of_the_backup_refused(home, 1):
                self.stopped_at_the_move_into_place(kit, mirrors)
            self.assertEqual(image(old), before)
            self.assertEqual(json.loads((home.roots.home / SAVED / 'moved.json').read_text()), [])

    def test_a_copy_that_cannot_be_moved_back_stays_whole_in_the_backup_under_its_name(self):
        for case in ('refused to the end', 'changed in the backup during a refusal'):
            with self.subTest(case), Home() as home:
                kit = Fixture(home)
                old = kit.install('1.0.0')
                before = image(old)
                mirrors, _, _ = kit.inspect()
                saved = home.roots.home / SAVED / '1.0.0'
                edit = (lambda copy: home.write(copy / 'late.txt', 'written during sync')) if 'changed' in case else None
                with (patch('sys.stderr', new_callable=io.StringIO) as stderr,
                      self.moves_out_of_the_backup_refused(home, 1 if edit else 20, edit)):
                    self.stopped_at_the_move_into_place(kit, mirrors)
                self.assertEqual(list(kit.cache.iterdir()), [])
                self.assertEqual({**image(saved)['entries'], **before['entries']}, image(saved)['entries'])
                self.assertEqual(json.loads((home.roots.home / SAVED / 'moved.json').read_text()),
                                 [{'from': str(old), 'to': str(saved)}])
                reason = (f'Account home plugin changed during sync: {saved}' if edit else
                          f'[Errno {errno.EACCES}] Access is denied: {str(saved)!r}')
                self.assertEqual(stderr.getvalue(),
                                 f'Account home copy left in the sync backup: {saved}, from {old}: {reason}\n')
                self.assertEqual(kit.inspect(), ((), (), ()))

    def test_a_second_interrupt_during_the_moves_back_leaves_moved_json_naming_the_copies_still_in_the_backup(self):
        for case in ('a later copy changed before moved.json was written', 'the move into place failed'):
            with self.subTest(case), Home() as home:
                kit = Fixture(home)
                native, old = kit.install(VERSION), kit.install('1.0.0')
                mirrors, _, _ = kit.inspect()
                early = case.startswith('a later copy')
                left = native if early else old
                saved = home.roots.home / SAVED / left.name
                rename = os.rename

                def attempt(source, target):
                    if Path(source) == saved:
                        raise PermissionError(errno.EACCES, 'Access is denied', str(source))
                    if Path(source).name == 'staged':
                        raise RuntimeError('stopped at the rename')
                    rename(source, target)
                    if early and Path(source) == native:
                        home.write(old / 'late.txt', 'written during sync')

                def interrupt(seconds):
                    raise KeyboardInterrupt

                with (patch.object(portable, 'WINDOWS', True), patch.object(portable.time, 'sleep', interrupt),
                      patch('os.rename', attempt), self.assertRaises(KeyboardInterrupt)):
                    kit.apply(mirrors)
                self.assertEqual(sorted(path.name for path in saved.parent.iterdir()), [left.name, 'moved.json'])
                self.assertEqual(json.loads((saved.parent / 'moved.json').read_text()),
                                 [{'from': str(left), 'to': str(saved)}])

    def test_part_of_a_copy_in_a_backup_on_another_file_system_is_not_moved_into_the_home(self):
        with Home() as home:
            kit = Fixture(home)
            old = kit.install('1.0.0')
            mirrors, _, _ = kit.inspect()
            saved = home.roots.home / SAVED / '1.0.0'
            copytree = shutil.copytree

            # Across file systems shutil.move copies and then deletes. This copy stops part-way, as when another
            # process removes the home's copy meanwhile.
            def stopped(source, target, *args, **kwargs):
                copytree(source, target, *args, **kwargs)
                if Path(target) != saved:
                    return target
                (saved / 'skills/publish/SKILL.md').unlink()
                shutil.rmtree(source)
                raise shutil.Error([(str(source), str(target), 'the source vanished part-way')])

            with (patch('sys.stderr', new_callable=io.StringIO) as stderr, patch.object(shutil, 'copytree', stopped),
                  refuse_moves_into(home.roots.home / 'backup', errno.EXDEV, []), self.assertRaises(shutil.Error)):
                kit.apply(mirrors)
            self.assertFalse(old.exists())
            self.assertTrue(saved.is_dir())
            self.assertEqual(json.loads((saved.parent / 'moved.json').read_text()), [{'from': str(old), 'to': str(saved)}])
            self.assertEqual(stderr.getvalue(), f'Account home copy left in the sync backup: {saved}, from {old}: '
                                                f'Account home plugin changed during sync: {saved}\n')

    def test_a_refused_move_into_the_backup_that_is_not_retried_leaves_the_old_copy_whole_where_it_was(self):
        with Home() as home:
            kit = Fixture(home)
            old = kit.install('1.0.0')
            before = image(old)
            mirrors, _, _ = kit.inspect()
            refused = []
            with (patch.object(portable, 'WINDOWS', False), self.assertRaises(PermissionError),
                  refuse_moves_into(home.roots.home / 'backup', errno.EACCES, refused)):
                kit.apply(mirrors)
            self.assertEqual(refused, [old])
            self.assertEqual(image(old), before)
            self.assertFalse((home.roots.home / SAVED / '1.0.0').exists())

    def test_a_backup_on_another_file_system_still_receives_the_old_copy(self):
        with Home() as home:
            kit = Fixture(home)
            old = kit.install('1.0.0')
            before = image(old)
            mirrors, _, _ = kit.inspect()
            refused = []
            with refuse_moves_into(home.roots.home / 'backup', errno.EXDEV, refused):
                kit.apply(mirrors)
            self.assertEqual(set(refused), {old})
            self.assertFalse(old.exists())
            self.assertEqual(image(home.roots.home / SAVED / '1.0.0'), before)
            self.assertEqual(image(kit.target), kit.record['image'])

    def test_trusted_hooks_need_approval_in_each_home(self):
        with Home() as home:
            kit = Fixture(home, hooks=Hooks.TRUSTED)
            kit.install('1.0.0')
            kit.apply(kit.inspect(hooks=True)[0])
            _, differences, _ = kit.inspect(hooks=True)
            self.assertEqual([d.kind.value for d in differences], ['approval'])
            self.assertIn('approve 1 kit@personal hooks in this account home', differences[0].detail)
            kit.trusted = frozenset({HOOK})
            self.assertEqual(kit.inspect(hooks=True), ((), (), ()))
            kit.trusted = Unread('Native hooks/list timed out')
            self.assertEqual([d.kind.value for d in kit.inspect(hooks=True)[1]], ['incomplete'])


if __name__ == '__main__':
    unittest.main()
