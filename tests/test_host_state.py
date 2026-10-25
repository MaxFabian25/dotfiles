import json
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

import host_state
import host_observation


def roots_for(home):
    home = Path(home)
    return host_state.DeviceRoots(home=home, codex_home=home / '.codex', claude_home=home / '.claude',
                                  agents_home=home / '.agents', engine=home / 'plugins/upstream-packages')


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TreeDigestTest(unittest.TestCase):
    def tree(self, home, upstream_text):
        roots = roots_for(home)
        root = roots.claude_home / 'plugins/cache/codex-aligned/kit/1.0'
        write(root / 'skills/a/SKILL.md', upstream_text)
        write(root / '.claude-plugin/codex-alignment.json',
              json.dumps({'codexSource': str(roots.codex_home / 'plugins/cache/personal/kit/1.0')}))
        return roots.tree_digest(root)

    def test_generated_files_hash_the_same_under_any_home(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            self.assertEqual(self.tree(Path(one) / 'alice', '# kit\n'), self.tree(Path(two) / 'bob', '# kit\n'))

    def test_upstream_files_that_embed_the_home_are_hashed_raw(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            alice, bob = Path(one) / 'alice', Path(two) / 'bob'
            self.assertNotEqual(self.tree(alice, f'see {alice}/notes\n'), self.tree(bob, f'see {bob}/notes\n'))

    def test_engine_receipts_hash_the_same_under_any_home(self):
        digests = []
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            for home in (Path(one) / 'alice', Path(two) / 'bob'):
                roots = roots_for(home)
                release = roots.engine / 'kit/1.0'
                write(release / 'skills/a/SKILL.md', '# kit\n')
                write(release / '.codex-plugin/upstream.json', json.dumps(
                    {'commit': 'abc', 'sourceSnapshot': str(roots.engine / 'standalone-snapshots/kit/1234')}, indent=2))
                write(release / '.claude-plugin/upstream.json', json.dumps(
                    {'codexRelease': str(roots.engine / 'kit/1.0'), 'version': '1.0.claude1'}, indent=2))
                digests.append(roots.tree_digest(release))
        self.assertEqual(digests[0], digests[1])

    def test_a_windows_alignment_receipt_hashes_like_a_macos_one(self):
        windows = host_state.DeviceRoots(home=PureWindowsPath('C:/Users/example'), codex_home=PureWindowsPath('C:/Users/example/.codex'),
                                         claude_home=PureWindowsPath('C:/Users/example/.claude'),
                                         agents_home=PureWindowsPath('C:/Users/example/.agents'),
                                         engine=PureWindowsPath('C:/Users/example/plugins/upstream-packages'))
        digests = []
        with tempfile.TemporaryDirectory() as directory:
            for roots, source in ((roots_for('/Users/example'), '/Users/example/.codex/plugins/cache/personal/kit/1.0'),
                                  (windows, 'C:\\Users\\example\\.codex\\plugins\\cache\\personal\\kit\\1.0')):
                root = Path(directory) / str(len(digests))
                write(root / 'skills/a/SKILL.md', '# kit\n')
                write(root / '.claude-plugin/codex-alignment.json', json.dumps({'codexSource': source, 'agents': []}, indent=2))
                digests.append(roots.tree_digest(root))
        self.assertEqual(digests[0], digests[1])

    def test_portable_receipt_tree_has_a_stable_digest(self):
        roots = roots_for('/Users/example')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root / 'skills/example/SKILL.md', '# Example\n')
            write(root / '.codex-plugin/upstream.json', json.dumps({
                'version': '1.0', 'sourceSnapshot': str(roots.engine / 'standalone-snapshots/example/pin')}))
            write(root / '.claude-plugin/upstream.json', json.dumps({
                'version': '1.0.claude1', 'codexRelease': str(roots.engine / 'example/1.0')}))
            self.assertEqual(roots.tree_digest(root),
                             'sha256:bf40576199a460acd5fe220a4085baf7339c6de4a5eaa54c5fed3a9b648c71fa')

    def test_runtime_markers_and_dependencies_are_not_content(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = roots_for(directory)
            root = Path(directory) / 'tree'
            write(root / 'SKILL.md', 'x\n')
            before = roots.tree_digest(root)
            for marker in ('.in_use/pid', '.orphaned_at', 'node_modules/a/index.js', '.git/HEAD',
                           'scripts/__pycache__/tool.cpython-314.pyc'):
                write(root / marker, 'noise\n')
            self.assertEqual(roots.tree_digest(root), before)

    def test_swap_reads_overlaid_bytes_as_the_release(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = roots_for(directory)
            root = Path(directory) / 'tree'
            write(root / 'SKILL.md', 'original\n')
            pristine = roots.tree_digest(root)
            write(root / 'SKILL.md', 'overlaid\n')
            write(root / 'agents/openai.yaml', 'policy: {}\n')
            self.assertEqual(roots.tree_digest(root, swap={root / 'SKILL.md': b'original\n',
                                                           root / 'agents/openai.yaml': None}), pristine)


class RootsTest(unittest.TestCase):
    def test_neutral_replaces_the_longest_root_first(self):
        roots = roots_for('/Users/example')
        text = b'/Users/example/plugins/upstream-packages/codex_agents.py /Users/example/.codex/agents /Users/example/x'
        self.assertEqual(roots.neutral(text), b'{engine}/codex_agents.py {codex_home}/agents {home}/x')

    def test_a_windows_role_header_neutralises_like_a_macos_one(self):
        windows = host_state.DeviceRoots(home=PureWindowsPath('C:/Users/example'), codex_home=PureWindowsPath('C:/Users/example/.codex'),
                                         claude_home=PureWindowsPath('C:/Users/example/.claude'),
                                         agents_home=PureWindowsPath('C:/Users/example/.agents'),
                                         engine=PureWindowsPath('C:/Users/example/plugins/upstream-packages'))
        posix = b'# Generated by /Users/example/plugins/upstream-packages/codex_agents.py from /Users/example/.codex/plugins/cache/skills/example/1.0.0/agents/x.md. Do not edit.\n'
        native = (b'# Generated by C:\\Users\\example\\plugins\\upstream-packages\\codex_agents.py from '
                  b'C:\\Users\\example\\.codex\\plugins\\cache\\skills\\example\\1.0.0\\agents\\x.md. Do not edit.\n')
        self.assertEqual(windows.neutral(native), roots_for('/Users/example').neutral(posix))
        self.assertEqual(windows.neutral(native),
                         b'# Generated by {engine}/codex_agents.py from {codex_home}/plugins/cache/skills/example/1.0.0/agents/x.md. Do not edit.\n')

    def test_only_declared_role_files_are_neutralised(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            digests = []
            for home in (Path(one), Path(two)):
                roots = roots_for(home)
                write(roots.codex_home / 'agents/role.toml', f'# Generated by {roots.engine}/codex_agents.py\n')
                write(roots.codex_home / 'other/role.toml', f'# {roots.engine}\n')
                digests.append((roots.file_digest(roots.codex_home / 'agents/role.toml'),
                                roots.file_digest(roots.codex_home / 'other/role.toml')))
            self.assertEqual(digests[0][0], digests[1][0])
            self.assertNotEqual(digests[0][1], digests[1][1])

    def test_tokens_spell_paths_by_root(self):
        roots = roots_for('/Users/example')
        self.assertEqual(roots.tokens('/Users/example/.codex/skills/example/SKILL.md'), '{codex_home}/skills/example/SKILL.md')
        self.assertEqual(roots.tokens('/Users/example/Documents/x'), '{home}/Documents/x')
        self.assertEqual(roots.tokens('/opt/x'), '/opt/x')


class ReceiptTest(unittest.TestCase):
    def test_overlaid_files_swap_to_base_and_unknown_bytes_are_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = roots_for(directory)
            root = roots.codex_home / 'plugins/cache/personal/kit/1.0'
            write(root / 'a/SKILL.md', 'noted\n')
            write(root / 'b/SKILL.md', 'hand edit\n')
            write(root / 'c/agents/openai.yaml', 'created\n')
            write(roots.codex_home / 'skill-updates/invocation-overrides/state.json', json.dumps({'files': {
                str(root / 'a/SKILL.md'): {'base': 'plain\n', 'effective': 'noted\n'},
                str(root / 'b/SKILL.md'): {'base': 'plain\n', 'effective': 'noted\n'},
                str(root / 'c/agents/openai.yaml'): {'base': None, 'effective': 'created\n'},
                str(roots.codex_home / 'elsewhere/SKILL.md'): {'base': 'x', 'effective': 'y'},
            }}))
            swap, edited = host_observation.Receipt(roots).pristine(root)
            self.assertEqual(swap, {root / 'a/SKILL.md': b'plain\n', root / 'c/agents/openai.yaml': None})
            self.assertEqual(edited, [root / 'b/SKILL.md'])


    def test_a_deleted_overlaid_file_is_an_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = roots_for(directory)
            root = roots.codex_home / 'plugins/cache/personal/kit/1.0'
            write(root / 'keep/SKILL.md', 'x\n')
            write(roots.codex_home / 'skill-updates/invocation-overrides/state.json', json.dumps({'files': {
                str(root / 'a/SKILL.md'): {'base': 'plain\n', 'effective': 'noted\n'},
                str(root / 'b/agents/openai.yaml'): {'base': 'upstream\n', 'effective': None}}}))
            self.assertEqual(host_observation.Receipt(roots).pristine(root),
                             ({root / 'b/agents/openai.yaml': b'upstream\n'}, [root / 'a/SKILL.md']))

    def test_bytes_that_are_not_utf8_read_as_an_edit_not_a_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = roots_for(directory)
            root = roots.codex_home / 'plugins/cache/personal/kit/1.0'
            write(root / 'a/SKILL.md', 'noted\n')
            (root / 'a/SKILL.md').write_bytes(b'\xff\xfe binary\n')
            write(roots.codex_home / 'skill-updates/invocation-overrides/state.json', json.dumps({'files': {
                str(root / 'a/SKILL.md'): {'base': 'plain\n', 'effective': 'noted\n'}}}))
            self.assertEqual(host_observation.Receipt(roots).pristine(root), ({}, [root / 'a/SKILL.md']))


class CheckoutTest(unittest.TestCase):
    def test_git_that_times_out_on_a_checkout_is_unread(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / '.git').mkdir()
            with mock.patch.object(host_observation, 'run', side_effect=subprocess.TimeoutExpired('git', 120)):
                with self.assertRaises(host_observation.Unread):
                    host_observation.checkout(Path(directory))

    def test_a_folder_that_is_not_a_checkout_is_none(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(host_observation.checkout(Path(directory)))


class ParsingTest(unittest.TestCase):
    def test_commit_rejects_branches_and_short_shas(self):
        self.assertEqual(host_state.parse_commit('d631a8827f99414d2b6daba4ef08b7f8701751d7'),
                         'd631a8827f99414d2b6daba4ef08b7f8701751d7')
        for text in ('main', 'd631a88', 'D631A8827F99414D2B6DABA4EF08B7F8701751D7'):
            with self.assertRaises(ValueError):
                host_state.parse_commit(text)

    def test_clone_url_spells_github_repositories_one_way(self):
        for text in ('https://github.com/cursor/plugins/tree/main/pstack', 'https://github.com/cursor/plugins',
                     'cursor/plugins', 'https://github.com/cursor/plugins.git/'):
            self.assertEqual(host_state.clone_url(text), 'https://github.com/cursor/plugins.git')
        self.assertEqual(host_state.clone_url('https://gitlab.com/a/b'), 'https://gitlab.com/a/b')


class InstalledEngineTest(unittest.TestCase):
    def test_reads_the_checkout_claude_installs_engine_builds_from(self):
        with tempfile.TemporaryDirectory() as temp:
            roots = roots_for(temp)
            self.assertIsNone(roots.installed_engine())
            checkout = Path(temp) / 'dotfiles'
            registry = {'personal-collections': {'source': {'source': 'directory', 'path': str(checkout / 'claude')}}}
            write(roots.claude_home / 'plugins/known_marketplaces.json', json.dumps(registry))
            self.assertEqual(roots.installed_engine(), checkout.resolve())

    def test_sync_refuses_an_engine_outside_the_installed_checkout(self):
        import sync
        with tempfile.TemporaryDirectory() as temp:
            roots = roots_for(temp)
            registry = {'personal-collections': {'source': {'source': 'directory', 'path': str(Path(temp) / 'dotfiles/claude')}}}
            write(roots.claude_home / 'plugins/known_marketplaces.json', json.dumps(registry))
            device = mock.Mock(roots=roots)
            with mock.patch('sync.load'), mock.patch('sync.Device.current', return_value=device), \
                    mock.patch('sync.plan') as plan, mock.patch('sys.stderr') as stderr:
                self.assertEqual(sync.main(['--check']), 3)
            plan.assert_not_called()
            self.assertIn('run sync.py or pull.py from that checkout', ''.join(call.args[0] for call in stderr.write.call_args_list))


if __name__ == '__main__':
    unittest.main()
