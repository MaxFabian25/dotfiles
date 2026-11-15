"""Retired on 15 November 2026. Changes to the dotfiles repositories land as GitHub pull requests."""
import sys

RETIRED = '''release.py is retired: changes to dotfiles and dotfiles-private now land as GitHub pull requests.
Work you started with `release.py start` is still in its worktrees. In each one that has changes:
  1. Commit the change on a branch.
  2. In the public one, run `python -B leaks.py --private <the private worktree>` and fix what it names.
  3. Push the branch and open a pull request with `gh pr create`.
  4. When its `tests` check has passed, merge it with `gh pr merge --squash`, the public one first.
  5. Run `pull-machines`, and pull on this machine.
The release-dotfiles skill, at skills/release-dotfiles/SKILL.md in dotfiles-private, has the steps in full.'''

if __name__ == '__main__':
    print(RETIRED, file=sys.stderr)
    raise SystemExit(2)
