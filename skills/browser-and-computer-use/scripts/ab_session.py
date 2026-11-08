#!/usr/bin/env python3
"""Run agent-browser commands in one named session with the same launch flags every time.

A later command that leaves out a launch flag, such as --profile, --headed or --idle-timeout, or passes a
different value, restarts the session's browser, and the restart loses the profile's sign-in.

  ab_session.py start NAME FLAG... -- [COMMAND...]   save NAME's flags, then run COMMAND, usually open URL
  ab_session.py attach NAME [COMMAND...]             start NAME on the user's running Chrome, pinned to its tab
  ab_session.py run NAME COMMAND...                  run COMMAND with NAME's saved flags
  ab_session.py close NAME                           close NAME with its saved flags and forget them

attach stops with exit 3 when the session reached only about:blank, the hidden browser a failed attach starts.
Close that session and attach again.

The flags live in ~/.local/state/agent-browser-sessions/NAME.json. For macOS and Linux. On Windows, start the
session with windows-session.ps1 and pass the same flags on every later command.
"""

import json
import os
import pathlib
import re
import subprocess
import sys

STATE = pathlib.Path.home() / ".local/state/agent-browser-sessions"
ATTACH = ["--auto-connect", "--pin-tab"]


def state_file(name: str) -> pathlib.Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        sys.exit(f"ab_session: unusable session name {name!r}")
    return STATE / f"{name}.json"


def run(name: str, flags: list[str], command: list[str]) -> None:
    argv = ["agent-browser", "--session", name, *flags, *command]
    os.execvp(argv[0], argv)


def reached_chrome(name: str, flags: list[str]) -> bool:
    done = subprocess.run(["agent-browser", "--session", name, *flags, "--json", "tab", "list"],
                          capture_output=True, text=True)
    if done.returncode:
        return False
    return any(tab.get("url") != "about:blank" for tab in json.loads(done.stdout)["data"]["tabs"])


def main(args: list[str]) -> int:
    if len(args) < 2 or args[0] not in ("start", "attach", "run", "close"):
        sys.exit(__doc__)
    op, name, rest = args[0], args[1], args[2:]
    attach = op == "attach"
    if attach:
        op, rest = "start", [*ATTACH, "--", *rest]
    path = state_file(name)
    if op == "start":
        if "--" not in rest:
            sys.exit("ab_session: start needs FLAG... -- [COMMAND...]")
        cut = rest.index("--")
        flags, command = rest[:cut], rest[cut + 1:]
        if path.exists():
            saved = json.loads(path.read_text())["flags"]
            if saved != flags:
                sys.exit(f"ab_session: {name} already runs with {saved}; close it first")
        else:
            STATE.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"flags": flags}))
        if attach and not reached_chrome(name, flags):
            print(f"ab_session: {name} reached only about:blank, so it is not attached; close it and attach again",
                  file=sys.stderr)
            return 3
        if command:
            run(name, flags, command)
        return 0
    if not path.exists():
        sys.exit(f"ab_session: no saved flags for {name}; run start first")
    flags = json.loads(path.read_text())["flags"]
    if op == "run":
        if not rest:
            sys.exit("ab_session: run needs a COMMAND")
        run(name, flags, rest)
    result = subprocess.run(["agent-browser", "--session", name, *flags, "close"])
    if result.returncode == 0:
        path.unlink()
    return result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
