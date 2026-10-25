"""Behaviour of the scripts the skills ship."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRAST = ROOT / "plugin/skills/charts/scripts/contrast.py"
SCAN = ROOT / "plugin/skills/prose/scripts/scan_prose.py"


def run(
    script: Path, *args: str, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(script), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


class ContrastTests(unittest.TestCase):
    def ratio(self, fg: str, bg: str, *extra: str) -> float:
        result = run(CONTRAST, "--json", *extra, fg, bg)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["ratio"]

    def test_black_on_white_is_twenty_one(self) -> None:
        self.assertAlmostEqual(self.ratio("#000", "#fff"), 21.0, places=3)

    def test_borderline_grey_fails_text_without_rounding_up(self) -> None:
        result = run(CONTRAST, "#777777", "#ffffff")
        self.assertIn("4.47:1", result.stdout)
        self.assertIn("text 4.5 FAIL", result.stdout)

    def test_alpha_is_composited_over_the_background(self) -> None:
        self.assertAlmostEqual(
            self.ratio("rgba(0,0,0,0.5)", "#fff"),
            self.ratio("#808080", "#fff"),
            places=1,
        )

    def test_oklch_white_matches_hex_white(self) -> None:
        self.assertAlmostEqual(self.ratio("oklch(1 0 0)", "#000"), 21.0, places=2)

    def test_min_sets_the_exit_status(self) -> None:
        self.assertEqual(run(CONTRAST, "--min", "4.5", "#777", "#fff").returncode, 1)
        self.assertEqual(run(CONTRAST, "--min", "3", "#777", "#fff").returncode, 0)

    def test_one_background_for_several_foregrounds(self) -> None:
        result = run(CONTRAST, "--bg", "#fff", "#595959", "#949494")
        self.assertEqual(len(result.stdout.splitlines()), 2)

    def test_unknown_colour_is_an_error(self) -> None:
        self.assertEqual(run(CONTRAST, "teal-ish", "#fff").returncode, 2)


class ScanTests(unittest.TestCase):
    def checks(self, text: str, suffix: str = ".md", *extra: str) -> list[str]:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / f"draft{suffix}"
            path.write_text(text, encoding="utf-8")
            result = run(SCAN, "--json", *extra, str(path))
        return [json.loads(line)["check"] for line in result.stdout.splitlines()]

    def test_missing_serial_comma_is_flagged(self) -> None:
        self.assertIn(
            "serial-comma", self.checks("We bought apples, pears and plums.\n")
        )

    def test_serial_comma_present_is_not_flagged(self) -> None:
        self.assertNotIn(
            "serial-comma", self.checks("We bought apples, pears, and plums.\n")
        )

    def test_introductory_phrase_and_relative_clause_are_not_series(self) -> None:
        text = "However, the team and the boss agreed. We met John, who likes tea and coffee.\n"
        self.assertNotIn("serial-comma", self.checks(text))

    def test_html_table_cells_are_not_joined(self) -> None:
        html = "<table><tr><td>Red, green</td><td>blue and yellow</td></tr></table>"
        self.assertEqual(self.checks(html, ".html"), [])

    def test_html_code_is_skipped(self) -> None:
        self.assertEqual(
            self.checks("<p>Run <code>a, b and c</code> now.</p>", ".html"), []
        )

    def test_markdown_code_fence_is_skipped(self) -> None:
        self.assertEqual(self.checks("```\nx — y... and/or z\n```\n"), [])

    def test_unslop_patterns_are_flagged(self) -> None:
        found = self.checks("It is not just fast but robust — it delves into data.\n")
        self.assertTrue({"em-dash", "vocabulary", "not-just"} <= set(found))

    def test_house_patterns_are_flagged(self) -> None:
        found = self.checks("Bring pens and/or paper, etc... Moreover, read it.\n")
        self.assertTrue({"and-or", "etc", "ellipsis"} <= set(found))

    def test_only_limits_the_groups(self) -> None:
        found = self.checks(
            "We bought apples, pears and plums — fast.\n", ".md", "--only", "house"
        )
        self.assertEqual(found, ["serial-comma"])

    def test_title_case_heading_is_flagged(self) -> None:
        self.assertIn(
            "title-case", self.checks("# Getting Started With The New Dashboard\n")
        )

    def test_clean_text_exits_zero(self) -> None:
        result = run(SCAN, "-", stdin="The plans cost the same.\n")
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
