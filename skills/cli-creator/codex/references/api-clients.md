# API clients

Use this reference for commands backed by an API or SDK.

## Source contract

Inspect the docs, SDK, or request examples for the requested jobs. When OpenAPI is available, use it to verify the relevant operations. Establish their auth, IDs, request and response shapes, pagination, rate limits, and write effects before implementing them.

## Auth and config

When credentials or configuration are required, add `doctor` or equivalent diagnostics. Report the active source category, missing setup, and relevant reachability or version checks without exposing secrets. Diagnostics must handle missing auth. If the tool also has an offline or fixture mode, identify that mode and whether its required data is available.

Support applicable auth sources in this precedence order:

1. The service's standard environment variable, such as `GITHUB_TOKEN`.
2. User config in a documented path, such as `~/.<tool-name>/config.toml`, when persistent configuration is useful.
3. A token flag only for explicit one-off tests. Prefer environment or config for normal use because flags can leak into shell history or process listings.

Use the provider's auth mechanism when required, such as OAuth or an SDK credential chain, and document how it relates to these sources. Add an `init` command only when it makes required setup easier.

Never print full tokens. Redact credentials from diagnostics, errors, fixtures, and request examples.

## Collections and raw requests

For paginated collections, follow the bounded output guidance in [CLI patterns](agent-cli-patterns.md). Test custom request construction and pagination against representative responses.

Add a raw request command only when the requested work needs endpoints beyond the named commands. Support reads first. Treat raw writes as live writes: execute a live non-GET/HEAD request only when the user authorized that specific write. A method called `debug` does not change its effect.
