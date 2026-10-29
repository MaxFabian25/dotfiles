# Dotfiles

Reviewed configuration for Codex, Claude Code, and developer tools on macOS, native Windows, and
Linux, including WSL. Source review happens centrally. Each device reconciles a pinned release locally.

## Contents

| Path | Purpose |
| --- | --- |
| `setup/` | Public source pins, selected skills, host choices, and reproducible build locks |
| `setup/codex-runtime.toml` | The reviewed archive of Codex's workspace runtime for Linux. Pulls on Linux and WSL install it, and `codex_runtime.py review --write` writes it |
| `packages/prose/` | Owned Prose source and its release checks |
| `skills/` | Owned portable skills |
| `config/mise/config.toml` | Reviewed tool versions and the PATH entry that keeps the selected npm ahead of Node's own, without credentials or local environment settings |
| `config/npm/npmrc.toml` | Reviewed top-level keys for each device's `~/.npmrc`. Other lines, such as registry credentials, stay local |
| `config/agent-browser/chrome.toml` | Google Chrome's install paths for each platform. Each device sets only `executablePath` in `~/.agent-browser/config.json` to an installed one, never Chrome for Testing, and a device without Google Chrome blocks its pull |
| `config/apt/packages.txt` | Ubuntu packages for Linux and WSL, installed by hand |
| `*.py` | Build, inspect, reconcile, and delivery tools |
| `tests/` | Synthetic behavior checks and an opt-in native-loader smoke test |

A separate private checkout contains private manifests, licensed source snapshots, user rules,
model routing, maintenance policy, and migration evidence. It is never a subtree of this repository.
Runtime caches, acceptance receipts, and backups stay on each device.

## Inspect a device

Install Git, uv, and the native Codex and Claude Code CLIs. Run from this checkout. The examples assume
a sibling private checkout. Omit `--private` for a public-only setup.

```sh
uv sync --locked
uv run --locked python sync.py --public ./setup --private ../dotfiles-private/setup --check
```

The default is check-only. Exit codes distinguish no drift, managed changes, attention, and incomplete
reads. Unknown installations and local edits are not permission to overwrite their files. Hook approval
is a separate native application decision, not something this tool grants.

An explicitly reviewed local plan can be applied with `--apply`. Backups and ownership receipts
are device-local. A successful repeated apply must leave content and receipt timestamps unchanged.
Inspect a reported blocker before retrying. Do not reset a checkout or delete a receipt to bypass it.

Personal Codex catalog exports carry explicit-only policy in their files. A changed policy gets a new
`+explicit.<digest>` version (or `.explicit.<digest>` after existing build metadata), under
`codex-exports/`; the upstream snapshot stays untouched. Fresh profile installs therefore receive the
same policy as the canonical cache. Harness adaptations remain a separate installation overlay.

Native publisher plugins keep their publisher versions and catalog ownership. A sync also checks
already-enabled, pinned native plugins in Orca account homes on this device. It repairs only invocation
sidecars that still match the verified upstream copy, with backups outside the profile cache. Unknown
versions, disabled plugins, credentials, roles and profile settings are not installed or adopted. Local
edits block the repair. Run a sync after a native profile cache refresh to reconcile these sidecars again;
this does not install a watcher or restart the application.

Claude's validated root `.in_use` leases and `.orphaned_at` timestamp are native cache state, not
plugin content. This distinction applies only to installed Claude plugins. Unknown entries, malformed
markers, and links still block reconciliation. A populated lease directory blocks replacement of that
same cache path. Source builds and staged replacements remain strict raw trees.

Python writes a `__pycache__` folder beside any module it imports. Comparisons of installed, accepted
and staged trees leave out these folders and the regular files in them, as the tree digests do, so a
skill whose script was imported is not a local edit. A link or special file inside one still blocks
reconciliation. Copies from a source leave the folders out, and a replacement moves the installed ones
aside with the rest of the old tree. A changed `.pyc` file therefore goes unnoticed.

## Review source changes

The entry in `setup` describes the intended source, selection, policy, and host placement. Its lock
records verified build outputs. Change only reviewed entries, then preview and verify their locks.

```sh
uv run --locked python lock_setup.py --public ./setup --private ../dotfiles-private/setup \
  --output ../dotfiles-build-review --entry prose
uv run --locked python lock_setup.py --public ./setup --private ../dotfiles-private/setup \
  --output ../dotfiles-build-review --entry prose --write
uv run --locked python build_setup.py --public ./setup --private ../dotfiles-private/setup \
  --output ../dotfiles-build-review --package prose
```

For intentionally edited owned sources, add `--refresh-owned` when previewing and writing the lock.
Never use that flag to accept unexplained installed drift. The daily maintainer skill in the private
checkout owns source review, freezes, holds, discovery choices, and compatibility monitoring.
Device pulls do not select upstream updates or new defaults.

## Deliver a paired release

The private checkout's `release.toml` identifies the exact reviewed public commit.

```toml
public_commit = "<full 40-character public commit>"
```

Publish the public commit before its private release pointer. A device fetches both histories and
selects the public commit named by the private release, even if a newer public commit exists.
Dirty checkouts and non-fast-forward changes stop the pull. The two Git merges are not an atomic
transaction, but installed reconciliation cannot start until the selected pair matches.

Every commit a pull would add, in either repository, must carry an SSH signature from a key in this
device's `~/.config/dotfiles/allowed_signers` (Git's allowed-signers format, restricted to
`namespaces="git"`). The file lives outside both checkouts, so a pushed commit cannot add its own
signer. A missing file, an unsigned commit, an OpenPGP or GitHub web signature, or a key the device
does not pin stops the pull before either checkout moves. Sign release commits with
`gpg.format = ssh`. Changing who may sign is a per-device edit, never a pushed change.

```sh
uv run --locked python pull.py --root . --private ../dotfiles-private --update --apply
```

`release.py` runs a whole delivery from this checkout. `start NAME` makes a worktree of each repository
at `origin/main` under `~/worktrees/NAME`. After editing there, `finish NAME --message FILE --push --pull`
locks and builds any `--entry` and `--package`, commits both worktrees, rebases them onto a newer
`origin/main` if another session pushed meanwhile, pairs `release.toml`, pushes, and pulls. It stops
first in each of these cases:

- A test fails, or the suite writes into the worktree's `.sync/`, the engine cache that real pulls use
  in the canonical checkout. It runs the whole public suite, in about 75 seconds, with the output in the
  release's `build/tests.log`. `CODEX_HOME` and `CLAUDE_CONFIG_DIR` point at a missing folder, as in CI,
  so no test reads this device's config.
- A new file would be left out of the commit by this allowlist `.gitignore`. It names the file.
- Origin changed a file this branch also changed. Read those commits, then pass `--overlap-reviewed`.
- This device's pull would stop on the pair. It runs the pull's check on the worktrees
  (`sync.py --candidate`), plus two checks a pull otherwise reaches only while applying: the settings
  validation, and the comparison of each rule and configuration file with the copy it last accepted.
  So it catches a model alias the engine rejects, an owned skill edited without `--refresh-owned`, or
  a configuration change the apply step would refuse. Plugins, roles and loose skills need a build
  before that comparison, so the check leaves them to the pull. It takes up to half a minute.
- A push would be refused. It checks both pushes with `--dry-run`, so a refused login publishes
  neither half.

After a pull that changed skills or packages, it runs `skill_listing.py`, which warns when Claude Code
lists skills without their descriptions. `clean NAME` removes the worktrees once `origin/main` has them.

```sh
uv run --locked python release.py finish NAME --message msg.txt --entry prose --refresh-owned --push --pull
```

`schedule.py` previews a half-hourly local pull schedule. `--install` enables it only after the pair
and the device check pass. macOS uses a LaunchAgent, Windows a per-user Scheduled Task, and Linux
a systemd user timer. WSL requires an available systemd user session. Keep the previous maintenance
owner active until the corresponding device handoff is verified, then retire its overlapping writes.

Daily source review and local delivery are separate jobs. Creating these files does not install either
schedule or authorise publishing private content.

## Install Linux packages

Pulls do not install system packages, because that needs `sudo`. On a new Linux or WSL installation,
install the reviewed list from this checkout:

```sh
sudo apt-get update
sed 's/#.*//' config/apt/packages.txt | xargs sudo apt-get install -y
```

Running it again is safe. apt skips packages that are already installed.

## Pin Codex's workspace runtime on Linux

On macOS and Windows, the Codex app downloads OpenAI's workspace runtime and registers the
`openai-primary-runtime` plugin catalog inside it. On Linux and WSL, a pull does both from
`setup/codex-runtime.toml`. The pull unpacks exactly the pinned archive at
`~/.cache/codex-runtimes/codex-primary-runtime`, the only path Codex accepts that catalog from, and
registers the catalog. When the pin moves, the same pull replaces the runtime and adds its plugins again,
because Codex keeps an installed plugin at its old version.

`~/.local/state/dotfiles/codex-runtime.json` records each file the pull unpacked, with its size, inode, and
modification and change times, so a later pull re-reads only the files where one of those moved. Any write moves
the change time, even one that puts the modification time back. A runtime folder that this device did
not install, or one edited since, stops the pull until you move it aside. A folder identical to the pin is the
exception, and the pull records it as its own. The `__pycache__` folders that Python writes inside the runtime
are not compared.

```sh
uv run --locked python codex_runtime.py review    # compare the pin with OpenAI's LATEST.json
uv run --locked python codex_runtime.py verify    # re-hash this device's runtime against the pin
```

`review` exits 1 when OpenAI has published a different archive. To pin it, run `review --write` with `--public`
and `--private` set to a release worktree's `setup` folders. It downloads and unpacks the archive, checks that
every plugin carries the runtime's version, and rewrites the pin. It runs only on Linux, such as WSL, because the
archive holds paths that differ only in case. It writes only into a linked worktree, because an uncommitted change
in a main checkout would stop every pull. Without `--write`, `review` runs on any platform.

## Verify changes

```sh
uv run --locked python -m unittest discover -s tests
uv run --locked python tests/native_smoke.py ./setup ../dotfiles-private/setup
uv run --locked python tests/native_policy_smoke.py
```

The smoke test uses temporary application homes and real native plugin loaders. It does not use a
live installation as its test fixture. It retains its isolated evidence on failure.

Only `build_setup.py`, `codex_runtime.py`, `lock_setup.py`, `sync.py`, `pull.py`, `release.py`, `schedule.py`, and `skill_listing.py` are supported public
command entry points. Lower-level packaging modules contain compatibility helpers, not a second
configuration authority. Do not recreate historical JSON registries to drive them.

## Source rights

Owned sources and third-party inputs retain their existing notices and licences. A source pin or
private snapshot does not grant redistribution rights. Licensed course content belongs only in the
private companion and its authorised local installations.
