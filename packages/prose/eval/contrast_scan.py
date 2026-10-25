"""List colours below 3:1 that a delivery's code draws with, after any opacity on the same line.

Usage: contrast_scan.py FOLDER [FOLDER ...]
Each FOLDER is a blinded delivery (judge/ROUND/TASK/<letter>). Scans files/ for hex colours in scripts,
styles and markup, composites any alpha or opacity on the same line over the page background, and
prints each colour below 3:1 with its file and line, so a reader can decide whether it is a mark the
reader needs.
"""

import re
import sys
from pathlib import Path

from harness import contrast

SKIP = {".venv", "node_modules", "__pycache__", "data", "_work", ".agents"}
EXTS = {".py", ".css", ".html", ".js", ".mjs", ".jsx", ".json"}
HEX = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
ALPHA = re.compile(
    r"(?:alpha|opacity|fill-opacity|stroke-opacity|globalAlpha)\s*[=:]\s*[\"']?(0?\.\d+)"
)
BG_HINT = re.compile(
    r"background|facecolor|--bg|--surface|--page|--paper|bgcolor|fill: ?var\(--(bg|surface)",
    re.I,
)


def rgb(h):
    h = h if len(h) == 6 else "".join(c * 2 for c in h)
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def background(files):
    for f in files:
        for line in f.read_text(errors="ignore").splitlines():
            if re.search(
                r"(body|:root|html)\b.*background|--bg\s*:|--surface\s*:|--page\s*:",
                line,
            ):
                m = HEX.search(line)
                if m:
                    return rgb(m.group(1))
    return (255, 255, 255)


def scan(folder):
    files = [
        p
        for p in (folder / "files").rglob("*")
        if p.is_file()
        and p.suffix in EXTS
        and not SKIP.intersection(p.relative_to(folder).parts)
        and p.stat().st_size < 400_000
    ]
    bg = background(files)
    hits = []
    for f in files:
        for n, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
            if BG_HINT.search(line):
                continue
            a = ALPHA.search(line)
            alpha = float(a.group(1)) if a else 1.0
            for m in HEX.finditer(line):
                drawn = tuple(
                    alpha * x + (1 - alpha) * y for x, y in zip(rgb(m.group(1)), bg)
                )
                r = contrast(drawn, bg)
                if r < 3:
                    hits.append(
                        (
                            r,
                            f.relative_to(folder / "files"),
                            n,
                            m.group(0),
                            alpha,
                            line.strip()[:110],
                        )
                    )
    return bg, hits


def main():
    for arg in sys.argv[1:]:
        folder = Path(arg)
        bg, hits = scan(folder)
        print(
            f"== {folder.parent.name}/{folder.name}  background #{''.join(f'{x:02x}' for x in bg)}  {len(hits)} occurrences below 3:1"
        )
        seen = set()
        for r, f, n, h, alpha, text in sorted(hits, key=lambda x: x[0]):
            if (h.lower(), alpha) in seen:
                continue
            seen.add((h.lower(), alpha))
            print(
                f"  {r:4.2f}:1  {h}{'' if alpha == 1 else f' @ {alpha}'}  {f}:{n}  {text}"
            )


if __name__ == "__main__":
    main()
