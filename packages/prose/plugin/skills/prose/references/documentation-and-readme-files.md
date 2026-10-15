# Documentation and README files

Apply this branch when documentation or a README file is written or reviewed. It governs keeping
docs fresh, accurate, and easy to find, not Markdown syntax.

## Keep docs fresh

- Keep a small set of fresh, accurate, and useful docs.
- Change affected documentation in the same change as the code.
- Check relevant docstrings, header files, README files, API documentation, examples, and user
  guides.
- Delete demonstrably wrong, stale, redundant, or unnecessary docs within authorised scope.
- Preserve records with legal, historical, audit, or decision value.
- After implementation, keep design docs as archives of the decisions. Do not use them as current
  documentation.
- Keep documentation where the same review can update it with the code.
- Prefer documentation that maintainers can read and update with the same tools as code.
- Prefer a small, complete change. Do not reduce accuracy, verification, or completion standards.

## Link authoritative documentation

- Update the authoritative guide when it is missing or out of date and the task authorises the
  work.
- Link externally maintained documentation from the project README file.
- Follow host and repository rules for README names, locations, and directory coverage.
- Check that each required document is linked from the applicable README file or project directory.
- Keep a local instruction self-contained when an external dependency would prevent reliable use.

## What to put in a README

- Summarise the contents of the directory for people browsing the code, especially first-time
  users.
- State what the package or library contains and what it is used for.
- Give points of contact for the package or library.
- State whether the package or library is deprecated or not for general release.
- Tell developers which files to look at first and whether any files define an API.
- Explain how to use the package or library. Put the simplest use case first.
- Include or link relevant sample code, copyable commands, interfaces, and detailed documentation.
- Include setup, testing, debugging, and release guidance only when readers need it.

Complete this branch when the docs are fresh, accurate, useful, and easy to find. Preserve required
records and check that readers can find each affected document from its README file or project
directory.
