# Domain docs

The dotfiles pair is one context: this repository's engine and the private repository's setup choices share one vocabulary.

## Before exploring, read these

- **`GLOSSARY.md`** at this repository's root.
- **`docs/adr/`**: read ADRs that touch the area you're about to work in.
- The paired private checkout's `GLOSSARY.md` and `docs/adr/`, for terms and decisions that name private material.
- The lines of the paired private checkout's `skills/maintain-dotfiles/references/preservation.md` and its dated records under `maintenance/` that name the entry or file you are about to change, because setup decisions live there.

If any of these files don't exist, **proceed silently**. The `domain-modeling` skill creates them when a term or decision is actually resolved.

## Keep this repository public-safe

Write glossary entries and ADRs here only when they are safe to publish. Terms and decisions that name hosts, accounts, credentials, private projects or course content go in the private repository's `GLOSSARY.md` or `docs/adr/`.

## Vocabulary and ADR conflicts

Use glossary terms consistently in specs, issues, code, tests and review findings. When work conflicts with an existing ADR, say so and name the decision that would need to be revisited.
