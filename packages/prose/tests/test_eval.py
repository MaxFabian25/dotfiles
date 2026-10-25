"""Checks of the evaluation harness that need no model, browser or network."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

import harness  # noqa: E402
import run_writers  # noqa: E402
import skill_usage  # noqa: E402


class EvalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name)

    def test_candidate_gets_a_claude_manifest_and_keeps_its_label(self) -> None:
        dest = run_writers.build_candidate(ROOT / "plugin", self.tmp / "label")
        manifest = json.loads((dest / ".claude-plugin/plugin.json").read_text())
        codex = json.loads((ROOT / "plugin/.codex-plugin/plugin.json").read_text())
        self.assertEqual(
            (manifest["name"], manifest["version"]), (codex["name"], codex["version"])
        )
        self.assertEqual(
            manifest["skills"],
            ["./skills/charts", "./skills/prose", "./skills/prose-maintainer"],
        )
        other = self.tmp / "other"
        (other / "skills/prose").mkdir(parents=True)
        (other / "skills/prose/SKILL.md").write_text("Changed.\n")
        with self.assertRaises(SystemExit):
            run_writers.build_candidate(other, dest)

    def test_boundaries_keep_the_task_regions_sorted_with_three_properties(
        self,
    ) -> None:
        def feature(nid: str) -> dict:
            props = {
                "NUTS_ID": nid,
                "NAME_LATN": f"Sjælland {nid}",
                "CNTR_CODE": nid[:2],
                "LEVL_CODE": 2,
            }
            return {"type": "Feature", "properties": props, "geometry": None}

        geo = {"features": [feature("SE11"), feature("DK02"), feature("DE30")]}
        out = json.loads(run_writers.filter_boundaries(geo, {"DK02", "SE11"}))
        self.assertEqual(
            [f["properties"]["NUTS_ID"] for f in out["features"]], ["DK02", "SE11"]
        )
        self.assertEqual(
            set(out["features"][0]["properties"]), {"NUTS_ID", "NAME_LATN", "CNTR_CODE"}
        )
        self.assertIn("Sjælland", run_writers.filter_boundaries(geo, {"DK02"}))

    def test_boundary_tasks_match_the_prompts_and_ship_no_boundaries(self) -> None:
        named = sorted(
            p.parent.name
            for p in harness.TASKS.glob("*/prompt.txt")
            if "nordic_nuts2.geojson" in p.read_text()
        )
        self.assertEqual(named, sorted(run_writers.NEEDS_BOUNDARIES))
        self.assertEqual(list(harness.TASKS.rglob("*.geojson")), [])

    def test_usage_restore_takes_back_only_the_writers_calls(self) -> None:
        path = self.tmp / "claude.json"
        before = {
            "prose:charts": {"usageCount": 5, "lastUsedAt": 1},
            "prose:prose": {"usageCount": 3, "lastUsedAt": 1},
        }
        after = {
            "prose:charts": {"usageCount": 9, "lastUsedAt": 4},
            "prose:prose": {"usageCount": 5, "lastUsedAt": 4},
            "prose:prose-maintainer": {"usageCount": 1, "lastUsedAt": 4},
        }
        path.write_text(json.dumps({"numStartups": 7, "skillUsage": after}))
        invoked = {"prose:charts": 3, "prose": 2, "prose:prose-maintainer": 1}
        changed = skill_usage.restore_usage(before, invoked, path)
        usage = json.loads(path.read_text())["skillUsage"]
        self.assertEqual(
            sorted(changed), ["prose:charts", "prose:prose", "prose:prose-maintainer"]
        )
        self.assertEqual(usage["prose:charts"], {"usageCount": 6, "lastUsedAt": 4})
        self.assertEqual(usage["prose:prose"], before["prose:prose"])
        self.assertNotIn("prose:prose-maintainer", usage)
        self.assertEqual(json.loads(path.read_text())["numStartups"], 7)

    def test_load_checks_name_the_prose_that_loaded(self) -> None:
        workspace = self.tmp / "ws"
        skills = f"{workspace}/.agents/skills"
        text = (
            f"<skills_instructions>\n- `r1` = `{skills}`\n- `r2` = `{skill_usage.INSTALLED[1]}/0.3.7/skills`\n"
            "- charts: House rules for charts (file: r1/charts/SKILL.md)\n"
            "- other:thing: Unrelated (file: r9/thing/SKILL.md)\n</skills_instructions>"
        )
        rollout = self.tmp / "rollout.jsonl"
        rollout.write_text(
            json.dumps({"payload": {"content": [{"type": "input_text", "text": text}]}})
            + "\n"
        )
        found = skill_usage.codex_prose(rollout)
        self.assertEqual(found, [f"{skills}/charts/SKILL.md"])
        self.assertEqual(skill_usage.which_prose(found, Path(skills)), "candidate")
        self.assertEqual(skill_usage.which_prose(found, None), "none")
        installed = [f"{skill_usage.INSTALLED[0]}/0.3.7"]
        self.assertEqual(skill_usage.which_prose(installed, None), "installed")
        self.assertEqual(
            skill_usage.which_prose(installed + found, Path(skills)), "both"
        )

    def test_delivered_lists_new_and_changed_files_only(self) -> None:
        seat = self.tmp / "runs/label/task/seat"
        ws = seat / "workspace"
        (ws / "data").mkdir(parents=True)
        (ws / "data/kept.csv").write_text("a\n")
        (ws / "data/edited.csv").write_text("b\n")
        inputs = {
            p.relative_to(ws).as_posix(): harness.digest(p)
            for p in ws.rglob("*")
            if p.is_file()
        }
        (seat / "inputs.json").write_text(json.dumps(inputs))
        (ws / "data/edited.csv").write_text("changed\n")
        (ws / "page.html").write_text("<p>New</p>")
        (ws / ".venv").mkdir()
        (ws / ".venv/lib.py").write_text("")
        self.assertEqual(
            [str(p) for p in harness.delivered(seat)], ["data/edited.csv", "page.html"]
        )


if __name__ == "__main__":
    unittest.main()
