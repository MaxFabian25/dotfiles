# Page checks and downloads: bullets from the Browser section of SKILL.md

## A page at a set width

- To render a page at a set width, such as a 390 px phone, run `set viewport <width> <height>` and then `reload`, and confirm the width with `eval 'window.innerWidth'`. Chrome started headless without agent-browser holds its window at 500 px or wider. For sharp phone images, add the device scale as a third argument, `set viewport 390 900 3`, confirm it with `eval 'window.devicePixelRatio'`, and capture one element with `screenshot <selector> <path>`, which gives a 1170 px-wide image of just that section.
- To check a page's layout at set widths, run `scripts/page_check.py <file-or-url>` with Python 3. It measures 390 and 1366 px and flags text under 12 px by default; `--help` lists its checks and options. Look at its contact sheet or one screenshot per revision rather than many full-page captures.

## Hover styles

- Headless Chrome reports no hover pointer, so `@media (hover: hover)` rules never apply and a probe measures the touch styles. To test hover styles, pass `--args "--blink-settings=availableHoverTypes=2,primaryHoverType=2,availablePointerTypes=4,primaryPointerType=4"` on `open` and every later command, as the launch-flag bullet in SKILL.md says, and confirm with `eval 'matchMedia("(hover: hover)").matches'`.

## Probes that wait on the page's timers

- A probe that waits on the page's own timers or `requestAnimationFrame` stalls or reports false misses in a headless tab on a busy machine, because Chrome throttles them. Wait from the driver side, turn on focus emulation, and split a long run, such as by width, so each part ends well inside the shell tool's time limit. For real pointer presses, forced states such as `:hover`, `Emulation.*` settings and awaited evaluations, drive the session's DevTools socket with `node scripts/cdp.mjs`, which attaches to the session's page and turns on focus emulation, rather than writing a driver of your own.

## Downloads

- Download by clicking: pass `--download-path <scratch-directory>` on `open`, click the link, and wait for the file to appear there. In 0.39.1 the `download` command cancels every download on Windows ([vercel-labs/agent-browser#1659](https://github.com/vercel-labs/agent-browser/issues/1659)) and sends later clicks to its own directory. For several files from one signed-in site, fetch each inside the page and return it as base64 through `eval --stdin`, because Chrome blocks a site's second and later automatic downloads; stop a download loop after its first miss.
