import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location("pstack_runtime", Path(__file__).parents[1] / "pstack_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class PstackRuntimeTests(unittest.TestCase):
    def native_process(self, pages):
        messages = [{'id': 1, 'result': {}}]
        messages.extend({'id': i, 'result': page} for i, page in enumerate(pages, 2))
        return Mock(stdin=io.StringIO(), stdout=io.StringIO(
            ''.join(json.dumps(message) + '\n' for message in messages)))

    def test_native_models_override_empty_stale_cache_and_follow_pages(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            cache = home / 'models_cache.json'
            stale = b'{"client_version":"0.167.0","models":[]}'
            cache.write_bytes(stale)
            pages = [
                {'data': [], 'nextCursor': 'second'},
                {'data': [{'id': 'selection-id', 'model': 'available-model',
                           'supportedReasoningEfforts': [{'reasoningEffort': 'xhigh'}]}],
                 'nextCursor': None},
            ]
            value = (b'## claude-code\narena runners: codex:available-model@xhigh\n'
                     b'## codex\nreader: available-model@xhigh\n')
            with patch.object(runtime.portable, 'codex_cli', return_value='codex'), \
                    patch.object(runtime.subprocess, 'Popen', return_value=self.native_process(pages)) as launch:
                self.assertEqual(runtime.validate_models(home, value, codex_home=home), 2)
            self.assertEqual(launch.call_args.kwargs['env']['CODEX_HOME'], str(home))
            self.assertEqual(cache.read_bytes(), stale)

    def test_preserves_bom_crlf_comments_and_unrelated_flags(self):
        before = b'\xef\xbb\xbfmodel = "gpt-6.1-sol"\r\n[features.multi_agent_v2]\r\nenabled = true\r\nexpose_spawn_agent_model_overrides = false # user choice\r\n[other]\r\nexpose_spawn_agent_model_overrides = false\r\n'
        after = runtime.patch_config(before, True)
        self.assertEqual(after, before.replace(b'false # user choice', b'true # user choice'))
        self.assertEqual(runtime.patch_config(after, True), after)

    def test_refuses_unknown_config_layout(self):
        for value in [b'[features]\nmulti_agent_v2 = true\n', b'[features.multi_agent_v2]\nenabled = true\n']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                runtime.patch_config(value, True)

    def test_catches_duplicate_roles(self):
        value = b'## claude-code\nhardest tasks: opus@xhigh\n## codex\nhardest tasks: gpt-6-astra@max\nhardest tasks: gpt-6.1-sol@xhigh\n'
        with self.assertRaisesRegex(ValueError, "duplicate"):
            runtime.model_entries(value)

    def test_does_not_overwrite_concurrent_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            path.write_bytes(b"concurrent edit")
            with self.assertRaisesRegex(RuntimeError, "Concurrent edit"):
                runtime.replace_checked(path, b"reviewed version", b"replacement")
            self.assertEqual(path.read_bytes(), b"concurrent edit")
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_accepts_claude_aliases_the_help_text_omits(self):
        with tempfile.TemporaryDirectory() as folder:
            codex_home = Path(folder)
            pages = [{'data': [{'model': 'gpt-6.1-sol',
                               'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}], 'nextCursor': None}]
            value = b'## claude-code\nrecall readers: haiku@low\n## codex\nrecall readers: gpt-6.1-sol@low\n'
            with patch.object(runtime.portable, "claude_cli", return_value="claude"), \
                    patch.object(runtime.portable, "codex_cli", return_value="codex"), \
                    patch.object(runtime.subprocess, 'Popen', side_effect=lambda *a, **kw: self.native_process(pages)):
                self.assertEqual(runtime.validate_models(codex_home, value, codex_home=codex_home), 2)
                cross_harness = (b'## claude-code\narena runners: haiku@low, codex:gpt-6.1-sol@low\n'
                                 b'## codex\narena runners: gpt-6.1-sol@low, claude:opus@high\n')
                self.assertEqual(runtime.validate_models(codex_home, cross_harness, codex_home=codex_home), 4)
                with self.assertRaisesRegex(ValueError, "Unconfirmed Claude alias for claude-code/recall readers: gpt"):
                    runtime.validate_models(codex_home, value.replace(b"haiku", b"gpt"), codex_home=codex_home)

    def test_rejects_model_or_effort_missing_from_native_catalog(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / 'models_cache.json').write_text(
                '{"models":[{"slug":"cached-model","visibility":"list",'
                '"supported_reasoning_levels":[{"effort":"max"}]}]}')
            pages = [{'data': [{'model': 'native-model',
                               'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}], 'nextCursor': None}]
            for selection in ('cached-model@max', 'native-model@max'):
                with self.subTest(selection=selection), \
                        patch.object(runtime.portable, 'codex_cli', return_value='codex'), \
                        patch.object(runtime.subprocess, 'Popen', return_value=self.native_process(pages)), \
                        self.assertRaisesRegex(ValueError, 'Unavailable Codex model or effort'):
                    runtime.validate_models(home, ('## claude-code\nrunner: codex:' + selection +
                                                  '\n## codex\nreader: inherit-parent@low\n').encode(), codex_home=home)

    def test_native_catalog_errors_do_not_fall_back_to_cache(self):
        for response in ({'error': {'message': 'unavailable'}},
                         {'result': {'data': [{}], 'nextCursor': None}},
                         {'result': {'data': [], 'nextCursor': 'again'}}):
            process = self.native_process([])
            process.stdout = io.StringIO(json.dumps({'id': 1, 'result': {}}) + '\n' +
                                          ''.join(json.dumps({'id': i, **response}) + '\n' for i in (2, 3)))
            with self.subTest(response=response), \
                    patch.object(runtime.portable, 'codex_cli', return_value='codex'), \
                    patch.object(runtime.subprocess, 'Popen', return_value=process), \
                    self.assertRaises((RuntimeError, ValueError)):
                runtime.validate_models(Path('/unused'), b'')

    def test_effective_config_must_resolve_requested_flag(self):
        for resolved in (True, False):
            process = self.native_process([{'config': {'features': {'multi_agent_v2': {
                runtime.OVERRIDE_KEY: resolved}}}}])
            with patch.object(runtime.portable, 'codex_cli', return_value='codex'), \
                    patch.object(runtime.subprocess, 'Popen', return_value=process):
                if resolved:
                    runtime.validate_codex(True, proposed=True)
                else:
                    with self.assertRaisesRegex(RuntimeError, 'did not resolve'):
                        runtime.validate_codex(True, proposed=True)


if __name__ == "__main__":
    unittest.main()
