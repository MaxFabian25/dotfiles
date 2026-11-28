#!/usr/bin/env python3
"""Compile a concise HTML body fragment into one self-contained Visual Kit HTML file.

Usage:
  build.py --input body.html --output out.html --title "Title" [--style nova|vega|maia]
           [--mode t3|standalone] [--features core,stepper,diagram,diff] [--diffs diffs.json]
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
KIT_DIR = PLUGIN_DIR / "kit"
RUNTIME_DIR = KIT_DIR / "runtime"

STYLES = ("nova", "vega", "maia")
MODES = ("t3", "standalone")
FEATURES = ("core", "stepper", "diagram", "diff")


def _safe_json_for_script(raw_json: str) -> str:
    parsed = json.loads(raw_json)
    dumped = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    return dumped.replace("</", "<\\/").replace("<!--", "<\\!--")


def parse_features(spec: str) -> list[str]:
    if not spec.strip():
        return []
    requested = [item.strip() for item in spec.split(",") if item.strip()]
    unknown = [item for item in requested if item not in FEATURES]
    if unknown:
        raise ValueError(f"Unknown feature(s): {', '.join(unknown)}. Allowed: {', '.join(FEATURES)}")
    ordered: list[str] = []
    if requested and "core" not in requested:
        ordered.append("core")
    for name in FEATURES:
        if name in requested and name not in ordered:
            ordered.append(name)
    return ordered


def compile_html(
    body_fragment: str,
    *,
    title: str = "Visual",
    style: str = "nova",
    mode: str = "t3",
    features: list[str] | None = None,
    extra_css: str = "",
    extra_js: str = "",
    diffs_json: str | None = None,
) -> str:
    if style not in STYLES:
        raise ValueError(f"Unknown style '{style}'. Allowed: {', '.join(STYLES)}")
    if mode not in MODES:
        raise ValueError(f"Unknown mode '{mode}'. Allowed: {', '.join(MODES)}")
    stripped = body_fragment.lstrip().lower()
    if stripped.startswith("<!doctype") or stripped.startswith("<html"):
        raise ValueError(
            "build.py expects an inner HTML body fragment, not a full <!doctype html> document."
        )

    feat_list = features or []
    if diffs_json is not None and "diff" not in feat_list:
        feat_list = [*feat_list, "diff"]
    if feat_list and "core" not in feat_list:
        feat_list = ["core", *feat_list]

    tokens_css = (KIT_DIR / "tokens.css").read_text(encoding="utf-8")
    components_css = (KIT_DIR / "components.css").read_text(encoding="utf-8")
    css_parts = [tokens_css, components_css]
    if extra_css.strip():
        css_parts.append(extra_css)
    combined_css = "\n".join(css_parts)

    js_parts = [(RUNTIME_DIR / f"{name}.js").read_text(encoding="utf-8") for name in feat_list]
    if extra_js.strip():
        js_parts.append(extra_js)
    script_block = ""
    if js_parts:
        combined_js = "\n".join(js_parts).replace("</script", "<\\/script")
        script_block = f"\n<script>\n{combined_js}\n</script>"

    diffs_block = ""
    if diffs_json is not None:
        safe = _safe_json_for_script(diffs_json)
        diffs_block = f'\n<script id="vk-diffs-json" type="application/json">{safe}</script>'

    escaped_title = html.escape(title, quote=True)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escaped_title}</title>\n"
        "<style>\n"
        f"{combined_css}\n"
        "</style>\n"
        "</head>\n"
        f'<body data-mode="{mode}">\n'
        f'<div class="vk-root" data-style="{style}">\n'
        f"{body_fragment.rstrip()}\n"
        "</div>"
        f"{diffs_block}"
        f"{script_block}\n"
        "</body>\n"
        "</html>\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile a self-contained Visual Kit HTML file.")
    parser.add_argument("--input", "-i", help="Body HTML fragment file path, or '-' for stdin.")
    parser.add_argument("--output", "-o", default="-", help="Output HTML file path, or '-' for stdout.")
    parser.add_argument("--title", default="Visual", help="Document title.")
    parser.add_argument("--style", default="nova", choices=STYLES, help="Visual preset (default: nova).")
    parser.add_argument("--mode", default="t3", choices=MODES, help="t3 (transparent) or standalone.")
    parser.add_argument("--features", default="", help=f"Comma-separated JS modules: {', '.join(FEATURES)}.")
    parser.add_argument("--extra-css", help="Optional path to extra CSS to inline.")
    parser.add_argument("--extra-js", help="Optional path to extra JS to inline.")
    parser.add_argument("--diffs", help="Optional JSON file mapping diff keys to unified diff strings.")
    parser.add_argument("--list-features", action="store_true", help="List available runtime modules and exit.")
    args = parser.parse_args(argv)

    if args.list_features:
        print("\n".join(FEATURES))
        return 0

    if not args.input:
        parser.error("--input is required unless --list-features is set")

    body = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
    extra_css = Path(args.extra_css).read_text(encoding="utf-8") if args.extra_css else ""
    extra_js = Path(args.extra_js).read_text(encoding="utf-8") if args.extra_js else ""
    diffs_json = Path(args.diffs).read_text(encoding="utf-8") if args.diffs else None
    features = parse_features(args.features)

    rendered = compile_html(
        body,
        title=args.title,
        style=args.style,
        mode=args.mode,
        features=features,
        extra_css=extra_css,
        extra_js=extra_js,
        diffs_json=diffs_json,
    )

    if args.output == "-":
        sys.stdout.write(rendered)
    else:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
