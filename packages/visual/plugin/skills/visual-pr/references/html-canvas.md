# Interactive HTML PR walkthrough

Read this file for `visual-pr`'s default HTML canvas mode. Paths below are relative to the plugin root (`../..` from `skills/visual-pr/`).

## Structure the walkthrough

Order the page like a peer walking a reviewer through the PR, never alphabetically by path:

1. **Header panel (`.vk-panel`):** PR title, number, base/head branch, author, and compact badges (`+N`, `-N`, `N files`) plus a 2 to 4 sentence plain-English summary of what changed and why.
2. **Optional architecture or flow map:** include a `.vk-flow` or stepper block when the PR changes control flow, data boundaries, or state transitions.
3. **Core file cards (`details.vk-card open`):** for each key file, show a concise reviewer annotation or plain-English pseudocode block (`.vk-code`) and any gotcha callout (`.vk-callout-warn`), with the full diff tucked inside a collapsed `details.vk-fold` (`<div data-vk-diff="key"></div>`).
4. **Mechanical or boilerplate files (`details.vk-card` closed):** group lockfiles, renames, generated files, and repetitive test updates collapsed by default with a one-line note.
5. **Review checklist (`.vk-panel` with `.vk-checklist`):** 3 to 5 concrete verification questions for the reviewer.

## Compile, verify, and display

Save a JSON file mapping each `data-vk-diff` key to its unified diff string (or array of diff lines) and write only the inner HTML body fragment:

```html
<details class="vk-card" open>
  <summary>
    <span class="vk-card-title">
      <svg class="vk-chev" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 4l4 4-4 4"/></svg>
      src/http/retryClient.ts
    </span>
    <span class="vk-badges">
      <span class="vk-badge vk-badge-ok">+18</span>
      <span class="vk-badge vk-badge-danger">-5</span>
    </span>
  </summary>
  <div class="vk-card-body">
    <div class="vk-callout vk-callout-warn"><strong>Check signal propagation.</strong> Confirm backoff sleep cancels on abort.</div>
    <pre class="vk-code">fetchWithRetry(req, signal):
  fail fast if circuit is open
  retry 429/5xx with full jitter</pre>
    <details class="vk-fold">
      <summary><span>Show diff (imports hidden, moved code detected)</span></summary>
      <div data-vk-diff="retryClient"></div>
    </details>
  </div>
</details>
```

Compile and check with Visual Kit (`--style vega` keeps chrome quiet for dense diffs; `--style nova` highlights flow diagrams):

```bash
python3 ../../scripts/build.py \
  --input /abs/path/pr-body.html \
  --diffs /abs/path/pr-diffs.json \
  --output /abs/path/pr-walkthrough.html \
  --title "PR #123 walkthrough" \
  --style nova \
  --mode t3 \
  --features diff,stepper,diagram

python3 ../../scripts/check.py /abs/path/pr-walkthrough.html
```

In T3 Code, verify with `html_preview` and display inline with `html_render` using the returned `contentHeight`. Outside T3 Code, compile with `--mode standalone` and open the HTML file in a browser.

## Diff engine notes

`kit/runtime/diff.js` automatically reads `<script id="vk-diffs-json" type="application/json">`, hides import churn (add `data-show-imports` on a `[data-vk-diff]` container to keep imports visible), collapses whitespace-only rewrites, and highlights moved blocks of 3 or more lines in `.vk-diff-moved`.
