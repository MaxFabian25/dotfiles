from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
import unittest

from description import Adaptation, CodexPlacement, EngineInstall, Package, Roots, Setup, ToolNotes, Visibility
from host_state import DeviceRoots, Host
from portable import GIT
from sync_sources import BuildError, Builder, Sources


def git(root, *arguments):
    return subprocess.run([*GIT, '-C', str(root), *arguments], check=True, capture_output=True,
                          text=True, timeout=30).stdout.strip()


class AdaptationAncestryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.upstream = self.root / 'upstream'
        self.upstream.mkdir()
        git(self.upstream, 'init', '-b', 'main')
        git(self.upstream, 'config', 'user.name', 'Test')
        git(self.upstream, 'config', 'user.email', 'test@example.invalid')
        self.base = self.commit('Base content.\n')
        self.middle = self.commit('Intermediate content.\n')
        self.adapted = self.commit('Adapted content.\n')
        self.commit('Moving branch content.\n')
        self.url = self.upstream.as_uri()
        self.sources = Sources(self.root / 'sources')
        self.roots = replace(DeviceRoots.current(self.root), engine=self.root / 'engine')
        self.checkout = self.roots.engine / 'harness-adaptations/kit'
        self.plugin = self.root / 'plugin'
        self.plugin.mkdir()
        (self.plugin / 'content.txt').write_bytes(b'Base content.\n')
        self.package = Package('kit', Visibility.PUBLIC, '', None, {}, ToolNotes.AUDIT,
                               CodexPlacement((EngineInstall(True),),
                                              adaptation=Adaptation(self.url, self.base, self.adapted)), None)
        self.setup = Setup(Roots(self.root / 'public', None), {'kit': self.package}, {}, {}, {},
                           None, (), frozenset(), {})

    def commit(self, content):
        (self.upstream / 'content.txt').write_bytes(content.encode())
        git(self.upstream, 'add', '.')
        git(self.upstream, 'commit', '-m', content.strip())
        return git(self.upstream, 'rev-parse', 'HEAD')

    def overlay(self):
        builder = Builder(self.setup, self.roots, self.roots.engine, sources=self.sources)
        return builder.overlay(self.package, Host.CODEX, 'kit@personal', self.plugin)

    def shallow_checkout(self):
        self.checkout.mkdir(parents=True)
        git(self.checkout, 'init')
        git(self.checkout, 'remote', 'add', 'origin', self.url)
        for commit in (self.base, self.adapted):
            git(self.checkout, 'fetch', '--depth=1', 'origin', commit)
        git(self.checkout, 'checkout', '--detach', self.adapted)

    def assert_adapted(self):
        self.assertEqual(self.overlay(), [(self.plugin / 'content.txt', 'Base content.\n',
                                          'Adapted content.\n', 'Codex', 'kit@personal')])
        self.assertEqual(git(self.checkout, 'rev-parse', 'HEAD'), self.adapted)
        self.assertEqual(git(self.checkout, 'merge-base', self.base, self.adapted), self.base)
        self.assertEqual((self.plugin / 'content.txt').read_text(), 'Base content.\n')

    def test_cold_cache_acquires_ancestry_of_exact_pins(self):
        self.assert_adapted()

    def test_disconnected_shallow_seed_is_read_only(self):
        seed = self.root / 'seed'
        seed.mkdir()
        git(seed, 'init', '--bare')
        git(seed, 'remote', 'add', 'origin', self.url)
        for commit in (self.base, self.adapted):
            git(seed, 'fetch', '--depth=1', 'origin', commit)
        before = {p.relative_to(seed): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in seed.rglob('*') if p.is_file()}
        self.sources = Sources(self.root / 'sources', seeds=(seed,))
        self.assert_adapted()
        self.assertEqual({p.relative_to(seed): (p.read_bytes(), p.stat().st_mtime_ns)
                          for p in seed.rglob('*') if p.is_file()}, before)
        self.assertEqual(git(seed, 'rev-parse', '--is-shallow-repository'), 'true')

    def test_retry_repairs_existing_shallow_cache_and_incomplete_checkout(self):
        self.sources.repository(self.url, self.base)
        self.sources.checkout(self.url, self.adapted, self.checkout)
        self.assertEqual(git(self.checkout, 'rev-parse', '--is-shallow-repository'), 'false')
        with self.assertRaises(subprocess.CalledProcessError):
            git(self.checkout, 'merge-base', self.base, self.adapted)
        self.assert_adapted()
        self.assertEqual(git(self.checkout, 'rev-parse', '--is-shallow-repository'), 'false')
        self.assert_adapted()

    def test_retry_repairs_existing_shallow_cache_and_checkout(self):
        self.sources.repository(self.url, self.base)
        self.sources.repository(self.url, self.adapted)
        self.shallow_checkout()
        self.assert_adapted()
        self.assertEqual(git(self.checkout, 'rev-parse', '--is-shallow-repository'), 'false')

    def test_retry_repairs_an_incomplete_nonshallow_source_cache(self):
        cache = self.sources.repository(self.url, self.base)
        self.sources.checkout(self.url, self.adapted, self.checkout)
        cache.rename(self.root / 'old-cache')
        git(self.root, 'clone', '--bare', str(self.checkout), str(cache))
        git(cache, 'remote', 'set-url', 'origin', self.url)
        self.assertEqual(git(cache, 'rev-parse', '--is-shallow-repository'), 'false')
        with self.assertRaises(subprocess.CalledProcessError):
            git(cache, 'rev-list', '--count', self.adapted)
        self.assert_adapted()
        self.assertEqual(git(cache, 'rev-list', '--count', self.adapted), '3')

    def test_existing_shallow_checkout_refreshes_from_a_complete_seed(self):
        self.shallow_checkout()
        git(self.upstream, 'remote', 'add', 'origin', self.url)
        self.sources = Sources(self.root / 'sources', seeds=(self.upstream,))
        self.assert_adapted()
        self.assertEqual(git(self.checkout, 'rev-parse', '--is-shallow-repository'), 'false')

    def test_nonancestor_base_is_rejected_before_planning_overlay(self):
        git(self.upstream, 'checkout', '--orphan', 'unrelated')
        unrelated = self.commit('Unrelated content.\n')
        self.package = replace(self.package, codex=replace(
            self.package.codex, adaptation=Adaptation(self.url, unrelated, self.adapted)))
        with self.assertRaisesRegex(BuildError, 'base.*ancestor'):
            self.overlay()
        self.assertEqual((self.plugin / 'content.txt').read_text(), 'Base content.\n')

    def test_divergent_base_is_rejected_even_with_an_older_merge_base(self):
        git(self.upstream, 'checkout', '-b', 'divergent', self.base)
        divergent = self.commit('Divergent content.\n')
        self.assertEqual(git(self.upstream, 'merge-base', divergent, self.adapted), self.base)
        self.package = replace(self.package, codex=replace(
            self.package.codex, adaptation=Adaptation(self.url, divergent, self.adapted)))
        with self.assertRaisesRegex(BuildError, 'base.*ancestor'):
            self.overlay()
        self.assertEqual((self.plugin / 'content.txt').read_text(), 'Base content.\n')

    def test_dirty_shallow_checkout_is_retained(self):
        self.shallow_checkout()
        (self.checkout / 'content.txt').write_text('Local tracked edit.\n')
        (self.checkout / 'untracked.txt').write_text('Local untracked work.\n')
        shallow = (self.checkout / '.git/shallow').read_bytes()
        with self.assertRaisesRegex(BuildError, 'Checkout has local changes'):
            self.overlay()
        self.assertEqual((self.checkout / 'content.txt').read_text(), 'Local tracked edit.\n')
        self.assertEqual((self.checkout / 'untracked.txt').read_text(), 'Local untracked work.\n')
        self.assertEqual(git(self.checkout, 'rev-parse', 'HEAD'), self.adapted)
        self.assertEqual((self.checkout / '.git/shallow').read_bytes(), shallow)

    def test_ordinary_pinned_source_still_uses_a_shallow_tree(self):
        destination = self.sources.tree(self.url, self.adapted, self.root / 'ordinary')
        self.assertEqual((destination / 'content.txt').read_bytes(), b'Adapted content.\n')
        cache = self.sources.repository(self.url, self.adapted)
        self.assertEqual(git(cache, 'rev-parse', '--is-shallow-repository'), 'true')
        with self.assertRaises(subprocess.CalledProcessError):
            git(cache, 'cat-file', '-e', self.middle + '^{commit}')


if __name__ == '__main__':
    unittest.main()
