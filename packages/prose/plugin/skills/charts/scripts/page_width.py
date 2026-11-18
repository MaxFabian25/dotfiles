#!/usr/bin/env python3
"""Check that web pages fit the phone and desktop test widths, with no text under 12 px.

Usage: page_width.py PAGE [PAGE ...]

PAGE is an HTML file or an http, https, or file URL. The script loads each page
in agent-browser at 390 px and 1366 px wide and prints, for each width, the
page's scroll width (document.documentElement.scrollWidth), whether it fits
the viewport, and the smallest rendered text, followed by one indented line for
each text run under 12 px. A wide table that scrolls inside its own container
fits. SVG text counts at its computed font size times the scale from
getScreenCTM(). Text drawn on a canvas is not measured.

Exit status: 0 when every page fits at both widths and has no text under 12 px,
1 when any page overflows or has text under 12 px, and 2 when agent-browser is
missing or a page cannot be loaded or measured.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

WIDTHS = (390, 1366)
TOOL = "agent-browser"
MISSING = (
    "page_width.py: agent-browser is not on PATH, so no page was measured. It loads "
    "each page in Chrome: install it with `npm install -g agent-browser` and "
    "`agent-browser install`, then run this again."
)
MEASURE = (
    "JSON.stringify({scroll: document.documentElement.scrollWidth,"
    " viewport: document.documentElement.clientWidth})"
)
# Text runs smaller than this, in px as rendered, fail the charts skill's size rule.
SMALL = 12
# The size of each visible text run, SVG text scaled by its screen transform, smallest first: the
# count, the smallest size, and each run under SMALL px (rounded to 0.1 px) with its first 40 characters.
FONTS = r"""
(() => {
  const out = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    const t = n.textContent.trim();
    const el = n.parentElement;
    if (!t || !el || el.closest('script,style,noscript,title')) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 2 || r.height <= 2) continue;
    let px = parseFloat(cs.fontSize);
    if (el instanceof SVGElement && el.getScreenCTM) {
      const m = el.getScreenCTM();
      if (m) px *= Math.hypot(m.a, m.b);
    }
    out.push([Math.round(px * 10) / 10, t.slice(0, 40)]);
  }
  out.sort((a, b) => a[0] - b[0]);
  return JSON.stringify({n: out.length, min: out.length ? out[0][0] : null,
                         small: out.filter(x => x[0] < 11.95)});
})()
"""


class PageError(Exception):
    pass


class Browser:
    """An agent-browser session of our own, closed when the block ends."""

    def __init__(self, name):
        self.session = f"{name}-{secrets.token_hex(3)}"

    def run(self, *args, stdin=None):
        return subprocess.run(
            [TOOL, "--session", self.session, *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def __call__(self, *args, stdin=None):
        return self.run(*args, stdin=stdin).stdout.strip()

    def open(self, url):
        """Load URL, or raise PageError with agent-browser's reason, so that a page
        that failed to load is never measured as the browser's error page."""
        out = self.run("open", url)
        if out.returncode:
            reason = (out.stdout + out.stderr).strip().splitlines() or ["no reason"]
            raise PageError(f"{url}: agent-browser could not open it: {reason[0]}")

    def at_width(self, width, wait_ms=1500):
        self("set", "viewport", str(width), "900")
        self("reload")
        self("wait", str(wait_ms))

    def json(self, script):
        """Run a script that returns JSON.stringify(...) and parse its result."""
        lines = self("eval", "--stdin", stdin=script).splitlines()
        return json.loads(json.loads(lines[-1])) if lines else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self("close")


class Width(NamedTuple):
    """One page at one test width: the scroll width against the viewport's."""

    width: int
    scroll: int
    viewport: int

    @property
    def fits(self) -> bool:
        return self.scroll <= self.viewport


def url_of(page: str) -> str:
    if re.match(r"(?i)(https?|file)://", page):
        return page
    path = Path(page)
    if not path.is_file():
        raise PageError(f"{page}: no such file")
    return path.resolve().as_uri()


def at_each_width(browser: Browser, url: str) -> Iterator[Width]:
    """Open URL and set each test width in turn, yielding its measurement while the
    page is at that width."""
    browser.open(url)
    for width in WIDTHS:
        browser.at_width(width)
        try:
            measured = browser.json(MEASURE) or {}
            result = Width(width, int(measured["scroll"]), int(measured["viewport"]))
        except (KeyError, ValueError) as error:
            raise PageError(f"{url}: agent-browser could not measure it") from error
        yield result


def text_sizes(browser: Browser, url: str) -> dict:
    """The FONTS measurement of the page at its current width."""
    sizes = browser.json(FONTS)
    if not isinstance(sizes, dict) or "small" not in sizes:
        raise PageError(f"{url}: agent-browser could not measure its text")
    return sizes


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check that web pages fit 390 px and 1366 px wide viewports, with no "
            "text under 12 px."
        )
    )
    parser.add_argument("pages", nargs="+", metavar="PAGE")
    args = parser.parse_args(argv)
    if shutil.which(TOOL) is None:
        print(MISSING, file=sys.stderr)
        return 2
    failed = False
    try:
        with Browser("page-width") as browser:
            for page in args.pages:
                url = url_of(page)
                for result in at_each_width(browser, url):
                    if result.fits:
                        verdict = "fits"
                    else:
                        failed = True
                        verdict = (
                            f"OVERFLOWS the {result.viewport} px viewport by "
                            f"{result.scroll - result.viewport} px"
                        )
                    sizes = text_sizes(browser, url)
                    if sizes["min"] is None:
                        text = "no text measured"
                    else:
                        text = f"smallest text {sizes['min']:g} px"
                    if sizes["small"]:
                        failed = True
                        text += (
                            f", {len(sizes['small'])} of {sizes['n']} text runs "
                            f"UNDER {SMALL} px"
                        )
                    print(
                        f"{page} at {result.width} px: scroll width "
                        f"{result.scroll} px, {verdict}; {text}",
                        flush=True,
                    )
                    for px, words in sizes["small"]:
                        print(f"  {px:g} px: {words}", flush=True)
    except PageError as error:
        print(f"page_width.py: {error}", file=sys.stderr)
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
