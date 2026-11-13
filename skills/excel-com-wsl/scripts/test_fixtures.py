#!/usr/bin/env python3
"""Prove excel.py in desktop Excel with workbooks that have each tripped its COM layer. Needs openpyxl.

    python3 -I test_fixtures.py EVIDENCE_DIR [--no-excel]

Prints PASS or FAIL per check and exits 1 on any FAIL. EVIDENCE_DIR must not exist yet, and keeps each job's JSON,
what it printed (NAME.out) and its stderr (NAME.stderr). --no-excel runs only the cases that start no Excel: the
stand-in workbooks, the cut of --set REF=VALUE, a job that argparse turns away, the stop of a sleeping process and
the crash check's read of Windows' log.
"""

import contextlib
import importlib.util
import io
import json
import math
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid
import zipfile

import openpyxl
from openpyxl.styles import Protection
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.formula import ArrayFormula

HERE = pathlib.Path(__file__).resolve().parent
EXCEL = HERE / "excel.py"
POWERSHELL = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
# The cells of the Many sheet: 150 separate error cells, whose list is longer than the 256 characters of an address
# that Range.Address gives.
MANY = [(2 * number, 1 + (number % 7) * 2) for number in range(1, 151)]
# What marks a formula as a dynamic array formula, as Excel itself writes it.
METADATA = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<metadata xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:xda="http://schemas.microsoft.com/office/spreadsheetml/2017/dynamicarray">'
    '<metadataTypes count="1"><metadataType name="XLDAPR" minSupportedVersion="120000" copy="1" pasteAll="1" pasteValues="1" '
    'merge="1" splitFirst="1" rowColShift="1" clearFormats="1" clearComments="1" assign="1" coerce="1" cellMeta="1"/></metadataTypes>'
    '<futureMetadata name="XLDAPR" count="1"><bk><extLst><ext uri="{bdbb8cdc-fa1e-496e-a857-3c3f30c029c3}">'
    '<xda:dynamicArrayProperties fDynamic="1" fCollapsed="0"/></ext></extLst></bk></futureMetadata>'
    '<cellMetadata count="1"><bk><rc t="1" v="0"/></bk></cellMetadata></metadata>'
)
# What a read or a set says of a reference that opens with a quote and is no sheet name in quotes and a range.
UNDOUBLED = "double each apostrophe inside the quotes, as in 'It''s'!B2"
# What a read says of a reference or name of two rectangles, and of a name for which Excel has no range.
TWO_RECTANGLES = "range has 2 rectangles; read each rectangle by its own REF"
NO_RANGE = ("Excel gave no range for the name {name}, whose text has {length} characters: it has none for a name that refers to no cells, "
            "to cells on several sheets, or to more rectangles than it gives as one range, so give each rectangle as a REF of its own")
# The names of refs.xlsx that list more rectangles than Excel gives as one range: 40 of two cells, and 150 cells.
PAIRS = ",".join(f"S!$A${row}:$A${row + 1}" for row in range(1, 120, 3))
SINGLES = ",".join(f"S!${'ACEG'[number % 4]}${2 * number}" for number in range(1, 151))
failures = 0


def check(name: str, passed: bool, detail: object = "") -> None:
    global failures
    failures += not passed
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + ("" if passed else f"  {detail}"))


def near(entry: dict | None, expected: float) -> bool:
    value = (entry or {}).get("value")
    return isinstance(value, (int, float)) and math.isclose(value, expected, rel_tol=1e-9)


def make_dynamic(path: pathlib.Path) -> None:
    """Turn the array formulas of a workbook that openpyxl wrote into dynamic array formulas, which spill.

    openpyxl writes an array formula the way Excel stored one before dynamic arrays, and Excel opens that as one
    formula across its whole range, where every cell is a formula cell. A formula cell that points at this metadata
    is a dynamic array formula: its first cell holds the formula and the others are spilled.
    """
    with zipfile.ZipFile(path) as source:
        parts = {item.filename: source.read(item.filename) for item in source.infolist()}
    marked = 0
    for name in parts:
        if name.startswith("xl/worksheets/sheet"):
            parts[name], count = re.subn(rb'<c r="([A-Z]+[0-9]+)"([^>]*)><f t="array"', rb'<c r="\1"\2 cm="1"><f t="array"', parts[name])
            marked += count
    if not marked:
        sys.exit(f"test_fixtures.py: {path.name} holds no array formula to mark as dynamic")
    parts["xl/metadata.xml"] = METADATA.encode()
    for name, end, entry in (
            ("[Content_Types].xml", b"</Types>", b'<Override PartName="/xl/metadata.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheetMetadata+xml"/>'),
            ("xl/_rels/workbook.xml.rels", b"</Relationships>", b'<Relationship Id="rIdMetadata" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sheetMetadata" Target="metadata.xml"/>')):
        if parts[name].count(end) != 1:
            sys.exit(f"test_fixtures.py: {name} of {path.name} is not as expected")
        parts[name] = parts[name].replace(end, entry + end)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in parts.items():
            target.writestr(name, data)


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

    # Excel's SpecialCells cannot look at a protected sheet, so the scan asks in formulas, and lists the formula cells
    # holding errors there too, one of them with a hidden formula. A typed-in error value is no formula cell, and a
    # sheet whose only error value is typed in stays clean, protected or not.
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Open"
    sheet["A1"] = "=NA()"
    sheet = book.create_sheet("Typed")
    sheet["A1"], sheet["A2"], sheet["A3"], sheet["A4"] = "#N/A", "=1+1", "text", True
    sheet["A5"], sheet["A6"], sheet["A7"], sheet["A8"] = False, '=""', "TRUE", "=1=2"
    sheet["A9"] = "x" * 300  # A text longer than the 255 characters a COUNTIF criterion may have.
    for title, formula in (("Locked", "=1/0"), ("LockedClean", "=1+1")):
        sheet = book.create_sheet(title)
        sheet["A1"], sheet["B2"] = formula, "=2+2"
        sheet.protection.sheet = True
        sheet.protection.password = "synthetic"
    sheet = book["Locked"]
    sheet["A3"], sheet["C5"] = "#N/A", "=NA()"
    sheet["C5"].protection = Protection(hidden=True)
    sheet = book.create_sheet("LockedTyped")
    sheet["A1"], sheet["A2"] = "#N/A", 5
    sheet.protection.sheet = True
    sheet.protection.password = "synthetic"
    book.save(out / "protected.xlsx")

    # What SpecialCells left out or got wrong on a sheet it can look at: an error spilled below a clean first cell,
    # beside a formula error (Spill), alone (SpillOnly) and beside a typed-in error in a merged cell, which it counted
    # four times (Merged); a list of cells longer than an address may be (Many); and a sheet name with an apostrophe.
    # Far is protected, and its used range is the whole sheet.
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Spill"
    sheet["A1"], sheet["C1"], sheet["A8"] = ArrayFormula("A1:A3", "=IF(_xlfn.SEQUENCE(3)=2,NA(),1)"), "=1/0", "#N/A"
    sheet = book.create_sheet("SpillOnly")
    sheet["B2"] = ArrayFormula("B2:B5", "=IF(_xlfn.SEQUENCE(4)>2,NA(),1)")
    sheet = book.create_sheet("Merged")
    sheet["A1"], sheet["D2"] = "#N/A", ArrayFormula("D2:D3", "=IF(_xlfn.SEQUENCE(2)=2,NA(),1)")
    sheet.merge_cells("A1:B2")
    sheet = book.create_sheet("Joined")
    sheet["A1"] = "=1/0"
    sheet.merge_cells("A1:B2")
    sheet = book.create_sheet("Many")
    for row, column in MANY:
        sheet.cell(row, column, "=NA()")
    sheet = book.create_sheet("It's")
    sheet["A1"], sheet["B2"], sheet["C3"] = 2, "=1/0", "=A1*3"
    book.defined_names["Tripled"] = DefinedName("Tripled", attr_text="'It''s'!$C$3")
    sheet = book.create_sheet("Far")
    sheet["A1"], sheet["ZZ900000"], sheet["XFD1048576"] = "=NA()", "=1/0", 5
    sheet.protection.sheet = True
    sheet.protection.password = "synthetic"
    book.save(out / "scan.xlsx")
    make_dynamic(out / "scan.xlsx")

    # A workbook in R1C1 reference style puts the Excel that opens it first in that style, in which the A1 address
    # C10:C12 means columns 10 to 12.
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Clean"
    sheet["A1"], sheet["B2"] = 5, "=A1*2"
    sheet = book.create_sheet("Column")
    sheet["C10"], sheet["C11"], sheet["C12"] = "=NA()", "=1/0", "=NA()"
    book.calculation.refMode = "R1C1"
    book.save(out / "r1c1.xlsx")

    # Error cells so far apart that finding them takes more questions than the scan asks about one sheet.
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Scattered"
    for number in range(1, 121):
        sheet.cell(13001 * number % 1048576 + 1, 211 * number % 16384 + 1, "=NA()")
    sheet = book.create_sheet("Small")
    sheet["A1"] = "=NA()"
    book.save(out / "scattered.xlsx")

    # References that the job took wrongly: of several rectangles, typed or through a name, whose Value2 is the first
    # rectangle only; a sheet named with a letter outside A to Z, which unquoted went to the name lookup; and a sheet
    # whose name holds "=", where --set cut the reference. Pairs and Singles list more rectangles than Excel gives as
    # one range, most of them over cells that are empty. Two names are scoped to the sheet with the letter outside A
    # to Z: Elsewhere, whose cell lies on another sheet, and FY2024, spelt as a cell's address, which Excel's own
    # screens refuse and a file from another tool can hold.
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "S"
    for row in (1, 2):
        for column in (1, 2, 3):
            sheet.cell(row, column, row * 100 + column)
    sheet = book.create_sheet("\u00d8konomi")
    sheet["A1"], sheet["B1"] = 7, "=A1*2"
    sheet.defined_names["Elsewhere"] = DefinedName("Elsewhere", attr_text="S!$A$1")
    sheet.defined_names["FY2024"] = DefinedName("FY2024", attr_text="'\u00d8konomi'!$B$1")
    sheet = book.create_sheet("a=b")
    sheet["A1"], sheet["B1"] = 1, "=A1*2"
    for name, text in (("Few", "S!$A$1:$A$2,S!$C$1:$C$2"), ("Pairs", PAIRS), ("Singles", SINGLES), ("Fixed", "5"),
                       ("Bel\u00f8b", "'\u00d8konomi'!$A$1")):
        book.defined_names[name] = DefinedName(name, attr_text=text)
    book.save(out / "refs.xlsx")

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


def excel_crashes(since_ms: int, log: str = "Application") -> dict[int, str] | None:
    script = (f"$since = [DateTimeOffset]::FromUnixTimeMilliseconds({since_ms}).LocalDateTime; "
              f"try {{ $found = @(Get-WinEvent -FilterHashtable @{{ LogName = '{log}'; Id = 1000; ProviderName = 'Application Error'; StartTime = $since }} -ErrorAction Stop) }} "
              "catch { if ($_.FullyQualifiedErrorId -notmatch 'NoMatchingEventsFound') { throw }; $found = @() }; "
              "$found | Where-Object { $_.Properties[0].Value -eq 'EXCEL.EXE' } | "
              "ForEach-Object { 'crash {0} {1}' -f [int]$_.Properties[8].Value, $_.Properties[3].Value }; 'read'")
    done = subprocess.run(["winps"], input=script, capture_output=True, text=True, timeout=120)
    if done.stderr:
        print(done.stderr.rstrip(), file=sys.stderr)
    return parse_crashes(done.returncode, done.stdout)


def parse_crashes(status: int, said: str) -> dict[int, str] | None:
    lines = said.splitlines()
    if status != 0 or not lines or lines[-1].strip() != "read":
        return None
    return {int(parts[1]): parts[2] for parts in (line.split(" ", 2) for line in lines[:-1]) if len(parts) == 3 and parts[0] == "crash"}


def job_excels(evidence: pathlib.Path) -> dict[int, str]:
    found = {}
    for path in evidence.glob("*.json"):
        try:
            result = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError:
            continue
        if not isinstance(result, dict):
            continue
        pids = [(result.get("excel") or {}).get("pid")]
        pids.extend(entry.get("pid") for entry in (result.get("runner") or {}).get("stopped", [])
                    if entry.get("program") == "EXCEL")
        for pid in pids:
            if isinstance(pid, int):
                found[pid] = path.stem
    return found


def crashed_jobs(crashed: dict[int, str] | None, jobs: dict[int, str]) -> object:
    if crashed is None:
        return "the log gave no answer"
    if not jobs:
        return "no job recorded its Excel"
    return {jobs[pid]: crashed[pid] for pid in sorted(set(crashed) & set(jobs))} or None


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


def start_job(evidence: pathlib.Path, name: str, book: str, *args: str) -> subprocess.Popen:
    """Start one excel.py job, with what it prints in NAME.out and its stderr in NAME.stderr, both in the evidence
    folder: a job that ends without a result says why on stderr only."""
    with (evidence / f"{name}.out").open("wb") as said, (evidence / f"{name}.stderr").open("wb") as complained:
        return subprocess.Popen([sys.executable, "-I", str(EXCEL), str(evidence / book), "--json", str(evidence / f"{name}.json"), *args],
                                stdout=said, stderr=complained)


def excel_cases(evidence: pathlib.Path, temp: pathlib.Path, temp_win: str) -> None:
    """The cases that run a job through real Excel."""
    build(evidence)

    def start(name: str, book: str, *args: str) -> subprocess.Popen:
        return start_job(evidence, name, book, *args)

    def finish(name: str, running: subprocess.Popen) -> tuple[dict, int]:
        code = running.wait(timeout=500)
        return json.loads((evidence / f"{name}.json").read_text(encoding="utf-8")), code

    def job(name: str, book: str, *args: str) -> tuple[dict, int]:
        return finish(name, start(name, book, *args))

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
    check("the error scan lists the cells", result.get("errors") == ["'S'!A1:A2"], result.get("errors"))
    check("a scan that ran on every sheet leaves the job passing", code == 0 and result.get("errors_unscanned") == [],
          {key: result.get(key) for key in ("ok", "error", "errors_unscanned")})

    kept = evidence / "unread-saved.xlsx"
    result, code = job("unread", "tiny.xlsx", "--read", "NoSuchName", "--read", "Result", "--save", str(kept))
    said = (evidence / "unread.out").read_text(encoding="utf-8")
    check("a read that failed fails the job", code == 1 and result.get("ok") is False
          and (result.get("error") or "").startswith("2 of 4 reads failed; the first is NoSuchName at open: ")
          and " ok=False " in said and " read_errors=2 " in said,
          {key: result.get(key) for key in ("ok", "error")} | {"exit": code, "printed": said})
    check("the reads that worked stay in a failed job's result", near((result.get("base") or {}).get("Result"), 110.25)
          and near(((result.get("reopened") or {}).get("values") or {}).get("Result"), 110.25)
          and "read_error" in (result.get("base") or {}).get("NoSuchName", {}), result.get("base"))
    check("--save keeps the copy of a job that Excel finished with a failed read", kept.is_file() and result.get("saved") == str(kept), result.get("saved"))

    result, code = job("protected", "protected.xlsx", "--read", "Open!A1")
    said = (evidence / "protected.out").read_text(encoding="utf-8")
    check("the error scan lists the formula cells holding errors on a protected sheet, and no typed-in error value",
          result.get("errors") == ["'Open'!A1", "'Locked'!A1,C5"] and result.get("errors_unscanned") == [],
          {key: result.get(key) for key in ("errors", "errors_unscanned")})
    check("a workbook with error cells on a protected sheet leaves the job passing", code == 0 and result.get("ok") is True
          and " errors=2 " in said and " unscanned_sheets=0 " in said, {key: result.get(key) for key in ("ok", "error")} | {"exit": code, "printed": said})

    result, code = job("scan", "scan.xlsx", "--read", "Spill!A1", "--read", "Spill!A2", "--read", "SpillOnly!B5", "--read", "Merged!D3")
    base, listed = result.get("base") or {}, result.get("errors") or []
    check("the fixture's dynamic array formulas spill in Excel", (result.get("open") or {}).get("load") == "normal"
          and all(base.get(ref, {}).get("has_spill") is True for ref in ("Spill!A1", "Spill!A2", "SpillOnly!B5", "Merged!D3"))
          and near(base.get("Spill!A1"), 1) and all(base.get(ref, {}).get("value") == {"error": "#N/A"} for ref in ("Spill!A2", "SpillOnly!B5", "Merged!D3")),
          {"open": result.get("open"), "base": base})
    check("the error scan lists an error spilled below a clean first cell", "'Spill'!C1,A2" in listed and "'SpillOnly'!B4:B5" in listed, listed)
    check("the error scan lists a spilled error beside a typed-in error in a merged cell, and a merged formula cell once",
          "'Merged'!D3" in listed and "'Joined'!A1" in listed, listed)
    check("the error scan lists every cell of a list longer than an address",
          "'Many'!" + ",".join(f"{get_column_letter(column)}{row}" for row, column in MANY) in listed, listed)
    check("the error scan doubles an apostrophe in a sheet name", "'It''s'!B2" in listed, listed)
    check("the error scan lists cells far apart on a protected sheet whose used range is the whole sheet", "'Far'!A1,ZZ900000" in listed, listed)
    check("a scan that listed every such sheet leaves the job passing", code == 0 and len(listed) == 7 and result.get("errors_unscanned") == [],
          {key: result.get(key) for key in ("ok", "error", "errors", "errors_unscanned")})

    # errors and a read's address name a sheet as Excel does, in quotes with an apostrophe in its name doubled, so
    # what the scan job listed for the sheet It's goes back in as a REF here, and this job's address into the next.
    entry = next((item for item in listed if item.startswith("'It")), "")
    result, code = job("quoted", "scan.xlsx", "--read", entry, "--read", "'It''s'!C3", "--read", "Tripled", "--set", "'It''s'!A1=5")
    base, after = result.get("base") or {}, result.get("after") or {}
    check("an entry copied from errors reads back, on a sheet whose name holds an apostrophe",
          entry == "'It''s'!B2" and entry in listed and base.get(entry, {}).get("value") == {"error": "#DIV/0!"}, {"entry": entry, "base": base})
    check("a read's address doubles an apostrophe in the sheet name, for a name that points at the sheet too",
          base.get(entry, {}).get("address") == "'It''s'!$B$2" and base.get("'It''s'!C3", {}).get("address") == "'It''s'!$C$3"
          and base.get("Tripled", {}).get("address") == "'It''s'!$C$3" and near(base.get("Tripled"), 6), base)
    check("--set reaches a cell on a sheet whose name holds an apostrophe", code == 0 and near(base.get("'It''s'!C3"), 6)
          and near(after.get("'It''s'!C3"), 15) and near(((result.get("reopened") or {}).get("values") or {}).get("'It''s'!C3"), 15),
          {key: result.get(key) for key in ("ok", "error", "base", "after")})
    address = base.get(entry, {}).get("address") or ""
    result, code = job("quoted-again", "scan.xlsx", "--read", address, "--read", "'It's'!B2")
    base = result.get("base") or {}
    check("an address copied from a read reads back, on a sheet whose name holds an apostrophe",
          address == "'It''s'!$B$2" and base.get(address, {}).get("value") == {"error": "#DIV/0!"} and base.get(address, {}).get("address") == address,
          {"address": address, "base": base})
    check("an undoubled apostrophe in a quoted sheet name fails the read, with an error that says to double it",
          code == 1 and UNDOUBLED in base.get("'It's'!B2", {}).get("read_error", "")
          and (result.get("error") or "").startswith("2 of 4 reads failed; the first is 'It's'!B2 at open: "),
          {key: result.get(key) for key in ("ok", "error", "base")} | {"exit": code})
    kept = evidence / "quoted-set-saved.xlsx"
    result, code = job("quoted-set", "scan.xlsx", "--read", "'It''s'!C3", "--set", "'It's'!A1=5", "--save", str(kept))
    check("an undoubled apostrophe in a set's quoted sheet name ends the job without a save, with an error that says to double it",
          code == 1 and result.get("ok") is False and UNDOUBLED in (result.get("error") or "") and near((result.get("base") or {}).get("'It''s'!C3"), 6)
          and result.get("after") is None and result.get("reopened") is None and "saved" not in result and not kept.exists(),
          {key: result.get(key) for key in ("ok", "error", "base", "after", "reopened", "saved")} | {"exit": code})

    # A range of several rectangles gives the values of its first one only, under the address of them all, so such
    # a read fails, typed or through a name, and each rectangle reads by its own REF.
    several = ("S!A1:A2,C1:C2", "S!A1,C1:C2", "Few")
    result, code = job("areas", "refs.xlsx", *(part for ref in (*several, "Pairs", "Singles", "Fixed", "S!A1:A2", "S!C1:C2") for part in ("--read", ref)))
    base = result.get("base") or {}
    check("a reference or name of several rectangles fails the read with their count, whether its first rectangle is one cell or more",
          all(base.get(ref) == {"read_error": TWO_RECTANGLES} for ref in several), {ref: base.get(ref) for ref in several})
    check("a name of more rectangles than Excel gives as one range fails the read with the count its text lists, or with no range at all",
          base.get("Pairs") == {"read_error": "range has 40 rectangles; read each rectangle by its own REF"}
          and base.get("Singles") == {"read_error": NO_RANGE.format(name="Singles", length=len(SINGLES) + 1)},
          {ref: base.get(ref) for ref in ("Pairs", "Singles")})
    check("a name that refers to no cells fails the read with that reason",
          base.get("Fixed") == {"read_error": NO_RANGE.format(name="Fixed", length=2)}, base.get("Fixed"))
    check("each rectangle reads by its own REF, in a job that the reads of several fail",
          code == 1 and base.get("S!A1:A2", {}).get("values") == [[101], [201]] and base.get("S!C1:C2", {}).get("values") == [[103], [203]]
          and (result.get("error") or "").startswith(f"12 of 16 reads failed; the first is S!A1:A2,C1:C2 at open: {TWO_RECTANGLES}"),
          {key: result.get(key) for key in ("ok", "error", "base")} | {"exit": code})
    result, code = job("areas-set", "refs.xlsx", "--read", "S!A1", "--set", "S!A1:A2,C1:C2=5")
    check("--set on several rectangles ends the job", code == 1 and (result.get("error") or "").startswith("set target S!A1:A2,C1:C2 is not one cell")
          and result.get("after") is None and result.get("reopened") is None, {key: result.get(key) for key in ("ok", "error", "after", "reopened")} | {"exit": code})
    result, code = job("no-range-set", "refs.xlsx", "--read", "S!A1", "--set", "Fixed=5")
    check("--set through a name that refers to no cells ends the job with that reason", code == 1 and result.get("error") == NO_RANGE.format(name="Fixed", length=2)
          and near((result.get("base") or {}).get("S!A1"), 101) and result.get("after") is None and result.get("reopened") is None,
          {key: result.get(key) for key in ("ok", "error", "base", "after", "reopened")} | {"exit": code})

    # A sheet name with a letter outside A to Z needs no quotes, and a read's address goes into --set whole, with a
    # "=" in its sheet name. A REF with such a sheet name unquoted is looked up as a name first, which finds a name
    # scoped to that sheet wherever its cells lie and where it is spelt as a cell's address.
    result, code = job("letters", "refs.xlsx", "--read", "\u00d8konomi!A1", "--read", "'\u00d8konomi'!A1", "--read", "'\u00d8konomi'!B1", "--read", "Bel\u00f8b",
                       "--read", "'a=b'!B1", "--read", "\u00d8konomi!Elsewhere", "--read", "\u00d8konomi!FY2024", "--set", "'a=b'!$A$1=5")
    base, after = result.get("base") or {}, result.get("after") or {}
    check("a name scoped to a sheet named with a letter outside A to Z reads where its cell lies on another sheet",
          near(base.get("\u00d8konomi!Elsewhere"), 101) and base.get("\u00d8konomi!Elsewhere", {}).get("address") == "'S'!$A$1", base.get("\u00d8konomi!Elsewhere"))
    check("such a name spelt as a cell's address reads the name, not the cell",
          near(base.get("\u00d8konomi!FY2024"), 14) and base.get("\u00d8konomi!FY2024", {}).get("address") == "'\u00d8konomi'!$B$1", base.get("\u00d8konomi!FY2024"))
    check("an unquoted sheet name with a letter outside A to Z reads as the quoted one does",
          near(base.get("\u00d8konomi!A1"), 7) and base.get("\u00d8konomi!A1", {}).get("address") == "'\u00d8konomi'!$A$1"
          and base.get("\u00d8konomi!A1") == base.get("'\u00d8konomi'!A1") and near(base.get("'\u00d8konomi'!B1"), 14), base)
    check("a workbook name with a letter outside A to Z reads", near(base.get("Bel\u00f8b"), 7) and base.get("Bel\u00f8b", {}).get("address") == "'\u00d8konomi'!$A$1", base.get("Bel\u00f8b"))
    check("--set takes a read's address on a sheet whose name holds =", base.get("'a=b'!B1", {}).get("address") == "'a=b'!$B$1" and near(base.get("'a=b'!B1"), 2)
          and near(after.get("'a=b'!B1"), 10) and near(((result.get("reopened") or {}).get("values") or {}).get("'a=b'!B1"), 10),
          {key: result.get(key) for key in ("ok", "error", "base", "after")})
    check("a job with such sheet names passes", code == 0 and result.get("ok") is True, {key: result.get(key) for key in ("ok", "error")} | {"exit": code})

    kept = evidence / "r1c1-saved.xlsx"
    result, code = job("r1c1", "r1c1.xlsx", "--read", "Clean!B2", "--save", str(kept))
    check("the error scan lists the cells of a workbook in R1C1 reference style, and the saved copy keeps that style",
          code == 0 and result.get("errors") == ["'Column'!C10:C12"] and result.get("errors_unscanned") == [] and near((result.get("base") or {}).get("Clean!B2"), 10)
          and kept.is_file() and openpyxl.load_workbook(kept).calculation.refMode == "R1C1",
          {key: result.get(key) for key in ("ok", "error", "errors", "errors_unscanned", "base")})

    result, code = job("scattered", "scattered.xlsx", "--read", "Small!A1")
    skipped = result.get("errors_unscanned") or []
    check("the error scan names a sheet whose error cells it could not list, and lists the other sheet's", result.get("errors") == ["'Small'!A1"]
          and [entry.get("sheet") for entry in skipped] == ["Scattered"]
          and (skipped[0].get("reason") or "") == "its error values are spread over too many cells to list: the scan reads at most 2000000 cells of one sheet",
          {key: result.get(key) for key in ("errors", "errors_unscanned")})
    said = (evidence / "scattered.out").read_text(encoding="utf-8")
    check("an error scan that could not tell for a sheet fails the job", code == 1 and result.get("ok") is False
          and "the error scan could not list the error cells of 1 sheet; the first is Scattered: " in (result.get("error") or "")
          and (result.get("base") or {}).get("Small!A1", {}).get("value") == {"error": "#N/A"} and " unscanned_sheets=1 " in said,
          {key: result.get(key) for key in ("ok", "error", "base")} | {"exit": code, "printed": said})

    result, code = job("damaged", "damaged.xlsx", "--read", "Result")
    recorded = result.get("ok") is False and len((result.get("open") or {}).get("failed_loads") or result.get("error") or "") > 0
    check("a damaged file is recorded without a hang", recorded or bool((result.get("open") or {}).get("repaired")), result)

    result, code = job("mixed", "tiny.xlsx", "--read", "Result", "--set", "Growth=0.1", "--set", "Model!B2=200")
    check("sets of different types in one job", code == 0 and near(result["after"]["Result"], 242), result.get("error"))
    result, code = job("mixed-formula", "tiny.xlsx", "--read", "Result", "--read", "Growth", "--set", "Growth==0.1", "--set", "Model!B2=200")
    check("a formula then a whole number", code == 0 and near(result["after"]["Result"], 242), result.get("error"))
    check("--set REF==0.1 writes the formula =0.1", (result.get("after") or {}).get("Growth", {}).get("formula") == "=0.1"
          and (result.get("base") or {}).get("Growth", {}).get("formula") == "0.05", {key: result.get(key) for key in ("base", "after")})

    result, code = job("forcefull", "forcefull.xlsx", "--read", "Result", "--set", "Growth=0.1")
    check("force full calculation doesn't time out", code == 0 and near(result["after"]["Result"], 121), result.get("error"))
    check("force full calculation is reported", (result.get("settings") or {}).get("force_full_calculation") is True, result.get("settings"))

    # The limit has to fall in the hold, after the open: on 13 November 2026 a job's start mark came at up to 7.9 s,
    # and one job with a limit of 10 s was still opening the workbook when it fell.
    result, code = job("timeout", "tiny.xlsx", "--read", "Result", "--job-timeout", "20", "--test-hold", "60")
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

    # Office writes four log files of up to 20 MiB for each Excel session to Diagnostics\EXCEL in the temp folder and
    # leaves them there. Each job deletes its own Excel's, by the session GUID that the files are named after, and
    # excel.py deletes them for a job it had to stop.
    sessions = {}
    for path in sorted(evidence.glob("*.json")):
        found = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(found, dict) or "runner" not in found:
            continue
        logs = found.get("office_logs") or found["runner"].get("office_logs")
        if found.get("excel") or logs:
            sessions[path.stem] = ((found.get("excel") or {}).get("session") or (logs or {}).get("session"), logs)
    unrecorded = sorted(name for name, (session, _) in sessions.items() if not session)
    check("each job whose Excel started records that Excel's Office session", "tiny" in sessions and not unrecorded, unrecorded)
    for name, what in (("tiny", "a job deletes its Excel's Office logs"), ("timeout", "excel.py deletes the Office logs of an Excel it stopped past the job's limit"),
                       ("driver-died", "excel.py deletes the Office logs of an Excel it stopped because the job's PowerShell died")):
        found = json.loads((evidence / f"{name}.json").read_text(encoding="utf-8"))
        logs = (found.get("office_logs") if name == "tiny" else found["runner"].get("office_logs")) or {}
        if name == "timeout" and not logs.get("removed"):
            # On a slow start the limit falls in the open, killing Excel ends the job's call, and the job deletes the
            # logs itself before excel.py gets to them, as in one run on 13 November 2026.
            logs = found.get("office_logs") or logs
        check(what, logs.get("removed", 0) > 0 and logs.get("left") == 0 and not logs.get("reason"), logs)
    said = (evidence / "tiny.out").read_text(encoding="utf-8")
    check("excel.py's one-line summary counts the Office logs a job left", " office_logs_left=0 " in said, said.strip())
    folder = temp / "Diagnostics" / "EXCEL"
    kept = sorted(path.name for session, _ in sessions.values() if session for path in folder.rglob(f"*_{session}.log"))
    check("no Office log of a job's Excel is left", not kept, kept)

    # A job that ends without a result says why on stderr only. These jobs usually write nothing there, so the check is
    # that each one's file is there beside what it printed; the no-value job of stand_in_cases shows what reaches it.
    jobs = sorted(path.stem for path in evidence.glob("*.out"))
    lost = [name for name in jobs if not (evidence / f"{name}.stderr").is_file()]
    check("each job through real Excel keeps its stderr in the evidence folder", "tiny" in jobs and "driver-died" in jobs and not lost, lost or jobs)


def office_log_cases(evidence: pathlib.Path, temp: pathlib.Path, temp_win: str) -> None:
    """office-logs.ps1 on stand-in log files and stand-in session entries, in a temp folder and a registry key of its
    own, with no Excel: which session it finds, which files it deletes, and what it says when it finds or deletes
    none."""
    helper = temp / f"xlbusy-{secrets.token_hex(4)}"
    win = f"{temp_win}\\{helper.name}"
    sessions = f"HKCU:\\Software\\xlcom-test-{helper.name}"
    guid = {name: str(uuid.uuid4()).upper() for name in ("own", "other", "held", "found", "empty", "stale", "late", "other_id", "unlistable", "no_files", "no_folder", "late_value")}
    logs = helper / "tmp" / "Diagnostics" / "EXCEL"
    (logs / "Additional").mkdir(parents=True)
    for name, session in guid.items():
        if name in ("no_files", "no_folder", "late_value"):
            continue
        folder = helper / "tmp2" / "Diagnostics" / "EXCEL" if name == "unlistable" else logs
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"Primary1_{session}.log").write_bytes(b"\0" * 1000)
    (logs / "Additional" / f"Additional2_{guid['own']}.log").write_bytes(b"\0" * 1000)
    began = int(time.time()) - 100  # When each stand-in job wrote ps.pid; it wrote excel.pid 10 s later.

    def entry(process: int, name: str, offset: int) -> dict:
        """A session entry as Office writes it, stamped `offset` seconds after ps.pid, as a FILETIME."""
        return {"ProcessId": process, "SessionId": "{" + guid[name] + "}", "SessionInitTime": str((began + offset + 11644473600) * 10**7)}

    entries = {1001: entry(1001, "found", 5), 1002: entry(1002, "stale", -5), 1003: entry(1003, "late", 15),
               1004: entry(9999, "other_id", 5), 1005: entry(1005, "empty", 5)}
    # Each stand-in job: the process ID in its excel.pid (4 is the System process, never an Excel, and has no entry),
    # what its excel.session holds, if it has one, and how office-logs.ps1 runs for it.
    jobs = {"own": (4, guid["own"], ""), "held": (4, guid["held"], ""), "not_a_guid": (4, "not-a-guid", ""), "unrecorded": (4, None, ""),
            "found": (1001, None, ""), "empty_file": (1005, "", ""), "stale": (1002, None, ""), "late": (1003, None, ""),
            "other_id": (1004, None, ""), "record": (1001, None, "-Record"), "record_none": (4, None, "-Record"),
            "no_ps_pid": (1001, None, "-Record"), "unlistable": (4, guid["unlistable"], ""), "no_files": (4, guid["no_files"], ""),
            "no_folder": (4, guid["no_folder"], "")}
    # Office makes an entry before it writes its value: the late_value job's entry gets its value 3 s after -Record
    # starts, so -Record finds it only by trying again after a read that failed.
    for job, (process, session, _) in (jobs | {"late_value": (1006, None, "-Record")}).items():
        (helper / job).mkdir()
        for file, offset in (("ps.pid", 0), ("excel.pid", 10)):
            if job != "no_ps_pid" or file != "ps.pid":
                (helper / job / file).write_text(str(process), encoding="utf-8")
                os.utime(helper / job / file, (began + offset, began + offset))
        if session is not None:
            (helper / job / "excel.session").write_text(session, encoding="utf-8")
    shutil.copyfile(HERE / "office-logs.ps1", helper / "office-logs.ps1")
    (helper / "entries.json").write_text(json.dumps({str(key): json.dumps(value) for key, value in entries.items()} | {"1006": ""}), encoding="utf-8")
    (helper / "entries.ps1").write_text(
        "param([string] $Key, [string] $From)\n"
        "New-Item -Path $Key -Force | Out-Null\n"
        "$list = Get-Content -LiteralPath $From -Raw | ConvertFrom-Json\n"
        "foreach ($p in $list.PSObject.Properties) { New-Item -Path (Join-Path $Key $p.Name) -Force | Out-Null; if ($p.Value) { "
        "New-ItemProperty -LiteralPath (Join-Path $Key $p.Name) -Name '4' -PropertyType Binary -Value ([Text.Encoding]::UTF8.GetBytes($p.Value)) | Out-Null } }\n",
        encoding="utf-8")
    unlistable = f"{win}\\tmp2\\Diagnostics\\EXCEL"
    holder = subprocess.Popen([*POWERSHELL, f"$f = [IO.File]::Open('{win}\\tmp\\Diagnostics\\EXCEL\\Primary1_{guid['held']}.log', 'Open', 'Read', 'None'); "
                               f"Set-Content -LiteralPath '{win}\\held.ready' -Value 1; Start-Sleep -Seconds 120"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=helper)
    said, kept, summaries = {}, {}, []
    real = temp / "Diagnostics" / "EXCEL"
    before = {path.name for path in real.rglob("*.log")} if real.exists() else set()
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", f"{win}\\entries.ps1",
                        "-Key", sessions, "-From", f"{win}\\entries.json"], capture_output=True, timeout=120, cwd=helper, check=True)
        deadline = time.monotonic() + 60
        while not (helper / "held.ready").exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        for job, (_, _, mode) in jobs.items():
            run = f"& '{win}\\office-logs.ps1' -Dir '{win}\\{job}' -Sessions '{sessions}' {mode}"
            if job == "unlistable":
                run = (f"$env:TMP = '{win}\\tmp2'; $env:TEMP = $env:TMP; $env:LOCALAPPDATA = $env:TMP; $env:USERPROFILE = $env:TMP; icacls '{unlistable}' /deny \"$($env:USERNAME):(RD)\" | Out-Null; "
                       f"try {{ {run} }} finally {{ icacls '{unlistable}' /remove:d $env:USERNAME | Out-Null }}")
            else:
                run = f"$env:TMP = '{win}\\{'tmp3' if job == 'no_folder' else 'tmp'}'; $env:TEMP = $env:TMP; $env:LOCALAPPDATA = $env:TMP; $env:USERPROFILE = $env:TMP; {run}"
            done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", run],
                                  capture_output=True, text=True, timeout=120, cwd=helper)
            try:
                said[job] = json.loads(done.stdout)
            except ValueError:
                said[job] = {"stdout": done.stdout[-300:], "stderr": done.stderr[-300:]}
        recording = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                                      f"& '{win}\\office-logs.ps1' -Dir '{win}\\late_value' -Sessions '{sessions}' -Record"],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=helper)
        time.sleep(3)  # A slower start of -Record finds the value at once, which passes too; only no second try fails.
        value = json.dumps(entry(1006, "late_value", 5))
        subprocess.run([*POWERSHELL, f"New-ItemProperty -LiteralPath '{sessions}\\1006' -Name '4' -PropertyType Binary "
                        f"-Value ([Text.Encoding]::UTF8.GetBytes('{value}')) | Out-Null"], capture_output=True, timeout=60, check=True)
        try:
            said["late_value"] = json.loads(recording.communicate(timeout=60)[0])
        except ValueError:
            said["late_value"] = {"error": "-Record printed no JSON"}
        kept = {name: (logs / f"Primary1_{session}.log").exists() for name, session in guid.items() if name not in ("unlistable", "no_files", "no_folder", "late_value")}
        kept["unlistable"] = (helper / "tmp2" / "Diagnostics" / "EXCEL" / f"Primary1_{guid['unlistable']}.log").exists()
        kept["own_additional"] = (logs / "Additional" / f"Additional2_{guid['own']}.log").exists()
        recorded_file = (helper / "record" / "excel.session").read_text(encoding="utf-8-sig").strip() if (helper / "record" / "excel.session").exists() else None
        spec = importlib.util.spec_from_file_location("excel", EXCEL)
        excel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(excel)
        (helper / "no_script").mkdir()
        said["no_script"] = excel.office_logs(helper / "no_script", f"{win}\\no_script")
        summaries = [excel.left_logs(result) for result in ({"office_logs": {"left": 0, "reason": None}, "runner": {}},
                                                             {"office_logs": {"left": 1, "reason": "1 left: in use"}, "runner": {}},
                                                             {"runner": {"office_logs": said["no_script"]}})]
    finally:
        holder.kill()
        holder.wait()
        subprocess.run([*POWERSHELL, f"Remove-Item -LiteralPath '{sessions}' -Recurse -Force -ErrorAction SilentlyContinue"], capture_output=True, timeout=60)
        for _ in range(20):  # The killed holder can keep its file open for a moment longer.
            shutil.rmtree(helper, ignore_errors=True)
            if not helper.exists():
                break
            time.sleep(0.5)
    (evidence / "office-logs.json").write_text(json.dumps({"said": said, "kept": kept}, indent=1), encoding="utf-8")
    gone = subprocess.run([*POWERSHELL, f"Test-Path -LiteralPath '{sessions}'"], capture_output=True, text=True, timeout=60).stdout.strip()
    check("the stand-in cases leave no registry key behind", gone == "False", gone)
    vanished = sorted(before - ({path.name for path in real.rglob("*.log")} if real.exists() else set()))
    check("the stand-in cases delete none of the real Office logs", not vanished, vanished)
    nothing = {"session": None, "removed": 0, "bytes": 0, "left": 0, "reason": "no Office session was recorded for the job's Excel"}
    check("office-logs.ps1 deletes the recorded session's stand-in log files, here and in Additional, and no other session's",
          said.get("own") == {"session": guid["own"], "removed": 2, "bytes": 2000, "left": 0, "reason": None}
          and not kept.get("own") and not kept.get("own_additional") and kept.get("other"), {"said": said.get("own"), "kept": kept})
    held = said.get("held") or {}
    check("office-logs.ps1 leaves a stand-in log file that another process holds, and says why",
          {key: held.get(key) for key in ("session", "removed", "left")} == {"session": guid["held"], "removed": 0, "left": 1}
          and (held.get("reason") or "").startswith("1 left: ") and "another process" in held["reason"] and kept.get("held"), held)
    check("office-logs.ps1 deletes nothing for a recorded session that is no GUID, or for a job that recorded none and whose process ID Office has no session for",
          said.get("not_a_guid") == nothing and said.get("unrecorded") == nothing, {job: said.get(job) for job in ("not_a_guid", "unrecorded")})
    check("office-logs.ps1 takes the session from Office's entry for a job that recorded none, or recorded an empty one",
          said.get("found") == {"session": guid["found"], "removed": 1, "bytes": 1000, "left": 0, "reason": None}
          and said.get("empty_file") == {"session": guid["empty"], "removed": 1, "bytes": 1000, "left": 0, "reason": None}, {job: said.get(job) for job in ("found", "empty_file")})
    check("office-logs.ps1 refuses an entry stamped before ps.pid or after excel.pid, or for another process, and keeps its files",
          all(said.get(job) == nothing for job in ("stale", "late", "other_id")) and all(kept.get(job) for job in ("stale", "late", "other_id")),
          {job: said.get(job) for job in ("stale", "late", "other_id")} | {"kept": kept})
    check("office-logs.ps1 -Record writes the session of Office's entry to excel.session, or says why it found none",
          said.get("record") == {"session": guid["found"], "reason": None} and recorded_file == guid["found"]
          and said.get("record_none") == {"session": None, "reason": "Office recorded no session for this Excel within 10 s"}
          and (said.get("no_ps_pid") or {}).get("session") is None and "ps.pid" in ((said.get("no_ps_pid") or {}).get("reason") or ""),
          {job: said.get(job) for job in ("record", "record_none", "no_ps_pid")} | {"excel.session": recorded_file})
    check("office-logs.ps1 -Record tries again after a read that failed, and finds an entry whose value Office writes later",
          said.get("late_value") == {"session": guid["late_value"], "reason": None}, said.get("late_value"))
    check("office-logs.ps1 says so when the recorded session has no log file, or Office wrote no log folder",
          said.get("no_files") == {"session": guid["no_files"], "removed": 0, "bytes": 0, "left": 0, "reason": "no log file of the session was found"}
          and said.get("no_folder") == {"session": guid["no_folder"], "removed": 0, "bytes": 0, "left": 0, "reason": "Office wrote no log folder"},
          {job: said.get(job) for job in ("no_files", "no_folder")})
    unlisted = said.get("unlistable") or {}
    check("office-logs.ps1 says why when it cannot list the log folder, and deletes nothing",
          unlisted.get("removed") == 0 and "denied" in (unlisted.get("reason") or "") and kept.get("unlistable"), unlisted)
    check("excel.py's summary counts the Office logs left, and says unknown when the deletion found no session or failed",
          summaries == [0, 1, "unknown"], summaries)
    check("excel.py reports a deletion that printed no result, with PowerShell's error",
          said.get("no_script", {}).get("reason", "").startswith("the deletion printed no result: ") and "office-logs.ps1" in said["no_script"]["reason"],
          said.get("no_script"))


def stand_in_cases(evidence: pathlib.Path, temp: pathlib.Path, temp_win: str) -> None:
    """The cases that start no Excel: stand-in workbooks for the reads and the error scan, a job that argparse turns
    away, a sleeping PowerShell for the stop, and the cut of --set REF=VALUE."""
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
        # A job that argparse turns away starts no Excel and says why on stderr only.
        (evidence / "no-workbook.xlsx").write_bytes(b"")
        turned_away = start_job(evidence, "no-value", "no-workbook.xlsx", "--set", "Growth").wait(timeout=120)
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
    silent = [name for name in ("no_range", "no_areas", "no_area_count", "no_count", "no_formula", "no_value", "no_values", "no_text") if "read_error" not in reads.get(name, {})]
    check("a range whose row and column counts Excel refused still reads whole", reads.get("no_rows", {}).get("values") == [[1], [2]], reads.get("no_rows"))
    check("a read that Excel refused is an error, not an empty value", bool(reads) and not silent, {name: reads.get(name) for name in silent} or reads)
    check("a stand-in range of several rectangles is an error that names their count, whether its first rectangle is one cell or more",
          all(reads.get(name) == {"read_error": TWO_RECTANGLES} for name in ("two_areas", "one_cell_first")), {name: reads.get(name) for name in ("two_areas", "one_cell_first")})
    check("a stand-in name with a text and no range is an error that says so, and one with neither is a refused read",
          reads.get("long_name") == {"read_error": NO_RANGE.format(name="long_name", length=281)}
          and reads.get("no_range") == {"read_error": "Excel gave no answer for the range"}, {name: reads.get(name) for name in ("long_name", "no_range")})
    check("a stand-in name whose range Excel gives on the second ask reads", reads.get("late_range", {}).get("value") == 5, reads.get("late_range"))
    shortened = {name: reads.get(name) for name in ("cut_to_one", "cut_short", "no_text")}
    check("a stand-in name whose text lists more rectangles than Excel's range has fails the read with the count its text lists, "
          "and one whose text Excel refused is a refused read",
          shortened == {"cut_to_one": {"read_error": "range has 40 rectangles; read each rectangle by its own REF"},
                  "cut_short": {"read_error": "range has 40 rectangles; read each rectangle by its own REF"},
                  "no_text": {"read_error": "Excel gave no answer for the text of the name no_text"}}, shortened)
    commas = {name: reads.get(name) for name in ("in_quotes", "doubled_in_quotes", "in_parentheses", "in_string", "nested_columns", "escaped_column",
                                                 "parenthesis_in_quotes", "apostrophe_in_column", "after_parentheses")}
    check("a stand-in name's text counts no comma inside a quoted sheet name, a function's parentheses, a string or a table's brackets as a union, "
          "and counts the union after a parenthesis in quotes, an apostrophe in brackets or a function's parentheses",
          all((commas[name] or {}).get("value") == 5 for name in list(commas)[:6])
          and all(commas[name] == {"read_error": TWO_RECTANGLES} for name in list(commas)[6:]), commas)
    sets = guard.get("sets") or {}
    check("--set through a stand-in name whose text lists several rectangles ends the job, where Excel gave one cell for it",
          sets == {"healthy_cell": "cells: 1", "cut_to_one": "set target cut_to_one is not one cell", "two_areas": "set target two_areas is not one cell"}, sets)

    def scan(name: str) -> dict:
        return {key: scans.get(name, {}).get(key) for key in ("found", "unscanned")}

    def named(name: str) -> tuple[list, str]:
        """The sheets a stand-in scan could not tell for, and the first one's reason."""
        entries = scans.get(name, {}).get("unscanned") or []
        return [entry.get("sheet") for entry in entries], (entries[0].get("reason") or "") if entries else ""

    clean = {"found": [], "unscanned": []}
    check("the error scan of stand-in sheets lists cells, or none", scan("finds_cells") == {"found": ["'S'!A1:A2"], "unscanned": []}
          and scan("no_error_cells") == clean and scan("typed_only") == clean
          and scan("several_sheets") == {"found": ["'Open'!A1:A2", "'Other'!A1"], "unscanned": []}, scans)
    check("the error scan asks one question of a sheet with no error value", scans.get("no_error_cells", {}).get("asked") == [1], scans.get("no_error_cells"))
    check("the error scan of stand-in sheets lists spilled errors, and no typed-in one beside them",
          scan("spilled") == {"found": ["'Spill'!C1,A2:A3"], "unscanned": []} and scan("spilled_only") == {"found": ["'Spill'!B4:B5"], "unscanned": []}
          and scan("typed_beside_spilled") == {"found": ["'Mix'!A2,A4,C7:C9"], "unscanned": []}, {name: scans.get(name) for name in ("spilled", "spilled_only", "typed_beside_spilled")})
    check("the error scan of stand-in sheets lists a long list whole, in reading order, under a sheet name with a doubled apostrophe",
          scan("long_list")["found"] == ["'Many'!" + ",".join(f"{get_column_letter(column)}{row}" for row, column in MANY)]
          and scan("reading_order")["found"] == ["'Block'!B2:D4,F2:F4,B6"] and scan("apostrophe")["found"] == ["'It''s'!B2"],
          {name: scans.get(name) for name in ("long_list", "reading_order", "apostrophe")})
    quoted, again = guard.get("quoted") or {}, guard.get("quoted_again") or {}
    addresses = {"'It''s'!B2": "'It''s'!$B$2", "'O''Neil''s'!C7": "'O''Neil''s'!$C$7", "'My sheet'!A1": "'My sheet'!$A$1", "S!A1": "'S'!$A$1",
                 "'\u00d8konomi'!A1": "'\u00d8konomi'!$A$1", "\u00d8konomi!B3": "'\u00d8konomi'!$B$3", "Data_1!A1": "'Data_1'!$A$1",
                 "Cafe\u0301!A2": "'Cafe\u0301'!$A$2",
                 "'My sheet'!T[Owner''s share]": "'My sheet'!T[Owner''s share]", "S!T['#Items]": "'S'!T['#Items]"}
    check("a stand-in sheet named in quotes reads, with each doubled apostrophe in its name as one, and so do one with a letter outside A to Z, "
          "quoted or not, unquoted ones with a digit and an underscore or with an accent as a combining mark, and a table column with an apostrophe in its name",
          bool(quoted) and all(quoted.get(ref, {}).get("value") == 5 for ref in addresses), quoted)
    check("a stand-in read's address doubles an apostrophe in the sheet name, and reads back",
          all(quoted.get(ref, {}).get("address") == address and again.get(address, {}).get("address") == address for ref, address in addresses.items())
          and set(again) == set(addresses.values()) | {"'\u00d8konomi'!$B$2", "'\u00d8konomi'!$FY$2024"}, {"quoted": quoted, "quoted_again": again})
    check("an undoubled apostrophe in a quoted stand-in sheet name is an error that says to double it",
          bool(quoted) and all(UNDOUBLED in quoted.get(ref, {}).get("read_error", "") for ref in ("'It's'!B2", "'O'Neil''s'!C7", "'It''s!B2", "'A'!B'C'!D1")),
          {ref: quoted.get(ref) for ref in ("'It's'!B2", "'O'Neil''s'!C7", "'It''s!B2", "'A'!B'C'!D1")})
    scoped = ("\u00d8konomi!Elsewhere", "\u00d8konomi!FY2024", "\u00d8konomi!LocalConst", "\u00d8konomi!Cut")
    check("a stand-in reference whose unquoted sheet name holds a letter outside A to Z is looked up as a name first, as before the change, "
          "so a name scoped to that sheet reads where its cell lies on another sheet and where it is spelt as a cell's address",
          quoted.get(scoped[0], {}).get("value") == 5 and quoted.get(scoped[0], {}).get("address") == "'S'!$A$1"
          and quoted.get(scoped[1], {}).get("address") == "'\u00d8konomi'!$B$2", {ref: quoted.get(ref) for ref in scoped[:2]})
    check("a stand-in name scoped to such a sheet, with a text and no range, is an error that says so",
          quoted.get(scoped[2]) == {"read_error": NO_RANGE.format(name=scoped[2], length=2)}, quoted.get(scoped[2]))
    check("a stand-in name scoped to such a sheet whose text lists two rectangles fails the read with that count, where Excel gave one cell for it",
          quoted.get(scoped[3]) == {"read_error": TWO_RECTANGLES}, quoted.get(scoped[3]))
    alone = ("'\u00d8konomi'!Elsewhere", "S!Elsewhere", "S!Nowhere")
    check("a stand-in sheet named in quotes or in A to Z is asked alone, as before the change, so a cell's address reads as the cell "
          "and a name whose cell lies on another sheet fails with the sheet's error",
          quoted.get("'\u00d8konomi'!FY2024", {}).get("address") == "'\u00d8konomi'!$FY$2024"
          and all(quoted.get(ref, {}).get("read_error", "").endswith(f'has no range {ref.split("!")[1]}"') for ref in alone)
          and guard.get("name_lookups") == ["\u00d8konomi!B3", "Cafe\u0301!A2", *scoped, "My sheet!A1", "My-sheet!A1"],
          {ref: quoted.get(ref) for ref in ("'\u00d8konomi'!FY2024", *alone)} | {"name_lookups": guard.get("name_lookups")})
    refused = guard.get("refused_lookups") or {}
    check("a stand-in name lookup that Excel refused is an error, and the read does not go on to the sheet",
          "refused" in refused.get("rejected", {}).get("read_error", "") and refused.get("no_name") == {"read_error": "Excel gave no answer for the range"}
          and refused.get("no_names") == {"read_error": "Excel gave no answer for the names"}, refused)
    broken = guard.get("failed_lookups") or {}
    check("a stand-in name lookup that fails with an error other than a missing name's is an error, and the read does not go on to the sheet",
          list(broken) == ["0x800AC472", "0x800706BA", "0x80010105", "other"]
          and all(set(entry) == {"read_error"} for entry in broken.values())
          and all(f"the stand-in lookup failed with {code}" in broken[code]["read_error"] for code in list(broken)[:3])
          and "the stand-in lookup broke" in broken["other"]["read_error"], broken)
    check("an unquoted stand-in sheet name with a space or a hyphen is taken for a workbook name",
          all("has no name" in quoted.get(ref, {}).get("read_error", "") for ref in ("My sheet!A1", "My-sheet!A1")), {ref: quoted.get(ref) for ref in ("My sheet!A1", "My-sheet!A1")})
    # What a question costs is part of the answer: a scan that read every piece would pass the limit on such sheets.
    check("the error scan of stand-in sheets finds cells far apart, a column and a row of errors within a few questions",
          scan("far_apart") == {"found": ["'Far'!A1,ZZ900000"], "unscanned": []} and scan("column_of_errors") == {"found": ["'Column'!C1:C40000"], "unscanned": []}
          and scan("row_of_errors") == {"found": ["'Row'!A20001:KN20001"], "unscanned": []}
          and all(0 < (scans.get(name, {}).get("asked") or [0])[0] <= 150 for name in ("far_apart", "column_of_errors", "row_of_errors")),
          {name: scans.get(name) for name in ("far_apart", "column_of_errors", "row_of_errors")})
    reasons = {
        "over_the_limit": (["Wide"], "its error values are spread over too many cells to list: the scan reads at most 30000 cells of one sheet"),
        "any_is_an_error": (["Old"], "Excel could not say whether A1:B3 holds an error value: ISERROR(SUM(A1:B3)) came back as code 2029"),
        "any_is_no_boolean": (["Odd"], "Excel could not say whether A1:B3 holds an error value: ISERROR(SUM(A1:B3)) came back as 1"),
        "any_throws": (["Odd"], 'protected sheet."'),
        "cells_are_an_error": (["Old"], "Excel could not say which cells of A1:B3 hold error values: ISERROR(A1:B3)*(1+ISFORMULA(A1:B3)) came back as code 2029"),
        "cells_throw": (["Odd"], 'protected sheet."'),
        "too_few_cells": (["Odd"], "Excel could not say which cells of A1:B3 hold error values: 3 answers for 6 cells"),
        "one_cell_is_odd": (["Odd"], "Excel could not say which cells of A1:B3 hold error values: one answer is '3'"),
        "one_cell_is_an_error": (["Odd"], "Excel could not say which cells of A1:B3 hold error values: one answer is '-2146826259'"),
        "one_cell_is_missing": (["Odd"], "Excel could not say which cells of A1:B3 hold error values: one answer is ''"),
        "spill_is_odd": (["Odd"], "Excel could not say whether A2 is spilled"),
    }
    wrong = {name: scans.get(name) for name, (sheets, reason) in reasons.items()
             if named(name)[0] != sheets or not named(name)[1].endswith(reason) or scans.get(name, {}).get("found") != ([] if name != "over_the_limit" else ["'Small'!A1"])}
    check("an error scan that could not tell for a sheet names it with the reason, and lists no cell of it", bool(scans) and not wrong, wrong or scans)
    refusals = ("no_sheets", "no_name", "no_used_range", "no_first_row", "no_rows", "no_columns", "no_row_count", "no_column_count",
                "no_any", "refused_any", "no_cells", "refused_cells", "no_range", "refused_range", "no_spill")
    silent = [name for name in refusals if "scan_error" not in scans.get(name, {})]
    check("an error scan that Excel refused is an error, not an empty list", bool(scans) and not silent, {name: scans.get(name) for name in silent} or scans)
    failed = {"ok": False, "error": "Excel stopped", "base": {"A": {"read_error": "no such name"}, "B": {"value": 1}}}
    excel.settle(failed)
    whole = {"ok": True, "error": None, "base": {"B": {"value": 1}}, "errors_unscanned": []}
    excel.settle(whole)
    check("a job that Excel stopped keeps its error beside its failed reads, and a whole result stays whole",
          failed["error"] == "Excel stopped; 1 of 2 reads failed; the first is A at open: no such name" and failed["ok"] is False
          and whole == {"ok": True, "error": None, "base": {"B": {"value": 1}}, "errors_unscanned": []}, [failed, whole])
    complaint = evidence / "no-value.stderr"
    check("a job's stderr is kept in the evidence folder", turned_away == 2 and complaint.is_file()
          and "--set needs REF=VALUE, not Growth" in complaint.read_text(encoding="utf-8", errors="replace"),
          {"exit": turned_away, "stderr": complaint.read_text(encoding="utf-8", errors="replace") if complaint.is_file() else None})
    check("a process that started after its ID was written is not stopped", (spared or {}).get("outcome", "").startswith("gone; the ID now belongs to a process started") and spared_alive, spared)
    check("a process that wrote its own ID is stopped", (stopped or {}).get("outcome") == "stopped", stopped)
    cuts = {"Growth=0.1": ("Growth", "0.1"), "Growth==0.1": ("Growth", "=0.1"), "Model!B2=a=b": ("Model!B2", "a=b"), "'a=b'!$A$1=5": ("'a=b'!$A$1", "5"),
            "'a=b'!A1==0.1": ("'a=b'!A1", "=0.1"), "'It''s=1'!B2=x=y": ("'It''s=1'!B2", "x=y"), "'=='!A1=": ("'=='!A1", ""), "'It's'!A1=5": ("'It's'!A1", "5"),
            "'a=b!A1=5": ("'a", "b!A1=5"), "Model!B2='a=b'!A1": ("Model!B2", "'a=b'!A1"), "'a=b'!A1": None, "Growth": None,
            "S!T[a=b]=5": ("S!T[a", "b]=5"), "'My sheet'!T[a=b]=5": ("'My sheet'!T[a", "b]=5"),
            "'a=b'!A1=='c=d'!B2": ("'a=b'!A1", "='c=d'!B2")}
    cut = {item: excel.split_set(item) for item in cuts}
    check("--set REF=VALUE is cut at the first = after a quoted sheet name, and at the first = of a REF without one, which falls inside a table column's "
          "name that holds =", cut == cuts, cut)


def crash_log_cases(evidence: pathlib.Path) -> None:
    ahead = excel_crashes(time.time_ns() // 1_000_000 + 3_600_000)
    check("the crash check reads Windows' log and finds no Excel crash in the time ahead",
          ahead == {}, ahead)
    error = io.StringIO()
    with contextlib.redirect_stderr(error):
        missing = excel_crashes(0, log="xlcom-no-such-log")
    check("the crash check fails when Windows' log gives no answer", missing is None)
    check("the crash check keeps Windows' error", "xlcom-no-such-log" in error.getvalue(), error.getvalue())
    parsed = [parse_crashes(0, "crash 12 combase.dll\nnote 14 other\ncrash 13 Some Module.dll\nread\n"), parse_crashes(0, "crash 12 combase.dll\n"),
              parse_crashes(1, "crash 12 combase.dll\nread\n"), parse_crashes(0, "")]
    check("the crash check takes each crash the log printed, and no answer from a read that did not finish", parsed == [
        {12: "combase.dll", 13: "Some Module.dll"}, None, None, None], parsed)
    rejected = subprocess.run([sys.executable, "-I", str(HERE / "test_fixtures.py"), "--no-excel"],
                              cwd=evidence, env={"PATH": ""}, capture_output=True, text=True, timeout=60)
    check("a missing evidence directory prints usage and starts no suite",
          rejected.returncode != 0 and rejected.stderr.startswith(__doc__.strip())
          and not (evidence / "--no-excel").exists(), rejected.stderr)
    stand_in = evidence / "crash-check"
    stand_in.mkdir()
    (stand_in / "kept.json").write_text(json.dumps({"ok": True, "excel": {"pid": 4242}}), encoding="utf-8-sig")
    (stand_in / "failed.json").write_text(json.dumps({"ok": False, "excel": {"pid": 4343}, "error": "x"}), encoding="utf-8")
    (stand_in / "no-excel.json").write_text(json.dumps({"ok": False, "excel": None}), encoding="utf-8")
    (stand_in / "helper.json").write_text(json.dumps({"bound": True, "excel_pid": 4444}), encoding="utf-8")
    (stand_in / "list.json").write_text("[1, 2]", encoding="utf-8")
    (stand_in / "torn.json").write_text('{"excel": {"pid": 45', encoding="utf-8")
    (stand_in / "text.json").write_text(json.dumps({"excel": {"pid": "4646"}}), encoding="utf-8")
    (stand_in / "stopped.json").write_text(json.dumps({"runner": {"stopped": [
        {"program": "EXCEL", "pid": 4545}, {"program": "powershell", "pid": 4646},
        {"program": "EXCEL", "pid": "4747"}]}}), encoding="utf-8")
    found = job_excels(stand_in)
    check("the crash check takes each job result's Excel and nothing else", found == {4242: "kept", 4343: "failed", 4545: "stopped"}, found)
    check("the crash check catches an Excel recorded only by the runner, without counting PowerShell",
          crashed_jobs({4545: "combase.dll"}, found) == {"stopped": "combase.dll"}
          and crashed_jobs({4646: "combase.dll"}, found) is None, found)
    verdicts = [crashed_jobs(None, found), crashed_jobs({}, {}), crashed_jobs({4343: "combase.dll", 9: "x"}, found),
                crashed_jobs({9: "combase.dll"}, found), crashed_jobs({}, found)]
    check("the crash check fails on a crashed job's Excel, on a log with no answer and on no job, and on nothing else",
          verdicts == ["the log gave no answer", "no job recorded its Excel", {"failed": "combase.dll"}, None, None], verdicts)


def main() -> int:
    if len(sys.argv) not in (2, 3) or sys.argv[2:] not in ([], ["--no-excel"]) or sys.argv[1].startswith("-"):
        sys.exit(__doc__)
    evidence = pathlib.Path(sys.argv[1]).resolve()
    evidence.mkdir(parents=True)
    temp, temp_win = windows_temp()
    before, folders_before = excel_count(), job_folders(temp)
    started_ms = time.time_ns() // 1_000_000
    if len(sys.argv) == 2:
        excel_cases(evidence, temp, temp_win)
    stand_in_cases(evidence, temp, temp_win)
    office_log_cases(evidence, temp, temp_win)
    crash_log_cases(evidence)
    if len(sys.argv) == 2:
        why = crashed_jobs(excel_crashes(started_ms), job_excels(evidence))
        check("no job's Excel crashed", why is None, why)
    after, folders_after = excel_count(), job_folders(temp)
    check("no Excel process left behind", before == after, f"before {before}, after {after}")
    check("no job folder left behind", folders_before == folders_after, folders_after)
    check("no helper folder left behind", not list(temp.glob("xlbusy-*")), list(temp.glob("xlbusy-*")))
    print(f"{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
