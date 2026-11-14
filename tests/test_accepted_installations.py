import contextlib
from dataclasses import replace
import inspect
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
from description import (Adaptation, AlignedInstall, ClaudePlacement, CodexPlacement, EngineInstall, Hooks, HostMarketplace,
                         LocalSource, Lock, LockedTree, LooseSkill, MarketplaceInstall, MarketplaceMode, NoteRule, Package,
                         PluginRoles, Repo, RepoSource, Roots, Rules, Setup, SkillRoot, SnapshotSource, ToolNotes,
                         Visibility, intent)
from device import Device
from device_sync import apply, plan
from homes import hold_built_trees_once, isolate_git
from host_observation import Observer, Unread
from host_state import (Approved, DeviceRoots, EngineMarketplace, FactKey, FileValue, Host, InstallDetail, Pinned,
                        Platform, PluginValue, Released, SkillMode, Surface, TreeValue, Unknown, bytes_digest)
import portable
from sync_files import ConcurrentEdit
from sync_sources import BuildError, Builder, Sources

# The words after the host name in the warning that a sync prints when it cannot switch a kept-current plugin off
# after a failed install.
COULD_NOT_SWITCH = 'could not switch'
ATTEMPTS = inspect.signature(portable.while_held).parameters['attempts'].default
WAITED = inspect.signature(portable.while_held).parameters['delay'].default * (ATTEMPTS - 1)


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


def moving_aside(tree):
    return lambda source, target: source == tree and target.name == 'displaced'


def moving_into_place(tree):
    return lambda source, target: source.name == 'staged' and target == tree


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

    @contextlib.contextmanager
    def refusing(self, moves=lambda source, target: False, replaces=lambda source, target: False,
                 at_last_attempt=None):
        refusals = []

        def refused(operation, check):
            def attempt(source, target, *args, **kwargs):
                if not check(Path(source), Path(target)):
                    return operation(source, target, *args, **kwargs)
                refusals.append(PermissionError(13, 'Access is denied', str(source), None, str(target)))
                if at_last_attempt and len(refusals) % ATTEMPTS == 0:
                    at_last_attempt()
                raise refusals[-1]
            return attempt

        with patch.object(portable, 'RETRIES_REFUSALS', True), patch.object(portable.time, 'sleep'), \
                patch('os.rename', refused(os.rename, moves)), patch('os.replace', refused(os.replace, replaces)):
            yield refusals

    def release_a_setting_written_after_the_plugins(self):
        config = self.public / 'config/npm/npmrc.toml'
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('min-release-age = 3\n')
        return self.roots.home / '.npmrc'

    def release_in_place(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        self.revise_loose('A second loose skill.\n')
        return self.installed(), self.release_a_setting_written_after_the_plugins()

    def move_refused(self, installed, refusal):
        return (f'Move refused: {installed}: Windows refused the move on each of {ATTEMPTS} tries over {WAITED:g} '
                f'seconds ({refusal}), as it does while a program holds a file in the folder being moved open, or '
                'while access rules forbid the move; this replacement waits as a pending one, and the next pull tries '
                'it again')

    def assert_move_waits(self, result, installed, npmrc, refusals):
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.blocked, ())
        self.assertEqual([d.detail for d in result.changes],
                         ['plugin:codex:kit@personal: Finish a verified pending replacement'])
        [held] = result.held
        self.assertEqual((type(held), held.path, held.line),
                         (accepted_installations.HeldMove, installed, self.move_refused(installed, refusals[-1])))
        self.assertIs(held.refusal.__cause__, refusals[-1])
        self.assertEqual(len(refusals), ATTEMPTS)
        self.assertIn('use the local reader', (installed / 'skills/one/SKILL.md').read_text())
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A second loose skill.\n')
        self.assertEqual(npmrc.read_text(), 'min-release-age=3\n')

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
        self.assertEqual(self.apply_reporting(), f'Work folder left in place: {work}: it is not one of '
                                                 f"{self.roots.codex_home / 'skills/loose'}'s work folders\n")
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
                self.assertIn(f'Work folder left in place: {named.parent}: it is not one of '
                              f"{skills / 'loose'}'s work folders\n", self.apply_reporting())
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
                           ('under another name', destination.parent / name / 'displaced'),
                           ('without the prefix', destination.parent / ('ab' * 16) / 'staged'),
                           ('with a long suffix', destination.parent / (name + 'x') / 'staged'),
                           ('in a work folder that is a link', destination.parent / name.replace('ab', 'cd') / 'staged')):
            with self.subTest(case):
                if 'link' in case:
                    (self.root / 'linked').mkdir()
                    mine.parent.symlink_to(self.root / 'linked', target_is_directory=True)
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

        def stop(source, destination, *args):
            copy_tree(source, destination, *args)
            if destination.name == 'staged':
                (destination / 'SKILL.md').unlink()
                raise RuntimeError('stopped while copying the stage')

        with patch('accepted_installations.copy_tree', stop), \
                self.assertRaisesRegex(RuntimeError, 'stopped while copying the stage'):
            self.run_apply()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A second loose skill.\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def copy_stopped_inside_a_file(self, data):
        self.assertEqual(self.run_apply().exit_code, 0)
        self.revise_loose('A second loose skill.\n')
        copy_tree = accepted_installations.copy_tree

        def stop(source, destination, *args):
            copy_tree(source, destination, *args)
            if destination.name == 'staged':
                (destination / 'SKILL.md').write_bytes(data)
                raise RuntimeError('stopped while copying the stage')

        with patch('accepted_installations.copy_tree', stop), \
                self.assertRaisesRegex(RuntimeError, 'stopped while copying the stage'):
            self.run_apply()
        stage, = (self.roots.codex_home / 'skills').glob('.sync-loose-*/staged')
        return stage

    def test_a_stage_whose_copy_stopped_before_it_wrote_a_file_is_built_again(self):
        self.copy_stopped_inside_a_file(b'')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A second loose skill.\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_stage_whose_copy_stopped_part_way_through_a_file_is_built_again(self):
        self.copy_stopped_inside_a_file(b'A second lo')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A second loose skill.\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_stage_file_that_does_not_begin_as_the_release_s_file_stops_its_release_and_stays(self):
        stage = self.copy_stopped_inside_a_file(b'A second edit')
        with self.assertRaisesRegex(ConcurrentEdit, f'^Prepared stage was edited: {re.escape(str(stage))}$'):
            self.run_apply()
        self.assertEqual((stage / 'SKILL.md').read_bytes(), b'A second edit')

    def test_a_stage_that_gained_a_file_its_release_lacks_stops_its_release_and_stays(self):
        stage = self.copy_stopped_inside_a_file(b'')
        (stage / 'mine.txt').write_text('mine')
        with self.assertRaisesRegex(ConcurrentEdit, f'^Prepared stage was edited: {re.escape(str(stage))}$'):
            self.run_apply()
        self.assertEqual((stage / 'mine.txt').read_text(), 'mine')

    def test_a_stage_that_holds_a_folder_where_its_release_has_a_file_stops_its_release_and_stays(self):
        stage = self.copy_stopped_inside_a_file(b'')
        (stage / 'SKILL.md').unlink()
        (stage / 'SKILL.md').mkdir()
        with self.assertRaisesRegex(ConcurrentEdit, f'^Prepared stage was edited: {re.escape(str(stage))}$'):
            self.run_apply()
        self.assertTrue((stage / 'SKILL.md').is_dir())

    def test_a_link_that_appears_where_the_old_tree_goes_back_keeps_the_tree_aside(self):
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        loose = self.roots.codex_home / 'skills/loose'
        rename = os.rename

        def refuse(source, target, *args, **kwargs):
            if Path(source).name == 'staged':
                if not loose.is_symlink():
                    loose.symlink_to(self.root / 'nowhere')
                raise PermissionError(13, 'Access is denied', str(target))
            return rename(source, target, *args, **kwargs)

        with patch('sys.stderr', new_callable=io.StringIO) as stderr, patch('os.rename', refuse), \
                patch('portable.time.sleep'), self.assertRaises(PermissionError):
            self.run_apply()
        displaced = Path(self.pending_of('codex:LOOSE_SKILL')['displaced'])
        self.assertEqual(stderr.getvalue(), f'Old tree left aside: {displaced}, from {loose}: its place is taken\n')
        self.assertTrue(loose.is_symlink())
        self.assertEqual((displaced / 'SKILL.md').read_text(), 'A second loose skill.\n')

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
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assert_held(destination)
        self.assertEqual(stderr.getvalue(),
                         f'Work folder left in place: {staged.parent}: its receipt names a displaced tree outside it\n')
        self.assertTrue(staged.is_dir())

    def test_a_stopped_replacement_whose_receipt_names_another_displaced_path_and_no_work_folder_names_none(self):
        destination = self._installed_claude()
        own_lease(destination / '.in_use')
        self.revise_release('first revision\n')
        self.assert_held(destination)
        receipt = self._pending_claude_receipt()
        value = json.loads(receipt.read_bytes())
        shutil.rmtree(Path(value['pending']['staged']).parent)
        value['pending']['displaced'] = str(destination.parent / ('.sync-1.0.0-' + 'ab' * 16) / 'displaced')
        receipt.write_text(json.dumps(value))
        self.revise_release('second revision\n')
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assert_held(destination)
        self.assertEqual(stderr.getvalue(), '')

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
        moved, rename = [], os.rename

        def recorded(source, target, *args, **kwargs):
            result = rename(source, target, *args, **kwargs)
            moved.append(Path(target).name)
            return result

        with patch('os.rename', recorded), self.assertRaises(ConcurrentEdit) as stopped:
            self.run_apply()
        [stage] = installed.parent.glob('.sync-*/staged')
        self.assertEqual(str(stopped.exception), f'Prepared stage was edited: {stage}')
        self.assertIn('use the local reader', (installed / 'skills/one/SKILL.md').read_text())
        self.assertEqual([path.name for path in stage.parent.iterdir()], ['staged'])
        shutil.rmtree(stage)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (installed / 'skills/one/SKILL.md').read_text())
        return moved

    def test_a_stage_edited_while_windows_holds_the_old_tree_leaves_the_old_tree_where_it_is(self):
        self.assertEqual(self.stage_edited_during_the_wait_for('displaced'), [])

    def test_a_stage_edited_while_windows_holds_the_new_tree_puts_the_old_tree_back(self):
        self.assertEqual(self.stage_edited_during_the_wait_for('1.0.0'), ['displaced', '1.0.0'])

    def test_a_stage_edited_before_a_first_install_moves_it_into_place_stops_the_install(self):
        loose = self.roots.codex_home / 'skills/loose'
        with self.edit_stage_when_the_receipt_is_saved(loose), self.assertRaises(ConcurrentEdit) as stopped:
            self.run_apply()
        [stage] = loose.parent.glob('.sync-loose-*/staged')
        self.assertEqual(str(stopped.exception), f'Prepared stage was edited: {stage}')
        self.assertFalse(loose.exists())

    def edit_stage_when_the_receipt_is_saved(self, destination):
        from accepted_installations import AcceptedInstallations
        save = AcceptedInstallations._save

        def edited(owner, artifact, accepted, pending=None):
            save(owner, artifact, accepted, pending)
            if pending and Path(pending['target']['path']) == destination and Path(pending['staged']).is_dir():
                next(path for path in sorted(Path(pending['staged']).rglob('*')) if path.is_file()).write_text(
                    'edited while the receipt was saved\n')

        return patch.object(AcceptedInstallations, '_save', edited)

    def test_a_stage_edited_before_the_old_tree_is_moved_aside_moves_nothing(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        installed = self.installed()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        moved, rename = [], os.rename

        def recorded(source, target, *args, **kwargs):
            moved.append(Path(target).name)
            return rename(source, target, *args, **kwargs)

        with self.edit_stage_when_the_receipt_is_saved(installed), patch('os.rename', recorded), \
                self.assertRaisesRegex(ConcurrentEdit, '^Prepared stage was edited: '):
            self.run_apply()
        self.assertNotIn('displaced', moved)
        self.assertIn('use the local reader', (installed / 'skills/one/SKILL.md').read_text())

    def test_an_empty_folder_that_appears_where_the_new_tree_goes_stays_and_the_old_tree_stays_aside(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        installed = self.installed()
        hold_built_trees_once(self, meanwhile=lambda target: target.name != 'displaced' and target.mkdir())
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        with patch('sys.stderr', new_callable=io.StringIO) as stderr, \
                self.assertRaisesRegex(ConcurrentEdit, f'^Tree changed during replacement: {re.escape(str(installed))}$'):
            self.run_apply()
        self.assertEqual(list(installed.iterdir()), [])
        displaced = Path(json.loads(self.receipt_of('plugin:').read_bytes())['pending']['displaced'])
        self.assertIn('use the local reader', (displaced / 'skills/one/SKILL.md').read_text())
        self.assertEqual(stderr.getvalue(), f'Old tree left aside: {displaced}, from {installed}: its place is taken\n')

    def test_an_empty_folder_where_a_stopped_swap_left_a_loose_skill_absent_blocks(self):
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_between_the_moves()
        (self.roots.codex_home / 'skills/loose').mkdir()
        self.assertEqual([d.detail for d in plan(self.setup, self.device).blocked],
                         ['codex:LOOSE_SKILL:{codex_home}/skills/loose: Unknown partial installation preserved: '
                          'codex:LOOSE_SKILL:{codex_home}/skills/loose'])

    def move_into_place_refused(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        installed = self.installed()
        rename = os.rename

        def refuse(source, target, *args, **kwargs):
            if Path(source).name == 'staged':
                raise PermissionError(13, 'Access is denied', str(target))
            return rename(source, target, *args, **kwargs)

        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        with patch.object(portable, 'RETRIES_REFUSALS', False), patch('os.rename', refuse), \
                self.assertRaises(PermissionError) as stopped:
            self.run_apply()
        self.assertEqual(Path(stopped.exception.filename), installed)
        self.assertIn('use the local reader', (installed / 'skills/one/SKILL.md').read_text())
        return installed

    def test_a_new_tree_that_cannot_be_moved_into_place_leaves_the_old_tree_in_place(self):
        installed = self.move_into_place_refused()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (installed / 'skills/one/SKILL.md').read_text())

    def test_a_newer_release_takes_over_a_replacement_whose_old_tree_was_put_back(self):
        installed = self.move_into_place_refused()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the newer release'),))
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the newer release', (installed / 'skills/one/SKILL.md').read_text())
        [work] = installed.parent.glob(f'.sync-{installed.name}-*')
        self.assertEqual([path.name for path in work.iterdir()], ['displaced'])
        self.assertIn('use the local reader', (work / 'displaced/skills/one/SKILL.md').read_text())

    def test_a_tree_that_cannot_be_read_again_while_windows_holds_it_is_left_where_it_is(self):
        probe = self.root / 'probe'
        probe.write_text('')
        probe.chmod(0)
        try:
            probe.read_bytes()
        except PermissionError:
            pass
        else:
            self.skipTest('needs a file its owner cannot read')
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

    def test_an_old_tree_windows_refuses_to_move_aside_waits_and_the_rest_of_the_release_applies(self):
        installed, npmrc = self.release_in_place()
        with self.refusing(moves=moving_aside(installed)) as refusals, \
                patch('sys.stderr', new_callable=io.StringIO) as stderr:
            result = self.run_apply()
        self.assert_move_waits(result, installed, npmrc, refusals)
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (installed / 'skills/one/SKILL.md').read_text())

    def test_a_new_tree_windows_refuses_to_move_into_place_waits_with_the_old_tree_put_back(self):
        installed, npmrc = self.release_in_place()
        with self.refusing(moves=moving_into_place(installed)) as refusals, \
                patch('sys.stderr', new_callable=io.StringIO) as stderr:
            result = self.run_apply()
        self.assert_move_waits(result, installed, npmrc, refusals)
        self.assertEqual(stderr.getvalue(), '')
        pending = json.loads(self.receipt_of('plugin:').read_bytes())['pending']
        self.assertEqual([path.name for path in Path(pending['staged']).parent.iterdir()], ['staged'])
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (installed / 'skills/one/SKILL.md').read_text())

    def test_a_newer_release_takes_over_a_replacement_waiting_on_a_refused_move(self):
        installed, npmrc = self.release_in_place()
        with self.refusing(moves=moving_aside(installed)) as refusals:
            self.assert_move_waits(self.run_apply(), installed, npmrc, refusals)
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the newer release'),))
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the newer release', (installed / 'skills/one/SKILL.md').read_text())
        self.assertEqual(self.unnamed_work_folders(), [])

    def stage_edited_during_the_last_refused_attempt(self, refused):
        installed, npmrc = self.release_in_place()

        def edit():
            [stage] = installed.parent.glob('.sync-*/staged')
            (stage / 'ordinary.txt').write_text('edited during the last attempt\n')

        with self.refusing(moves=refused(installed), at_last_attempt=edit) as refusals, \
                self.assertRaises(portable.StillHeld) as stopped:
            self.run_apply()
        self.assertIs(stopped.exception.__cause__, refusals[-1])
        self.assertFalse(npmrc.exists())
        self.assertIn('use the local reader', (installed / 'skills/one/SKILL.md').read_text())
        [stage] = installed.parent.glob('.sync-*/staged')
        self.assertEqual((stage / 'ordinary.txt').read_text(), 'edited during the last attempt\n')
        with self.assertRaisesRegex(ConcurrentEdit, f'^Prepared stage was edited: {re.escape(str(stage))}$'):
            self.run_apply()
        self.assertEqual((stage / 'ordinary.txt').read_text(), 'edited during the last attempt\n')

    def test_a_stage_edited_during_the_last_refused_move_aside_stops_the_sync_and_keeps_the_edit(self):
        self.stage_edited_during_the_last_refused_attempt(moving_aside)

    def test_a_stage_edited_during_the_last_refused_move_into_place_stops_the_sync_and_keeps_the_edit(self):
        self.stage_edited_during_the_last_refused_attempt(moving_into_place)

    def test_an_old_tree_windows_refuses_to_put_back_after_a_refused_move_into_place_stops_the_sync(self):
        installed, npmrc = self.release_in_place()
        with self.refusing(moves=lambda source, target: target == installed) as refusals, \
                patch('sys.stderr', new_callable=io.StringIO) as stderr, self.assertRaises(portable.StillHeld) as stopped:
            self.run_apply()
        displaced = Path(json.loads(self.receipt_of('plugin:').read_bytes())['pending']['displaced'])
        self.assertIs(stopped.exception.__cause__, refusals[ATTEMPTS - 1])
        self.assertEqual(stderr.getvalue(), f'Old tree left aside: {displaced}, from {installed}: {refusals[-1]}\n')
        self.assertFalse(installed.exists())
        self.assertFalse(npmrc.exists())

    def test_a_codex_version_bump_whose_installed_tree_windows_refuses_to_move_aside_stops_the_sync(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        old = self.installed()
        (self.source / '.codex-plugin/plugin.json').write_text('{"name":"kit","version":"1.1.0"}')
        digest = self.roots.tree_digest(self.source)
        package = replace(self.setup.packages['kit'], source=LocalSource('owned/kit', digest))
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}), trees={Host.CODEX: LockedTree('1.1.0', digest)})
        self.setup = replace(self.setup, packages={'kit': package}, lock={**self.setup.lock, 'kit': lock})
        npmrc = self.release_a_setting_written_after_the_plugins()
        def plugin_add_removes_the_other_versions(installed):
            shutil.rmtree(old)

        self.interrupt = plugin_add_removes_the_other_versions
        new = old.parent / '1.1.0'
        with self.refusing(moves=moving_aside(new)) as refusals, \
                self.assertRaises(portable.StillHeld) as stopped:
            self.run_apply()
        self.assertIs(stopped.exception.__cause__, refusals[-1])
        self.assertEqual([args[:2] for _, args in self.calls][-1:], [('plugin', 'add')])
        self.assertFalse(old.exists())
        self.assertFalse(npmrc.exists())

    def receipt_save_refused(self, saves_allowed):
        installed, npmrc = self.release_in_place()
        receipt, saves = self.receipt_of('plugin:'), []

        def refused(source, target):
            if target != receipt:
                return False
            saves.append(source)
            return len(saves) > saves_allowed

        with self.refusing(replaces=refused) as refusals, self.assertRaises(portable.StillHeld) as stopped:
            self.run_apply()
        self.assertIs(stopped.exception.__cause__, refusals[-1])
        self.assertEqual(len(refusals), ATTEMPTS)
        self.assertFalse(npmrc.exists())
        self.assertIn('use the local reader', (installed / 'skills/one/SKILL.md').read_text())
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (installed / 'skills/one/SKILL.md').read_text())
        self.assertEqual(npmrc.read_text(), 'min-release-age=3\n')

    def test_a_pending_replacement_whose_first_save_windows_refuses_stops_the_sync_and_the_next_sync_finishes(self):
        self.receipt_save_refused(0)

    def test_a_save_before_the_folder_moves_that_windows_refuses_stops_the_sync_and_the_next_sync_finishes(self):
        self.receipt_save_refused(1)

    def test_a_settings_write_windows_refuses_stops_the_sync(self):
        installed, npmrc = self.release_in_place()
        with self.refusing(replaces=lambda source, target: target == npmrc) as refusals, \
                self.assertRaises(portable.StillHeld) as stopped:
            self.run_apply()
        self.assertIs(stopped.exception.__cause__, refusals[-1])
        self.assertFalse(npmrc.exists())

    def receipt_changed_during_the_last_refused_move(self, change):
        installed, npmrc = self.release_in_place()
        receipt = self.receipt_of('plugin:')
        with self.refusing(moves=moving_aside(installed), at_last_attempt=lambda: change(receipt)) as refusals, \
                self.assertRaises(portable.StillHeld) as stopped:
            self.run_apply()
        self.assertIs(stopped.exception.__cause__, refusals[-1])
        self.assertFalse(npmrc.exists())
        self.assertIn('use the local reader', (installed / 'skills/one/SKILL.md').read_text())

    def test_a_receipt_removed_during_the_last_refused_move_stops_the_sync(self):
        self.receipt_changed_during_the_last_refused_move(Path.unlink)

    def test_a_receipt_changed_during_the_last_refused_move_stops_the_sync(self):
        def changed(receipt):
            value = json.loads(receipt.read_bytes())
            value['pending']['destination_before'] = {'kind': 'absent'}
            receipt.write_text(json.dumps(value))

        self.receipt_changed_during_the_last_refused_move(changed)

    def test_a_receipt_that_cannot_be_read_after_the_last_refused_move_stops_the_sync_with_the_refusal(self):
        self.receipt_changed_during_the_last_refused_move(lambda receipt: receipt.write_text('not a receipt'))

    def test_a_malformed_receipt_after_the_last_refused_move_preserves_the_stop_message(self):
        installed, npmrc = self.release_in_place()
        receipt = self.receipt_of('plugin:')
        saved = receipt.read_bytes()
        malformed = json.loads(saved)
        del malformed['accepted']['bindings'][0]['path']
        missing_path = json.dumps(malformed)
        malformed['accepted']['bindings'] = None
        for changed in ('[]', missing_path, json.dumps(malformed)):
            with self.subTest(receipt=changed):
                receipt.write_bytes(saved)
                with self.refusing(moves=moving_aside(installed),
                                   at_last_attempt=lambda: receipt.write_text(changed)) as refusals:
                    self.assertEqual(self.sync_main(), (3, f'Sync stopped: {refusals[-1]}\n'))
                self.assertEqual(receipt.read_text(), changed)
                self.assertFalse(npmrc.exists())

    def precached_codex_update(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        old = self.installed()
        (self.source / '.codex-plugin/plugin.json').write_text('{"name":"kit","version":"1.1.0"}')
        digest = self.roots.tree_digest(self.source)
        package = replace(self.setup.packages['kit'], source=LocalSource('owned/kit', digest))
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}), trees={Host.CODEX: LockedTree('1.1.0', digest)})
        self.setup = replace(self.setup, packages={'kit': package}, lock={**self.setup.lock, 'kit': lock})
        new = old.parent / '1.1.0'
        shutil.copytree(self.source, new)
        return old, new, self.release_a_setting_written_after_the_plugins()

    def test_a_precached_update_with_a_changed_previous_tree_stops_before_other_writes(self):
        old, new, npmrc = self.precached_codex_update()
        edited = old / 'ordinary.txt'
        with self.refusing(moves=moving_aside(new),
                           at_last_attempt=lambda: edited.write_text('concurrent edit\n')) as refusals:
            self.assertEqual(self.sync_main(), (3, f'Sync stopped: {refusals[-1]}\n'))
        self.assertEqual(edited.read_text(), 'concurrent edit\n')
        self.assertFalse(npmrc.exists())
        self.assertTrue(json.loads(self.receipt_of('plugin:').read_bytes())['pending'])

    def test_a_precached_update_waits_and_finishes_after_the_previous_version_is_removed(self):
        old, new, npmrc = self.precached_codex_update()
        with self.refusing(moves=moving_aside(new)) as refusals:
            self.assertEqual(self.sync_main(), (1, self.move_refused(new, refusals[-1]) + '\n'))
        self.assertEqual(npmrc.read_text(), 'min-release-age=3\n')
        shutil.rmtree(old)
        self.assertEqual(self.sync_main(), (0, ''))
        self.assertIn('use the local reader', (new / 'skills/one/SKILL.md').read_text())
        self.assertIsNone(json.loads(self.receipt_of('plugin:').read_bytes())['pending'])

    def sync_main(self):
        import sync
        with patch('sync.load', return_value=self.setup), patch('sync.Device.current', return_value=self.device), \
                patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO) as stderr:
            code = sync.main(['--apply'])
        return code, ''.join(line for line in stderr.getvalue().splitlines(keepends=True)
                             if not line.startswith('Backups: '))

    def test_a_sync_names_the_move_windows_refused_and_exits_1(self):
        installed, npmrc = self.release_in_place()
        with self.refusing(moves=moving_aside(installed)) as refusals:
            self.assertEqual(self.sync_main(), (1, self.move_refused(installed, refusals[-1]) + '\n'))
        self.assertEqual(npmrc.read_text(), 'min-release-age=3\n')

    def test_a_sync_stopped_by_a_refused_move_that_cannot_wait_prints_the_refusal_alone(self):
        installed, npmrc = self.release_in_place()
        receipt = self.receipt_of('plugin:')
        with self.refusing(moves=moving_aside(installed), at_last_attempt=receipt.unlink) as refusals:
            self.assertEqual(self.sync_main(), (3, f'Sync stopped: {refusals[-1]}\n'))

    def test_a_sync_stopped_by_a_refused_receipt_save_prints_the_refusal_alone(self):
        installed, npmrc = self.release_in_place()
        receipt = self.receipt_of('plugin:')
        with self.refusing(replaces=lambda source, target: target == receipt) as refusals:
            self.assertEqual(self.sync_main(), (3, f'Sync stopped: {refusals[-1]}\n'))

    @unittest.skipUnless(portable.WINDOWS, 'Windows refuses to move a folder that holds a file a program has open')
    def test_a_tree_holding_a_file_a_program_has_open_waits_and_the_rest_of_the_release_applies(self):
        installed, npmrc = self.release_in_place()
        with (installed / 'ordinary.txt').open('rb'):
            result = self.run_apply()
        self.assertEqual(result.exit_code, 1)
        [held] = result.held
        refusal = held.refusal
        self.assertEqual((type(held), held.path, type(refusal), refusal.winerror, Path(refusal.filename),
                          Path(refusal.filename2).name, str(refusal)),
                         (accepted_installations.HeldMove, installed, portable.StillHeld, 5, installed, 'displaced',
                          str(refusal.__cause__)))
        self.assertIn('use the local reader', (installed / 'skills/one/SKILL.md').read_text())
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A second loose skill.\n')
        self.assertEqual(npmrc.read_text(), 'min-release-age=3\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (installed / 'skills/one/SKILL.md').read_text())

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
        self.assertNotIn(COULD_NOT_SWITCH, printed)
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
        self.assertNotIn(COULD_NOT_SWITCH, self.stopped_by(RuntimeError('codex plugin add failed: exit 1')))
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
        self.assertNotIn(COULD_NOT_SWITCH, printed)

    def test_a_native_install_that_fails_leaving_config_toml_unreadable_stops_with_its_own_error(self):
        printed = self.left_by_a_failed_install(b'[plugins."kit@personal"]\nenabled = tr')
        self.assertRegex(printed, rf'(?m)^codex: {COULD_NOT_SWITCH} kit@personal off .*: TOMLDecodeError: .*line 2')

    def test_a_native_install_that_fails_where_the_switch_cannot_be_written_stops_with_its_own_error(self):
        printed = self.left_by_a_failed_install(b'[plugins]\n"kit@personal" = true\n')
        self.assertRegex(printed, rf'(?m)^codex: {COULD_NOT_SWITCH} kit@personal off .*: TypeError: .*item assignment')

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
            if args[:2] in (('plugin', 'install'), ('plugin', 'update')):
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
            version = builder.setup.lock['kit'].trees[Host.CLAUDE].version
            release = catalog / 'plugins/kit' / version
            shutil.copytree(codex.root, release)
            return {plugin: Built('kit', release, version, builder.roots.tree_digest(release))}

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
        self.assertNotIn(COULD_NOT_SWITCH, printed)
        self.assertIs(self.claude_switches()['kit@personal-collections'], False)

    def test_a_claude_code_install_that_exits_non_zero_after_switching_a_kept_current_plugin_on_leaves_it_switched_off(self):
        self.keep_kit_current_on_claude()
        self.stopped_by(RuntimeError('claude plugin install failed: exit 1'), self.switch_claude_on)
        self.assertIs(self.claude_switches()['kit@personal-collections'], False)

    def release_kit_2_on_claude(self):
        """After a first sync installed kit 1.0.0 on Claude Code, kit 2.0.0 as the next release describes it."""
        self.assertEqual(self.run_apply().exit_code, 0)
        (self.source / '.codex-plugin/plugin.json').write_text('{"name":"kit","version":"2.0.0"}')
        digest = self.roots.tree_digest(self.source)
        kit = replace(self.setup.packages['kit'], source=LocalSource('owned/kit', digest))
        lock = replace(self.setup.lock['kit'], intent=intent(kit, {}), trees={Host.CLAUDE: LockedTree('2.0.0', digest)})
        self.setup = replace(self.setup, packages={'kit': kit}, lock={'kit': lock})
        self.calls.clear()

    def test_a_claude_code_update_of_a_kept_current_plugin_leaves_it_switched_off(self):
        self.keep_kit_current_on_claude()
        self.release_kit_2_on_claude()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn(('plugin', 'update'), [args[:2] for _, args in self.calls])
        self.assertIs(self.claude_switches()['kit@personal-collections'], False)

    def test_a_claude_code_update_that_fails_after_switching_a_kept_current_plugin_on_leaves_it_switched_off(self):
        self.keep_kit_current_on_claude()
        self.release_kit_2_on_claude()
        self.stopped_by(RuntimeError('claude plugin update failed: exit 1'), self.switch_claude_on)
        commands = [args[:2] for _, args in self.calls]
        self.assertIn(('plugin', 'update'), commands)
        self.assertNotIn(('plugin', 'install'), commands)
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
        self.assertRegex(printed,
                         rf'(?m)^claude: {COULD_NOT_SWITCH} kit@personal-collections off .*: JSONDecodeError: .*line 1')
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
        self.assertEqual([(type(held), held.path, held.holders, held.line) for held in result.held],
                         [(accepted_installations.HeldCache, destination, (os.getpid(),),
                           f'Claude lease: {destination}, held by running Claude processes {os.getpid()}, stays as '
                           'it is; the first pull after they exit replaces it')])
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

    def test_a_skill_text_change_whose_cache_windows_refuses_to_move_while_a_session_runs_waits_at_each_sync(self):
        destination = self._installed_claude()
        lease = own_lease(destination / '.in_use')
        marker = lease.read_text()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        for _ in range(2):
            with self.refusing(moves=moving_aside(destination)):
                result = self.run_apply()
            self.assertEqual((result.exit_code, [(type(held), held.path) for held in result.held]),
                             (1, [(accepted_installations.HeldMove, destination)]))
            self.assertEqual(self.held(), [])
            self.assertEqual(lease.read_text(), marker)
            self.assertIn('use the local reader', (destination / 'skills/one/SKILL.md').read_text())
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (destination / 'skills/one/SKILL.md').read_text())
        self.assertEqual(lease.read_text(), marker)
        self.assertEqual(self.calls, [])

    def test_a_claude_version_bump_whose_new_tree_windows_refuses_to_move_into_place_stops_the_sync(self):
        old = self._installed_claude()
        (self.source / 'ordinary.txt').write_text('release 2.0.0\n')
        digest = self.roots.tree_digest(self.source)
        package = replace(self.setup.packages['kit'], source=LocalSource('owned/kit', digest))
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}), trees={Host.CLAUDE: LockedTree('2.0.0', digest)})
        self.setup = replace(self.setup, packages={'kit': package}, lock={'kit': lock})
        npmrc = self.release_a_setting_written_after_the_plugins()
        new = old.parent / '2.0.0'
        with self.refusing(moves=moving_into_place(new)) as refusals, self.assertRaises(portable.StillHeld) as stopped:
            self.run_apply()
        self.assertIs(stopped.exception.__cause__, refusals[-1])
        self.assertEqual([args[:2] for _, args in self.calls][-1], ('plugin', 'update'))
        self.assertEqual((new / 'ordinary.txt').read_text(), 'release 2.0.0\n')
        self.assertFalse(npmrc.exists())

    def stopped_between_the_moves(self):
        rename = Path.rename

        def crash(path, target):
            if path.name == 'staged':
                raise RuntimeError('between the moves')
            return rename(path, target)

        with patch.object(Path, 'rename', crash), self.assertRaisesRegex(RuntimeError, 'between the moves'):
            self.run_apply()

    def test_a_change_to_skill_text_alone_that_a_later_run_finishes_keeps_its_leases(self):
        destination = self._installed_claude()
        lease = own_lease(destination / '.in_use')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        self.stopped_between_the_moves()
        self.assertFalse(destination.exists())
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (destination / 'skills/one/SKILL.md').read_text())
        self.assertTrue(lease.exists())
        self.revise_release('revised release bytes\n')
        self.assert_held(destination)

    def test_a_change_to_skill_text_alone_that_stopped_after_its_moves_keeps_its_leases(self):
        destination = self._installed_claude()
        lease = own_lease(destination / '.in_use')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        rename = Path.rename

        def crash(path, target):
            result = rename(path, target)
            if path.name == 'staged':
                raise RuntimeError('after the moves')
            return result

        with patch.object(Path, 'rename', crash), self.assertRaisesRegex(RuntimeError, 'after the moves'):
            self.run_apply()
        self.assertIn('use the reviewed replacement', (destination / 'skills/one/SKILL.md').read_text())
        self.assertFalse(lease.exists())
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertTrue(lease.exists())
        self.revise_release('revised release bytes\n')
        self.assert_held(destination)

    def test_a_new_tree_that_cannot_be_moved_into_place_by_the_run_that_finishes_a_swap_puts_the_old_tree_back(self):
        destination = self._installed_claude()
        self.revise_release('revised release bytes\n')
        self.stopped_between_the_moves()
        rename = os.rename

        def refuse(source, target, *args, **kwargs):
            if Path(source).name == 'staged':
                raise PermissionError(13, 'Access is denied', str(target))
            return rename(source, target, *args, **kwargs)

        with patch.object(portable, 'RETRIES_REFUSALS', False), patch('os.rename', refuse), \
                self.assertRaises(PermissionError):
            self.run_apply()
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'revised release bytes\n')

    def test_a_run_that_resumes_a_swap_and_cannot_save_its_receipt_puts_the_old_tree_back(self):
        from accepted_installations import AcceptedInstallations
        self.assertEqual(self.run_apply().exit_code, 0)
        installed = self.installed()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        self.stopped_between_the_moves()
        self.assertFalse(installed.exists())
        save, failed = AcceptedInstallations._save, []

        def fail_once(owner, artifact, accepted, pending=None):
            if pending is not None and not failed:
                failed.append(artifact.identity)
                raise OSError(28, 'No space left on device')
            return save(owner, artifact, accepted, pending)

        with patch.object(AcceptedInstallations, '_save', fail_once), self.assertRaises(OSError):
            self.run_apply()
        self.assertIn('use the local reader', (installed / 'skills/one/SKILL.md').read_text())
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (installed / 'skills/one/SKILL.md').read_text())

    def test_an_old_tree_edited_after_it_was_moved_aside_is_not_put_back(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        installed = self.installed()
        rename = os.rename

        def refuse(source, target, *args, **kwargs):
            if Path(source).name == 'staged':
                (Path(source).parent / 'displaced/ordinary.txt').write_text('edited after the move aside\n')
                raise PermissionError(13, 'Access is denied', str(target))
            return rename(source, target, *args, **kwargs)

        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        with patch('os.rename', refuse), patch('sys.stderr', new_callable=io.StringIO) as stderr, \
                self.assertRaises(PermissionError):
            self.run_apply()
        self.assertFalse(installed.exists())
        displaced = Path(json.loads(self.receipt_of('plugin:').read_bytes())['pending']['displaced'])
        self.assertEqual((displaced / 'ordinary.txt').read_text(), 'edited after the move aside\n')
        self.assertEqual(stderr.getvalue(),
                         f'Old tree left aside: {displaced}, from {installed}: it changed after it was moved aside\n')

    def test_an_old_tree_that_cannot_be_put_back_is_named_and_the_next_run_finishes_the_swap(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        installed = self.installed()
        rename = os.rename

        def refuse(source, target, *args, **kwargs):
            if Path(source).name in ('staged', 'displaced'):
                raise PermissionError(13, 'Access is denied', str(target))
            return rename(source, target, *args, **kwargs)

        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        with patch('os.rename', refuse), patch('sys.stderr', new_callable=io.StringIO) as stderr, \
                self.assertRaises(PermissionError):
            self.run_apply()
        self.assertFalse(installed.exists())
        displaced = Path(json.loads(self.receipt_of('plugin:').read_bytes())['pending']['displaced'])
        self.assertEqual(stderr.getvalue(), f'Old tree left aside: {displaced}, from {installed}: '
                                            f'[Errno 13] Access is denied: {str(installed)!r}\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (installed / 'skills/one/SKILL.md').read_text())

    def session_started_between_the_moves(self):
        destination = self._installed_claude()
        self.revise_release('revised release bytes\n')
        self.stopped_between_the_moves()
        destination.mkdir()
        return destination, own_lease(destination / '.in_use')

    def test_a_marker_a_session_wrote_between_the_moves_goes_into_the_tree_a_later_run_installs(self):
        destination, lease = self.session_started_between_the_moves()
        self.assertEqual(self.held(), [])
        self.assertEqual(plan(self.setup, self.device).blocked, ())
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'revised release bytes\n')
        self.assertTrue(lease.exists())
        self.revise_release('revised again\n')
        self.assert_held(destination)

    def test_a_marker_a_session_wrote_again_between_the_moves_replaces_its_marker_in_the_old_tree(self):
        destination = self._installed_claude()
        own_lease(destination / '.in_use', start='1')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        self.stopped_between_the_moves()
        destination.mkdir()
        lease = own_lease(destination / '.in_use')
        marker = lease.read_text()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (destination / 'skills/one/SKILL.md').read_text())
        self.assertEqual(lease.read_text(), marker)

    def test_a_marker_still_being_written_between_the_moves_stops_the_run_until_it_is_written(self):
        destination, lease = self.session_started_between_the_moves()
        writing = destination / f'.in_use/{lease.name}.tmp.0123abcd'
        writing.write_text(lease.read_text())
        with self.assertRaisesRegex(ConcurrentEdit, f'^Tree changed before replacement: {re.escape(str(destination))}$'):
            self.run_apply()
        self.assertTrue(writing.exists())
        backup = Path(json.loads(self._pending_claude_receipt().read_bytes())['pending']['displaced'])
        self.assertEqual((backup / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        writing.replace(lease)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'revised release bytes\n')
        self.assertTrue(lease.exists())

    def test_a_folder_between_the_moves_that_holds_more_than_markers_blocks(self):
        destination, _ = self.session_started_between_the_moves()
        for name, text in (('theirs.txt', 'made between the moves\n'), ('.orphaned_at', '1794063570885')):
            with self.subTest(name):
                (destination / name).write_text(text)
                current = plan(self.setup, self.device)
                self.assertEqual([d.detail for d in current.blocked],
                                 ['plugin:claude:kit@personal-collections: Unknown partial installation preserved: '
                                  'plugin:claude:kit@personal-collections'])
                (destination / name).unlink()

    def test_a_link_that_appears_where_the_marker_folder_was_keeps_the_markers_it_leads_to(self):
        from accepted_installations import AcceptedInstallations
        destination = self._installed_claude()
        self.revise_release('revised release bytes\n')
        self.stopped_between_the_moves()
        other = self.root / 'other-cache'
        other.mkdir()
        marker = own_lease(other / '.in_use')
        tree = AcceptedInstallations._tree

        def linked_first(owner, prepared, pending, accepted, actual):
            destination.symlink_to(other, target_is_directory=True)
            return tree(owner, prepared, pending, accepted, actual)

        with patch.object(AcceptedInstallations, '_tree', linked_first), \
                patch('sys.stderr', new_callable=io.StringIO) as stderr, self.assertRaises(ConcurrentEdit):
            self.run_apply()
        displaced = Path(json.loads(self._pending_claude_receipt().read_bytes())['pending']['displaced'])
        self.assertTrue(marker.exists())
        self.assertFalse((displaced / '.in_use' / marker.name).exists())
        self.assertEqual(stderr.getvalue(), f'Old tree left aside: {displaced}, from {destination}: its place is taken\n')

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

    def pending_of(self, prefix):
        return json.loads(self.receipt_of(prefix).read_bytes())['pending']

    def backup_of(self, prefix):
        return Path(json.loads(self.receipt_of(prefix).read_bytes())['accepted']['backup'])

    def release_version(self, version, text):
        (self.source / '.codex-plugin/plugin.json').write_text(json.dumps({'name': 'kit', 'version': version}))
        (self.source / 'ordinary.txt').write_text(text)
        digest = self.roots.tree_digest(self.source)
        package = replace(self.setup.packages['kit'], source=LocalSource('owned/kit', digest))
        host, = self.setup.lock['kit'].trees
        lock = replace(self.setup.lock['kit'], intent=intent(package, {}), trees={host: LockedTree(version, digest)})
        self.setup = replace(self.setup, packages={'kit': package}, lock={**self.setup.lock, 'kit': lock})

    def stopped_after_the_moves(self):
        rename = Path.rename

        def crash(path, target):
            result = rename(path, target)
            if path.name == 'staged':
                raise RuntimeError('after the moves')
            return result

        with patch.object(Path, 'rename', crash), self.assertRaisesRegex(RuntimeError, 'after the moves'):
            self.run_apply()

    def refused_into_place(self):
        rename = os.rename

        def refuse(source, target, *args, **kwargs):
            if Path(source).name == 'staged':
                raise PermissionError(13, 'Access is denied', str(target))
            return rename(source, target, *args, **kwargs)

        with patch('os.rename', refuse), patch('portable.time.sleep'), self.assertRaises(PermissionError):
            self.run_apply()

    def test_a_newer_release_takes_over_a_claude_cache_whose_old_tree_a_stopped_replacement_moved_aside(self):
        destination = self._installed_claude()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the first replacement'),))
        self.stopped_between_the_moves()
        stale = self.pending_of('plugin:claude')
        self.assertFalse(destination.exists())
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the second replacement'),))
        self.assertEqual(plan(self.setup, self.device).blocked, ())
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the second replacement', (destination / 'skills/one/SKILL.md').read_text())
        self.assertIn('use the local reader', (self.backup_of('plugin:claude') / 'skills/one/SKILL.md').read_text())
        self.assertFalse(Path(stale['staged']).parent.exists())
        self.assertEqual(self.unnamed_work_folders(), [])
        self.assertEqual(self.calls, [])

    def test_a_newer_release_puts_back_the_loose_skill_a_stopped_replacement_moved_aside(self):
        old_backup = self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_between_the_moves()
        stale = self.pending_of('codex:LOOSE_SKILL')
        self.revise_loose('A fourth loose skill.\n')
        self.assertEqual(plan(self.setup, self.device).blocked, ())
        self.assertEqual(self.apply_reporting(), '')
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A fourth loose skill.\n')
        self.assertEqual((self.backup_of('codex:LOOSE_SKILL') / 'SKILL.md').read_text(), 'A second loose skill.\n')
        self.assertFalse(Path(stale['staged']).parent.exists())
        self.assertFalse(old_backup.exists())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_newer_release_takes_over_a_codex_aligned_claude_cache_a_stopped_replacement_moved_aside(self):
        destination = self._interrupted_claude_install(aligned=True)
        self.revise_release('newest aligned release\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'newest aligned release\n')
        self.assertEqual((self.backup_of('plugin:claude') / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_newer_release_puts_back_the_old_tree_with_the_marker_of_a_session_that_started_while_it_was_aside(self):
        destination, lease = self.session_started_between_the_moves()
        self.revise_release('newest release\n')
        self.assertEqual(plan(self.setup, self.device).blocked, ())
        self.assert_held(destination)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertTrue(lease.exists())
        lease.unlink()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'newest release\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_newer_release_puts_back_the_old_tree_with_a_session_marker_that_replaces_a_finished_one_of_its_name(self):
        destination = self._installed_claude()
        own_lease(destination / '.in_use', start='1')
        self.revise_release('revised release bytes\n')
        with patch('accepted_installations._lease_held', return_value=False):
            self.stopped_between_the_moves()
        destination.mkdir()
        lease = own_lease(destination / '.in_use')
        live = lease.read_text()
        self.revise_release('newest release\n')
        self.assert_held(destination)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertEqual(lease.read_text(), live)

    def test_a_newer_version_takes_over_a_version_update_whose_native_tree_a_stopped_replacement_moved_aside(self):
        old = self._installed_claude()
        self.release_version('2.0.0', 'second release bytes\n')
        self.stopped_between_the_moves()
        self.assertFalse(old.with_name('2.0.0').exists())
        self.release_version('3.0.0', 'third release bytes\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((old.with_name('3.0.0') / 'ordinary.txt').read_text(), 'third release bytes\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_newer_version_takes_over_a_version_update_stopped_before_its_swap_over_a_release_claude_code_cached(self):
        old = self._installed_claude()
        self.release_version('2.0.0', 'second release bytes\n')
        shutil.copytree(self.source, old.with_name('2.0.0'))
        copy = accepted_installations.copy_tree

        def stop(source, target, *args):
            if target.name == 'staged':
                raise RuntimeError('stopped while staging')
            return copy(source, target, *args)

        with patch('accepted_installations.copy_tree', stop), self.assertRaisesRegex(RuntimeError, 'stopped while staging'):
            self.run_apply()
        pending = self.pending_of('plugin:claude')
        self.assertEqual(pending['destination_before'], pending['pristine'])
        self.release_version('3.0.0', 'third release bytes\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((old.with_name('3.0.0') / 'ordinary.txt').read_text(), 'third release bytes\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_newer_release_of_a_version_takes_over_its_update_stopped_before_installing_over_a_release_claude_code_cached(self):
        from sync_hosts import Apply
        old = self._installed_claude()
        self.release_version('2.0.0', 'second release bytes\n')
        cached = old.with_name('2.0.0')
        shutil.copytree(self.source, cached)
        with patch.object(Apply, '_install', side_effect=RuntimeError('native install stopped')), \
                self.assertRaisesRegex(RuntimeError, 'native install stopped'):
            self.run_apply()
        self.assertEqual(self.pending_of('plugin:claude')['target']['path'], str(cached))
        self.release_version('2.0.0', 'newer bytes of the same version\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((cached / 'ordinary.txt').read_text(), 'newer bytes of the same version\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def first_install_that_lost_its_old_tree_aside(self):
        destination = self._interrupted_claude_install()
        pending = self.pending_of('plugin:claude')
        shutil.rmtree(pending['displaced'])
        return destination, Path(pending['staged'])

    def test_a_first_install_that_lost_its_tree_aside_is_finished_from_its_stage(self):
        destination, _ = self.first_install_that_lost_its_old_tree_aside()
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), (self.source / 'ordinary.txt').read_text())

    def test_a_first_install_that_lost_its_tree_aside_and_whose_stage_was_edited_stays_blocked(self):
        destination, staged = self.first_install_that_lost_its_old_tree_aside()
        (staged / 'extra.txt').write_text('edited in the stage\n')
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 3)
        self.assertIn('cache not on disk', ' '.join(d.detail for d in current.blocked))
        self.assertEqual((staged / 'extra.txt').read_text(), 'edited in the stage\n')

    def test_a_newer_release_takes_over_a_first_install_that_lost_its_tree_aside_and_keeps_its_edited_stage(self):
        destination, staged = self.first_install_that_lost_its_old_tree_aside()
        (staged / 'extra.txt').write_text('edited in the stage\n')
        self.revise_release('revised release bytes\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        printed = self.apply_reporting()
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'revised release bytes\n')
        self.assertIn(f'Work folder left in place: {staged.parent}: ', printed)
        self.assertEqual((staged / 'extra.txt').read_text(), 'edited in the stage\n')

    def test_a_newer_release_whose_old_tree_cannot_go_back_names_where_it_stays(self):
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_between_the_moves()
        stale = self.pending_of('codex:LOOSE_SKILL')
        self.revise_loose('A fourth loose skill.\n')
        put_back = accepted_installations.AcceptedInstallations._put_back

        def taken(owner, artifact, pending):
            Path(pending['target']['path']).mkdir()
            return put_back(owner, artifact, pending)

        with patch.object(accepted_installations.AcceptedInstallations, '_put_back', taken), self.assertRaisesRegex(
                ConcurrentEdit, f'^Old tree left aside: {re.escape(stale["displaced"])}, from '
                                f'{re.escape(stale["target"]["path"])}: its place is taken$'):
            self.run_apply()
        self.assertEqual((Path(stale['displaced']) / 'SKILL.md').read_text(), 'A second loose skill.\n')

    def test_a_newer_release_moves_an_old_tree_back_once_windows_lets_go_of_it(self):
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_between_the_moves()
        loose = self.roots.codex_home / 'skills/loose'
        self.revise_loose('A fourth loose skill.\n')
        held = hold_built_trees_once(self)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn(loose, held)
        self.assertEqual((loose / 'SKILL.md').read_text(), 'A fourth loose skill.\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_newer_release_keeps_an_old_tree_aside_that_changed_while_windows_held_it(self):
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_between_the_moves()
        stale = self.pending_of('codex:LOOSE_SKILL')
        displaced, loose = Path(stale['displaced']), self.roots.codex_home / 'skills/loose'
        self.revise_loose('A fourth loose skill.\n')

        def edit(target):
            if target == loose:
                (displaced / 'SKILL.md').write_text('edited during the wait\n')

        held = hold_built_trees_once(self, meanwhile=edit)
        with self.assertRaisesRegex(ConcurrentEdit, f'^Old tree left aside: {re.escape(str(displaced))}, from '
                                                    f'{re.escape(str(loose))}: it changed after it was moved aside$'):
            self.run_apply()
        self.assertEqual(held, [loose])
        self.assertFalse(loose.exists())
        self.assertEqual((displaced / 'SKILL.md').read_text(), 'edited during the wait\n')

    def test_a_newer_release_keeps_an_old_tree_aside_whose_place_was_taken_while_windows_held_it(self):
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_between_the_moves()
        stale = self.pending_of('codex:LOOSE_SKILL')
        displaced, loose = Path(stale['displaced']), self.roots.codex_home / 'skills/loose'
        self.revise_loose('A fourth loose skill.\n')
        held = hold_built_trees_once(self, meanwhile=lambda target: target == loose and target.mkdir())
        with self.assertRaisesRegex(ConcurrentEdit, f'^Old tree left aside: {re.escape(str(displaced))}, from '
                                                    f'{re.escape(str(loose))}: its place is taken$'):
            self.run_apply()
        self.assertEqual(held, [loose])
        self.assertEqual(list(loose.iterdir()), [])
        self.assertEqual((displaced / 'SKILL.md').read_text(), 'A second loose skill.\n')

    def test_a_newer_release_whose_old_tree_is_held_names_where_it_stays_and_the_next_run_finishes(self):
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_between_the_moves()
        stale = self.pending_of('codex:LOOSE_SKILL')
        displaced = Path(stale['displaced'])
        self.revise_loose('A fourth loose skill.\n')
        rename = os.rename

        def held(source, target, *args, **kwargs):
            if Path(source) == displaced:
                raise PermissionError(13, 'Access is denied', str(target))
            return rename(source, target, *args, **kwargs)

        with patch('os.rename', held), patch('portable.time.sleep'), self.assertRaisesRegex(
                ConcurrentEdit, f'^Old tree left aside: {re.escape(str(displaced))}, from '
                                f'{re.escape(stale["target"]["path"])}: .*Access is denied'):
            self.run_apply()
        self.assertEqual((displaced / 'SKILL.md').read_text(), 'A second loose skill.\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A fourth loose skill.\n')

    def test_a_tree_moved_aside_that_cannot_be_read_is_not_named_as_the_backup(self):
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_after_the_moves()
        displaced = Path(self.pending_of('codex:LOOSE_SKILL')['displaced'])
        read = accepted_installations.AcceptedInstallations._read

        def refused(owner, artifact, path=None, **kwargs):
            if path == displaced:
                raise PermissionError(13, 'Access is denied', str(path))
            return read(owner, artifact, path, **kwargs)

        with patch.object(accepted_installations.AcceptedInstallations, '_read', refused):
            printed = self.apply_reporting()
        self.assertNotIn('backup', json.loads(self.receipt_of('codex:LOOSE_SKILL').read_bytes())['accepted'])
        self.assertEqual((displaced / 'SKILL.md').read_text(), 'A second loose skill.\n')
        self.assertEqual(printed, f'Work folder left in place: {displaced.parent}: the tree moved aside changed: '
                                  f'[Errno 13] Access is denied: {str(displaced)!r}\n')

    def test_a_newer_version_refuses_a_copy_of_a_stopped_replacement_s_native_output_where_it_goes(self):
        self.refused_into_place()
        native_output = self.installed()
        self.release_version('2.0.0', 'second release bytes\n')
        copy = native_output.with_name('2.0.0')
        shutil.copytree(native_output, copy)
        self.assertEqual([d.detail for d in plan(self.setup, self.device).blocked],
                         [f'plugin:codex:kit@personal: Unverified destination preserved: {copy}'])
        with self.assertRaisesRegex(RuntimeError, 'blocked'):
            self.run_apply()
        self.assertEqual((copy / 'ordinary.txt').read_text(), (native_output / 'ordinary.txt').read_text())

    def test_a_third_release_of_a_version_takes_over_a_second_stopped_over_a_release_claude_code_cached(self):
        from sync_hosts import Apply
        old = self._installed_claude()
        self.release_version('2.0.0', 'second release bytes\n')
        cached = old.with_name('2.0.0')
        shutil.copytree(self.source, cached)
        for text in ('second release bytes\n', 'newer bytes of the same version\n'):
            self.release_version('2.0.0', text)
            with patch.object(Apply, '_install', side_effect=RuntimeError('native install stopped')), \
                    self.assertRaisesRegex(RuntimeError, 'native install stopped'):
                self.run_apply()
        self.release_version('2.0.0', 'third bytes of the same version\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((cached / 'ordinary.txt').read_text(), 'third bytes of the same version\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_newer_version_takes_over_a_take_over_that_stopped_before_its_native_install(self):
        from sync_hosts import Apply
        self.refused_into_place()
        first = self.installed()
        self.release_version('2.0.0', 'second release bytes\n')
        with patch.object(Apply, '_install', side_effect=RuntimeError('stopped before the native install')), \
                self.assertRaisesRegex(RuntimeError, 'stopped before the native install'):
            self.run_apply()
        self.assertEqual(self.pending_of('plugin:codex')['before_path'], str(first))
        self.release_version('3.0.0', 'third release bytes\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(self.installed().name, '3.0.0')
        self.assertEqual((self.installed() / 'ordinary.txt').read_text(), 'third release bytes\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def empty_folder_is_finished_like_a_missing_cache(self, **kwargs):
        destination = self._interrupted_claude_install(**kwargs)
        destination.mkdir()
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), (self.source / 'ordinary.txt').read_text())

    def test_a_pending_claude_cache_whose_folder_is_empty_is_finished_like_a_missing_one(self):
        self.empty_folder_is_finished_like_a_missing_cache()

    def test_a_pending_codex_aligned_cache_whose_folder_is_empty_is_finished_like_a_missing_one(self):
        self.empty_folder_is_finished_like_a_missing_cache(aligned=True)

    def empty_folder_is_taken_over_by_a_newer_release(self, **kwargs):
        destination = self._interrupted_claude_install(**kwargs)
        destination.mkdir()
        self.revise_release('newest release\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'newest release\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_newer_release_takes_over_a_pending_claude_cache_whose_folder_is_empty(self):
        self.empty_folder_is_taken_over_by_a_newer_release()

    def test_a_newer_release_takes_over_a_pending_codex_aligned_cache_whose_folder_is_empty(self):
        self.empty_folder_is_taken_over_by_a_newer_release(aligned=True)

    def test_a_newer_release_accepts_a_loose_skill_replacement_that_stopped_after_its_moves(self):
        old_backup = self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_after_the_moves()
        stale = self.pending_of('codex:LOOSE_SKILL')
        self.revise_loose('A fourth loose skill.\n')
        self.assertEqual(plan(self.setup, self.device).blocked, ())
        self.assertEqual(self.apply_reporting(), '')
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A fourth loose skill.\n')
        self.assertEqual((self.backup_of('codex:LOOSE_SKILL') / 'SKILL.md').read_text(), 'A third loose skill.\n')
        self.assertFalse(Path(stale['displaced']).parent.exists())
        self.assertFalse(old_backup.exists())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_newer_release_finishes_a_replacement_that_stopped_while_removing_the_backup_before_it(self):
        old_backup = self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        rmtree = shutil.rmtree

        def stop_part_way(path, *args, **kwargs):
            if Path(path) != old_backup.parent:
                return rmtree(path, *args, **kwargs)
            (old_backup / 'SKILL.md').unlink()
            raise RuntimeError('stopped during the removal')

        with patch('shutil.rmtree', stop_part_way), self.assertRaisesRegex(RuntimeError, 'stopped during the removal'):
            self.run_apply()
        self.revise_loose('A fourth loose skill.\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A fourth loose skill.\n')
        self.assertEqual((self.backup_of('codex:LOOSE_SKILL') / 'SKILL.md').read_text(), 'A third loose skill.\n')
        self.assertFalse(old_backup.parent.exists())
        self.assertEqual(self.unnamed_work_folders(), [])

    def swapped_with_its_backup_changed(self, change, reason):
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_after_the_moves()
        displaced = Path(self.pending_of('codex:LOOSE_SKILL')['displaced'])
        change(displaced)
        self.revise_loose('A fourth loose skill.\n')
        printed = self.apply_reporting()
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A fourth loose skill.\n')
        self.assertEqual(printed, f'Work folder left in place: {displaced.parent}: the tree moved aside changed: '
                                  f'{reason(displaced)}\n')
        self.assertNotEqual(self.backup_of('codex:LOOSE_SKILL'), displaced)
        self.assertEqual(self.unnamed_work_folders(), [str(displaced.parent.relative_to(self.roots.home))])
        return displaced

    def test_a_newer_release_keeps_a_tree_moved_aside_that_lost_a_file_and_names_it(self):
        displaced = self.swapped_with_its_backup_changed(lambda tree: (tree / 'SKILL.md').unlink(),
                                                         lambda tree: 'its tree is not the one its receipt holds')
        self.assertEqual(list(displaced.iterdir()), [])

    def test_a_newer_release_keeps_a_tree_moved_aside_that_gained_a_link_and_names_it(self):
        outside = self.root / 'outside.txt'
        outside.write_text('mine')
        displaced = self.swapped_with_its_backup_changed(lambda tree: (tree / 'link.txt').symlink_to(outside),
                                                         lambda tree: f'Refusing a linked tree entry: {tree / "link.txt"}')
        self.assertTrue((displaced / 'link.txt').is_symlink())
        self.assertEqual(outside.read_text(), 'mine')

    def native_first_install_put_back(self):
        destination = self._interrupted_claude_install()
        self.refused_into_place()
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertNotIn('use the local reader', (destination / 'skills/one/SKILL.md').read_text())
        return destination

    def test_a_newer_release_takes_over_a_first_install_whose_native_output_was_put_back(self):
        destination = self.native_first_install_put_back()
        self.revise_release('revised release bytes\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'revised release bytes\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_newer_version_takes_over_a_first_install_whose_native_output_was_put_back(self):
        destination = self.native_first_install_put_back()
        self.release_version('2.0.0', 'second release bytes\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination.with_name('2.0.0') / 'ordinary.txt').read_text(), 'second release bytes\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_newer_version_is_refused_where_its_folder_holds_content_no_receipt_records(self):
        destination = self.native_first_install_put_back()
        self.release_version('2.0.0', 'second release bytes\n')
        occupied = destination.with_name('2.0.0')
        occupied.mkdir()
        (occupied / 'local.txt').write_text('not owned\n')
        self.assertEqual([d.detail for d in plan(self.setup, self.device).blocked],
                         [f'plugin:claude:kit@personal-collections: Unverified destination preserved: {occupied}'])
        with self.assertRaisesRegex(RuntimeError, 'blocked'):
            self.run_apply()
        self.assertEqual([path.name for path in occupied.iterdir()], ['local.txt'])

    def codex_version_update_put_back_after_codex_removed_the_old_version(self):
        from sync_hosts import Apply
        self.assertEqual(self.run_apply().exit_code, 0)
        old = self.installed()
        self.release_version('2.0.0', 'second release bytes\n')
        native = Apply.command

        def command(operation, host, *args):
            result = native(operation, host, *args)
            if args[:2] == ('plugin', 'add'):
                shutil.rmtree(old)   # as codex plugin add does with the version folder it replaces
            return result

        with patch.object(Apply, 'command', command):
            self.refused_into_place()
        self.assertFalse(old.exists())
        self.assertEqual((self.installed() / 'ordinary.txt').read_text(), 'second release bytes\n')
        return self.installed()

    def test_a_codex_version_update_put_back_after_codex_removed_the_old_version_finishes(self):
        installed = self.codex_version_update_put_back_after_codex_removed_the_old_version()
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the local reader', (installed / 'skills/one/SKILL.md').read_text())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_newer_release_takes_over_a_codex_version_update_put_back_after_codex_removed_the_old_version(self):
        installed = self.codex_version_update_put_back_after_codex_removed_the_old_version()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use a newer reader'),))
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use a newer reader', (installed / 'skills/one/SKILL.md').read_text())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_first_install_over_a_copy_of_the_release_that_no_receipt_records_is_refused(self):
        destination = self.roots.codex_home / 'plugins/cache/personal/kit/1.0.0'
        shutil.copytree(self.source, destination)
        with self.assertRaisesRegex(ConcurrentEdit, '^Unverified existing content preserved: plugin:codex:kit@personal;'):
            self.run_apply()
        self.assertNotIn('use the local reader', (destination / 'skills/one/SKILL.md').read_text())

    def test_a_newer_release_refuses_a_copy_of_the_native_output_away_from_its_stopped_replacement(self):
        self.refused_into_place()
        native_output = self.installed()
        copy = native_output.with_name('0.9.0')
        shutil.copytree(native_output, copy)
        self.host.installed['kit@personal'] = ('0.9.0', copy)
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use a newer reader'),))
        self.assertEqual([d.detail for d in plan(self.setup, self.device).blocked],
                         ['plugin:codex:kit@personal: Unverified existing content preserved: plugin:codex:kit@personal; '
                          'an independently pinned baseline is required'])
        with self.assertRaisesRegex(RuntimeError, 'blocked'):
            self.run_apply()
        self.assertNotIn('use the local reader', (copy / 'skills/one/SKILL.md').read_text())

    def managed_keys_stopped(self, *, written):
        from sync_files import Files
        source = self.public / 'config/mise/config.toml'
        source.parent.mkdir(parents=True)
        source.write_text('[tools]\nnode = "24"\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        live = self.roots.home / '.config/mise/config.toml'
        live.write_bytes(live.read_bytes() + b'\n[settings]\nlocal = true\n')
        source.write_text('[tools]\nnode = "25"\n')
        write = Files.write

        def stop(files, path, data, *, expected):
            if path == live and not written:
                raise RuntimeError('stopped at the write')
            result = write(files, path, data, expected=expected)
            if path == live:
                raise RuntimeError('stopped at the write')
            return result

        with patch.object(Files, 'write', stop), self.assertRaisesRegex(RuntimeError, 'stopped at the write'):
            self.run_apply()
        return source, live

    def managed_keys_taken_over(self, release, *, written):
        source, live = self.managed_keys_stopped(written=written)
        source.write_text(release)
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)
        written = tomllib.loads(live.read_text())
        self.assertEqual(written['settings'], {'local': True})
        return written['tools']

    def test_a_release_that_adds_a_managed_key_takes_over_a_release_stopped_before_its_write(self):
        self.assertEqual(self.managed_keys_taken_over('[tools]\nnode = "26"\nuv = "1"\n', written=False),
                         {'node': '26', 'uv': '1'})

    def test_a_release_that_adds_a_managed_key_takes_over_a_release_stopped_after_its_write(self):
        self.assertEqual(self.managed_keys_taken_over('[tools]\nnode = "26"\nuv = "1"\n', written=True),
                         {'node': '26', 'uv': '1'})

    def test_a_release_that_drops_a_managed_key_takes_over_a_release_stopped_before_its_write(self):
        self.assertEqual(self.managed_keys_taken_over('[tools]\nuv = "1"\n', written=False), {'node': '24', 'uv': '1'})

    def test_a_release_that_drops_a_managed_key_takes_over_a_release_stopped_after_its_write(self):
        self.assertEqual(self.managed_keys_taken_over('[tools]\nuv = "1"\n', written=True), {'node': '25', 'uv': '1'})

    def test_a_local_edit_of_a_key_a_stopped_release_managed_blocks_a_release_that_drops_it(self):
        source, live = self.managed_keys_stopped(written=True)
        source.write_text('[tools]\nuv = "1"\n')
        live.write_bytes(live.read_bytes().replace(b'node = "25"', b'node = "local edit"'))
        self.assertEqual([d.detail for d in plan(self.setup, self.device).blocked],
                         ['configuration:mise-tools: Unknown partial installation preserved: configuration:mise-tools'])
        self.assertIn(b'node = "local edit"', live.read_bytes())

    def list_codex_as_its_cli_does(self):
        """codex-cli 0.169.0-alpha.6 on 13 November 2026 named a plugin's version after the last folder under its
        cache folder, so it named the engine's work folder once the version folder was moved aside."""
        folder = self.roots.codex_home / 'plugins/cache/personal/kit'

        def run(command, seconds=None, **_):
            if command[-1] == '--version':
                return 'codex-test\n'
            if command[-4:] == ['plugin', 'list', '--available', '--json']:
                version = sorted(path.name for path in folder.iterdir() if path.is_dir())[-1]
                return json.dumps({'installed': [{
                    'pluginId': 'kit@personal', 'name': 'kit', 'marketplaceName': 'personal', 'version': version,
                    'installed': True, 'enabled': True,
                    'source': {'source': 'local', 'path': str(self.roots.engine / 'build/kit')}}], 'available': []})
            raise AssertionError(command)

        patch('codex_host.run', run).start()
        self.device = Device(Platform.LINUX, self.roots, (CodexState(self.roots, 'codex'),), ())

    def codex_tree_aside(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        self.stopped_between_the_moves()
        self.assertFalse(self.installed().exists())
        self.list_codex_as_its_cli_does()
        self.calls.clear()
        return self.installed()

    def test_a_codex_plugin_whose_cli_lists_the_work_folder_finishes_its_replacement_without_reinstalling(self):
        installed = self.codex_tree_aside()
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (installed / 'skills/one/SKILL.md').read_text())
        self.assertEqual(self.calls, [])

    def test_a_newer_release_takes_over_a_codex_plugin_whose_cli_lists_the_work_folder_without_reinstalling(self):
        installed = self.codex_tree_aside()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use a newer reader'),))
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use a newer reader', (installed / 'skills/one/SKILL.md').read_text())
        self.assertIn('use the local reader', (self.backup_of('plugin:codex') / 'skills/one/SKILL.md').read_text())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_an_accepted_codex_plugin_whose_folder_went_missing_beside_its_backup_is_installed_again(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        self.assertEqual(self.run_apply().exit_code, 0)
        installed = self.installed()
        shutil.rmtree(installed)
        self.list_codex_as_its_cli_does()
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (installed / 'skills/one/SKILL.md').read_text())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_newer_release_takes_over_a_git_pinned_codex_plugin_without_reinstalling_it(self):
        from accepted_installations import AcceptedInstallations, image
        from host_observation import Observer
        from sync_files import Files
        commit = 'a' * 40
        repository = Repo('upstream', Visibility.PUBLIC, '', 'https://example.invalid/upstream.git', commit,
                          HostMarketplace('upstream', MarketplaceMode.GIT), None)
        package = replace(self.setup.packages['kit'], source=RepoSource('upstream', '.'),
                          codex=CodexPlacement((MarketplaceInstall('upstream', True),)))
        self.setup = replace(self.setup, packages={'kit': package}, repos={'upstream': repository}, loose={},
                             rules=None)
        self.device = Device(Platform.LINUX, self.roots, (CodexState(self.roots, 'codex'),), ())
        owner = AcceptedInstallations(self.setup, self.device, configurations=False)
        destination = self.roots.codex_home / 'plugins/cache/upstream/kit/1.0.0'
        installs = []

        def install(host, subject):
            installs.append(subject)
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(self.source, destination)

        def observed(version):
            seen = Observer(Host.CODEX, self.roots)
            if version:
                seen.add(Surface.PLUGIN, 'kit@upstream', PluginValue(True, Pinned(commit)),
                         detail=InstallDetail(version, destination.parent / version))
            return {Host.CODEX: seen.finish('git-pinned listing', {'kit@upstream'})}

        def applied(text, version):
            source = self.root / f'overlay-{len(owner._prepared)}'
            shutil.copytree(self.source, source)
            (source / 'skills/one/SKILL.md').write_text(text)
            artifact, = (item for item in owner.artifacts(observed(version)) if item.kind == 'plugin')
            token = object()
            owner._prepared[token] = owner._prepare_artifact(artifact, image(source), source, None, image(self.source))
            owner.apply(token, Files(self.root / 'file-backups'), native=install)
            return artifact

        artifact = applied('the overlay before both releases\n', None)
        accepted = owner._receipt(artifact)['accepted']['image']
        with self.assertRaisesRegex(RuntimeError, 'between the moves'):
            rename = Path.rename

            def crash(path, target):
                if path.name == 'staged':
                    raise RuntimeError('between the moves')
                return rename(path, target)

            with patch.object(Path, 'rename', crash):
                applied('the overlay of the stopped release\n', '1.0.0')
        work = Path(owner._receipt(artifact)['pending']['displaced']).parent
        installs.clear()
        applied('the overlay of the newer release\n', work.name)
        self.assertEqual(installs, [])
        self.assertEqual((destination / 'skills/one/SKILL.md').read_text(), 'the overlay of the newer release\n')
        self.assertEqual(owner._receipt(artifact)['accepted']['backup_image'], accepted)

    def lost_work_folder(self):
        """Claude Code 2.1.322 sweeps the folders under its plugin cache that no install names after 14 days (read in
        its bundle, not run)."""
        destination = self._installed_claude()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the stopped replacement'),))
        self.stopped_between_the_moves()
        shutil.rmtree(Path(self.pending_of('plugin:claude')['staged']).parent)
        return destination

    def test_a_replacement_whose_work_folder_went_missing_is_installed_again(self):
        destination = self.lost_work_folder()
        self.assertEqual(plan(self.setup, self.device).blocked, ())
        self.assertEqual(self.apply_reporting(), '')
        self.assertIn('use the stopped replacement', (destination / 'skills/one/SKILL.md').read_text())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_newer_release_takes_over_a_replacement_whose_work_folder_went_missing(self):
        destination = self.lost_work_folder()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use a newer reader'),))
        self.assertEqual(plan(self.setup, self.device).blocked, ())
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use a newer reader', (destination / 'skills/one/SKILL.md').read_text())
        self.assertEqual(self.unnamed_work_folders(), [])

    def lost_work_folder_while_a_session_holds_its_place(self, note):
        destination = self.lost_work_folder()
        destination.mkdir()
        lease = own_lease(destination / '.in_use')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', note),))
        self.assertEqual(plan(self.setup, self.device).blocked, ())
        self.assert_held(destination)
        self.assertTrue(lease.exists())
        lease.unlink()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn(note, (destination / 'skills/one/SKILL.md').read_text())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_replacement_whose_work_folder_went_missing_waits_for_the_session_that_holds_its_place(self):
        self.lost_work_folder_while_a_session_holds_its_place('use the stopped replacement')

    def test_a_newer_release_takes_over_a_replacement_whose_work_folder_went_missing_once_its_place_is_free(self):
        self.lost_work_folder_while_a_session_holds_its_place('use a newer reader')

    def test_a_first_install_whose_work_folder_went_missing_is_installed_again(self):
        destination = self._interrupted_claude_install()
        shutil.rmtree(Path(self.pending_of('plugin:claude')['staged']).parent)
        self.assertEqual(plan(self.setup, self.device).blocked, ())
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the local reader', (destination / 'skills/one/SKILL.md').read_text())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_version_update_whose_old_version_folder_went_missing_while_it_was_stopped_finishes(self):
        old = self._installed_claude()
        self.release_version('2.0.0', 'second release bytes\n')
        self.stopped_between_the_moves()
        shutil.rmtree(old)
        self.assertEqual(plan(self.setup, self.device).blocked, ())
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((old.with_name('2.0.0') / 'ordinary.txt').read_text(), 'second release bytes\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_take_over_stopped_after_moving_a_session_marker_into_the_old_tree_is_finished_by_the_next_run(self):
        destination, lease = self.session_started_between_the_moves()
        displaced = Path(self.pending_of('plugin:claude')['displaced'])
        self.revise_release('newest release\n')
        rename = Path.rename

        def stop(path, target):
            if path == displaced:
                raise RuntimeError('after the marker moved')
            return rename(path, target)

        with patch.object(Path, 'rename', stop), self.assertRaisesRegex(RuntimeError, 'after the marker moved'):
            self.run_apply()
        self.assertFalse(destination.exists())
        self.assertTrue((displaced / '.in_use' / lease.name).exists())
        self.assert_held(destination)
        self.assertTrue(lease.exists())
        lease.unlink()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'newest release\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_take_over_stopped_after_putting_the_old_tree_back_is_finished_by_the_next_run(self):
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_between_the_moves()
        displaced = Path(self.pending_of('codex:LOOSE_SKILL')['displaced'])
        self.revise_loose('A fourth loose skill.\n')
        rename = Path.rename

        def stop(path, target):
            result = rename(path, target)
            if path == displaced:
                raise RuntimeError('after the put-back')
            return result

        with patch.object(Path, 'rename', stop), self.assertRaisesRegex(RuntimeError, 'after the put-back'):
            self.run_apply()
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A second loose skill.\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A fourth loose skill.\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_take_over_stopped_while_removing_the_stopped_replacement_s_stage_is_finished_by_the_next_run(self):
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_between_the_moves()
        work = Path(self.pending_of('codex:LOOSE_SKILL')['staged']).parent
        self.revise_loose('A fourth loose skill.\n')
        rmtree = shutil.rmtree

        def stop_part_way(path, *args, **kwargs):
            if Path(path) != work:
                return rmtree(path, *args, **kwargs)
            (work / 'staged/SKILL.md').unlink()
            raise RuntimeError('stopped during the removal')

        with patch('shutil.rmtree', stop_part_way), self.assertRaisesRegex(RuntimeError, 'stopped during the removal'):
            self.run_apply()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A fourth loose skill.\n')
        self.assertFalse(work.exists())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_take_over_stopped_while_removing_the_backup_before_both_releases_is_finished_by_the_next_run(self):
        old_backup = self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_after_the_moves()
        self.revise_loose('A fourth loose skill.\n')
        rmtree = shutil.rmtree

        def stop_part_way(path, *args, **kwargs):
            if Path(path) != old_backup.parent:
                return rmtree(path, *args, **kwargs)
            (old_backup / 'SKILL.md').unlink()
            raise RuntimeError('stopped during the removal')

        with patch('shutil.rmtree', stop_part_way), self.assertRaisesRegex(RuntimeError, 'stopped during the removal'):
            self.run_apply()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A fourth loose skill.\n')
        self.assertEqual((self.backup_of('codex:LOOSE_SKILL') / 'SKILL.md').read_text(), 'A third loose skill.\n')
        self.assertFalse(old_backup.parent.exists())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_take_over_stopped_while_moving_the_markers_of_sessions_is_finished_by_the_next_run(self):
        destination = self._installed_claude()
        lease = own_lease(destination / '.in_use')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the first replacement'),))
        self.stopped_after_the_moves()
        displaced = Path(self.pending_of('plugin:claude')['displaced'])
        self.assertTrue((displaced / '.in_use' / lease.name).exists())
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the second replacement'),))
        move = Path.replace

        def stop(path, target):
            if path.parent == displaced / '.in_use':
                raise RuntimeError('while moving the markers')
            return move(path, target)

        with patch.object(Path, 'replace', stop), self.assertRaisesRegex(RuntimeError, 'while moving the markers'):
            self.run_apply()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the second replacement', (destination / 'skills/one/SKILL.md').read_text())
        self.assertTrue(lease.exists())
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_take_over_stopped_after_accepting_the_stopped_replacement_is_finished_by_the_next_run(self):
        from accepted_installations import AcceptedInstallations
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_after_the_moves()
        stale = self.pending_of('codex:LOOSE_SKILL')
        self.revise_loose('A fourth loose skill.\n')
        save = AcceptedInstallations._save

        def stop(owner, artifact, accepted, pending=None):
            save(owner, artifact, accepted, pending)
            if artifact.kind == 'loose' and pending is None:
                raise RuntimeError('after the acceptance')

        with patch.object(AcceptedInstallations, '_save', stop), self.assertRaisesRegex(RuntimeError, 'after the acceptance'):
            self.run_apply()
        settled = json.loads(self.receipt_of('codex:LOOSE_SKILL').read_bytes())
        self.assertEqual((settled['pending'], settled['accepted']['image']), (None, stale['target']['image']))
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A fourth loose skill.\n')
        self.assertEqual((self.backup_of('codex:LOOSE_SKILL') / 'SKILL.md').read_text(), 'A third loose skill.\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def test_a_newer_release_over_a_stopped_replacement_in_no_known_state_is_blocked_as_the_pull_refuses(self):
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_between_the_moves()
        self.revise_loose('A fourth loose skill.\n')
        loose = self.roots.codex_home / 'skills/loose'
        loose.mkdir()
        (loose / 'SKILL.md').write_text('Written while the replacement was stopped.\n')
        self.assertEqual([d.detail for d in plan(self.setup, self.device).blocked],
                         ['codex:LOOSE_SKILL:{codex_home}/skills/loose: Unknown partial installation preserved: '
                          'codex:LOOSE_SKILL:{codex_home}/skills/loose'])
        with self.assertRaisesRegex(RuntimeError, 'blocked'):
            self.run_apply()
        self.assertEqual((loose / 'SKILL.md').read_text(), 'Written while the replacement was stopped.\n')

    def test_a_newer_release_over_a_stopped_replacement_whose_old_tree_changed_aside_is_blocked(self):
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_between_the_moves()
        displaced = Path(self.pending_of('codex:LOOSE_SKILL')['displaced'])
        (displaced / 'SKILL.md').write_text('Edited while it was aside.\n')
        self.revise_loose('A fourth loose skill.\n')
        self.assertEqual([d.detail for d in plan(self.setup, self.device).blocked],
                         ['codex:LOOSE_SKILL:{codex_home}/skills/loose: Unknown partial installation preserved: '
                          'codex:LOOSE_SKILL:{codex_home}/skills/loose'])
        with self.assertRaisesRegex(RuntimeError, 'blocked'):
            self.run_apply()
        self.assertEqual((displaced / 'SKILL.md').read_text(), 'Edited while it was aside.\n')
        self.assertFalse((self.roots.codex_home / 'skills/loose').exists())

    def test_a_newer_release_does_not_put_back_a_tree_its_receipt_names_outside_the_replacement_s_work_folder(self):
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_between_the_moves()
        receipt = self.receipt_of('codex:LOOSE_SKILL')
        value = json.loads(receipt.read_bytes())
        elsewhere = self.root / 'elsewhere/displaced'
        elsewhere.parent.mkdir()
        Path(value['pending']['displaced']).rename(elsewhere)
        value['pending']['displaced'] = str(elsewhere)
        receipt.write_text(json.dumps(value))
        self.revise_loose('A fourth loose skill.\n')
        self.assertEqual([d.detail for d in plan(self.setup, self.device).blocked],
                         ['codex:LOOSE_SKILL:{codex_home}/skills/loose: Unknown partial installation preserved: '
                          'codex:LOOSE_SKILL:{codex_home}/skills/loose'])
        self.assertEqual((elsewhere / 'SKILL.md').read_text(), 'A second loose skill.\n')
        self.assertFalse((self.roots.codex_home / 'skills/loose').exists())

    def test_a_stopped_replacement_whose_place_changes_after_a_newer_release_prepared_is_left_as_it_is(self):
        from sync_hosts import Apply
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_between_the_moves()
        displaced = Path(self.pending_of('codex:LOOSE_SKILL')['displaced'])
        self.revise_loose('A fourth loose skill.\n')
        loose = self.roots.codex_home / 'skills/loose'
        original = Apply.run

        def written(operation):
            loose.mkdir()
            (loose / 'SKILL.md').write_text('Written after the plan.\n')
            return original(operation)

        with patch.object(Apply, 'run', written), self.assertRaisesRegex(
                ConcurrentEdit, '^Unknown partial installation preserved: codex:LOOSE_SKILL:'):
            self.run_apply()
        self.assertEqual((loose / 'SKILL.md').read_text(), 'Written after the plan.\n')
        self.assertEqual((displaced / 'SKILL.md').read_text(), 'A second loose skill.\n')

    def test_a_stopped_replacement_whose_receipt_changes_after_a_newer_release_prepared_is_left_as_it_is(self):
        from sync_hosts import Apply
        self.loose_backup()
        self.revise_loose('A third loose skill.\n')
        self.stopped_between_the_moves()
        receipt = self.receipt_of('codex:LOOSE_SKILL')
        displaced = Path(json.loads(receipt.read_bytes())['pending']['displaced'])
        self.revise_loose('A fourth loose skill.\n')
        original = Apply.run

        def edited(operation):
            value = json.loads(receipt.read_bytes())
            value['pending']['swap_before'] = value['pending']['target']['image']
            receipt.write_text(json.dumps(value))
            return original(operation)

        with patch.object(Apply, 'run', edited), self.assertRaisesRegex(
                ConcurrentEdit, '^Pending replacement changed after preparation: codex:LOOSE_SKILL:'):
            self.run_apply()
        self.assertFalse((self.roots.codex_home / 'skills/loose').exists())
        self.assertEqual((displaced / 'SKILL.md').read_text(), 'A second loose skill.\n')

    def test_a_newer_version_takes_over_where_claude_code_cached_its_release_beside_a_first_install_put_back(self):
        destination = self.native_first_install_put_back()
        self.release_version('2.0.0', 'second release bytes\n')
        cached = destination.with_name('2.0.0')
        shutil.copytree(self.source, cached)
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((cached / 'ordinary.txt').read_text(), 'second release bytes\n')
        self.assertIn('use the local reader', (cached / 'skills/one/SKILL.md').read_text())

    def assert_newer_version_refused_over_unowned(self, folder):
        destination = self.native_first_install_put_back()
        self.release_version('2.0.0', 'second release bytes\n')
        cached = destination.with_name('2.0.0')
        shutil.copytree(self.source, cached)
        (cached / folder).mkdir()
        (cached / folder / 'HEAD').write_text('mine\n')
        self.assertEqual([d.detail for d in plan(self.setup, self.device).blocked],
                         [f'plugin:claude:kit@personal-collections: Unowned tree content preserved: {cached}'])
        with self.assertRaisesRegex(RuntimeError, 'blocked'):
            self.run_apply()
        self.assertEqual((cached / folder / 'HEAD').read_text(), 'mine\n')

    def test_a_newer_version_is_refused_where_its_cached_release_holds_a_working_copy(self):
        self.assert_newer_version_refused_over_unowned('.git')

    def test_a_newer_version_is_refused_where_its_cached_release_holds_installed_packages(self):
        self.assert_newer_version_refused_over_unowned('node_modules')

    def test_a_newer_version_whose_place_changes_after_the_checks_leaves_the_stopped_replacement_as_it_is(self):
        from sync_hosts import Apply
        old = self._installed_claude()
        self.release_version('2.0.0', 'second release bytes\n')
        self.stopped_between_the_moves()
        displaced = Path(self.pending_of('plugin:claude')['displaced'])
        self.release_version('3.0.0', 'third release bytes\n')
        newest = old.with_name('3.0.0')
        catalogs = Apply._catalogs

        def occupied(operation):
            newest.mkdir()
            (newest / 'local.txt').write_text('not owned\n')
            return catalogs(operation)

        with patch.object(Apply, '_catalogs', occupied), self.assertRaisesRegex(
                ConcurrentEdit, '^Changed after preparation: plugin:claude:'):
            self.run_apply()
        self.assertTrue(displaced.is_dir())
        self.assertFalse(old.with_name('2.0.0').exists())
        self.assertEqual([path.name for path in newest.iterdir()], ['local.txt'])

    def test_a_newer_release_moves_the_markers_of_sessions_out_of_a_tree_moved_aside_that_changed(self):
        destination = self._installed_claude()
        lease = own_lease(destination / '.in_use')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the first replacement'),))
        self.stopped_after_the_moves()
        displaced = Path(self.pending_of('plugin:claude')['displaced'])
        (displaced / 'mine.txt').write_text('mine')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the second replacement'),))
        self.assertEqual(self.apply_reporting(), f'Work folder left in place: {displaced.parent}: the tree moved aside '
                                                 'changed: its tree is not the one its receipt holds\n')
        self.assertIn('use the second replacement', (destination / 'skills/one/SKILL.md').read_text())
        self.assertTrue(lease.exists())
        self.assertEqual((displaced / 'mine.txt').read_text(), 'mine')

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

    def test_a_loose_skill_from_a_snapshot_installs_the_tree_stored_under_its_digest(self):
        stored = self.root / 'stored'
        stored.mkdir()
        (stored / 'SKILL.md').write_text('A snapshot loose skill.\n')
        digest = self.roots.tree_digest(stored)
        private = self.root / 'private/setup'
        (private / 'snapshots').mkdir(parents=True)
        stored.rename(private / 'snapshots' / digest.removeprefix('sha256:'))
        skill = replace(self.setup.loose['loose'], visibility=Visibility.PRIVATE,
                        source=SnapshotSource({SkillRoot.CODEX: digest}))
        self.setup = replace(self.setup, roots=Roots(self.public / 'setup', private), loose={'loose': skill},
                             lock={**self.setup.lock, 'loose': Lock(intent(skill, {}), placements={SkillRoot.CODEX: digest})})
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.roots.codex_home / 'skills/loose/SKILL.md').read_text(), 'A snapshot loose skill.\n')
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

    def marker_only_folder_is_finished_like_a_missing_cache(self, **kwargs):
        destination = self._interrupted_claude_install(**kwargs)
        destination.mkdir()
        lease = own_lease(destination / '.in_use')
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((destination / 'ordinary.txt').read_text(), (self.source / 'ordinary.txt').read_text())
        self.assertTrue(lease.exists())

    def test_a_pending_claude_cache_whose_folder_holds_only_a_session_marker_is_finished_like_a_missing_one(self):
        self.marker_only_folder_is_finished_like_a_missing_cache()

    def test_a_pending_codex_aligned_cache_whose_folder_holds_only_a_session_marker_is_finished_like_a_missing_one(self):
        self.marker_only_folder_is_finished_like_a_missing_cache(aligned=True)

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

    def claude_reads_rules_through(self, alias):
        from claude_host import ClaudeState
        from types import SimpleNamespace

        def observe(request):
            seen = Observer(Host.CLAUDE, self.roots)
            if alias.is_file():
                seen.add(Surface.RULES, 'CLAUDE.md', FileValue(bytes_digest(alias.read_bytes())))
            return seen.finish('rules host', set())
        claude = SimpleNamespace(host=Host.CLAUDE, project=lambda setup: ClaudeState.project(None, setup), observe=observe)
        self.device = replace(self.device, hosts=(self.host, claude))

    def test_shared_rule_alias_has_one_receipt_and_retargeting_blocks(self):
        self.roots.claude_home.mkdir(parents=True)
        alias = self.roots.claude_home / 'CLAUDE.md'
        alias.symlink_to(self.roots.codex_home / 'AGENTS.md')
        self.claude_reads_rules_through(alias)
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

    def test_a_rule_alias_whose_missing_target_runs_through_a_linked_folder_applies(self):
        self.roots.claude_home.mkdir(parents=True)
        linked = self.root / 'linked-home'
        linked.symlink_to(self.roots.claude_home.parent, target_is_directory=True)
        alias = self.roots.claude_home / 'CLAUDE.md'
        alias.symlink_to(linked / '.codex' / 'AGENTS.md')
        self.claude_reads_rules_through(alias)
        self.assertEqual(self.run_apply().exit_code, 0)
        receipts = [json.loads(p.read_bytes()) for p in self.receipt_paths()]
        self.assertEqual(len([r for r in receipts if r['identity'].startswith('rules:')]), 1)
        self.assertEqual(alias.read_bytes(), b'Managed rules.\n')
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

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
