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
import math
import pathlib
import secrets
import shutil
import subprocess
import sys
import time

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


def recorded(pid_file: pathlib.Path) -> int | None:
    """The process ID the job wrote, or None while the file is missing or still empty."""
    try:
        return int(pid_file.read_text(encoding="utf-8-sig").strip())
    except (OSError, ValueError):
        return None


def stop(pid_file: pathlib.Path, program: str) -> dict | None:
    """Stop the process whose ID the job wrote, if it is still the named program and started before the job wrote it.

    A process that started later only reuses the ID of one that has ended. The file's time is rounded up to the
    second, which is as fine as the comparison on Windows is given.
    """
    pid = recorded(pid_file)
    if pid is None:
        return None
    written = math.ceil(pid_file.stat().st_mtime)
    script = (f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
              f"$written = [DateTimeOffset]::FromUnixTimeSeconds({written}).UtcDateTime; "
              f"if (-not $p) {{ 'gone' }} "
              f"elseif ($p.ProcessName -ne '{program}') {{ 'gone; the ID now belongs to ' + $p.ProcessName }} "
              f"elseif ($p.StartTime.ToUniversalTime() -gt $written) {{ 'gone; the ID now belongs to a process started ' + $p.StartTime.ToUniversalTime().ToString('o') + ', after ' + $written.ToString('o') }} "
              f"else {{ $why = ''; try {{ Stop-Process -Id {pid} -Force -ErrorAction Stop }} catch {{ $why = ': ' + $_.Exception.Message }}; "
              f"if ($p.WaitForExit(60000)) {{ 'stopped' }} else {{ 'still running' + $why }} }}")
    try:
        outcome = subprocess.run([*POWERSHELL, "-Command", script], capture_output=True, text=True, timeout=120).stdout.strip()
    except subprocess.TimeoutExpired:
        outcome = "the stop did not answer"
    return {"pid": pid, "program": program, "outcome": outcome or "the stop printed nothing"}


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
        said = b""
        driver = subprocess.Popen([*POWERSHELL, "-ExecutionPolicy", "Bypass", "-File", f"{work_win}\\excel-job.ps1",
                                   "-Job", f"{work_win}\\job.json"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=work)
        try:
            said = driver.communicate(timeout=job_timeout)[1]
        except subprocess.TimeoutExpired:
            runner["timed_out"] = True
            # A limit that falls while Excel is starting finds no process ID yet. Ending PowerShell then would leave
            # that Excel running with nothing to find it by, so give the job a moment to write the ID first.
            deadline = time.monotonic() + 60
            while recorded(work / "excel.pid") is None and driver.poll() is None and time.monotonic() < deadline:
                time.sleep(0.2)
        report = work / "result.json"
        if runner["timed_out"] or not report.exists():
            # The job's PowerShell is past its limit, or ended before it could quit its Excel.
            for pid_file, program in (("excel.pid", "EXCEL"), ("ps.pid", "powershell")):
                if program == "powershell" and driver.poll() is not None:
                    continue  # It has ended, so its ID could only name another process by now.
                outcome = stop(work / pid_file, program)
                if outcome:
                    runner["stopped"].append(outcome)
        if driver.poll() is None:
            driver.kill()
            driver.communicate()
        if report.exists():
            result = json.loads(report.read_text(encoding="utf-8-sig"))
        else:
            why = " (timed out)" if runner["timed_out"] else ": " + (said.decode(errors="replace").strip()[-800:] or "PowerShell printed no error")
            result = {"ok": False, "error": "the job ended without a result" + why}
        if save and result.get("ok"):
            shutil.copyfile(work / "out.xlsx", save)
            result["saved"] = str(save)
        result["runner"] = runner
        return result
    finally:
        for _ in range(20):  # An Excel that has just ended can hold its workbook for a moment longer.
            shutil.rmtree(work, ignore_errors=True)
            if not work.exists():
                break
            time.sleep(0.5)
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
        stages = (result.get("base"), result.get("after"), (result.get("reopened") or {}).get("values"))
        unread = sum("read_error" in entry for stage in stages for entry in (stage or {}).values())
        print(f"excel.py: ok={result.get('ok')} load={load} errors={len(result.get('errors') or [])} read_errors={unread} error={result.get('error')}")
    else:
        print(json.dumps(result, indent=1))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
