#!/usr/bin/env python3
"""Run a command that attaches to the user's Chrome on Linux, and approve only the prompts that command raises.

Chrome asks "Allow remote debugging?" for each new DevTools connection. This script records the Allow buttons
already on screen, starts COMMAND, and presses each Allow button that appears while COMMAND runs, through AT-SPI
on the session's accessibility bus. Chrome ignores a press that lands as the prompt appears, so it presses again
while the prompt stays. It never presses a prompt that was there before it started, because that one belongs to
another connection. It exits with COMMAND's status.

  uv run --with jeepney python -I approve_attach.py --pid CHROME_PID -- python3 ab_session.py attach NAME

`ab_session.py attach --approve NAME` runs exactly that. Use it only after the user said yes to attaching in this
task. CHROME_PID is Chrome's browser process, the one
without --type= in its arguments. --max caps the presses, and a prompt left over from a failed attempt stays on
screen until someone cancels it. For Linux, WSLg's display :0 included.
"""

import argparse
import subprocess
import sys
import time
from collections import deque

PROMPT = ("alert", "Allow remote debugging?")
BUTTON = ("push button", "Allow")
PAGE = "document web"
REPRESS = 1  # seconds; a press Chrome accepts takes the prompt down within this


def allow_buttons(roots, info, children):
    """The Allow button of every remote-debugging prompt under roots. `info` gives a node's role and name and
    `children` its child nodes. Page content is left out, and so is an Allow button outside such a prompt, such
    as a site permission bubble's."""
    found, queue = set(), deque((root, False) for root in roots)
    while queue:
        node, in_prompt = queue.popleft()
        try:
            role, label = info(node)
        except Exception:
            continue
        if role == PAGE:
            continue
        if in_prompt and (role, label) == BUTTON:
            found.add(node)
        in_prompt = in_prompt or (role, label) == PROMPT
        try:
            queue.extend((child, in_prompt) for child in children(node))
        except Exception:
            pass
    return found


def approve(process, buttons, press, limit, report, clock=time.monotonic, pause=0.2):
    """Press each Allow button that appears while process runs, on at most `limit` prompts, and return the
    process's status. `buttons` lists the buttons on screen now; the ones present at the start are never pressed.
    A prompt this run pressed that is still up after REPRESS seconds is pressed again."""
    earlier = buttons()
    report(f"{len(earlier)} earlier prompt(s) left alone")
    start, pressed = clock(), {}  # each prompt this run approved, with the time of its last press
    while process.poll() is None:
        for button in sorted(buttons() - earlier):
            now, again = clock(), button in pressed
            skip = now - pressed[button] < REPRESS if again else len(pressed) >= limit
            if skip or process.poll() is not None:
                continue
            done = press(button)
            pressed[button] = now
            report(f"pressed Allow {'again ' if again else ''}after {now - start:.1f}s: {bool(done)}")
        time.sleep(pause)
    return process.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pid", type=int, required=True, help="process ID of Chrome's browser process")
    parser.add_argument("--max", type=int, default=4, help="most prompts to approve")
    parser.add_argument("command", nargs="+", help="the attach command, after --")
    args = parser.parse_args()

    from jeepney import DBusAddress, new_method_call
    from jeepney.io.blocking import open_dbus_connection

    session = open_dbus_connection(bus="SESSION")
    address = session.send_and_get_reply(
        new_method_call(DBusAddress("/org/a11y/bus", "org.a11y.Bus", "org.a11y.Bus"), "GetAddress")).body[0]
    a11y = open_dbus_connection(bus=address)

    def call(node, interface, method, signature=None, body=()):
        name, path = node
        return a11y.send_and_get_reply(new_method_call(DBusAddress(path, name, interface), method, signature, body)).body

    def info(node):
        role = call(node, "org.a11y.atspi.Accessible", "GetRoleName")[0]
        label = call(node, "org.freedesktop.DBus.Properties", "Get", "ss", ("org.a11y.atspi.Accessible", "Name"))
        return role, label[0][1] or ""

    def children(node):
        return [tuple(child) for child in call(node, "org.a11y.atspi.Accessible", "GetChildren")[0]]

    def bus_pid(name):
        message = new_method_call(DBusAddress("/org/freedesktop/DBus", "org.freedesktop.DBus", "org.freedesktop.DBus"),
                                  "GetConnectionUnixProcessID", "s", (name,))
        return a11y.send_and_get_reply(message).body[0]

    roots = []
    for node in children(("org.a11y.atspi.Registry", "/org/a11y/atspi/accessible/root")):
        try:
            if bus_pid(node[0]) == args.pid:
                roots.append(node)
        except Exception:
            pass
    if not roots:
        sys.exit(f"approve_attach: no accessible application for pid {args.pid}")

    def buttons():
        return allow_buttons(roots, info, children)

    def press(button):
        return call(button, "org.a11y.atspi.Action", "DoAction", "i", (0,))[0]

    earlier_report = lambda text: print(f"approve_attach: {text}", file=sys.stderr)  # noqa: E731
    buttons()  # a broken bus stops here, before the command starts
    sys.exit(approve(subprocess.Popen(args.command), buttons, press, args.max, earlier_report))


if __name__ == "__main__":
    main()
