"""Check which Prose each writer loaded and list the skills and skill files it used.

Usage: skill_usage.py [--work DIR] LABEL
Reads each run's stream.jsonl (Claude) or rollout.jsonl (Codex) and writes runs/LABEL/skill-usage.md.
"""

import argparse
import json
import os
import re
import secrets
from collections import Counter
from pathlib import Path

from harness import seat_dirs, work_dir

CLAUDE_JSON = Path.home() / ".claude.json"
INSTALLED = (
    Path.home() / ".claude/marketplaces/codex-aligned/plugins/prose",
    Path.home() / ".codex/plugins/cache/personal/prose",
)
SKILL_PATH = re.compile(
    r"((?:[^\s\"'`]*/)?(?<![\w-])skills/([\w.:-]+)/[^\s\"'`]*?\.(?:md|csv|py|js))"
)
CODEX_ROOT = re.compile(r"^- `(r\d+)` = `([^`]+)`$", re.M)
CODEX_SKILL = re.compile(r"^- ([\w:.-]+): .*\(file: ([^)\s]+)\)$", re.M)


def records(path):
    for line in path.read_text(errors="ignore").splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def codex_rollout(session):
    hits = sorted(
        (Path.home() / ".codex" / "sessions").glob(f"*/*/*/rollout-*{session}.jsonl")
    )
    return hits[-1] if hits else None


def tool_calls(path):
    """(tool name, arguments as JSON text) for each tool call in a Claude stream or a Codex rollout."""
    for entry in records(path):
        content = (entry.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "tool_use":
                yield block.get("name"), json.dumps(block.get("input"))
        payload = entry.get("payload") or {}
        if payload.get("type") in (
            "function_call",
            "custom_tool_call",
            "local_shell_call",
        ):
            yield (
                payload.get("name"),
                json.dumps(payload.get("arguments") or payload.get("input") or payload),
            )


def skill_calls(path):
    """Skill tool calls by the skill name the writer passed."""
    return Counter(
        json.loads(args).get("skill")
        for name, args in tool_calls(path)
        if name == "Skill"
    )


def claude_prose(stream):
    """Paths of the plugins named prose in a Claude stream's init event."""
    for entry in records(stream):
        if entry.get("subtype") == "init":
            return [
                p.get("path", "")
                for p in entry.get("plugins") or []
                if p.get("name") == "prose"
            ]
    return []


def codex_prose(rollout):
    """SKILL.md paths of the Prose skills in the skill list Codex gave the model."""
    for entry in records(rollout):
        for item in (entry.get("payload") or {}).get("content") or []:
            text = (item.get("text") or "") if isinstance(item, dict) else ""
            if "<skills_instructions>" not in text:
                continue
            roots = dict(CODEX_ROOT.findall(text))
            found = []
            for name, short in CODEX_SKILL.findall(text):
                if name in ("charts", "prose", "prose-maintainer") or name.startswith(
                    "prose:"
                ):
                    root, _, rest = short.partition("/")
                    found.append(f"{roots[root]}/{rest}" if root in roots else short)
            return found
    return []


def which_prose(paths, candidate):
    """Name what loaded: candidate, installed, both, or none. CANDIDATE is the candidate's root or None."""
    cand = candidate is not None and any(
        Path(p).is_relative_to(candidate) for p in paths
    )
    inst = any(Path(p).is_relative_to(root) for p in paths for root in INSTALLED)
    return {
        (True, False): "candidate",
        (False, True): "installed",
        (True, True): "both",
    }.get((cand, inst), "none")


def usage_snapshot(path=CLAUDE_JSON):
    return json.loads(path.read_text()).get("skillUsage") or {}


def restore_usage(before, invoked, path=CLAUDE_JSON):
    """Take the writers' Skill calls back off Claude's skill usage counters.

    BEFORE is the snapshot from before the runs; INVOKED counts Skill calls by the name the writer
    passed. Counts that other sessions added meanwhile stay. Returns the keys changed.
    """
    data = json.loads(path.read_text())
    usage = data.get("skillUsage") or {}
    changed = []
    for name, calls in invoked.items():
        key = name if name in usage else None
        key = key or next(
            (
                k
                for k in usage
                if k.split(":")[-1] == name and usage[k] != before.get(k)
            ),
            None,
        )
        if key is None:
            continue
        old, count = before.get(key), usage[key]["usageCount"] - calls
        if old is None and count <= 0:
            del usage[key]
        elif old is not None and count <= old["usageCount"]:
            usage[key] = dict(old)
        else:
            usage[key]["usageCount"] = count
        changed.append(key)
    if changed:
        tmp = path.with_name(f"{path.name}.eval-{secrets.token_hex(3)}")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    return changed


def transcript(seat_dir):
    return seat_dir / (
        "stream.jsonl" if (seat_dir / "stream.jsonl").exists() else "rollout.jsonl"
    )


def report(work, label):
    rows = [
        "| Run | Prose loaded | Skill tool | Skill files opened |",
        "| --- | --- | --- | --- |",
    ]
    for seat_dir in seat_dirs(work, label):
        meta = json.loads((seat_dir / "meta.json").read_text())
        path = transcript(seat_dir)
        run = f"{meta['task']}/{meta['seat']}"
        if not path.exists():
            rows.append(f"| {run} | {meta.get('loaded', '?')} | transcript missing | |")
            continue
        invoked = sorted(n for n in skill_calls(path) if n)
        files = sorted(
            {
                f"{skill}: {full.split(f'skills/{skill}/')[-1]}"
                for _, args in tool_calls(path)
                for full, skill in SKILL_PATH.findall(args)
            }
        )
        rows.append(
            f"| {run} | {meta.get('loaded', '?')} | {', '.join(invoked) or '-'} | {'<br>'.join(files) or '-'} |"
        )
    return "\n".join(rows) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("label")
    ap.add_argument("--work")
    a = ap.parse_args()
    work = work_dir(a.work)
    out = work / "runs" / a.label / "skill-usage.md"
    out.write_text(report(work, a.label))
    print(out.read_text(), end="")


if __name__ == "__main__":
    main()
