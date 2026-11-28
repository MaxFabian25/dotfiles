---
name: visual-pr
description: Generate an interactive HTML pull-request walkthrough or a visual GitHub Markdown PR description.
disable-model-invocation: true
---

# Visual PR

Explain a pull request clearly so a reviewer sees why the change exists, what matters most, and how the implementation hangs together.

## Choose the output mode

- **Interactive HTML walkthrough (default):** read [references/html-canvas.md](references/html-canvas.md). Produces a self-contained Geist-styled HTML walkthrough with pseudocode summaries, moved-code diffs, flow diagrams, and a reviewer checklist, shown inline in T3 Code via `html_render` or opened in a browser.
- **GitHub Markdown PR description (`--github`, or when asked to write or update the PR body on GitHub):** read [references/github-pr.md](references/github-pr.md). Writes a tight Markdown PR body using native GitHub Mermaid and structural `diff` outlines, then updates the PR via `pull_request_*` tools or `gh pr edit`.

## Gather PR context first

Fetch metadata, changed files, and review comments before drafting either mode:

- Prefer T3 Code `pull_request_*` tools when available; otherwise use `gh pr view` and `gh api repos/{owner}/{repo}/pulls/{number}/files --paginate`.
- For an unpushed local branch with no PR yet, inspect `git status --short --branch` and `git diff origin/main...HEAD`.
- Read the complete diff and enough surrounding code to group files by reviewer narrative (core behaviour changes first, mechanical or boilerplate edits last).
