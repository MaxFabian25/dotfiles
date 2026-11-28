# Visual Kit HTML guide

Read this file when generating an interactive HTML visual from `show-me`. All paths below are relative to the plugin root (`../..` from `skills/show-me/`).

## Build and check commands

```bash
python3 ../../scripts/build.py \
  --input /abs/path/body.html \
  --output /abs/path/visual.html \
  --title "Short descriptive title" \
  --style nova \
  --mode t3 \
  --features stepper,diagram

python3 ../../scripts/check.py /abs/path/visual.html
```

- **`--style`**: `nova` (default, cobalt accent + Geist Sans and Mono), `vega` (quiet monochrome zinc, useful for dense code or diffs), or `maia` (warm stone + Geist Serif headings for reading guides).
- **`--mode`**: `t3` (default, transparent `html`/`body` matching T3 Code `html_render`) or `standalone` (centered 920 px framed page with `light-dark()` OKLCH ground for browser opening).
- **`--features`**: comma-separated subset of `core`, `stepper`, `diagram`, `diff`. `build.py` tree-shakes unused modules and always includes `core` when any feature is selected.
- **`--extra-css` / `--extra-js`**: optional files for bespoke interactive controls or SVG charts.
- **`--browser` on `check.py`**: when `agent-browser` is available and a visual uses custom SVG or dense tables, run `check.py --browser` to confirm zero overflow and rendered text at 12 px or above at 390 px and 728 px.

## Visual rules enforced by the kit

- **Geist typography**: `--vk-font-sans` (`Geist`), `--vk-font-mono` (`Geist Mono` strictly for code, paths, diffs, and tabular figures), and `--vk-font-serif` (`Geist Serif` in `maia`).
- **T3 `html_render` contract**: keep `html`, `body`, and `.vk-root` transparent with zero outer card, zero `vh`, and zero `height: 100%`.
- **Clean chrome**: omit eyebrow kickers above headings, left-accent bars on rounded boxes, nested cards inside cards, and entrance animations on initial load.
- **Accessible controls and graphics**: use `<button>` and `<details>` (never clickable `<div>` or `<span>`), give every chart or diagram `<svg>` `role="img"` and `aria-label`, and give decorative icons `aria-hidden="true"`.
- **Responsive SVG text**: use `.vk-flow` HTML cards for flow diagrams (stacks vertically under 520 px so labels never shrink below 12 px), and use `window.VK.fitSvg(svg, height, drawFn)` for custom SVG charts so `viewBox` width matches real pixel width at 390 px and 728 px.

## Six interactive archetypes

### 1. Interactive stepper (`--features stepper`)

Prefer a 3 to 5 stage stepper over one crowded all-in-one diagram.

```html
<section class="vk-panel" data-vk-stepper>
  <div class="vk-panel-head">
    <div>
      <h2 class="vk-title">Request lifecycle</h2>
      <p class="vk-sub">Use tabs or Left/Right arrow keys to step through each state.</p>
    </div>
    <span class="vk-badge vk-badge-ok">3 stages</span>
  </div>
  <div class="vk-tabs" role="tablist" aria-label="Stages">
    <button class="vk-tab" role="tab" aria-selected="true" data-step="s1">1. Validate</button>
    <button class="vk-tab" role="tab" aria-selected="false" data-step="s2">2. Execute</button>
    <button class="vk-tab" role="tab" aria-selected="false" data-step="s3">3. Persist</button>
  </div>
  <div data-step-panel="s1" class="vk-caption"><span><strong>Validate.</strong> Schema checks run at the boundary.</span></div>
  <div data-step-panel="s2" class="vk-caption" hidden><span><strong>Execute.</strong> Pure domain logic computes next state.</span></div>
  <div data-step-panel="s3" class="vk-caption" hidden><span><strong>Persist.</strong> Single SQL transaction commits the update.</span></div>
</section>
```

### 2. Clickable responsive flow map (`--features diagram`)

Each `.vk-node` opens the shared native `popover="auto"` inspector with its file path and invariant.

```html
<section class="vk-panel">
  <div class="vk-diagram">
    <div class="vk-flow">
      <button class="vk-node" data-vk-node data-active="true" data-title="Router" data-desc="Decodes params with Schema." data-path="src/http/router.ts:24">
        <span class="vk-node-title">Router</span>
        <span class="vk-node-sub">router.ts:24</span>
      </button>
      <div class="vk-flow-arrow" data-active="true" aria-hidden="true">→</div>
      <button class="vk-node" data-vk-node data-title="Service" data-desc="Runs inside an Effect scope." data-path="src/domain/service.ts:51">
        <span class="vk-node-title">Service</span>
        <span class="vk-node-sub">service.ts:51</span>
      </button>
    </div>
  </div>
  <div id="vk-inspector" popover="auto" class="vk-popover">
    <h4 data-vk-pop-title></h4>
    <p data-vk-pop-body></p>
    <code class="vk-code-inline" data-vk-pop-path></code>
  </div>
</section>
```

### 3. Explorable playground (`--features core` + `--extra-js`)

Bind `<input type="range">` inside `.vk-controls` and `.vk-field` to `.vk-kpis` readouts and a `VK.fitSvg(...)` chart so the reader can test parameters directly.

### 4. Comparison and tradeoff matrix

Use `.vk-table-wrap` and `.vk-table` with `.vk-num` on numeric columns and signed changes (`+12 ms`, `-40%`). Wide tables scroll inside `.vk-table-wrap` without overflowing the page.

### 5. Annotated code and collapsible anatomy

Use `details.vk-card` with `.vk-code` and `.vk-callout` (`.vk-callout-warn` for reviewer gotchas) to present a plain-English summary above expandable implementation detail.

### 6. Data and chart blocks (`--features core` + `--extra-js`)

Render responsive inline SVG inside `.vk-chart-box` using `window.VK.fitSvg('#chart', 170, (svg, w, h) => ...)`. Use `--vk-chart-1` through `--vk-chart-6`, keep text at 12 px or above, label key values directly in `tabular-nums`, and add a `.vk-chart-source` line with the measure and source.
