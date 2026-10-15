---
name: cli-creator
description: Build and install a reusable CLI for Codex from an API, SDK, web app, or existing script.
---

# CLI Creator

Create a CLI that future Codex tasks can run by command name from any working directory. For a one-off task, keep the script in the current repo unless the user wants a reusable command.

## Scope and setup

Use the user's stated jobs to choose the commands. Identify the source, install name, and expected inputs and outputs. Prefer `~/code/clis/<tool-name>` for a personal tool when the user has not named a repo. Check `command -v <tool-name>` before creating it to avoid replacing an unrelated command.

Choose a runtime that fits the source and installed toolchains. Respect the user's choice and existing repo conventions. Explain the command design, runtime choice, and installation path briefly before implementation.

Load only the runtime reference you choose:

- [Rust](references/runtime-rust.md) for a standalone compiled binary.
- [TypeScript/Node](references/runtime-typescript.md) when an SDK, auth helper, or existing Node tooling is useful.
- [Python](references/runtime-python.md) for Python tooling or local data and file transformations.

Load source guidance only when it applies:

- [API clients](references/api-clients.md) for API contracts, auth, pagination, and raw requests.
- [Web apps](references/web-apps.md) for commands derived from browser workflows or DevTools requests.
- [Existing scripts](references/existing-scripts.md) for adapting scripts, shell history, fixtures, or log processing.
- [Media and artifacts](references/media-artifacts.md) for uploads, presigned transfers, and processing flows.

## Command design

Implement the commands needed for the user's jobs. A local transformation may need only one command. Add discovery and resolution when the source uses resource IDs, and auth diagnostics when credentials or configuration are required. Add a raw API command only when the requested work needs endpoints beyond the named commands.

Read [CLI patterns](references/agent-cli-patterns.md) when designing multiple command families or structured output. Its examples are options to apply to the requested jobs.

Make `--help` describe the implemented capabilities and required inputs. Use stable machine-readable output where Codex needs to parse results, preserving native file or stream formats when those are the output. Document the output contract and meaningful error behavior in the README or equivalent.

Write commands should perform one named action each and offer a draft, preview, or dry-run mode where the service supports it. Keep credentials out of output, logs, fixtures, and committed files. A live write used for verification must be covered by the user's authorization for that action; otherwise ask before executing it. Prefer reversible or draft actions for verification.

## Build and verify

Read enough of the source to establish the behavior the requested commands rely on, then implement them and document setup, usage, and installation.

Install the CLI on PATH. From another repo or `/tmp`, run `command -v <tool-name>`, `<tool-name> --help`, and a representative installed command using a fixture, local input, dry run, or authorized read. This must exercise actual behavior outside the source folder, including access to packaged files when required.

Run the relevant formatter and build or type checks for the chosen runtime. Test custom behavior that can fail, such as transformation rules, request construction, pagination, or error handling. Choose checks for the implemented behavior and its risks; avoid a fixed inventory of tests for commands or modes the tool does not need.

## Companion skill

After the CLI works, create or update a small companion skill using `$skill-creator` when available. Use `$CODEX_HOME/skills/<tool-name>/SKILL.md` for a personal tool unless the user names another location.

Describe the path through the implemented commands for the common jobs, with usable examples and any auth or authorization requirements. Include discovery, write, or raw-command guidance only when the CLI has those capabilities. Keep detailed API reference material in the CLI docs or a conditional skill reference.
