# Dotfiles

Reviewed configuration for Codex, Claude Code, and developer tools on macOS, native Windows, and
Linux, including WSL. Source review happens centrally. Each device reconciles a pinned release locally.

## Contents

| Path | Purpose |
| --- | --- |
| `setup/` | Public source pins, selected skills, host choices, and reproducible build locks |
| `packages/prose/` | Owned Prose source and its release checks |
| `skills/` | Owned portable skills |
| `config/mise/config.toml` | Reviewed tool versions, without credentials or local environment settings |
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

Claude's validated root `.in_use` leases and `.orphaned_at` timestamp are native cache state, not
plugin content. This distinction applies only to installed Claude plugins. Unknown entries, malformed
markers, and links still block reconciliation. A populated lease directory blocks replacement of that
same cache path. Source builds and staged replacements remain strict raw trees.

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
before rebasing when origin changed a file this branch also changed, until you have read those
commits and pass `--overlap-reviewed`, and it checks both pushes with `--dry-run` first, so a refused
login publishes neither half. Before pushing, it runs this device's pull check on the worktree pair
(`sync.py --candidate`), including the settings validation a pull otherwise runs only while applying,
and stops where the pull would stop, such as on a model alias the engine rejects or an owned skill
edited without `--refresh-owned`. That check takes about half a minute. After a pull that changed skills or packages, it runs `skill_listing.py`,
which warns when Claude Code lists skills without their descriptions. `clean NAME` removes the
worktrees once `origin/main` has them.

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

## Verify changes

```sh
uv run --locked python -m unittest discover -s tests
uv run --locked python tests/native_smoke.py ./setup ../dotfiles-private/setup
```

The smoke test uses temporary application homes and real native plugin loaders. It does not use a
live installation as its test fixture. It retains its isolated evidence on failure.

Only `build_setup.py`, `lock_setup.py`, `sync.py`, `pull.py`, `release.py`, `schedule.py`, and `skill_listing.py` are supported public
command entry points. Lower-level packaging modules contain compatibility helpers, not a second
configuration authority. Do not recreate historical JSON registries to drive them.

## Source rights

Owned sources and third-party inputs retain their existing notices and licences. A source pin or
private snapshot does not grant redistribution rights. Licensed course content belongs only in the
private companion and its authorised local installations.
