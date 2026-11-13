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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_setup  # noqa: E402
from description import (AlignedInstall, BuildSource, ClaudePlacement, CodexPlacement, EngineInstall,  # noqa: E402
                         GenericRecipe, Lock, LockedTree, Package, Roots, Setup, ToolNotes, Visibility, WindowsReplace,
                         intent)
from homes import isolate_git  # noqa: E402
from host_state import DeviceRoots, Host, Platform, SkillMode  # noqa: E402
import lock_setup  # noqa: E402
from portable import GIT  # noqa: E402
from sync_sources import Builder, Sources  # noqa: E402


def setUpModule():
    isolate_git()


URL = 'https://example.invalid/kit.git'
SKILLS = {'one': '---\nname: one\ndescription: One.\n---\nThen run [two](../two/SKILL.md).\n',
          'two': '---\nname: two\ndescription: Two.\n---\nBody.\n',
          'bad': '---\nname: bad\ndescription: Bad.\n---\nThen run [absent](../absent/SKILL.md).\n',
          'odd': '---\nname: Odd_Name\ndescription: Odd.\n---\nBody.\n'}
DESCRIPTIONS = {'kit': 'Kit.', 'kit-core': 'Core.', 'kit-extra': 'Extra.'}


class OverlayAtRelease(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.seed = self.root / 'upstream'
        self.seed.mkdir()
        (self.seed / 'plugin.json').write_text(json.dumps({'version': '1.0.0', 'author': {'name': 'Upstream'}}),
                                               newline='\n')
        for name, text in SKILLS.items():
            (self.seed / 'skills' / name).mkdir(parents=True)
            (self.seed / 'skills' / name / 'SKILL.md').write_text(text, newline='\n')
        for arguments in (('init', '-q'), ('remote', 'add', 'origin', URL), ('add', '.'),
                          ('-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'Fixture')):
            subprocess.run([*GIT, '-C', str(self.seed), *arguments], check=True, capture_output=True)
        self.commit = subprocess.run([*GIT, '-C', str(self.seed), 'rev-parse', 'HEAD'], check=True,
                                     capture_output=True, text=True).stdout.strip()

    def kit(self, select, codex, claude=None, *, modes=None, platforms=frozenset()):
        recipe = GenericRecipe(URL, self.commit, None, 'plugin.json', tuple(f'skills/{name}' for name in select))
        source = BuildSource(recipe, 1, 'Kit', metadata={'url': 'https://example.invalid/kit',
                                                         'claudeDescriptions': DESCRIPTIONS})
        return Package('kit', Visibility.PUBLIC, '', source, modes or {}, ToolNotes.AUDIT, codex, claude, platforms)

    def setup(self, package, lock=None):
        return Setup(Roots(self.root / 'public/setup', None), {'kit': package}, {}, {}, {}, None, (), frozenset(),
                     {'kit': lock} if lock else {})

    def builder(self, setup):
        output = Path(tempfile.mkdtemp(dir=self.root))
        return Builder(setup, replace(DeviceRoots.current(), engine=output), output,
                       sources=Sources(output / '.sync/sources', seeds=(self.seed,)))

    def refresh(self, package):
        setup = self.setup(package)
        return lock_setup.refresh(setup, self.builder(setup), ['kit'])

    def build_with_unchecked_lock(self, package, *flags):
        builder = self.builder(self.setup(package))
        codex = builder.codex(package, verify=False)
        trees = {Host.CODEX: LockedTree(codex.version, codex.digest),
                 **{Host.CLAUDE: LockedTree(tree.version, tree.digest)
                    for tree in builder.claude(package, codex, verify=False).values()}}
        setup = self.setup(package, Lock(intent(package, {}), trees))
        argv = ['build_setup.py', '--public', '/public', '--output', tempfile.mkdtemp(dir=self.root),
                '--seed', str(self.seed), '--platform', 'linux', *flags]
        with mock.patch('build_setup.load', return_value=setup), mock.patch('sys.argv', argv), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = build_setup.main()
        return code, out.getvalue().splitlines()

    def assert_refused(self, package, named):
        with self.assertRaises(ValueError) as refusal:
            self.refresh(package)
        self.assertIn(named, str(refusal.exception))
        code, lines = self.build_with_unchecked_lock(package)
        self.assertEqual(code, 1, lines)
        self.assertRegex(lines[0], rf'^FAILED kit: .*{re.escape(named)}')

    def assert_released(self, package):
        self.assertIn(Host.CODEX, self.refresh(package).lock['kit'].trees)
        code, lines = self.build_with_unchecked_lock(package)
        self.assertEqual(code, 0, lines)
        self.assertRegex(lines[0], r'^verified kit: ')

    def test_a_link_to_a_skill_the_codex_plugin_lacks_stops_the_release(self):
        self.assert_refused(self.kit(('bad', 'two'), CodexPlacement((EngineInstall(True),))), '../absent/SKILL.md')
        self.assert_refused(self.kit(('bad', 'two'), CodexPlacement((EngineInstall(False, keep_current=True),))),
                            '../absent/SKILL.md')
        self.assert_released(self.kit(('one', 'two'), CodexPlacement((EngineInstall(True),))))

    def test_a_claude_engine_install_is_checked_in_the_tree_claude_gets(self):
        def kit(select, core, extra):
            return self.kit(select, CodexPlacement((EngineInstall(False),)),
                            ClaudePlacement((EngineInstall(True, 'kit-core', core), EngineInstall(False, 'kit-extra', extra))))
        self.assert_refused(kit(('one', 'two'), ('one',), ('two',)), '../two/SKILL.md')
        self.assert_released(kit(('bad', 'one', 'two'), ('one', 'two'), ('bad',)))

    def test_the_unmanaged_option_builds_a_package_no_pull_installs_whatever_its_links(self):
        package = self.kit(('bad', 'two'), CodexPlacement((EngineInstall(False),)), ClaudePlacement((EngineInstall(False),)))
        code, lines = self.build_with_unchecked_lock(package, '--unmanaged')
        self.assertEqual(code, 0, lines)
        self.assertEqual([line.split(':')[0] for line in lines[:-1]],
                         ['built unmanaged kit codex', 'built unmanaged kit kit@personal-collections'])

    def test_a_windows_patch_whose_text_upstream_changed_stops_a_release_that_windows_pulls(self):
        def kit(find, platforms):
            patch = WindowsReplace('skills/two/SKILL.md', find, 'Patched.\n')
            return self.kit(('one', 'two'), CodexPlacement((EngineInstall(True),), windows_replace=(patch,)),
                            platforms=platforms)
        with mock.patch('sync_sources.WINDOWS', False):
            self.assert_refused(kit('Changed.\n', frozenset()), 'kit@personal:skills/two/SKILL.md')
            self.assert_released(kit('Changed.\n', frozenset({Platform.LINUX, Platform.MACOS})))
            self.assert_released(kit('Body.\n', frozenset()))

    def test_a_codex_aligned_copy_gets_no_overlay(self):
        patch = WindowsReplace('skills/two/SKILL.md', 'Changed.\n', 'Patched.\n')
        shapes = {'windows patch': self.kit(('one', 'two'), CodexPlacement((EngineInstall(True),)),
                                            ClaudePlacement((AlignedInstall(True),), windows_replace=(patch,))),
                  'explicit skill the copy renames': self.kit(('odd', 'two'), CodexPlacement((EngineInstall(True),)),
                                                              ClaudePlacement((AlignedInstall(True),)),
                                                              modes={'Odd_Name': SkillMode.EXPLICIT})}
        for shape, package in shapes.items():
            with self.subTest(shape=shape):
                self.assert_released(package)

    def test_a_codex_aligned_copy_built_on_windows_matches_the_lock_a_linux_release_wrote(self):
        patch = WindowsReplace('skills/two/SKILL.md', 'Body.\n', 'Patched.\n')
        package = self.kit(('one', 'two'), CodexPlacement((EngineInstall(True),), windows_replace=(patch,)),
                           ClaudePlacement((AlignedInstall(True),)))
        with mock.patch('sync_sources.WINDOWS', False):
            setup = self.refresh(package)
        builder = self.builder(setup)
        with mock.patch('sync_sources.WINDOWS', True):
            self.assertEqual(list(builder.claude(package, builder.codex(package))), ['kit@codex-aligned'])


if __name__ == '__main__':
    unittest.main()
