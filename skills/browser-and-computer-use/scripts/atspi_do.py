#!/usr/bin/env python3
"""List or act on elements in one process's AT-SPI tree, such as Chrome on WSLg's display :0.

Without --action it prints each element whose role and name match, with the actions it offers. With --action it
runs that action on the one matching element through org.a11y.atspi.Action.DoAction, and refuses when none or
several elements match, so a click never lands on a lookalike. --frame limits the walk to the window whose frame
has exactly that name, and --skip-documents leaves page content out, for tab-strip work.

Run it with one D-Bus connection for the whole walk:
  uv run --with jeepney python -I atspi_do.py --pid PID --role 'push button' --name '^Save$' --action press
Exit status 1 means no single match or a refused action.
"""

import argparse
import re
import sys
from collections import deque

from jeepney import DBusAddress, new_method_call
from jeepney.io.blocking import open_dbus_connection

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--pid", type=int, required=True, help="process ID of the app, such as Chrome's browser process")
parser.add_argument("--role", required=True, help="regular expression for the role name, such as 'push button'")
parser.add_argument("--name", required=True, help="regular expression for the accessible name")
parser.add_argument("--action", help="action to run on the single match, such as press, click, check or uncheck")
parser.add_argument("--frame", help="exact name of the window frame to search in")
parser.add_argument("--skip-documents", action="store_true", help="do not descend into page documents")
parser.add_argument("--limit", type=int, default=20000, help="most nodes to visit")
args = parser.parse_args()
role_re, name_re = re.compile(args.role, re.I), re.compile(args.name, re.I)

session = open_dbus_connection(bus="SESSION")
bus = session.send_and_get_reply(new_method_call(DBusAddress("/org/a11y/bus", "org.a11y.Bus", "org.a11y.Bus"), "GetAddress")).body[0]
a11y = open_dbus_connection(bus=bus)


def call(name, path, interface, method, signature=None, body=()):
    return a11y.send_and_get_reply(new_method_call(DBusAddress(path, name, interface), method, signature, body)).body


def prop(name, path, interface, key):
    return call(name, path, "org.freedesktop.DBus.Properties", "Get", "ss", (interface, key))[0][1]


def info(name, path):
    return call(name, path, "org.a11y.atspi.Accessible", "GetRoleName")[0], prop(name, path, "org.a11y.atspi.Accessible", "Name") or ""


def children(name, path):
    return call(name, path, "org.a11y.atspi.Accessible", "GetChildren")[0]


def actions(name, path):
    try:
        count = prop(name, path, "org.a11y.atspi.Action", "NActions")
        return [call(name, path, "org.a11y.atspi.Action", "GetName", "i", (i,))[0] for i in range(count)]
    except Exception:
        return []


def bus_pid(name):
    message = new_method_call(DBusAddress("/org/freedesktop/DBus", "org.freedesktop.DBus", "org.freedesktop.DBus"), "GetConnectionUnixProcessID", "s", (name,))
    return a11y.send_and_get_reply(message).body[0]


roots = []
for name, path in children("org.a11y.atspi.Registry", "/org/a11y/atspi/accessible/root"):
    try:
        if bus_pid(name) == args.pid:
            roots.append((name, path))
    except Exception:
        pass
if not roots:
    sys.exit(f"no accessible application for pid {args.pid}")
if args.frame:
    roots = [(n, p) for root in roots for n, p in children(*root) if info(n, p) == ("frame", args.frame)]
    if len(roots) != 1:
        sys.exit(f"expected one frame named {args.frame!r}, found {len(roots)}")

matches, queue, seen = [], deque(roots), 0
while queue and seen < args.limit:
    name, path = queue.popleft()
    seen += 1
    try:
        role, label = info(name, path)
    except Exception:
        continue
    if role_re.search(role) and name_re.search(label):
        matches.append((name, path, role, label))
    if args.skip_documents and role == "document web":
        continue
    try:
        queue.extend(children(name, path))
    except Exception:
        pass

for name, path, role, label in matches[:20]:
    print(f"{role} {label!r} actions={actions(name, path)}")
if not args.action:
    sys.exit(0 if matches else 1)
if len(matches) != 1:
    sys.exit(f"{len(matches)} matches after {seen} nodes; act only on exactly one")
name, path, _, _ = matches[0]
offered = actions(name, path)
if args.action not in offered:
    sys.exit(f"action {args.action!r} not offered")
done = call(name, path, "org.a11y.atspi.Action", "DoAction", "i", (offered.index(args.action),))[0]
print(f"{args.action}: {done}")
sys.exit(0 if done else 1)
