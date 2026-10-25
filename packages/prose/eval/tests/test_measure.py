"""Check measure.py against fixture pages with known values. Needs agent-browser and Chrome.

Run from packages/prose: make eval-test
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import measure  # noqa: E402

# Three regions on white: #808080 drawn as black at half opacity, #767676 and black. The group scales by 2, so the
# 0.5 outlines render 1 px wide, and so does the 1 px non-scaling outline.
MAP = """<!doctype html><body style="margin:0">
<svg width="300" height="150"><rect width="300" height="150" fill="#ffffff"/>
<g transform="scale(2)" stroke="#767676">
<path d="M0 0h40v40h-40z" fill="#000000" fill-opacity="0.5" stroke-width="0.5"/>
<path d="M50 0h40v40h-40z" fill="#767676" stroke-width="0.5"/>
<path d="M100 0h40v40h-40z" fill="#000000" stroke-width="1" vector-effect="non-scaling-stroke"/>
</g></svg></body>"""
# 14 px body text, and SVG text at 10 and 5 user units in a viewBox drawn at twice its size: 20 px and 10 px.
TEXT = """<!doctype html><body style="margin:0"><p style="font-size:14px">Body</p>
<svg width="200" height="100" viewBox="0 0 100 50"><text x="5" y="15" font-size="10">Big</text>
<text x="5" y="40" font-size="5">Tiny</text></svg></body>"""
WIDE = """<!doctype html><body style="margin:0"><div style="width:600px;height:20px"></div></body>"""


@unittest.skipUnless(shutil.which("agent-browser"), "needs agent-browser")
class MeasureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        work = Path(cls.temp.name)
        # The same pages as a run and as a blinded copy, whose workspace is files/.
        blinded = work / "judge/r1/task/A"
        for seat, folder in (
            (work / "runs/fixture/task/seat", "workspace"),
            (blinded, "files"),
        ):
            (seat / folder).mkdir(parents=True)
            (seat / "meta.json").write_text("{}")
            (seat / "inputs.json").write_text("{}")
            for name, page in (
                ("map.html", MAP),
                ("text.html", TEXT),
                ("wide.html", WIDE),
            ):
                (seat / folder / name).write_text(page)
        out = work / "measure.md"
        argv = [
            "measure.py",
            "--work",
            str(work),
            "--map",
            "--out",
            str(out),
            "fixture",
            str(blinded.parent),
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch("builtins.print"):
            measure.main()
        rows = [
            line.split(" | ")
            for line in out.read_text().splitlines()
            if line.startswith("| ")
        ]
        cls.rows = [r for r in rows if r[0] == "| fixture/task/seat"]
        cls.blinded = [r for r in rows if r[0] == "| r1/task/A"]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def rows_for(self, page, cells):
        return [r[2:] for r in self.rows if r[1] == page and len(r) == cells]

    def test_a_blinded_copy_measures_the_same(self):
        self.assertEqual([r[1:] for r in self.blinded], [r[1:] for r in self.rows])

    def test_width_flags_a_page_wider_than_the_phone(self):
        self.assertEqual(self.rows_for("wide.html", 5), [["600", "1366", "no |"]])
        self.assertEqual(self.rows_for("map.html", 5), [["390", "1366", "yes |"]])

    def test_text_is_scaled_by_its_screen_transform(self):
        self.assertEqual(
            self.rows_for("text.html", 6),
            [[w, "10", "1/3", "10px Tiny |"] for w in ("390", "1366")],
        )

    def test_map_blends_fills_and_scales_outlines(self):
        cells = [
            "#ffffff",
            "#808080 ×1 3.95, #767676 ×1 4.54, #000000 ×1 21.00",
            "#767676 1px ×3 4.54",
            "0.60 |",
        ]
        self.assertEqual(
            self.rows_for("map.html", 7), [[w, *cells] for w in ("390", "1366")]
        )


if __name__ == "__main__":
    unittest.main()
