# Evaluate a Prose change

These tools run writers in Claude Code and Codex on fixed tasks, with the installed Prose or a
candidate, and help grade what they produce. [CONTRIBUTING.md](../CONTRIBUTING.md#check-the-change)
says when to run a comparison and how to judge it.

Run the tools from this directory with `python3 -B`. They need `claude`, `codex` and `uv`.
Rendering and measuring pages also need `agent-browser` with Google Chrome, and Office files need
LibreOffice (`soffice`) and Poppler (`pdftoppm`).

## Tasks

`tasks/` holds 23 tasks, each a folder with `prompt.txt`, the request a user would type, and the
files the writer starts with. Twelve ask for charts, tables and dashboards: `board-slide`,
`board-variance`, `chart-review`, `club-growth`, `club-map`, `ebit-ibcs`, `energy-mix`,
`line-report`, `ops-dashboard`, `rate-table`, `release-table` and `store-scores`. Eleven ask for
prose: `cronlet-docs`, `fenmoor-conversion`, `fenmoor-summary`, `fieldnote-widget`,
`harbourline-notes`, `harbourline-reservation`, `kasse-conformance`, `kasse-guide`,
`ledgerly-help`, `staff-guide` and `tidepool-backup`. The companies and data are invented, except the
population figures in the club tasks, which are Eurostat's `demo_r_d2jan` (CC BY 4.0).

The club tasks also give writers `data/nordic_nuts2.geojson`, Eurostat GISCO's NUTS 2024 boundaries
(© EuroGeographics for the administrative boundaries). GISCO grants a non-transferable right to use
them for non-commercial purposes, so the file stays out of this repository: `run_writers.py`
downloads it once into the work directory and cuts it down to the task's regions.

## Work directory

Everything the tools write goes to `~/prose-eval`, or to `$PROSE_EVAL_WORK` or `--work DIR` when
set:

| Path | Contents |
| --- | --- |
| `runs/LABEL/TASK/SEAT/` | A finished run: `meta.json`, `final.md`, `deliverable.md`, `workspace/`, `inputs.json`, and `stream.jsonl` (Claude) or `rollout.jsonl` (Codex) |
| `renders/LABEL/TASK/SEAT/` | Images of what the writer delivered |
| `judge/ROUND/` | Blinded copies for grading, and `key.json` |
| `plugins/LABEL/` | The candidate as Claude loads it |
| `.venv`, `sources/` | The writers' Python environment and the downloaded boundaries |

Keep each round's plan and results in the work directory too. A label names one candidate:
`run_writers.py` stops when a label's earlier runs used different skills.

## Run writers

```sh
python3 -B run_writers.py maps-v4 club-growth,club-map opus-1,opus-2,gpt --candidate ../plugin
```

Seats named `opus-*` run `claude -p` with Opus at high effort; any other seat, such as `gpt`, runs
`codex exec`. Each task and seat is one run, so this example starts six. Up to three run at a time;
change that with `--parallel`. `--done FILE` writes the results to FILE when every run has ended,
for a waiter. Leave out `--candidate` to run the installed Prose.

The runner:

- creates the writers' Python environment from `writer-requirements.txt` on first use;
- with `--candidate`, copies the plugin to `plugins/LABEL` with a Claude manifest, loads it through
  `--plugin-dir` in Claude and through `.agents/skills` in each Codex workspace, and turns off the
  installed Prose in both;
- runs one short probe per host first and stops unless the intended Prose loaded, then records the
  Prose each writer's transcript shows in its `meta.json` as `loaded`;
- starts each run in a fresh workspace outside the repository, with standard input closed, `CLAUDE*`
  variables removed and standard error in its own file;
- skips runs that already have a `meta.json`, so a rerun finishes an interrupted round;
- takes the writers' Skill calls back off the usage counts in `~/.claude.json`, which Claude uses to
  rank skills, keeping counts that other sessions added meanwhile.

## Grade

```sh
python3 -B render.py maps-v4
python3 -B skill_usage.py maps-v4
python3 -B blind.py r17 17 prose-037,maps-v4 club-growth club-map
python3 -B measure.py --map --out ~/prose-eval/judge/r17/measure.md ~/prose-eval/judge/r17/club-map
python3 -B contrast_scan.py ~/prose-eval/judge/r17/club-map/*
python3 -B mech_checks.py maps-v4
```

- `render.py` takes full-page screenshots of each HTML page at 390 px and 1366 px and converts
  Office files and Vega-Lite specs to images.
- `skill_usage.py` lists, per run, which Prose loaded, the skills called and the skill files opened.
- `blind.py` copies the runs of one or more labels into lettered folders in a seeded random order.
  Grade the folders before opening `key.json`.
- `measure.py` reports each page's scroll width and smallest text at both widths, and with `--map`
  the map's region fills and outlines with their contrast. It takes labels or blinded folders.
- `contrast_scan.py` lists the colours under 3:1 in a blinded delivery's code.
- `mech_checks.py` runs pattern checks on the prose tasks.

`make eval-test`, from `packages/prose`, checks `measure.py` against pages with known values.
