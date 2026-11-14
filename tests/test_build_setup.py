"""build_setup.py rebuilds each package an install manages. With --unmanaged it also builds each package that no install
manages and that Codex installs through the engine, prints its digests, and checks a tree the lock holds. A package
whose build fails is reported for that package, whatever the fault."""
import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_setup  # noqa: E402
from description import (Adaptation, AlignedInstall, BuildSource, ClaudePlacement, CodexPlacement, EngineInstall,  # noqa: E402
                         GenericRecipe, Lock, LockedTree, MarketplaceInstall, Package, PluginRoles, Roots, Setup, ToolNotes, Visibility,
                         intent)
from host_state import DeviceRoots, Digest, Host, Platform  # noqa: E402
from portable import GIT  # noqa: E402
from homes import isolate_git  # noqa: E402


def setUpModule():
    isolate_git()


URL = 'https://example.invalid/kit.git'


@contextlib.contextmanager
def process_stderr():
    """What this process and its subprocesses write to file descriptor 2, which redirect_stderr does not reach."""
    with tempfile.TemporaryFile() as sink:
        saved = os.dup(2)
        os.dup2(sink.fileno(), 2)
        written = io.StringIO()
        try:
            yield written
        finally:
            os.dup2(saved, 2)
            os.close(saved)
            sink.seek(0)
            written.write(sink.read().decode('utf-8', 'replace').replace('\r\n', '\n'))


class UnmanagedBuild(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        upstream = self.seed = self.root / 'upstream'
        (upstream / 'skills/one').mkdir(parents=True)
        (upstream / 'plugin.json').write_text(json.dumps({'version': '1.0.0', 'author': {'name': 'Upstream'}}), newline='\n')
        (upstream / 'authorless.json').write_text(json.dumps({'version': '1.0.0'}), newline='\n')
        (upstream / 'skills/one/SKILL.md').write_text('---\nname: one\ndescription: One.\n---\nBody.\n', newline='\n')
        (upstream / 'agents').mkdir()
        (upstream / 'agents/helper.toml').write_text('name = "helper"\ndescription = "Helps."\n', newline='\n')
        for arguments in (('init', '-q'), ('remote', 'add', 'origin', URL), ('add', '.'),
                          ('-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'Fixture')):
            subprocess.run([*GIT, '-C', str(upstream), *arguments], check=True, capture_output=True)
        commit = subprocess.run([*GIT, '-C', str(upstream), 'rev-parse', 'HEAD'], check=True, capture_output=True,
                                text=True).stdout.strip()
        self.version = f'1.0.0+upstream.{commit[:12]}.codex1'
        self.commit = commit
        self.package = self.kit(None, CodexPlacement((EngineInstall(False),)), ClaudePlacement((EngineInstall(False),)))

    def kit(self, subdirectory, codex, claude, *, manifest='plugin.json', select=(), url=URL):
        source = BuildSource(GenericRecipe(url, self.commit, subdirectory, manifest, select), 1, 'Kit',
                             metadata={'url': 'https://example.invalid/kit', 'claudeDescriptions': {'kit': 'Kit.'}})
        return Package('kit', Visibility.PUBLIC, '', source, {}, ToolNotes.AUDIT, codex, claude)

    def run_build(self, output, *flags, trees=None, roles=None, relative=False, platform='linux'):
        """With relative, the output and seed paths are given relative to the folder the test runs in. With no
        platform, build_setup.py picks the one it runs on."""
        setup = Setup(Roots(self.root / 'public/setup', None), {'kit': self.package}, {}, {}, {}, None, (), frozenset(),
                      {'kit': Lock(intent(self.package, {}), trees or {}, roles or {})})
        paths = (output, self.seed.relative_to(self.root)) if relative else (self.root / output, self.seed)
        argv = ['build_setup.py', '--public', '/public', '--private', '/private', '--output', str(paths[0]),
                '--seed', str(paths[1]), *(['--platform', platform] if platform else []), *flags]
        with mock.patch('build_setup.load', return_value=setup) as load, mock.patch('sys.argv', argv), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = build_setup.main()
        load.assert_called_once_with(Roots(Path('/public'), Path('/private')))
        return code, out.getvalue().splitlines()

    def test_without_the_option_an_unmanaged_package_is_skipped(self):
        self.assertEqual(self.run_build('skipped'), (0, ['0 packages rebuilt and verified; 0 failed. '
                                                         'No host configuration or installed cache changed.']))
        self.assertFalse((self.root / 'skipped/kit').exists())

    def test_a_package_only_claude_manages_is_rebuilt_and_verified_without_the_option(self):
        _, [codex, claude, _] = self.run_build('unmanaged', '--unmanaged')
        trees = {host: LockedTree(*line.split()[-2:]) for host, line in ((Host.CODEX, codex), (Host.CLAUDE, claude))}
        self.package = self.kit(None, CodexPlacement((EngineInstall(False),)), ClaudePlacement((EngineInstall(True),)))
        self.assertEqual(self.run_build('claude-only', trees=trees), (0, [
            'verified kit: Codex tree, 1 Claude tree(s), 0 role(s)',
            '1 packages rebuilt and verified; 0 failed. No host configuration or installed cache changed.']))

    def test_a_managed_codex_package_has_its_agent_roles_checked_against_the_lock(self):
        _, [codex, claude, _] = self.run_build('unmanaged', '--unmanaged')
        trees = {host: LockedTree(*line.split()[-2:]) for host, line in ((Host.CODEX, codex), (Host.CLAUDE, claude))}
        for codex_on, built in ((True, ['FAILED kit: kit: rebuilt agent roles differ from the lock']),
                                (False, ['verified kit: Codex tree, 1 Claude tree(s), 0 role(s)'])):
            with self.subTest(codex_on=codex_on):
                self.package = self.kit(None, CodexPlacement((EngineInstall(codex_on),), roles=PluginRoles('agents')),
                                        ClaudePlacement((EngineInstall(True),)))
                code, lines = self.run_build(f'roles-{codex_on}', trees=trees,
                                             roles={'helper.toml': Digest('sha256:' + '0' * 64)})
                self.assertEqual((code, lines[:-1]), (int(codex_on), built))

    def test_without_a_platform_option_it_builds_for_the_platform_it_runs_on(self):
        _, [codex, claude, _] = self.run_build('unmanaged', '--unmanaged')
        trees = {host: LockedTree(*line.split()[-2:]) for host, line in ((Host.CODEX, codex), (Host.CLAUDE, claude))}
        self.package = replace(self.kit(None, CodexPlacement((EngineInstall(True),)), ClaudePlacement((EngineInstall(True),))),
                               platforms=frozenset({Platform.MACOS}))
        for platform, built in ((Platform.MACOS, ['verified kit: Codex tree, 1 Claude tree(s), 0 role(s)']),
                                (Platform.LINUX, [])):
            with self.subTest(platform=platform), mock.patch.object(Platform, 'native', return_value=platform):
                self.assertEqual(self.run_build(f'native-{platform}', trees=trees, platform=None),
                                 (0, built + [f'{len(built)} packages rebuilt and verified; 0 failed. '
                                              'No host configuration or installed cache changed.']))

    def test_the_option_builds_it_and_two_runs_print_the_same_digest(self):
        first, second = self.run_build('first', '--unmanaged'), self.run_build('second', '--unmanaged')
        self.assertEqual(first, second)
        code, [codex, claude, summary] = first
        self.assertEqual(code, 0)
        self.assertRegex(codex, rf'^built unmanaged kit codex: {re.escape(self.version)} sha256:[0-9a-f]{{64}}$')
        self.assertRegex(claude, rf'^built unmanaged kit kit@personal-collections: {re.escape(self.version)}[.]claude1 '
                                 r'sha256:[0-9a-f]{64}$')
        self.assertEqual(summary, '0 packages rebuilt and verified; 1 unmanaged packages built; 0 failed. '
                                  'No host configuration or installed cache changed.')
        self.assertTrue((self.root / 'first/kit' / self.version / 'skills/one/SKILL.md').is_file())
        digest = replace(DeviceRoots.current(), engine=self.root / 'first').tree_digest
        self.assertEqual(codex.split()[-1], digest(self.root / 'first/kit' / self.version))
        self.assertEqual(claude.split()[-1], digest(self.root / 'first/claude/plugins/kit' / (self.version + '.claude1')))
        self.assertNotEqual(codex.split()[-1], claude.split()[-1])

    def test_the_option_switches_on_engine_and_aligned_installs_and_leaves_a_marketplace_install_off(self):
        self.package = self.kit(None, CodexPlacement((MarketplaceInstall('upstream', False), EngineInstall(False))),
                                ClaudePlacement((AlignedInstall(False), MarketplaceInstall('upstream', False))))
        code, lines = self.run_build('aligned', '--unmanaged')
        self.assertEqual(code, 0, lines)
        self.assertEqual([line.split(':')[0] for line in lines[:-1]],
                         ['built unmanaged kit codex', 'built unmanaged kit kit@codex-aligned'])
        version, digest = lines[1].split()[-2:]
        engine = replace(DeviceRoots.current(), engine=self.root / 'aligned')
        self.assertEqual(digest, engine.tree_digest(self.root / 'aligned/.sync/claude-aligned/plugins/kit' / version))

    def test_a_tree_the_lock_holds_is_still_verified(self):
        locked = Digest('sha256:' + '0' * 64)
        for host, version, identity in ((Host.CODEX, self.version, 'kit'),
                                        (Host.CLAUDE, self.version + '.claude1', 'kit@personal-collections')):
            with self.subTest(host=host):
                code, lines = self.run_build(f'locked-{host.value}', '--unmanaged', trees={host: LockedTree(version, locked)})
                self.assertEqual(code, 1)
                self.assertRegex(lines[0], rf'^FAILED kit: {identity}: rebuilt tree sha256:[0-9a-f]{{64}} differs from '
                                           rf'lock {locked}$')

    def test_a_codex_aligned_copy_is_built_at_the_version_its_lock_names(self):
        self.package = self.kit(None, CodexPlacement((EngineInstall(False),)), ClaudePlacement((AlignedInstall(False),)))
        locked = '1.0.0+codex-aligned.000000000000'
        _, [failed, _] = self.run_build('lock-unknown', '--unmanaged', trees={Host.CLAUDE: LockedTree(locked, '-')})
        digest = re.search(r'rebuilt tree (\S+) differs', failed)[1]
        code, [_, aligned, _] = self.run_build('lock-known', '--unmanaged', trees={Host.CLAUDE: LockedTree(locked, digest)})
        self.assertEqual((code, aligned), (0, f'built unmanaged kit kit@codex-aligned: {locked} {digest}'))
        self.assertTrue((self.root / 'lock-known/.sync/claude-aligned/plugins/kit' / locked).is_dir())

    def test_the_option_skips_a_package_without_a_codex_engine_install(self):
        shapes = {'codex from a marketplace, claude aligned': (CodexPlacement((MarketplaceInstall('upstream', False),)),
                                                               ClaudePlacement((AlignedInstall(False),))),
                  'codex from a marketplace, claude engine': (CodexPlacement((MarketplaceInstall('upstream', False),)),
                                                              ClaudePlacement((EngineInstall(False),))),
                  'no codex, claude engine': (None, ClaudePlacement((EngineInstall(False),)))}
        for number, (shape, (codex, claude)) in enumerate(shapes.items()):
            with self.subTest(shape=shape):
                self.package = self.kit(None, codex, claude)
                self.assertEqual(self.run_build(f'skipped-{number}', '--unmanaged'),
                                 (0, ['0 packages rebuilt and verified; 0 unmanaged packages built; 0 failed. '
                                      'No host configuration or installed cache changed.']))
                self.assertFalse((self.root / f'skipped-{number}/kit').exists())

    def test_the_option_builds_only_the_packages_named(self):
        self.assertEqual(self.run_build('other', '--unmanaged', '--package', 'other'),
                         (0, ['0 packages rebuilt and verified; 0 unmanaged packages built; 0 failed. '
                              'No host configuration or installed cache changed.']))
        self.assertFalse((self.root / 'other/kit').exists())

    def test_a_package_whose_build_fails_is_reported_for_it_whatever_the_fault(self):
        cases = {'git': ({'subdirectory': 'absent'}, r'fatal: .+ \(exit \d+ from git .* archive .+\)', r'^$'),
                 'builder': ({'select': ('skills/absent',)}, r'Missing selected skill directory: skills/absent', r'^$'),
                 'recipe field': ({'manifest': 'authorless.json'}, r"KeyError\('author'\)",
                                  r'''^Traceback (?s:.+) in package\n(?s:.+)\nKeyError: 'author'\n$''')}
        for fault, (recipe, message, shown) in cases.items():
            with self.subTest(fault=fault):
                options = {'select': recipe.get('select', ()), 'manifest': recipe.get('manifest', 'plugin.json')}
                self.package = self.kit(recipe.get('subdirectory'), CodexPlacement((EngineInstall(False),)), None, **options)
                # Git translates its messages, "fatal:" included, unless the locale is C.
                with process_stderr() as stderr, mock.patch.dict(os.environ, {'LC_ALL': 'C'}):
                    code, [failed, summary] = self.run_build(f'failure-{fault}', '--unmanaged')
                self.assertRegex(stderr.getvalue(), shown)
                self.assertEqual(code, 1)
                self.assertRegex(failed, f'^FAILED kit: {message}$')
                self.assertEqual(summary, '0 packages rebuilt and verified; 0 unmanaged packages built; 1 failed. '
                                          'No host configuration or installed cache changed.')

    def test_relative_output_and_seed_paths_build_as_absolute_ones_do(self):
        # The adapted commit is on no branch of the upstream, so the adaptation's checkout fetches it from the seed or
        # from the source cache under the output folder.
        (self.seed / 'skills/one/SKILL.md').write_text('---\nname: one\ndescription: One.\n---\nAdapted.\n', newline='\n')
        for arguments in (('-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-qam', 'Adapted'),):
            subprocess.run([*GIT, '-C', str(self.seed), *arguments], check=True, capture_output=True)
        adapted = subprocess.run([*GIT, '-C', str(self.seed), 'rev-parse', 'HEAD'], check=True, capture_output=True,
                                 text=True).stdout.strip()
        subprocess.run([*GIT, '-C', str(self.seed), 'reset', '-q', '--hard', self.commit], check=True, capture_output=True)
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        for source, url in (('seeded', URL), ('fetched', self.seed.as_uri())):
            with self.subTest(source=source):
                self.package = self.kit(None, CodexPlacement((EngineInstall(False),),
                                                             adaptation=Adaptation(url, self.commit, adapted)),
                                        ClaudePlacement((AlignedInstall(False),)), url=url)
                os.chdir(here)
                absolute = self.run_build(f'absolute-{source}', '--unmanaged')
                os.chdir(self.root)
                self.assertEqual(self.run_build(f'relative-{source}', '--unmanaged', relative=True), absolute)
                self.assertEqual(absolute[0], 0, absolute)


if __name__ == '__main__':
    unittest.main()
