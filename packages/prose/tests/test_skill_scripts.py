"""Behaviour of the scripts the skills ship."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRAST = ROOT / "plugin/skills/charts/scripts/contrast.py"
PAGE_WIDTH = ROOT / "plugin/skills/charts/scripts/page_width.py"
SCAN = ROOT / "plugin/skills/prose/scripts/scan_prose.py"


def run(
    script: Path,
    *args: str,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(script), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
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


# A chart drawn 600 px wide, and a page whose 900 px table scrolls inside its own container, with a
# body tall enough to need a vertical scrollbar.
WIDE_PAGE = """<!doctype html><meta name="viewport" content="width=device-width">
<body style="margin:0"><div style="width:600px;height:20px">Chart</div></body>"""
FITTING_PAGE = """<!doctype html><meta name="viewport" content="width=device-width">
<body style="margin:0"><div style="overflow-x:auto"><table style="width:900px"><tr><td>Wide
table</td></tr></table></div><div style="height:3000px"></div></body>"""
# A note set at 10 px under a 16 px chart title, and the same page with the note at 12 px.
SMALL_TEXT_PAGE = """<!doctype html><meta name="viewport" content="width=device-width">
<body style="margin:0"><h1 style="font-size:16px">Title</h1>
<p style="font-size:10px">Source note</p></body>"""
TWELVE_PX_PAGE = SMALL_TEXT_PAGE.replace("font-size:10px", "font-size:12px")


class PageWidthTests(unittest.TestCase):
    def test_missing_browser_tool_is_named(self) -> None:
        result = run(PAGE_WIDTH, "page.html", env={"PATH": str(ROOT / "no-such-bin")})
        self.assertEqual(result.returncode, 2)
        self.assertIn("agent-browser is not on PATH", result.stderr)


@unittest.skipUnless(shutil.which("agent-browser"), "needs agent-browser")
class PageWidthBrowserTests(unittest.TestCase):
    def measure(self, html: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as folder:
            page = Path(folder) / "page.html"
            page.write_text(html, encoding="utf-8")
            return run(PAGE_WIDTH, str(page))

    def test_a_page_wider_than_the_phone_fails(self) -> None:
        result = self.measure(WIDE_PAGE)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn("at 390 px: scroll width 600 px, OVERFLOWS", lines[0])
        self.assertIn("at 1366 px: scroll width 1366 px, fits", lines[1])

    def test_a_table_scrolling_in_its_own_container_passes(self) -> None:
        result = self.measure(FITTING_PAGE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count(", fits"), 2)

    def test_text_under_twelve_px_fails_and_is_listed(self) -> None:
        result = self.measure(SMALL_TEXT_PAGE)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn(
            "at 390 px: scroll width 390 px, fits; smallest text 10 px, "
            "1 of 2 text runs UNDER 12 px",
            lines[0],
        )
        self.assertEqual(lines.count("  10 px: Source note"), 2)

    def test_text_at_twelve_px_passes(self) -> None:
        result = self.measure(TWELVE_PX_PAGE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("smallest text 12 px"), 2)
        self.assertNotIn("UNDER", result.stdout)

    def test_a_page_that_fails_to_load_is_an_error(self) -> None:
        result = run(PAGE_WIDTH, "http://127.0.0.1:9/")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("could not open it", result.stderr)


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

    def test_appositive_pair_is_not_flagged(self) -> None:
        text = "We compared the reporting data, current and target.\n"
        self.assertNotIn("serial-comma", self.checks(text))

    def test_pair_inside_a_list_with_its_serial_comma_is_not_flagged(self) -> None:
        text = "The stack is Microsoft 365, SharePoint and OneDrive, and Power BI.\n"
        self.assertNotIn("serial-comma", self.checks(text))

    def test_pair_of_prepositions_after_a_comma_is_not_flagged(self) -> None:
        for text in (
            "Compare one task, written with and without that rule.\n",
            "Time the suite, measured before and after the change.\n",
        ):
            with self.subTest(text=text):
                self.assertNotIn("serial-comma", self.checks(text))

    def test_series_that_resembles_those_exceptions_is_flagged(self) -> None:
        for text in (
            "The table shows revenue, actual and planned costs.\n",
            "We compared the past, current and target.\n",
            "We bought apples, pears and plums, and then we left.\n",
            "We sampled the data before, during and after the move.\n",
            "We built it, set it up and shipped it.\n",
        ):
            with self.subTest(text=text):
                self.assertIn("serial-comma", self.checks(text))

    def test_no_serial_comma_skips_only_that_check(self) -> None:
        found = self.checks(
            "We bought apples, pears and plums, etc.\n", ".md", "--no-serial-comma"
        )
        self.assertEqual(found, ["etc"])

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
