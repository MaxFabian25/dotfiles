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

Native publisher plugins keep their publisher versions and catalog ownership.

An account home is a separate Codex home that another app keeps, such as each account of the Orca desktop app,
with its own plugin cache. The private `devices/<device list>.toml`, `wsl.toml` on WSL, `linux.toml` on other Linux,
`macos.toml` or `windows.toml`, lists them as globs under the home folder,
`account_homes = [".local/share/orca/codex-accounts/*/home"]`, and a device whose file lists none has none. When Codex starts
in one, its `plugin/list` call installs a newer catalog version, which lacks the install overlay and harness
adaptation of the accepted `~/.codex` copy. So for each plugin a home has installed and enabled, and for each
kept-current plugin a home has installed, whatever that home's switch says, a sync
copies the accepted `~/.codex` copy into that home in place of an older version, the native copy, its own
earlier copy, or the `~/.codex` copy a release replaced at the same version, which a home that already
matched holds without a copy receipt (`profile_plugins.py`). It decides which homes need a copy after updating `~/.codex`, so a
release that changes the accepted copy reaches every home in the same sync. The copies it replaces move to
the sync backup. A copy with any other file blocks the sync as a local edit. A home whose marketplace
setting differs from `~/.codex` gets a note instead. A sync installs no plugin a home lacks, enables nothing, and leaves settings, credentials
and roles alone. A cache that is linked to `~/.codex`, as on Windows, is already up to date. A plugin
whose hooks are `trusted` blocks the sync until its hooks are approved in each home as well; a sync
never approves them.

Claude's validated root `.in_use` leases and `.orphaned_at` timestamp are native cache state, not
plugin content. This distinction applies only to installed Claude plugins. Unknown entries, malformed
markers, and links still block reconciliation. A lease from a running Claude process keeps that cache
path as it is: the pull leaves its replacement pending, applies the rest of the release, exits 1, and
replaces the cache at the first pull after those processes exit. A replacement that changes only
Markdown files under `skills/`, as tool notes and invocation choices do, goes ahead at once, since a
session reads those files when it uses the skill; the swap moves the sessions' leases into the new
folder. Source builds and staged replacements remain strict raw trees.

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

`build_setup.py` builds only packages with a managed install. Add `--unmanaged` to also build each package that no
install manages and that Codex installs through the engine, with its engine and codex-aligned installs switched
on. A package that only a host marketplace installs stays out. The option prints each built version and digest,
so two runs, one before and one after a change to a builder, show whether the change altered what those
packages build.

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

On Linux, a pull that sync did not block also copies each file under the private checkout's
`config/agent-env/home/` to the same path under the home folder, with the source's executable bit
(`home_files.py`). It replaces a copy only when the file is absent or still holds what a pull last delivered
there, which `~/.local/state/dotfiles/home-files.json` records, and it saves each replaced copy under
`~/.local/state/dotfiles/home-file-backups/`. A copy that was edited on the device stays as it is: the pull
prints `Home file left as it is` with its path, applies the rest, records the pair and exits 1. A first pull
has no record, so it takes over only the copies that already match. A pull removes no file that a release
dropped from that folder. Without `--apply` it prints `Home file to deliver` for each file and exits 1.

`release.py` runs a whole delivery from this checkout. `start NAME` makes a worktree of each repository
at `origin/main` under `~/worktrees/NAME`. After editing there, `finish NAME --message FILE --push --pull`
locks and builds any `--entry` and `--package`, runs the tests, commits both worktrees, rebases them onto a
newer `origin/main` if another session pushed meanwhile, pairs `release.toml`, pushes, and pulls, once any
other pull on the machine has ended.

Several finishes run their checks and tests at the same time on one machine. A finish takes the machine's
release lock only after its tests, for the steps from the commit to the pull, once no other release, pull or
sync runs there. On 12 November 2026, when a finish still held the lock through its tests, two releases held
it for 305 and 360 seconds, of which the tests took 88 and 78 per cent. A finish notes the worktrees whose
tests passed in `build/tests-passed` under the release's folder, and a later finish of the same files does
not run the tests again; delete that note to run them. A worktree without work of its own counts as
`origin/main` there, wherever that stands, as no rebase is tested again either. A finish starts again from
the engine as it is now when a pull or sync runs as it starts, once that has ended, and when a pull changed
the engine before it takes the lock. It then runs its checks, locks and builds again, but not the tests, and
`--timeout` counts anew. When it gives the lock up it prints how long it held it and how long it waited
for it, and before it starts again how long it had waited by then. One finish runs per release, and a second one stops and names the first.

It stops first in each of these cases:

- The release changes code that `--reviewed FILE` does not show another agent reviewed. `release.py review NAME`
  prints the files, the command that shows their changes and the lines the reviewer writes, against
  `docs/agents/coding-standards.md`. It checks again just before the push, because the commit takes whatever
  the worktree holds by then.
- Another release, pull or sync still runs on this machine `--timeout` seconds after the tests, 1800 unless
  given. It names that run's process and command. Finish again, which does not run the tests a second time.
  A pull or sync that runs as the finish starts is waited for as long, before any check.
- A file changed while the tests ran, or between the tests and the commit. Finish again, which tests the
  files as they are then.
- A test fails, or the suite writes into the worktree's `.sync/`, the engine cache that real pulls use
  in the canonical checkout. It runs the whole public suite, in about 75 seconds, with the output in the
  release's `build/tests.log`, and each private test file by itself, as `python -I -B FILE`, which is how
  an agent runs one, with their output in `build/private-tests.log`. A private test file therefore needs
  its own `sys.path` line for a module beside it, and `unittest.main()` at its end.
  Each run moves the logs of the run before aside as `tests.DATE-TIME.log` and `private-tests.DATE-TIME.log`,
  named for when they were written, and `finish --detach` does the same with `finish.log`, so a failed
  attempt can still be read after a retry. `CODEX_HOME` and `CLAUDE_CONFIG_DIR` point at a missing folder, as
  in CI, so no test reads this device's config.
- A new file would be left out of the commit by this allowlist `.gitignore`. It names the file.
- Origin changed a file this branch also changed. Read those commits, then pass `--overlap-reviewed`.
  It asks this before the tests, through a trial merge of the worktree as it stands, and again after the
  commit. A conflict stops it the same way: commit the worktree and rebase it by hand. `setup/lock.toml`,
  and a `setup/skills` or `setup/packages` file in which the branch changed only the pinned `tree`, count
  for neither stop when `--entry` or `--entry NAME --refresh-owned` writes them anew. On a worktree that is
  committed, as after such a hand rebase, and holds nothing else uncommitted, `finish` amends what that
  writes into the branch's newest commit and needs no message for it, unless that commit is a merge or a pair.
- This device's pull would stop on the pair. It runs the pull's check on the worktrees
  (`sync.py --candidate`), plus two checks a pull otherwise reaches only while applying: the settings
  validation, and the comparison of each rule and configuration file with the copy it last accepted.
  So it catches a model alias the engine rejects, an owned skill edited without `--refresh-owned`, or
  a configuration change the apply step would refuse. Plugins, roles and loose skills need a build
  before that comparison, so the check leaves them to the pull. It takes up to half a minute.
- A push would be refused. It checks both pushes with `--dry-run`, so a refused login publishes
  neither half.

When the private push fails after the public commit went out, `finish` says so and keeps a note of that commit
in `build/public-pushed` under the release's folder. Run the same `finish` again: it pairs the private half
with the public commit then on origin, which is the one it pushed or another session's commit on top of it.
The note is the only record that the public half went out. A rerun without it, after `build/` was deleted,
leaves the private origin naming the public commit before: it pushes a private commit of its own unpaired, or
stops with "nothing to release" when the release changed only the public side. Writing the pushed commit's
full id into `build/public-pushed` and finishing again pairs it. So `clean` without `--force` stops on that
note, as it does on a branch that is not on origin, and removes nothing. A `start` under the name of a release
whose branches are gone deletes a note it finds, because that note is another release's.

After a pull that changed skills or packages, it runs `skill_listing.py`, which warns when Claude Code
lists skills without their descriptions. `clean NAME` removes the worktrees once `origin/main` has them.
`status` names the release that holds the machine's lock and any pull or sync that runs, and lists every open
release worktree with its commits, uncommitted files and the files it changes, with a line under each release
whose finish runs, which covers one that is still in its checks and tests; `start` lists the other open releases. Under each release both name the T3 Code
thread it was started in, which `start` notes in `t3-thread` beside the worktrees when `T3CODE_THREAD_ID` is set.
For a skill description change, `skill_triggers.py` counts how often the installed skills load in Claude Code for a
set of prompts, and `codex_triggers.py` does the same in Codex. Measure the draft first, then release, then run the
same prompts again after the pull. On Linux, `skill_draft.py DRAFT_SKILL.md -- COMMAND` runs either tool with the
draft in place of the installed copy and changes no file; with `--new` it adds a skill that is not installed
yet.

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
uv run --locked python -B tests/run.py
uv run --locked python tests/native_smoke.py ./setup ../dotfiles-private/setup
uv run --locked python tests/native_policy_smoke.py
```

The smoke test uses temporary application homes and real native plugin loaders. It does not use a
live installation as its test fixture. It retains its isolated evidence on failure.

Only `build_setup.py`, `codex_runtime.py`, `codex_triggers.py`, `lock_setup.py`, `sync.py`, `pull.py`, `release.py`, `schedule.py`, `skill_draft.py`, `skill_listing.py`, and `skill_triggers.py` are supported public
command entry points. Lower-level packaging modules contain compatibility helpers, not a second
configuration authority. Do not recreate historical JSON registries to drive them.

## Source rights

Owned sources and third-party inputs retain their existing notices and licences. A source pin or
private snapshot does not grant redistribution rights. Licensed course content belongs only in the
private companion and its authorised local installations.
