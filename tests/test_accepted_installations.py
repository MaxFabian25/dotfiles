import contextlib
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
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
from description import (Adaptation, AlignedInstall, ClaudePlacement, CodexPlacement, EngineInstall, Hooks, LocalSource,
                         Lock, LockedTree, LooseSkill, NoteRule, Package, PluginRoles, Repo, RepoSource, Roots, Rules,
                         Setup, SkillRoot, SnapshotSource, ToolNotes, Visibility, intent)
from device import Device
from device_sync import apply, plan
from homes import hold_built_trees_once, isolate_git
from host_observation import Observer, Unread
from host_state import (Approved, DeviceList, DeviceRoots, EngineMarketplace, FactKey, FileValue, Host, InstallDetail,
                        Platform, PluginValue, Released, SkillMode, Surface, TreeValue, Unknown, bytes_digest,
                        upstream_version)
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
        catalog = self.roots.agents_home / 'plugins/marketplace.json'
        if catalog.is_file():
            listed = frozenset(entry['name'] for entry in json.loads(catalog.read_text())['plugins'])
            seen.add(Surface.MARKETPLACE, 'personal', EngineMarketplace(listed))
        config = self.roots.codex_home / 'config.toml'
        switches = tomllib.loads(config.read_text()).get('plugins', {}) if config.is_file() else {}
        for identity, (version, directory) in self.installed.items():
            directory = codex_loads(directory)
            version = directory.name
            enabled = switches.get(identity, {}).get('enabled', True)
            seen.add(Surface.PLUGIN, identity, PluginValue(enabled, Released(upstream_version(version))),
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


def codex_loads(installed):
    """The version folder Codex 0.169.0-alpha.6 loads a plugin from: the highest under the plugin's folder in semver
    order with build metadata compared as text, never a folder whose name starts with a dot beside others, as an
    isolated probe of that version showed on 14 November 2026, and the last dot folder when only such folders are
    there, as that version listed the engine's work folder on 13 November 2026."""
    def order(folder):
        core, _, build = folder.name.partition('+')
        release, _, pre = core.partition('-')
        return tuple(int(part) for part in release.split('.')), not pre, pre, build

    folders = sorted(path for path in installed.parent.glob('*') if path.is_dir())
    named = [path for path in folders if not path.name.startswith('.')]
    return max(named, key=order) if named else folders[-1] if folders else installed


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


def claude_records(roots):
    return roots.claude_home / 'plugins/installed_plugins.json'


def claude_install(roots, plugin, release):
    """Install a release as `claude plugin install` and `claude plugin update` do: copy it to its version folder in
    Claude Code's cache and name that folder in the user-scope entry of the install record, keeping the other
    entries. Gives the version folder."""
    name, marketplace = plugin.split('@')
    destination = roots.claude_home / 'plugins/cache' / marketplace / name / release.version
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(release.root, destination, dirs_exist_ok=True)
    path = claude_records(roots)
    data = json.loads(path.read_text()) if path.is_file() else {'version': 2, 'plugins': {}}
    others = [entry for entry in data['plugins'].get(plugin, []) if entry['scope'] != 'user']
    data['plugins'][plugin] = [{'scope': 'user', 'installPath': str(destination), 'version': release.version,
                                'installedAt': '2026-11-14T08:00:00.000Z'}, *others]
    path.write_text(json.dumps(data, indent=2))
    return destination


def claude_listing(roots, enabled):
    """What `claude plugin list --available --json` prints: each entry of the install record, with the folder the
    record names."""
    path = claude_records(roots)
    plugins = json.loads(path.read_text())['plugins'] if path.is_file() else {}
    return json.dumps({'installed': [{'id': plugin, 'enabled': enabled(plugin), **entry}
                                     for plugin, entries in plugins.items() for entry in entries], 'available': []})


def claude_entry(roots, plugin):
    entry, = (entry for entry in json.loads(claude_records(roots).read_text())['plugins'][plugin]
              if entry['scope'] == 'user')
    return entry


def stopped_clock(when):
    """The engine's clock, stopped at when, a UTC time."""
    class Stopped(datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    return patch('accepted_installations.datetime', Stopped)


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
                # Codex 0.169.0-alpha.6's `plugin add` removed the plugin's other version folders in an isolated
                # probe on 14 November 2026 (evidence/codex/downgrade-summary.txt). The probe had no dot folders.
                release = operation.built['kit']
                destination = self.roots.codex_home / 'plugins/cache/personal/kit' / release.version
                for folder in destination.parent.glob('[!.]*'):
                    shutil.rmtree(folder)
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
        """The folder Codex loads the kit from."""
        return codex_loads(self.host.installed['kit@personal'][1])

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

    def apply_reporting(self, current=None):
        """Run a sync that exits 0 and give what it printed to stderr."""
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assertEqual(self.run_apply(current).exit_code, 0)
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
        """Install, then release a second loose skill, which a sync replaces in place, and a setting the sync writes
        after it. Gives the skill's folder and the setting's file."""
        self.assertEqual(self.run_apply().exit_code, 0)
        self.revise_loose('A second loose skill.\n')
        return self.roots.codex_home / 'skills/loose', self.release_a_setting_written_after_the_plugins()

    def move_refused(self, installed, refusal):
        return (f'Move refused: {installed}: Windows refused the move on each of {ATTEMPTS} tries over {WAITED:g} '
                f'seconds ({refusal}), as it does while a program holds a file in the folder being moved open, or '
                'while access rules forbid the move; this change waits, and the next pull tries it again')

    def assert_move_waits(self, result, installed, npmrc, refusals):
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.blocked, ())
        self.assertEqual([d.detail for d in result.changes],
                         ['codex:LOOSE_SKILL:{codex_home}/skills/loose: Finish a verified pending replacement'])
        [held] = result.held
        self.assertEqual((type(held), held.path, held.line),
                         (accepted_installations.HeldMove, installed, self.move_refused(installed, refusals[-1])))
        self.assertIs(held.refusal.__cause__, refusals[-1])
        self.assertEqual(len(refusals), ATTEMPTS)
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A loose skill.\n')
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
        # WSL is Platform.LINUX too, but its homes come from devices/wsl.toml only.
        self.assertEqual(plan(self.setup, replace(self.device, wsl=True)).profile_mirrors, ())
        self.setup = replace(self.setup, account_homes={DeviceList.WSL: ('.local/share/orca/codex-accounts/*/home',)})
        self.assertEqual([mirror.profile for mirror in plan(self.setup, replace(self.device, wsl=True)).profile_mirrors],
                         [profile])

    def test_an_account_home_holding_an_earlier_version_folder_s_copy_is_brought_up_to_date(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        earlier = self.installed()
        profile = self.roots.home / '.local/share/orca/codex-accounts/account/home'
        copy = profile / 'plugins/cache/personal/kit/1.0.0'
        shutil.copytree(earlier, copy)
        (profile / 'config.toml').write_text('[plugins."kit@personal"]\nenabled = true\n')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the first reader'),),
                             account_homes={Platform.LINUX: ('.local/share/orca/codex-accounts/*/home',)})
        with patch('profile_plugins.apply', side_effect=RuntimeError('before the account homes')), \
                self.assertRaisesRegex(RuntimeError, 'before the account homes'):
            self.run_apply()
        self.assertNotEqual(self.installed(), earlier)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the first reader', (copy / 'skills/one/SKILL.md').read_text())
        self.assertIn('use the local reader', (earlier / 'skills/one/SKILL.md').read_text())

    def test_an_account_home_holding_the_copy_an_earlier_engine_kept_as_its_backup_is_brought_up_to_date(self):
        release, saved, work = self.replaced_in_place_by_an_earlier_engine()
        shutil.copytree(release, work / 'displaced')
        (work / 'displaced/ordinary.txt').write_text('bytes of the release before\n')
        saved['accepted']['backup_image'] = accepted_installations.image(work / 'displaced')
        self.receipt_of('plugin:').write_text(json.dumps(saved))
        profile = self.roots.home / '.local/share/orca/codex-accounts/account/home'
        copy = profile / 'plugins/cache/personal/kit/1.0.0'
        shutil.copytree(work / 'displaced', copy)
        (profile / 'config.toml').write_text('[plugins."kit@personal"]\nenabled = true\n')
        self.setup = replace(self.setup, account_homes={Platform.LINUX: ('.local/share/orca/codex-accounts/*/home',)})
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((copy / 'ordinary.txt').read_text(), 'ordinary release bytes\n')

    def test_an_account_home_whose_cache_is_linked_to_codex_home_s_needs_no_copy(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        profile = self.roots.home / '.local/share/orca/codex-accounts/account/home'
        (profile / 'plugins').mkdir(parents=True)
        (profile / 'plugins/cache').symlink_to(self.roots.codex_home / 'plugins/cache', target_is_directory=True)
        (profile / 'config.toml').write_text('[plugins."kit@personal"]\nenabled = true\n')
        self.setup = replace(self.setup, account_homes={Platform.LINUX: ('.local/share/orca/codex-accounts/*/home',)})
        current = plan(self.setup, self.device)
        self.assertEqual((current.exit_code, current.profile_mirrors, current.blocked), (0, (), ()))

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

    def receipt_of(self, prefix):
        path, = (path for path in self.receipt_paths() if json.loads(path.read_bytes())['identity'].startswith(prefix))
        return path

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

    def test_a_receipt_edited_to_name_a_folder_elsewhere_as_its_stage_stops_its_release_and_keeps_the_folder(self):
        destination = self.loose_waiting_on_a_refused_move()
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
                # A copy of the release, so that only the folder's place and name keep it.
                (mine / 'SKILL.md').write_text('A second loose skill.\n')
                receipt = self.receipt_of('codex:LOOSE_SKILL')
                value = json.loads(receipt.read_bytes())
                value['pending']['staged'] = str(mine)
                receipt.write_text(json.dumps(value))
                with self.assertRaisesRegex(ConcurrentEdit, f'^Prepared stage was edited: {re.escape(str(mine))}$'):
                    self.run_apply()
                self.assertEqual((mine / 'SKILL.md').read_text(), 'A second loose skill.\n')

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

    def loose_waiting_on_a_refused_move(self):
        """A second loose skill whose swap waits on a move Windows refused, with its stage named in its receipt."""
        installed, _ = self.release_in_place()
        with self.refusing(moves=moving_aside(installed)):
            self.assertEqual(self.run_apply().exit_code, 1)
        return installed

    def receipt_naming_another_displaced_path(self, installed):
        receipt = self.receipt_of('codex:LOOSE_SKILL')
        value = json.loads(receipt.read_bytes())
        value['pending']['displaced'] = str(installed.parent / ('.sync-loose-' + 'ab' * 16) / 'displaced')
        receipt.write_text(json.dumps(value))
        return Path(value['pending']['staged'])

    def test_a_stopped_replacement_whose_receipt_names_another_displaced_path_keeps_its_stage(self):
        installed = self.loose_waiting_on_a_refused_move()
        staged = self.receipt_naming_another_displaced_path(installed)
        self.revise_loose('A third loose skill.\n')
        self.assertEqual(self.apply_reporting(),
                         f'Work folder left in place: {staged.parent}: its receipt names a displaced tree outside it\n')
        self.assertTrue(staged.is_dir())
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A third loose skill.\n')

    def test_a_stopped_replacement_whose_receipt_names_another_displaced_path_and_no_work_folder_names_none(self):
        installed = self.loose_waiting_on_a_refused_move()
        shutil.rmtree(self.receipt_naming_another_displaced_path(installed).parent)
        self.revise_loose('A third loose skill.\n')
        self.assertEqual(self.apply_reporting(), '')
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A third loose skill.\n')

    def refused_once(self, refused):
        """Refuse the first move of a stage as Windows does while a program holds the folder, and record its target in
        refused."""
        rename = os.rename

        def attempt(source, target, *args, **kwargs):
            if Path(source).name == 'staged' and not refused:
                refused.append(Path(target))
                raise PermissionError(13, 'Access is denied', str(source), None, str(target))
            return rename(source, target, *args, **kwargs)

        return patch('os.rename', attempt)

    def test_a_plugin_folder_windows_briefly_holds_is_moved_into_place_once_it_is_free(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        before = self.installed()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        refused = []
        with patch.object(portable, 'RETRIES_REFUSALS', True), patch.object(portable.time, 'sleep'), \
                self.refused_once(refused):
            self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(refused, [self.installed()])
        self.assertIn('use the reviewed replacement', (self.installed() / 'skills/one/SKILL.md').read_text())
        self.assertIn('use the local reader', (before / 'skills/one/SKILL.md').read_text())

    def test_a_plugin_stage_edited_while_windows_holds_its_move_stops_and_leaves_the_loaded_folder(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        before = self.installed()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        refused = []

        def edit(seconds):
            [stage] = before.parent.parent.glob('.sync-kit-*/staged')
            (stage / 'ordinary.txt').write_text('edited during the wait\n')

        with patch.object(portable, 'RETRIES_REFUSALS', True), patch.object(portable.time, 'sleep', edit), \
                self.refused_once(refused), self.assertRaisesRegex(ConcurrentEdit, '^Prepared stage was edited: '):
            self.run_apply()
        self.assertFalse(refused[0].exists())
        self.assertEqual(self.installed(), before)
        self.assertIn('use the local reader', (before / 'skills/one/SKILL.md').read_text())

    def test_a_tree_edited_while_windows_holds_it_is_left_where_it_is(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        installed = self.roots.codex_home / 'skills/loose'

        def edit(target):
            if target.name == 'displaced':
                (installed / 'SKILL.md').write_text('edited during the wait\n')

        held = hold_built_trees_once(self, meanwhile=edit)
        self.revise_loose('A second loose skill.\n')
        with self.assertRaisesRegex(ConcurrentEdit, f'^Tree changed before replacement: {re.escape(str(installed))}$'):
            self.run_apply()
        self.assertEqual([path.name for path in held], ['displaced'])
        self.assertEqual((installed / 'SKILL.md').read_text(), 'edited during the wait\n')

    def stage_edited_during_the_wait_for(self, name):
        self.assertEqual(self.run_apply().exit_code, 0)
        installed = self.roots.codex_home / 'skills/loose'

        def edit(target):
            if target.name == name:
                [stage] = installed.parent.glob('.sync-*/staged')
                (stage / 'SKILL.md').write_text('edited during the wait\n')

        held = hold_built_trees_once(self, meanwhile=edit)
        if name == 'displaced':
            held.append(installed)  # so that only the first move waits, and the second is tried once
        self.revise_loose('A second loose skill.\n')
        moved, rename = [], os.rename

        def recorded(source, target, *args, **kwargs):
            result = rename(source, target, *args, **kwargs)
            moved.append(Path(target).name)
            return result

        with patch('os.rename', recorded), self.assertRaises(ConcurrentEdit) as stopped:
            self.run_apply()
        [stage] = installed.parent.glob('.sync-*/staged')
        self.assertEqual(str(stopped.exception), f'Prepared stage was edited: {stage}')
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A loose skill.\n')
        self.assertEqual([path.name for path in stage.parent.iterdir()], ['staged'])
        shutil.rmtree(stage)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A second loose skill.\n')
        return moved

    def test_a_stage_edited_while_windows_holds_the_old_tree_leaves_the_old_tree_where_it_is(self):
        self.assertEqual(self.stage_edited_during_the_wait_for('displaced'), [])

    def test_a_stage_edited_while_windows_holds_the_new_tree_puts_the_old_tree_back(self):
        self.assertEqual(self.stage_edited_during_the_wait_for('loose'), ['displaced', 'loose'])

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
        installed = self.roots.codex_home / 'skills/loose'
        self.revise_loose('A second loose skill.\n')
        moved, rename = [], os.rename

        def recorded(source, target, *args, **kwargs):
            moved.append(Path(target).name)
            return rename(source, target, *args, **kwargs)

        with self.edit_stage_when_the_receipt_is_saved(installed), patch('os.rename', recorded), \
                self.assertRaisesRegex(ConcurrentEdit, '^Prepared stage was edited: '):
            self.run_apply()
        self.assertNotIn('displaced', moved)
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A loose skill.\n')

    def test_an_empty_folder_that_appears_where_the_new_tree_goes_stays_and_the_old_tree_stays_aside(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        installed = self.roots.codex_home / 'skills/loose'
        hold_built_trees_once(self, meanwhile=lambda target: target.name != 'displaced' and target.mkdir())
        self.revise_loose('A second loose skill.\n')
        with patch('sys.stderr', new_callable=io.StringIO) as stderr, \
                self.assertRaisesRegex(ConcurrentEdit, f'^Tree changed during replacement: {re.escape(str(installed))}$'):
            self.run_apply()
        self.assertEqual(list(installed.iterdir()), [])
        displaced = Path(self.pending_of('codex:LOOSE_SKILL')['displaced'])
        self.assertEqual((displaced / 'SKILL.md').read_text(), 'A loose skill.\n')
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
        installed = self.roots.codex_home / 'skills/loose'
        rename = os.rename

        def refuse(source, target, *args, **kwargs):
            if Path(source).name == 'staged':
                raise PermissionError(13, 'Access is denied', str(target))
            return rename(source, target, *args, **kwargs)

        self.revise_loose('A second loose skill.\n')
        with patch.object(portable, 'RETRIES_REFUSALS', False), patch('os.rename', refuse), \
                self.assertRaises(PermissionError) as stopped:
            self.run_apply()
        self.assertEqual(Path(stopped.exception.filename), installed)
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A loose skill.\n')
        return installed

    def test_a_new_tree_that_cannot_be_moved_into_place_leaves_the_old_tree_in_place(self):
        installed = self.move_into_place_refused()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A second loose skill.\n')

    def test_a_newer_release_takes_over_a_replacement_whose_old_tree_was_put_back(self):
        installed = self.move_into_place_refused()
        self.revise_loose('A third loose skill.\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A third loose skill.\n')
        [work] = installed.parent.glob(f'.sync-{installed.name}-*')
        self.assertEqual([path.name for path in work.iterdir()], ['displaced'])
        self.assertEqual((work / 'displaced/SKILL.md').read_text(), 'A loose skill.\n')

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
        installed = self.roots.codex_home / 'skills/loose'
        held = hold_built_trees_once(
            self, meanwhile=lambda target: target.name == 'displaced' and (installed / 'SKILL.md').chmod(0))
        self.revise_loose('A second loose skill.\n')
        with self.assertRaises(PermissionError) as stopped:
            self.run_apply()
        self.assertEqual(Path(stopped.exception.filename), installed / 'SKILL.md')
        self.assertEqual([path.name for path in held], ['displaced'])
        (installed / 'SKILL.md').chmod(0o644)
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A loose skill.\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A second loose skill.\n')

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
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A second loose skill.\n')

    def test_a_new_tree_windows_refuses_to_move_into_place_waits_with_the_old_tree_put_back(self):
        installed, npmrc = self.release_in_place()
        with self.refusing(moves=moving_into_place(installed)) as refusals, \
                patch('sys.stderr', new_callable=io.StringIO) as stderr:
            result = self.run_apply()
        self.assert_move_waits(result, installed, npmrc, refusals)
        self.assertEqual(stderr.getvalue(), '')
        pending = self.pending_of('codex:LOOSE_SKILL')
        self.assertEqual([path.name for path in Path(pending['staged']).parent.iterdir()], ['staged'])
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A second loose skill.\n')

    def test_a_newer_release_takes_over_a_replacement_waiting_on_a_refused_move(self):
        installed, npmrc = self.release_in_place()
        with self.refusing(moves=moving_aside(installed)) as refusals:
            self.assert_move_waits(self.run_apply(), installed, npmrc, refusals)
        self.revise_loose('A third loose skill.\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A third loose skill.\n')
        self.assertEqual(self.unnamed_work_folders(), [])

    def stage_edited_during_the_last_refused_attempt(self, refused):
        installed, npmrc = self.release_in_place()

        def edit():
            [stage] = installed.parent.glob('.sync-*/staged')
            (stage / 'SKILL.md').write_text('edited during the last attempt\n')

        with self.refusing(moves=refused(installed), at_last_attempt=edit) as refusals, \
                self.assertRaises(portable.StillHeld) as stopped:
            self.run_apply()
        self.assertIs(stopped.exception.__cause__, refusals[-1])
        self.assertFalse(npmrc.exists())
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A loose skill.\n')
        [stage] = installed.parent.glob('.sync-*/staged')
        self.assertEqual((stage / 'SKILL.md').read_text(), 'edited during the last attempt\n')
        with self.assertRaisesRegex(ConcurrentEdit, f'^Prepared stage was edited: {re.escape(str(stage))}$'):
            self.run_apply()
        self.assertEqual((stage / 'SKILL.md').read_text(), 'edited during the last attempt\n')

    def test_a_stage_edited_during_the_last_refused_move_aside_stops_the_sync_and_keeps_the_edit(self):
        self.stage_edited_during_the_last_refused_attempt(moving_aside)

    def test_a_stage_edited_during_the_last_refused_move_into_place_stops_the_sync_and_keeps_the_edit(self):
        self.stage_edited_during_the_last_refused_attempt(moving_into_place)

    def test_an_old_tree_windows_refuses_to_put_back_after_a_refused_move_into_place_stops_the_sync(self):
        installed, npmrc = self.release_in_place()
        with self.refusing(moves=lambda source, target: target == installed) as refusals, \
                patch('sys.stderr', new_callable=io.StringIO) as stderr, self.assertRaises(portable.StillHeld) as stopped:
            self.run_apply()
        displaced = Path(self.pending_of('codex:LOOSE_SKILL')['displaced'])
        self.assertIs(stopped.exception.__cause__, refusals[ATTEMPTS - 1])
        self.assertEqual(stderr.getvalue(), f'Old tree left aside: {displaced}, from {installed}: {refusals[-1]}\n')
        self.assertFalse(installed.exists())
        self.assertFalse(npmrc.exists())

    def receipt_save_refused(self, saves_allowed):
        installed, npmrc = self.release_in_place()
        receipt, saves = self.receipt_of('codex:LOOSE_SKILL'), []

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
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A loose skill.\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A second loose skill.\n')
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
        receipt = self.receipt_of('codex:LOOSE_SKILL')
        with self.refusing(moves=moving_aside(installed), at_last_attempt=lambda: change(receipt)) as refusals, \
                self.assertRaises(portable.StillHeld) as stopped:
            self.run_apply()
        self.assertIs(stopped.exception.__cause__, refusals[-1])
        self.assertFalse(npmrc.exists())
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A loose skill.\n')

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
        receipt = self.receipt_of('codex:LOOSE_SKILL')
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
        receipt = self.receipt_of('codex:LOOSE_SKILL')
        with self.refusing(moves=moving_aside(installed), at_last_attempt=receipt.unlink) as refusals:
            self.assertEqual(self.sync_main(), (3, f'Sync stopped: {refusals[-1]}\n'))

    def test_a_sync_stopped_by_a_refused_receipt_save_prints_the_refusal_alone(self):
        installed, npmrc = self.release_in_place()
        receipt = self.receipt_of('codex:LOOSE_SKILL')
        with self.refusing(replaces=lambda source, target: target == receipt) as refusals:
            self.assertEqual(self.sync_main(), (3, f'Sync stopped: {refusals[-1]}\n'))

    @unittest.skipUnless(portable.WINDOWS, 'Windows refuses to move a folder that holds a file a program has open')
    def test_a_tree_holding_a_file_a_program_has_open_waits_and_the_rest_of_the_release_applies(self):
        installed, npmrc = self.release_in_place()
        with (installed / 'SKILL.md').open('rb'):
            result = self.run_apply()
        self.assertEqual(result.exit_code, 1)
        [held] = result.held
        refusal = held.refusal
        self.assertEqual((type(held), held.path, type(refusal), refusal.winerror, Path(refusal.filename),
                          Path(refusal.filename2).name, str(refusal)),
                         (accepted_installations.HeldMove, installed, portable.StillHeld, 5, installed, 'displaced',
                          str(refusal.__cause__)))
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A loose skill.\n')
        self.assertEqual(npmrc.read_text(), 'min-release-age=3\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A second loose skill.\n')

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

        def plugins():
            return {path: value for path, value in self.snapshot().items() if path.parts[:3] == ('home', '.codex', 'plugins')}

        with self.assertRaises(RuntimeError):
            self.run_apply()
        before = plugins()
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 1)
        with self.assertRaisesRegex(ConcurrentEdit, '^Unverified existing content preserved: plugin:codex:kit@personal;'):
            self.run_apply(current)
        self.assertEqual(plugins(), before)
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

    def catalog_names(self):
        return [entry['name'] for entry in json.loads((self.roots.agents_home / 'plugins/marketplace.json').read_bytes())['plugins']]

    def test_an_entry_the_description_does_not_manage_leaves_the_catalog_without_reinstalling(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        catalog = self.roots.agents_home / 'plugins/marketplace.json'
        data = json.loads(catalog.read_bytes())
        data['plugins'].append({'name': 'retired', 'source': {'source': 'local', 'path': './retired/1.0'}})
        catalog.write_text(json.dumps(data))
        current = plan(self.setup, self.device)
        self.assertEqual([(d.expected.plugins, d.observed.plugins) for d in current.changes
                          if d.key and d.key.surface == Surface.MARKETPLACE], [({'kit'}, {'kit', 'retired'})])
        self.calls.clear()
        self.assertEqual(self.run_apply(current).exit_code, 0)
        self.assertEqual(self.catalog_names(), ['kit'])
        self.assertFalse(any(args[:2] == ('plugin', 'add') for _, args in self.calls))
        self.assertIn((Host.CODEX, ('plugin', 'marketplace', 'add', str(self.roots.home), '--json')), self.calls)
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_a_package_switched_off_leaves_the_catalog(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        kit = replace(self.setup.packages['kit'], codex=CodexPlacement((EngineInstall(False),)))
        self.setup = replace(self.setup, packages={'kit': kit},
                             lock={**self.setup.lock, 'kit': replace(self.setup.lock['kit'], intent=intent(kit, {}))})
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(self.catalog_names(), [])
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

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
        self.claude_plugin = plugin

        def observe(command, **_):
            if command[-1] == '--version':
                return 'claude-test\n'
            if command[-4:] == ['plugin', 'list', '--available', '--json']:
                switches = self.claude_switches()
                return claude_listing(self.roots, lambda identity: switches.get(identity, False))
            raise AssertionError(command)

        def native(operation, host, *args):
            self.calls.append((host, args))
            if args[:2] in (('plugin', 'install'), ('plugin', 'update')):
                destination = claude_install(self.roots, plugin, operation.claude[plugin])
                if self.interrupt:
                    self.interrupt(destination)
                self.switch_claude_on()
            return ''

        def build(builder, package, codex, *, verify=True):
            version = builder.setup.lock['kit'].trees[Host.CLAUDE].version
            release = catalog / 'plugins/kit' / version
            if not release.exists():     # as claude_package.build, which leaves a finished build as it is
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

    def test_a_claude_engine_catalog_that_lists_too_much_or_too_little_converges_in_one_apply(self):
        self.keep_kit_current_on_claude()
        self.assertEqual(self.run_apply().exit_code, 0)
        path = self.roots.engine / 'claude/.claude-plugin/marketplace.json'

        def listed():
            return [entry['name'] for entry in json.loads(path.read_bytes())['plugins']]
        self.assertEqual(listed(), ['kit'])
        retired = {'name': 'retired', 'source': './plugins/retired/1', 'version': '1'}
        for label, plugins in (('a retired entry', lambda current: current + [retired]), ('no entries', lambda _: [])):
            with self.subTest(label):
                data = json.loads(path.read_bytes())
                data['plugins'] = plugins(data['plugins'])
                path.write_text(json.dumps(data), encoding='utf-8')
                current = plan(self.setup, self.device)
                self.assertIn(FactKey(Host.CLAUDE, Surface.MARKETPLACE, 'personal-collections'),
                              [d.key for d in current.changes])
                self.calls.clear()
                self.assertEqual(self.run_apply(current).exit_code, 0)
                self.assertEqual(listed(), ['kit'])
                self.assertIn(('plugin', 'marketplace', 'add'), [args[:3] for _, args in self.calls])
                self.assertEqual(plan(self.setup, self.device).changes, ())

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
        manifest = self.source / '.claude-plugin/plugin.json'
        if manifest.exists():
            manifest.write_text('{"name":"kit","version":"2.0.0"}')
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

    def _claude_kit(self, *, aligned=False, implicit=False):
        """Release the kit to Claude Code alone, on a Claude Code whose CLI keeps its install record as
        `claude_install` and `claude_listing` do. Gives the folder of the release, where its native install goes."""
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
            (alignment.parent / 'plugin.json').write_text('{"name":"kit","version":"1.0.0"}')
        # The kit's tool notes make the installed image differ from the release, so the engine writes a folder of its
        # own. A real description refuses tool notes for an engine install, since Claude Code runs the release.
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
        self.claude_plugin = plugin

        def observe_command(command, **_):
            if command[-1] == '--version':
                return 'claude-test\n'
            if command[-4:] == ['plugin', 'list', '--available', '--json']:
                return claude_listing(self.roots, lambda _: True)
            raise AssertionError(command)

        def native_command(operation, host, *args):
            self.calls.append((host, args))
            if args[:2] in (('plugin', 'install'), ('plugin', 'update')):
                destination = claude_install(self.roots, plugin, operation.claude[plugin])
                if self.interrupt:
                    self.interrupt(destination)
            return ''

        def build_claude(builder, package, codex, *, verify=True):
            version = builder.setup.lock['kit'].trees[Host.CLAUDE].version
            release = (self.roots.engine / '.sync/claude-aligned' if aligned else marketplace) / 'plugins/kit' / version
            release.parent.mkdir(parents=True, exist_ok=True)
            if release.exists():
                shutil.rmtree(release)
            shutil.copytree(codex.root, release)
            return {plugin: Built('kit', release, version, builder.roots.tree_digest(release))}

        patch('claude_host.run', observe_command).start()
        patch('sync_hosts.Apply.command', native_command).start()
        patch.object(Builder, 'claude', build_claude).start()
        return self.roots.claude_home / 'plugins/cache' / marketplace_name / 'kit/1.0.0'

    def _claude_receipt(self):
        return next(path for path in self.receipt_paths()
                    if json.loads(path.read_bytes())['identity'] == f'plugin:claude:{self.claude_plugin}')

    def claude_loads(self):
        if self.claude_plugin.endswith('@codex-aligned'):
            catalog = self.roots.claude_home / 'marketplaces/codex-aligned'
            entry, = json.loads((catalog / '.claude-plugin/marketplace.json').read_bytes())['plugins']
            return catalog / entry['source']
        return Path(claude_entry(self.roots, self.claude_plugin)['installPath'])

    def test_aligned_delivery_accepts_the_loaded_tree_without_installing_a_cache(self):
        self._claude_kit(aligned=True)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertFalse((self.roots.claude_home / 'plugins/cache').exists())
        self.assertFalse(claude_records(self.roots).exists())
        self.assertEqual([args for host, args in self.calls if args[:2] in
                          (('plugin', 'install'), ('plugin', 'update'))], [])
        loaded = self.claude_loads()
        self.assertEqual((loaded / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertIn('disable-model-invocation: true', (loaded / 'skills/one/SKILL.md').read_text())
        accepted = json.loads(self._claude_receipt().read_bytes())['accepted']
        self.assertEqual(accepted['path'], str(loaded))
        before = self.snapshot()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(self.snapshot(), before)

    def legacy_aligned_receipt(self):
        from accepted_installations import _bindings
        from sync_sources import Built

        loaded = self._installed_claude(aligned=True)
        receipt = self._claude_receipt()
        value = json.loads(receipt.read_bytes())
        cache = claude_install(self.roots, self.claude_plugin,
                               Built('kit', loaded, '1.0.0', self.roots.tree_digest(loaded)))
        accepted = value['accepted']
        accepted.pop('delivery')
        accepted['path'] = str(cache)
        accepted['bindings'] = _bindings((cache,))
        receipt.write_text(json.dumps(value))
        return loaded, cache, value

    def test_aligned_receipt_migration_preserves_cache_records_and_old_evidence(self):
        loaded, cache, previous = self.legacy_aligned_receipt()
        records = json.loads(claude_records(self.roots).read_bytes())
        records['plugins'][self.claude_plugin].append({'scope': 'project', 'installPath': str(cache), 'version': '0.9.0'})
        claude_records(self.roots).write_text(json.dumps(records))
        before = self.state_of(cache), claude_records(self.roots).read_bytes(), self.state_of(loaded)
        self.assertEqual(self.run_apply().exit_code, 0)
        accepted = json.loads(self._claude_receipt().read_bytes())['accepted']
        self.assertEqual((accepted['path'], accepted['delivery'], accepted['legacy']),
                         (str(loaded), 'directory', previous))
        self.assertEqual((self.state_of(cache), claude_records(self.roots).read_bytes(), self.state_of(loaded)), before)
        self.assertEqual(self.calls, [])
        shutil.rmtree(cache)
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_aligned_migration_refuses_an_edited_cache_or_loaded_build(self):
        loaded, cache, _ = self.legacy_aligned_receipt()
        for folder in (cache, loaded):
            with self.subTest(folder=folder):
                marker = folder / 'ordinary.txt'
                original = marker.read_bytes()
                marker.write_text('local edit\n')
                before = self.snapshot()
                current = plan(self.setup, self.device)
                self.assertEqual(current.exit_code, 2)
                self.assertTrue(any(d.kind.value == 'local-edit' for d in current.blocked))
                with self.assertRaises(RuntimeError):
                    self.run_apply(current)
                self.assertEqual(self.snapshot(), before)
                marker.write_bytes(original)

    def test_aligned_migration_accepts_a_recorded_pending_build_before_a_newer_release(self):
        from accepted_installations import _bindings, image

        loaded, cache, previous = self.legacy_aligned_receipt()
        release2 = loaded.with_name('2.0.0')
        shutil.copytree(loaded, release2)
        for relative in ('.codex-plugin/plugin.json', '.claude-plugin/plugin.json'):
            (release2 / relative).write_text('{"name":"kit","version":"2.0.0"}')
        cache2 = cache.with_name('2.0.0')
        shutil.copytree(release2, cache2)
        work = cache2.parent / ('.sync-2.0.0-' + 'cd' * 16)
        target = {**previous['accepted'], 'path': str(cache2), 'image': image(cache2),
                  'bindings': _bindings((cache2,)),
                  'intent': 'interrupted-version-two'}
        previous['pending'] = {'before': image(cache), 'before_path': str(cache),
                               'bindings': target['bindings'], 'destination_before': image(cache2),
                               'target': target, 'pristine': image(cache2),
                               'staged': str(work / 'staged'), 'displaced': str(work / 'displaced'),
                               'swap_before': image(cache2)}
        self._claude_receipt().write_text(json.dumps(previous))
        catalog = loaded.parents[2] / '.claude-plugin/marketplace.json'
        data = json.loads(catalog.read_bytes())
        data['plugins'][0].update(source='./plugins/kit/2.0.0', version='2.0.0')
        catalog.write_text(json.dumps(data))
        self.point_claude_at(cache2)
        for relative in ('.codex-plugin/plugin.json', '.claude-plugin/plugin.json'):
            (self.source / relative).write_text('{"name":"kit","version":"3.0.0"}')
        digest = self.roots.tree_digest(self.source)
        kit = replace(self.setup.packages['kit'], source=LocalSource('owned/kit', digest))
        lock = replace(self.setup.lock['kit'], intent=intent(kit, {}), trees={Host.CLAUDE: LockedTree('3.0.0', digest)})
        self.setup = replace(self.setup, packages={'kit': kit}, lock={'kit': lock})
        for folder in (release2, cache2):
            with self.subTest(folder=folder):
                marker = folder / 'ordinary.txt'
                original = marker.read_bytes()
                marker.write_bytes(b'local edit to pending release')
                edited = self.snapshot()
                current = plan(self.setup, self.device)
                self.assertEqual(current.exit_code, 2)
                self.assertTrue(any(item.kind.value == 'local-edit' for item in current.blocked))
                with self.assertRaises(RuntimeError):
                    self.run_apply(current)
                self.assertEqual(self.snapshot(), edited)
                marker.write_bytes(original)
        before = self.state_of(loaded), self.state_of(release2), self.state_of(cache), self.state_of(cache2)
        records = claude_records(self.roots).read_bytes()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(self.claude_loads().name, '3.0.0')
        self.assertEqual((self.state_of(loaded), self.state_of(release2), self.state_of(cache), self.state_of(cache2)), before)
        self.assertEqual(claude_records(self.roots).read_bytes(), records)
        self.assertEqual(json.loads(self._claude_receipt().read_bytes())['accepted']['legacy'], previous)

    def test_aligned_update_and_rollback_leave_prior_session_bytes_and_native_records(self):
        loaded, cache, _ = self.legacy_aligned_receipt()
        first_setup = self.setup
        first_source = {path.relative_to(self.source): path.read_bytes() for path in self.source.rglob('*') if path.is_file()}
        before = self.state_of(loaded), self.state_of(cache), claude_records(self.roots).read_bytes()
        self.release_kit_2_on_claude()
        self.assertEqual(self.run_apply().exit_code, 0)
        second = self.claude_loads()
        self.assertNotEqual(second, loaded)
        self.assertEqual(json.loads((second / '.claude-plugin/plugin.json').read_bytes())['version'], '2.0.0')
        second_before = self.state_of(second)
        self.assertEqual((self.state_of(loaded), self.state_of(cache), claude_records(self.roots).read_bytes()), before)
        self.setup = first_setup
        for relative, data in first_source.items():
            (self.source / relative).write_bytes(data)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(self.claude_loads(), loaded)
        self.assertEqual(self.state_of(second), second_before)
        self.assertEqual([args for host, args in self.calls if args[:2] in
                          (('plugin', 'install'), ('plugin', 'update'))], [])

    def test_aligned_preparation_rejects_catalog_cache_and_loaded_source_races(self):
        from sync_hosts import Apply

        loaded, cache, _ = self.legacy_aligned_receipt()
        catalog = loaded.parents[2] / '.claude-plugin/marketplace.json'
        for path in (catalog, cache / 'ordinary.txt', loaded / 'ordinary.txt'):
            with self.subTest(path=path):
                current = plan(self.setup, self.device)
                operation = Apply(self.setup, self.device, current, Builder(self.setup, self.roots, self.roots.engine))
                with operation.owner.staging:
                    operation.prepare()
                    original, receipt = path.read_bytes(), self._claude_receipt().read_bytes()
                    path.write_bytes(original + b' ')
                    with self.assertRaises(ConcurrentEdit):
                        operation.run()
                    self.assertEqual(self._claude_receipt().read_bytes(), receipt)
                    self.assertEqual(path.read_bytes(), original + b' ')
                    path.write_bytes(original)

    def test_aligned_disabled_kept_current_retains_project_and_local_switches(self):
        self._claude_kit(aligned=True)
        kit = replace(self.setup.packages['kit'], claude=ClaudePlacement((AlignedInstall(False, keep_current=True),)))
        self.setup = replace(self.setup, packages={'kit': kit}, lock={
            'kit': replace(self.setup.lock['kit'], intent=intent(kit, {}))})
        settings = self.claude_settings()
        data = json.loads(settings.read_bytes())
        data['enabledPlugins'] = {self.claude_plugin: False}
        settings.write_text(json.dumps(data))
        scoped = self.roots.home / 'project/.claude'
        scoped.mkdir(parents=True)
        for name in ('settings.json', 'settings.local.json'):
            (scoped / name).write_text(json.dumps({'enabledPlugins': {self.claude_plugin: True}}))
        before = self.state_of(scoped)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIs(self.claude_switches()[self.claude_plugin], False)
        self.assertEqual(self.state_of(scoped), before)
        self.assertEqual((self.claude_loads() / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertFalse(claude_records(self.roots).exists())

    def test_aligned_catalog_change_during_publication_preserves_the_other_writer(self):
        from sync_hosts import Apply

        loaded = self._installed_claude(aligned=True)
        self.release_kit_2_on_claude()
        catalog = loaded.parents[2] / '.claude-plugin/marketplace.json'
        previous = self._claude_receipt().read_bytes()
        stage = Apply._stage_source
        raced = []

        def competing_writer(source, destination):
            stage(source, destination)
            data = json.loads(catalog.read_bytes())
            data['another_writer'] = True
            catalog.write_text(json.dumps(data))
            raced.append(catalog.read_bytes())

        with patch.object(Apply, '_stage_source', staticmethod(competing_writer)), self.assertRaisesRegex(
                ConcurrentEdit, 'Changed during sync'):
            self.run_apply()
        self.assertEqual(len(raced), 1)
        self.assertEqual(catalog.read_bytes(), raced[0])
        self.assertEqual(self._claude_receipt().read_bytes(), previous)
        self.assertEqual(self.claude_loads(), loaded)

    def test_aligned_catalog_or_build_change_after_publication_prevents_acceptance(self):
        from sync_hosts import Apply

        loaded = self._installed_claude(aligned=True)
        self.release_kit_2_on_claude()
        catalog = loaded.parents[2] / '.claude-plugin/marketplace.json'
        before_catalog, before_receipt = catalog.read_bytes(), self._claude_receipt().read_bytes()
        command = Apply.command
        for edit in ('catalog', 'build'):
            with self.subTest(edit=edit):
                def competing_writer(operation, host, *args):
                    result = command(operation, host, *args)
                    if args[:3] == ('plugin', 'marketplace', 'add'):
                        if edit == 'catalog':
                            catalog.write_bytes(before_catalog)
                        else:
                            (self.claude_loads() / 'ordinary.txt').write_text('concurrent edit\n')
                    return result

                with patch.object(Apply, 'command', competing_writer), self.assertRaisesRegex(
                        ConcurrentEdit, 'no longer selects|differs from preparation'):
                    self.run_apply()
                self.assertEqual(self._claude_receipt().read_bytes(), before_receipt)
                self.assertEqual((loaded / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
                self.assertFalse(claude_records(self.roots).exists())

    def _installed_claude(self, **kit):
        self._claude_kit(**kit)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.calls.clear()
        return self.claude_loads()

    def left_aside_by_an_earlier_engine(self, loaded, receipt):
        """Turn a plugin that a host loads from loaded into what an earlier engine left when its in-place replacement
        stopped between its two moves: the release folder moved into a work folder beside a stage, as the only folder
        of the plugin, and a receipt whose pending record names both. Gives the release folder and the work folder."""
        release = loaded.parent / '1.0.0'
        if loaded != release:
            shutil.rmtree(release)
        work = release.parent / ('.sync-1.0.0-' + 'ab' * 16)
        work.mkdir()
        loaded.rename(work / 'displaced')
        shutil.copytree(work / 'displaced', work / 'staged')
        value = json.loads(receipt.read_bytes())
        accepted = {**value['accepted'], 'path': str(release)}
        tree = accepted['image']
        value.update(accepted=accepted, pending={
            'before': tree, 'before_path': str(release), 'bindings': accepted['bindings'], 'destination_before': tree,
            'target': accepted, 'pristine': tree, 'staged': str(work / 'staged'), 'displaced': str(work / 'displaced'),
            'swap_before': tree})
        receipt.write_text(json.dumps(value))
        return release, work

    def legacy_claude_swap_aside(self, **kit):
        """left_aside_by_an_earlier_engine for the Claude cache, whose install record names the release folder."""
        if kit.get('aligned'):
            _, cached, _ = self.legacy_aligned_receipt()
        else:
            cached = self._installed_claude(**kit)
        release, work = self.left_aside_by_an_earlier_engine(cached, self._claude_receipt())
        self.point_claude_at(release)
        return release, work

    def point_claude_at(self, folder):
        data = json.loads(claude_records(self.roots).read_text())
        entry, = (entry for entry in data['plugins'][self.claude_plugin] if entry['scope'] == 'user')
        entry['installPath'] = str(folder)
        claude_records(self.roots).write_text(json.dumps(data))

    def state_of(self, folder):
        """Each entry of folder, and the folder itself, with its modification time and its bytes if a file."""
        return {path.relative_to(folder): (path.lstat().st_mtime_ns, path.read_bytes() if path.is_file() else None)
                for path in (folder, *folder.rglob('*'))}

    def test_a_release_gets_a_version_folder_of_its_own_that_codex_loads(self):
        with stopped_clock(datetime(2026, 11, 14, 19, 0, 0, 250, tzinfo=timezone.utc)):
            self.assertEqual(self.run_apply().exit_code, 0)
        release, loaded = self.host.installed['kit@personal'][1], self.installed()
        self.assertEqual((release.name, loaded.parent), ('1.0.0', release.parent))
        accepted = json.loads(self.receipt_of('plugin:').read_bytes())['accepted']
        # The digest of the image in its canonical encoding, so a later release can verify a folder this one named.
        canonical = json.dumps(accepted['image'], sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        self.assertEqual(loaded.name, f'1.0.0+notes.20261114T190000Z.{hashlib.sha256(canonical).hexdigest()[:12]}')
        self.assertIn('use the local reader', (loaded / 'skills/one/SKILL.md').read_text())
        self.assertNotIn('use the local reader', (release / 'skills/one/SKILL.md').read_text())
        self.assertEqual(accepted['path'], str(loaded))
        self.assertEqual(list(release.parent.parent.glob('.sync-*')), [])
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_a_change_leaves_the_folder_codex_loaded_as_it_was(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        first = self.installed()
        before = self.state_of(first)
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertNotEqual(self.installed(), first)
        self.assertIn('use the reviewed replacement', (self.installed() / 'skills/one/SKILL.md').read_text())
        self.assertEqual(self.state_of(first), before)
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_a_rollback_gets_a_newer_folder_than_the_one_that_held_its_content_before(self):
        notes = self.setup.note_rules
        with stopped_clock(datetime(2026, 11, 14, 19, 0, 0, tzinfo=timezone.utc)):
            self.assertEqual(self.run_apply().exit_code, 0)
            first = self.installed()
            self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
            self.assertEqual(self.run_apply().exit_code, 0)
            second = self.installed()
            self.setup = replace(self.setup, note_rules=notes)
            self.assertEqual(self.run_apply().exit_code, 0)
        third = self.installed()
        self.assertEqual([folder.name.split('.')[-2] for folder in (first, second, third)],
                         ['20261114T190000Z', '20261114T190001Z', '20261114T190002Z'])
        self.assertEqual(third.name.split('.')[-1], first.name.split('.')[-1])
        self.assertEqual((third / 'skills/one/SKILL.md').read_bytes(), (first / 'skills/one/SKILL.md').read_bytes())
        self.assertTrue(second.is_dir())
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_a_folder_windows_refuses_to_move_into_place_waits_with_the_loaded_folder_kept(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        first = self.installed()
        npmrc = self.release_a_setting_written_after_the_plugins()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        with self.refusing(moves=lambda source, target: source.name == 'staged' and target.parent == first.parent) \
                as refusals:
            result = self.run_apply()
        self.assertEqual((result.exit_code, result.blocked), (1, ()))
        [held] = result.held
        # The stage waits outside the plugin's folder, where neither host reads versions.
        self.assertEqual((type(held), held.path, held.into.parent, held.path.parent.parent),
                         (accepted_installations.HeldMove, Path(refusals[-1].filename), first.parent, first.parent.parent))
        self.assertEqual(held.line, (
            f'Move refused: {held.path}: Windows refused its move to {held.into} on each of {ATTEMPTS} tries over '
            f'{WAITED:g} seconds ({refusals[-1]}), as it does while a program holds a file in the folder being moved '
            f'open, or while access rules forbid the move; {held.into} was not created, the folder the app loads '
            'stays in use, and the next pull tries again'))
        self.assertEqual(len(refusals), ATTEMPTS)
        self.assertFalse(held.into.exists())
        self.assertEqual(self.installed(), first)
        self.assertEqual(list(first.parent.parent.glob('.sync-*')), [])
        self.assertEqual(npmrc.read_text(), 'min-release-age=3\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (self.installed() / 'skills/one/SKILL.md').read_text())
        self.assertIn('use the local reader', (first / 'skills/one/SKILL.md').read_text())

    def test_an_upstream_release_is_installed_natively_once_and_gets_a_folder_of_its_own(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        first = self.installed()
        self.release_version('2.0.0', 'second release bytes\n')
        self.calls.clear()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual([args[:3] for _, args in self.calls if args[:2] == ('plugin', 'add')],
                         [('plugin', 'add', 'kit@personal')])
        loaded = self.installed()
        self.assertEqual((loaded.parent, loaded.name.startswith('2.0.0+notes.')), (first.parent, True))
        self.assertEqual((loaded / 'ordinary.txt').read_text(), 'second release bytes\n')
        self.assertIn('use the local reader', (loaded / 'skills/one/SKILL.md').read_text())
        self.assertEqual(sorted(path.name for path in first.parent.iterdir()), ['2.0.0', loaded.name])
        self.calls.clear()
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(self.calls, [])

    def test_a_codex_plugin_an_earlier_engine_left_moved_aside_gets_a_folder_of_its_own(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        release, work = self.left_aside_by_an_earlier_engine(self.installed(), self.receipt_of('plugin:'))
        # Codex lists the work folder when it is the plugin's only folder.
        self.assertEqual(self.installed(), work)
        self.calls.clear()
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.apply_reporting(),
                         f'Work folders left in place: {work}: an earlier engine replaced plugin:codex:kit@personal in '
                         'place there, and this one leaves those folders as they are\n')
        self.assertEqual(self.calls, [])
        self.assertEqual((self.installed().parent, self.installed().name.startswith('1.0.0+notes.')),
                         (release.parent, True))
        self.assertIn('use the local reader', (self.installed() / 'skills/one/SKILL.md').read_text())
        self.assertEqual(sorted(path.name for path in work.iterdir()), ['displaced', 'staged'])
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def replaced_in_place_by_an_earlier_engine(self):
        """Install, then turn the plugin into what an earlier engine left after it replaced the release folder in
        place: the receipt names that folder, and a work folder beside it holds the backup the receipt names. Gives
        the release folder, the receipt as saved, and the work folder."""
        self.assertEqual(self.run_apply().exit_code, 0)
        loaded = self.installed()
        release = loaded.parent / '1.0.0'
        shutil.rmtree(release)
        loaded.rename(release)
        work = release.parent / ('.sync-1.0.0-' + 'ab' * 16)
        receipt = self.receipt_of('plugin:')
        value = json.loads(receipt.read_bytes())
        value['accepted'] = {**value['accepted'], 'path': str(release), 'backup': str(work / 'displaced'),
                             'backup_image': value['accepted']['image']}
        receipt.write_text(json.dumps(value))
        return release, value, work

    def test_each_state_an_earlier_engine_s_replacement_stopped_in_is_known_and_any_other_blocks(self):
        release, saved, _ = self.replaced_in_place_by_an_earlier_engine()
        held = accepted_installations.image(release)
        other = {'kind': 'tree', 'entries': {'other.txt': {'kind': 'file', 'sha256': 'sha256:' + '0' * 64}}}
        accepted = {**saved['accepted'], 'image': other}
        work = release.parent / ('.sync-1.0.0-' + 'cd' * 16)
        # A newer release, so that only the receipt's record of the stopped replacement vouches for the folder.
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        for known in (None, 'destination_before', 'pristine', 'target', 'before'):
            with self.subTest(known=known):
                tree = {name: held if name == known else other
                        for name in ('before', 'destination_before', 'pristine', 'target')}
                pending = {'before': tree['before'], 'before_path': str(release), 'bindings': accepted['bindings'],
                           'destination_before': tree['destination_before'], 'pristine': tree['pristine'],
                           'target': {**accepted, 'image': tree['target']}, 'staged': str(work / 'staged'),
                           'displaced': str(work / 'displaced'), 'swap_before': other}
                self.receipt_of('plugin:').write_text(json.dumps({**saved, 'accepted': accepted, 'pending': pending}))
                current = plan(self.setup, self.device)
                if known:
                    self.assertEqual((current.blocked, [d.detail for d in current.changes]),
                                     ((), ['plugin:codex:kit@personal: Write a folder of its own beside the replacement '
                                           'an earlier engine left unfinished']))
                else:
                    self.assertEqual([d.detail for d in current.blocked],
                                     ['plugin:codex:kit@personal: Unverified existing content preserved: '
                                      'plugin:codex:kit@personal; an independently pinned baseline is required'])
        self.assertEqual(self.run_apply(current).exit_code, 0)
        self.assertIn('use the reviewed replacement', (self.installed() / 'skills/one/SKILL.md').read_text())
        self.assertIn('use the local reader', (release / 'skills/one/SKILL.md').read_text())

    def test_the_backup_an_earlier_engine_kept_beside_a_plugin_is_named_once_and_left_in_place(self):
        release, _, work = self.replaced_in_place_by_an_earlier_engine()
        shutil.copytree(release, work / 'displaced')
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        self.assertEqual(self.apply_reporting(),
                         f'Work folders left in place: {work}: an earlier engine replaced plugin:codex:kit@personal in '
                         'place there, and this one leaves those folders as they are\n')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the second replacement'),))
        self.assertEqual(self.apply_reporting(), '')
        self.assertIn('use the second replacement', (self.installed() / 'skills/one/SKILL.md').read_text())
        self.assertEqual([path.name for path in work.iterdir()], ['displaced'])
        self.assertIn('use the local reader', (release / 'skills/one/SKILL.md').read_text())

    def test_a_backup_an_earlier_engine_named_that_is_gone_is_not_named(self):
        self.replaced_in_place_by_an_earlier_engine()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        self.assertEqual(self.apply_reporting(), '')
        self.assertIn('use the reviewed replacement', (self.installed() / 'skills/one/SKILL.md').read_text())

    def test_a_folder_that_appears_where_the_new_folder_goes_while_windows_holds_the_move_stays(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        first = self.installed()
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        refused = []
        with patch.object(portable, 'RETRIES_REFUSALS', True), \
                patch.object(portable.time, 'sleep', lambda seconds: refused[0].mkdir()), self.refused_once(refused), \
                self.assertRaisesRegex(ConcurrentEdit, '^Tree changed during replacement: '):
            self.run_apply()
        self.assertEqual(list(refused[0].iterdir()), [])
        self.assertIn('use the local reader', (first / 'skills/one/SKILL.md').read_text())
        self.assertEqual(list(first.parent.parent.glob('.sync-*')), [])

    def test_a_new_folder_edited_before_its_receipt_is_saved_stops_and_is_not_accepted(self):
        from accepted_installations import AcceptedInstallations
        self.assertEqual(self.run_apply().exit_code, 0)
        write = AcceptedInstallations._write_folder

        def edited(owner, prepared):
            folder = write(owner, prepared)
            (folder / 'ordinary.txt').write_text('edited as it appeared\n')
            return folder

        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        with patch.object(AcceptedInstallations, '_write_folder', edited), self.assertRaisesRegex(
                ConcurrentEdit, '^Effective output failed verification: plugin:codex:kit@personal$'):
            self.run_apply()
        with self.assertRaisesRegex(ConcurrentEdit, 'Unverified existing content preserved: plugin:codex:kit@personal;'):
            self.run_apply()
        self.assertEqual((self.installed() / 'ordinary.txt').read_text(), 'edited as it appeared\n')

    def test_a_change_that_leaves_a_plugin_s_bytes_as_they_are_writes_no_folder(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        loaded = self.installed()
        folders, state = sorted(loaded.parent.iterdir()), self.state_of(loaded)
        self.setup = replace(self.setup, note_rules=(*self.setup.note_rules, NoteRule('Grep', 'a tool the kit never names')))
        self.assertEqual(plan(self.setup, self.device).exit_code, 1)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((sorted(loaded.parent.iterdir()), self.state_of(loaded)), (folders, state))
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_a_plugin_stage_edited_before_its_move_stops_and_writes_no_folder(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        first = self.installed()
        folders = sorted(first.parent.iterdir())
        copy = accepted_installations.copy_tree

        def edited(source, destination, *args):
            copy(source, destination, *args)
            if destination.name == 'staged':
                (destination / 'ordinary.txt').write_text('edited in the stage\n')

        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        with patch('accepted_installations.copy_tree', edited), \
                self.assertRaisesRegex(ConcurrentEdit, '^Prepared stage was edited: '):
            self.run_apply()
        self.assertEqual(sorted(first.parent.iterdir()), folders)

    def test_a_sync_stopped_after_the_native_install_of_a_new_release_is_finished_by_the_next_run(self):
        from accepted_installations import AcceptedInstallations
        self.assertEqual(self.run_apply().exit_code, 0)
        first = self.installed()
        self.release_version('2.0.0', 'second release bytes\n')
        with patch.object(AcceptedInstallations, '_write_folder', side_effect=RuntimeError('stopped')), \
                self.assertRaisesRegex(RuntimeError, 'stopped'):
            self.run_apply()
        self.assertEqual(self.installed(), first.parent / '2.0.0')
        self.calls.clear()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual([args for _, args in self.calls if args[:2] == ('plugin', 'add')], [])
        self.assertEqual((self.installed() / 'ordinary.txt').read_text(), 'second release bytes\n')
        self.assertTrue(self.installed().name.startswith('2.0.0+notes.'))

    def stopped_before_the_plugin_receipt(self):
        """Run a sync of the reviewed replacement that stops after it pointed the host at the plugin's new folder and
        before it saved the plugin's receipt, as a closed terminal or a full disk stops one."""
        from accepted_installations import AcceptedInstallations
        save = AcceptedInstallations._save

        def stopped(owner, artifact, accepted, pending=None):
            if artifact.kind == 'plugin':
                raise RuntimeError('stopped before the receipt')
            return save(owner, artifact, accepted, pending)

        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        with patch.object(AcceptedInstallations, '_save', stopped), \
                self.assertRaisesRegex(RuntimeError, 'stopped before the receipt'):
            self.run_apply()

    def test_a_sync_stopped_after_the_new_folder_appeared_accepts_it_without_another(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        self.stopped_before_the_plugin_receipt()
        written = self.installed()
        folders = sorted(written.parent.iterdir())
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((self.installed(), sorted(written.parent.iterdir())), (written, folders))
        self.assertEqual(json.loads(self.receipt_of('plugin:').read_bytes())['accepted']['path'], str(written))

    def test_a_folder_a_stopped_sync_left_codex_loading_is_the_baseline_for_a_newer_release(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        first = self.installed()
        self.stopped_before_the_plugin_receipt()
        written = self.installed()
        self.assertNotEqual(written, first)
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the second replacement'),))
        current = plan(self.setup, self.device)
        self.assertEqual((current.exit_code, current.blocked), (1, ()))
        self.assertEqual(self.run_apply(current).exit_code, 0)
        self.assertIn('use the second replacement', (self.installed() / 'skills/one/SKILL.md').read_text())
        self.assertIn('use the reviewed replacement', (written / 'skills/one/SKILL.md').read_text())
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_a_folder_a_stopped_sync_left_claude_loading_is_the_baseline_for_a_newer_release(self):
        first = self._installed_claude()
        self.stopped_before_the_plugin_receipt()
        written = self.claude_loads()
        self.assertNotEqual(written, first)
        # A session that started on the new folder holds a lease there, which its image leaves out.
        own_lease(written / '.in_use')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the second replacement'),))
        current = plan(self.setup, self.device)
        self.assertEqual((current.exit_code, current.blocked), (1, ()))
        self.assertEqual(self.run_apply(current).exit_code, 0)
        self.assertIn('use the second replacement', (self.claude_loads() / 'skills/one/SKILL.md').read_text())
        self.assertIn('use the reviewed replacement', (written / 'skills/one/SKILL.md').read_text())
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_a_folder_a_stopped_sync_left_loaded_and_then_edited_blocks_a_newer_release(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        self.stopped_before_the_plugin_receipt()
        written = self.installed()
        (written / 'ordinary.txt').write_text('edited after the stopped sync\n')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the second replacement'),))
        with self.assertRaisesRegex(ConcurrentEdit, 'Unverified existing content preserved: plugin:codex:kit@personal;'):
            self.run_apply()
        self.assertEqual((self.installed(), (written / 'ordinary.txt').read_text()),
                         (written, 'edited after the stopped sync\n'))

    def test_a_claude_session_marker_in_the_folder_an_earlier_engine_moved_aside_does_not_block(self):
        release, _ = self.legacy_claude_swap_aside()
        release.mkdir()
        lease = own_lease(release / '.in_use')
        current = plan(self.setup, self.device)
        self.assertEqual((current.exit_code, current.blocked), (1, ()))
        self.apply_reporting(current)
        self.assertNotEqual(self.claude_loads(), release)
        self.assertTrue(lease.exists())
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)

    def test_a_claude_record_repointed_during_the_sync_stops_it_before_any_write(self):
        first = self._installed_claude()
        release = first.parent / '1.0.0'
        read = accepted_installations.contents

        def repointed(path):
            if path == claude_records(self.roots):
                self.point_claude_at(release)
            return read(path)

        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        names = sorted(path.name for path in first.parent.iterdir())
        with patch('accepted_installations.contents', repointed), self.assertRaisesRegex(
                ConcurrentEdit, f"^Claude Code's install record does not name {re.escape(str(first))} for plugin:claude:"):
            self.run_apply()
        self.assertEqual(sorted(path.name for path in first.parent.iterdir()), names)
        self.assertEqual(self.claude_loads(), release)

    def test_a_claude_change_points_the_record_at_a_new_folder_and_leaves_the_running_one_alone(self):
        first = self._installed_claude()
        lease = own_lease(first / '.in_use')
        data = json.loads(claude_records(self.roots).read_text())
        project = {'scope': 'project', 'projectPath': str(self.root / 'project'), 'installPath': str(first),
                   'version': '1.0.0'}
        data['plugins'][self.claude_plugin].append(project)
        claude_records(self.roots).write_text(json.dumps(data))
        entry = claude_entry(self.roots, self.claude_plugin)
        before = self.state_of(first)
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        result = self.run_apply()
        self.assertEqual((result.exit_code, result.held), (0, ()))
        folder = self.claude_loads()
        self.assertEqual((folder.parent, folder.name.startswith('1.0.0-notes.')), (first.parent, True))
        self.assertIn('use the reviewed replacement', (folder / 'skills/one/SKILL.md').read_text())
        self.assertEqual(json.loads(claude_records(self.roots).read_text())['plugins'][self.claude_plugin],
                         [{**entry, 'installPath': str(folder)}, project])
        self.assertEqual(self.state_of(first), before)
        self.assertTrue(lease.exists())
        self.assertEqual(self.calls, [])
        unchanged = self.snapshot()
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(self.snapshot(), unchanged)

    def test_a_claude_record_changed_while_the_folder_is_written_stays_as_changed_until_the_next_run(self):
        from accepted_installations import AcceptedInstallations
        first = self._installed_claude()
        write = AcceptedInstallations._write_folder

        def meanwhile(owner, prepared):
            folder = write(owner, prepared)
            data = json.loads(claude_records(self.roots).read_text())
            data['plugins'][self.claude_plugin][0]['lastUpdated'] = '2026-11-14T19:00:00.000Z'
            claude_records(self.roots).write_text(json.dumps(data))
            return folder

        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        with patch.object(AcceptedInstallations, '_write_folder', meanwhile), self.assertRaisesRegex(
                ConcurrentEdit, f'^Changed during sync: {re.escape(str(claude_records(self.roots)))}$'):
            self.run_apply()
        self.assertEqual(self.claude_loads(), first)
        self.assertEqual(claude_entry(self.roots, self.claude_plugin)['lastUpdated'], '2026-11-14T19:00:00.000Z')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertIn('use the reviewed replacement', (self.claude_loads() / 'skills/one/SKILL.md').read_text())
        self.assertEqual(claude_entry(self.roots, self.claude_plugin)['lastUpdated'], '2026-11-14T19:00:00.000Z')

    def test_an_upstream_release_on_claude_is_updated_natively_once_and_gets_a_folder_of_its_own(self):
        first = self._installed_claude()
        self.release_version('2.0.0', 'second release bytes\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual([args[:2] for _, args in self.calls if args[:2] in (('plugin', 'install'), ('plugin', 'update'))],
                         [('plugin', 'update')])
        folder = self.claude_loads()
        self.assertEqual((folder.parent, folder.name.startswith('2.0.0-notes.')), (first.parent, True))
        self.assertEqual((folder / 'ordinary.txt').read_text(), 'second release bytes\n')
        self.assertEqual(claude_entry(self.roots, self.claude_plugin)['version'], '2.0.0')
        self.assertTrue(first.is_dir())
        self.calls.clear()
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual(self.calls, [])

    def test_a_claude_record_that_is_not_json_stops_the_sync_without_writes(self):
        self._installed_claude()
        claude_records(self.roots).write_text('{not json')
        self.setup = replace(self.setup, note_rules=(NoteRule('Read', 'use the reviewed replacement'),))
        before = self.snapshot()
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 3)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertEqual(self.snapshot(), before)

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
        receipt = self._claude_receipt()
        receipt.unlink()
        self.assertEqual(self.run_apply().exit_code, 0)
        accepted = json.loads(receipt.read_bytes())['accepted']['image']['entries']
        self.assertNotIn('.in_use', accepted)
        self.assertNotIn('.orphaned_at', accepted)
        self.assertEqual((leases / '17').read_text(), '{"pid":17,"procStart":"Thu Oct 15 12:30:00 2026"}')
        self.assertEqual((destination / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertEqual(self.calls, [])

    def test_claude_session_markers_read_as_unchanged_content_whatever_they_hold(self):
        destination = self._installed_claude()
        # Claude Code writes a lease as <pid>.tmp.<8 hex> and renames it, so a sync can meet one empty or part-written,
        # as a finish on WSL did on 14 November 2026.
        cases = [('.in_use/2512477.tmp.45e56d19', b''), ('.in_use/2512477.tmp.45e56d19', b'{"pid":2512'),
                 ('.in_use/17', b'not json'), ('.orphaned_at', b'not a time')]
        for name, data in cases:
            with self.subTest(name=name, data=data):
                entry = destination / name
                entry.parent.mkdir(parents=True, exist_ok=True)
                entry.write_bytes(data)
                before = self.snapshot()
                self.assertEqual(plan(self.setup, self.device).exit_code, 0)
                self.assertEqual(self.run_apply().exit_code, 0)
                self.assertEqual((self.snapshot(), self.calls), (before, []))
                top = destination / Path(name).parts[0]
                shutil.rmtree(top) if top.is_dir() else top.unlink()

    def stopped_between_the_moves(self):
        rename = Path.rename

        def crash(path, target):
            if path.name == 'staged':
                raise RuntimeError('between the moves')
            return rename(path, target)

        with patch.object(Path, 'rename', crash), self.assertRaisesRegex(RuntimeError, 'between the moves'):
            self.run_apply()

    def test_a_run_that_resumes_a_swap_and_cannot_save_its_receipt_puts_the_old_tree_back(self):
        from accepted_installations import AcceptedInstallations
        self.assertEqual(self.run_apply().exit_code, 0)
        installed = self.roots.codex_home / 'skills/loose'
        self.revise_loose('A second loose skill.\n')
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
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A loose skill.\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A second loose skill.\n')

    def test_an_old_tree_edited_after_it_was_moved_aside_is_not_put_back(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        installed = self.roots.codex_home / 'skills/loose'
        rename = os.rename

        def refuse(source, target, *args, **kwargs):
            if Path(source).name == 'staged':
                (Path(source).parent / 'displaced/SKILL.md').write_text('edited after the move aside\n')
                raise PermissionError(13, 'Access is denied', str(target))
            return rename(source, target, *args, **kwargs)

        self.revise_loose('A second loose skill.\n')
        with patch('os.rename', refuse), patch('sys.stderr', new_callable=io.StringIO) as stderr, \
                self.assertRaises(PermissionError):
            self.run_apply()
        self.assertFalse(installed.exists())
        displaced = Path(self.pending_of('codex:LOOSE_SKILL')['displaced'])
        self.assertEqual((displaced / 'SKILL.md').read_text(), 'edited after the move aside\n')
        self.assertEqual(stderr.getvalue(),
                         f'Old tree left aside: {displaced}, from {installed}: it changed after it was moved aside\n')

    def test_an_old_tree_that_cannot_be_put_back_is_named_and_the_next_run_finishes_the_swap(self):
        self.assertEqual(self.run_apply().exit_code, 0)
        installed = self.roots.codex_home / 'skills/loose'
        rename = os.rename

        def refuse(source, target, *args, **kwargs):
            if Path(source).name in ('staged', 'displaced'):
                raise PermissionError(13, 'Access is denied', str(target))
            return rename(source, target, *args, **kwargs)

        self.revise_loose('A second loose skill.\n')
        with patch('os.rename', refuse), patch('sys.stderr', new_callable=io.StringIO) as stderr, \
                self.assertRaises(PermissionError):
            self.run_apply()
        self.assertFalse(installed.exists())
        displaced = Path(self.pending_of('codex:LOOSE_SKILL')['displaced'])
        self.assertEqual(stderr.getvalue(), f'Old tree left aside: {displaced}, from {installed}: '
                                            f'[Errno 13] Access is denied: {str(installed)!r}\n')
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertEqual((installed / 'SKILL.md').read_text(), 'A second loose skill.\n')

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

    def test_claude_historical_receipt_metadata_is_not_silently_normalized(self):
        destination = self._installed_claude()
        (destination / '.in_use').mkdir()
        receipt = self._claude_receipt()
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

    def legacy_missing_cache_gets_a_folder_of_its_own(self, **kit):
        """A first run over an earlier engine's replacement that stopped with the folder Claude Code's record names
        moved aside: the skill reads of that missing folder wait, the plugin gets a folder of its own without a native
        install, the record names it, and the earlier engine's work folder stays as it was."""
        release, work = self.legacy_claude_swap_aside(**kit)
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 1, [(d.kind.value, d.key, d.detail) for d in current.result.differences])
        self.assertEqual([d.detail for d in current.changes],
                         [f'plugin:claude:{self.claude_plugin}: Write a folder of its own beside the replacement an '
                          'earlier engine left unfinished'])
        self.assertEqual(len(current.observations[Host.CLAUDE].unknown), 1)
        self.assertEqual(self.apply_reporting(current),
                         f'Work folders left in place: {work}: an earlier engine replaced '
                         f'plugin:claude:{self.claude_plugin} in place there, and this one leaves those folders as they '
                         'are\n')
        self.assertEqual(self.calls, [])
        folder = self.claude_loads()
        self.assertEqual((folder.parent, folder.name.startswith('1.0.0-notes.')), (release.parent, True))
        self.assertEqual((folder / 'ordinary.txt').read_text(), 'ordinary release bytes\n')
        self.assertEqual(sorted(path.name for path in work.iterdir()), ['displaced', 'staged'])
        self.assertEqual(plan(self.setup, self.device).exit_code, 0)
        return folder

    def test_aligned_claude_missing_legacy_cache_accepts_the_loaded_build_and_preserves_pending_evidence(self):
        cache, work = self.legacy_claude_swap_aside(aligned=True)
        previous = json.loads(self._claude_receipt().read_bytes())
        before = self.state_of(work), claude_records(self.roots).read_bytes()
        current = plan(self.setup, self.device)
        self.assertEqual((current.exit_code, current.observations[Host.CLAUDE].unknown), (1, ()))
        self.assertEqual(self.run_apply(current).exit_code, 0)
        self.assertFalse(cache.exists())
        self.assertEqual((self.state_of(work), claude_records(self.roots).read_bytes()), before)
        accepted = json.loads(self._claude_receipt().read_bytes())['accepted']
        self.assertEqual(accepted['legacy'], previous)
        self.assertEqual(accepted['path'], str(self.claude_loads()))
        (self.claude_loads() / '.claude-plugin/codex-alignment.json').write_text('{bad json')
        self.assertEqual(plan(self.setup, self.device).exit_code, 3)

    def test_aligned_claude_implicit_skills_load_without_a_cache(self):
        self._claude_kit(aligned=True, implicit=True)
        self.assertEqual(self.run_apply().exit_code, 0)
        self.assertNotIn('disable-model-invocation', (self.claude_loads() / 'skills/one/SKILL.md').read_text())
        self.assertFalse((self.roots.claude_home / 'plugins/cache').exists())

    def test_a_claude_cache_an_earlier_engine_left_moved_aside_gets_a_folder_of_its_own(self):
        self.legacy_missing_cache_gets_a_folder_of_its_own()

    def test_claude_missing_cache_without_pending_does_not_defer_skill_error(self):
        self.legacy_claude_swap_aside()
        receipt = self._claude_receipt()
        receipt.rename(receipt.with_suffix('.retained'))
        before = self.snapshot()
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 3)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertEqual(self.snapshot(), before)

    def test_a_missing_folder_that_claude_code_s_record_names_beside_another_s_stopped_replacement_blocks(self):
        release, _ = self.legacy_claude_swap_aside()
        self.point_claude_at(release.with_name('0.9.0'))
        before = self.snapshot()
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 3)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertEqual(self.snapshot(), before)

    def test_a_missing_folder_that_claude_code_s_record_names_without_a_pending_replacement_blocks(self):
        first = self._installed_claude()
        release = first.parent / '1.0.0'
        shutil.rmtree(release)
        self.point_claude_at(release)
        before = self.snapshot()
        current = plan(self.setup, self.device)
        self.assertEqual(current.exit_code, 3)
        with self.assertRaises(RuntimeError):
            self.run_apply(current)
        self.assertEqual(self.snapshot(), before)

    def test_pending_claude_cache_other_skill_unread_still_blocks(self):
        from claude_host import ClaudeState

        destination, _ = self.legacy_claude_swap_aside()
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
        destination, _ = self.legacy_claude_swap_aside(aligned=True, implicit=True)
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
        destination, _ = self.legacy_claude_swap_aside()
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


if __name__ == '__main__':
    unittest.main()
