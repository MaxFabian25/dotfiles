#!/usr/bin/env python3
"""Attach to a throwaway Chrome several times and print how many attaches worked. For Linux.

Run it after an agent-browser or Chrome version change, and after a change to ab_session.py or approve_attach.py:

  python3 attach_selftest.py [--cycles N] [--new-tab]

The unit tests of those scripts run against stand-ins, so they cannot show that a new Chrome or agent-browser
still behaves as the scripts expect. On 10 November 2026 Chrome ignored an Allow press that landed as its prompt
appeared, and only attaches to a real Chrome showed it.

It makes a folder under ~/.local/state with a Chrome profile that has remote debugging switched on, starts
Chrome on it as a systemd user unit, and runs `ab_session.py attach --approve` and `close` N times. The user's
own Chrome and profile are never touched, but a Chrome window opens on their desktop for the run. --new-tab
starts Chrome on its New Tab page, which agent-browser's tab list leaves out. It stops that Chrome and removes
the folder, and exits 0 when every attach worked.
"""

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--cycles", type=int, default=5, help="attach and close this many times")
parser.add_argument("--new-tab", action="store_true", help="start Chrome on its New Tab page instead of about:blank")
parser.add_argument("--scripts", type=pathlib.Path, default=HERE, help="folder with ab_session.py and approve_attach.py")
args = parser.parse_args()
if sys.platform != "linux":
    sys.exit("attach_selftest: Linux only, because the approval goes through AT-SPI")

tag = f"attach-selftest-{os.getpid()}"
home = pathlib.Path.home() / ".local/state" / tag
profile = home / ".config/google-chrome"
profile.mkdir(parents=True)
(profile / "Local State").write_text(json.dumps({"devtools": {"remote_debugging": {"user-enabled": True}}}))

# The children get the throwaway home, so agent-browser finds this Chrome and saves its session files there. They
# keep the caller's tools: a mise shim would look for its settings in that home.
found = shutil.which("agent-browser") or sys.exit("attach_selftest: agent-browser is not on PATH")
real = subprocess.run(["mise", "which", "agent-browser"], capture_output=True, text=True).stdout.strip() or found
env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"),
       "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(pathlib.Path.home() / ".cache/uv")),
       "PATH": f"{pathlib.Path(real).parent}:{os.environ['PATH']}"}
chrome = ["systemd-run", "--user", f"--unit={tag}", "--collect", "--quiet", *(f"--setenv={key}={env[key]}" for key in
          ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME")), "/usr/bin/env", "-u", "WAYLAND_DISPLAY",
          f"DISPLAY={os.environ.get('DISPLAY', ':0')}", shutil.which("google-chrome") or "google-chrome",
          "--force-renderer-accessibility", "--password-store=basic", "--no-first-run", "--no-default-browser-check",
          *([] if args.new_tab else ["about:blank"])]


def chrome_pid():
    """The browser process that holds the throwaway profile, once it has written its DevTools port."""
    try:
        pid = int(os.readlink(profile / "SingletonLock").rsplit("-", 1)[1])
        os.kill(pid, 0)
    except (OSError, IndexError, ValueError):
        return None
    return pid if (profile / "DevToolsActivePort").exists() else None


worked = 0
try:
    subprocess.run(chrome, check=True)
    deadline = time.monotonic() + 30
    while not (pid := chrome_pid()):
        if time.monotonic() > deadline:
            sys.exit("attach_selftest: Chrome did not start within 30 s")
        time.sleep(0.5)
    for cycle in range(1, args.cycles + 1):
        name = f"{tag}-{cycle}"
        session = [sys.executable, str(args.scripts / "ab_session.py")]
        try:
            attach = subprocess.run([*session, "attach", f"--approve={pid}", name], env=env, capture_output=True,
                                    text=True, timeout=90)
            status, output = attach.returncode, attach.stdout + attach.stderr
        except subprocess.TimeoutExpired as hung:
            status, output = "timeout", f"{hung.stdout or ''}{hung.stderr or ''}"
        worked += status == 0
        print(f"attach {cycle}: {'ok' if status == 0 else f'FAILED ({status})'}", flush=True)
        if status != 0:
            print(str(output)[-600:], flush=True)
        subprocess.run([*session, "close", name], env=env, capture_output=True, text=True, timeout=60)
        if not chrome_pid():
            sys.exit(f"attach_selftest: Chrome stopped after attach {cycle}")
finally:
    subprocess.run(["systemctl", "--user", "stop", tag], capture_output=True)
    shutil.rmtree(home, ignore_errors=True)
    # agent-browser 0.42.0 leaves a closed session's .target file behind in its runtime folder.
    for left in (pathlib.Path(os.environ.get("XDG_RUNTIME_DIR", "/nonexistent")) / "agent-browser").glob(f"{tag}-*.target"):
        left.unlink(missing_ok=True)
print(f"{worked} of {args.cycles} attached")
sys.exit(0 if worked == args.cycles else 1)
