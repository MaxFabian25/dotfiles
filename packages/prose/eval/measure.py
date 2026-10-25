"""Measure each delivered HTML page in a browser at 390 px and 1366 px.

Usage: measure.py [--work DIR] [--map] [--out FILE] LABEL[/TASK]|FOLDER ...
A FOLDER is a blinded task, judge/ROUND/TASK, or one lettered run in it.
Reports the page's scroll width against the viewport, and the smallest rendered text, with SVG text
scaled by its screen transform (canvas text is not measured). With --map, also reports the region
fills and outlines of the SVG with the most paths, blended over the map background, with contrast.
"""

import argparse
from collections import Counter
from pathlib import Path

from harness import Browser, contrast, files_of, lum, pages, specs, work_dir

WIDTHS = (390, 1366)
SMALL = 12
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
  return JSON.stringify({width: document.documentElement.scrollWidth, n: out.length,
                         min: out.length ? out[0][0] : null, small: out.filter(x => x[0] < 11.95)});
})()
"""
MAP = r"""
(() => {
  const svgs = [...document.querySelectorAll('svg')];
  if (!svgs.length) return JSON.stringify({error: 'no svg'});
  const svg = svgs.sort((a, b) => b.querySelectorAll('path').length - a.querySelectorAll('path').length)[0];
  const box = svg.getBoundingClientRect();
  let bg = null;
  for (const r of svg.querySelectorAll('rect')) {
    const b = r.getBoundingClientRect();
    const f = getComputedStyle(r).fill;
    if (b.width * b.height > 0.8 * box.width * box.height && f !== 'none') { bg = f; break; }
  }
  for (let el = svg; !bg && el; el = el.parentElement) {
    const c = getComputedStyle(el).backgroundColor;
    if (c && c !== 'transparent' && !c.endsWith(', 0)')) bg = c;
  }
  const scale = m => (m ? Math.hypot(m.a, m.b) : 1);
  const regions = [];
  for (const p of svg.querySelectorAll('path')) {
    const cs = getComputedStyle(p);
    if (cs.fill === 'none' || cs.display === 'none') continue;
    const b = p.getBoundingClientRect();
    if (b.width < 1 && b.height < 1) continue;
    const fixed = cs.vectorEffect === 'non-scaling-stroke';
    regions.push({fill: cs.fill, fo: +cs.fillOpacity * +cs.opacity, stroke: cs.stroke,
                  so: +cs.strokeOpacity * +cs.opacity,
                  sw: parseFloat(cs.strokeWidth) * (fixed ? 1 : scale(p.getScreenCTM()))});
  }
  return JSON.stringify({bg: bg || 'rgb(255, 255, 255)', regions});
})()
"""


def rgb(css):
    if not css.startswith("rgb"):
        return None
    return [
        float(x) for x in css[css.index("(") + 1 : -1].replace("/", ",").split(",")[:3]
    ]


def blend(c, alpha, bg):
    return [a * alpha + b * (1 - alpha) for a, b in zip(c, bg)]


def ok_l(c):
    """OKLab lightness."""
    lin = [
        x / 255 / 12.92 if x / 255 <= 0.04045 else ((x / 255 + 0.055) / 1.055) ** 2.4
        for x in c
    ]
    l = 0.4122214708 * lin[0] + 0.5363325363 * lin[1] + 0.0514459929 * lin[2]  # noqa: E741
    m = 0.2119034982 * lin[0] + 0.6806995451 * lin[1] + 0.1073969566 * lin[2]
    s = 0.0883024619 * lin[0] + 0.2817188376 * lin[1] + 0.6299787005 * lin[2]
    return (
        0.2104542553 * l ** (1 / 3)
        + 0.7936177850 * m ** (1 / 3)
        - 0.0040720320 * s ** (1 / 3)
    )


def hexed(c):
    return "#" + "".join(f"{round(x):02x}" for x in c)


def map_cells(d):
    """Background, fills lightest first with count and contrast, the three commonest outlines, and the
    OKLab lightness span of the fills."""
    bg = rgb(d["bg"])
    fills = Counter(
        hexed(blend(rgb(r["fill"]), r["fo"], bg))
        for r in d["regions"]
        if rgb(r["fill"])
    )
    strokes = Counter(
        (hexed(blend(rgb(r["stroke"]), r["so"], bg)), round(r["sw"], 2))
        for r in d["regions"]
        if rgb(r["stroke"]) and r["sw"] > 0
    )
    as_rgb = {
        h: [int(h[i : i + 2], 16) for i in (1, 3, 5)]
        for h in list(fills) + [s for s, _ in strokes]
    }
    classes = sorted(fills, key=lambda h: -lum(as_rgb[h]))
    fill_cells = ", ".join(
        f"{h} ×{fills[h]} {contrast(as_rgb[h], bg):.2f}" for h in classes
    )
    stroke_cells = ", ".join(
        f"{h} {w}px ×{n} {contrast(as_rgb[h], bg):.2f}"
        for (h, w), n in strokes.most_common(3)
    )
    span = ok_l(as_rgb[classes[0]]) - ok_l(as_rgb[classes[-1]]) if classes else 0
    return [hexed(bg), fill_cells, stroke_cells or "none", f"{span:.2f}"]


def measure_page(browser, page, with_map):
    """Rows for the width, text and map tables."""
    browser("open", page.resolve().as_uri())
    widths, fonts, maps = [], [], []
    for width in WIDTHS:
        browser.at_width(width)
        f = browser.json(FONTS)
        widths.append(str(f["width"]))
        examples = "; ".join(f"{px}px {text}" for px, text in f["small"][:3])
        fonts.append(
            [str(width), str(f["min"]), f"{len(f['small'])}/{f['n']}", examples]
        )
        if with_map:
            d = browser.json(MAP)
            maps.append(
                [
                    str(width),
                    *(
                        map_cells(d)
                        if "regions" in d
                        else [d.get("error", "?"), "", "", ""]
                    ),
                ]
            )
    fits = widths == [str(w) for w in WIDTHS]
    return [*widths, "yes" if fits else "no"], fonts, maps


def table(header, rows):
    lines = [f"| {' | '.join(header)} |", f"| {' | '.join('---' for _ in header)} |"]
    return "\n".join(lines + [f"| {' | '.join(r)} |" for r in rows])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs", nargs="+", metavar="LABEL[/TASK]|FOLDER")
    ap.add_argument(
        "--map", action="store_true", help="also measure map fills and outlines"
    )
    ap.add_argument("--out", type=Path)
    ap.add_argument("--work")
    a = ap.parse_args()
    work = work_dir(a.work)
    width_rows, font_rows, map_rows = [], [], []
    with Browser("measure") as browser:
        for seat_dir in specs(work, a.runs):
            run = "/".join(seat_dir.parts[-3:])
            for rel in pages(seat_dir):
                widths, fonts, maps = measure_page(
                    browser, files_of(seat_dir) / rel, a.map
                )
                width_rows.append([run, str(rel), *widths])
                font_rows += [[run, str(rel), *r] for r in fonts]
                map_rows += [[run, str(rel), *r] for r in maps]
                print(
                    f"{run} {rel}: scroll widths {widths[0]} and {widths[1]}",
                    flush=True,
                )
    sections = [
        "Scroll width of each page; it fits when it equals the viewport.",
        table(["Run", "Page", "390 px", "1366 px", "Fits"], width_rows),
        f"Smallest rendered text, and how many text runs are below {SMALL} px.",
        table(["Run", "Page", "Width", "Smallest", "Below", "Examples"], font_rows),
    ]
    if a.map:
        sections += [
            "Map region fills, lightest first, with region count and contrast against the background, and the commonest outlines.",
            table(
                [
                    "Run",
                    "Page",
                    "Width",
                    "Background",
                    "Fills",
                    "Outlines",
                    "OKLab L span",
                ],
                map_rows,
            ),
        ]
    text = "\n\n".join(sections) + "\n"
    if a.out:
        a.out.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
