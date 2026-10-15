# Rust CLI setup

Use this reference after choosing Rust. Check that `cargo` and `rustc` are available before scaffolding. If required tooling is missing, use another suitable installed runtime or obtain authorization to install it.

Prefer established crates for the capabilities needed:

- `clap` for arguments and help.
- `reqwest` for HTTP.
- `serde` and `serde_json` for structured payloads.
- `toml` for config files.
- `anyhow` for CLI error context.

Provide a documented install command, such as a `make install-local` target, that builds release and installs the binary into `~/.local/bin`. The installed command must work without `cargo run` or the source directory as its working directory.
