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

A sync never moves or writes a plugin folder that Claude Code or Codex loads from `~/.claude` or `~/.codex`.
When a plugin's installed bytes change, as the release or its installation overlay does, the sync writes them
to a new folder beside the old one in the plugin's cache folder and points the app there
(`accepted_installations.py`). On Codex the folder is named
`<version>+notes.<UTC time>.<first 12 hex digits of its content's digest>` (`.notes.` after existing build
metadata), with a time later than every earlier such folder, so it sorts above every folder of that version and
Codex loads it. On Claude Code the name has `-` for `+`, and the sync changes only the `installPath` of the
plugin's user-scope entry in `~/.claude/plugins/installed_plugins.json`, keeping the upstream `version` and every
other entry. An isolated probe on 14 November 2026 showed `claude plugin update` putting an entry whose version is
not the upstream one back on the upstream folder. A session that started on the old folder reads it to the
end, except on Codex when the upstream version changes: the native `codex plugin add` that the sync runs first
removes the plugin's other version folders, as Codex 0.169.0-alpha.6 did in an isolated probe on 14 November
2026. A rerun that finds the app loading a folder with the reviewed bytes writes no folder. A loaded folder whose
name carries the digest of the bytes it holds counts as the sync's own unedited output, so a sync that stopped
before saving its receipt leaves a folder the next sync builds on, whatever release that sync brings. An edit
changes the digest, and the sync then stops rather than build on it. The sync stages the new folder outside the plugin's folder and
moves it into place with one rename, so an app sees all of it or none. When Windows refuses that rename, the
app keeps the folder it loads, which is the unadapted upstream release when the same sync installed a new one, the sync applies the rest of the release and exits 1, and the next pull tries again. Work folders that an earlier engine left beside a
plugin it replaced in place stay where they are, and the sync names them once.

Native publisher plugins keep their publisher versions and catalog ownership.

An account home is a separate Codex home that another app keeps, such as each account of the Orca desktop app,
with its own plugin cache. The private `devices/<device list>.toml`, `wsl.toml` on WSL, `linux.toml` on other Linux,
`macos.toml` or `windows.toml`, lists them as globs under the home folder,
`account_homes = [".local/share/orca/codex-accounts/*/home"]`, and a device whose file lists none has none. When Codex starts
in one, its `plugin/list` call installs a newer catalog version, which lacks the install overlay and harness
adaptation of the accepted `~/.codex` copy. So for each plugin a home has installed and enabled, and for each
kept-current plugin a home has installed, whatever that home's switch says, a sync
copies the accepted `~/.codex` copy into that home in place of an older version, the native copy, its own
earlier copy, or an earlier `~/.codex` copy, which a home that already matched holds without a copy receipt:
another version folder of the plugin in `~/.codex`, or the backup an earlier engine kept when it replaced the
`~/.codex` copy in place (`profile_plugins.py`). It decides which homes need a copy after updating `~/.codex`, so a
release that changes the accepted copy reaches every home in the same sync. The copies it replaces move to
the sync backup. A copy with any other file blocks the sync as a local edit. A home whose marketplace
setting differs from `~/.codex` gets a note instead. A sync installs no plugin a home lacks, enables nothing, and leaves settings, credentials
and roles alone. A cache that is linked to `~/.codex`, as on Windows, is already up to date. A plugin
whose hooks are `trusted` blocks the sync until its hooks are approved in each home as well; a sync
never approves them.

Claude's root `.in_use` leases and `.orphaned_at` timestamp are native cache state, not plugin content,
and a sync leaves both unread, since Claude Code writes them while it runs. This distinction applies only to
installed Claude plugins. Other unknown entries and links still block reconciliation. A running Claude
session holds back no change, since the sync leaves the folder it runs from as it is. Source builds and staged replacements remain strict raw trees.

Python writes a `__pycache__` folder beside any module it imports. Comparisons of installed, accepted
and staged trees leave out these folders and the regular files in them, as the tree digests do, so a
skill whose script was imported is not a local edit. A link or special file inside one still blocks
reconciliation. Copies from a source leave the folders out, and a replacement leaves the installed ones
with the rest of the old tree. A changed `.pyc` file therefore goes unnoticed.

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

`build_setup.py` builds only packages with a managed install. Add `--unmanaged` to also build each package that no
install manages and that Codex installs through the engine, with its engine and codex-aligned installs switched
on. A package that only a host marketplace installs stays out. The option prints each built version and digest,
so two runs, one before and one after a change to a builder, show whether the change altered what those
packages build.

## Deliver a paired release

A change lands as a GitHub pull request, in this repository, in the private one, or in both. Work in a
worktree of `origin/main`, never in the checkout a device pulls into, because a pull needs that one clean.
Before you push a branch of this repository, run `python -B leaks.py --private ../dotfiles-private`: a
pushed branch is public at once, and the list of strings to keep out is private. Open the pull request with
`gh pr create`, and merge it with `gh pr merge --squash` once its `tests` check has passed, the public half
first when a change has both. The check runs the suite on Linux, macOS and Windows here, and on a
self-hosted runner in the private repository.

A change to a setup entry also writes `setup/lock.toml` anew before the commit, with
`python -B lock_setup.py --public setup --private ../dotfiles-private/setup --output OUT --entry NAME --write`,
adding `--refresh-owned` for an owned skill or package whose files changed. `python -B sync.py --candidate
--public setup --private ../dotfiles-private/setup` then says whether this device's pull would stop on the
change, which nothing checks after the merge until a device pulls.

A device fetches the private history, then the public one, and fast-forwards each checkout's `main` to
`origin/main`. A change's public half is merged first, so a pull never installs a private commit without
the public commit it needs, though it can install a public commit before the private commit that goes with
it. Dirty checkouts, a checkout on another branch and non-fast-forward changes stop the pull. The private `release.toml` named the public commit to install until 15 November 2026, and
nothing reads it now.

A pull trusts what GitHub holds on `main`, a merged pull request included, and checks no signature.
Whoever can write to either repository on GitHub can therefore change what every device installs. The
user chose this on 15 November 2026, over an SSH signature pinned on each device, so that changes can
land through GitHub's merge button.

```sh
uv run --locked python pull.py --root . --private ../dotfiles-private --update --apply
```

On Linux, a pull that sync did not block also copies each file under the private checkout's
`config/agent-env/home/` to the same path under the home folder, with the source's executable bit
(`home_files.py`). It replaces a copy only when the file is absent or still holds what a pull last delivered
there, which `~/.local/state/dotfiles/home-files.json` records, and it saves each replaced copy under
`~/.local/state/dotfiles/home-file-backups/`. A copy that was edited on the device stays as it is: the pull
prints `Home file left as it is` with its path, applies the rest, records the pair and exits 1. A first pull
has no record, so it takes over only the copies that already match. A pull removes no file that a release
dropped from that folder. Without `--apply` it prints `Home file to deliver` for each file and exits 1.

A private test file runs by itself, as `python -I -B FILE`, so it needs its own `sys.path` line for a module
beside it, and `unittest.main()` at its end.

After a pull that changed skills or packages, run `skill_listing.py`, which checks that Claude Code lists
skills with their descriptions. For a skill description change, `skill_triggers.py` counts how often the installed skills load in Claude Code for a
set of prompts, and `codex_triggers.py` does the same in Codex. Measure the draft first, then release, then run the
same prompts again after the pull. On Linux, `skill_draft.py DRAFT_SKILL.md -- COMMAND` runs either tool with the
draft in place of the installed copy and changes no file; with `--new` it adds a skill that is not installed
yet.

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
uv run --locked python -B tests/run.py
uv run --locked python tests/native_smoke.py ./setup ../dotfiles-private/setup
uv run --locked python tests/native_policy_smoke.py
```

The smoke test uses temporary application homes and real native plugin loaders. It does not use a
live installation as its test fixture. It retains its isolated evidence on failure.

Only `build_setup.py`, `codex_runtime.py`, `codex_triggers.py`, `lock_setup.py`, `sync.py`, `pull.py`, `schedule.py`, `skill_draft.py`, `skill_listing.py`, and `skill_triggers.py` are supported public
command entry points. Lower-level packaging modules contain compatibility helpers, not a second
configuration authority. Do not recreate historical JSON registries to drive them.

## Source rights

Owned sources and third-party inputs retain their existing notices and licences. A source pin or
private snapshot does not grant redistribution rights. Licensed course content belongs only in the
private companion and its authorised local installations.
