#!/usr/bin/env python3
"""Measure a deck in desktop PowerPoint on Windows from WSL, and optionally export PowerPoint's own PDF.

    python3 -I powerpoint.py DECK.pptx [--json OUT.json] [--pdf OUT.pdf] [--allow-running]

Copies the deck and measure.ps1 to a job folder in the Windows user's temp folder, runs it there,
copies the results out, and deletes the job folder. Stops with exit 3 when PowerPoint is already
running, unless --allow-running is given, and never quits a PowerPoint it did not start.
"""

import argparse
import json
import pathlib
import secrets
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent


def windows_temp() -> tuple[pathlib.Path, str]:
    done = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "[IO.Path]::GetTempPath()"],
        capture_output=True, text=True, check=True,
    )
    win = done.stdout.strip().rstrip("\\")
    if not win:
        sys.exit("powerpoint.py: Windows did not report a temp folder")
    wsl = subprocess.run(["wslpath", "-u", win], capture_output=True, text=True, check=True).stdout.strip()
    return pathlib.Path(wsl), win


def measure(deck: pathlib.Path, pdf: pathlib.Path | None, allow_running: bool, timeout: int = 900) -> dict:
    temp_wsl, temp_win = windows_temp()
    job = f"pptcom-{secrets.token_hex(4)}"
    work, work_win = temp_wsl / job, f"{temp_win}\\{job}"
    work.mkdir()
    try:
        shutil.copyfile(HERE / "measure.ps1", work / "measure.ps1")
        shutil.copyfile(deck, work / "deck.pptx")
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-File", f"{work_win}\\measure.ps1", "-Name", "deck"]
        if pdf:
            command.append("-Pdf")
        if allow_running:
            command.append("-AllowRunning")
        done = subprocess.run(command, capture_output=True, timeout=timeout, cwd=work)
        out = done.stdout.decode(errors="replace").strip()
        if done.returncode == 3:
            print(f"powerpoint.py: {out}", file=sys.stderr)
            raise SystemExit(3)
        report = work / "deck.json"
        if done.returncode != 0 or not report.exists():
            err = done.stderr.decode(errors="replace").strip()
            sys.exit(f"powerpoint.py: PowerPoint could not measure {deck}: {out[-800:]} {err[-800:]}")
        if pdf:
            shutil.copyfile(work / "deck.pdf", pdf)
        return json.loads(report.read_text(encoding="utf-8-sig"))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if work.exists():
            print(f"powerpoint.py: could not delete the job folder {work}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure a deck in desktop PowerPoint on Windows from WSL.")
    parser.add_argument("deck", type=pathlib.Path)
    parser.add_argument("--json", type=pathlib.Path, help="write the measurements here")
    parser.add_argument("--pdf", type=pathlib.Path, help="export PowerPoint's own PDF here")
    parser.add_argument("--allow-running", action="store_true", help="measure in a PowerPoint that is already open")
    args = parser.parse_args()
    result = measure(args.deck.resolve(), args.pdf.resolve() if args.pdf else None, args.allow_running)
    if args.json:
        args.json.write_text(json.dumps(result), encoding="utf-8")
    texts = [s["text"] for p in result["pages"] for s in p["shapes"] if "text" in s]
    texts += [cell for p in result["pages"] for s in p["shapes"] for row in s.get("rows", []) for cell in row["cells"]]
    splits = sum(len(text.get("splits", [])) for text in texts)
    print(f"PowerPoint {result['version']}: {len(result['pages'])} pages, {splits} words broken across lines")


if __name__ == "__main__":
    main()
