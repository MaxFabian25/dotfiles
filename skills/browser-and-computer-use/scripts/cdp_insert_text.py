#!/usr/bin/env python3
"""Put text from standard input into one field of an open tab in the user's own Chrome, over DevTools.

For a Chrome whose fields take no text through accessibility, such as Chrome on WSLg's display :0. The user first
switches on remote debugging at chrome://inspect/#remote-debugging; connecting then makes Chrome ask "Allow remote
debugging?", and the script waits up to --wait seconds for that answer. It attaches only to the one page tab whose
address starts with --url-prefix, finds the one input, textarea or editable element whose placeholder, aria-label,
name or id matches --field, focuses it, inserts the text, and reads the field back. It prints only whether the field
holds the text, so a secret on standard input never reaches the output.

  uv run --with websockets python -I cdp_insert_text.py --url-prefix https://example.com/ --field '^Email' < value
Exit status 1 means no single tab or field, 2 that the field does not hold the text afterwards.
"""

import argparse
import asyncio
import itertools
import json
import sys
from pathlib import Path

import websockets

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--url-prefix", required=True, help="start of the tab's address")
parser.add_argument("--field", required=True, help="regular expression for the field's placeholder, aria-label, name or id")
parser.add_argument("--profile", default="~/.config/google-chrome", help="Chrome user data directory holding DevToolsActivePort")
parser.add_argument("--wait", type=int, default=180, help="seconds to wait for the user to allow the connection")
args = parser.parse_args()
text = sys.stdin.read().rstrip("\r\n")
if not text:
    sys.exit("no text on standard input")
port, path = (Path(args.profile).expanduser() / "DevToolsActivePort").read_text().split()[:2]

FIND = """((source) => {
  const re = new RegExp(source, 'i');
  const keys = e => [e.placeholder, e.getAttribute('aria-label'), e.getAttribute('name'), e.id];
  return [...document.querySelectorAll('input, textarea, [contenteditable="true"]')].filter(e => keys(e).some(k => k && re.test(k)));
})"""
FOCUS = f"""(() => {{
  const found = {FIND}({json.dumps(args.field)});
  if (found.length !== 1) return 'fields: ' + found.length;
  const el = found[0]; el.focus(); if (el.select) el.select();
  return document.activeElement === el ? 'focused' : 'not focused';
}})()"""
READ = f"""(() => {{ const el = {FIND}({json.dumps(args.field)})[0]; return el ? (el.value ?? el.textContent) : null; }})()"""


async def main():
    ids = itertools.count(1)
    async with websockets.connect(f"ws://127.0.0.1:{port}{path}", max_size=None, open_timeout=args.wait) as ws:
        async def send(method, params=None, session=None):
            number = next(ids)
            message = {"id": number, "method": method, "params": params or {}}
            if session:
                message["sessionId"] = session
            await ws.send(json.dumps(message))
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("id") == number:
                    if "error" in reply:
                        raise RuntimeError(f"{method}: {reply['error'].get('message')}")
                    return reply.get("result", {})

        tabs = [t for t in (await send("Target.getTargets"))["targetInfos"] if t["type"] == "page" and t["url"].startswith(args.url_prefix)]
        if len(tabs) != 1:
            print(f"tabs matching the prefix: {len(tabs)}")
            return 1
        session = (await send("Target.attachToTarget", {"targetId": tabs[0]["targetId"], "flatten": True}))["sessionId"]

        async def evaluate(expression):
            return (await send("Runtime.evaluate", {"expression": expression, "returnByValue": True}, session))["result"].get("value")

        state = await evaluate(FOCUS)
        print(f"field: {state}")
        if state != "focused":
            return 1
        await send("Input.insertText", {"text": text}, session)
        holds = await evaluate(READ) == text
        print(f"field holds the text: {holds}")
        await send("Target.detachFromTarget", {"sessionId": session})
        return 0 if holds else 2


sys.exit(asyncio.run(main()))
