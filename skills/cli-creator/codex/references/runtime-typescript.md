# TypeScript and Node CLI setup

Use this reference after choosing TypeScript or Node. Inspect the installed runtime, package manager, and existing repo conventions before scaffolding. If required tooling is missing, use another suitable installed runtime or obtain authorization to install it.

Use the libraries the task needs:

- `commander` or `cac` for arguments and help.
- Native `fetch`, the official SDK, or an existing HTTP helper for API calls.
- `zod` where external payload validation prevents real breakage.
- A `package.json` `bin` entry for the installed command.
- `tsup`, `tsx`, or `tsc` following the repo's convention.

Document installation using the chosen package manager, such as build followed by `pnpm link --global`, or provide a wrapper in `~/.local/bin`. Verify that the installed command finds its runtime and packaged files outside the source directory.
