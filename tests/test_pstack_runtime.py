import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location("pstack_runtime", Path(__file__).parents[1] / "pstack_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class PstackRuntimeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
