#!/usr/bin/env python3
"""Check a workbook in desktop Excel on Windows from WSL.

    python3 -I excel.py WORKBOOK.xlsx [--read REF]... [--set REF=VALUE]... [--save OUT.xlsx] [--json OUT.json]
                        [--job-timeout 300] [--calc-timeout 120]

REF is a workbook-scoped name or a Sheet!A1 / Sheet!A1:B9 range. Excel opens a copy in a hidden instance of its own,
rebuilds the calculation and reads each REF. With --set it changes those cells, recalculates and reads again. It then
saves the copy, reopens it and reads once more. Prints the result as JSON, or writes it to --json and prints one line.
Exits 0 when Excel finished the job and 1 when it did not.
"""

import argparse
import json
import pathlib
import secrets
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
POWERSHELL = ["powershell.exe", "-NoProfile", "-NonInteractive"]


def value(text: str):
    for kind in (int, float):
        try:
            return kind(text)
        except ValueError:
            pass
    return {"true": True, "false": False, "null": None}.get(text.lower(), text)


def windows_temp() -> tuple[pathlib.Path, str]:
    done = subprocess.run([*POWERSHELL, "-Command", "[IO.Path]::GetTempPath()"], capture_output=True, text=True, check=True)
    win = done.stdout.strip().rstrip("\\")
    if not win:
        sys.exit("excel.py: Windows did not report a temp folder")
    wsl = subprocess.run(["wslpath", "-u", win], capture_output=True, text=True, check=True).stdout.strip()
    return pathlib.Path(wsl), win


def stop(pid_file: pathlib.Path, program: str) -> dict | None:
    """Stop the process whose ID the job wrote, if it is still the named program."""
    try:
        pid = int(pid_file.read_text(encoding="utf-8-sig").strip())
    except (OSError, ValueError):
        return None
    script = (f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
              f"if ($p -and $p.ProcessName -eq '{program}') {{ Stop-Process -Id {pid} -Force; 'stopped' }} else {{ 'gone' }}")
    done = subprocess.run([*POWERSHELL, "-Command", script], capture_output=True, text=True, timeout=60)
    return {"pid": pid, "program": program, "outcome": done.stdout.strip()}


def run_job(book: pathlib.Path, read: list[str], sets: list[dict], save: pathlib.Path | None,
            job_timeout: int, calc_timeout: int, hold: int) -> dict:
    temp_wsl, temp_win = windows_temp()
    name = f"xlcom-{secrets.token_hex(4)}"
    work, work_win = temp_wsl / name, f"{temp_win}\\{name}"
    work.mkdir()
    runner: dict = {"job_timeout_s": job_timeout, "timed_out": False, "stopped": []}
    try:
        shutil.copyfile(HERE / "excel-job.ps1", work / "excel-job.ps1")
        shutil.copyfile(book, work / "in.xlsx")
        spec = {"workbook": f"{work_win}\\in.xlsx", "out": f"{work_win}\\out.xlsx", "read": read, "set": sets,
                "calc_timeout_s": calc_timeout, "hold_s": hold}
        (work / "job.json").write_text(json.dumps(spec), encoding="utf-8")
        try:
            subprocess.run([*POWERSHELL, "-ExecutionPolicy", "Bypass", "-File", f"{work_win}\\excel-job.ps1",
                            "-Job", f"{work_win}\\job.json"], capture_output=True, timeout=job_timeout, cwd=work)
        except subprocess.TimeoutExpired:
            runner["timed_out"] = True
            for pid_file, program in (("excel.pid", "EXCEL"), ("ps.pid", "powershell")):
                outcome = stop(work / pid_file, program)
                if outcome:
                    runner["stopped"].append(outcome)
        report = work / "result.json"
        if report.exists():
            result = json.loads(report.read_text(encoding="utf-8-sig"))
        else:
            result = {"ok": False, "error": "the job ended without a result" + (" (timed out)" if runner["timed_out"] else "")}
        if save and result.get("ok"):
            shutil.copyfile(work / "out.xlsx", save)
            result["saved"] = str(save)
        result["runner"] = runner
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if work.exists():
            print(f"excel.py: could not delete the job folder {work}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a workbook in desktop Excel on Windows from WSL.")
    parser.add_argument("workbook", type=pathlib.Path)
    parser.add_argument("--read", action="append", default=[], metavar="REF")
    parser.add_argument("--set", action="append", default=[], metavar="REF=VALUE")
    parser.add_argument("--save", type=pathlib.Path, help="keep the copy Excel saved here")
    parser.add_argument("--json", type=pathlib.Path, help="write the result here")
    parser.add_argument("--job-timeout", type=int, default=300)
    parser.add_argument("--calc-timeout", type=int, default=120)
    parser.add_argument("--test-hold", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.workbook.is_file():
        parser.error(f"{args.workbook} is not a file")
    sets = []
    for item in args.set:
        ref, sep, text = item.partition("=")
        if not sep:
            parser.error(f"--set needs REF=VALUE, not {item}")
        sets.append({"ref": ref, "value": value(text)})
    result = run_job(args.workbook.resolve(), args.read, sets, args.save.resolve() if args.save else None,
                     args.job_timeout, args.calc_timeout, args.test_hold)
    if args.json:
        args.json.write_text(json.dumps(result, indent=1), encoding="utf-8")
        load = (result.get("open") or {}).get("load")
        print(f"excel.py: ok={result.get('ok')} load={load} errors={len(result.get('errors') or [])} error={result.get('error')}")
    else:
        print(json.dumps(result, indent=1))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
