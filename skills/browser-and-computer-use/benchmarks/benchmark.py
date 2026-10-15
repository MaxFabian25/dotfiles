#!/usr/bin/env python3
"""Compare local form workflow timings. Requires python3 and playwright-cli on PATH."""

import argparse
import functools
import http.server
import json
import os
from pathlib import Path
import platform
import re
import shutil
import statistics
import subprocess
import threading
import time
import uuid

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", required=True, type=Path)
parser.add_argument("--repetitions", type=int, default=5)
args = parser.parse_args()
if args.repetitions < 1:
    parser.error("--repetitions must be positive; use at least 5 for measured comparisons")
cli = shutil.which("playwright-cli")
if not cli:
    parser.error("playwright-cli must already be installed and on PATH")
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=False)
scratch = output / "cli"
scratch.mkdir()
(output / "commands").mkdir()
session = "tuning-" + uuid.uuid4().hex[:12]
env = {**os.environ, "NO_UPDATE_NOTIFIER": "1", "PLAYWRIGHT_MCP_OUTPUT_DIR": str(scratch)}
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
    http.server.SimpleHTTPRequestHandler, directory=str(Path(__file__).resolve().parent)))
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
url = f"http://127.0.0.1:{server.server_port}/fixture.html"
initial_snapshot = scratch / "initial.yaml"
report = {"session": session, "daemon_pid": None, "fixture_url": url, "output_directory": str(output),
          "initial_snapshot": str(initial_snapshot), "cli_version": None, "repetitions": args.repetitions,
          "platform": platform.platform(), "logical_cores": os.cpu_count(),
          "load_average_before": os.getloadavg() if hasattr(os, "getloadavg") else None,
          "commands": [], "warmups": [], "trials": [],
          "measurement": "Local fixture workflow wall time, including CLI process startup, default waits and snapshots. "
                         "Setup, navigation/reset, discovery, warmups, screenshot, log writes and teardown are excluded. "
                         "Argument UTF-8 bytes sum argv strings excluding the executable and separators. "
                         "Script UTF-8 bytes count callback file contents separately; creating those files is timed. "
                         "Stdout bytes exclude generated snapshot files. Neither metric measures tokens or total agent context. "
                         "This does not measure whole-agent website latency.",
          "claim_under_test": "Bounded batching reduces CLI round trips and stdout while preserving the same checked result.",
          "interpretation": "Timings alone do not identify the limiter or establish a speedup. Profile separately."}
pending_logs = []


def run(command, phase, raw=False):
    script_bytes = 0
    if command[0] == "run-code":
        snippet = scratch / f"snippet-{len(report['commands']) + 1:04d}.js"
        data = command[1].encode("utf-8")
        snippet.write_bytes(data)
        script_bytes = len(data)
        command = ["run-code", f"--filename={snippet}"]
    argv = [cli, f"-s={session}", *(["--raw"] if raw else []), *command]
    started = time.perf_counter()
    try:
        proc = subprocess.run(argv, cwd=scratch, env=env, capture_output=True, timeout=90)
        code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as error:
        code, stdout, stderr = 124, error.stdout or b"", (error.stderr or b"") + b"\nProbe subprocess timeout"
    elapsed = time.perf_counter() - started
    stem = f"commands/{len(report['commands']) + 1:04d}-{phase}"
    record = {"phase": phase, "argv": argv, "elapsed_s": elapsed, "returncode": code,
              "argument_utf8_bytes": sum(len(part.encode("utf-8")) for part in argv[1:]),
              "script_utf8_bytes": script_bytes,
              "stdout_bytes": len(stdout), "stderr_bytes": len(stderr),
              "stdout_path": stem + ".stdout", "stderr_path": stem + ".stderr"}
    report["commands"].append(record)
    pending_logs.extend([(output / record["stdout_path"], stdout), (output / record["stderr_path"], stderr)])
    return record, stdout.decode("utf-8", errors="replace")


def persist():
    for path, data in pending_logs:
        path.write_bytes(data)
    pending_logs.clear()
    (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")


expected = {"name": "Ada Example", "email": "ada@example.test", "role": "editor", "agree": True,
            "result": "Saved Name=Ada Example; Email=ada@example.test; Role=editor; Agree=true"}
assertions = """
  const expected = EXPECTED;
  await page.waitForFunction(text => document.querySelector('#result').textContent === text, expected.result);
  await page.locator('#result').waitFor({ state: 'visible' });
  const actual = await page.locator('#editor form').evaluate(form => ({
    name: form.elements.name.value, email: form.elements.email.value,
    role: form.elements.role.value, agree: form.elements.agree.checked,
    result: document.querySelector('#result').textContent
  }));
  if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error('Form/result mismatch ' + JSON.stringify(actual));
  return { verified: 'PLAYWRIGHT_TUNING_VERIFIED_V1', actual };
""".replace("EXPECTED", json.dumps(expected))
actions = [(["fill", '#editor [name="name"]', expected["name"]], "await page.locator('#editor [name=\"name\"]').fill('Ada Example');"),
           (["fill", '#editor [name="email"]', expected["email"]], "await page.locator('#editor [name=\"email\"]').fill('ada@example.test');"),
           (["select", '#editor [name="role"]', "editor"], "await page.locator('#editor [name=\"role\"]').selectOption('editor');"),
           (["check", '#editor [name="agree"]'], "await page.locator('#editor [name=\"agree\"]').check();"),
           (["click", '#editor button[type="submit"]'], "await page.locator('#editor button[type=\"submit\"]').click();")]
separate = [command for command, _ in actions] + [["run-code", "async page => {" + assertions + "}"]]
variants = [("A", "segmented", False, separate), ("B", "segmented_raw", True, separate),
            ("C", "batched_raw", True, [["run-code", "async page => {" + "\n".join(js for _, js in actions) + assertions + "}"]])]
reset_check = """async page => {
  const state = await page.locator('#editor form').evaluate(form =>
    [form.elements.name.value, form.elements.email.value, form.elements.role.value, form.elements.agree.checked]);
  if (JSON.stringify(state) !== JSON.stringify(['', '', 'reader', false])) throw new Error('Reset failed');
  if (await page.locator('#result').isVisible()) throw new Error('Result was not reset');
  if (await page.locator('#fixture-identity').textContent() !== 'playwright-tuning-fixture-v1') throw new Error('Wrong fixture');
  return 'RESET_VERIFIED';
}"""
opened = False
try:
    version_record, version = run(["--version"], "setup")
    assert version_record["returncode"] == 0, "CLI version failed"
    report["cli_version"] = version.strip()
    opened = True
    record, text = run(["open", url], "setup")
    match = re.search(r"opened with pid (\d+)", text)
    report["daemon_pid"] = int(match[1]) if match else None
    persist()
    print(f"Metadata ready at {output / 'results.json'}", flush=True)
    assert record["returncode"] == 0, "Browser open failed"
    record, _ = run(["snapshot", f"--filename={initial_snapshot}"], "discovery")
    assert record["returncode"] == 0 and "playwright-tuning-fixture-v1" in initial_snapshot.read_text(), "Snapshot identity failed"
    persist()
    for repetition in range(args.repetitions + 1):
        phase = "warmup" if repetition == 0 else "timed"
        for variant, name, raw, commands in variants:
            reset, _ = run(["goto", f"{url}?reset={repetition}-{variant}"], "reset")
            checked, text = run(["run-code", reset_check], "reset", True)
            assert reset["returncode"] == checked["returncode"] == 0 and "RESET_VERIFIED" in text, "Fixture reset failed"
            records = []
            started = time.perf_counter()
            for command in commands:
                record, text = run(command, phase, raw)
                records.append(record)
            elapsed = time.perf_counter() - started
            result_text = text
            if not raw:
                result_section = re.search(r"### Result\s*\n(.*?)(?=\n### |\Z)", text, re.S)
                result_text = result_section[1] if result_section else ""
            try:
                actual_result = json.loads(result_text)
            except json.JSONDecodeError:
                actual_result = None
            verified = actual_result == {"verified": "PLAYWRIGHT_TUNING_VERIFIED_V1", "actual": expected}
            success = all(item["returncode"] == 0 for item in records) and verified
            trial = {"variant": variant, "name": name, "repetition": repetition, "elapsed_s": elapsed,
                     "success": success, "calls": len(records), "stdout_bytes": sum(item["stdout_bytes"] for item in records),
                     "argument_utf8_bytes": sum(item["argument_utf8_bytes"] for item in records),
                     "script_utf8_bytes": sum(item["script_utf8_bytes"] for item in records),
                     "failed_commands": sum(item["returncode"] != 0 for item in records),
                     "result_verified": verified, "commands": records}
            report["warmups" if repetition == 0 else "trials"].append(trial)
            persist()
            print(f"{phase} {variant} {repetition} {elapsed:.3f}s success={success}", flush=True)
            assert repetition != 0 or success, f"Warmup {variant} failed"
except Exception as error:
    report["fatal_error"] = repr(error)
finally:
    if opened:
        screenshot = scratch / "final.png"
        record, _ = run(["screenshot", f"--filename={screenshot}"], "screenshot")
        png_valid = screenshot.is_file() and screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        report["final_screenshot"] = str(screenshot) if record["returncode"] == 0 and png_valid else None
        record, _ = run(["close"], "teardown")
        close_ok = record["returncode"] == 0
        listed, listing = run(["list", "--json"], "teardown")
        try:
            remaining = [browser["name"] for browser in json.loads(listing)["browsers"] if browser["name"] == session]
        except (json.JSONDecodeError, KeyError, TypeError):
            remaining = None
        report["owned_sessions_remaining"] = remaining
        report["owned_session_close_success"] = close_ok and listed["returncode"] == 0 and remaining == []
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    report["owned_server_stopped"] = not thread.is_alive()
    report["load_average_after"] = os.getloadavg() if hasattr(os, "getloadavg") else None
    report["summary"] = {}
    for variant, name, _, _ in variants:
        trials = [trial for trial in report["trials"] if trial["variant"] == variant]
        passed = [trial for trial in trials if trial["success"]]
        summary = {"name": name, "successes": len(passed), "failures": len(trials) - len(passed)}
        for field in ("elapsed_s", "calls", "stdout_bytes", "argument_utf8_bytes", "script_utf8_bytes"):
            values = [trial[field] for trial in passed]
            summary[field] = {"median": statistics.median(values), "min": min(values), "max": max(values)} if values else None
        summary["total_calls"] = sum(trial["calls"] for trial in trials)
        summary["total_stdout_bytes"] = sum(trial["stdout_bytes"] for trial in trials)
        summary["total_argument_utf8_bytes"] = sum(trial["argument_utf8_bytes"] for trial in trials)
        summary["total_script_utf8_bytes"] = sum(trial["script_utf8_bytes"] for trial in trials)
        report["summary"][variant] = summary
    persist()
print(json.dumps(report["summary"], indent=2))
raise SystemExit(int(bool(report.get("fatal_error")) or any(not trial["success"] for trial in report["trials"])
                     or not report.get("final_screenshot") or not report.get("owned_session_close_success")
                     or not report["owned_server_stopped"]))
