---
name: prose-maintainer
description: Maintain the Prose plugin source and verify a release.
---

# Prose maintainer

Work in `packages/prose` in the [dotfiles repository](https://github.com/MaxFabian25/dotfiles),
not in an installed copy. The Prose row of `~/dotfiles-private/config/agent-rules/repos.md` names
that checkout. Read `packages/prose/CONTRIBUTING.md` for editing, verification, and release
instructions. Apply the requested change there. It is complete when each check that the guide
names for the change has passed, such as `make check`, the checklist for each instruction file you
changed, and the comparison of each host's copy after a release.
