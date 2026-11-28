#!/usr/bin/env python3
"""Verify Visual Kit HTML artifacts against static and optional browser quality gates.

Usage:
  check.py PAGE [PAGE ...] [--browser]
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

WIDTHS = (390, 728)
MIN_TEXT_PX = 12.0

EXTERNAL_URL_RE = re.compile(
    r"""<(?:link|script)\b[^>]*(?:href|src)\s*=\s*["']https?://""",
    re.IGNORECASE,
)
ROOT_BLOCK_RE = re.compile(
    r"""(?:^|[}\s,])(?:html|body)(?:\s*,\s*(?:html|body))*\s*\{([^}]*)\}""",
    re.IGNORECASE | re.DOTALL,
)
CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
BG_DECL_RE = re.compile(r"""(?:^|;)\s*background(?:-color)?\s*:\s*([^;]+)""", re.IGNORECASE)
VH_OR_FULL_HEIGHT_RE = re.compile(
    r"""(?:^|;)\s*(?:min-|max-)?height\s*:\s*(?:100%|\d+(?:\.\d+)?(?:d|s|l)?vh)\b""",
    re.IGNORECASE,
)

BROWSER_PROBE = r"""
(() => {
  const scroll = document.documentElement.scrollWidth;
  const viewport = document.documentElement.clientWidth;
  const geist = document.fonts ? document.fonts.check('14px "Geist"') : null;
  const runs = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    const t = n.textContent.trim();
    const el = n.parentElement;
    if (!t || !el || el.closest('script,style,noscript,title,[hidden]')) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) continue;
    let px = parseFloat(cs.fontSize);
    if (el instanceof SVGElement && el.getScreenCTM) {
      const m = el.getScreenCTM();
      if (m) px *= Math.hypot(m.a, m.b);
    }
    runs.push([Math.round(px * 10) / 10, t.slice(0, 40)]);
  }
  runs.sort((a, b) => a[0] - b[0]);
  return JSON.stringify({
    scroll,
    viewport,
    geist,
    minPx: runs.length ? runs[0][0] : null,
    small: runs.filter(x => x[0] < 11.95)
  });
})()
"""


class _TagAuditor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inaccessible_clicks: list[tuple[int, str]] = []
        self.unlabeled_svgs: list[int] = []
        self.body_mode: str = "t3"
        self.in_style = False
        self.styles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        t = tag.lower()
        if t == "body":
            self.body_mode = attr_map.get("data-mode", "t3") or "t3"
        elif t == "style":
            self.in_style = True
        elif t in ("div", "span") and "onclick" in attr_map:
            if attr_map.get("role") not in ("button", "tab") or "tabindex" not in attr_map:
                self.inaccessible_clicks.append((self.getpos()[0], t))
        elif t == "svg":
            aria_hidden = attr_map.get("aria-hidden", "").lower() == "true"
            has_img_label = attr_map.get("role") == "img" and bool(attr_map.get("aria-label", "").strip())
            if not aria_hidden and not has_img_label:
                self.unlabeled_svgs.append(self.getpos()[0])

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "style":
            self.in_style = False

    def handle_data(self, data: str) -> None:
        if self.in_style:
            self.styles.append(data)


def check_static(html_text: str) -> list[str]:
    issues: list[str] = []
    auditor = _TagAuditor()
    auditor.feed(html_text)
    css_text = "\n".join(auditor.styles)

    if "'Geist'" not in css_text and '"Geist"' not in css_text:
        issues.append("missing explicit Geist font declaration in <style>")

    if EXTERNAL_URL_RE.search(html_text):
        issues.append("external http(s) <link> or <script> breaks self-contained HTML requirement")

    stripped_css = CSS_COMMENT_RE.sub("", css_text)
    for block_match in ROOT_BLOCK_RE.finditer(stripped_css):
        body = block_match.group(1)
        if VH_OR_FULL_HEIGHT_RE.search(body):
            issues.append("html or body rule uses vh or height:100%, which breaks T3 html_render")
        if auditor.body_mode == "t3":
            for bg_match in BG_DECL_RE.finditer(body):
                val = bg_match.group(1).strip().lower()
                if val not in ("transparent", "none", "inherit"):
                    issues.append("t3-mode body sets an opaque background instead of transparent")

    for line, tag in auditor.inaccessible_clicks:
        issues.append(f"line {line}: <{tag} onclick=...> is not keyboard accessible; use <button> or <details>")

    for line in auditor.unlabeled_svgs:
        issues.append(f"line {line}: <svg> needs role=\"img\" and aria-label, or aria-hidden=\"true\"")

    return issues


def check_browser(path: Path) -> list[str]:
    if shutil.which("agent-browser") is None:
        return ["agent-browser not on PATH; cannot run --browser check"]
    session = f"vk-check-{secrets.token_hex(3)}"
    url = path.resolve().as_uri()
    issues: list[str] = []

    def run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["agent-browser", "--session", session, *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=90,
        )

    try:
        opened = run("open", url)
        if opened.returncode != 0:
            return [f"agent-browser failed to open {path}: {(opened.stdout + opened.stderr).strip()}"]
        for width in WIDTHS:
            run("set", "viewport", str(width), "900")
            run("reload")
            run("wait", "600")
            res = run("eval", "--stdin", stdin=BROWSER_PROBE)
            lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
            if not lines:
                issues.append(f"{width}px: browser probe returned empty output")
                continue
            data = json.loads(json.loads(lines[-1]))
            if data["scroll"] > data["viewport"]:
                issues.append(
                    f"{width}px: horizontal overflow (scrollWidth {data['scroll']} > viewport {data['viewport']})"
                )
            if data["geist"] is False:
                issues.append(f"{width}px: Geist font did not resolve in document.fonts")
            if data["small"]:
                sample = ", ".join(f"{px}px '{txt}'" for px, txt in data["small"][:4])
                issues.append(f"{width}px: {len(data['small'])} text run(s) under {MIN_TEXT_PX:g}px ({sample})")
    finally:
        run("close")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Visual Kit HTML artifacts.")
    parser.add_argument("pages", nargs="+", type=Path, help="HTML file(s) to check.")
    parser.add_argument("--browser", action="store_true", help="Also verify overflow, font, and SVG text via agent-browser.")
    args = parser.parse_args(argv)

    failed = False
    for page in args.pages:
        if not page.is_file():
            print(f"FAIL {page}: file not found", file=sys.stderr)
            failed = True
            continue
        issues = check_static(page.read_text(encoding="utf-8"))
        if args.browser and not issues:
            issues.extend(check_browser(page))
        if issues:
            failed = True
            print(f"FAIL {page}:")
            for item in issues:
                print(f"  - {item}")
        else:
            print(f"PASS {page}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
