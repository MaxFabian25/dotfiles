#!/usr/bin/env python3
"""Run agent-browser commands in one named session with the same launch flags every time.

A later command that leaves out a launch flag, such as --profile, --headed or --idle-timeout, or passes a
different value, restarts the session's browser, and the restart loses the profile's sign-in.

  ab_session.py start NAME FLAG... -- [COMMAND...]   save NAME's flags, then run COMMAND, usually open URL
  ab_session.py run NAME COMMAND...                  run COMMAND with NAME's saved flags
  ab_session.py close NAME                           close NAME with its saved flags and forget them

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


def state_file(name: str) -> pathlib.Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        sys.exit(f"ab_session: unusable session name {name!r}")
    return STATE / f"{name}.json"


def run(name: str, flags: list[str], command: list[str]) -> None:
    argv = ["agent-browser", "--session", name, *flags, *command]
    os.execvp(argv[0], argv)


def main(args: list[str]) -> int:
    if len(args) < 2 or args[0] not in ("start", "run", "close"):
        sys.exit(__doc__)
    op, name, rest = args[0], args[1], args[2:]
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
