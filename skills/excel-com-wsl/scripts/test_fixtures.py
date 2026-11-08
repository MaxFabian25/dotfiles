#!/usr/bin/env python3
"""Prove excel.py in desktop Excel with workbooks that have each tripped its COM layer. Needs openpyxl.

    python3 -I test_fixtures.py EVIDENCE_DIR

Prints PASS or FAIL per check and exits 1 on any FAIL. EVIDENCE_DIR must not exist yet, and keeps each job's JSON.
"""

import json
import math
import pathlib
import subprocess
import sys
import zipfile

import openpyxl
from openpyxl.workbook.defined_name import DefinedName

EXCEL = pathlib.Path(__file__).resolve().parent / "excel.py"
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


def excel_count() -> str:
    done = subprocess.run([*POWERSHELL, "@(Get-Process EXCEL -ErrorAction SilentlyContinue).Count"],
                          capture_output=True, text=True, timeout=60)
    return done.stdout.strip()


def job_folders() -> list[str]:
    done = subprocess.run([*POWERSHELL, "[IO.Path]::GetTempPath()"], capture_output=True, text=True, check=True, timeout=60)
    temp = subprocess.run(["wslpath", "-u", done.stdout.strip()], capture_output=True, text=True, check=True).stdout.strip()
    return [path.name for path in pathlib.Path(temp).glob("xlcom-*")]


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    evidence = pathlib.Path(sys.argv[1]).resolve()
    evidence.mkdir(parents=True)
    build(evidence)

    def job(name: str, book: str, *args: str) -> tuple[dict, int]:
        out = evidence / f"{name}.json"
        done = subprocess.run([sys.executable, "-I", str(EXCEL), str(evidence / book), "--json", str(out), *args],
                              capture_output=True, text=True, timeout=500)
        return json.loads(out.read_text(encoding="utf-8")), done.returncode

    before, folders_before = excel_count(), job_folders()

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

    after, folders_after = excel_count(), job_folders()
    check("no Excel process left behind", before == after, f"before {before}, after {after}")
    check("no job folder left behind", folders_before == folders_after, folders_after)
    print(f"{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
