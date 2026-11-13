from contextlib import chdir, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402
from host_state import DeviceRoots  # noqa: E402
from release_case import CandidateCase, git, parser_into, release_main  # noqa: E402


def allow_future_key(public):
    parser = public / 'description_toml.py'
    before = parser.read_text(encoding='utf-8')
    anchor = "        schema = table.get('schema', int)"
    if before.count(anchor) != 1:
        raise AssertionError('the fixture parser has no unique schema read')
    parser.write_text(before.replace(anchor, anchor + "\n        table.get('future_marker', bool, False)"),
                      encoding='utf-8', newline='\n')


@unittest.skipIf(sys.platform == 'win32', 'releases are made on WSL or Omarchy')
class ParserFinishTest(CandidateCase):
    def new_key(self):
        self.seed_parser()
        self.prepare('new-key')
        allow_future_key(self.mine)
        setup = self.mine / release.SETUP
        setup.parent.mkdir()
        setup.write_text('schema = 1\npublic = true\nfuture_marker = true\n', encoding='utf-8')
        ignore = self.mine / '.gitignore'
        ignore.write_text(ignore.read_text(encoding='utf-8') + '!/setup/\n!/setup/**\n', encoding='utf-8')

    def test_finish_accepts_a_new_key_from_the_same_release(self):
        self.new_key()
        remote = self.origin_heads()

        with redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()) as output:
            status = release_main(self.argv, self.public)

        self.assertEqual(status, 0, error.getvalue())
        self.assertIn('not pushed', output.getvalue())
        self.assertEqual(self.origin_heads(), remote)
        self.assertTrue(release.tests_passed(self.base).is_file())

    def test_finish_ignores_unavailable_parent_parser_modules(self):
        self.new_key()
        modules = dict.fromkeys(('description', 'description_toml', 'drift', 'host_state'))
        with patch.dict(sys.modules, modules), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main(self.argv, self.public), 0, error.getvalue())
        self.assertTrue(release.tests_passed(self.base).is_file())

    def test_detached_candidate_uses_its_own_committed_parser(self):
        self.new_key()
        git(self.mine, 'add', '-A')
        git(self.mine, 'commit', '-qm', 'Add a setup key and use it')
        with self.make_candidate() as draft:
            (self.mine / 'description_toml.py').write_text('raise RuntimeError("editable parser rejected")\n',
                                                        encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'editable parser rejected'):
                release.edited_owned(self.mine, self.private_tree, self.private)
            self.assertEqual(release.edited_owned(draft.public, draft.private, self.private), {})
            self.assertEqual(git(draft.public, 'rev-parse', 'HEAD'), draft.commits.public)
            self.assertTrue((draft.public / 'description_toml.py').is_file())

    def test_malformed_candidate_stops_before_receipt_or_push(self):
        self.new_key()
        remote = self.origin_heads()
        candidate = release.candidate

        @contextmanager
        def malformed(*args, **kwargs):
            with candidate(*args, **kwargs) as draft:
                with (draft.public / release.SETUP).open('a', encoding='utf-8') as setup:
                    setup.write('candidate_only_bad_key = true\n')
                yield draft

        with patch.object(release, 'candidate', malformed), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release_main([*self.argv, '--push'], self.public), 3)
        self.assertIn('candidate_only_bad_key: unknown key', error.getvalue())
        self.assertEqual(self.origin_heads(), remote)
        self.assertFalse(release.tests_passed(self.base).exists())


class ParserQueryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.public = self.root / 'public tr\u00e6'
        self.private = self.root / 'private tr\u00e6'
        self.engine = self.root / 'canonical engine'
        for path in (self.public, self.private, self.engine):
            path.mkdir()
        parser_into(self.public)
        engine = patch.object(release, 'ENGINE', self.engine)
        engine.start()
        self.addCleanup(engine.stop)

    def setup_file(self):
        path = self.public / release.SETUP
        path.parent.mkdir(exist_ok=True)
        path.write_text('schema = 1\npublic = true\n', encoding='utf-8')
        return path

    def owned(self, root, name, kind='skills'):
        source = root / kind / name
        source.mkdir(parents=True)
        file = source / 'SKILL.md'
        file.write_bytes(b'Pinned\n')
        self.pin(root, name, kind)
        return file

    def pin(self, root, name, kind='skills'):
        source = root / kind / name
        digest = replace(DeviceRoots.current(), engine=self.engine).tree_digest(source)
        entry = root / 'setup' / kind / f'{name}.toml'
        entry.parent.mkdir(parents=True, exist_ok=True)
        flag = 'public = true\n' if root == self.public else ''
        place = '\n[place]\n' if kind == 'skills' else ''
        entry.write_text(f'{flag}[source]\nlocal = "{kind}/{name}"\ntree = "{digest}"\n{place}', encoding='utf-8')

    def query(self):
        return release.edited_owned(self.public, self.private, self.private)

    def test_missing_public_setup_does_not_start_a_child(self):
        with patch.object(release.subprocess, 'run', side_effect=AssertionError('a child started')):
            self.assertEqual(self.query(), {})

    def test_absent_and_empty_private_setup_are_accepted(self):
        self.setup_file()
        self.owned(self.public, 'mine')
        self.assertEqual(self.query(), {})
        (self.private / 'setup').mkdir()
        self.assertEqual(self.query(), {})

    def test_owned_sources_from_both_roots_are_named_after_edits(self):
        self.setup_file()
        entries = ((self.public, 'public-skill', 'skills'), (self.private, 'private-skill', 'skills'),
                   (self.public, 'public-package', 'packages'), (self.private, 'private-package', 'packages'))
        files = [self.owned(*entry) for entry in entries]
        self.assertEqual(self.query(), {})
        for file in files:
            file.write_bytes(b'Edited\n')
        self.assertEqual(self.query(), {name: f'{kind}/{name}' for _, name, kind in entries})

    def test_generated_paths_keep_canonical_engine_normalisation(self):
        self.setup_file()
        file = self.owned(self.public, 'generated')
        metadata = file.parent / '.claude-plugin/codex-alignment.json'
        metadata.parent.mkdir()
        metadata.write_text(json.dumps({'source': str(self.engine / 'source')}), encoding='utf-8')
        self.pin(self.public, 'generated')
        self.assertEqual(self.query(), {})
        file.write_bytes(b'Edited\n')
        self.assertEqual(self.query(), {'generated': 'skills/generated'})

    def test_relative_paths_with_spaces_and_non_ascii_resolve_before_child_cwd(self):
        self.setup_file()
        file = self.owned(self.private, 'mine')
        metadata = file.parent / '.claude-plugin/codex-alignment.json'
        metadata.parent.mkdir()
        metadata.write_text(json.dumps({'source': str(self.engine / 'source')}), encoding='utf-8')
        self.pin(self.private, 'mine')
        with chdir(self.root), patch.object(release, 'ENGINE', Path(self.engine.name)):
            self.assertEqual(release.edited_owned(Path(self.public.name), Path(self.private.name), self.private), {})
            file.write_bytes(b'Edited\n')
            self.assertEqual(release.edited_owned(Path(self.public.name), Path(self.private.name), self.private),
                             {'mine': 'skills/mine'})

    def test_child_launch_closes_stdin_and_keeps_the_existing_isolation_and_timeout(self):
        self.setup_file()
        with patch.object(release.subprocess, 'run', wraps=subprocess.run) as launch:
            self.assertEqual(self.query(), {})
        args, options = launch.call_args
        self.assertEqual(args[0][:4], [sys.executable, '-I', '-B', '-c'])
        self.assertEqual(args[0][-3:], [str(self.public), str(self.private), str(self.engine)])
        self.assertEqual(options['cwd'], self.public)
        self.assertEqual(options['stdin'], subprocess.DEVNULL)
        self.assertTrue(options['capture_output'])
        self.assertEqual(options['timeout'], 1800)
        self.assertFalse((self.public / '__pycache__').exists())

    def test_parent_modules_and_pythonpath_cannot_override_tree_modules(self):
        self.setup_file()
        file = self.owned(self.public, 'mine')
        file.write_bytes(b'Edited\n')
        poison = self.root / 'poison'
        poison.mkdir()
        modules = ('description', 'description_toml', 'drift', 'host_state')
        (poison / 'sitecustomize.py').write_text(
            'import sys\nfrom types import ModuleType\n'
            'parser = ModuleType("description_toml")\n'
            'def load(roots):\n    raise RuntimeError("PYTHONPATH parser selected")\n'
            'parser.load = load\nsys.modules["description_toml"] = parser\n', encoding='utf-8')
        with patch.dict(sys.modules, dict.fromkeys(modules)), patch.dict(os.environ, {'PYTHONPATH': str(poison)}):
            self.assertEqual(self.query(), {'mine': 'skills/mine'})

    def test_tree_parser_can_reject_input_the_parent_parser_accepts(self):
        import description
        import description_toml
        self.setup_file()
        description_toml.load(description.Roots(self.public / 'setup', None))
        parser = self.public / 'description_toml.py'
        before = parser.read_text(encoding='utf-8')
        anchor = "        schema = table.get('schema', int)"
        self.assertEqual(before.count(anchor), 1)
        parser.write_text(before.replace(anchor, anchor + "\n        table.error('schema', 'selected tree rejects setup')")
                          + '\nimport sys\nprint("tree parser diagnostic", file=sys.stderr)\n',
                          encoding='utf-8', newline='\n')
        with redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(RuntimeError) as raised:
                self.query()
        self.assertIn('the setup does not load:\n', str(raised.exception))
        self.assertIn('schema: selected tree rejects setup', str(raised.exception))
        self.assertNotIn('parser exited', str(raised.exception))
        self.assertEqual(error.getvalue(), 'tree parser diagnostic\n')

    def test_missing_tree_parser_never_falls_back_to_the_parent(self):
        self.setup_file()
        (self.public / 'description_toml.py').unlink()
        with self.assertRaises(RuntimeError) as raised:
            self.query()
        self.assertIn('parser exited 1', str(raised.exception))
        self.assertIn("No module named 'description_toml'", str(raised.exception))

    def test_tree_digest_module_and_successful_stderr_are_used(self):
        self.setup_file()
        with (self.public / 'drift.py').open('a', encoding='utf-8') as output:
            output.write('\ndef edited_owned(setup, roots):\n    import sys\n'
                         '    print("selected digest warning", file=sys.stderr)\n'
                         '    return {"selected": "tree-source"}\n')
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(self.query(), {'selected': 'tree-source'})
        self.assertEqual(error.getvalue(), 'selected digest warning\n')

    def test_process_failures_preserve_diagnostics_without_setup_advice(self):
        self.setup_file()
        for module, body, diagnostic in (
                ('description_toml.py', 'raise RuntimeError("parser crashed")\n', 'RuntimeError: parser crashed'),
                ('description_toml.py', 'import missing_parser_dependency\n', "No module named 'missing_parser_dependency'"),
                ('description_toml.py', 'def load(roots):\n    raise RuntimeError("unexpected setup read failure")\n',
                 'RuntimeError: unexpected setup read failure'),
                ('drift.py', 'from description import DescriptionError\ndef edited_owned(setup, roots):\n'
                 '    raise DescriptionError(["digest failed after setup load"])\n', 'digest failed after setup load')):
            with self.subTest(module=module, diagnostic=diagnostic):
                parser_into(self.public)
                (self.public / module).write_text(body, encoding='utf-8')
                with patch.object(release, 'unloadable', side_effect=AssertionError('misclassified process failure')):
                    with self.assertRaises(RuntimeError) as raised:
                        self.query()
                self.assertIn(str(self.public), str(raised.exception))
                self.assertIn('parser exited 1', str(raised.exception))
                self.assertIn(diagnostic, str(raised.exception))

    def test_bad_json_and_reply_shapes_stop_with_stderr(self):
        self.setup_file()
        for reply in ('', 'not JSON', 'null', '[]', '{}', '{"unknown": {}}', '{"edited": {}, "extra": true}',
                      '{"edited": []}', '{"edited": {"mine": 3}}', '{"description_error": []}',
                      '{"edited": {"valid": "skills/valid", "invalid": 3}}',
                      '{"edited": {}, "description_error": "contradictory"}'):
            with self.subTest(reply=reply):
                (self.public / 'description_toml.py').write_text(
                    f'import sys\nprint("child protocol diagnostic", file=sys.stderr)\n'
                    f'print({reply!r})\nraise SystemExit(0)\n', encoding='utf-8')
                with self.assertRaises(RuntimeError) as raised:
                    self.query()
                self.assertIn(str(self.public), str(raised.exception))
                self.assertIn('parser exited 0 with', str(raised.exception))
                self.assertIn('child protocol diagnostic', str(raised.exception))

    def test_nonzero_status_cannot_be_hidden_by_a_success_reply(self):
        self.setup_file()
        (self.public / 'description_toml.py').write_text(
            'import sys\nprint(\'{"edited": {}}\')\nprint("child interrupted", file=sys.stderr)\n'
            'raise SystemExit(9)\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'parser exited 9\\nchild interrupted'):
            self.query()

    def test_launch_errors_stop_with_their_diagnostics(self):
        self.setup_file()
        with patch.object(release.subprocess, 'run', side_effect=OSError('launch refused')):
            with self.assertRaises(RuntimeError) as raised:
                self.query()
        self.assertIn(str(self.public), str(raised.exception))
        self.assertIn('could not start owned-source parser: launch refused', str(raised.exception))

    def test_timeouts_preserve_partial_bytes_text_and_missing_stderr(self):
        self.setup_file()
        for stderr, ending in (
                (b'partial timeout diagnostic\xff', '\npartial timeout diagnostic\ufffd'),
                ('partial timeout diagnostic', '\npartial timeout diagnostic'),
                (None, 'seconds\n')):
            with self.subTest(stderr=stderr):
                timeout = subprocess.TimeoutExpired(['parser'], 1800, stderr=stderr)
                with patch.object(release.subprocess, 'run', side_effect=timeout):
                    with self.assertRaises(RuntimeError) as raised:
                        self.query()
                self.assertIn(str(self.public), str(raised.exception))
                self.assertEqual(str(raised.exception).splitlines()[0],
                                 f'{self.public}: owned-source parser timed out after 1800 seconds')
                self.assertTrue(str(raised.exception).endswith(ending), str(raised.exception))
                self.assertNotIn('setup does not load', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
