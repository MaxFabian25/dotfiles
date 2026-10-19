#!/usr/bin/env python3
"""Compare local form workflow timings. Requires python3 and agent-browser on PATH."""

import argparse
import functools
import http.server
import json
import os
from pathlib import Path
import platform
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
cli = shutil.which("agent-browser")
if not cli:
    parser.error("agent-browser must already be installed and on PATH")
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=False)
scratch = output / "cli"
scratch.mkdir()
(output / "commands").mkdir()
session = "tuning-" + uuid.uuid4().hex[:12]
env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_BROWSER_")}
env["NO_UPDATE_NOTIFIER"] = "1"
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
    http.server.SimpleHTTPRequestHandler, directory=str(Path(__file__).resolve().parent)))
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
url = f"http://127.0.0.1:{server.server_port}/fixture.html"
initial_snapshot = scratch / "initial.txt"
report = {"session": session, "daemon_pid": None, "fixture_url": url, "output_directory": str(output),
          "initial_snapshot": str(initial_snapshot), "cli_version": None, "repetitions": args.repetitions,
          "platform": platform.platform(), "logical_cores": os.cpu_count(),
          "load_average_before": os.getloadavg() if hasattr(os, "getloadavg") else None,
          "commands": [], "warmups": [], "trials": [],
          "measurement": "Local fixture workflow wall time, including CLI process startup and default waits. "
                         "Setup, navigation/reset, discovery, warmups, screenshot and teardown are excluded. "
                         "Command output streams to log files during the timed calls. "
                         "Argument UTF-8 bytes sum argv strings excluding the executable and separators. "
                         "Script UTF-8 bytes count batch and eval scripts passed on standard input separately; creating those files is timed. "
                         "Neither metric measures tokens or total agent context. "
                         "This does not measure whole-agent website latency.",
          "claim_under_test": "Bounded batching reduces CLI round trips and stdout while preserving the same checked result.",
          "interpretation": "Timings alone do not identify the limiter or establish a speedup. Profile separately."}


def run(command, phase, as_json=False):
    script_bytes = 0
    stdin = None
    # Scripts travel on standard input. Windows launchers cut off or reject multiline arguments.
    if command[0] in ("batch", "eval"):
        batch = command[0] == "batch"
        script = scratch / f"script-{len(report['commands']) + 1:04d}.{'json' if batch else 'js'}"
        data = (json.dumps(command[1]) if batch else command[1]).encode("utf-8")
        script.write_bytes(data)
        script_bytes = len(data)
        stdin = script.open("rb")
        command = ["batch", "--bail"] if batch else ["eval", "--stdin"]
    argv = [cli, "--session", session, *(["--json"] if as_json else []), *command]
    stem = f"commands/{len(report['commands']) + 1:04d}-{phase}"
    # Output goes to files, not pipes: on Windows the daemon inherits the launching command's
    # handles, so a pipe reader would wait for the daemon to exit.
    started = time.perf_counter()
    with open(output / (stem + ".stdout"), "w+b") as out, open(output / (stem + ".stderr"), "w+b") as err:
        try:
            code = subprocess.run(argv, cwd=scratch, env=env, stdin=stdin, stdout=out, stderr=err, timeout=90).returncode
        except subprocess.TimeoutExpired:
            code = 124
            err.write(b"\nProbe subprocess timeout")
        finally:
            if stdin:
                stdin.close()
        elapsed = time.perf_counter() - started
        out.seek(0)
        err.seek(0)
        stdout, stderr = out.read(), err.read()
    record = {"phase": phase, "argv": argv, "elapsed_s": elapsed, "returncode": code,
              "argument_utf8_bytes": sum(len(part.encode("utf-8")) for part in argv[1:]),
              "script_utf8_bytes": script_bytes,
              "stdout_bytes": len(stdout), "stderr_bytes": len(stderr),
              "stdout_path": stem + ".stdout", "stderr_path": stem + ".stderr"}
    report["commands"].append(record)
    return record, stdout.decode("utf-8", errors="replace")


def evaluated(text, as_json):
    """Decode the string an eval returned, from plain output or one JSON response."""
    try:
        if as_json:
            return json.loads(json.loads(text)["data"]["result"])
        return json.loads(json.loads(text.strip().splitlines()[-1]))
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None


def sessions():
    record, text = run(["session", "list"], "teardown", True)
    try:
        return record, json.loads(text)["data"]["sessions"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return record, None


def persist():
    (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")


expected = {"name": "Ada Example", "email": "ada@example.test", "role": "editor", "agree": True,
            "result": "Saved Name=Ada Example; Email=ada@example.test; Role=editor; Agree=true"}
settled = "document.querySelector('#result').textContent === %s && !document.querySelector('#result').hidden" % json.dumps(
    expected["result"])
check = """(() => {
  const form = document.querySelector('#editor form');
  return JSON.stringify({ verified: 'BROWSER_TUNING_VERIFIED_V1', actual: {
    name: form.elements.name.value, email: form.elements.email.value,
    role: form.elements.role.value, agree: form.elements.agree.checked,
    result: document.querySelector('#result').textContent } });
})()"""
actions = [["fill", '#editor [name="name"]', expected["name"]],
           ["fill", '#editor [name="email"]', expected["email"]],
           ["select", '#editor [name="role"]', "editor"],
           ["check", '#editor [name="agree"]'],
           ["click", '#editor button[type="submit"]'],
           ["wait", "--fn", settled]]
separate = actions + [["eval", check]]
variants = [("A", "segmented", False, separate), ("B", "segmented_json", True, separate),
            ("C", "batched", False, [["batch", separate]])]
reset_check = """(() => {
  const form = document.querySelector('#editor form');
  const state = [form.elements.name.value, form.elements.email.value, form.elements.role.value, form.elements.agree.checked];
  if (JSON.stringify(state) !== JSON.stringify(['', '', 'reader', false])) throw new Error('Reset failed');
  if (!document.querySelector('#result').hidden) throw new Error('Result was not reset');
  if (document.querySelector('#fixture-identity').textContent !== 'browser-tuning-fixture-v1') throw new Error('Wrong fixture');
  return JSON.stringify('RESET_VERIFIED');
})()"""
opened = False
try:
    version_record, version = run(["--version"], "setup")
    assert version_record["returncode"] == 0, "CLI version failed"
    report["cli_version"] = version.strip()
    opened = True
    record, _ = run(["open", url], "setup")
    info, text = run(["session", "info"], "setup", True)
    try:
        report["daemon_pid"] = json.loads(text)["data"]["pid"]
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    persist()
    print(f"Metadata ready at {output / 'results.json'}", flush=True)
    assert record["returncode"] == 0, "Browser open failed"
    record, text = run(["snapshot"], "discovery")
    initial_snapshot.write_text(text)
    assert record["returncode"] == 0 and "browser-tuning-fixture-v1" in text, "Snapshot identity failed"
    persist()
    for repetition in range(args.repetitions + 1):
        phase = "warmup" if repetition == 0 else "timed"
        for variant, name, as_json, commands in variants:
            reset, _ = run(["open", f"{url}?reset={repetition}-{variant}"], "reset")
            checked, text = run(["eval", reset_check], "reset")
            assert reset["returncode"] == checked["returncode"] == 0 and evaluated(text, False) == "RESET_VERIFIED", \
                "Fixture reset failed"
            records = []
            started = time.perf_counter()
            for command in commands:
                record, text = run(command, phase, as_json)
                records.append(record)
            elapsed = time.perf_counter() - started
            verified = evaluated(text, as_json) == {"verified": "BROWSER_TUNING_VERIFIED_V1", "actual": expected}
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
        record, _ = run(["screenshot", str(screenshot)], "screenshot")
        png_valid = screenshot.is_file() and screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        report["final_screenshot"] = str(screenshot) if record["returncode"] == 0 and png_valid else None
        record, _ = run(["close"], "teardown")
        close_ok = record["returncode"] == 0
        # A closed session leaves the listing once its daemon has exited.
        deadline = time.monotonic() + 5
        while True:
            listed, names = sessions()
            remaining = None if names is None else [name for name in names if name == session]
            if remaining == [] or time.monotonic() > deadline:
                break
            time.sleep(0.25)
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
