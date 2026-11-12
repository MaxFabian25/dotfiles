"""build_setup.py --unmanaged builds a package whose engine installs are off, prints its digest, and checks a tree the
lock holds. A failed git command in one package is reported for that package."""
import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_setup  # noqa: E402
from description import (AlignedInstall, BuildSource, ClaudePlacement, CodexPlacement, EngineInstall,  # noqa: E402
                         GenericRecipe, Lock, LockedTree, MarketplaceInstall, Package, Roots, Setup, ToolNotes, Visibility,
                         intent)
from host_state import DeviceRoots, Digest, Host  # noqa: E402
from portable import GIT  # noqa: E402

URL = 'https://example.invalid/kit.git'


class UnmanagedBuild(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        upstream = self.seed = self.root / 'upstream'
        (upstream / 'skills/one').mkdir(parents=True)
        (upstream / 'plugin.json').write_text(json.dumps({'version': '1.0.0', 'author': {'name': 'Upstream'}}))
        (upstream / 'skills/one/SKILL.md').write_text('---\nname: one\ndescription: One.\n---\nBody.\n')
        for arguments in (('init', '-q'), ('remote', 'add', 'origin', URL), ('add', '.'),
                          ('-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'Fixture')):
            subprocess.run([*GIT, '-C', str(upstream), *arguments], check=True, capture_output=True)
        commit = subprocess.run([*GIT, '-C', str(upstream), 'rev-parse', 'HEAD'], check=True, capture_output=True,
                                text=True).stdout.strip()
        self.version = f'1.0.0+upstream.{commit[:12]}.codex1'
        self.commit = commit
        self.package = self.kit(None, CodexPlacement((EngineInstall(False),)), ClaudePlacement((EngineInstall(False),)))

    def kit(self, subdirectory, codex, claude):
        source = BuildSource(GenericRecipe(URL, self.commit, subdirectory, 'plugin.json', ()), 1, 'Kit',
                             metadata={'url': 'https://example.invalid/kit', 'claudeDescriptions': {'kit': 'Kit.'}})
        return Package('kit', Visibility.PUBLIC, '', source, {}, ToolNotes.AUDIT, codex, claude)

    def run_build(self, output, *flags, trees=None):
        setup = Setup(Roots(self.root / 'public/setup', None), {'kit': self.package}, {}, {}, {}, None, (), frozenset(),
                      {'kit': Lock(intent(self.package, {}), trees or {})})
        argv = ['build_setup.py', '--public', '/public', '--output', str(self.root / output), '--seed', str(self.seed),
                '--platform', 'linux', *flags]
        with mock.patch('build_setup.load', return_value=setup), mock.patch('sys.argv', argv), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = build_setup.main()
        return code, out.getvalue().splitlines()

    def test_without_the_option_an_unmanaged_package_is_skipped(self):
        self.assertEqual(self.run_build('skipped'), (0, ['0 packages rebuilt and verified; 0 failed. '
                                                         'No host configuration or installed cache changed.']))
        self.assertFalse((self.root / 'skipped/kit').exists())

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
        self.package = self.kit(None, CodexPlacement((EngineInstall(False),)),
                                ClaudePlacement((AlignedInstall(False), MarketplaceInstall('upstream', False))))
        code, lines = self.run_build('aligned', '--unmanaged')
        self.assertEqual(code, 0, lines)
        self.assertEqual([line.split(':')[0] for line in lines[:-1]],
                         ['built unmanaged kit codex', 'built unmanaged kit kit@codex-aligned'])

    def test_a_tree_the_lock_holds_is_still_verified(self):
        locked = Digest('sha256:' + '0' * 64)
        code, lines = self.run_build('locked', '--unmanaged', trees={Host.CODEX: LockedTree(self.version, locked)})
        self.assertEqual(code, 1)
        self.assertRegex(lines[0], rf'^FAILED kit: kit: rebuilt tree sha256:[0-9a-f]{{64}} differs from lock {locked}$')

    def test_the_option_skips_a_package_that_codex_installs_only_from_a_host_marketplace(self):
        self.package = self.kit(None, CodexPlacement((MarketplaceInstall('upstream', False),)),
                                ClaudePlacement((AlignedInstall(False),)))
        self.assertEqual(self.run_build('marketplace', '--unmanaged'),
                         (0, ['0 packages rebuilt and verified; 0 unmanaged packages built; 0 failed. '
                              'No host configuration or installed cache changed.']))
        self.assertFalse((self.root / 'marketplace/kit').exists())

    def test_a_failed_git_command_is_reported_for_its_package(self):
        self.package = self.kit('absent', CodexPlacement((EngineInstall(False),)), None)
        code, [failed, summary] = self.run_build('git-failure', '--unmanaged')
        self.assertEqual(code, 1)
        self.assertRegex(failed, r"^FAILED kit: Command .*'archive'.* returned non-zero exit status \d+\.$")
        self.assertEqual(summary, '0 packages rebuilt and verified; 0 unmanaged packages built; 1 failed. '
                                  'No host configuration or installed cache changed.')


if __name__ == '__main__':
    unittest.main()
