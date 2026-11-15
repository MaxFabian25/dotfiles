import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import old_builds  # noqa: E402

CATALOG, CACHE = old_builds.TREES
BOOT = 1_000_000.0


class SweepTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / '.claude'
        self.notes = Path(folder.name) / 'state/builds-out-of-use.json'
        self.offer('1.0.0+codex-aligned.bbbbbbbbbbbb')

    def build(self, tree, version):
        folder = self.home / tree / 'pstack' / version
        (folder / 'skills').mkdir(parents=True, exist_ok=True)
        (folder / 'skills/SKILL.md').write_text('text', encoding='utf-8')
        return folder

    def offer(self, version, projects=()):
        """The catalog offers this version, and the install record names its copy at user scope and each of
        projects at project scope, as after a sync that applied."""
        self.build(CATALOG, version)
        entries = [{'scope': 'user', 'installPath': str(self.build(CACHE, version.replace('+', '-')))}]
        entries += [{'scope': 'project', 'installPath': str(folder)} for folder in projects]
        for path, data in ((old_builds.CATALOG + '/.claude-plugin/marketplace.json',
                            {'plugins': [{'name': 'pstack', 'source': f'./plugins/pstack/{version}', 'version': version}]}),
                           ('plugins/installed_plugins.json', {'plugins': {'pstack@codex-aligned': entries}})):
            (self.home / path).parent.mkdir(parents=True, exist_ok=True)
            (self.home / path).write_text(json.dumps(data), encoding='utf-8')

    def sweep(self, now, readable=True):
        """A pull at now. The machine started at BOOT, and before that at a much earlier time."""
        started = BOOT if now > BOOT else BOOT - 5_000
        return old_builds.sweep(self.home, self.notes, now, started if readable else None)

    def test_a_replaced_catalog_build_goes_after_reboot_but_its_historical_cache_stays(self):
        old = [self.build(CATALOG, '1.0.0+codex-aligned.aaaaaaaaaaaa'), self.build(CACHE, '1.0.0-codex-aligned.aaaaaaaaaaaa')]
        self.assertEqual(self.sweep(now=BOOT - 50), [])
        self.assertEqual(self.sweep(now=BOOT + 50), old[:1])
        self.assertEqual([build.exists() for build in old], [False, True])
        self.assertEqual(json.loads(self.notes.read_text(encoding='utf-8')), {str(old[1]): BOOT - 50})

    def test_native_and_engine_named_historical_caches_are_retained_and_tracked(self):
        caches = [self.build(CACHE, '0.9.0'), self.build(CACHE, '0.9.0-notes.20261114T190000Z.aaaaaaaaaaaa')]
        self.sweep(now=BOOT - 50)
        self.assertEqual(self.sweep(now=BOOT + 50), [])
        self.assertTrue(all(path.exists() for path in caches))
        self.assertEqual(json.loads(self.notes.read_text(encoding='utf-8')),
                         {str(path): BOOT - 50 for path in caches})

    def test_catalog_retirement_needs_no_native_install_record(self):
        old = self.build(CATALOG, '0.9.0')
        (self.home / 'plugins/installed_plugins.json').unlink()
        self.sweep(now=BOOT - 50)
        self.assertEqual(self.sweep(now=BOOT + 50), [old])
        self.assertTrue((self.home / CATALOG / 'pstack/1.0.0+codex-aligned.bbbbbbbbbbbb').is_dir())

    def test_a_build_replaced_since_the_machine_started_stays_however_many_pulls_see_it(self):
        old = self.build(CACHE, '1.0.0-codex-aligned.aaaaaaaaaaaa')
        for now in (BOOT + 10, BOOT + 20, BOOT + 9_000_000):
            self.assertEqual(self.sweep(now), [])
        self.assertTrue(old.exists())
        self.assertEqual(json.loads(self.notes.read_text(encoding='utf-8')), {str(old): BOOT + 10})

    def test_a_build_first_seen_replaced_by_this_pull_stays_even_when_its_files_are_older_than_the_start(self):
        old = self.build(CACHE, '1.0.0-codex-aligned.aaaaaaaaaaaa')
        self.assertEqual(self.sweep(now=BOOT + 50), [])
        self.assertTrue(old.exists())

    def test_the_builds_a_new_session_can_load_stay(self):
        project = self.build(CACHE, '0.9.0-codex-aligned.cccccccccccc')
        self.offer('1.0.0+codex-aligned.bbbbbbbbbbbb', projects=[project])
        self.sweep(now=BOOT - 50)
        self.assertEqual(self.sweep(now=BOOT + 50), [])
        self.assertEqual(sorted(path.name for path in old_builds.builds(self.home)),
                         ['0.9.0-codex-aligned.cccccccccccc', '1.0.0+codex-aligned.bbbbbbbbbbbb',
                          '1.0.0-codex-aligned.bbbbbbbbbbbb'])

    def test_the_cache_copy_of_the_build_the_catalog_offers_stays_while_the_record_names_another_folder(self):
        copy = self.home / CACHE / 'pstack/1.0.0-codex-aligned.bbbbbbbbbbbb'
        upstream = self.build(CACHE, '1.0.0')
        (self.home / 'plugins/installed_plugins.json').write_text(json.dumps(
            {'plugins': {'pstack@codex-aligned': [{'scope': 'user', 'installPath': str(upstream)}]}}), encoding='utf-8')
        self.sweep(now=BOOT - 50)
        self.assertEqual(self.sweep(now=BOOT + 50), [])
        self.assertTrue(copy.exists() and upstream.exists())

    def test_a_build_that_is_current_again_is_noted_anew_when_it_is_next_replaced(self):
        first = self.build(CACHE, '1.0.0-codex-aligned.aaaaaaaaaaaa')
        self.sweep(now=BOOT - 50)
        self.offer('1.0.0+codex-aligned.aaaaaaaaaaaa')
        self.sweep(now=BOOT - 40)
        self.offer('1.0.0+codex-aligned.bbbbbbbbbbbb')
        self.assertEqual(self.sweep(now=BOOT + 50), [])
        self.assertTrue(first.exists())

    def test_nothing_goes_when_the_machines_start_cannot_be_read(self):
        old = self.build(CACHE, '1.0.0-codex-aligned.aaaaaaaaaaaa')
        self.sweep(now=BOOT - 50, readable=False)
        self.assertEqual(self.sweep(now=BOOT + 50, readable=False), [])
        self.assertTrue(old.exists())

    def test_nothing_goes_and_nothing_is_noted_when_the_catalog_or_the_record_cannot_be_read(self):
        old = self.build(CACHE, '1.0.0-codex-aligned.aaaaaaaaaaaa')
        self.sweep(now=BOOT - 50)
        noted = self.notes.read_bytes()
        for path, text in (('plugins/installed_plugins.json', '{"plugins": []}'),
                           (old_builds.CATALOG + '/.claude-plugin/marketplace.json', 'not JSON')):
            with self.subTest(path=path):
                before = (self.home / path).read_text(encoding='utf-8')
                (self.home / path).write_text(text, encoding='utf-8')
                self.assertEqual(self.sweep(now=BOOT + 50), [])
                (self.home / path).write_text(before, encoding='utf-8')
                self.assertTrue(old.exists())
                self.assertEqual(self.notes.read_bytes(), noted)

    def test_a_work_folder_of_a_build_in_progress_and_other_marketplaces_copies_stay(self):
        work = self.build(CATALOG, '.build-x1')
        other = self.home / 'plugins/cache/claude-plugins-official/skill-creator/1.0.0'
        other.mkdir(parents=True)
        self.sweep(now=BOOT - 50)
        self.assertEqual(self.sweep(now=BOOT + 50), [])
        self.assertTrue(work.exists() and other.exists())


class StartedTest(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt' or Path('/proc/stat').exists(), 'no start time to read here')
    def test_the_machine_started_before_now_and_within_the_last_three_years(self):
        started = old_builds.started()
        self.assertIsNotNone(started)
        self.assertLess(started, time.time())
        self.assertGreater(started, time.time() - 3 * 365 * 86400)


if __name__ == '__main__':
    unittest.main()
