#!/usr/bin/env python3
"""Prove excel.py in desktop Excel with workbooks that have each tripped its COM layer. Needs openpyxl.

    python3 -I test_fixtures.py EVIDENCE_DIR

Prints PASS or FAIL per check and exits 1 on any FAIL. EVIDENCE_DIR must not exist yet, and keeps each job's JSON.
"""

import json
import math
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
    check("a job past its limit is stopped", code == 1 and result["runner"]["timed_out"] is True, result.get("runner"))
    result, code = job("timeout-at-start", "tiny.xlsx", "--read", "Result", "--job-timeout", "1", "--test-hold", "60")
    programs = [(entry["program"], entry["outcome"]) for entry in result["runner"]["stopped"]]
    check("a limit that falls while Excel starts still stops that Excel", code == 1 and ("EXCEL", "stopped") in programs, result.get("runner"))

    # A second client keeps the job's Excel calculating, so the job's calls are turned away as a large workbook's are.
    helper = temp / f"xlbusy-{secrets.token_hex(4)}"
    helper.mkdir()
    try:
        shutil.copyfile(HERE / "test-busy.ps1", helper / "test-busy.ps1")
        known = job_folders(temp)
        running = start("busy", "busy.xlsx", "--read", "Controls!B10:D15", "--read", "Rate")
        folder, _, _ = job_folder(temp, known, evidence / "busy.xlsx")
        said = (evidence / "busy-helper.stderr").open("wb")
        busy = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                 f"{temp_win}\\{helper.name}\\test-busy.ps1", "-JobDir", f"{temp_win}\\{folder.name}",
                                 "-Log", f"{temp_win}\\{helper.name}\\busy.json"],
                                stdout=subprocess.DEVNULL, stderr=said, cwd=helper)
        result, code = finish("busy", running)
        busy.wait(timeout=300)
        report = helper / "busy.json"
        kept = json.loads(report.read_text(encoding="utf-8-sig")) if report.exists() else {"error": "the helper wrote no report; see busy-helper.stderr"}
        (evidence / "busy-helper.json").write_text(json.dumps(kept, indent=1), encoding="utf-8")
    finally:
        shutil.rmtree(helper, ignore_errors=True)
    check("the helper kept the job's Excel busy", kept.get("bound") is True and kept.get("calls", 0) > 0, kept)
    check("a busy Excel doesn't fail the job", code == 0 and (result.get("busy_retries") or 0) > 0,
          {key: result.get(key) for key in ("error", "error_line", "busy_retries")})
    for stage, values in (("at open", result.get("base")), ("after the save and reopen", (result.get("reopened") or {}).get("values"))):
        entry = (values or {}).get("Controls!B10:D15") or {}
        check(f"a range on a protected sheet reads {stage} while Excel is busy",
              (entry.get("values") or [None])[0] == [2.5, 3.5, 4.5] and len(entry["values"]) == 6, entry or result.get("error"))

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

    after, folders_after = excel_count(), job_folders(temp)
    check("no Excel process left behind", before == after, f"before {before}, after {after}")
    check("no job folder left behind", folders_before == folders_after, folders_after)
    check("no helper folder left behind", not list(temp.glob("xlbusy-*")), list(temp.glob("xlbusy-*")))
    print(f"{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
