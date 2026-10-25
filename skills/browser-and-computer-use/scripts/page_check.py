#!/usr/bin/env python3
"""Check a web page's layout at set widths with agent-browser, in a session of its own.

At each width it reports the page's scrollWidth against the viewport, elements that stick out past the viewport
outside a scrolling container, the containers that scroll sideways, visible text below --min-text pixels, and
elements matching --nowrap that wrap onto a second line. With --pdf it also prints the page to PDF, counts its
pages, and draws a contact sheet of them; with --shots it saves a full-page screenshot per width. It closes its
session when done. Exit status 1 means a problem was found, 3 that agent-browser failed.

Look at the contact sheet or one screenshot per revision instead of many separate captures.
"""

import argparse
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

MEASURE = r'''
(() => {
  const min = __MIN__, nowrap = __NOWRAP__, vw = window.innerWidth, doc = document.documentElement;
  const all = [...document.querySelectorAll('body *')];
  const name = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id :
    el.classList.length ? '.' + [...el.classList].slice(0, 2).join('.') : '');
  const scrollers = all.filter(el => /(auto|scroll)/.test(getComputedStyle(el).overflowX)
    && el.scrollWidth > el.clientWidth + 1);
  const wide = [];
  for (const el of all) {
    const box = el.getBoundingClientRect();
    if (!box.width || box.right <= vw + 1 || scrollers.some(s => s !== el && s.contains(el))) continue;
    if (wide.length < 8) wide.push(`${name(el)} reaches ${Math.round(box.right)} px`);
  }
  const small = [];
  let smallCount = 0;
  for (const el of all) {
    if (![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) continue;
    const style = getComputedStyle(el), size = parseFloat(style.fontSize);
    if (style.display === 'none' || style.visibility === 'hidden' || !el.getClientRects().length) continue;
    if (size < min) {
      smallCount += 1;
      if (small.length < 5) small.push(`${name(el)} ${size}px: ${el.textContent.trim().slice(0, 40)}`);
    }
  }
  const wrapped = [];
  if (nowrap) for (const el of document.querySelectorAll(nowrap)) {
    const style = getComputedStyle(el);
    const line = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.2;
    if (el.getClientRects().length > 1 || el.getBoundingClientRect().height > line * 1.5)
      wrapped.push(el.textContent.trim().slice(0, 40));
  }
  return {innerWidth: vw, scrollWidth: doc.scrollWidth, height: doc.scrollHeight, wide, smallCount, small, wrapped,
          scrollers: scrollers.map(s => `${name(s)} ${s.scrollWidth}/${s.clientWidth}`)};
})()
'''


class Browser:
    def __init__(self, session):
        self.session = session

    def run(self, *args, stdin=None):
        result = subprocess.run(['agent-browser', '--session', self.session, '--json', *args], input=stdin,
                                capture_output=True, text=True, encoding='utf-8', timeout=120)
        try:
            data = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError(f'agent-browser {args[0]}: {(result.stdout + result.stderr)[-400:]}') from None
        if not data.get('success'):
            raise RuntimeError(f'agent-browser {args[0]}: {data.get("error")}')
        return data.get('data') or {}


def page_count(pdf):
    if shutil.which('pdfinfo'):
        info = subprocess.run(['pdfinfo', str(pdf)], capture_output=True, text=True).stdout
        found = re.search(r'^Pages:\s+(\d+)', info, re.MULTILINE)
        if found:
            return int(found[1])
    return None


def contact_sheet(pdf, out):
    """Tile the PDF's pages into one image; needs pdftoppm and Pillow, and returns None without them."""
    if not shutil.which('pdftoppm'):
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    prefix = out / 'sheet-page'
    subprocess.run(['pdftoppm', '-r', '40', '-png', str(pdf), str(prefix)], check=True)
    pages = sorted(out.glob('sheet-page*.png'))
    images = [Image.open(p) for p in pages]
    if not images:
        return None
    columns = min(6, len(images))
    width, height = max(i.width for i in images), max(i.height for i in images)
    rows = -(-len(images) // columns)
    sheet = Image.new('RGB', (columns * width + (columns + 1) * 8, rows * height + (rows + 1) * 8), 'white')
    for index, image in enumerate(images):
        sheet.paste(image, (8 + index % columns * (width + 8), 8 + index // columns * (height + 8)))
        image.close()
    for page in pages:
        page.unlink()
    path = out / 'contact-sheet.png'
    sheet.save(path)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('page', help='a local HTML file or a URL')
    parser.add_argument('--widths', default='390,1366', help='comma-separated viewport widths in px')
    parser.add_argument('--height', type=int, default=900)
    parser.add_argument('--min-text', type=float, default=12, help='smallest allowed text size in px')
    parser.add_argument('--nowrap', default='', help='CSS selector of labels that must stay on one line')
    parser.add_argument('--out', type=Path, help='folder for --pdf and --shots output')
    parser.add_argument('--pdf', action='store_true', help='print to PDF, count pages, and draw a contact sheet')
    parser.add_argument('--shots', action='store_true', help='save a full-page screenshot per width')
    args = parser.parse_args()
    if (args.pdf or args.shots) and not args.out:
        parser.error('--pdf and --shots need --out')
    path = Path(args.page)
    url = path.resolve().as_uri() if path.exists() else args.page
    widths = [int(w) for w in args.widths.split(',') if w.strip()]
    script = MEASURE.replace('__MIN__', json.dumps(args.min_text)).replace('__NOWRAP__', json.dumps(args.nowrap))
    browser = Browser(f'page-check-{secrets.token_hex(3)}')
    problems = 0
    try:
        browser.run('open', url)
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
        for width in widths:
            browser.run('set', 'viewport', str(width), str(args.height))
            browser.run('reload')
            browser.run('wait', '--load', 'load')
            m = browser.run('eval', '--stdin', stdin=script).get('result') or {}
            issues = []
            if m.get('innerWidth') != width:
                issues.append(f"viewport is {m.get('innerWidth')} px, not {width}")
            if m.get('scrollWidth', 0) > m.get('innerWidth', width):
                issues.append(f"page scrolls sideways: scrollWidth {m['scrollWidth']} px")
            issues += [f'sticks out: {w}' for w in m.get('wide', [])]
            if m.get('smallCount'):
                issues.append(f"{m['smallCount']} text elements below {args.min_text:g} px, such as "
                              + '; '.join(m.get('small', [])))
            issues += [f'wraps: {w}' for w in m.get('wrapped', [])]
            problems += len(issues)
            print(f"{width} px: scrollWidth {m.get('scrollWidth')}, height {m.get('height')} px, "
                  f"{'no problems' if not issues else str(len(issues)) + ' problems'}")
            for line in issues:
                print(f'  {line}')
            for scroller in m.get('scrollers', []):
                print(f'  scrolls inside: {scroller}')
            if args.shots:
                shot = args.out / f'width-{width}.png'
                browser.run('screenshot', '--full', str(shot))
                print(f'  screenshot: {shot}')
        if args.pdf:
            pdf = args.out / 'page.pdf'
            browser.run('pdf', str(pdf))
            count = page_count(pdf)
            sheet = contact_sheet(pdf, args.out)
            print(f"PDF: {pdf}, {count if count is not None else 'unknown number of'} pages"
                  + (f', contact sheet {sheet}' if sheet else ''))
        return 1 if problems else 0
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f'page_check stopped: {error}', file=sys.stderr)
        return 3
    finally:
        try:
            browser.run('close')
        except (RuntimeError, OSError, subprocess.SubprocessError):
            pass


if __name__ == '__main__':
    raise SystemExit(main())
