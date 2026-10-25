"""Render each run's delivered files to images for grading.

Usage: render.py [--work DIR] LABEL[/TASK] [LABEL[/TASK] ...]
Writes renders/LABEL/TASK/SEAT/: full-page screenshots of HTML pages at 390 px and 1366 px through
agent-browser, PDFs and page images of Office files through LibreOffice and pdftoppm, Vega-Lite specs
through vl-convert, and copies of delivered images, with deliverables.txt listing what the writer made.
"""

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

from harness import Browser, delivered, files_of, specs, work_dir

WIDTHS = (("phone", 390), ("desktop", 1366))


def office(src, out, stem):
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmp, str(src)],
            capture_output=True,
            timeout=300,
        )
        pdfs = list(Path(tmp).glob("*.pdf"))
        if pdfs:
            shutil.copy(pdfs[0], out / f"{stem}.pdf")
            subprocess.run(
                ["pdftoppm", "-r", "110", "-png", str(pdfs[0]), str(out / stem)],
                timeout=300,
            )


def vega(python, spec, png):
    code = "import sys, vl_convert as v; open(sys.argv[2], 'wb').write(v.vegalite_to_png(open(sys.argv[1]).read(), scale=2))"
    subprocess.run(
        [str(python), "-I", "-c", code, str(spec), str(png)],
        capture_output=True,
        timeout=120,
    )


def is_vega(path):
    head = path.read_text(errors="ignore")[:400]
    return '"$schema"' in head and "vega" in head


def render_run(work, seat_dir, browser):
    task, seat = seat_dir.parent.name, seat_dir.name
    out = work / "renders" / seat_dir.parent.parent.name / task / seat
    out.mkdir(parents=True, exist_ok=True)
    files = list(delivered(seat_dir))
    (out / "deliverables.txt").write_text("".join(f"{rel}\n" for rel in files))
    for rel in files:
        src = files_of(seat_dir) / rel
        stem = rel.with_suffix("").as_posix().replace("/", "__")
        ext = src.suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".svg"):
            shutil.copy(src, out / f"{stem}{ext}")
        elif ext in (".html", ".htm"):
            browser("open", src.resolve().as_uri())
            for name, width in WIDTHS:
                browser.at_width(width)
                browser("screenshot", "--full", str(out / f"{stem}__{name}.png"))
        elif ext in (".xlsx", ".xlsm", ".pptx", ".docx"):
            office(src, out, stem)
        elif ext == ".json" and is_vega(src):
            vega(work / ".venv" / "bin" / "python", src, out / f"{stem}__vl.png")
    return f"{task}/{seat}: {len(files)} files"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs", nargs="+", metavar="LABEL[/TASK]")
    ap.add_argument("--work")
    a = ap.parse_args()
    work = work_dir(a.work)
    with Browser("render") as browser:
        for seat_dir in specs(work, a.runs):
            print(render_run(work, seat_dir, browser), flush=True)


if __name__ == "__main__":
    main()
