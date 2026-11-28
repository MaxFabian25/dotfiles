---
name: show-me
description: Explain the current topic visually with concise diagrams, code-shape sketches, or compiled interactive HTML visuals.
disable-model-invocation: false
---

# Show me

Help the user understand the current topic visually. Pick the smallest view that makes the point clear, place each visual beside the brief prose it supports, and keep only the calls, files, states, and boundaries that answer the question.

## Choose the medium

- **Text blocks** for logic, call order, component trees, file layout, or focused changes.
- **Compiled interactive HTML** for architecture maps, state machines, step-by-step lifecycles, explorable parameter playgrounds, before/after comparisons, charts, or UI mockups. In T3 Code, Mermaid blocks display as raw code and `open` shows nothing, so use compiled HTML shown via `html_preview` and `html_render`.
- **Mermaid** only in Markdown destinations that render it natively (such as GitHub PRs or docs).

## Concise text views

- **Algorithm or control flow** as pseudocode:

```text
on(save)
  if content is unchanged
    return cached result
  write new content
  return fresh result
```

- **Runtime call flow** as an indented call tree:

```text
submitForm
  createSession
    persistPrompt
    launchAgent
  navigateToSession
```

- **UI structure** as a component tree with module boundaries and hooks:

```tsx
<SessionPage> (apps/web/src/routes/session.tsx)
  useSessionEvents()
  <SessionToolbar>
    <RunSkillButton> (packages/ui)
```

- **File responsibility** as a shallow file tree:

```text
src/
├── commands/   # parses user actions
├── sessions/   # owns session state
└── transport/  # sends API requests
```

- **Focused changes** as a `diff` block matching the shape that changed (component tree, file layout, call tree, or pseudocode). Show the complete target block when most of it is new or when diff notation hides order or ownership.

## Interactive HTML visuals

When the topic needs an interactive diagram, stepper, playground, chart, or visual comparison, read [references/html-kit.md](references/html-kit.md) before writing HTML.

1. Write a concise inner HTML fragment using Visual Kit classes (never hand-write a full stylesheet or `<!doctype html>` wrapper).
2. Compile one self-contained file with `python3 ../../scripts/build.py --input body.html --output out.html --title "..." --style nova --features ...` (resolve paths relative to this skill's folder; use `--mode standalone` outside T3 Code).
3. Verify the output with `python3 ../../scripts/check.py out.html`.
4. In T3 Code, inspect the render with `html_preview` (using the returned `contentHeight`) and present it with `html_render`. If two `html_preview` calls time out, capture a screenshot with headless Chromium (`DISPLAY=` unset on WSL) and inspect that PNG before calling `html_render`. Outside T3 Code, open `out.html` in the browser.

Done when the visual answers the question with real labels and data, passes `check.py`, and renders cleanly at both desktop (728 px) and mobile (390 px) widths.
