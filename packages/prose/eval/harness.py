"""Paths and helpers shared by the evaluation tools.

Tasks live beside this file in tasks/. Everything a run produces goes to the work directory: --work,
else $PROSE_EVAL_WORK, else ~/prose-eval. Runs are kept as runs/LABEL/TASK/SEAT/ with a meta.json.
"""

import hashlib
import json
import os
import secrets
import subprocess
from pathlib import Path

EVAL = Path(__file__).resolve().parent
TASKS = EVAL / "tasks"
SKIP_PARTS = frozenset(
    {".venv", "node_modules", "__pycache__", ".git", ".agents", ".claude"}
)


def work_dir(arg=None):
    return Path(
        arg or os.environ.get("PROSE_EVAL_WORK") or Path.home() / "prose-eval"
    ).expanduser()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seat_dirs(work, label, task=None):
    """Finished runs of LABEL, optionally only TASK, as runs/LABEL/TASK/SEAT folders."""
    pattern = f"{task}/*/meta.json" if task else "*/*/meta.json"
    return [m.parent for m in sorted((work / "runs" / label).glob(pattern))]


def specs(work, args):
    """Expand LABEL or LABEL/TASK arguments into finished seat folders.

    An argument that is a folder, such as a blinded task in judge/ROUND/TASK, stands for itself or the
    lettered runs inside it.
    """
    for arg in args:
        folder = Path(arg).expanduser()
        if folder.is_dir():
            yield from (
                [folder]
                if (folder / "inputs.json").exists()
                else sorted(p.parent for p in folder.glob("*/inputs.json"))
            )
            continue
        label, _, task = arg.partition("/")
        yield from seat_dirs(work, label, task or None)


def files_of(seat_dir):
    """A run's workspace: workspace/ in a run, files/ in a blinded copy."""
    return seat_dir / ("workspace" if (seat_dir / "workspace").exists() else "files")


def delivered(seat_dir):
    """Files the writer created or changed, relative to its workspace.

    inputs.json holds the digest of each file the workspace started with, fetched files included.
    """
    workspace = files_of(seat_dir)
    inputs = json.loads((seat_dir / "inputs.json").read_text())
    for path in sorted(workspace.rglob("*")):
        rel = path.relative_to(workspace)
        if (
            not path.is_file()
            or path.is_symlink()
            or SKIP_PARTS.intersection(rel.parts)
        ):
            continue
        if any("venv" in part or part == "site-packages" for part in rel.parts):
            continue
        if inputs.get(rel.as_posix()) != digest(path):
            yield rel


def pages(seat_dir):
    """Delivered HTML pages, leaving out templates."""
    for rel in delivered(seat_dir):
        if rel.suffix in (".html", ".htm") and "template" not in rel.name:
            yield rel


def lum(rgb):
    """WCAG relative luminance of a colour with channels from 0 to 255."""
    lin = [
        c / 255 / 12.92 if c / 255 <= 0.04045 else ((c / 255 + 0.055) / 1.055) ** 2.4
        for c in rgb
    ]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    hi, lo = sorted([lum(a), lum(b)], reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class Browser:
    """An agent-browser session of our own, closed when the block ends."""

    def __init__(self, name):
        self.session = f"{name}-{secrets.token_hex(3)}"

    def __call__(self, *args, stdin=None):
        out = subprocess.run(
            ["agent-browser", "--session", self.session, *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return out.stdout.strip()

    def at_width(self, width, wait_ms=1500):
        self("set", "viewport", str(width), "900")
        self("reload")
        self("wait", str(wait_ms))

    def json(self, script):
        """Run a script that returns JSON.stringify(...) and parse its result."""
        lines = self("eval", "--stdin", stdin=script).splitlines()
        return json.loads(json.loads(lines[-1])) if lines else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self("close")
