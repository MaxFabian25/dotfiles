# Codex CLI patterns

Use this reference when designing multiple command families or structured output. Apply the patterns that serve the requested jobs; the examples are not a required command inventory.

## Help and command names

Write `--help` for a future Codex task that has the binary and a goal. Give each command a short description and flags with literal names from the product or source.

Use consistent product nouns and verbs where the tool has resource families:

```bash
tool-name --json projects list
tool-name --json channels resolve --name codex
tool-name --json messages search "exact phrase"
tool-name --json messages context <message-id> --before 3 --after 3
tool-name --json logs download <build-url> --failed --out ./logs
tool-name --json drafts create --body-file draft.json
```

A direct verb or a single command can fit a smaller tool:

```bash
tool-name transform input.csv --out output.json
tool-name logs tail
```

Separate operations when their results are useful inputs to other jobs. Keep a single operation intact when extra phases would only complicate its use.

## Resource IDs

When the source uses resource IDs, expose the discovery or lookup needed to obtain them. Accept an existing stable ID directly so Codex can read an object without repeating a broad search. Add surrounding context, such as nearby messages or logs, when a requested job needs it.

## Text, JSON, files, and exit codes

Use human text when useful and structured output where Codex needs to parse or pipe results. A local transformer can emit its native output format directly.

For `--json`:

- Emit JSON to stdout and progress or diagnostics to stderr.
- Document API pass-through or an envelope, success and error shapes, and examples for the implemented command families.
- Make errors under `--json` machine-readable too.
- Redact tokens, cookies, customer secrets, private headers, and unrelated payloads.

Add filtering or templating only when the user needs it. Stable output can be piped into an existing tool such as `jq`.

For downloads and exports, support a user-provided output path. Structured metadata can include the resulting path, byte count when cheap, and source URL or ID.

Exit zero for success, including an empty result. Exit nonzero for invalid input or failed or incomplete work, with a useful error in the documented format. When diagnostics exist, make them report missing auth or configuration rather than crash.

## Pagination

For paginated collections, bound the default result size and document it. Add the provider's actual cursor, offset, or page controls as needed:

```bash
tool-name --json messages search "topic" --limit 10
tool-name --json drafts list --limit 20 --offset 40
```

Return continuation metadata from the provider. Make any option to fetch additional pages explicit and bounded.

## Raw requests

When a raw API command is needed, reuse configured auth, base URL, JSON parsing, redaction, and error handling. Keep named commands for repeated jobs. See [API clients](api-clients.md) for raw-request authorization and diagnostics.

## Companion examples

Show an implemented common job from inputs to result. Start with diagnostics or discovery only when setup or a resource ID is needed. Include write or raw-request examples only when those commands exist, and state the authorization needed for their actual effects. Include output details only when Codex needs them to choose the next command.
