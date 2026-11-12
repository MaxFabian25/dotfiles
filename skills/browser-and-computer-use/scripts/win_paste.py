#!/usr/bin/env python3
"""Paste text into a window on the Windows desktop from WSL, at a pixel of its screenshot, through cua-driver.exe.

  python3 -I win_paste.py --pid PID --window-id ID --x X --y Y [--replace] < text

For a window with no accessibility tree, such as an Electron app's message box, where `set_value` has no
route and typed text stops when the window loses focus. Take PID, ID and the pixel from `list_windows` and a
`get_window_state` screenshot. It sends nothing but the click and the paste: look at the window afterwards,
then press the app's own send button yourself.

The steps run in one process, in this order, and each waits for the one before: read the window, read the
user's clipboard, put the text there, read it back and compare, click the pixel, paste, wait, put the user's
clipboard back. The wait comes before the clipboard goes back even when the paste failed, because a paste
that lands late takes whatever the clipboard holds then, and it is at least 10 seconds when the driver did
not answer or this command was interrupted, because the steps may still be running. A signal that arrives
during the wait or while the clipboard goes back is held until both are done. On 11 November 2026 a
paste sent before the clipboard held the text put the user's own clipboard into a message box. The
clipboard's text is held in memory only and never printed, and a message quotes no driver output beyond an
error code. Give the command a time limit of 4 minutes: a driver call may take 20 seconds.

--replace selects all in the clicked field before the paste. The click goes to the window in the background.
The key combinations bring the window to the front for a moment, which the user sees, because a combination
sent in the background can arrive as plain letters. Tell the user before you run it.

Exit 3 means nothing was changed: the clipboard holds something other than plain text, such as an image, a
file or formatted text from a browser or Word, which this cannot put back. Exit 1 is any other failure. Its
message says when the user's clipboard was not put back, and then tell the user. An empty clipboard cannot
be made empty again, so it holds the pasted text afterwards, which the output says.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

DRIVER = "/mnt/c/Users/{user}/AppData/Local/Programs/Cua/cua-driver/bin/cua-driver.exe"
SESSION = f"win-paste-{os.getpid()}"
PLAIN_TEXT = {"CF_LOCALE", "CF_OEMTEXT", "CF_TEXT", "CF_UNICODETEXT"}  # what a text-only clipboard listed, 2026-11-11
TIMEOUT = float(os.environ.get("WIN_PASTE_TIMEOUT") or 20)  # per driver call; the tests shorten it
# How long the clipboard keeps the text when nobody knows whether the steps still run; the tests shorten it.
UNSURE_WAIT = float(os.environ.get("WIN_PASTE_UNSURE_WAIT") or 10)


def say(line):
    try:
        print(line, flush=True)  # flushed, so the lines survive when this command is ended
    except OSError:
        # A reader that closed its end, as `| head -1` does, must not stop the clipboard going back. The
        # null device takes the rest, or Python's own flush at exit fails on the closed pipe with status 120.
        sys.stdout = open(os.devnull, "w")


def stop(message, code=1) -> NoReturn:
    print(f"win_paste: {message}", file=sys.stderr, flush=True)
    sys.exit(code)


class Refused(Exception):
    pass


class Ended(Exception):
    """This command was told to end. Raised from the signal handler so that the clipboard still goes back."""


def why(error):
    """A reason that holds no driver output and no clipboard text: our own messages, or the error's type."""
    return str(error) if isinstance(error, (Refused, Ended)) else f"unexpected {type(error).__name__}"


def call(tool, **arguments):
    """One driver call. A Refused message names the tool and the driver's error code and quotes no output,
    because a reply can hold the user's clipboard text."""
    home = Path(os.environ.get("WIN_PASTE_HOME") or f"/mnt/c/Users/{Path.home().name}")
    driver = os.environ.get("WIN_PASTE_DRIVER") or DRIVER.format(user=home.name)
    try:
        # A Windows program needs a Windows folder as its working directory. Its own session keeps a
        # terminal's Ctrl+C, which goes to the whole group, from ending the call that puts the clipboard back.
        done = subprocess.run([driver, "call", tool], input=json.dumps(arguments).encode(), cwd=home,
                              capture_output=True, timeout=TIMEOUT, start_new_session=True)
    except subprocess.TimeoutExpired:
        raise Refused(f"{tool}: no reply within {TIMEOUT:g} seconds") from None
    except OSError as error:
        raise Refused(f"{tool}: cannot run the driver: {error}") from None
    if done.returncode:
        raise Refused(f"{tool}: the driver exited with status {done.returncode}")
    try:
        reply = json.loads(done.stdout.decode("utf-8"))
    except ValueError:
        raise Refused(f"{tool}: the driver's reply was not JSON in UTF-8") from None
    if not isinstance(reply, dict):
        raise Refused(f"{tool}: the driver's reply was not a JSON object")
    # A refusal can come with exit 0, as `{"error_code": "clipboard_unavailable", "supported": false}` did.
    if reply.get("supported") is False or any(reply.get(key) for key in ("error", "error_code", "isError")):
        raise Refused(f"{tool}: refused, error code {str(reply.get('error_code') or 'not given')[:80]}")
    return reply


def clipboard_text():
    text = call("clipboard_read", include_text=True).get("text")
    return text if isinstance(text, str) else ""


def seconds(value):
    number = float(value)
    if not 0 <= number <= 60:
        raise argparse.ArgumentTypeError("between 0 and 60 seconds")
    return number


def same(a, b):
    return a.replace("\r\n", "\n") == b.replace("\r\n", "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--window-id", type=int, required=True)
    parser.add_argument("--x", type=int, required=True, help="pixel in the window's screenshot")
    parser.add_argument("--y", type=int, required=True)
    parser.add_argument("--replace", action="store_true", help="select all in the field before the paste")
    parser.add_argument("--settle", type=seconds, default=2.0, help="seconds the app gets to take the paste")
    args = parser.parse_args()
    text = sys.stdin.read()
    if not text.strip():
        stop("no text on standard input")
    window = {"pid": args.pid, "window_id": args.window_id, "session": SESSION}

    held = None  # becomes a list once the wait and the restore begin, which a signal must not stop

    def ended(number, _frame):
        if held is None:
            raise Ended(f"ended by signal {number}")
        held.append(number)

    for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(number, ended)

    try:
        call("get_window_state", include_accessibility_tree=False, **window)  # a pixel click needs a read first
        before = call("clipboard_read", include_text=True)
        if not isinstance(before.get("types"), list):
            raise Refused("clipboard_read: the reply does not list the clipboard's types")
        types = {str(kind) for kind in before["types"]}
        saved = before.get("text") if isinstance(before.get("text"), str) else None
        refuse = not types <= PLAIN_TEXT or (bool(types) and saved is None)
    except BaseException as error:
        stop(f"nothing was changed: {why(error)}")
    if refuse:
        kinds = ", ".join(sorted(types - PLAIN_TEXT)[:6]) or "text the driver did not return"
        stop(f"nothing was changed: the clipboard holds more than plain text ({kinds}), which this cannot put "
             "back. Ask the user to copy something else first.", 3)

    # From here on every way out, expected or not, goes through the wait and the restore below.
    failure, wait = None, 0.0
    try:
        try:
            call("clipboard_write", text=text)
            if not same(clipboard_text(), text):
                raise Refused("the clipboard does not hold the text after writing it")
            say("clipboard holds the text")
            keys = [["ctrl", "a"], ["ctrl", "v"]] if args.replace else [["ctrl", "v"]]
            # Sent in the background, a key combination can arrive as its letters alone and still report
            # success: on 11 November 2026 a text box received "av" for Ctrl+A, Ctrl+V. In the foreground the
            # driver brings the window forward for each combination and then puts the user's window back.
            steps = [{"tool": "click", "args": {"x": args.x, "y": args.y}}]
            steps += [{"tool": "hotkey", "args": {"keys": combination, "delivery_mode": "foreground"}}
                      for combination in keys]
            wait = max(args.settle, UNSURE_WAIT)  # until the driver has answered, the steps may still be running
            report = call("run_steps", steps=steps, delay_ms=300, **window)
            wait = args.settle
            results = report.get("steps")
            if (report.get("ok") is not True or not isinstance(results, list) or len(results) != len(steps)
                    or not all(isinstance(step, dict) and step.get("ok") is True for step in results)):
                raise Refused("run_steps: a step failed or is missing from the driver's report")
            say(f"sent the click and {' then '.join('+'.join(c) for c in keys)}")
        except BaseException as error:
            failure = why(error)
        held = []
        # The app reads the clipboard after the keystroke, so the text stays until the paste has had its time.
        time.sleep(wait)
        if saved is None:
            say("the clipboard was empty before, so it holds the pasted text now unless the write failed")
        else:
            call("clipboard_write", text=saved)
            if not same(clipboard_text(), saved):
                raise Refused("it does not hold the user's text after writing it")
            say("the user's clipboard is back")
    except BaseException as error:
        failure = f"{failure + '; ' if failure else ''}the user's clipboard was NOT put back: {why(error)}"
    if held and not failure:
        failure = f"ended by signal {held[0]}"  # the same words as a signal just before the wait
    if failure:
        stop(failure)
    say("pasted; look at the window before you send")


if __name__ == "__main__":
    main()
