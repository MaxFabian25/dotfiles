#!/usr/bin/env python3
"""Run agent-browser commands in one named session with the same launch flags every time.

A later command that leaves out a launch flag, such as --profile, --headed or --idle-timeout, or passes a
different value, restarts the session's browser, and the restart loses the profile's sign-in.

  ab_session.py start NAME FLAG... -- [COMMAND...]   save NAME's flags, then run COMMAND, usually open URL
  ab_session.py attach NAME [COMMAND...]             start NAME on the user's running Chrome, pinned to its tab
  ab_session.py attach --approve[=PID] NAME [COMMAND...]
                                                     the same on Linux, pressing Allow on the prompts this attach raises
  ab_session.py run NAME COMMAND...                  run COMMAND with NAME's saved flags
  ab_session.py close NAME                           close NAME with its saved flags and forget them

attach stops with exit 3 and agent-browser's error when Chrome refused the session or nobody approved it in time.
It closes that session, so attach again. --approve runs the attach under approve_attach.py, which needs uv,
exits 4 without attaching when Chrome was not started with --force-renderer-accessibility, and
takes Chrome's browser process from the default profile's SingletonLock unless PID is given. Use it only after
the user said yes to attaching in this task.

close ends the session's daemon itself when agent-browser's own close fails, as it did on WSL on 10 November
2026 after attaches nobody approved: on Linux it reads the daemon's PID from the session's pid file, checks that
the process carries this session's name, and stops it.

The flags live in ~/.local/state/agent-browser-sessions/NAME.json. For macOS and Linux. On Windows, start the
session with windows-session.ps1 and pass the same flags on every later command.
"""

import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time

STATE = pathlib.Path.home() / ".local/state/agent-browser-sessions"
ATTACH = ["--auto-connect", "--pin-tab"]
CHROME_LOCK = pathlib.Path.home() / ".config/google-chrome/SingletonLock"
# One agent-browser for every call: a shell started before an upgrade still has the older version first on its
# PATH, and two versions on one session restart its daemon, which closes its pages.
SHIMS = pathlib.Path.home() / ".local/share/mise/shims"
if (SHIMS / "agent-browser").exists():
    os.environ["PATH"] = f"{SHIMS}{os.pathsep}{os.environ.get('PATH', '')}"


def state_file(name: str) -> pathlib.Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        sys.exit(f"ab_session: unusable session name {name!r}")
    return STATE / f"{name}.json"


def run(name: str, flags: list[str], command: list[str]) -> None:
    argv = ["agent-browser", "--session", name, *flags, *command]
    os.execvp(argv[0], argv)


def attach_error(name: str, flags: list[str]) -> str | None:
    """Run the session's first command, which makes the attach, and return agent-browser's error when it fails."""
    done = subprocess.run(["agent-browser", "--session", name, *flags, "--json", "tab", "list"],
                          capture_output=True, text=True)
    try:
        reply = json.loads(done.stdout)
    except ValueError:
        reply = {}
    if done.returncode == 0 and reply.get("success"):
        return None
    return reply.get("error") or (done.stdout + done.stderr).strip()[-300:] or f"exit status {done.returncode}"


def chrome_pid(lock: pathlib.Path) -> int:
    """Chrome's browser process, from the HOST-PID target of its profile folder's SingletonLock link."""
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[1])
        os.kill(pid, 0)  # a Chrome that crashed leaves its lock behind
    except PermissionError:
        pass
    except (OSError, IndexError, ValueError):
        sys.exit(f"ab_session: no running Chrome found at {lock}; pass --approve=PID")
    return pid


def approve_command(pid: int, name: str, rest: list[str]) -> list[str]:
    here = pathlib.Path(__file__).resolve().parent
    return ["uv", "run", "-q", "--with", "jeepney", "python", "-I", str(here / "approve_attach.py"), "--pid", str(pid),
            "--", sys.executable, str(here / "ab_session.py"), "attach", name, *rest]


def daemon_pid(name: str, runtime: pathlib.Path, proc: pathlib.Path) -> int | None:
    """The PID in NAME's pid file, or None unless that process carries NAME as its session. The check keeps a
    recycled PID, or another session's daemon, from being stopped."""
    try:
        pid = int((runtime / f"{name}.pid").read_text().strip())
        environment = (proc / str(pid) / "environ").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return None
    return pid if f"AGENT_BROWSER_SESSION={name}".encode() in environment else None


def stop_daemon(name: str, runtime: pathlib.Path, proc: pathlib.Path = pathlib.Path("/proc")) -> bool:
    """End NAME's daemon after its own close failed. True once that process is gone."""
    pid = daemon_pid(name, runtime, proc)
    if pid is None:
        return False
    for sent in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sent)
        except ProcessLookupError:
            return True
        for _ in range(20):
            time.sleep(0.1)
            if daemon_pid(name, runtime, proc) is None:
                print(f"ab_session: {name}'s daemon ignored close, so process {pid} was stopped", file=sys.stderr)
                return True
    return False


def close(name: str, flags: list[str], path: pathlib.Path) -> int:
    result = subprocess.run(["agent-browser", "--session", name, *flags, "close"])
    runtime = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR", "/nonexistent")) / "agent-browser"
    if result.returncode == 0 or stop_daemon(name, runtime):
        path.unlink()
        return 0
    return result.returncode


def main(args: list[str]) -> int:
    if len(args) < 2 or args[0] not in ("start", "attach", "run", "close"):
        sys.exit(__doc__)
    op, name, rest = args[0], args[1], args[2:]
    attach = op == "attach"
    if attach and name.startswith("--approve"):
        if not rest:
            sys.exit("ab_session: attach --approve needs a NAME")
        pid = int(name.split("=", 1)[1]) if "=" in name else chrome_pid(CHROME_LOCK)
        os.execvp("uv", approve_command(pid, rest[0], rest[1:]))
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
        if attach:
            error = attach_error(name, flags)
            if error:
                print(f"ab_session: {name} is not attached: {error}", file=sys.stderr)
                close(name, flags, path)
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
    return close(name, flags, path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
