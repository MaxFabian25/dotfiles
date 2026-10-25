#!/usr/bin/env python3
"""Measure WCAG contrast between colours as drawn.

Usage:
  contrast.py FG BG [FG BG ...]      each pair, foreground first
  contrast.py --bg BG FG [FG ...]    several foregrounds on one background
Options:
  --page COLOUR   what a translucent background sits on (default white)
  --min RATIO     exit 1 when any pair falls below RATIO, such as 4.5 or 3
  --json          print one JSON object per pair

Colours: #rgb, #rgba, #rrggbb, #rrggbbaa, rgb()/rgba(), hsl()/hsla(),
oklch(), white, black, and transparent. A foreground with alpha is composited
over its background, and a background with alpha over the page, the way a
browser draws them, before the ratio is measured. oklch() colours outside sRGB
are clipped, which can differ slightly from a browser's gamut mapping.

Ratios are shown truncated, never rounded up, because WCAG gives no credit for
rounding: 4.499:1 fails 4.5:1.
"""

from __future__ import annotations

import argparse
import colorsys
import json
import math
import re
import sys

NAMED = {
    "white": (1.0, 1.0, 1.0, 1.0),
    "black": (0.0, 0.0, 0.0, 1.0),
    "transparent": (0.0, 0.0, 0.0, 0.0),
}
FUNCTION = re.compile(r"^(rgba?|hsla?|oklch)\((.*)\)$")


def parse_alpha(token: str) -> float:
    if token.endswith("%"):
        return float(token[:-1]) / 100
    return float(token)


def split_arguments(body: str) -> tuple[list[str], str | None]:
    alpha = None
    if "/" in body:
        body, alpha = body.split("/", 1)
        alpha = alpha.strip()
    parts = [p for p in re.split(r"[\s,]+", body.strip()) if p]
    if alpha is None and len(parts) == 4:
        alpha = parts.pop()
    return parts, alpha


def oklch_to_srgb(lightness: float, chroma: float, hue: float) -> tuple[float, ...]:
    a = chroma * math.cos(math.radians(hue))
    b = chroma * math.sin(math.radians(hue))
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    linear = (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )
    return tuple(encode(min(1.0, max(0.0, c))) for c in linear)


def parse_colour(text: str) -> tuple[float, ...]:
    value = text.strip().lower()
    if value in NAMED:
        return NAMED[value]
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) in (3, 4):
            digits = "".join(d * 2 for d in digits)
        if len(digits) not in (6, 8) or not re.fullmatch(r"[0-9a-f]+", digits):
            raise ValueError(f"not a hex colour: {text}")
        channels = [int(digits[i : i + 2], 16) / 255 for i in range(0, len(digits), 2)]
        if len(channels) == 3:
            channels.append(1.0)
        return tuple(channels)
    match = FUNCTION.match(value)
    if not match:
        raise ValueError(f"unrecognised colour: {text}")
    kind, body = match.groups()
    parts, alpha_token = split_arguments(body)
    if len(parts) != 3:
        raise ValueError(f"expected three channels: {text}")
    alpha = parse_alpha(alpha_token) if alpha_token else 1.0
    if kind.startswith("rgb"):
        rgb = [
            float(p[:-1]) / 100 if p.endswith("%") else float(p) / 255 for p in parts
        ]
    elif kind.startswith("hsl"):
        hue = float(parts[0].removesuffix("deg")) % 360 / 360
        saturation, light = (float(p.rstrip("%")) / 100 for p in parts[1:])
        rgb = list(colorsys.hls_to_rgb(hue, light, saturation))
    else:
        lightness = (
            float(parts[0][:-1]) / 100 if parts[0].endswith("%") else float(parts[0])
        )
        chroma = (
            float(parts[1][:-1]) * 0.004 if parts[1].endswith("%") else float(parts[1])
        )
        hue = 0.0 if parts[2] == "none" else float(parts[2].removesuffix("deg"))
        rgb = list(oklch_to_srgb(lightness, chroma, hue))
    return (*(min(1.0, max(0.0, c)) for c in rgb), min(1.0, max(0.0, alpha)))


def encode(linear: float) -> float:
    if linear <= 0.0031308:
        return 12.92 * linear
    return 1.055 * linear ** (1 / 2.4) - 0.055


def linearise(channel: float) -> float:
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def over(top: tuple[float, ...], below: tuple[float, ...]) -> tuple[float, ...]:
    alpha = top[3]
    return tuple(alpha * t + (1 - alpha) * b for t, b in zip(top[:3], below[:3]))


def luminance(rgb: tuple[float, ...]) -> float:
    r, g, b = (linearise(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str, page: str = "white") -> float:
    page_rgb = over(parse_colour(page), (1.0, 1.0, 1.0))
    bg_rgb = over(parse_colour(bg), page_rgb)
    fg_rgb = over(parse_colour(fg), bg_rgb)
    high, low = sorted((luminance(fg_rgb), luminance(bg_rgb)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def truncate(ratio: float) -> str:
    return f"{math.floor(ratio * 100) / 100:.2f}"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Measure WCAG contrast between colours."
    )
    parser.add_argument("colours", nargs="+")
    parser.add_argument("--bg", help="one background for every foreground")
    parser.add_argument("--page", default="white")
    parser.add_argument("--min", type=float, dest="minimum")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.bg:
        pairs = [(fg, args.bg) for fg in args.colours]
    elif len(args.colours) % 2:
        parser.error("give colours in FG BG pairs, or one background with --bg")
    else:
        pairs = list(zip(args.colours[::2], args.colours[1::2]))
    failed = False
    for fg, bg in pairs:
        try:
            ratio = contrast(fg, bg, args.page)
        except ValueError as error:
            parser.error(str(error))
        text_ok, marks_ok = ratio >= 4.5, ratio >= 3
        below = args.minimum is not None and ratio < args.minimum
        failed = failed or below
        if args.json:
            print(
                json.dumps(
                    {
                        "fg": fg,
                        "bg": bg,
                        "ratio": round(ratio, 4),
                        "text": text_ok,
                        "large_text_and_marks": marks_ok,
                        "below_min": below,
                    }
                )
            )
        else:
            verdict = (
                f"text 4.5 {'pass' if text_ok else 'FAIL'}, marks and large text 3 "
            )
            verdict += "pass" if marks_ok else "FAIL"
            flag = f"  below --min {args.minimum:g}" if below else ""
            print(f"{fg} on {bg}: {truncate(ratio)}:1  {verdict}{flag}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
