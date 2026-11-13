from dataclasses import replace
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import accepted_installations
from codex_host import CodexState
from configuration import desired
from description import (Adaptation, AlignedInstall, ClaudePlacement, CodexPlacement, EngineInstall, Hooks, LocalSource, Lock, LockedTree,
                         LooseSkill, NoteRule, Package, PluginRoles, Repo, RepoSource, Roots, Rules, Setup, SkillRoot,
                         ToolNotes, Visibility, intent)
from device import Device
from device_sync import apply, plan
from homes import hold_built_trees_once, isolate_git
from host_observation import Observer, Unread
from host_state import (Approved, DeviceRoots, EngineMarketplace, FactKey, FileValue, Host, InstallDetail,
                        Platform, PluginValue, Released, SkillMode, Surface, TreeValue, Unknown, bytes_digest)
from sync_files import ConcurrentEdit
from sync_sources import BuildError, Builder, Sources


def setUpModule():
    isolate_git()


class DiskHost:
    host = Host.CODEX
    project = CodexState.project

    def __init__(self, roots):
        self.roots, self.installed, self.approved = roots, {}, True

    def observe(self, request):
        seen = Observer(self.host, self.roots)
        if (self.roots.agents_home / 'plugins/marketplace.json').is_file():
            seen.add(Surface.MARKETPLACE, 'personal', EngineMarketplace())
        config = self.roots.codex_home / 'config.toml'
        switches = tomllib.loads(config.read_text()).get('plugins', {}) if config.is_file() else {}
        for identity, (version, directory) in self.installed.items():
            enabled = switches.get(identity, {}).get('enabled', True)
            seen.add(Surface.PLUGIN, identity, PluginValue(enabled, Released(version)),
                     detail=InstallDetail(version, directory))
            if directory.is_dir():
                seen.add(Surface.TREE, identity, TreeValue(self.roots.tree_digest(directory)))
            if self.approved:
                seen.add(Surface.HOOK, identity, Approved(1))
        for path in (self.roots.codex_home / 'agents').glob('*.toml'):
            seen.add(Surface.AGENT, path.name, FileValue(self.roots.file_digest(path)))
        for directory in (self.roots.codex_home / 'skills').glob('*'):
            if directory.is_dir():
                seen.add(Surface.LOOSE_SKILL, self.roots.tokens(directory), FileValue(self.roots.tree_digest(directory)))
        rules = self.roots.codex_home / 'AGENTS.md'
        if rules.is_file():
            seen.add(Surface.RULES, 'AGENTS.md', FileValue(bytes_digest(rules.read_bytes())))
        return seen.finish('disk test host', set(self.installed))


def own_lease(directory, start=None):
    """A Claude lease marker naming this test's process, which is running, with its own start time unless start
    gives another. macOS markers give the start as a date."""
    if start is None:
        start = (Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19] if sys.platform.startswith('linux')
                 else 'Thu Oct 15 12:30:00 2026')
    directory.mkdir(exist_ok=True)
    lease = directory / str(os.getpid())
    lease.write_text(json.dumps({'pid': os.getpid(), 'procStart': start}, separators=(',', ':')))
    return lease


class InstallationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        home = self.root / 'home'
        home.mkdir()
        self.roots = DeviceRoots(home, home / '.codex', home / '.claude', home / '.agents', self.root / 'engine')
        self.public = self.root / 'public'
        source = self.public / 'owned/kit'
        (source / '.codex-plugin').mkdir(parents=True)
        (source / '.codex-plugin/plugin.json').write_text('{"name":"kit","version":"1.0.0"}')
        (source / 'skills/one').mkdir(parents=True)
        (source / 'skills/one/SKILL.md').write_text('---\nname: one\ndescription: Use a tool.\n---\nUse Read now.\n')
        (source / 'ordinary.txt').write_text('ordinary release bytes\n')
        (source / 'agents').mkdir()
        self.role = b'model = "example-model"\n'
        (source / 'agents/reviewer.toml').write_bytes(self.role)
        self.source = source
        loose_source = self.public / 'owned/loose'
        loose_source.mkdir()
        (loose_source / 'SKILL.md').write_text('A loose skill.\n')
        package = Package('kit', Visibility.PUBLIC, '', LocalSource('owned/kit', self.roots.tree_digest(source)),
                          {}, ToolNotes.APPLY, CodexPlacement((EngineInstall(True),), roles=PluginRoles('agents')), None)
        loose = LooseSkill('loose', Visibility.PUBLIC, '', LocalSource('owned/loose', self.roots.tree_digest(loose_source)),
                           {SkillRoot.CODEX: True})
        locks = {'kit': Lock(intent(package, {}), {Host.CODEX: LockedTree('1.0.0', self.roots.tree_digest(source))},
                             {'reviewer.toml': bytes_digest(self.role)}),
                 'loose': Lock(intent(loose, {}), placements={SkillRoot.CODEX: self.roots.tree_digest(loose_source)})}
        self.setup = Setup(Roots(self.public / 'setup', None), {'kit': package}, {}, {'loose': loose}, {},
                           Rules(b'Managed rules.\n'), (NoteRule('Read', 'use the local reader'),), frozenset(), locks)
        self.host = DiskHost(self.roots)
        self.device = Device(Platform.LINUX, self.roots, (self.host,), ())
        self.calls = []
        self.interrupt = None
        self.seeds = ()

        def command(operation, host, *args):
            self.operation = operation   # kept alive, so a staged tree goes only when apply leaves the staging stack
            self.calls.append((host, args))
            if args[:2] == ('plugin', 'add'):
                release = operation.built['kit']
                destination = self.roots.codex_home / 'plugins/cache/personal/kit' / release.version
                if destination.exists():
                    shutil.rmtree(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(release.root, destination)
                self.host.installed[args[2]] = (release.version, destination)
                if self.interrupt:
                    self.interrupt(destination)
            return ''
        self.addCleanup(patch.stopall)
        patch('sync_hosts.Apply.command', command).start()

    def run_apply(self, current=None):
        return apply(self.setup, self.device, current or plan(self.setup, self.device),
                     builder=Builder(self.setup, self.roots, self.roots.engine,
                                     sources=Sources(self.roots.engine / 'sources', seeds=self.seeds)))[0]

    def installed(self):
        return self.host.installed['kit@personal'][1]

    def receipt_paths(self):
        return sorted((self.roots.codex_home / 'dotfiles/accepted').glob('*.json'))

    def snapshot(self):
        return {p.relative_to(self.root): (p.lstat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                for p in self.root.rglob('*')}

    def staged_images(self):
        scratch = self.roots.engine / '.sync/accepted-images'
        return sorted(path.name for path in scratch.iterdir()) if scratch.is_dir() else []

    def unnamed_work_folders(self):
        """The replacement work folders on the device that no receipt names as its backup or its pending swap."""
        named = set()
        for path in self.receipt_paths():
            receipt = json.loads(path.read_bytes())
            accepted, pending = receipt['accepted'] or {}, receipt['pending'] or {}
            named |= {Path(value).parent for value in
                      (accepted.get('backup'), pending.get('staged'), pending.get('displaced')) if value}
        return sorted(str(path.relative_to(self.roots.home)) for path in self.roots.home.rglob('.sync-*')
                      if path.is_dir() and path not in named)

    def revise_loose(self, text):
        source = self.public / 'owned/loose'
        (source / 'SKILL.md').write_text(text)
        digest = self.roots.tree_digest(source)
        skill = replace(self.setup.loose['loose'], source=LocalSource('owned/loose', digest))
        self.setup = replace(self.setup, loose={'loose': skill},
                             lock={**self.setup.lock, 'loose': Lock(intent(skill, {}), placements={SkillRoot.CODEX: digest})})

    def loose_backup(self):
        """Install, then replace the loose skill once, and give the tree that replacement moved aside."""
        self.assertEqual(self.run_apply().exit_code, 0)
        self.revise_loose('A second loose skill.\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        backup, = (self.roots.codex_home / 'skills').glob('.sync-loose-*/displaced')
        return backup

    def apply_reporting(self):
        """Run a sync that exits 0 and give what it printed to stderr."""
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assertEqual(self.run_apply().exit_code, 0)
        return stderr.getvalue()

    def test_a_sync_removes_the_plugin_trees_it_staged(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(self.staged_images(), [])

    def test_a_sync_that_fails_removes_the_plugin_trees_it_staged(self):
        def stop(destination):
            raise RuntimeError('the native install stopped')

        self.interrupt = stop
        with self.assertRaisesRegex(RuntimeError, 'the native install stopped'):
            self.run_apply()
        self.assertEqual(self.staged_images(), [])

    def test_a_sync_that_stops_while_preparing_removes_the_plugin_trees_it_staged(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        self.revise_loose('A second loose skill.\n')
        (self.roots.codex_home / 'skills/loose/SKILL.md').write_text('My local edit.\n')
        stopped = None
        try:
            self.run_apply()
        except ConcurrentEdit as error:
            stopped = error   # its traceback keeps the owner alive, so collecting the owner cannot remove the trees
        self.assertIn('Unverified existing content preserved: codex:LOOSE_SKILL', str(stopped))
        self.assertEqual(self.staged_images(), [])

    def test_replacements_leave_only_the_work_folder_a_receipt_names(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(self.unnamed_work_folders(), [])
        for text in ('A second loose skill.\n', 'A third loose skill.\n'):
            self.revise_loose(text)
            self.assertEqual(self.run_apply().exit_code, 0)
            self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), text)
            self.assertEqual(self.unnamed_work_folders(), [])
        backups = [path for path in (self.roots.codex_home / 'skills').glob('.sync-loose-*/displaced/SKILL.md')]
        self.assertEqual([path.read_text() for path in backups], ['A second loose skill.\n'])

    def test_a_replacement_stopped_after_its_swap_finishes_with_its_backup_named(self):
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        rename = Path.rename

        def crash(path, target):
            result = rename(path, target)
            if path.name == 'staged':
                raise RuntimeError('after the swap')
            return result

        with patch.object(Path, 'rename', crash), self.assertRaisesRegex(RuntimeError, 'after the swap'):
            self.run_apply()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(self.unnamed_work_folders(), [])
        backups = [path for path in (self.roots.codex_home / 'skills').glob('.sync-loose-*/displaced/SKILL.md')]
        self.assertEqual([path.read_text() for path in backups], ['A second loose skill.\n'])

    def test_a_plan_covers_only_the_account_homes_the_device_file_lists(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        profile = self.roots.home / '.local/share/orca/codex-accounts/account/home'
        shutil.copytree(self.installed(), profile / 'plugins/cache/personal/kit/0.9.0')
        (profile / 'config.toml').write_text('[plugins."kit@personal"]\nenabled = true\n')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the first reader'),))
        current = plan(self.setup, self.device)
        self.assertEqual((current.profile_mirrors, current.exit_code), ((), 1))
        self.assertEqual(self.run_apply(current).exit_code, 0)
        self.assertEqual([path.name for path in (profile / 'plugins/cache/personal/kit').iterdir()], ['0.9.0'])
        self.setup = replace(self.setup, account_homes={Platform.LINUX: ('.local/share/orca/codex-accounts/*/home',)})
        self.assertEqual([mirror.profile for mirror in plan(self.setup, self.device).profile_mirrors], [profile])

    def test_a_release_that_changes_only_intent_keeps_the_backup_an_account_home_is_matched_against(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        profile = self.roots.home / '.local/share/orca/codex-accounts/account/home'
        copy = profile / 'plugins/cache/personal/kit/1.0.0'
        shutil.copytree(self.installed(), copy)
        (profile / 'config.toml').write_text('[plugins."kit@personal"]\nenabled = true\n')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the first reader'),),
                             account_homes={Platform.LINUX: ('.local/share/orca/codex-accounts/*/home',)})
        with patch('profile_plugins.apply', side_effect=RuntimeError('before the account homes')), \
                self.assertRaisesRegex(RuntimeError, 'before the account homes'):
            self.run_apply()
        backup, = self.installed().parent.glob('.sync-1.0.0-*/displaced')
        self.setup = replace(self.setup, note_rules=(*self.setup.note_rules, NoteRule('Grep', 'a tool the kit never names')))
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the first reader', (copy / 'skills/one/SKILL.md').read_text())
        self.assertIn('use the local reader', (backup / 'skills/one/SKILL.md').read_text())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_backup_kept_through_a_release_that_changes_only_intent_is_still_checked_before_it_goes(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        backup, = self.installed().parent.glob('.sync-1.0.0-*/displaced')
        self.setup = replace(self.setup, note_rules=(*self.setup.note_rules, NoteRule('Grep', 'a tool the kit never names')))
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(json.loads(self.receipt_of('plugin:').read_bytes())['accepted']['backup'], str(backup))
        (backup / 'ordinary.txt').write_text('My edit in the backup.\n')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        self.assertIn(f'Work folder left in place: {backup.parent}: ', self.apply_reporting())
        self.assertEqual((backup / 'ordinary.txt').read_text(), 'My edit in the backup.\n')

    def test_a_backup_kept_through_a_release_that_changes_only_intent_goes_at_the_next_replacement(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        work, = self.installed().parent.glob('.sync-1.0.0-*')
        self.setup = replace(self.setup, note_rules=(*self.setup.note_rules, NoteRule('Grep', 'a tool the kit never names')))
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertTrue(work.exists())
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertFalse(work.exists())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_destination_that_already_holds_the_new_release_drops_the_backup_of_the_one_before(self):
        backup = self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        (self.roots.codex_home / 'skills/loose/SKILL.md').write_text('A third loose skill.\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertFalse(backup.parent.exists())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_work_folder_that_is_a_link_stays_with_the_tree_it_points_to(self):
        work = self.loose_backup().parent
        outside = self.root / 'outside'
        shutil.move(work, outside)
        work.symlink_to(outside, target_is_directory=True)
        self.revise_loose('A third loose skill.\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertTrue(work.is_symlink())
        self.assertEqual((outside / 'displaced/SKILL.md').read_text(), 'A loose skill.\n')

    def test_a_work_folder_that_holds_anything_else_stays(self):
        backup = self.loose_backup()
        note = backup.parent / 'note.txt'
        note.write_text('mine')
        self.revise_loose('A third loose skill.\n')
        self.assertEqual(self.apply_reporting(),
                         f'Work folder left in place: {backup.parent}: it holds more than the tree\n')
        self.assertEqual(note.read_text(), 'mine')
        self.assertEqual((backup / 'SKILL.md').read_text(), 'A loose skill.\n')

    def test_a_receipt_that_names_a_backup_outside_its_own_work_folders_removes_nothing(self):
        skills = self.loose_backup().parent.parent
        receipt = next(path for path in self.receipt_paths() if 'LOOSE_SKILL' in json.loads(path.read_bytes())['identity'])
        name = '.sync-loose-' + 'ab' * 16
        for case, named in (('under another folder', self.root / 'elsewhere' / name / 'displaced'),
                            ('of another skill', skills / ('.sync-other-' + 'ab' * 16) / 'displaced'),
                            ('with a short suffix', skills / '.sync-loose-abcd/displaced'),
                            ('holding another name', skills / name / 'kept')):
            with self.subTest(case):
                value = json.loads(receipt.read_bytes())
                # A copy of the recorded backup, so that only the folder's place and name keep it.
                shutil.copytree(value['accepted']['backup'], named)
                value['accepted']['backup'] = str(named)
                receipt.write_text(json.dumps(value))
                self.revise_loose(f'A loose skill {case}.\n')
                self.assertEqual(self.run_apply().exit_code, 0)
                self.assertTrue((named / 'SKILL.md').exists())

    def test_a_version_change_removes_the_backup_beside_the_old_version(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        old, = self.installed().parent.glob('.sync-1.0.0-*')
        (self.source / '.codex-plugin/plugin.json').write_text('{"name":"kit","version":"1.1.0"}')
        digest = self.roots.tree_digest(self.source)
        package = replace(self.setup.packages['kit'], source=LocalSource('owned/kit', digest))
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}), trees={Host.CODEX: LockedTree('1.1.0', digest)})
        self.setup = replace(self.setup, packages={'kit': package}, lock={**self.setup.lock, 'kit': lock})
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(self.installed().name, '1.1.0')
        self.assertFalse(old.exists())
        self.assertEqual(self.unnamed_work_folders(), [])

    def receipt_of(self, prefix):
        path, = (path for path in self.receipt_paths() if json.loads(path.read_bytes())['identity'].startswith(prefix))
        return path

    def test_a_version_that_changes_in_the_lock_alone_with_its_folder_in_place_drops_the_backup_beside_the_old(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        old = self.installed()
        work, = old.parent.glob('.sync-1.0.0-*')
        lock = replace(self.setup.lock['kit'],
                       trees={Host.CODEX: LockedTree('1.1.0', self.setup.lock['kit'].trees[Host.CODEX].digest)})
        self.setup = replace(self.setup, lock={**self.setup.lock, 'kit': lock})
        shutil.copytree(old, old.parent / '1.1.0')
        # The fixture's host goes on reporting 1.0.0 as installed, so the sync ends with that difference left.
        self.assertEqual(self.run_apply().exit_code, 1)
        accepted = json.loads(self.receipt_of('plugin:').read_bytes())['accepted']
        self.assertEqual((Path(accepted['path']).name, accepted.get('backup')), ('1.1.0', None))
        self.assertFalse(work.exists())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_destination_that_went_missing_is_created_again_without_the_backup_of_the_tree_before(self):
        backup = self.loose_backup()
        shutil.rmtree(self.roots.codex_home / 'skills/loose')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A second loose skill.\n')
        self.assertFalse(backup.parent.exists())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_backup_edited_after_its_swap_stays_when_its_receipt_stops_naming_it(self):
        backup = self.loose_backup()
        (backup / 'SKILL.md').write_text('My edit in the backup.\n')
        self.revise_loose('A third loose skill.\n')
        self.assertEqual(self.apply_reporting(),
                         f'Work folder left in place: {backup.parent}: its tree is not the one its record holds\n')
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A third loose skill.\n')
        self.assertEqual((backup / 'SKILL.md').read_text(), 'My edit in the backup.\n')
        self.assertEqual(self.unnamed_work_folders(), [str(backup.parent.relative_to(self.roots.home))])

    def test_a_file_where_the_backup_was_stays_and_is_reported(self):
        backup = self.loose_backup()
        shutil.rmtree(backup)
        backup.write_text('mine')
        self.revise_loose('A third loose skill.\n')
        self.assertEqual(self.apply_reporting(),
                         f'Work folder left in place: {backup.parent}: its tree is not the one its record holds\n')
        self.assertEqual(backup.read_text(), 'mine')

    def test_a_backup_that_gained_a_file_after_its_swap_stays(self):
        backup = self.loose_backup()
        (backup / 'mine.txt').write_text('mine')
        self.revise_loose('A third loose skill.\n')
        self.assertIn(f'Work folder left in place: {backup.parent}: ', self.apply_reporting())
        self.assertEqual((backup / 'mine.txt').read_text(), 'mine')

    def test_a_receipt_that_names_a_work_folder_holding_another_tree_removes_nothing(self):
        skills = self.loose_backup().parent.parent
        other = skills / ('.sync-loose-' + 'ab' * 16) / 'displaced'
        other.mkdir(parents=True)
        (other / 'SKILL.md').write_text('mine')
        receipt = self.receipt_of('codex:LOOSE_SKILL')
        value = json.loads(receipt.read_bytes())
        value['accepted']['backup'] = str(other)
        receipt.write_text(json.dumps(value))
        self.revise_loose('A third loose skill.\n')
        self.assertIn(f'Work folder left in place: {other.parent}: ', self.apply_reporting())
        self.assertEqual((other / 'SKILL.md').read_text(), 'mine')

    def test_a_backup_recorded_without_its_image_stays_and_is_reported(self):
        backup = self.loose_backup()
        receipt = self.receipt_of('codex:LOOSE_SKILL')
        value = json.loads(receipt.read_bytes())
        del value['accepted']['backup_image']
        receipt.write_text(json.dumps(value))
        self.revise_loose('A third loose skill.\n')
        self.assertEqual(self.apply_reporting(),
                         f'Work folder left in place: {backup.parent}: its record holds no image of the tree\n')
        self.assertEqual((backup / 'SKILL.md').read_text(), 'A loose skill.\n')
        self.assertEqual(self.unnamed_work_folders(), [str(backup.parent.relative_to(self.roots.home))])

    def test_a_backup_whose_recorded_image_is_no_image_of_a_tree_stays_and_is_reported(self):
        backup = self.loose_backup()
        receipt = self.receipt_of('codex:LOOSE_SKILL')
        for number, malformed in enumerate(({}, 'x', {'entries': {}}, {'kind': 'tree', 'entries': []})):
            with self.subTest(malformed=malformed):
                value = json.loads(receipt.read_bytes())
                value['accepted']['backup_image'] = malformed
                receipt.write_text(json.dumps(value))
                self.revise_loose(f'Loose skill {number}.\n')
                self.assertEqual(self.apply_reporting(), f'Work folder left in place: {backup.parent}: '
                                                         'its record holds no image of the tree\n')
                self.assertTrue((backup / 'SKILL.md').exists())
                backup = Path(json.loads(receipt.read_bytes())['accepted']['backup'])

    def test_a_backup_that_cannot_be_read_is_reported_and_the_rest_of_the_release_applies(self):
        backup = self.loose_backup()
        read = accepted_installations.image

        def refuse(path, *args, **kwargs):
            if path == backup:
                raise PermissionError(13, 'Permission denied', str(path))
            return read(path, *args, **kwargs)

        self.revise_loose('A third loose skill.\n')
        self.setup = replace(self.setup, rules=Rules(b'Revised rules.\n'))
        with patch('accepted_installations.image', refuse):
            printed = self.apply_reporting()
        self.assertEqual((self.roots.codex_home / 'AGENTS.md').read_text(), 'Revised rules.\n')
        self.assertTrue((backup / 'SKILL.md').exists())
        self.assertIn(f'Work folder left in place: {backup.parent}: ', printed)
        self.assertIn('Permission denied', printed)

    def refuse_removal_of(self, work, error):
        rmtree = shutil.rmtree

        def refuse(path, *args, **kwargs):
            if Path(path) == work:
                raise error
            return rmtree(path, *args, **kwargs)

        return patch('shutil.rmtree', refuse)

    def test_a_work_folder_that_cannot_be_removed_is_reported_and_the_rest_of_the_release_applies(self):
        work = self.loose_backup().parent
        self.revise_loose('A third loose skill.\n')
        self.setup = replace(self.setup, rules=Rules(b'Revised rules.\n'))
        with self.refuse_removal_of(work, PermissionError(13, 'Access is denied', str(work))), \
                patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A third loose skill.\n')
        self.assertEqual((self.roots.codex_home / 'AGENTS.md').read_text(), 'Revised rules.\n')
        self.assertEqual((work / 'displaced/SKILL.md').read_text(), 'A loose skill.\n')
        self.assertIn(f'Work folder left in place: {work}: ', stderr.getvalue())
        self.assertIn('Access is denied', stderr.getvalue())

    def test_a_run_that_stops_while_removing_the_folder_of_a_replacement_that_moved_nothing_aside_leaves_it_named(self):
        skills = self.roots.codex_home / 'skills'
        rmtree = shutil.rmtree

        def stop(path, *args, **kwargs):
            if Path(path).name.startswith('.sync-loose-'):
                raise RuntimeError('stopped during the removal')
            return rmtree(path, *args, **kwargs)

        with patch('shutil.rmtree', stop), self.assertRaisesRegex(RuntimeError, 'stopped during the removal'):
            self.run_apply()
        self.assertEqual(len(list(skills.glob('.sync-loose-*'))), 1)
        self.assertEqual(self.unnamed_work_folders(), [])
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(list(skills.glob('.sync-loose-*')), [])

    def test_a_run_that_stops_while_removing_the_old_backup_leaves_it_for_the_next_run_to_remove(self):
        work = self.loose_backup().parent
        self.revise_loose('A third loose skill.\n')
        with self.refuse_removal_of(work, RuntimeError('stopped during the removal')), \
                self.assertRaisesRegex(RuntimeError, 'stopped during the removal'):
            self.run_apply()
        self.assertEqual(self.unnamed_work_folders(), [])
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertFalse(work.exists())
        self.assertEqual(self.unnamed_work_folders(), [])
        backups = [path for path in (self.roots.codex_home / 'skills').glob('.sync-loose-*/displaced/SKILL.md')]
        self.assertEqual([path.read_text() for path in backups], ['A second loose skill.\n'])

    def test_a_removal_that_stopped_part_way_is_finished_by_the_next_run(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        work, = self.installed().parent.glob('.sync-1.0.0-*')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        rmtree = shutil.rmtree

        def stop_part_way(path, *args, **kwargs):
            if Path(path) != work:
                return rmtree(path, *args, **kwargs)
            (work / 'displaced/ordinary.txt').unlink()
            rmtree(work / 'displaced/skills')
            raise RuntimeError('stopped during the removal')

        with patch('shutil.rmtree', stop_part_way), self.assertRaisesRegex(RuntimeError, 'stopped during the removal'):
            self.run_apply()
        self.assertTrue((work / 'displaced/agents/reviewer.toml').exists())
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertFalse(work.exists())
        self.assertEqual(self.unnamed_work_folders(), [])

    def claude_backup_folder(self, note):
        """Replace the Claude cache in place with a release that carries note, and give the folder of its backup."""
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', note),))
        self.assertEqual(self.run_apply().exit_code, 0)
        return Path(json.loads(self._pending_claude_receipt().read_bytes())['accepted']['backup']).parent

    def test_a_claude_backup_folder_goes_with_a_valid_orphan_marker_and_stays_with_an_invalid_one(self):
        self._installed_claude()
        marked = self.claude_backup_folder('a first note')
        (marked / '.orphaned_at').write_text('1794063570885')
        invalid = self.claude_backup_folder('a second note')
        self.assertFalse(marked.exists())
        (invalid / '.orphaned_at').write_text('not a time')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'a third note'),))
        self.assertIn(f'Work folder left in place: {invalid}: Invalid Claude orphan marker', self.apply_reporting())
        self.assertEqual(sorted(path.name for path in invalid.iterdir()), ['.orphaned_at', 'displaced'])

    def test_a_claude_backup_that_holds_the_markers_of_sessions_goes_like_any_other(self):
        self._installed_claude()
        first = self.claude_backup_folder('a first note')
        (first / 'displaced/.in_use').mkdir()
        (first / 'displaced/.in_use/17').write_text('{"pid":17,"procStart":"Thu Oct 15 12:30:00 2026"}')
        self.claude_backup_folder('a second note')
        self.assertFalse(first.exists())

    def test_a_claude_backup_that_holds_a_file_beside_its_markers_stays_and_is_reported(self):
        self._installed_claude()
        first = self.claude_backup_folder('a first note')
        (first / 'displaced/.in_use').mkdir()
        (first / 'displaced/.in_use/notes.txt').write_text('mine')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'a second note'),))
        self.assertIn(f'Work folder left in place: {first}: ', self.apply_reporting())
        self.assertEqual((first / 'displaced/.in_use/notes.txt').read_text(), 'mine')

    def stage_removed_part_way(self):
        """Hold a Claude cache so that a first revision stays staged, then stop a second revision's run part-way
        through removing that stage. Gives the cache, the lease that holds it and the stage's work folder."""
        destination = self._installed_claude()
        lease = own_lease(destination / '.in_use')
        self.revise_release('first revision\n')
        self.assert_held(destination)
        work = Path(json.loads(self._pending_claude_receipt().read_bytes())['pending']['staged']).parent
        self.revise_release('second revision\n')
        rmtree = shutil.rmtree

        def stop_part_way(path, *args, **kwargs):
            if Path(path) != work:
                return rmtree(path, *args, **kwargs)
            (work / 'staged/ordinary.txt').unlink()
            raise RuntimeError('stopped during the removal')

        with patch('shutil.rmtree', stop_part_way), self.assertRaisesRegex(RuntimeError, 'stopped during the removal'):
            self.run_apply()
        return destination, lease, work

    def test_a_stage_removed_part_way_is_removed_by_the_next_run_of_the_release_that_took_over(self):
        destination, _, work = self.stage_removed_part_way()
        self.assert_held(destination)
        self.assertFalse(work.exists())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_stage_removed_part_way_is_built_again_for_the_release_it_was_staged_for(self):
        destination, lease, _ = self.stage_removed_part_way()
        self.revise_release('first revision\n')
        lease.unlink()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'first revision\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_stage_that_holds_an_edit_stops_its_release_and_keeps_the_edit(self):
        destination = self._installed_claude()
        lease = own_lease(destination / '.in_use')
        self.revise_release('first revision\n')
        self.assert_held(destination)
        edited = Path(json.loads(self._pending_claude_receipt().read_bytes())['pending']['staged']) / 'ordinary.txt'
        edited.write_text('edited in the stage\n')
        lease.unlink()
        with self.assertRaisesRegex(ConcurrentEdit, f'^Prepared stage was edited: {re.escape(str(edited.parent))}$'):
            self.run_apply()
        self.assertEqual(edited.read_text(), 'edited in the stage\n')

    def test_a_receipt_edited_to_name_a_folder_elsewhere_as_its_stage_stops_its_release_and_keeps_the_folder(self):
        destination = self._installed_claude()
        lease = own_lease(destination / '.in_use')
        self.revise_release('first revision\n')
        self.assert_held(destination)
        name = f'.sync-{destination.name}-{"ab" * 16}'
        for case, mine in (('outside the work folders', self.root / 'elsewhere/mine'),
                           ('under another folder', self.root / 'elsewhere' / name / 'staged'),
                           ('with a short suffix', destination.parent / f'.sync-{destination.name}-abcd/staged'),
                           ('under another name', destination.parent / name / 'displaced')):
            with self.subTest(case):
                mine.mkdir(parents=True)
                (mine / 'ordinary.txt').write_text('first revision\n')
                receipt = self._pending_claude_receipt()
                value = json.loads(receipt.read_bytes())
                value['pending']['staged'] = str(mine)
                receipt.write_text(json.dumps(value))
                lease.unlink(missing_ok=True)
                with self.assertRaisesRegex(ConcurrentEdit, f'^Prepared stage was edited: {re.escape(str(mine))}$'):
                    self.run_apply()
                self.assertEqual((mine / 'ordinary.txt').read_text(), 'first revision\n')

    def test_a_stage_that_holds_session_markers_stays_when_a_newer_release_takes_over(self):
        destination = self._installed_claude()
        own_lease(destination / '.in_use')
        self.revise_release('first revision\n')
        self.assert_held(destination)
        staged = Path(json.loads(self._pending_claude_receipt().read_bytes())['pending']['staged'])
        (staged / '.in_use').mkdir()
        (staged / '.in_use/17').write_text('{"pid":17,"procStart":"Thu Oct 15 12:30:00 2026"}')
        self.revise_release('second revision\n')
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assert_held(destination)
        self.assertIn(f'Work folder left in place: {staged.parent}: its tree is not the one its record holds\n',
                      stderr.getvalue())
        self.assertTrue((staged / '.in_use/17').exists())

    def test_a_stage_whose_copy_stopped_part_way_is_built_again(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        self.revise_loose('A second loose skill.\n')
        copy_tree = accepted_installations.copy_tree

        def stop(source, destination):
            copy_tree(source, destination)
            if destination.name == 'staged':
                (destination / 'SKILL.md').unlink()
                raise RuntimeError('stopped while copying the stage')

        with patch('accepted_installations.copy_tree', stop), \
                self.assertRaisesRegex(RuntimeError, 'stopped while copying the stage'):
            self.run_apply()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A second loose skill.\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_run_that_stops_while_removing_a_stage_a_newer_release_took_over_leaves_it_for_the_next_run(self):
        destination = self._installed_claude()
        own_lease(destination / '.in_use')
        self.revise_release('first revision\n')
        self.assert_held(destination)
        work = Path(json.loads(self._pending_claude_receipt().read_bytes())['pending']['staged']).parent
        self.revise_release('second revision\n')
        with self.refuse_removal_of(work, RuntimeError('stopped during the removal')), \
                self.assertRaisesRegex(RuntimeError, 'stopped during the removal'):
            self.run_apply()
        self.assertEqual(self.unnamed_work_folders(), [])
        self.assert_held(destination)
        self.assertFalse(work.exists())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_run_that_stops_while_removing_a_stage_its_release_no_longer_needs_leaves_it_for_the_next_run(self):
        destination = self._installed_claude()
        lease = own_lease(destination / '.in_use')
        self.revise_release('first revision\n')
        self.assert_held(destination)
        work = Path(json.loads(self._pending_claude_receipt().read_bytes())['pending']['staged']).parent
        self.revise_release('ordinary release bytes\n')
        with self.refuse_removal_of(work, RuntimeError('stopped during the removal')), \
                self.assertRaisesRegex(RuntimeError, 'stopped during the removal'):
            self.run_apply()
        self.assertEqual(self.unnamed_work_folders(), [])
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertFalse(work.exists())
        self.assertEqual(self.unnamed_work_folders(), [])
        self.assertTrue(lease.exists())

    def test_a_stopped_replacement_whose_receipt_names_another_displaced_path_keeps_its_stage(self):
        destination = self._installed_claude()
        own_lease(destination / '.in_use')
        self.revise_release('first revision\n')
        self.assert_held(destination)
        receipt = self._pending_claude_receipt()
        value = json.loads(receipt.read_bytes())
        staged = Path(value['pending']['staged'])
        value['pending']['displaced'] = str(destination.parent / ('.sync-1.0.0-' + 'ab' * 16) / 'displaced')
        receipt.write_text(json.dumps(value))
        self.revise_release('second revision\n')
        self.assert_held(destination)
        self.assertTrue(staged.is_dir())

    def test_a_tree_windows_briefly_holds_is_moved_once_it_is_free(self):
        held = hold_built_trees_once(self)
        self.assertEqual(self.run_apply().exit_code, 0)
        installed = self.installed()
        self.assertIn(installed, held)
        self.assertIn('use the local reader', (installed / 'skills/one/SKILL.md').read_text())
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('displaced', [path.name for path in held], 'the replaced tree is moved aside')
        self.assertIn('use the reviewed replacement', (installed / 'skills/one/SKILL.md').read_text())

    def test_a_tree_edited_while_windows_holds_it_is_left_where_it_is(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        installed = self.installed()

        def edit(target):
            if target.name == 'displaced':
                (installed / 'ordinary.txt').write_text('edited during the wait\n')

        held = hold_built_trees_once(self, meanwhile=edit)
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        with self.assertRaisesRegex(ConcurrentEdit, f'^Tree changed before replacement: {re.escape(str(installed))}$'):
            self.run_apply()
        self.assertEqual([path.name for path in held], ['displaced'])
        self.assertEqual((installed / 'ordinary.txt').read_text(), 'edited during the wait\n')
        self.assertIn('use the local reader', (installed / 'skills/one/SKILL.md').read_text())

    def stage_edited_during_the_wait_for(self, name):
        self.assertEqual(self.run_apply().exit_code, 0)
        installed = self.installed()

        def edit(target):
            if target.name == name:
                [stage] = installed.parent.glob('.sync-*/staged')
                (stage / 'ordinary.txt').write_text('edited during the wait\n')

        held = hold_built_trees_once(self, meanwhile=edit)
        if name == 'displaced':
            held.append(installed)  # so that only the first move waits, and the second is tried once
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        with self.assertRaises(ConcurrentEdit) as stopped:
            self.run_apply()
        [stage] = installed.parent.glob('.sync-*/staged')
        self.assertEqual(str(stopped.exception), f'Prepared stage was edited: {stage}')
        self.assertFalse(installed.exists(), 'the old tree was moved aside before the stage was read again')
        self.assertIn('use the local reader', (stage.parent / 'displaced/skills/one/SKILL.md').read_text())

    def test_a_stage_edited_while_windows_holds_the_old_tree_stops_the_replacement(self):
        self.stage_edited_during_the_wait_for('displaced')

    def test_a_stage_edited_while_windows_holds_the_new_tree_stops_the_replacement(self):
        self.stage_edited_during_the_wait_for('1.0.0')

    @unittest.skipIf(sys.platform == 'win32' or getattr(os, 'geteuid', lambda: 1)() == 0, 'needs a file its owner cannot read')
    def test_a_tree_that_cannot_be_read_again_while_windows_holds_it_is_left_where_it_is(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        installed = self.installed()
        held = hold_built_trees_once(
            self, meanwhile=lambda target: target.name == 'displaced' and (installed / 'ordinary.txt').chmod(0))
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        with self.assertRaises(PermissionError) as stopped:
            self.run_apply()
        self.assertEqual(Path(stopped.exception.filename), installed / 'ordinary.txt')
        self.assertEqual([path.name for path in held], ['displaced'])
        (installed / 'ordinary.txt').chmod(0o644)
        self.assertEqual((installed / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertIn('use the local reader', (installed / 'skills/one/SKILL.md').read_text())
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (installed / 'skills/one/SKILL.md').read_text())

    def test_a_folder_that_appears_where_windows_holds_the_new_tree_is_not_replaced(self):
        def appear(target):
            if target.name != 'displaced':
                target.mkdir()
                (target / 'theirs.txt').write_text('made during the wait\n')

        held = hold_built_trees_once(self, meanwhile=appear)
        with self.assertRaises(ConcurrentEdit) as stopped:
            self.run_apply()
        self.assertEqual(str(stopped.exception), f'Tree changed during replacement: {held[-1]}')
        self.assertEqual([path.name for path in held[-1].iterdir()], ['theirs.txt'])

    def test_install_then_strict_noop_and_complete_manifest(self):
        after = self.run_apply()
        self.assertEqual(after.exit_code, 0)
        self.assertIn('use the local reader', (self.installed() / 'skills/one/SKILL.md').read_text())
        self.assertEqual((self.roots.codex_home / 'agents/reviewer.toml').read_bytes(), self.role)
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A loose skill.\n')
        self.assertEqual((self.roots.codex_home / 'AGENTS.md').read_text(), 'Managed rules.\n')
        receipt = next(json.loads(p.read_bytes()) for p in self.receipt_paths()
                       if json.loads(p.read_bytes())['identity'].startswith('plugin:'))
        self.assertEqual(receipt['accepted']['image']['entries']['ordinary.txt'],
                         {'kind': 'file', 'sha256': str(bytes_digest((self.source / 'ordinary.txt').read_bytes()))})
        snapshot = self.snapshot()
        with patch('sync_sources.Builder', side_effect=AssertionError('no-op built sources')):
            result, backup = apply(self.setup, self.device, plan(self.setup, self.device))
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(backup)
        self.assertEqual(self.snapshot(), snapshot)

    def test_preplan_ordinary_edit_is_blocked(self):
        self.run_apply()
        ordinary = self.installed() / 'ordinary.txt'
        ordinary.write_text('uncommitted user work\n')
        before = self.snapshot()
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 2)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(ordinary.read_text(), 'uncommitted user work\n')

    def test_repository_receipt_preserves_the_resolved_pin_and_replayable_selection(self):
        from tests.test_sync import git
        repo = self.root / 'upstream'
        shutil.copytree(self.source, repo / 'plugins/kit')
        (repo / '.codex-plugin').mkdir()
        (repo / '.codex-plugin/marketplace.json').write_text(json.dumps(
            {'name': 'origin', 'plugins': [{'name': 'kit', 'source': './plugins/kit'}]}))
        git(repo, 'init', '-b', 'main')
        git(repo, 'config', 'user.name', 'Test')
        git(repo, 'config', 'user.email', 'test@example.invalid')
        git(repo, 'remote', 'add', 'origin', 'https://example.invalid/kit.git')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'Pinned source')
        commit = git(repo, 'rev-parse', 'HEAD')
        repository = Repo('origin', Visibility.PUBLIC, '', 'https://example.invalid/kit.git', commit, None, None)
        self.seeds = (repo,)
        for path, selection in (('plugins/kit', {'path': 'plugins/kit'}), (None, {'marketplace_plugin': 'kit'})):
            with self.subTest(selection=selection):
                package = replace(self.setup.packages['kit'], source=RepoSource('origin', path))
                repositories = {'origin': repository}
                lock = replace(self.setup.lock['kit'], intent=intent(package, repositories))
                self.setup = replace(self.setup, packages={'kit': package}, repos=repositories,
                                     lock={**self.setup.lock, 'kit': lock})
                self.assertEqual(self.run_apply().exit_code, 0)
                receipt = next(json.loads(p.read_bytes()) for p in self.receipt_paths()
                               if json.loads(p.read_bytes())['identity'].startswith('plugin:'))
                self.assertEqual(receipt['accepted']['provenance']['repository'], {
                    'url': 'https://example.invalid/kit.git', 'commit': commit, 'selection': selection})
                before = self.snapshot()
                self.assertEqual(self.run_apply().exit_code, 0)
                self.assertEqual(self.snapshot(), before)

    def test_role_rules_and_loose_preplan_edits_are_protected(self):
        self.run_apply()
        paths = [self.roots.codex_home / 'agents/reviewer.toml', self.roots.codex_home / 'AGENTS.md',
                 self.roots.codex_home / 'skills/loose/SKILL.md']
        for path in paths:
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(b'local work\n')
                current = plan(self.setup, self.device)
                self.assertEqual(current.exit_code, 2)
                with self.assertRaises(RuntimeError):
                    self.run_apply(current)
                self.assertEqual(path.read_bytes(), b'local work\n')
                path.write_bytes(original)

    def test_receiptless_exact_content_is_independently_adopted_without_content_writes(self):
        self.run_apply()
        for path in self.receipt_paths():
            path.unlink()
        installed = [p for p in self.roots.home.rglob('*') if p.is_file()]
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in installed}
        self.calls.clear()
        result = self.run_apply()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual({p: (p.stat().st_mtime_ns, p.read_bytes()) for p in installed}, before)
        self.assertEqual(len(self.receipt_paths()), 4)
        self.assertEqual(self.calls, [])

    def test_receiptless_mismatch_refuses_all_live_changes(self):
        self.run_apply()
        for path in self.receipt_paths():
            path.unlink()
        ordinary = self.installed() / 'ordinary.txt'
        ordinary.write_text('unknown historical bytes\n')
        (self.roots.codex_home / 'AGENTS.md').unlink()
        self.calls.clear()
        with self.assertRaisesRegex(ConcurrentEdit, 'independently pinned baseline'):
            self.run_apply()
        self.assertEqual(ordinary.read_text(), 'unknown historical bytes\n')
        self.assertFalse((self.roots.codex_home / 'AGENTS.md').exists())
        self.assertEqual(self.calls, [])

    def test_rehearsal_refuses_what_the_apply_step_refuses_without_writing(self):
        from accepted_installations import AcceptedInstallations
        self.run_apply()
        for path in self.receipt_paths():
            path.unlink()
        (self.roots.codex_home / 'AGENTS.md').write_text('unknown historical bytes\n')
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 1)
        owner = AcceptedInstallations(self.setup.for_platform(self.device.platform), self.device)
        before = self.snapshot()
        with self.assertRaisesRegex(ConcurrentEdit, 'rules:.*independently pinned baseline'):
            owner.rehearse(current.observations)
        self.assertEqual(self.snapshot(), before)

    def test_rehearsal_passes_a_reviewed_change(self):
        from accepted_installations import AcceptedInstallations
        self.run_apply()
        self.setup = replace(self.setup, rules=Rules(b'Updated reviewed rules.\n'))
        current = plan(self.setup, self.device)
        before = self.snapshot()
        AcceptedInstallations(self.setup.for_platform(self.device.platform), self.device).rehearse(current.observations)
        self.assertEqual(self.snapshot(), before)

    def test_edit_after_prepare_is_rechecked_before_mutation(self):
        from sync_hosts import Apply
        self.run_apply()
        self.setup = replace(self.setup, rules=Rules(b'Updated reviewed rules.\n'))
        rules = self.roots.codex_home / 'AGENTS.md'
        original = Apply.run

        def race(operation):
            rules.write_bytes(b'new user edit\n')
            return original(operation)
        with patch.object(Apply, 'run', race), self.assertRaises(ConcurrentEdit):
            self.run_apply()
        self.assertEqual(rules.read_bytes(), b'new user edit\n')

    def test_pristine_pending_retry_finishes_overlay_without_reinstalling(self):
        def crash(path):
            raise RuntimeError('interrupted after native output')
        self.interrupt = crash
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.run_apply()
        self.assertNotIn('use the local reader', (self.installed() / 'skills/one/SKILL.md').read_text())
        self.interrupt = None
        self.calls.clear()
        result = self.run_apply()
        self.assertEqual(result.exit_code, 0)
        self.assertIn('use the local reader', (self.installed() / 'skills/one/SKILL.md').read_text())
        self.assertFalse(any(args[:2] == ('plugin', 'add') for _, args in self.calls))

    def test_unknown_partial_native_output_is_preserved_on_retry(self):
        def crash(path):
            (path / 'ordinary.txt').write_text('partial native output\n')
            raise RuntimeError('interrupted')
        self.interrupt = crash
        with self.assertRaises(RuntimeError):
            self.run_apply()
        before = self.snapshot()
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 2)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.installed() / 'ordinary.txt').read_text(), 'partial native output\n')

    def assert_catalog_rebuilt_without_reinstalling(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        catalog = self.roots.agents_home / 'plugins/marketplace.json'
        catalog.unlink()
        self.calls.clear()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(json.loads(catalog.read_bytes())['plugins'][0]['name'], 'kit')
        self.assertFalse(any(args[:2] == ('plugin', 'add') for _, args in self.calls))

    def test_catalog_only_drift_reconstructs_entries_without_reinstalling(self):
        self.assert_catalog_rebuilt_without_reinstalling()

    def keep_kit_current(self):
        """kit switched off and kept current, its roles line dropped while its lock still lists the role file."""
        kit = replace(self.setup.packages['kit'], codex=CodexPlacement((EngineInstall(False, keep_current=True),)))
        self.setup = replace(self.setup, packages={'kit': kit},
                             lock={**self.setup.lock, 'kit': replace(self.setup.lock['kit'], intent=intent(kit, {}))})

    def switch(self):
        return tomllib.loads((self.roots.codex_home / 'config.toml').read_text())['plugins']['kit@personal']['enabled']

    def test_catalog_only_drift_reconstructs_a_kept_current_entry_without_reinstalling(self):
        self.keep_kit_current()
        self.assert_catalog_rebuilt_without_reinstalling()

    def test_a_kept_current_install_whose_lock_still_lists_roles_installs_switched_off_without_them(self):
        self.keep_kit_current()
        self.assertEqual(self.setup.lock['kit'].roles, {'reviewer.toml': bytes_digest(self.role)})
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertTrue((self.installed() / 'ordinary.txt').is_file())
        self.assertFalse((self.roots.codex_home / 'agents/reviewer.toml').exists())
        self.assertIs(self.switch(), False)

    def stopped_after_install(self):
        """The switch config.toml holds once a sync stops on output that the native install, which writes the switch
        on as `codex plugin add` does, left beside the release."""
        def switch_on_and_leave_output(destination):
            (self.roots.codex_home / 'config.toml').write_text('[plugins."kit@personal"]\nenabled = true\n')
            (destination / 'ordinary.txt').write_text('partial native output\n')
        self.interrupt = switch_on_and_leave_output
        with self.assertRaisesRegex(ConcurrentEdit, 'Unknown native output preserved'):
            self.run_apply()
        return self.switch()

    def test_a_sync_that_stops_after_installing_a_kept_current_plugin_leaves_it_switched_off(self):
        self.keep_kit_current()
        self.assertIs(self.stopped_after_install(), False)

    def test_a_sync_that_stops_after_installing_an_enabled_plugin_leaves_the_switch_as_the_install_wrote_it(self):
        self.keep_a_second_package_current()
        self.install_each_package_and_interrupt_only_kit()
        self.assertIs(self.stopped_after_install(), True)

    def keep_a_second_package_current(self):
        source = self.public / 'owned/second'
        (source / '.codex-plugin').mkdir(parents=True)
        (source / '.codex-plugin/plugin.json').write_text('{"name":"second","version":"1.0.0"}', encoding='utf-8')
        digest = self.roots.tree_digest(source)
        second = Package('second', Visibility.PUBLIC, '', LocalSource('owned/second', digest), {}, ToolNotes.AUDIT,
                         CodexPlacement((EngineInstall(False, keep_current=True),)), None)
        lock = Lock(intent(second, {}), {Host.CODEX: LockedTree('1.0.0', digest)})
        self.setup = replace(self.setup, packages={**self.setup.packages, 'second': second},
                             lock={**self.setup.lock, 'second': lock})

    def install_each_package_and_interrupt_only_kit(self):
        def command(operation, host, *args):
            if args[:2] == ('plugin', 'add'):
                name = args[2].removesuffix('@personal')
                release = operation.built[name]
                destination = self.roots.codex_home / 'plugins/cache/personal' / name / release.version
                shutil.copytree(release.root, destination)
                self.host.installed[args[2]] = (release.version, destination)
                if name == 'kit' and self.interrupt:
                    self.interrupt(destination)
            return ''
        patch('sync_hosts.Apply.command', command).start()

    def test_a_kept_current_plugin_switched_on_in_config_toml_is_switched_off_again_without_a_reinstall(self):
        self.keep_kit_current()
        self.assertEqual(self.run_apply().exit_code, 0)
        config = self.roots.codex_home / 'config.toml'
        switched_on = config.read_text(encoding='utf-8').replace('enabled = false', 'enabled = true')
        config.write_text(switched_on, encoding='utf-8')
        self.assertIs(self.switch(), True)
        self.calls.clear()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIs(self.switch(), False)
        self.assertFalse(any(args[:2] == ('plugin', 'add') for _, args in self.calls))

    def stopped_by(self, failure, write=None):
        def write_then_fail(destination):
            if write:
                write()
            raise failure
        from contextlib import redirect_stderr
        import io
        self.interrupt = write_then_fail
        with self.assertRaises(type(failure)) as stopped, redirect_stderr(io.StringIO()) as printed:
            self.run_apply()
        self.assertIs(stopped.exception, failure)
        return printed.getvalue()

    def switch_codex_on(self):
        (self.roots.codex_home / 'config.toml').write_text('[plugins."kit@personal"]\nenabled = true\n', encoding='utf-8')

    @staticmethod
    def past_its_time_limit(*command):
        import subprocess
        return subprocess.TimeoutExpired(command, 180)

    def test_a_codex_install_that_times_out_after_switching_a_kept_current_plugin_on_leaves_it_switched_off(self):
        self.keep_kit_current()
        printed = self.stopped_by(self.past_its_time_limit('codex', 'plugin', 'add'), self.switch_codex_on)
        self.assertNotIn('could not switch', printed)
        self.assertIs(self.switch(), False)

    def test_a_codex_install_interrupted_after_switching_a_kept_current_plugin_on_leaves_it_switched_off(self):
        self.keep_kit_current()
        self.stopped_by(KeyboardInterrupt(), self.switch_codex_on)
        self.assertIs(self.switch(), False)

    def test_a_native_install_that_fails_after_switching_an_enabled_plugin_on_leaves_it_on(self):
        self.stopped_by(RuntimeError('codex plugin add failed: exit 1'), self.switch_codex_on)
        self.assertIs(self.switch(), True)

    def test_a_native_install_that_fails_before_config_toml_lists_a_kept_current_plugin_writes_no_config_toml(self):
        self.keep_kit_current()
        self.assertNotIn('could not switch', self.stopped_by(RuntimeError('codex plugin add failed: exit 1')))
        self.assertFalse((self.roots.codex_home / 'config.toml').exists())

    def left_by_a_failed_install(self, text):
        """What the sync printed when a native install left text in config.toml and failed, after a check that
        config.toml still holds that text."""
        self.keep_kit_current()
        config = self.roots.codex_home / 'config.toml'
        printed = self.stopped_by(RuntimeError('codex plugin add failed: exit 1'), lambda: config.write_bytes(text))
        self.assertEqual(config.read_bytes(), text)
        return printed

    def test_a_native_install_that_fails_before_config_toml_lists_a_kept_current_plugin_leaves_config_toml_as_it_was(self):
        printed = self.left_by_a_failed_install(b'[plugins."other@personal"]\nenabled = true\n')
        self.assertNotIn('could not switch', printed)

    def test_a_native_install_that_fails_leaving_config_toml_unreadable_stops_with_its_own_error(self):
        printed = self.left_by_a_failed_install(b'[plugins."kit@personal"]\nenabled = tr')
        self.assertRegex(printed, r'^codex: could not switch kit@personal off .*: TOMLDecodeError: ')

    def test_a_native_install_that_fails_where_the_switch_cannot_be_written_stops_with_its_own_error(self):
        printed = self.left_by_a_failed_install(b'[plugins]\n"kit@personal" = true\n')
        self.assertRegex(printed, r'^codex: could not switch kit@personal off .*: TypeError: ')

    def keep_kit_current_on_claude(self):
        from claude_host import ClaudeState
        from sync_sources import Built
        plugin, catalog = 'kit@personal-collections', self.roots.engine / 'claude'
        kit = replace(self.setup.packages['kit'], codex=None, tool_notes=ToolNotes.AUDIT,
                      claude=ClaudePlacement((EngineInstall(False, keep_current=True),)))
        lock = replace(self.setup.lock['kit'], intent=intent(kit, {}),
                       trees={Host.CLAUDE: LockedTree('1.0.0', self.roots.tree_digest(self.source))}, roles={})
        self.setup = replace(self.setup, packages={'kit': kit}, loose={}, rules=None, lock={'kit': lock})
        self.device = Device(Platform.LINUX, self.roots, (ClaudeState(self.roots, 'claude'),), ())
        self.roots.claude_home.mkdir(parents=True)
        self.claude_settings().write_text(json.dumps({'extraKnownMarketplaces': {'personal-collections': {
            'source': {'source': 'directory', 'path': str(catalog)}}}}), encoding='utf-8')
        installed = []

        def observe(command, **_):
            if command[-1] == '--version':
                return 'claude-test\n'
            if command[-4:] == ['plugin', 'list', '--available', '--json']:
                switches = self.claude_switches()
                return json.dumps({'installed': [{**record, 'enabled': switches.get(record['id'], False)}
                                                 for record in installed], 'available': []})
            raise AssertionError(command)

        def native(operation, host, *args):
            self.calls.append((host, args))
            if args[:2] == ('plugin', 'install'):
                release = operation.claude[plugin]
                destination = self.roots.claude_home / 'plugins/cache/personal-collections/kit' / release.version
                shutil.copytree(release.root, destination)
                installed[:] = [{'id': plugin, 'scope': 'user', 'version': release.version,
                                 'installPath': str(destination)}]
                if self.interrupt:
                    self.interrupt(destination)
                self.switch_claude_on()
            return ''

        def build(builder, package, codex, *, verify=True):
            release = catalog / 'plugins/kit/1.0.0'
            shutil.copytree(codex.root, release)
            return {plugin: Built('kit', release, '1.0.0', builder.roots.tree_digest(release))}

        patch('claude_host.run', observe).start()
        patch('sync_hosts.Apply.command', native).start()
        patch.object(Builder, 'claude', build).start()

    def claude_settings(self):
        return self.roots.claude_home / 'settings.json'

    def switch_claude_on(self):
        settings = json.loads(self.claude_settings().read_text(encoding='utf-8'))
        settings.setdefault('enabledPlugins', {})['kit@personal-collections'] = True
        self.claude_settings().write_text(json.dumps(settings), encoding='utf-8')

    def claude_switches(self):
        return json.loads(self.claude_settings().read_text(encoding='utf-8')).get('enabledPlugins', {})

    def test_a_kept_current_plugin_installed_on_claude_code_ends_switched_off(self):
        self.keep_kit_current_on_claude()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertTrue(any(args[:2] == ('plugin', 'install') for _, args in self.calls))
        self.assertIs(self.claude_switches()['kit@personal-collections'], False)

    def test_a_claude_code_install_that_times_out_after_switching_a_kept_current_plugin_on_leaves_it_switched_off(self):
        self.keep_kit_current_on_claude()
        printed = self.stopped_by(self.past_its_time_limit('claude', 'plugin', 'install'), self.switch_claude_on)
        self.assertNotIn('could not switch', printed)
        self.assertIs(self.claude_switches()['kit@personal-collections'], False)

    def test_a_claude_code_install_that_exits_non_zero_after_switching_a_kept_current_plugin_on_leaves_it_switched_off(self):
        self.keep_kit_current_on_claude()
        self.stopped_by(RuntimeError('claude plugin install failed: exit 1'), self.switch_claude_on)
        self.assertIs(self.claude_switches()['kit@personal-collections'], False)

    def test_a_claude_code_install_that_fails_before_switching_a_kept_current_plugin_on_adds_no_switch(self):
        self.keep_kit_current_on_claude()
        self.stopped_by(RuntimeError('claude plugin install failed: exit 1'))
        self.assertNotIn('kit@personal-collections', self.claude_switches())

    def test_a_claude_code_install_that_fails_leaving_settings_json_unreadable_stops_with_its_own_error(self):
        self.keep_kit_current_on_claude()
        cut_short = b'{"enabledPlugins": {"kit@personal-collections": tr'
        printed = self.stopped_by(RuntimeError('claude plugin install failed: exit 1'),
                                  lambda: self.claude_settings().write_bytes(cut_short))
        self.assertRegex(printed, r'^claude: could not switch kit@personal-collections off .*: JSONDecodeError: ')
        self.assertEqual(self.claude_settings().read_bytes(), cut_short)

    def test_outside_home_source_is_staged_and_edited_cache_is_refused(self):
        self.run_apply()
        catalog = self.roots.agents_home / 'plugins/marketplace.json'
        source = json.loads(catalog.read_bytes())['plugins'][0]['source']
        self.assertEqual(source['source'], 'local')
        staged = self.roots.home / source['path']
        self.assertEqual((staged / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        (staged / 'ordinary.txt').write_text('edited staging cache\n')
        catalog.unlink()
        with self.assertRaisesRegex(ConcurrentEdit, 'immutable source'):
            self.run_apply()
        self.assertEqual((staged / 'ordinary.txt').read_text(), 'edited staging cache\n')

    def test_unowned_extra_content_and_linked_working_copy_are_blocked(self):
        self.run_apply()
        extra = self.installed() / 'node_modules/local/index.js'
        extra.parent.mkdir(parents=True)
        extra.write_text('user owned dependency\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 2)
        with self.assertRaises(RuntimeError):
            self.run_apply()
        self.assertEqual(extra.read_text(), 'user owned dependency\n')
        shutil.rmtree(self.installed() / 'node_modules')
        loose = self.roots.codex_home / 'skills/loose'
        moved = self.root / 'working-copy'
        loose.rename(moved)
        loose.symlink_to(moved, target_is_directory=True)
        self.assertEqual(plan(self.setup, self.device).exit_code, 2)
        self.assertTrue(loose.is_symlink())

    def test_approval_only_plan_is_not_success_and_does_not_write(self):
        self.run_apply()
        package = self.setup.packages['kit']
        package = replace(package, codex=replace(package.codex, hooks=Hooks.TRUSTED))
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}), hooks=1)
        self.setup = replace(self.setup, packages={'kit': package}, lock={**self.setup.lock, 'kit': lock})
        self.run_apply()
        self.host.approved = False
        before = self.snapshot()
        current = plan(self.setup, self.device)
        self.assertEqual([d.kind.value for d in current.blocked], ['approval'])
        self.assertEqual(current.exit_code, 2)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertEqual(self.snapshot(), before)

    def test_first_install_is_not_held_for_an_approval_only_the_install_makes_possible(self):
        package = self.setup.packages['kit']
        package = replace(package, codex=replace(package.codex, hooks=Hooks.TRUSTED))
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}), hooks=1)
        self.setup = replace(self.setup, packages={'kit': package}, lock={**self.setup.lock, 'kit': lock})
        self.host.approved = False
        current = plan(self.setup, self.device)
        self.assertEqual(current.blocked, ())
        installed = self.run_apply(current)
        self.assertEqual([d.kind.value for d in installed.blocked], ['approval'])
        self.assertEqual(installed.exit_code, 2)
        self.host.approved = True
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_toml_fields_share_the_owner_and_preserve_unmanaged_values(self):
        config_source = self.public / 'config/mise/config.toml'
        config_source.parent.mkdir(parents=True)
        config_source.write_text('[tools]\nnode = "24"\n')
        live = self.roots.home / '.config/mise/config.toml'
        live.parent.mkdir(parents=True)
        live.write_text('# keep me\n[env]\nSECRET = "test-only-local"\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('SECRET = "test-only-local"', live.read_text())
        self.assertIn('node = "24"', live.read_text())
        live.write_text(live.read_text().replace('node = "24"', 'node = "20"'))
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 2)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertIn('node = "20"', live.read_text())

    def test_toml_fields_accept_a_newly_managed_key_only_while_it_is_absent(self):
        config_source = self.public / 'config/mise/config.toml'
        config_source.parent.mkdir(parents=True)
        config_source.write_text('[tools]\nnode = "24"\nuv = "0.1"\n')
        live = self.roots.home / '.config/mise/config.toml'
        self.assertEqual(self.run_apply().exit_code, 0)
        config_source.write_text('[tools]\nnode = "24"\nop = "2"\n')
        live.write_text(live.read_text() + 'go = "1"\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)
        self.assertIn('op = "2"', live.read_text())
        self.assertIn('uv = "0.1"', live.read_text())
        config_source.write_text('[tools]\nnode = "24"\nop = "2"\npython = "3"\n')
        live.write_text(live.read_text() + 'python = "2"\n')
        with self.assertRaises(RuntimeError):
            self.run_apply()
        self.assertIn('python = "2"', live.read_text())
        config_source.write_text('[tools]\nnode = "24"\nop = "2"\n')
        live.write_text(live.read_text().replace('python = "2"\n', '').replace('uv = "0.1"', 'uv = "0.2"'))
        config_source.write_text('[tools]\nnode = "24"\nop = "2"\nyarn = "1"\n')
        self.assertEqual(self.run_apply().exit_code, 0)

    def test_mise_env_entry_already_on_the_device_is_adopted_then_updated(self):
        config_source = self.public / 'config/mise/config.toml'
        config_source.parent.mkdir(parents=True)
        config_source.write_text('[tools]\nnode = "24"\n')
        entry = ('\n# Keep npm first.\n[env._.path]\n'
                 "path = ['{{ tools[\"npm:npm\"].path }}/node_modules/.bin']\ntools = true\n")
        live = self.roots.home / '.config/mise/config.toml'
        live.parent.mkdir(parents=True)
        live.write_text('# keep me\n[tools]\nnode = "24"\n\n[env]\nLOCAL = "test-only-local"\n'
                        + entry.replace('.bin', 'other'))
        self.assertEqual(self.run_apply().exit_code, 0)
        config_source.write_text('[tools]\nnode = "24"\n' + entry)
        with self.assertRaises(ConcurrentEdit):
            self.run_apply()
        self.assertIn('/node_modules/other', live.read_text())
        live.write_text(live.read_text().replace('/node_modules/other', '/node_modules/.bin'))
        before = live.read_bytes()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)
        self.assertEqual(live.read_bytes(), before)
        config_source.write_text('[tools]\nnode = "24"\n' + entry.replace('/node_modules/.bin', '/bin'))
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(tomllib.loads(live.read_text()),
                         tomllib.loads(before.decode().replace('/node_modules/.bin', '/bin')))
        self.assertIn('# keep me', live.read_text())
        self.assertIn('# Keep npm first.', live.read_text())

    def test_mise_source_accepts_only_tools_and_env_tables(self):
        config_source = self.public / 'config/mise/config.toml'
        config_source.parent.mkdir(parents=True)
        for text in ('[tools]\nnode = "24"\n[settings]\nexperimental = true\n', '[env]\nLOCAL = "1"\n'):
            config_source.write_text(text)
            with self.assertRaises(ValueError):
                desired(self.public, None, self.roots, Platform.LINUX)

    def test_npmrc_fields_share_the_owner_and_preserve_unmanaged_lines(self):
        config_source = self.public / 'config/npm/npmrc.toml'
        config_source.parent.mkdir(parents=True)
        config_source.write_text('min-release-age = 3\n')
        live = self.roots.home / '.npmrc'
        live.write_text('//registry.example.invalid/:_authToken=test-only-placeholder\nallow-scripts=@stripe/cli\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(live.read_text(), '//registry.example.invalid/:_authToken=test-only-placeholder\n'
                                           'allow-scripts=@stripe/cli\nmin-release-age=3\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)
        live.write_text(live.read_text().replace('min-release-age=3', 'min-release-age=0'))
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 2)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertIn('min-release-age=0', live.read_text())

    def test_npmrc_source_accepts_only_top_level_scalars(self):
        config_source = self.public / 'config/npm/npmrc.toml'
        config_source.parent.mkdir(parents=True)
        config_source.write_text('[scope]\nmin-release-age = 3\n')
        with self.assertRaises(ValueError):
            desired(self.public, None, self.roots, Platform.LINUX)
        config_source.write_text('min-release-age = 3\nfund = false\n')
        npm = next(item for item in desired(self.public, None, self.roots, Platform.LINUX) if item.name == 'npm-user')
        self.assertEqual(npm.fields, {('min-release-age',): '3', ('fund',): 'false'})

    def chrome_candidates(self, *installed):
        candidates = [self.root / 'chrome-a', self.root / 'chrome-b']
        for name in installed:
            (self.root / name).write_bytes(b'')
        source = self.public / 'config/agent-browser/chrome.toml'
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('macos = ["/none"]\nwindows = ["/none"]\nlinux = [' +
                          ', '.join(f"'{path}'" for path in candidates) + ']\n')
        live = self.roots.home / '.agent-browser/config.json'
        live.parent.mkdir(parents=True, exist_ok=True)
        return [str(path) for path in candidates], live

    def test_agent_browser_gets_the_first_installed_chrome_and_keeps_other_keys(self):
        (a, b), live = self.chrome_candidates('chrome-b')
        live.write_text('{"headed": true, "executablePath": "/cft/chrome"}')
        with self.assertRaises(ConcurrentEdit):
            self.run_apply()
        live.write_text('{"headed": true}')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(json.loads(live.read_text()), {'headed': True, 'executablePath': b})
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_agent_browser_keeps_a_current_installed_chrome(self):
        (a, b), live = self.chrome_candidates('chrome-a', 'chrome-b')
        live.write_text(json.dumps({'executablePath': b, 'profile': 'kept'}))
        before = live.read_bytes()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(live.read_bytes(), before)
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_agent_browser_without_installed_chrome_blocks_the_pull(self):
        _, live = self.chrome_candidates()
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 3)
        self.assertTrue(any('No installed Google Chrome' in d.detail for d in current.blocked))
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertFalse(live.exists())
        self.assertEqual(self.calls, [])

    def test_receiptless_mismatched_toml_field_is_not_overwritten(self):
        config_source = self.public / 'config/mise/config.toml'
        config_source.parent.mkdir(parents=True)
        config_source.write_text('[tools]\nnode = "24"\n')
        live = self.roots.home / '.config/mise/config.toml'
        live.parent.mkdir(parents=True)
        live.write_text('[tools]\nnode = "20"\n[env]\nSECRET = "test-only-local"\n')
        with self.assertRaises(ConcurrentEdit):
            self.run_apply()
        self.assertEqual(live.read_text(), '[tools]\nnode = "20"\n[env]\nSECRET = "test-only-local"\n')
        self.assertEqual(self.calls, [])

    def test_accepted_adaptation_checks_without_its_clone_or_builder(self):
        from tests.test_sync import git
        repo = self.root / 'adaptation'
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        git(repo, 'config', 'user.name', 'Test')
        git(repo, 'config', 'user.email', 'test@example.invalid')
        git(repo, 'remote', 'add', 'origin', 'https://example.invalid/adaptation.git')
        (repo / 'adapted.txt').write_text('base\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'Base')
        base = git(repo, 'rev-parse', 'HEAD')
        (repo / 'adapted.txt').write_text('adapted\n')
        git(repo, 'commit', '-am', 'Adapted')
        adapted = git(repo, 'rev-parse', 'HEAD')
        (self.source / 'adapted.txt').write_text('base\n')
        package = self.setup.packages['kit']
        package = replace(package, source=replace(package.source, digest=self.roots.tree_digest(self.source)),
                          codex=replace(package.codex, adaptation=Adaptation('https://example.invalid/adaptation.git', base, adapted)))
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}),
                       trees={Host.CODEX: LockedTree('1.0.0', self.roots.tree_digest(self.source))})
        self.setup = replace(self.setup, packages={'kit': package}, lock={**self.setup.lock, 'kit': lock})
        self.seeds = (repo,)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.installed() / 'adapted.txt').read_text(), 'adapted\n')
        # Git makes its object files read-only, and Windows refuses to delete a read-only file.
        shutil.rmtree(self.roots.engine / 'harness-adaptations',
                      onexc=lambda remove, path, _: (os.chmod(path, 0o600), remove(path)))
        before = self.snapshot()
        with patch('sync_sources.Builder', side_effect=AssertionError('check built sources')):
            current = plan(self.setup, self.device)
            self.assertEqual(current.exit_code, 0)
            self.assertEqual(apply(self.setup, self.device, current), (current, None))
        self.assertEqual(self.snapshot(), before)

    def test_pending_before_file_write_retries_only_accepted_content(self):
        from sync_files import Files
        self.run_apply()
        self.setup = replace(self.setup, rules=Rules(b'New reviewed rules.\n'))
        with patch.object(Files, 'write', side_effect=RuntimeError('before file mutation')):
            with self.assertRaisesRegex(RuntimeError, 'before file mutation'):
                self.run_apply()
        self.assertEqual((self.roots.codex_home / 'AGENTS.md').read_bytes(), b'Managed rules.\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.roots.codex_home / 'AGENTS.md').read_bytes(), b'New reviewed rules.\n')

    def test_pending_target_finishes_with_receipt_only(self):
        from accepted_installations import AcceptedInstallations
        self.run_apply()
        self.setup = replace(self.setup, rules=Rules(b'New reviewed rules.\n'))
        save = AcceptedInstallations._save

        def crash(owner, artifact, accepted, pending=None):
            if artifact.identity.startswith('rules:') and pending is None:
                raise RuntimeError('before acceptance')
            return save(owner, artifact, accepted, pending)
        with patch.object(AcceptedInstallations, '_save', crash), self.assertRaisesRegex(RuntimeError, 'before acceptance'):
            self.run_apply()
        path = self.roots.codex_home / 'AGENTS.md'
        before = path.stat().st_mtime_ns
        self.assertEqual(path.read_bytes(), b'New reviewed rules.\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_interrupted_tree_swap_recovers_its_recorded_stage(self):
        self.run_apply()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        rename = Path.rename
        failed = False

        def crash(path, target):
            nonlocal failed
            if path.name == 'staged' and not failed:
                failed = True
                raise RuntimeError('between tree renames')
            return rename(path, target)
        with patch.object(Path, 'rename', crash), self.assertRaisesRegex(RuntimeError, 'between tree renames'):
            self.run_apply()
        self.assertFalse(self.installed().exists())
        self.calls.clear()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (self.installed() / 'skills/one/SKILL.md').read_text())
        self.assertFalse(any(args[:2] == ('plugin', 'add') for _, args in self.calls))

    def _interrupted_claude_install(self, *, aligned=False, implicit=False):
        from claude_host import ClaudeState
        from sync_sources import Built

        marketplace_name = 'codex-aligned' if aligned else 'personal-collections'
        plugin = 'kit@' + marketplace_name
        skill = self.source / 'skills/one/SKILL.md'
        if not implicit:
            skill.write_text(skill.read_text().replace('description: Use a tool.',
                                                       'description: Use a tool.\ndisable-model-invocation: true'))
        if aligned:
            alignment = self.source / '.claude-plugin/codex-alignment.json'
            alignment.parent.mkdir()
            alignment.write_text('{"excludedSkillNames": []}')
        # The kit's tool notes make the installed image differ from the release, which the interrupted swaps here
        # need. A real description refuses tool notes for an engine install, since Claude Code runs the release.
        package = replace(self.setup.packages['kit'], source=LocalSource('owned/kit', self.roots.tree_digest(self.source)), codex=None,
                          claude=ClaudePlacement((AlignedInstall(True) if aligned else EngineInstall(True),)),
                          modes={} if implicit else {'one': SkillMode.EXPLICIT})
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}),
                       trees={Host.CLAUDE: LockedTree('1.0.0', self.roots.tree_digest(self.source))}, roles={})
        self.setup = replace(self.setup, packages={'kit': package}, loose={}, rules=None, lock={'kit': lock})
        self.device = Device(Platform.WINDOWS, self.roots, (ClaudeState(self.roots, 'claude'),), ())
        marketplace = (self.roots.claude_home / 'marketplaces/codex-aligned' if aligned
                       else self.roots.engine / 'claude')
        self.roots.claude_home.mkdir(parents=True)
        (self.roots.claude_home / 'settings.json').write_text(json.dumps({
            'extraKnownMarketplaces': {marketplace_name: {'source': {
                'source': 'directory', 'path': str(marketplace)}}}}))
        inventory = []

        def observe_command(command, **_):
            if command[-1] == '--version':
                return 'claude-test\n'
            if command[-4:] == ['plugin', 'list', '--available', '--json']:
                return json.dumps({'installed': inventory, 'available': []})
            raise AssertionError(command)

        def native_command(operation, host, *args):
            self.calls.append((host, args))
            if args[:2] in (('plugin', 'install'), ('plugin', 'update')):
                release = operation.claude[plugin]
                destination = self.roots.claude_home / 'plugins/cache' / marketplace_name / 'kit' / release.version
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(release.root, destination, dirs_exist_ok=True)
                inventory[:] = [{'id': plugin, 'scope': 'user', 'enabled': True,
                                 'version': release.version, 'installPath': str(destination)}]
                if self.interrupt:
                    self.interrupt(destination)
            return ''

        def build_claude(builder, package, codex, *, verify=True):
            version = builder.setup.lock['kit'].trees[Host.CLAUDE].version
            release = marketplace / 'plugins/kit' / version
            release.parent.mkdir(parents=True, exist_ok=True)
            if release.exists():
                shutil.rmtree(release)
            shutil.copytree(codex.root, release)
            return {plugin: Built('kit', release, version, builder.roots.tree_digest(release))}

        rename = Path.rename
        failed = False

        def crash(path, target):
            nonlocal failed
            if path.name == 'staged' and not failed:
                failed = True
                raise RuntimeError('between tree renames')
            return rename(path, target)

        patch('claude_host.run', observe_command).start()
        patch('sync_hosts.Apply.command', native_command).start()
        patch.object(Builder, 'claude', build_claude).start()
        if aligned:
            self.assertEqual(self.run_apply().exit_code, 0)
            (self.source / 'ordinary.txt').write_text('revised release bytes\n')
            package = replace(package, source=LocalSource('owned/kit', self.roots.tree_digest(self.source)))
            lock = replace(lock, intent=intent(package, {}),
                           trees={Host.CLAUDE: LockedTree('1.0.0', self.roots.tree_digest(self.source))})
            self.setup = replace(self.setup, packages={'kit': package}, lock={'kit': lock})
        with patch.object(Path, 'rename', crash), self.assertRaisesRegex(RuntimeError, 'between tree renames'):
            self.run_apply()
        destination = self.roots.claude_home / 'plugins/cache' / marketplace_name / 'kit/1.0.0'
        self.assertFalse(destination.exists())
        return destination

    def _pending_claude_receipt(self):
        return next(path for path in self.receipt_paths()
                    if json.loads(path.read_bytes())['identity'] == 'plugin:claude:kit@personal-collections')

    def _installed_claude(self):
        destination = self._interrupted_claude_install()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.calls.clear()
        return destination

    def test_an_edit_to_the_folder_claude_code_runs_blocks_like_a_cache_edit(self):
        self._installed_claude()
        loaded = self.roots.engine / 'claude/plugins/kit/1.0.0/ordinary.txt'
        original = loaded.read_text()
        loaded.write_text('edited where Claude Code runs it\n')
        edited = plan(self.setup, self.device)
        self.assertEqual(edited.exit_code, 2)
        self.assertEqual([(d.kind.value, d.detail) for d in edited.blocked],
                         [('local-edit', 'plugin:claude:kit@personal-collections: The folder Claude Code runs it from, '
                                            '{engine}/claude/plugins/kit/1.0.0, differs from its locked release')])
        loaded.write_text(original)
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_claude_runtime_metadata_is_not_owned_and_exact_adoption_preserves_it(self):
        destination = self._installed_claude()
        leases = destination / '.in_use'
        leases.mkdir()
        (leases / '17').write_text('{"pid":17,"procStart":"Thu Oct 15 12:30:00 2026"}')
        (leases / '29.tmp.0123abcd').write_text('{"pid":29,"procStartFt":"134044866000000000"}')
        (destination / '.orphaned_at').write_text('1792091304500')
        before = self.snapshot()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calls, [])
        receipt = self._pending_claude_receipt()
        receipt.unlink()
        self.assertEqual(self.run_apply().exit_code, 0)
        accepted = json.loads(receipt.read_bytes())['accepted']['image']['entries']
        self.assertNotIn('.in_use', accepted)
        self.assertNotIn('.orphaned_at', accepted)
        self.assertEqual((leases / '17').read_text(), '{"pid":17,"procStart":"Thu Oct 15 12:30:00 2026"}')
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertEqual(self.calls, [])

    def test_claude_malformed_runtime_metadata_blocks_without_writes(self):
        destination = self._installed_claude()
        cases = [
            ('.in_use', b'17'),
            ('.orphaned_at', None),
            ('.orphaned_at', b''),
            ('.orphaned_at', b'1792091304500\n'),
            ('.orphaned_at', b'8640000000000001'),
            ('.in_use/notes.txt', b'{"pid":17}'),
            ('.in_use/17/child', b'{"pid":17}'),
            ('.in_use/17', b'not json'),
            ('.in_use/17', '{"pid":17}'.encode('utf-16')),
            ('.in_use/17', b'{}'),
            ('.in_use/17', b'{"pid":18}'),
            ('.in_use/17', b'{"pid":true}'),
            ('.in_use/17', b'{"pid":17,"procStart":17}'),
            ('.in_use/17', b'{"pid":17,"procStartFt":null}'),
            ('.in_use/17', b'{"pid":17,"other":"user content"}'),
            ('.in_use/17', b'{"pid":18,"pid":17}'),
            ('.in_use/2147483648', b'{"pid":2147483648}'),
            ('.in_use/17.tmp.1234ABCD', b'{"pid":17}'),
            ('.in_use/17.tmp.1234abcd', b''),
            ('.in_use/17.tmp.1234abcd', b'{"pid":17,"procStart":"' + b'0' * 4096 + b'"}'),
        ]
        for name, data in cases:
            with self.subTest(name=name, data=data[:80] if data else data):
                entry = destination / name
                entry.parent.mkdir(parents=True, exist_ok=True)
                entry.mkdir() if data is None else entry.write_bytes(data)
                before = self.snapshot()
                current = plan(self.setup, self.device)
                self.assertEqual(current.exit_code, 2)
                with self.assertRaisesRegex(RuntimeError, 'blocked'):
                    self.run_apply(current)
                self.assertEqual(self.snapshot(), before)
                top = destination / Path(name).parts[0]
                shutil.rmtree(top) if top.is_dir() else top.unlink()

    def test_claude_runtime_links_are_rejected(self):
        destination = self._installed_claude()
        external = self.root / 'external-marker'
        external.write_text('{"pid":17}')
        external_directory = self.root / 'external-leases'
        external_directory.mkdir()
        for name, target, hardlink in [('.in_use', external_directory, False),
                                       ('.in_use/17', external, False),
                                       ('.in_use/17', external, True),
                                       ('.orphaned_at', external, False)]:
            with self.subTest(name=name, hardlink=hardlink):
                entry = destination / name
                entry.parent.mkdir(parents=True, exist_ok=True)
                entry.hardlink_to(target) if hardlink else entry.symlink_to(target, target_is_directory=target.is_dir())
                self.assertEqual(plan(self.setup, self.device).exit_code, 2)
                self.assertEqual(external.read_text(), '{"pid":17}')
                entry.unlink()
                if entry.parent.name == '.in_use':
                    entry.parent.rmdir()

    def test_claude_lease_disappearing_during_read_is_a_noop(self):
        destination = self._installed_claude()
        lease = destination / '.in_use/17'
        lease.parent.mkdir()
        lease.write_text('{"pid":17,"procStart":"42"}')
        read_bytes = Path.read_bytes

        def released(path):
            if path == lease:
                path.unlink()
            return read_bytes(path)

        with patch.object(Path, 'read_bytes', released):
            self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertEqual(tuple(lease.parent.iterdir()), ())
        self.assertEqual(self.calls, [])

    def held(self):
        from accepted_installations import AcceptedInstallations
        owner = AcceptedInstallations(self.setup.for_platform(self.device.platform), self.device)
        return owner.held_replacements(plan(self.setup, self.device).observations)

    def assert_held(self, destination):
        """Apply while this test's process holds the cache: the rest of the release applies, and the cache
        waits as it was for a later pull."""
        result = self.run_apply()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.held, ((destination, (os.getpid(),)),))
        return result

    def revise_release(self, text):
        """Release new bytes for the kit's file outside its skills at the same version, a change running Claude
        sessions hold its cache against."""
        (self.source / 'ordinary.txt').write_text(text)
        digest = self.roots.tree_digest(self.source)
        package = replace(self.setup.packages['kit'], source=LocalSource('owned/kit', digest))
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}), trees={Host.CLAUDE: LockedTree('1.0.0', digest)})
        self.setup = replace(self.setup, packages={'kit': package}, lock={'kit': lock})

    def test_a_claude_session_that_starts_while_windows_holds_the_cache_keeps_it(self):
        destination = self._installed_claude()
        self.revise_release('revised release bytes\n')
        held = hold_built_trees_once(
            self, meanwhile=lambda target: target.name == 'displaced' and own_lease(destination / '.in_use'))
        self.assert_held(destination)
        self.assertEqual([path.name for path in held], ['displaced'])
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')

    def test_claude_same_path_replacement_waits_for_a_running_holder(self):
        destination = self._installed_claude()
        lease = own_lease(destination / '.in_use')
        marker = lease.read_text()
        self.assertEqual(self.held(), [])
        self.revise_release('revised release bytes\n')
        holder = [('plugin:claude:kit@personal-collections', destination, (os.getpid(),))]
        self.assertEqual(self.held(), holder)
        self.assert_held(destination)
        self.assertEqual(self.held(), holder)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertEqual(lease.read_text(), marker)
        lease.unlink()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'revised release bytes\n')
        self.assertEqual(self.calls, [])

    def test_a_held_claude_cache_leaves_the_rest_of_the_release_to_apply(self):
        destination = self._installed_claude()
        lease = own_lease(destination / '.in_use')
        # The plugin comes before the configurations in the apply order, so a stop on it would leave the npm file.
        self.revise_release('revised release bytes\n')
        config_source = self.public / 'config/npm/npmrc.toml'
        config_source.parent.mkdir(parents=True)
        config_source.write_text('min-release-age = 3\n')
        result = self.assert_held(destination)
        self.assertEqual((self.roots.home / '.npmrc').read_text(), 'min-release-age=3\n')
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertEqual([d.detail for d in result.changes],
                         ['plugin:claude:kit@personal-collections: Finish a verified pending replacement'])
        lease.unlink()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'revised release bytes\n')

    def test_a_change_to_skill_text_alone_replaces_a_held_cache_and_keeps_its_leases(self):
        destination = self._installed_claude()
        lease = own_lease(destination / '.in_use')
        marker = lease.read_text()
        # Claude Code writes a marker under a temporary name and renames it in its folder, so that one stays.
        writing = destination / '.in_use/29.tmp.0123abcd'
        writing.write_text('{"pid":29,"procStartFt":"134044866000000000"}')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        result = self.run_apply()
        self.assertEqual((result.exit_code, result.held), (0, ()))
        self.assertIn('use the reviewed replacement', (destination / 'skills/one/SKILL.md').read_text())
        self.assertEqual(lease.read_text(), marker)
        backup = Path(json.loads(self._pending_claude_receipt().read_bytes())['accepted']['backup'])
        self.assertEqual([path.name for path in (backup / '.in_use').iterdir()], [writing.name])
        self.assertEqual(self.calls, [])
        # The moved lease still holds the new folder against a change beyond the skill text.
        self.revise_release('revised release bytes\n')
        self.assert_held(destination)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')

    def test_a_pending_change_to_skill_text_alone_is_not_held(self):
        destination = self._installed_claude()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        rename = Path.rename

        def crash(path, target):
            if path == destination:
                raise RuntimeError('before the swap')
            return rename(path, target)

        with patch.object(Path, 'rename', crash), self.assertRaisesRegex(RuntimeError, 'before the swap'):
            self.run_apply()
        self.assertIsNotNone(json.loads(self._pending_claude_receipt().read_bytes())['pending'])
        lease = own_lease(destination / '.in_use')
        self.assertEqual(self.held(), [])
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (destination / 'skills/one/SKILL.md').read_text())
        self.assertTrue(lease.exists())

    def test_only_markdown_files_under_skills_that_both_caches_hold_count_as_skill_text(self):
        from accepted_installations import _skill_text_only

        def tree(entries):
            return {'kind': 'tree', 'entries': {path: {'kind': 'directory'} if digest is None else {'kind': 'file', 'sha256': digest}
                                                for path, digest in entries.items()}}

        installed = {'skills': None, 'skills/one': None, 'skills/one/SKILL.md': 'a', 'skills/one/run.sh': 'b', 'README.md': 'c'}
        self.assertTrue(_skill_text_only(tree(installed), tree({**installed, 'skills/one/SKILL.md': 'z'})))
        without = {path: digest for path, digest in installed.items() if path != 'skills/one/SKILL.md'}
        for changed in [{**installed, 'README.md': 'z'}, {**installed, 'skills/one/run.sh': 'z'},
                        {**installed, 'skills/one/SKILL.md': None}, {**installed, 'skills/one/OTHER.md': 'd'}, without]:
            with self.subTest(changed=changed):
                self.assertFalse(_skill_text_only(tree(installed), tree(changed)))
        self.assertFalse(_skill_text_only(tree(installed), {'kind': 'absent'}))

    def test_a_newer_release_takes_over_a_replacement_a_running_holder_stopped(self):
        destination = self._installed_claude()
        lease = own_lease(destination / '.in_use')
        self.revise_release('first revision\n')
        self.assert_held(destination)
        stale = json.loads(self._pending_claude_receipt().read_bytes())['pending']
        self.assertTrue(Path(stale['staged']).is_dir())
        # Claude Code marks the work folder as an orphaned version, as it did on WSL on 2026-11-07.
        (Path(stale['staged']).parent / '.orphaned_at').write_text('1794063570885')
        self.revise_release('second revision\n')
        self.assert_held(destination)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertFalse(Path(stale['staged']).parent.exists())
        lease.unlink()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'second revision\n')
        self.assertIsNone(json.loads(self._pending_claude_receipt().read_bytes())['pending'])
        self.assertEqual(self.calls, [])

    def test_a_newer_release_takes_over_a_stopped_replacement_of_content_it_restores(self):
        destination = self._installed_claude()
        lease = own_lease(destination / '.in_use')
        self.revise_release('first revision\n')
        self.assert_held(destination)
        stale = json.loads(self._pending_claude_receipt().read_bytes())['pending']
        self.revise_release('ordinary release bytes\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertTrue(lease.exists())
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertFalse(Path(stale['staged']).parent.exists())
        self.assertIsNone(json.loads(self._pending_claude_receipt().read_bytes())['pending'])

    def test_a_stopped_replacement_keeps_a_stage_it_does_not_recognise(self):
        destination = self._installed_claude()
        lease = own_lease(destination / '.in_use')
        self.revise_release('first revision\n')
        self.assert_held(destination)
        stale = json.loads(self._pending_claude_receipt().read_bytes())['pending']
        extra = Path(stale['staged']) / 'ordinary.txt'
        extra.write_text('edited in the stage\n')
        lease.unlink()
        self.revise_release('second revision\n')
        self.assertIn(f'Work folder left in place: {extra.parent.parent}: ', self.apply_reporting())
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'second revision\n')
        self.assertEqual(extra.read_text(), 'edited in the stage\n')

    def test_a_replacement_that_began_its_swap_finishes_before_a_newer_release(self):
        destination = self._installed_claude()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the first replacement'),))
        rename = Path.rename

        def crash(path, target):
            if path.name == 'staged':
                raise RuntimeError('between tree renames')
            return rename(path, target)

        with patch.object(Path, 'rename', crash), self.assertRaisesRegex(RuntimeError, 'between tree renames'):
            self.run_apply()
        pending = json.loads(self._pending_claude_receipt().read_bytes())['pending']
        self.assertFalse(destination.exists())
        self.assertTrue(Path(pending['displaced']).is_dir())
        first = replace(self.setup)
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the second replacement'),))
        current = plan(self.setup, self.device)
        self.assertEqual([d.detail for d in current.blocked],
                         ['{claude_home}/plugins/cache/personal-collections/kit/1.0.0: cache not on disk: '
                          '{claude_home}/plugins/cache/personal-collections/kit/1.0.0'])
        with self.assertRaisesRegex(RuntimeError, 'blocked'):
            self.run_apply(current)
        self.assertFalse(destination.exists())
        self.assertTrue(Path(pending['staged']).is_dir())
        self.setup = first
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the first replacement', (destination / 'skills/one/SKILL.md').read_text())

    def test_claude_same_path_replacement_ignores_a_finished_session_lease(self):
        destination = self._installed_claude()
        lease = destination / '.in_use/17'
        lease.parent.mkdir()
        lease.write_text('{"pid":17,"procStart":"42"}')
        self.revise_release('revised release bytes\n')
        with patch('accepted_installations._lease_held', return_value=False):
            self.assertEqual(self.held(), [])
            self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'revised release bytes\n')
        self.assertEqual(self.calls, [])

    def test_claude_lease_replaced_during_disappearance_check_is_not_ignored(self):
        destination = self._installed_claude()
        lease = destination / '.in_use/17'
        lease.parent.mkdir()
        lease.write_text('{"pid":17}')
        read_bytes = Path.read_bytes

        def replaced_marker(path):
            if path == lease:
                path.write_text('replacement bytes')
                raise FileNotFoundError(path)
            return read_bytes(path)

        with patch.object(Path, 'read_bytes', replaced_marker):
            self.assertEqual(plan(self.setup, self.device).exit_code, 2)
        self.assertEqual(lease.read_text(), 'replacement bytes')
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')

    def test_claude_pending_displaced_runtime_metadata_recovers_but_stage_metadata_does_not(self):
        destination = self._interrupted_claude_install()
        pending = json.loads(self._pending_claude_receipt().read_bytes())['pending']
        displaced = Path(pending['displaced'])
        (displaced / '.in_use').mkdir()
        (displaced / '.orphaned_at').write_text('1792091304500')
        staged_marker = Path(pending['staged']) / '.in_use'
        staged_marker.mkdir()
        self.assertEqual(plan(self.setup, self.device).exit_code, 3)
        staged_marker.rmdir()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertFalse((destination / '.orphaned_at').exists())
        self.assertEqual((displaced / '.orphaned_at').read_text(), '1792091304500')

    def test_claude_source_runtime_metadata_is_not_adopted_as_content(self):
        destination = self._installed_claude()
        builder = Builder(self.setup, self.roots, self.roots.engine)
        source = builder.codex(self.setup.packages['kit']).root
        (source / '.in_use').mkdir()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        before = (destination / 'skills/one/SKILL.md').read_bytes()
        with self.assertRaisesRegex(BuildError, 'runtime metadata is not source content'):
            self.run_apply()
        self.assertEqual((destination / 'skills/one/SKILL.md').read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_claude_version_update_pending_retry_tolerates_old_native_bookkeeping_only(self):
        destination = self._installed_claude()
        old_lease = destination / '.in_use/17'
        old_lease.parent.mkdir()
        old_lease.write_text('{"pid":17,"procStart":"42"}')
        (self.source / '.codex-plugin/plugin.json').write_text('{"name":"kit","version":"2.0.0"}')
        (self.source / 'ordinary.txt').write_text('second release bytes\n')
        digest = self.roots.tree_digest(self.source)
        package = replace(self.setup.packages['kit'], source=LocalSource('owned/kit', digest))
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}),
                       trees={Host.CLAUDE: LockedTree('2.0.0', digest)})
        self.setup = replace(self.setup, packages={'kit': package}, lock={'kit': lock})

        def native_bookkeeping(new):
            (destination / '.orphaned_at').write_text('1792091304500')
            (new / '.in_use').mkdir()

        self.interrupt = native_bookkeeping
        rename = Path.rename

        def crash(path, target):
            if path.name == 'staged':
                raise RuntimeError('between version tree renames')
            return rename(path, target)

        with patch.object(Path, 'rename', crash), self.assertRaisesRegex(RuntimeError, 'between version tree renames'):
            self.run_apply()
        self.assertEqual((destination / '.orphaned_at').read_text(), '1792091304500')
        self.assertEqual(old_lease.read_text(), '{"pid":17,"procStart":"42"}')
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        (destination / 'ordinary.txt').write_text('user edit in old cache\n')
        edited = plan(self.setup, self.device)
        self.assertEqual(edited.exit_code, 3)
        self.assertTrue(any('Previous installation changed' in difference.detail for difference in edited.blocked))
        (destination / 'ordinary.txt').write_text('ordinary release bytes\n')
        self.calls.clear()
        self.assertEqual(self.run_apply().exit_code, 0)
        current = destination.with_name('2.0.0')
        self.assertEqual((current / 'ordinary.txt').read_text(), 'second release bytes\n')
        self.assertFalse((current / '.orphaned_at').exists())
        self.assertEqual(old_lease.read_text(), '{"pid":17,"procStart":"42"}')
        self.assertEqual(self.calls, [])

    def test_claude_version_update_adopts_the_pinned_release_a_native_listing_cached_and_registers_it(self):
        destination = self._installed_claude()
        (self.source / '.codex-plugin/plugin.json').write_text('{"name":"kit","version":"2.0.0"}')
        (self.source / 'ordinary.txt').write_text('second release bytes\n')
        digest = self.roots.tree_digest(self.source)
        package = replace(self.setup.packages['kit'], source=LocalSource('owned/kit', digest))
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}),
                       trees={Host.CLAUDE: LockedTree('2.0.0', digest)})
        self.setup = replace(self.setup, packages={'kit': package}, lock={'kit': lock})
        current = destination.with_name('2.0.0')
        shutil.copytree(self.source, current)
        (current / '.in_use').mkdir()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((current / 'ordinary.txt').read_text(), 'second release bytes\n')
        self.assertIn('use the local reader', (current / 'skills/one/SKILL.md').read_text())
        self.assertIn(('plugin', 'update'), [args[:2] for _, args in self.calls])

    def test_claude_version_update_preserves_other_content_at_the_new_cache_path(self):
        destination = self._installed_claude()
        (self.source / '.codex-plugin/plugin.json').write_text('{"name":"kit","version":"2.0.0"}')
        digest = self.roots.tree_digest(self.source)
        package = replace(self.setup.packages['kit'], source=LocalSource('owned/kit', digest))
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}),
                       trees={Host.CLAUDE: LockedTree('2.0.0', digest)})
        self.setup = replace(self.setup, packages={'kit': package}, lock={'kit': lock})
        current = destination.with_name('2.0.0')
        shutil.copytree(self.source, current)
        (current / 'ordinary.txt').write_text('unknown bytes\n')
        with self.assertRaisesRegex(ConcurrentEdit, 'Unverified destination preserved'):
            self.run_apply()
        self.assertEqual((current / 'ordinary.txt').read_text(), 'unknown bytes\n')
        self.assertEqual(self.calls, [])

    def test_claude_lease_created_after_swap_receipt_stops_the_rename(self):
        from accepted_installations import AcceptedInstallations

        destination = self._installed_claude()
        self.revise_release('revised release bytes\n')
        save = AcceptedInstallations._save
        writes = 0
        lease = None

        def concurrent_lease(owner, artifact, accepted, pending=None):
            nonlocal writes, lease
            save(owner, artifact, accepted, pending)
            if pending:
                writes += 1
                if writes == 2:
                    lease = own_lease(destination / '.in_use')

        with patch.object(AcceptedInstallations, '_save', concurrent_lease):
            self.assert_held(destination)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertTrue(lease.exists())
        lease.unlink()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'revised release bytes\n')

    def test_claude_historical_receipt_metadata_is_not_silently_normalized(self):
        destination = self._installed_claude()
        (destination / '.in_use').mkdir()
        receipt = self._pending_claude_receipt()
        value = json.loads(receipt.read_bytes())
        value['accepted']['image']['entries']['.in_use'] = {'kind': 'directory'}
        receipt.write_text(json.dumps(value))
        before = receipt.read_bytes()
        self.assertEqual(plan(self.setup, self.device).exit_code, 2)
        self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')

    def test_runtime_marker_names_outside_claude_cache_root_remain_owned_content(self):
        self.run_apply()
        for root in (self.installed(), self.roots.codex_home / 'skills/loose'):
            marker = root / '.in_use'
            marker.mkdir()
            self.assertEqual(plan(self.setup, self.device).exit_code, 2)
            marker.rmdir()
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)
        destination = self._installed_claude()
        for name in ('skills/.in_use', 'skills/.orphaned_at', '.last_used'):
            marker = destination / name
            marker.write_text('1792091304500')
            self.assertEqual(plan(self.setup, self.device).exit_code, 2)
            marker.unlink()
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_python_caches_are_not_local_edits(self):
        self.run_apply()
        for cache in (self.installed() / 'skills/one/__pycache__', self.roots.codex_home / 'skills/loose/__pycache__'):
            cache.mkdir()
            (cache / 'tool.cpython-314.pyc').write_bytes(b'compiled')
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)
        destination = self._installed_claude()
        for name in ('__pycache__', 'skills/one/__pycache__'):
            (destination / name).mkdir()
            (destination / name / 'tool.cpython-314.pyc').write_bytes(b'compiled')
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_a_reviewed_update_replaces_a_loose_skill_with_python_caches(self):
        self.run_apply()
        loose = self.roots.codex_home / 'skills/loose'
        (loose / '__pycache__').mkdir()
        (loose / '__pycache__/tool.cpython-314.pyc').write_bytes(b'compiled')
        source = self.public / 'owned/loose'
        (source / 'SKILL.md').write_text('An updated loose skill.\n')
        (source / '__pycache__').mkdir()
        (source / '__pycache__/tool.cpython-314.pyc').write_bytes(b'source cache')
        digest = self.roots.tree_digest(source)
        skill = replace(self.setup.loose['loose'], source=LocalSource('owned/loose', digest))
        self.setup = replace(self.setup, loose={'loose': skill},
                             lock={**self.setup.lock, 'loose': Lock(intent(skill, {}), placements={SkillRoot.CODEX: digest})})
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((loose / 'SKILL.md').read_text(), 'An updated loose skill.\n')
        self.assertFalse((loose / '__pycache__').exists())
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_a_linked_python_cache_still_blocks(self):
        self.run_apply()
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        cache = self.roots.codex_home / 'skills/loose/__pycache__'
        cache.symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(plan(self.setup, self.device).exit_code, 2)
        cache.unlink()
        cache.mkdir()
        (cache / 'tool.cpython-314.pyc').symlink_to(elsewhere)
        self.assertEqual(plan(self.setup, self.device).exit_code, 2)
        destination = self._installed_claude()
        (destination / '__pycache__').symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(plan(self.setup, self.device).exit_code, 2)

    def test_claude_skill_unread_during_verified_pending_swap_recovers(self):
        destination = self._interrupted_claude_install()
        settings = self.roots.claude_home / 'settings.json'
        configured = json.loads(settings.read_bytes())
        configured['skillOverrides'] = {'kit:one': 'off'}
        settings.write_text(json.dumps(configured))
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 1, [(d.kind.value, d.key, d.detail) for d in current.result.differences])
        self.assertEqual(current.observations[Host.CLAUDE].unknown[0].missing_tree,
                         FactKey(Host.CLAUDE, Surface.TREE, 'kit@personal-collections'))
        self.assertEqual([(d.kind.value, d.key.surface.name) for d in current.changes],
                         [('changed', 'TREE')])
        self.calls.clear()
        after_recovery = self.run_apply(current)
        self.assertEqual(self.calls, [])
        self.assertTrue((destination / 'skills/one/SKILL.md').is_file())
        self.assertEqual([(d.kind.value, d.key.subject) for d in after_recovery.changes
                          if d.key and d.key.surface == Surface.SKILL], [('changed', 'kit@personal-collections:one')])
        self.assertEqual(self.run_apply(after_recovery).exit_code, 0)
        self.assertEqual(self.calls, [])
        # Claude Code runs the catalog's copy, so flags are read there; an edited cache still stops the plan.
        (destination / 'skills/one/SKILL.md').write_text('---\nnot valid yaml: [\n---\n')
        edited = plan(self.setup, self.device)
        self.assertEqual(edited.exit_code, 2, edited.blocked)
        (self.roots.engine / 'claude/plugins/kit/1.0.0/skills/one/SKILL.md').write_text('---\nnot valid yaml: [\n---\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 3)

    def test_aligned_claude_missing_cache_defers_alignment_and_skill_reads(self):
        destination = self._interrupted_claude_install(aligned=True)
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 1, [(d.kind.value, d.key, d.detail) for d in current.result.differences])
        self.assertEqual(len(current.observations[Host.CLAUDE].unknown), 1)
        self.calls.clear()
        self.assertEqual(self.run_apply(current).exit_code, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'revised release bytes\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)
        (destination / '.claude-plugin/codex-alignment.json').write_text('{bad json')
        self.assertEqual(plan(self.setup, self.device).exit_code, 3)

    def test_aligned_claude_implicit_skills_missing_cache_recovers(self):
        destination = self._interrupted_claude_install(aligned=True, implicit=True)
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 1, [(d.kind.value, d.key, d.detail) for d in current.result.differences])
        self.assertEqual(len(current.observations[Host.CLAUDE].unknown), 1)
        self.calls.clear()
        self.assertEqual(self.run_apply(current).exit_code, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'revised release bytes\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_pending_claude_cache_unknown_stage_or_displaced_blocks(self):
        self._interrupted_claude_install()
        pending = json.loads(self._pending_claude_receipt().read_bytes())['pending']
        for field in ('staged', 'displaced'):
            with self.subTest(field=field):
                ordinary = Path(pending[field]) / 'ordinary.txt'
                original = ordinary.read_bytes()
                ordinary.write_bytes(b'unrecognized bytes\n')
                before = self.snapshot()
                current = plan(self.setup, self.device)
                self.assertEqual(current.exit_code, 3)
                with self.assertRaises(RuntimeError):
                    self.run_apply(current)
                self.assertEqual(self.snapshot(), before)
                ordinary.write_bytes(original)

    def test_pending_claude_cache_changed_binding_or_stage_path_blocks(self):
        self._interrupted_claude_install()
        receipt = self._pending_claude_receipt()
        original = json.loads(receipt.read_bytes())
        for field in ('binding', 'stage'):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(original))
                if field == 'binding':
                    changed['pending']['target']['bindings'][0]['physical'] = '/retargeted'
                else:
                    changed['pending']['staged'] = str(Path(changed['pending']['staged']).parent.parent / 'elsewhere')
                receipt.write_text(json.dumps(changed))
                before = self.snapshot()
                current = plan(self.setup, self.device)
                self.assertEqual(current.exit_code, 3)
                with self.assertRaises(RuntimeError):
                    self.run_apply(current)
                self.assertEqual(self.snapshot(), before)
                receipt.write_text(json.dumps(original))

    def test_claude_missing_cache_without_pending_does_not_defer_skill_error(self):
        self._interrupted_claude_install()
        receipt = self._pending_claude_receipt()
        receipt.rename(receipt.with_suffix('.retained'))
        before = self.snapshot()
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 3)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertEqual(self.snapshot(), before)

    def test_pending_claude_linked_work_parent_blocks(self):
        self._interrupted_claude_install()
        pending = json.loads(self._pending_claude_receipt().read_bytes())['pending']
        work = Path(pending['staged']).parent
        retained = work.with_name(work.name + '-retained')
        work.rename(retained)
        work.symlink_to(retained, target_is_directory=True)
        before = self.snapshot()
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 3)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertEqual(self.snapshot(), before)

    def test_pending_claude_cache_other_skill_unread_still_blocks(self):
        from claude_host import ClaudeState

        destination = self._interrupted_claude_install()
        state = self.device.host(Host.CLAUDE)
        original = state.observe
        plugin = 'kit@personal-collections'
        extra = Unknown(self.roots.tokens(destination), 'other read failed', frozenset({Surface.SKILL}),
                        frozenset({plugin}))
        for first in (True, False):
            with self.subTest(extra_first=first):
                def observe(request):
                    result = original(request)
                    unknown = (extra, *result.unknown) if first else (*result.unknown, extra)
                    return replace(result, unknown=unknown)

                with patch.object(state, 'observe', observe):
                    before = self.snapshot()
                    current = plan(self.setup, self.device)
                    self.assertEqual(current.exit_code, 3)
                    with self.assertRaises(RuntimeError):
                        self.run_apply(current)
                    self.assertEqual(self.snapshot(), before)
        with patch.object(ClaudeState, '_explicit', side_effect=Unread('other skill read failed')):
            self.assertEqual(plan(self.setup, self.device).exit_code, 3)

    def test_pending_aligned_implicit_cache_other_orphan_unread_still_blocks(self):
        destination = self._interrupted_claude_install(aligned=True, implicit=True)
        state = self.device.host(Host.CLAUDE)
        original = state.observe
        extra = Unknown(self.roots.tokens(destination), 'independent unread', frozenset({Surface.SKILL}),
                        frozenset({'other@codex-aligned'}))

        def observe(request):
            result = original(request)
            return replace(result, unknown=(*result.unknown, extra))

        with patch.object(state, 'observe', observe):
            before = self.snapshot()
            current = plan(self.setup, self.device)
            self.assertEqual(current.exit_code, 3)
            self.assertEqual(len([d for d in current.blocked if d.kind.value == 'incomplete']), 1)
            with self.assertRaises(RuntimeError):
                self.run_apply(current)
            self.assertEqual(self.snapshot(), before)

    def test_claude_non_directory_or_unreadable_cache_has_no_missing_marker(self):
        destination = self._interrupted_claude_install()
        destination.write_text('not a directory')
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 3)
        self.assertIsNone(current.observations[Host.CLAUDE].unknown[0].missing_tree)
        destination.unlink()
        stat = Path.stat

        def denied(path, *args, **kwargs):
            if path == destination:
                raise PermissionError('test permission denial')
            return stat(path, *args, **kwargs)

        with patch.object(Path, 'stat', denied):
            current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 3)
        self.assertIsNone(current.observations[Host.CLAUDE].unknown[0].missing_tree)

    def test_whole_absence_can_recreate_an_accepted_tree(self):
        self.run_apply()
        shutil.rmtree(self.installed())
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.installed() / 'ordinary.txt').read_text(), 'ordinary release bytes\n')

    def test_stale_receipt_adopts_exact_new_desired_bytes(self):
        self.run_apply()
        self.setup = replace(self.setup, rules=Rules(b'Already deployed reviewed rules.\n'))
        path = self.roots.codex_home / 'AGENTS.md'
        path.write_bytes(self.setup.rules.text)
        before = path.stat().st_mtime_ns
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(path.read_bytes(), b'Already deployed reviewed rules.\n')

    def test_partial_deletion_and_raw_kind_changes_are_not_absence(self):
        self.run_apply()
        path = self.installed() / 'ordinary.txt'
        path.unlink()
        self.assertEqual(plan(self.setup, self.device).exit_code, 2)
        path.mkdir()
        self.assertEqual(plan(self.setup, self.device).exit_code, 2)
        self.assertTrue(path.is_dir())

    def test_malformed_receipt_is_incomplete_and_preserved(self):
        self.run_apply()
        receipt = self.receipt_paths()[0]
        receipt.write_text('{not valid json')
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 3)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertEqual(receipt.read_text(), '{not valid json')

    def test_shared_rule_alias_has_one_receipt_and_retargeting_blocks(self):
        from claude_host import ClaudeState
        from types import SimpleNamespace
        self.roots.claude_home.mkdir(parents=True)
        alias = self.roots.claude_home / 'CLAUDE.md'
        alias.symlink_to(self.roots.codex_home / 'AGENTS.md')

        def observe(request):
            seen = Observer(Host.CLAUDE, self.roots)
            if alias.is_file():
                seen.add(Surface.RULES, 'CLAUDE.md', FileValue(bytes_digest(alias.read_bytes())))
            return seen.finish('rules host', set())
        claude = SimpleNamespace(host=Host.CLAUDE, project=lambda setup: ClaudeState.project(None, setup), observe=observe)
        self.device = replace(self.device, hosts=(self.host, claude))
        self.assertEqual(self.run_apply().exit_code, 0)
        receipts = [json.loads(p.read_bytes()) for p in self.receipt_paths()]
        self.assertEqual(len([r for r in receipts if r['identity'].startswith('rules:')]), 1)
        self.assertEqual(alias.read_bytes(), b'Managed rules.\n')
        alias.unlink()
        destination = self.root / 'other-rules.md'
        alias.symlink_to(destination)
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 2)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertFalse(destination.exists())

    def test_private_models_and_runtime_fields_are_owned_without_touching_other_settings(self):
        private = self.root / 'private'
        source = private / 'config/pstack'
        source.mkdir(parents=True)
        models = b'## codex\ncode: auto@high\n## claude-code\ncode: inherit-parent@high\n'
        (source / 'models.md').write_bytes(models)
        (source / 'runtime.toml').write_text('spawn_overrides = true\n')
        self.setup = replace(self.setup, roots=replace(self.setup.roots, private=private / 'setup'))
        self.roots.codex_home.mkdir(parents=True)
        runtime = self.roots.codex_home / 'config.toml'
        runtime.write_text('# preserved\n[mcp_servers.example.env]\nTOKEN = "test-only-local"\n')
        with patch('accepted_installations.validate_runtime') as validate:
            self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual([call.kwargs['proposed'] for call in validate.call_args_list], [True, False])
        models_path = self.roots.home / '.config/pstack/models.md'
        self.assertEqual(models_path.read_bytes(), models)
        self.assertIn('TOKEN = "test-only-local"', runtime.read_text())
        self.assertIn('expose_spawn_agent_model_overrides = true', runtime.read_text())
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.receipt_paths()}
        with patch('accepted_installations.validate_runtime', side_effect=RuntimeError('effective runtime unavailable')):
            for _ in range(2):
                failed = plan(self.setup, self.device)
                self.assertEqual(failed.exit_code, 3)
                self.assertTrue(any('effective runtime unavailable' in issue.detail for issue in failed.blocked))
                with self.assertRaises(RuntimeError):
                    self.run_apply(failed)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.receipt_paths()}, before)
        models_path.write_bytes(models + b'local edit\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 2)
        models_path.write_bytes(models)
        runtime.write_text(runtime.read_text().replace('expose_spawn_agent_model_overrides = true',
                                                       'expose_spawn_agent_model_overrides = false'))
        self.assertEqual(plan(self.setup, self.device).exit_code, 2)

    def test_excluded_model_stops_planning_before_managed_writes(self):
        private = self.root / 'private'
        source = private / 'config/pstack'
        source.mkdir(parents=True)
        (source / 'models.md').write_text(
            '## claude-code\ncode: opus@high\n## codex\ncode: retired-model@high\n')
        (source / 'runtime.toml').write_text(
            'spawn_overrides = true\nexcluded_codex_models = ["retired-model"]\n')
        self.setup = replace(self.setup, roots=replace(self.setup.roots, private=private / 'setup'))
        before = list(self.roots.home.iterdir())
        with self.assertRaisesRegex(ValueError, 'Excluded Codex model for codex/code: retired-model'):
            self.run_apply()
        self.assertEqual(list(self.roots.home.iterdir()), before)
        self.assertEqual(self.calls, [])

    def test_toml_unmanaged_concurrent_edit_survives_managed_update(self):
        from sync_hosts import Apply
        source = self.public / 'config/mise/config.toml'
        source.parent.mkdir(parents=True)
        source.write_text('[tools]\nnode = "24"\n')
        self.run_apply()
        source.write_text('[tools]\nnode = "25"\n')
        live = self.roots.home / '.config/mise/config.toml'
        run = Apply.run

        def race(operation):
            live.write_text(live.read_text() + '\n[env]\nSECRET = "test-only-concurrent-value"\n')
            return run(operation)
        with patch.object(Apply, 'run', race):
            self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('node = "25"', live.read_text())
        self.assertIn('SECRET = "test-only-concurrent-value"', live.read_text())


class LeaseHolderTest(unittest.TestCase):
    """Real processes, because the point is telling a running Claude session from one that has exited."""

    def test_a_running_process_holds_its_lease(self):
        import os
        from accepted_installations import _lease_held
        stat = Path('/proc/self/stat')
        start = {'procStart': stat.read_text().rsplit(')', 1)[1].split()[19]} if stat.exists() else {}
        self.assertTrue(_lease_held({'pid': os.getpid(), **start}))

    def test_a_finished_process_holds_no_lease(self):
        import subprocess
        from accepted_installations import _lease_held
        child = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'],
                               capture_output=True, text=True, check=True)
        self.assertFalse(_lease_held({'pid': int(child.stdout)}))

    def test_a_running_process_holds_its_lease_whatever_windows_start_it_records(self):
        import os
        from accepted_installations import _lease_held
        self.assertTrue(_lease_held({'pid': os.getpid(), 'procStartFt': '1'}))

    @unittest.skipUnless(os.name == 'nt', 'only Windows markers hold a procStart that is not the clock ticks Linux uses')
    def test_a_running_process_holds_its_lease_whatever_procstart_claude_records_on_windows(self):
        from accepted_installations import _lease_held
        self.assertTrue(_lease_held({'pid': os.getpid(), 'procStart': '639202300505758780'}))

    @unittest.skipUnless(Path('/proc/self/stat').exists(), 'only Linux records the start time sync can compare')
    def test_a_reused_process_id_holds_no_lease(self):
        import os
        from accepted_installations import _lease_held
        self.assertFalse(_lease_held({'pid': os.getpid(), 'procStart': '1'}))

    @unittest.skipUnless(Path('/proc/self/stat').exists(), 'only Linux records the start time sync can compare')
    def test_a_lease_is_held_where_proc_cannot_tell_its_process(self):
        from accepted_installations import _lease_held
        with patch('portable._proc_shows_host', return_value=False):  # a sandbox's own /proc
            self.assertTrue(_lease_held({'pid': os.getpid(), 'procStart': '1'}))
        self.assertTrue(_lease_held({'pid': os.getpid(), 'procStart': 'Thu Oct 15 12:30:00 2026'}))


if __name__ == '__main__':
    unittest.main()
