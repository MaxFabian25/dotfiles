"""Run writer seats on tasks in fresh workspaces and keep what they produce.

Usage: run_writers.py LABEL TASK[,TASK...] SEAT[,SEAT...] [--candidate PLUGIN_DIR] [--parallel N]
                      [--done FILE] [--work DIR]

Seats named opus-* run claude -p; any other seat, such as gpt or gpt-2, runs codex exec. Without
--candidate, writers use the installed Prose. With it, Claude loads a copy of PLUGIN_DIR through
--plugin-dir, and Codex finds its skills in the workspace's .agents/skills; both hosts have the
installed Prose turned off. One probe per host checks which Prose loads before any writer starts, and
each writer's own transcript is checked again afterwards. Runs land in WORK/runs/LABEL/TASK/SEAT.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from harness import EVAL, TASKS, delivered, digest, work_dir
from skill_usage import (
    claude_prose,
    codex_prose,
    codex_rollout,
    records,
    restore_usage,
    skill_calls,
    usage_snapshot,
    which_prose,
)

CLAUDE_BASE = [
    "claude", "-p", "--model", "claude-opus-5-5", "--effort", "high", "--permission-mode", "bypassPermissions",
    "--no-session-persistence", "--disallowedTools", "WebFetch", "WebSearch", "--output-format", "stream-json",
    "--verbose",
]  # fmt: skip
CODEX_BASE = [
    "codex", "exec", "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"', "-c", 'web_search="disabled"',
    "-c", "notify=[]", "--skip-git-repo-check",
]  # fmt: skip
# The installed Prose, turned off when a candidate runs. Codex wants the key unquoted.
INSTALLED_OFF = {
    "claude": ["--settings", '{"enabledPlugins":{"prose@codex-aligned":false}}'],
    "codex": ["-c", "plugins.prose@personal.enabled=false"],
}
BOUNDARIES = "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/NUTS_RG_20M_2024_4326_LEVL_2.geojson"
# Tasks given data/nordic_nuts2.geojson. GISCO's licence keeps the file out of the repository, so it is
# fetched into WORK/sources and cut down to the task's regions.
NEEDS_BOUNDARIES = ("club-growth", "club-map")
STARTED = []  # run folders this invocation started, whose Skill calls come off Claude's usage counts


def host(seat):
    return "claude" if seat.startswith("opus") else "codex"


def child_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}


def tree_digest(root):
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.name != ".DS_Store":
            h.update(f"{p.relative_to(root).as_posix()} {digest(p)}\n".encode())
    return h.hexdigest()


def claude_manifest(codex_manifest, skills):
    keep = ("name", "version", "description", "author", "repository")
    return {
        **{k: codex_manifest[k] for k in keep if k in codex_manifest},
        "skills": [f"./skills/{s}" for s in skills],
    }


def build_candidate(source, dest):
    """Copy a plugin source to DEST with a Claude manifest made from its Codex one.

    A label keeps one candidate: a second run of the label with different skills stops.
    """
    if dest.exists():
        if tree_digest(dest / "skills") != tree_digest(source / "skills"):
            raise SystemExit(
                f"{dest.name} already ran with different skills; choose a new label"
            )
        return dest
    ignore = shutil.ignore_patterns("__pycache__", ".DS_Store")
    shutil.copytree(source / "skills", dest / "skills", ignore=ignore)
    codex = json.loads((source / ".codex-plugin" / "plugin.json").read_text())
    skills = sorted(p.name for p in (dest / "skills").iterdir() if p.is_dir())
    (dest / ".claude-plugin").mkdir()
    (dest / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(claude_manifest(codex, skills), indent=2) + "\n"
    )
    return dest


def filter_boundaries(geo, ids):
    """The GISCO features for IDS, sorted, with only the properties the tasks describe."""
    feats = sorted(
        (f for f in geo["features"] if f["properties"]["NUTS_ID"] in ids),
        key=lambda f: f["properties"]["NUTS_ID"],
    )
    for f in feats:
        p = f["properties"]
        f["properties"] = {
            "NUTS_ID": p["NUTS_ID"],
            "NAME_LATN": p["NAME_LATN"],
            "CNTR_CODE": p["CNTR_CODE"],
        }
    return json.dumps(
        {"type": "FeatureCollection", "features": feats}, ensure_ascii=False
    )


def boundaries(work, task):
    cache = work / "sources" / BOUNDARIES.rsplit("/", 1)[1]
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(BOUNDARIES, timeout=300) as response:
            part = cache.with_suffix(".part")
            part.write_bytes(response.read())
        part.replace(cache)
    with (TASKS / task / "data" / "club_members.csv").open(encoding="utf-8") as f:
        ids = {row["nuts_id"] for row in csv.DictReader(f)}
    return filter_boundaries(json.loads(cache.read_text(encoding="utf-8")), ids)


def writer_venv(work):
    venv = work / ".venv"
    if not (venv / "bin" / "python").exists():
        subprocess.run(["uv", "venv", "--python", "3.12", str(venv)], check=True)
        packages = EVAL / "writer-requirements.txt"
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(venv / "bin" / "python"),
                "-r",
                str(packages),
            ],
            check=True,
        )


def workspace(work, task, seat, plugin):
    """A fresh workspace and the digest of each file it starts with."""
    ws = work / "workspaces" / f"{task or 'probe'}-{secrets.token_hex(3)}"
    if task:
        shutil.copytree(TASKS / task, ws, ignore=shutil.ignore_patterns("prompt.txt"))
    else:
        ws.mkdir(parents=True)
    if task in NEEDS_BOUNDARIES:
        (ws / "data" / "nordic_nuts2.geojson").write_text(
            boundaries(work, task), encoding="utf-8"
        )
    inputs = {
        p.relative_to(ws).as_posix(): digest(p)
        for p in sorted(ws.rglob("*"))
        if p.is_file()
    }
    (ws / ".venv").symlink_to(work / ".venv")
    if plugin and host(seat) == "codex":
        shutil.copytree(plugin / "skills", ws / ".agents" / "skills")
    return ws, inputs


def run_claude(ws, out, prompt, plugin):
    cmd = (
        CLAUDE_BASE
        + (["--plugin-dir", str(plugin), *INSTALLED_OFF["claude"]] if plugin else [])
        + [prompt]
    )
    with (out / "stream.jsonl").open("w") as so, (out / "stderr.log").open("w") as se:
        rc = subprocess.run(
            cmd,
            cwd=ws,
            stdin=subprocess.DEVNULL,
            stdout=so,
            stderr=se,
            env=child_env(),
            timeout=3600,
        ).returncode
    result = next(
        (e for e in records(out / "stream.jsonl") if e.get("type") == "result"), {}
    )
    (out / "final.md").write_text(result.get("result") or "")
    return (
        rc,
        result.get("session_id"),
        which_prose(claude_prose(out / "stream.jsonl"), plugin),
    )


def run_codex(ws, out, prompt, plugin):
    cmd = (
        CODEX_BASE
        + (INSTALLED_OFF["codex"] if plugin else [])
        + ["-C", str(ws), "-o", str(out / "final.md"), "-"]
    )
    with (out / "codex.log").open("w") as so, (out / "stderr.log").open("w") as se:
        rc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            stdout=so,
            stderr=se,
            env=child_env(),
            timeout=3600,
        ).returncode
    logs = (out / "codex.log").read_text(errors="ignore") + (
        out / "stderr.log"
    ).read_text(errors="ignore")
    match = re.search(r"^session id: (\S+)", logs, re.M)
    session = match.group(1) if match else None
    rollout = codex_rollout(session) if session else None
    if rollout:
        shutil.copy(rollout, out / "rollout.jsonl")
    if not (out / "final.md").exists():
        (out / "final.md").write_text("")
    loaded = codex_prose(out / "rollout.jsonl") if rollout else []
    return (
        rc,
        session,
        which_prose(loaded, ws / ".agents" / "skills" if plugin else None),
    )


def bundle(seat_dir):
    """The changed or new text files, then the final message, for reading a prose run in one place."""
    parts = []
    for rel in delivered(seat_dir):
        data = (seat_dir / "workspace" / rel).read_bytes()
        try:
            parts.append(f"## File: {rel}\n\n{data.decode('utf-8')}")
        except UnicodeDecodeError:
            parts.append(f"## File: {rel}\n\n(binary, {len(data)} bytes)")
    parts.append(f"## Final message\n\n{(seat_dir / 'final.md').read_text()}")
    return "\n\n".join(parts) + "\n"


def run_one(work, label, task, seat, plugin):
    out = work / "runs" / label / task / seat
    if (out / "meta.json").exists():
        return f"skip {label}/{task}/{seat}"
    if out.exists():
        shutil.rmtree(out)  # an unfinished earlier attempt
    out.mkdir(parents=True)
    STARTED.append(out)
    ws, inputs = workspace(work, task, seat, plugin)
    (out / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    start = time.time()
    run = run_claude if host(seat) == "claude" else run_codex
    rc, session, loaded = run(
        ws, out, (TASKS / task / "prompt.txt").read_text(), plugin
    )
    shutil.copytree(
        ws,
        out / "workspace",
        symlinks=True,
        ignore=shutil.ignore_patterns(".venv", ".agents"),
    )
    shutil.rmtree(ws)
    (out / "deliverable.md").write_text(bundle(out))
    expected = "candidate" if plugin else "installed"
    meta = {
        "label": label, "task": task, "seat": seat, "host": host(seat), "candidate": str(plugin) if plugin else None,
        "session": session, "returncode": rc, "seconds": round(time.time() - start), "loaded": loaded,
    }  # fmt: skip
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    warning = (
        ""
        if loaded == expected
        else f"  WARNING: Prose loaded: {loaded}, expected {expected}"
    )
    return f"done {label}/{task}/{seat} rc={rc} {meta['seconds']}s{warning}"


def probe(work, label, seat, plugin):
    """Start a writer's setup with a one-line prompt and stop unless the intended Prose loaded."""
    out = work / "probes" / f"{label}-{host(seat)}-{secrets.token_hex(3)}"
    out.mkdir(parents=True)
    ws, _ = workspace(work, None, seat, plugin)
    run = run_claude if host(seat) == "claude" else run_codex
    rc, _, loaded = run(ws, out, "Reply with OK.", plugin)
    shutil.rmtree(ws)
    expected = "candidate" if plugin else "installed"
    if rc != 0 or loaded != expected:
        raise SystemExit(
            f"{host(seat)} probe: exit {rc}, Prose loaded: {loaded}, expected {expected}. See {out}"
        )
    shutil.rmtree(out)
    return f"{host(seat)} loads the {loaded} Prose"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("label")
    ap.add_argument("tasks")
    ap.add_argument("seats")
    ap.add_argument(
        "--candidate",
        type=Path,
        help="plugin source directory, such as packages/prose/plugin",
    )
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument(
        "--done", type=Path, help="write the results here when every seat has finished"
    )
    ap.add_argument("--work")
    a = ap.parse_args()
    work = work_dir(a.work)
    tasks, seats = a.tasks.split(","), a.seats.split(",")
    missing = [t for t in tasks if not (TASKS / t / "prompt.txt").exists()]
    if missing:
        raise SystemExit(f"no such task: {', '.join(missing)}")
    work.mkdir(parents=True, exist_ok=True)
    writer_venv(work)
    plugin = (
        build_candidate(a.candidate.resolve(), work / "plugins" / a.label)
        if a.candidate
        else None
    )
    first = {host(s): s for s in reversed(seats)}
    for seat in first.values():
        print(probe(work, a.label, seat, plugin), flush=True)
    before = usage_snapshot() if "claude" in first else None
    results = []
    try:
        with ThreadPoolExecutor(a.parallel) as ex:
            futures = [
                ex.submit(run_one, work, a.label, t, s, plugin)
                for t in tasks
                for s in seats
            ]
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except (
                    Exception
                ) as e:  # keep the other seats running and report this one
                    results.append(f"failed: {e!r}")
                print(results[-1], flush=True)
    finally:
        if before is not None:
            calls = sum(
                (
                    skill_calls(out / "stream.jsonl")
                    for out in STARTED
                    if (out / "stream.jsonl").exists()
                ),
                Counter(),
            )
            changed = restore_usage(before, calls)
            if changed:
                print(
                    f"took the writers' calls back off Claude's usage counts for {', '.join(changed)}",
                    flush=True,
                )
        if a.done:
            a.done.write_text("\n".join(results) + "\n")


if __name__ == "__main__":
    main()
