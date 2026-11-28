"""Unit tests for packages/visual builder, checker, and token assets."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

VISUAL_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = VISUAL_ROOT / "plugin"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build_mod = _load_module("visual_build", PLUGIN_DIR / "scripts" / "build.py")
check_mod = _load_module("visual_check", PLUGIN_DIR / "scripts" / "check.py")


class VisualKitTests(unittest.TestCase):
    def test_tokens_css_has_geist_all_styles_and_linear_spring(self) -> None:
        tokens = (PLUGIN_DIR / "kit" / "tokens.css").read_text(encoding="utf-8")
        self.assertIn("'Geist'", tokens)
        self.assertIn("'Geist Mono'", tokens)
        self.assertIn("'Geist Serif'", tokens)
        self.assertIn('[data-style="nova"]', tokens)
        self.assertIn('[data-style="vega"]', tokens)
        self.assertIn('[data-style="maia"]', tokens)
        self.assertIn("linear(", tokens)

    def test_build_compiles_self_contained_html_and_tree_shakes_features(self) -> None:
        fragment = (
            '<section class="vk-panel">'
            '<h2 class="vk-title">Flow</h2>'
            '<svg role="img" aria-label="Sample flow"></svg>'
            "</section>"
        )
        out = build_mod.compile_html(
            fragment,
            title="Flow <test>",
            style="vega",
            mode="t3",
            features=["stepper"],
        )
        self.assertIn("<title>Flow &lt;test&gt;</title>", out)
        self.assertIn('<body data-mode="t3">', out)
        self.assertIn('<div class="vk-root" data-style="vega">', out)
        self.assertIn("initSteppers", out)
        self.assertIn("fitSvg", out)
        self.assertNotIn("initDiffRenderer", out)
        self.assertEqual(check_mod.check_static(out), [])

    def test_build_rejects_full_html_document_input(self) -> None:
        with self.assertRaises(ValueError):
            build_mod.compile_html("<!doctype html><html><body>hi</body></html>")

    def test_build_escapes_script_closing_tag_in_diff_json(self) -> None:
        payload = json.dumps({"a.ts": "+ const x = '</script><script>alert(1)</script>';"})
        out = build_mod.compile_html(
            '<div data-vk-diff="a.ts"></div>',
            diffs_json=payload,
        )
        self.assertIn("initDiffRenderer", out)
        self.assertNotIn("</script><script>alert(1)", out)
        self.assertIn("<\\/script>", out)
        self.assertEqual(check_mod.check_static(out), [])

    def test_build_standalone_mode_passes_static_checks(self) -> None:
        out = build_mod.compile_html(
            '<section class="vk-panel"><h2 class="vk-title">Standalone</h2></section>',
            style="maia",
            mode="standalone",
        )
        self.assertIn('<body data-mode="standalone">', out)
        self.assertEqual(check_mod.check_static(out), [])

    def test_check_static_catches_inaccessible_clicks_external_urls_vh_and_unlabeled_svg(self) -> None:
        bad_html = """<!doctype html>
<html><head>
<link href="https://fonts.googleapis.com/css2?family=Inter" rel="stylesheet">
<style>
  html, body { height: 100vh; background: #1a1a1a; font-family: Inter, sans-serif; }
</style>
</head>
<body data-mode="t3">
  <div onclick="toggle(this)">Toggle</div>
  <svg viewBox="0 0 100 50"><rect width="50" height="20"/></svg>
</body></html>"""
        issues = check_mod.check_static(bad_html)
        joined = "\n".join(issues)
        self.assertIn("Geist", joined)
        self.assertIn("external", joined)
        self.assertIn("vh", joined)
        self.assertIn("opaque background", joined)
        self.assertIn("keyboard accessible", joined)
        self.assertIn("<svg>", joined)

    def test_cli_roundtrip_writes_output_and_passes_check_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frag = Path(tmp) / "body.html"
            out = Path(tmp) / "page.html"
            frag.write_text(
                '<section class="vk-panel"><h2 class="vk-title">Check CLI</h2></section>\n',
                encoding="utf-8",
            )
            rc_build = build_mod.main(
                ["--input", str(frag), "--output", str(out), "--style", "nova", "--features", "diagram"]
            )
            self.assertEqual(rc_build, 0)
            rc_check = check_mod.main([str(out)])
            self.assertEqual(rc_check, 0)


if __name__ == "__main__":
    unittest.main()
