# GitHub Markdown PR description

Read this file when `visual-pr` is asked to write or update a pull request's GitHub description (`--github`).

## Rules before publishing

- Check the repository's rules and your global agent rules for any PR body prefix required on the target repository.
- GitHub renders fenced `mermaid` blocks natively inside PR descriptions, so use Mermaid sequence or flow diagrams where they clarify interactions.
- Write in plain, concise sentences as one engineer talking to another. Omit empty sections instead of padding them.

## Template

```markdown
## Why the change

{Exactly one sentence stating the problem this PR solves and what becomes possible after it lands.}

## Special things to note

- {1 to 3 reviewer warnings: migrations, compatibility constraints, deliberate omissions, or risky invariants. Write "None." when there are none.}

## Change outline

{Pick the smallest set of structural views that explain the implementation. Prefer `diff` blocks for changes to an existing shape, and full blocks when the shape is new. Order views to tell the story clearly.}

{Short sentence introducing the diagram, schema, type, file tree, call tree, or pseudocode block.}

```diff
 src/
 ├── http/
+│   └── breaker.ts       # tracks 5xx streak and open cooldown
 └── client.ts
```
```

## Save and update

1. Write the rendered Markdown body to the current task folder (or a temporary file outside the repository so no stray untracked file enters git).
2. Update the PR via T3 Code's `pull_request_*` edit tool when available; otherwise run `gh pr edit {number} --body-file {path}`.
3. Confirm the update succeeded and report the PR URL and a concise list of changed files.
