#!/usr/bin/env python3
"""Run a command that attaches to the user's Chrome on Linux, and approve only the prompts that command raises.

Chrome asks "Allow remote debugging?" for each new DevTools connection. This script records the Allow buttons
already on screen, starts COMMAND, and presses each Allow button that appears while COMMAND runs, through AT-SPI
on the session's accessibility bus. It never presses a prompt that was there before it started, because that one
belongs to another connection. It exits with COMMAND's status.

  uv run --with jeepney python -I approve_attach.py --pid CHROME_PID -- python3 ab_session.py attach NAME

Use it only after the user said yes to attaching in this task. CHROME_PID is Chrome's browser process, the one
without --type= in its arguments. --max caps the presses, and a prompt left over from a failed attempt stays on
screen until someone cancels it. For Linux, WSLg's display :0 included.
"""

import argparse
import subprocess
import sys
import time
from collections import deque

from jeepney import DBusAddress, new_method_call
from jeepney.io.blocking import open_dbus_connection

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--pid", type=int, required=True, help="process ID of Chrome's browser process")
parser.add_argument("--max", type=int, default=4, help="most prompts to approve")
parser.add_argument("command", nargs="+", help="the attach command, after --")
args = parser.parse_args()

session = open_dbus_connection(bus="SESSION")
address = session.send_and_get_reply(
    new_method_call(DBusAddress("/org/a11y/bus", "org.a11y.Bus", "org.a11y.Bus"), "GetAddress")).body[0]
a11y = open_dbus_connection(bus=address)


def call(name, path, interface, method, signature=None, body=()):
    return a11y.send_and_get_reply(new_method_call(DBusAddress(path, name, interface), method, signature, body)).body


def info(name, path):
    role = call(name, path, "org.a11y.atspi.Accessible", "GetRoleName")[0]
    label = call(name, path, "org.freedesktop.DBus.Properties", "Get", "ss", ("org.a11y.atspi.Accessible", "Name"))
    return role, label[0][1] or ""


def children(name, path):
    return call(name, path, "org.a11y.atspi.Accessible", "GetChildren")[0]


def bus_pid(name):
    message = new_method_call(DBusAddress("/org/freedesktop/DBus", "org.freedesktop.DBus", "org.freedesktop.DBus"),
                              "GetConnectionUnixProcessID", "s", (name,))
    return a11y.send_and_get_reply(message).body[0]


roots = []
for name, path in children("org.a11y.atspi.Registry", "/org/a11y/atspi/accessible/root"):
    try:
        if bus_pid(name) == args.pid:
            roots.append((name, path))
    except Exception:
        pass
if not roots:
    sys.exit(f"approve_attach: no accessible application for pid {args.pid}")


def allow_buttons():
    """The Allow buttons of every "Allow remote debugging?" prompt on screen, page content left out."""
    found, queue = set(), deque((root, False) for root in roots)
    while queue:
        (name, path), in_prompt = queue.popleft()
        try:
            role, label = info(name, path)
        except Exception:
            continue
        if role == "document web":
            continue
        if in_prompt and role == "push button" and label == "Allow":
            found.add((name, path))
        in_prompt = in_prompt or (role == "alert" and label == "Allow remote debugging?")
        try:
            queue.extend((child, in_prompt) for child in children(name, path))
        except Exception:
            pass
    return found


seen = allow_buttons()
print(f"approve_attach: {len(seen)} earlier prompt(s) left alone", file=sys.stderr)
process = subprocess.Popen(args.command)
start, pressed = time.monotonic(), 0
while process.poll() is None:
    for name, path in sorted(allow_buttons() - seen):
        seen.add((name, path))
        if pressed >= args.max or process.poll() is not None:
            continue
        done = call(name, path, "org.a11y.atspi.Action", "DoAction", "i", (0,))[0]
        pressed += 1
        print(f"approve_attach: pressed Allow after {time.monotonic() - start:.1f}s: {bool(done)}", file=sys.stderr)
    time.sleep(0.2)
sys.exit(process.returncode)
