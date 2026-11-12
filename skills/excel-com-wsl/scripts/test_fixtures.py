#!/usr/bin/env python3
"""Prove excel.py in desktop Excel with workbooks that have each tripped its COM layer. Needs openpyxl.

    python3 -I test_fixtures.py EVIDENCE_DIR

Prints PASS or FAIL per check and exits 1 on any FAIL. EVIDENCE_DIR must not exist yet, and keeps each job's JSON.
"""

import importlib.util
import json
import math
import os
import pathlib
import secrets
import shutil
import subprocess
import sys
import time
import zipfile

import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName

HERE = pathlib.Path(__file__).resolve().parent
EXCEL = HERE / "excel.py"
POWERSHELL = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
failures = 0


def check(name: str, passed: bool, detail: object = "") -> None:
    global failures
    failures += not passed
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + ("" if passed else f"  {detail}"))


def near(entry: dict | None, expected: float) -> bool:
    value = (entry or {}).get("value")
    return isinstance(value, (int, float)) and math.isclose(value, expected, rel_tol=1e-9)


def build(out: pathlib.Path) -> None:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Model"
    sheet["A1"], sheet["B1"] = "Growth", 0.05
    sheet["A2"], sheet["B2"] = "Revenue, year 1", 100
    sheet["A3"], sheet["B3"] = "Revenue, year 2", "=B2*(1+Growth)"
    sheet["A4"], sheet["B4"] = "Revenue, year 3", "=B3*(1+Growth)"
    book.defined_names["Growth"] = DefinedName("Growth", attr_text="Model!$B$1")
    book.defined_names["Result"] = DefinedName("Result", attr_text="Model!$B$4")
    book.save(out / "tiny.xlsx")
    book.calculation.forceFullCalc = True
    book.save(out / "forcefull.xlsx")

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "S"
    sheet["A1"], sheet["A2"], sheet["A3"] = "=1/0", "=NA()", 5
    book.save(out / "errors.xlsx")

    with zipfile.ZipFile(out / "tiny.xlsx") as source, zipfile.ZipFile(out / "damaged.xlsx", "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            target.writestr(item, data[: len(data) // 2] if item.filename == "xl/worksheets/sheet1.xml" else data)

    # Large enough that a rebuild takes seconds, which is when Excel turns calls away. The read range sits on a
    # protected sheet, as it did when a read after the reopen first failed.
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Controls"
    sheet["A1"], sheet["B1"] = "Rate", 0.05
    for row in range(10, 16):
        for column in "BCD":
            sheet[f"{column}{row}"] = "=ROW()*Rate+COLUMN()"
    book.defined_names["Rate"] = DefinedName("Rate", attr_text="Controls!$B$1")
    sheet.protection.sheet = True
    sheet.protection.password = "synthetic"
    for number in (1, 2):
        data = book.create_sheet(f"Data{number}")
        for row in range(1, 401):
            data.cell(row, 1, row)
            for column in range(2, 202):
                left = f"{get_column_letter(column - 1)}{row}"
                data.cell(row, column, f"=SUMPRODUCT(($A$1:$A$400>{left})*1)*Rate+RAND()+OFFSET({left},0,0)")
    book.save(out / "busy.xlsx")


def excel_count() -> str:
    done = subprocess.run([*POWERSHELL, "@(Get-Process EXCEL -ErrorAction SilentlyContinue).Count"],
                          capture_output=True, text=True, timeout=60)
    return done.stdout.strip()


def windows_temp() -> tuple[pathlib.Path, str]:
    done = subprocess.run([*POWERSHELL, "[IO.Path]::GetTempPath()"], capture_output=True, text=True, check=True, timeout=60)
    win = done.stdout.strip().rstrip("\\")
    wsl = subprocess.run(["wslpath", "-u", win], capture_output=True, text=True, check=True).stdout.strip()
    return pathlib.Path(wsl), win


def job_folders(temp: pathlib.Path) -> list[str]:
    return sorted(path.name for path in temp.glob("xlcom-*"))


def job_folder(temp: pathlib.Path, known: list[str], book: pathlib.Path) -> tuple[pathlib.Path, int, int]:
    """The folder of the job this run just started on `book`, with the process IDs of its Excel and its PowerShell.

    The folder is this job's when it was not in `known`, the folders there just before the job started, and its
    workbook copy has the bytes of the workbook this run built, which no other session's job has. The job is under
    way once it has written both process IDs.
    """
    wanted = book.read_bytes()
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        for name in job_folders(temp):
            try:
                if name not in known and (temp / name / "in.xlsx").read_bytes() == wanted:
                    return temp / name, *(int((temp / name / file).read_text(encoding="utf-8-sig").strip()) for file in ("excel.pid", "ps.pid"))
            except (OSError, ValueError):
                pass  # The job is still copying the workbook or writing an ID.
        time.sleep(0.2)
    sys.exit(f"test_fixtures.py: no job folder for {book.name} appeared")


def process_name(pid: int) -> str:
    done = subprocess.run([*POWERSHELL, f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).ProcessName"],
                          capture_output=True, text=True, timeout=60)
    return done.stdout.strip()


def stop_process(pid: int, program: str) -> None:
    """Stop a process whose ID a job of this run wrote, if it is still the named program."""
    subprocess.run([*POWERSHELL, f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
                    f"if ($p -and $p.ProcessName -eq '{program}') {{ Stop-Process -Id {pid} -Force; Wait-Process -Id {pid} -Timeout 30 -ErrorAction SilentlyContinue }}"],
                   capture_output=True, text=True, timeout=90)


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    evidence = pathlib.Path(sys.argv[1]).resolve()
    evidence.mkdir(parents=True)
    build(evidence)

    temp, temp_win = windows_temp()

    def start(name: str, book: str, *args: str) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-I", str(EXCEL), str(evidence / book), "--json", str(evidence / f"{name}.json"), *args],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def finish(name: str, running: subprocess.Popen) -> tuple[dict, int]:
        code = running.wait(timeout=500)
        return json.loads((evidence / f"{name}.json").read_text(encoding="utf-8")), code

    def job(name: str, book: str, *args: str) -> tuple[dict, int]:
        return finish(name, start(name, book, *args))

    before, folders_before = excel_count(), job_folders(temp)

    saved = evidence / "tiny-saved.xlsx"
    result, code = job("tiny", "tiny.xlsx", "--read", "Growth", "--read", "Result", "--read", "Model!B2:B4",
                       "--set", "Growth=0.1", "--save", str(saved))
    check("a file with no cached values rebuilds", code == 0 and near(result["base"]["Result"], 110.25), result.get("base"))
    check("a named input drives the result", near((result.get("after") or {}).get("Result"), 121), result.get("after"))
    check("the change survives a save and reopen", near(result["reopened"]["values"]["Result"], 121), result.get("reopened"))
    check("a formula stays a formula", result["base"]["Result"].get("formula") == "=B3*(1+Growth)", result["base"]["Result"])
    check("a multi-cell range reads as rows", result["base"]["Model!B2:B4"].get("values") == [[100], [105], [110.25]], result["base"]["Model!B2:B4"])
    check("a clean file loads normally", result["open"]["load"] == "normal", result["open"])
    cached = openpyxl.load_workbook(saved, data_only=True)["Model"]["B4"].value
    check("--save keeps Excel's calculated copy", near({"value": cached}, 121), cached)

    result, code = job("errors", "errors.xlsx", "--read", "S!A1", "--read", "S!A2", "--read", "S!A1:A3")
    check("error values come back by name", result["base"]["S!A1"].get("value") == {"error": "#DIV/0!"} and result["base"]["S!A2"].get("value") == {"error": "#N/A"}, result.get("base"))
    check("the error scan lists the cells", bool(result.get("errors")), result.get("errors"))

    result, code = job("damaged", "damaged.xlsx", "--read", "Result")
    recorded = result.get("ok") is False and len((result.get("open") or {}).get("failed_loads") or result.get("error") or "") > 0
    check("a damaged file is recorded without a hang", recorded or bool((result.get("open") or {}).get("repaired")), result)

    result, code = job("mixed", "tiny.xlsx", "--read", "Result", "--set", "Growth=0.1", "--set", "Model!B2=200")
    check("sets of different types in one job", code == 0 and near(result["after"]["Result"], 242), result.get("error"))
    result, code = job("mixed-formula", "tiny.xlsx", "--read", "Result", "--set", "Growth==0.1", "--set", "Model!B2=200")
    check("a formula then a whole number", code == 0 and near(result["after"]["Result"], 242), result.get("error"))

    result, code = job("forcefull", "forcefull.xlsx", "--read", "Result", "--set", "Growth=0.1")
    check("force full calculation doesn't time out", code == 0 and near(result["after"]["Result"], 121), result.get("error"))
    check("force full calculation is reported", (result.get("settings") or {}).get("force_full_calculation") is True, result.get("settings"))

    result, code = job("timeout", "tiny.xlsx", "--read", "Result", "--job-timeout", "10", "--test-hold", "60")
    programs = [(entry["program"], entry["outcome"]) for entry in result["runner"]["stopped"]]
    check("a job past its limit is stopped", code == 1 and result["runner"]["timed_out"] is True
          and ("EXCEL", "stopped") in programs and ("powershell", "stopped") in programs, result.get("runner"))
    result, code = job("timeout-at-start", "tiny.xlsx", "--read", "Result", "--job-timeout", "1", "--test-hold", "60")
    programs = [(entry["program"], entry["outcome"]) for entry in result["runner"]["stopped"]]
    check("a limit that falls while Excel starts still stops that Excel", code == 1 and ("EXCEL", "stopped") in programs, result.get("runner"))

    # A second client keeps the job's Excel calculating, so the job's calls are turned away as a large workbook's are.
    # Whether its rebuilds land at all, and whether one lands on the copy the job reopened, is a matter of timing: on
    # 12 November 2026 two of three runs needed a third job before Excel turned one of the job's calls away. So the
    # job runs up to three times, until a rebuild lands on the reopened copy. Every one of those jobs has to pass and
    # one has to have waited for a busy Excel. A run in which no rebuild landed on the reopened copy says so in a NOTE
    # line, because its read after the reopen was then not under load.
    helper = temp / f"xlbusy-{secrets.token_hex(4)}"
    helper.mkdir()
    attempts = []
    try:
        shutil.copyfile(HERE / "test-busy.ps1", helper / "test-busy.ps1")
        for attempt in ("busy", "busy-2", "busy-3"):
            known = job_folders(temp)
            running = start(attempt, "busy.xlsx", "--read", "Controls!B10:D15", "--read", "Rate")
            folder, _, _ = job_folder(temp, known, evidence / "busy.xlsx")
            report = helper / f"{attempt}.json"
            with (evidence / f"{attempt}-helper.stderr").open("wb") as said:
                busy = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                         f"{temp_win}\\{helper.name}\\test-busy.ps1", "-JobDir", f"{temp_win}\\{folder.name}",
                                         "-Log", f"{temp_win}\\{helper.name}\\{attempt}.json"],
                                        stdout=subprocess.DEVNULL, stderr=said, cwd=helper)
                result, code = finish(attempt, running)
                busy.wait(timeout=300)
            kept = json.loads(report.read_text(encoding="utf-8-sig")) if report.exists() else {"error": f"the helper wrote no report; see {attempt}-helper.stderr"}
            (evidence / f"{attempt}-helper.json").write_text(json.dumps(kept, indent=1), encoding="utf-8")
            attempts.append((result, code, kept))
            if code != 0 or kept.get("calls_on_saved_copy", 0) > 0:
                break
    finally:
        shutil.rmtree(helper, ignore_errors=True)
    result, code, kept = attempts[-1]
    check("the helper kept the job's Excel busy", all(entry[2].get("bound") is True for entry in attempts)
          and any(entry[2].get("calls", 0) > 0 for entry in attempts), [entry[2] for entry in attempts])
    if not kept.get("calls_on_saved_copy", 0) > 0:
        print(f"NOTE  no rebuild landed on the reopened copy in {len(attempts)} jobs, so the read after the reopen was not under load")
    check("a busy Excel doesn't fail the job", all(entry[1] == 0 for entry in attempts) and any((entry[0].get("busy_retries") or 0) > 0 for entry in attempts),
          [{key: entry[0].get(key) for key in ("error", "error_line", "busy_retries")} for entry in attempts])
    for stage in ("at open", "after the save and reopen"):
        entries = [((entry[0].get("base") if stage == "at open" else (entry[0].get("reopened") or {}).get("values")) or {}).get("Controls!B10:D15") or {}
                   for entry in attempts]
        check(f"a range on a protected sheet reads {stage} in every busy job",
              all((entry.get("values") or [None])[0] == [2.5, 3.5, 4.5] and len(entry["values"]) == 6 for entry in entries), entries)

    known = job_folders(temp)
    running = start("driver-died", "tiny.xlsx", "--read", "Result", "--test-hold", "120")
    _, excel_pid, driver_pid = job_folder(temp, known, evidence / "tiny.xlsx")
    stop_process(driver_pid, "powershell")
    result, code = finish("driver-died", running)
    left = process_name(excel_pid) == "EXCEL"
    if left:
        stop_process(excel_pid, "EXCEL")
    stopped = {"pid": excel_pid, "program": "EXCEL", "outcome": "stopped"} in result["runner"]["stopped"]
    check("an Excel whose job died is stopped", code == 1 and stopped and not left, result.get("runner"))
    check("a job that died says why", "ended without a result: " in (result.get("error") or ""), result.get("error"))

    # No Excel in the next two: stand-in workbooks for the reads, and a sleeping PowerShell for the stop.
    helper = temp / f"xlbusy-{secrets.token_hex(4)}"
    helper.mkdir()
    sleeper = None
    try:
        for name in ("test-read-guard.ps1", "excel-job.ps1"):
            shutil.copyfile(HERE / name, helper / name)
        done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                               f"{temp_win}\\{helper.name}\\test-read-guard.ps1", "-JobScript", f"{temp_win}\\{helper.name}\\excel-job.ps1",
                               "-Log", f"{temp_win}\\{helper.name}\\reads.json"], capture_output=True, timeout=120, cwd=helper)
        (evidence / "read-guard.stderr").write_bytes(done.stderr)
        report = helper / "reads.json"
        guard = json.loads(report.read_text(encoding="utf-8-sig")) if report.exists() else {}
        (evidence / "read-guard.json").write_text(json.dumps(guard, indent=1), encoding="utf-8")
        reads, scans = guard.get("reads") or {}, guard.get("scans") or {}

        spec = importlib.util.spec_from_file_location("excel", EXCEL)
        excel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(excel)
        pid_file = helper / "sleeper.pid"
        sleeper = subprocess.Popen([*POWERSHELL, f"Set-Content -LiteralPath '{temp_win}\\{helper.name}\\sleeper.pid' -Value $PID; Start-Sleep -Seconds 120"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=helper)
        deadline = time.monotonic() + 60
        while excel.recorded(pid_file) is None and time.monotonic() < deadline:
            time.sleep(0.2)
        sleeper_pid = excel.recorded(pid_file)
        asked = subprocess.run([*POWERSHELL, f"[DateTimeOffset]::new((Get-Process -Id {sleeper_pid}).StartTime.ToUniversalTime()).ToUnixTimeMilliseconds()"],
                               capture_output=True, text=True, timeout=60)
        began = int(asked.stdout.strip()) / 1000
        # As if an earlier process, since ended, had written this ID one second before this one started. Through WSL,
        # a time set on a Windows file keeps whole seconds only.
        os.utime(pid_file, (math.floor(began) - 1, math.floor(began) - 1))
        spared = excel.stop(pid_file, "powershell")
        spared_alive = process_name(sleeper_pid) == "powershell"
        os.utime(pid_file, (math.ceil(began), math.ceil(began)))  # Written in the second the process started.
        stopped = excel.stop(pid_file, "powershell")
    finally:
        if sleeper:
            sleeper.kill()
            sleeper.wait()
        shutil.rmtree(helper, ignore_errors=True)
    healthy = (reads.get("healthy_cell", {}).get("value") == 5 and reads.get("empty_cell", {}).get("formula") == ""
               and reads.get("healthy_rows", {}).get("values") == [[1], [2]])
    check("stand-in workbooks that answer read as values", healthy, reads)
    silent = [name for name in ("no_range", "no_count", "no_formula", "no_value", "no_values") if "read_error" not in reads.get(name, {})]
    check("a range whose row and column counts Excel refused still reads whole", reads.get("no_rows", {}).get("values") == [[1], [2]], reads.get("no_rows"))
    check("a read that Excel refused is an error, not an empty value", bool(reads) and not silent, {name: reads.get(name) for name in silent} or reads)
    found = scans.get("finds_cells", {}).get("found") == ["'S'!A1:A2"] and scans.get("no_error_cells", {}).get("found") == []
    check("the error scan of stand-in sheets lists cells, or none", found, scans)
    silent = [name for name in ("no_sheets", "no_used_range", "refused_call", "no_cells") if "scan_error" not in scans.get(name, {})]
    check("an error scan that Excel refused is an error, not an empty list", bool(scans) and not silent, {name: scans.get(name) for name in silent} or scans)
    check("a process that started after its ID was written is not stopped", (spared or {}).get("outcome", "").startswith("gone; the ID now belongs to a process started") and spared_alive, spared)
    check("a process that wrote its own ID is stopped", (stopped or {}).get("outcome") == "stopped", stopped)

    after, folders_after = excel_count(), job_folders(temp)
    check("no Excel process left behind", before == after, f"before {before}, after {after}")
    check("no job folder left behind", folders_before == folders_after, folders_after)
    check("no helper folder left behind", not list(temp.glob("xlbusy-*")), list(temp.glob("xlbusy-*")))
    print(f"{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
