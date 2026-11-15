# Check public pushes

The native Git `pre-push` hook runs `leaks.py --pre-push` with the private denylist before Git sends a push.
It checks every non-deletion ref in Git's push input, including branches other than `HEAD` and annotated tag
messages. A private string removed by a later commit still blocks the push. A ref held by another remote does
not exempt that history. The scanner reads the destination's advertised refs and excludes the published
history whose objects are available locally. If it cannot read those refs, it stops the push. This adds one
read of the destination to each push. A destination with no refs requires a scan of the whole reachable history.

## Install in a canonical checkout

Keep the public checkout at `dotfiles` and the private one at its sibling `dotfiles-private`.
Run `uv sync --locked` in the public checkout to create its Python environment.

First inspect the existing hook setting with `git config --show-origin --get-all core.hooksPath`.
Exit 1 with no output means that Git uses its default hooks directory. Inspect that directory with
`git rev-parse --git-path hooks`. Preserve active hooks before changing the setting. A new `core.hooksPath`
replaces the directory Git reads, so an existing hook needs to be retained in the selected directory.

When there are no other active hooks, run this in the canonical public checkout, using a POSIX shell or
Git Bash on native Windows:

```sh
git config --local core.hooksPath "$(pwd -P)/.githooks"
```

The absolute path keeps linked worktrees on the canonical hook. The hook finds that checkout through
Git's common directory, then uses its `.venv/bin/python` or native Windows `.venv/Scripts/python.exe`.
It reads the scanner from the canonical checkout and checks the refs being pushed from the current worktree.
It stops the push when the runtime or required private input is missing or unreadable. Deletion-only pushes
need no denylist because they publish no new objects. Uncommitted files are outside the push scan.

## Keep the other publication checks

The hook cannot read a pull request's future title or description. Scan that text separately before opening
or editing the pull request:

```sh
python -B leaks.py --private ../dotfiles-private --message PR_TEXT_FILE
```

Keep GitHub push protection enabled. It catches supported credential patterns, while this hook uses the
private denylist. Like other local Git hooks, the pre-push check can be bypassed with `--no-verify`.

## Verify without publishing a private string

Run `python -B tests/run.py -p test_pre_push.py`. The tests create local bare remotes and synthetic
denylists. They verify rejected pushes and unchanged remote refs, including a push from a linked worktree
whose nearby private checkout is a decoy. No test deliberately sends a private string to GitHub.
