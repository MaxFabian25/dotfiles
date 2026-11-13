"""Tests for skills/browser-and-computer-use/scripts/win_paste.py, with a stand-in for cua-driver.exe.

The stand-in keeps a clipboard and a text field in state.json and appends each call to calls.jsonl. A Ctrl+V
step puts the clipboard's text into the field, so a paste sent at the wrong moment shows in the field. With
`late` among the modes the paste lands one second after its step and takes what the clipboard holds then.
The other modes in state.json's `modes` make it misbehave:

  lose-write, lose-restore   ignore every write, or only the second
  refuse-write, refuse-read, refuse-clipboard-read   answer with exit 0 and `supported: false`
  refuse-flag-only, refuse-code-only, refuse-error-only, refuse-iserror-only   clipboard_read refuses with one
      sign alone, and with the clipboard's text
  null-types   clipboard_read's `types` is null
  slow-restore   the second clipboard_write writes the file `restoring`, then waits for the file `released`
  user-copies   the user copies "NEWER" while run_steps runs
  user-copies-image   the user copies an image while run_steps runs, which has no text
  user-copies-formatted   the user copies their earlier text again while run_steps runs, now with formatting
  refuse-last-read   the third clipboard_read, the one after the wait, refuses
  slow-read   get_window_state takes 2 seconds
  hang-read   get_window_state takes 60 seconds
  fail-step, fail-step-only, fail-report, short-report   a failed last step, in the ways a report can show it
  slow-steps   run_steps takes 5 seconds
  noise, exit-1   clipboard_read prints a line before its JSON, or exits 1, both with the clipboard's text
  not-object   every reply is a JSON list
  bad-bytes   clipboard_read's reply holds a byte that is not UTF-8
  no-types   clipboard_read's reply has no `types`
  odd-report   run_steps reports `steps` as a number
  crlf   clipboard_read returns the text with Windows line endings
"""

import contextlib
import importlib.util
import runpy
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "skills/browser-and-computer-use/scripts/win_paste.py"
MINE = "PRIVATE-COPY-OF-THE-USER"
STAND_IN = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
box = Path(__file__).parent
state = json.loads((box / "state.json").read_text())
tool, args = sys.argv[2], json.loads(sys.stdin.read())
with open(box / "calls.jsonl", "a") as log:
    log.write(json.dumps([tool, args]) + "\n")
modes, reply, code = state.get("modes", []), {"supported": True}, 0
state["cwd"] = os.getcwd()
refused = {"error_code": "clipboard_unavailable", "status": "unavailable", "supported": False}

def save():
    (box / "state.json").write_text(json.dumps(state))

if state.get("due") and time.time() >= state["due"]:  # a late paste that has landed by now
    state["field"] += state["text"] or ""
    state["due"] = None
if tool == "clipboard_read":
    text = state["text"].replace("\n", "\r\n") if "crlf" in modes and state["text"] else state["text"]
    reply.update(types=state["types"], text=text if args.get("include_text") else None)
    state["reads"] = state.get("reads", 0) + 1
    if "refuse-clipboard-read" in modes or "refuse-last-read" in modes and state["reads"] == 3:
        reply = refused
    if "refuse-flag-only" in modes:
        reply = {"supported": False, "types": state["types"], "text": state["text"]}
    if "refuse-code-only" in modes:
        reply = {"error_code": "clipboard_busy", "types": state["types"], "text": state["text"]}
    if "refuse-error-only" in modes:
        reply = {"error": "busy", "types": state["types"], "text": state["text"]}
    if "refuse-iserror-only" in modes:
        reply = {"isError": True, "types": state["types"], "text": state["text"]}
    if "null-types" in modes:
        reply["types"] = None
    if "noise" in modes:
        print("notice: " + (state["text"] or ""))
    code = 1 if "exit-1" in modes else 0
    if "no-types" in modes:
        del reply["types"]
    if "bad-bytes" in modes:
        save()
        sys.stdout.buffer.write(json.dumps(reply).encode().replace(b"supported", b"sup\xffported"))
        sys.exit(0)
elif tool == "clipboard_write":
    state["writes"] = state.get("writes", 0) + 1
    if "slow-restore" in modes and state["writes"] == 2:
        (box / "restoring").touch()
        give_up = time.monotonic() + 60  # a test that died before releasing it leaves no stand-in behind
        while not (box / "released").exists() and time.monotonic() < give_up:
            time.sleep(0.05)
    if "refuse-write" in modes:
        reply = refused
    elif "lose-write" not in modes and not ("lose-restore" in modes and state["writes"] == 2):
        state.update(types=["CF_UNICODETEXT", "CF_TEXT"], text=args["text"])
elif tool == "run_steps":
    steps = []
    for index, step in enumerate(args["steps"]):
        ok = not ({"fail-step", "fail-step-only"} & set(modes) and index == len(args["steps"]) - 1)
        keys = step["args"].get("keys")
        if ok and keys == ["ctrl", "a"]:
            state["field"] = ""
        if keys == ["ctrl", "v"] and "late" in modes:
            state["due"] = time.time() + 1
        elif ok and keys == ["ctrl", "v"]:
            state["field"] += state["text"] or ""
        steps.append({"index": index, "ok": ok, "tool": step["tool"]})
    if "user-copies" in modes:
        state.update(types=["CF_UNICODETEXT"], text="NEWER")
    if "user-copies-image" in modes:
        state.update(types=["PNG"], text=None)
    if "user-copies-formatted" in modes:
        state.update(types=["CF_UNICODETEXT", "HTML Format"], text=state["again"])
    if "short-report" in modes:
        steps.pop()
    reply.update(ok="fail-report" not in modes and ("fail-step-only" in modes or all(s["ok"] for s in steps)),
                 steps=steps)
    if "odd-report" in modes:
        reply["steps"] = 3
    if "slow-steps" in modes:
        save()
        time.sleep(5)
elif tool == "get_window_state" and "slow-read" in modes:
    time.sleep(2)
elif tool == "get_window_state" and "hang-read" in modes:
    time.sleep(60)
elif tool == "get_window_state" and "refuse-read" in modes:
    reply = {"error_code": "window_gone", "supported": False}
save()
print(json.dumps([reply] if "not-object" in modes else reply))
sys.exit(code)
'''


class NativeWindows(unittest.TestCase):
    def test_on_windows_itself_it_stops_with_a_sentence_before_anything_runs(self):
        as_windows = f"import runpy, sys; sys.platform = 'win32'; runpy.run_path({str(TOOL)!r}, run_name='__main__')"
        with tempfile.TemporaryDirectory() as box:
            done = subprocess.run(
                [sys.executable, "-I", "-c", as_windows, "--pid", "7", "--window-id", "9", "--x", "40", "--y", "50"],
                input="text", capture_output=True, text=True, timeout=60,
                env={**os.environ, "WIN_PASTE_DRIVER": str(Path(box) / "driver"), "WIN_PASTE_HOME": box})
            self.assertEqual(os.listdir(box), [])
        self.assertEqual((done.returncode, done.stdout, done.stderr),
                         (1, "", "win_paste: run this from WSL, not from Windows itself; nothing was changed\n"))


@unittest.skipIf(sys.platform == "win32", "win_paste.py runs on WSL and handles signals Windows lacks")
class WinPaste(unittest.TestCase):
    def setUp(self):
        box = tempfile.TemporaryDirectory()
        self.addCleanup(box.cleanup)
        self.box = Path(box.name)
        self.driver = self.box / "driver"
        self.driver.write_text(STAND_IN, encoding="utf-8")
        self.driver.chmod(0o755)

    def start(self, *args, timeout="20", unsure="0", driver=None, group=False, **state):
        state = {"types": ["CF_UNICODETEXT", "CF_TEXT", "CF_LOCALE", "CF_OEMTEXT"], "text": MINE, "field": "old ",
                 **state}
        (self.box / "state.json").write_text(json.dumps(state))
        (self.box / "calls.jsonl").unlink(missing_ok=True)
        return subprocess.Popen([sys.executable, "-I", str(TOOL), "--pid", "7", "--window-id", "9", "--x", "40",
                                 "--y", "50", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=group,
                                env={**os.environ, "WIN_PASTE_DRIVER": str(driver or self.driver),
                                     "WIN_PASTE_HOME": str(self.box), "WIN_PASTE_TIMEOUT": timeout,
                                     "WIN_PASTE_UNSURE_WAIT": unsure})

    def wait_for_call(self, tool):
        """Return once the stand-in has logged a call of the tool, which it does as the call starts."""
        log, limit = self.box / "calls.jsonl", time.monotonic() + 30
        while not (log.exists() and f'["{tool}"' in log.read_text()):
            self.assertLess(time.monotonic(), limit, f"no {tool} call within 30 seconds")
            time.sleep(0.05)

    def end(self, child, note):
        out, err = child.communicate(note, timeout=60)
        log = self.box / "calls.jsonl"
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        state = json.loads((self.box / "state.json").read_text())
        if state.get("due"):  # a late paste that has not landed yet takes what the clipboard holds in the end
            state["field"] += state["text"] or ""
        return subprocess.CompletedProcess(child.args, child.returncode, out, err), calls, state

    def paste(self, note, *args, settle="0", **state):
        return self.end(self.start("--settle", settle, *args, **state), note)

    def test_the_text_arrives_and_the_users_clipboard_comes_back_unprinted(self):
        result, calls, state = self.paste("a note\nline two")
        self.assertEqual((result.returncode, state["field"], state["text"]), (0, "old a note\nline two", MINE))
        self.assertEqual(state["cwd"], str(self.box.resolve()))  # the real driver needs a Windows folder to start in
        self.assertEqual([tool for tool, _ in calls], ["get_window_state", "clipboard_read", "clipboard_write",
                                                       "clipboard_read", "run_steps", "clipboard_read",
                                                       "clipboard_write", "clipboard_read"])
        self.assertNotIn(MINE, result.stdout + result.stderr)
        self.assertIn("the user's clipboard is back", result.stdout)

    def test_plain_text_copied_in_a_chromium_app_is_accepted_with_its_two_source_markers(self):
        # What the user's clipboard listed on 12 November 2026, after a copy of plain text in such an app.
        types = ["CF_LOCALE", "CF_OEMTEXT", "CF_TEXT", "CF_UNICODETEXT", "Chromium internal source RFH token",
                 "Chromium internal source URL"]
        result, _, state = self.paste("x", types=types)
        self.assertEqual((result.returncode, state["field"], state["text"]), (0, "old x", MINE))
        result, calls, state = self.paste("x", types=types, modes=["lose-write"])  # the markers are still there
        self.assertEqual((state["text"], [tool for tool, _ in calls].count("clipboard_write")), (MINE, 1))
        self.assertIn("the user's clipboard is back", result.stdout)

    def test_the_click_is_at_the_pixel_and_the_paste_is_sent_in_the_foreground(self):
        _, calls, _ = self.paste("x")
        batch, read = dict(calls)["run_steps"], dict(calls)["get_window_state"]
        self.assertEqual((batch["pid"], batch["window_id"], batch["delay_ms"]), (7, 9, 300))
        self.assertEqual(batch["steps"], [{"tool": "click", "args": {"x": 40, "y": 50}},
                                          {"tool": "hotkey", "args": {"keys": ["ctrl", "v"],
                                                                      "delivery_mode": "foreground"}}])
        self.assertEqual((read["session"], read["include_accessibility_tree"]), (batch["session"], False))

    def test_replace_selects_all_first(self):
        result, calls, state = self.paste("new", "--replace")
        self.assertEqual((result.returncode, state["field"]), (0, "new"))
        self.assertEqual([step["args"].get("keys") for step in dict(calls)["run_steps"]["steps"]],
                         [None, ["ctrl", "a"], ["ctrl", "v"]])

    def test_a_clipboard_that_holds_more_than_plain_text_is_left_alone(self):
        markers = ["Chromium internal source RFH token", "Chromium internal source URL"]
        for types, text in ((["PNG"], None), (["CF_UNICODETEXT", "HTML Format"], MINE), (["CF_UNICODETEXT"], None),
                            (["CF_UNICODETEXT", *markers, "HTML Format"], MINE), (markers, None),
                            (["CF_UNICODETEXT", "Chromium Web Custom MIME Data Format"], MINE)):
            result, calls, state = self.paste("x", types=types, text=text)
            self.assertEqual((result.returncode, state["field"], state["types"], state["text"]),
                             (3, "old ", types, text), types)
            self.assertEqual([tool for tool, _ in calls], ["get_window_state", "clipboard_read"], types)
            self.assertIn("nothing was changed", result.stderr)
            self.assertNotIn(MINE, result.stdout + result.stderr)

    def test_an_empty_clipboard_is_said_to_hold_the_text_afterwards(self):
        result, _, state = self.paste("x", types=[], text=None)
        self.assertEqual((result.returncode, state["field"], state["text"]), (0, "old x", "x"))
        self.assertIn("was empty before, so it holds the pasted text now", result.stdout)
        result, _, state = self.paste("x", types=[], text=None, modes=["refuse-write"])
        self.assertEqual((result.returncode, state["text"]), (1, None))
        self.assertIn("the clipboard is empty, as it was before", result.stdout)
        result, _, state = self.paste("x", types=[], text=None, modes=["user-copies"])
        self.assertEqual((result.returncode, state["text"]), (1, "NEWER"))
        self.assertIn("the clipboard changed during the paste and was left as it is", result.stderr)
        self.assertNotIn("holds the pasted text", result.stdout)

    def test_windows_line_endings_in_the_read_back_are_the_same_text(self):
        result, _, state = self.paste("one\ntwo", modes=["crlf"])
        self.assertEqual((result.returncode, state["field"], state["text"]), (0, "old one\ntwo", MINE))

    def test_nothing_is_pasted_while_the_clipboard_does_not_hold_the_text(self):
        for mode in ("lose-write", "refuse-write"):
            result, calls, state = self.paste("x", modes=[mode])
            self.assertEqual((result.returncode, state["field"], state["text"]), (1, "old ", MINE), mode)
            self.assertNotIn("run_steps", dict(calls), mode)

    def test_a_first_read_that_fails_changes_nothing(self):
        cases = [dict(modes=[mode]) for mode in ("refuse-read", "refuse-clipboard-read", "refuse-flag-only",
                                                 "refuse-code-only", "refuse-error-only", "refuse-iserror-only",
                                                 "null-types", "noise", "exit-1",
                                                 "not-object", "bad-bytes", "no-types")]
        for case in cases + [dict(driver=self.box / "no-such-driver")]:
            result, calls, state = self.paste("x", **case)
            self.assertEqual((result.returncode, state["field"], state["text"]), (1, "old ", MINE), case)
            self.assertNotIn("clipboard_write", dict(calls), case)
            self.assertIn("nothing was changed", result.stderr, case)
            self.assertNotIn("Traceback", result.stderr, case)
            self.assertNotIn("unexpected", result.stderr, case)
            self.assertNotIn(MINE, result.stdout + result.stderr, case)

    def test_being_ended_before_the_first_write_says_that_nothing_was_changed(self):
        child = self.start(modes=["slow-read"])
        child.stdin.write("x")
        child.stdin.close()
        self.wait_for_call("get_window_state")  # by then the tool has set its signal handlers
        child.send_signal(signal.SIGTERM)
        self.assertEqual(child.wait(timeout=30), 1)
        err = child.stderr.read()
        child.stdout.close()
        child.stderr.close()
        self.assertEqual(err, "win_paste: nothing was changed: ended by signal 15\n")

    def test_a_failed_or_missing_step_is_a_failure_and_the_clipboard_still_comes_back(self):
        for mode in ("fail-step", "fail-step-only", "fail-report", "short-report"):
            result, _, state = self.paste("x", modes=[mode])
            self.assertEqual((result.returncode, state["text"]), (1, MINE), mode)
            self.assertIn("run_steps", result.stderr, mode)
            self.assertNotIn("pasted; look", result.stdout, mode)

    def test_a_paste_that_lands_late_still_takes_the_text(self):
        for modes in (["late"], ["late", "fail-step"]):
            result, _, state = self.paste("x", settle="1.6", modes=modes)
            self.assertEqual((state["field"], state["text"]), ("old x", MINE), modes)
            self.assertEqual(result.returncode, 1 if "fail-step" in modes else 0, modes)

    def waits_and_writes(self, run_steps, copied_during_the_wait=None):
        """Run the tool in this process with no real waiting, and return its exit status and, in order, each
        wait and each clipboard write. The tests that measured these on the clock failed on a busy machine on
        12 November 2026, when starting the stand-in alone took longer than their limits."""
        spec = importlib.util.spec_from_file_location("win_paste", TOOL)
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGQUIT):
            self.addCleanup(signal.signal, number, signal.getsignal(number))
        held, events = {"text": MINE}, []

        def call(name, **arguments):
            if name == "clipboard_write":
                held["text"] = arguments["text"]
                events.append(("write", arguments["text"]))
            if name == "run_steps":
                return run_steps(tool)
            return {"types": ["CF_UNICODETEXT"], "text": held["text"]}

        tool.call, tool.UNSURE_WAIT = call, 7.0
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            def wait(seconds):
                events.append(("wait", seconds))
                if copied_during_the_wait:
                    held["text"] = copied_during_the_wait

            stack.enter_context(unittest.mock.patch.object(tool.time, "sleep", wait))
            stack.enter_context(unittest.mock.patch.object(sys, "stdin", io.StringIO("x")))
            stack.enter_context(unittest.mock.patch.object(
                sys, "argv", ["win_paste.py", "--pid", "7", "--window-id", "9", "--x", "1", "--y", "2",
                              "--settle", "0.5"]))
            try:
                tool.main()
                code = 0
            except SystemExit as stopped:
                code = stopped.code
        return code, events

    def test_a_driver_that_does_not_answer_or_refuses_keeps_the_text_for_the_long_wait(self):
        def no_reply(tool):
            raise tool.Refused("run_steps: no reply within 20 seconds")

        self.assertEqual(self.waits_and_writes(no_reply), (1, [("write", "x"), ("wait", 7.0), ("write", MINE)]))

    def test_a_driver_that_answered_gets_the_settle_time_and_not_the_long_wait(self):
        for ok, code in ((True, 0), (False, 1)):
            self.assertEqual(self.waits_and_writes(lambda tool: {"ok": ok, "steps": [{"ok": True}, {"ok": ok}]}),
                             (code, [("write", "x"), ("wait", 0.5), ("write", MINE)]), ok)

    def test_an_error_nobody_expected_in_the_first_reads_says_that_nothing_was_changed(self):
        def call(name, **arguments):
            raise RuntimeError(MINE)

        spec = importlib.util.spec_from_file_location("win_paste", TOOL)
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGQUIT):
            self.addCleanup(signal.signal, number, signal.getsignal(number))
        tool.call, err = call, io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stderr(err))
            stack.enter_context(unittest.mock.patch.object(sys, "stdin", io.StringIO("x")))
            stack.enter_context(unittest.mock.patch.object(
                sys, "argv", ["win_paste.py", "--pid", "7", "--window-id", "9", "--x", "1", "--y", "2"]))
            with self.assertRaises(SystemExit) as stopped:
                tool.run()
        self.assertEqual((stopped.exception.code, err.getvalue()),
                         (1, "win_paste: nothing was changed: unexpected RuntimeError\n"))

    def test_a_copy_the_user_made_during_the_wait_is_left_alone(self):
        self.assertEqual(self.waits_and_writes(lambda tool: {"ok": True, "steps": [{"ok": True}, {"ok": True}]},
                                               copied_during_the_wait="NEWER"),
                         (1, [("write", "x"), ("wait", 0.5)]))

    def test_a_driver_call_that_does_not_end_is_given_up_on(self):
        # Nothing here needs the stand-in to start within the limit: a slow start also ends as no reply.
        result, calls, state = self.paste("x", timeout="0.5", modes=["hang-read"])
        self.assertEqual((result.returncode, state["text"]), (1, MINE))
        self.assertNotIn("clipboard_write", dict(calls))
        self.assertEqual(result.stderr,
                         "win_paste: nothing was changed: get_window_state: no reply within 0.5 seconds\n")

    def test_being_ended_during_the_paste_still_puts_the_clipboard_back_after_the_wait(self):
        for number, reason in ((signal.SIGTERM, "ended by signal 15"), (signal.SIGHUP, "ended by signal 1"),
                               (signal.SIGINT, "ended by signal 2"), (signal.SIGQUIT, "ended by signal 3")):
            child = self.start("--settle", "0", unsure="1.6", modes=["late", "slow-steps"])
            child.stdin.write("x")
            child.stdin.close()
            self.assertEqual(child.stdout.readline(), "clipboard holds the text\n")
            self.wait_for_call("run_steps")  # a fixed pause was too short on a busy machine on 12 November 2026
            # The signal ends the stand-in too, so it must have saved the late paste's due time first.
            limit = time.monotonic() + 30
            while not json.loads((self.box / "state.json").read_text() or "{}").get("due"):
                self.assertLess(time.monotonic(), limit, "the stand-in never saved the late paste's due time")
                time.sleep(0.05)
            child.send_signal(number)
            self.assertEqual(child.wait(timeout=30), 1, reason)
            out, err = child.stdout.read(), child.stderr.read()
            child.stdout.close()
            child.stderr.close()
            state = json.loads((self.box / "state.json").read_text())
            self.assertEqual((state["field"], state["text"]), ("old x", MINE), reason)
            self.assertIn("the user's clipboard is back", out, reason)
            self.assertIn(reason, err, reason)
            self.assertNotIn("Traceback", err, reason)

    def test_being_ended_during_the_wait_still_puts_the_clipboard_back(self):
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGQUIT):
            child = self.start("--settle", "1.5")
            child.stdin.write("x")
            child.stdin.close()
            self.assertEqual(child.stdout.readline(), "clipboard holds the text\n")
            self.assertEqual(child.stdout.readline(), "sent the click and ctrl+v\n")
            child.send_signal(number)
            self.assertEqual(child.wait(timeout=30), 1, number)
            out, err = child.stdout.read(), child.stderr.read()
            child.stdout.close()
            child.stderr.close()
            state = json.loads((self.box / "state.json").read_text())
            self.assertEqual((state["field"], state["text"]), ("old x", MINE), number)
            self.assertEqual(out, "the user's clipboard is back\n", number)
            self.assertEqual(err, f"win_paste: ended by signal {number}\n", number)

    def test_a_terminals_interrupt_during_the_restore_does_not_end_the_call_that_restores(self):
        child = self.start("--settle", "0", group=True, modes=["slow-restore"])
        child.stdin.write("x")
        child.stdin.close()
        self.assertEqual(child.stdout.readline(), "clipboard holds the text\n")
        self.assertEqual(child.stdout.readline(), "sent the click and ctrl+v\n")
        restoring, limit = self.box / "restoring", time.monotonic() + 30
        while not restoring.exists():
            self.assertLess(time.monotonic(), limit, "the restore did not start within 30 seconds")
            time.sleep(0.05)
        os.killpg(child.pid, signal.SIGINT)  # as Ctrl+C does: to the command and every child in its group
        (self.box / "released").touch()
        self.assertEqual(child.wait(timeout=30), 1)
        out, err = child.stdout.read(), child.stderr.read()
        child.stdout.close()
        child.stderr.close()
        state = json.loads((self.box / "state.json").read_text())
        self.assertEqual((state["text"], out, err), (MINE, "the user's clipboard is back\n",
                                                     "win_paste: ended by signal 2\n"))

    def test_a_copy_the_user_made_during_the_paste_is_left_alone_and_said(self):
        for modes in (["user-copies"], ["user-copies", "fail-step"]):
            result, calls, state = self.paste("x", modes=modes)
            self.assertEqual((result.returncode, state["text"]), (1, "NEWER"), modes)
            self.assertEqual([tool for tool, _ in calls].count("clipboard_write"), 1, modes)
            self.assertIn("the clipboard changed during the paste and was left as it is", result.stderr, modes)
            self.assertEqual("run_steps: a step failed" in result.stderr, "fail-step" in modes, modes)
            self.assertNotIn("is back", result.stdout, modes)
            self.assertNotIn("NEWER", result.stdout + result.stderr, modes)

    def test_an_image_the_user_copied_during_the_paste_is_left_alone_and_said(self):
        # An image has no text, so it read as an empty clipboard, or as an empty text still there, and exit 0.
        for before in (dict(types=[], text=None), dict(types=["CF_UNICODETEXT"], text=""), {}):
            result, calls, state = self.paste("x", modes=["user-copies-image"], **before)
            self.assertEqual((result.returncode, state["types"], state["text"]), (1, ["PNG"], None), before)
            self.assertEqual([tool for tool, _ in calls].count("clipboard_write"), 1, before)
            self.assertEqual(result.stdout, "clipboard holds the text\nsent the click and ctrl+v\n", before)
            self.assertIn("the clipboard changed during the paste and was left as it is", result.stderr, before)
        result, calls, state = self.paste("x", modes=["user-copies-formatted"], again=MINE)
        self.assertEqual((result.returncode, state["types"], state["text"]),
                         (1, ["CF_UNICODETEXT", "HTML Format"], MINE))
        self.assertEqual([tool for tool, _ in calls].count("clipboard_write"), 1)
        self.assertNotIn("is back", result.stdout)

    def test_a_read_after_the_wait_that_fails_says_the_clipboard_was_not_put_back(self):
        result, calls, state = self.paste("x", modes=["refuse-last-read"])
        self.assertEqual((result.returncode, state["text"]), (1, "x"))
        self.assertEqual([tool for tool, _ in calls].count("clipboard_write"), 1)
        self.assertEqual(result.stderr, "win_paste: the user's clipboard was NOT put back: clipboard_read: refused, "
                                        "error code clipboard_unavailable\n")
        self.assertNotIn("pasted; look", result.stdout)

    def test_a_signal_as_the_failed_paste_is_noted_is_not_said_to_have_changed_nothing(self):
        # The signal has to arrive on one line, the first of the handler for a failed paste, so a line trace
        # delivers it there. A second signal then finds out whether signals are held from then on.
        spec = importlib.util.spec_from_file_location("win_paste", TOOL)
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGQUIT):
            self.addCleanup(signal.signal, number, signal.getsignal(number))
        lines = TOOL.read_text(encoding="utf-8").splitlines()
        target = [number for number, line in enumerate(lines, 1)
                  if line.strip().startswith("held = []")]  # the first line of the handler for a failed paste
        self.assertEqual(len(target), 1)
        held, why, sent = {"text": MINE}, tool.why, []

        def call(name, **arguments):
            if name == "run_steps":
                raise tool.Refused("run_steps: no reply")
            if name == "clipboard_write":
                held["text"] = arguments["text"]
            return {"types": ["CF_UNICODETEXT"], "text": held["text"]}

        def on_line(frame, event, _argument):
            if event == "line" and frame.f_lineno == target[0] and not sent:
                sent.append(signal.SIGTERM)
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, frame)
            return on_line

        def why_while_a_second_signal_arrives(error):
            sent.append(signal.SIGHUP)
            signal.getsignal(signal.SIGHUP)(signal.SIGHUP, None)
            return why(error)

        tool.call, tool.why, err = call, why_while_a_second_signal_arrives, io.StringIO()
        self.addCleanup(sys.settrace, sys.gettrace())
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(err))
            stack.enter_context(unittest.mock.patch.object(sys, "stdin", io.StringIO("x")))
            stack.enter_context(unittest.mock.patch.object(
                sys, "argv", ["win_paste.py", "--pid", "7", "--window-id", "9", "--x", "1", "--y", "2"]))
            sys.settrace(lambda frame, event, _argument: on_line if frame.f_code.co_name == "main" else None)
            with self.assertRaises(SystemExit) as stopped:
                tool.run()
            sys.settrace(None)
        self.assertEqual(sent, [signal.SIGTERM, signal.SIGHUP])
        self.assertEqual((stopped.exception.code, held["text"], err.getvalue()),
                         (1, "x", "win_paste: the user's clipboard was NOT put back: ended by signal 15\n"))

    def test_a_first_write_that_was_lost_needs_no_second_write(self):
        result, calls, state = self.paste("x", modes=["lose-write"])
        self.assertEqual((state["text"], [tool for tool, _ in calls].count("clipboard_write")), (MINE, 1))
        self.assertIn("the user's clipboard is back", result.stdout)

    def test_a_failed_paste_and_a_failed_restore_are_both_said(self):
        result, _, _ = self.paste("x", modes=["fail-step", "lose-restore"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("run_steps: a step failed", result.stderr)
        self.assertIn("the user's clipboard was NOT put back", result.stderr)

    def test_an_error_nobody_expected_still_puts_the_clipboard_back(self):
        result, _, state = self.paste("x", modes=["odd-report"])
        self.assertEqual((result.returncode, state["text"]), (1, MINE))
        self.assertIn("run_steps", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_an_error_from_outside_the_script_still_puts_the_clipboard_back_or_says_it_did_not(self):
        # No reply of the stand-in raises anything but the script's own errors, so the driver call is replaced.
        spec = importlib.util.spec_from_file_location("win_paste", TOOL)
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGQUIT):
            self.addCleanup(signal.signal, number, signal.getsignal(number))
        why = tool.why

        def why_while_a_signal_arrives(error):
            os.kill(os.getpid(), signal.SIGTERM)
            return why(error)

        for breaks, clipboard, message in (("run_steps", MINE, "win_paste: unexpected RuntimeError\n"),
                                           ("run_steps and a signal", MINE, "win_paste: unexpected RuntimeError\n"),
                                           ("restore", "x", "win_paste: the user's clipboard was NOT put back: "
                                                            "unexpected RuntimeError\n")):
            held, writes = {"text": MINE}, []

            def call(name, **arguments):
                if name == "clipboard_write":
                    writes.append(arguments["text"])
                if name == "run_steps" and breaks != "restore" or len(writes) == 2 and breaks == "restore":
                    raise RuntimeError(held["text"])
                if name == "clipboard_write":
                    held["text"] = arguments["text"]
                return {"types": ["CF_UNICODETEXT"], "text": held["text"],
                        "ok": True, "steps": [{"ok": True}, {"ok": True}]}

            tool.call = call
            tool.why = why_while_a_signal_arrives if breaks == "run_steps and a signal" else why
            out, err = io.StringIO(), io.StringIO()
            with contextlib.ExitStack() as stack:
                stack.enter_context(contextlib.redirect_stdout(out))
                stack.enter_context(contextlib.redirect_stderr(err))
                stack.enter_context(unittest.mock.patch.object(sys, "stdin", io.StringIO("x")))
                stack.enter_context(unittest.mock.patch.object(
                    sys, "argv", ["win_paste.py", "--pid", "7", "--window-id", "9", "--x", "1", "--y", "2",
                                  "--settle", "0"]))
                with self.assertRaises(SystemExit) as stopped:
                    tool.main()
            self.assertEqual((stopped.exception.code, held["text"], err.getvalue()), (1, clipboard, message), breaks)
            self.assertNotIn(MINE, out.getvalue() + err.getvalue(), breaks)

    def test_a_signal_between_the_first_reads_and_the_first_write_says_that_nothing_was_changed(self):
        # The file is run as a command, so this also fails when its last line calls main() and not run().
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGQUIT):
            self.addCleanup(signal.signal, number, signal.getsignal(number))
        leave, left = sys.exit, []

        def leave_while_a_signal_arrives(code=0):
            if not left:  # the refusal of a clipboard that holds an image, which runs between the two blocks
                left.append(code)
                os.kill(os.getpid(), signal.SIGTERM)
            leave(code)

        reply = subprocess.CompletedProcess([], 0, json.dumps({"types": ["PNG"], "text": None}).encode(), b"")
        err = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stderr(err))
            stack.enter_context(unittest.mock.patch.object(sys, "exit", leave_while_a_signal_arrives))
            stack.enter_context(unittest.mock.patch.object(subprocess, "run", lambda *a, **k: reply))
            stack.enter_context(unittest.mock.patch.object(sys, "stdin", io.StringIO("x")))
            stack.enter_context(unittest.mock.patch.object(
                sys, "argv", ["win_paste.py", "--pid", "7", "--window-id", "9", "--x", "1", "--y", "2"]))
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(TOOL), run_name="__main__")
        self.assertEqual((left, stopped.exception.code), ([3], 1))
        self.assertEqual(err.getvalue().splitlines()[-1], "win_paste: nothing was changed: ended by signal 15")

    def test_a_long_format_name_stays_short_and_on_one_line(self):
        result, _, _ = self.paste("x", types=["CF_UNICODETEXT", "odd\nname " + "z" * 300], text=MINE)
        self.assertEqual((result.returncode, result.stderr.count("\n")), (3, 1))
        self.assertLess(len(result.stderr), 250)

    def test_a_reader_that_closed_its_end_does_not_stop_the_clipboard_going_back(self):
        child = self.start("--settle", "0")
        child.stdout.close()
        child.stdin.write("x")
        child.stdin.close()
        self.assertEqual(child.wait(timeout=30), 0)
        err = child.stderr.read()
        child.stderr.close()
        state = json.loads((self.box / "state.json").read_text())
        self.assertEqual((state["field"], state["text"], err), ("old x", MINE, ""))

    def test_a_settle_time_that_is_no_time_is_refused_before_anything_runs(self):
        result, calls, _ = self.paste("x", settle="-1")
        self.assertEqual((result.returncode, calls), (2, []))

    def test_a_clipboard_that_did_not_come_back_is_said_loudly(self):
        result, _, state = self.paste("x", modes=["lose-restore"])
        self.assertEqual((result.returncode, state["text"]), (1, "x"))
        self.assertIn("the user's clipboard was NOT put back", result.stderr)
        self.assertNotIn(MINE, result.stdout + result.stderr)

    def test_no_text_runs_nothing(self):
        result, calls, _ = self.paste("  \n")
        self.assertEqual((result.returncode, calls), (1, []))


if __name__ == "__main__":
    unittest.main()
