# Develop Prose

Run development commands from `packages/prose` in the dotfiles checkout. Install Python 3.10 or later,
[uv](https://docs.astral.sh/uv/getting-started/installation/), Node.js 22 or later, npm, and Make.

Install the development dependencies:

```sh
make setup
```

## Edit the guidance

Apply Prose's relevant [language and clarity rules](plugin/skills/prose/references/index.md#language-voice-and-readers)
and [documentation rules](plugin/skills/prose/references/index.md#documents-and-editing) to authored
instructions and explanations. Preserve governed source forms and technical literals. Apply
specialist conventions only within their stated scope.

Check a named style's rules against an authorised copy of its manual. Use the style's website for
supplemental and later guidance, not as a substitute for the manual.

Keep each writing rule in one reference. Link to that rule where another module needs it. When
changing a rule's source support, update its evidence and affected source or terminology records.
Update controlled inbound links when a heading or path changes.

Remove superseded instructions and duplicate representations. Git history retains old designs and
completed experiments. Keep the current tree focused on material needed to use or maintain Prose.

## Check the change

Run the repository checks:

```sh
make check
```

This checks lint, formatting, Markdown, release tests, reference integrity, and the payload. It
does not establish writing quality. When a model comparison can resolve a material question about
an instruction change, use concrete writing tasks in fresh contexts with the same model settings.
Before comparing, state the behaviour expected to change, the behaviour that must remain, and the
observable conditions for success. Judge both versions against those conditions on the same tasks,
with version labels removed and output order randomised. Record factual, scope, and preservation
failures separately; stylistic quality cannot offset them. These comparison safeguards adapt
[Clarity's evaluation protocol](https://github.com/addyosmani/clarity/blob/e27ceeff60368cf6966b4ea00a5b9b36418ee9a0/evals/JUDGE.md).
Preserve the actual outputs and record an inconclusive result as inconclusive. Use the real host
when checking skill discovery. Run each comparison with the plugin loaded as the host loads it and
a prompt a user would type. A brief that names Prose or tells writers to open its references
changes which references they read, and so inflates a rule's measured effect.

Tests should exercise retained behaviour through the validator's public interface. Preserve checks
for meaningful contracts such as broken links and exact installed bytes. Avoid assertions about
incidental wording, headings, or internal call order.

Use `make format` for Python formatting. Commit lockfiles when dependencies change. Development
scripts and tests stay outside `plugin/`.

## Deliver a plugin update

Make the change in a `release.py start NAME` worktree, and bump the plugin version when its payload
changes. Then, from the dotfiles root, run `release.py finish NAME --message FILE --entry prose
--refresh-owned --package prose --push --pull`, which locks, builds, pairs, pushes, and pulls, as the
dotfiles README's paired-release section describes. The device sync publishes Prose through the
personal marketplace and builds Claude's aligned copy. It preserves current activation choices.

After installation, replace `INSTALLED_PLUGIN_ROOT` with the installed directory and compare it
with the source:

```sh
uv run --locked python -B scripts/validate_release.py --source plugin --installed INSTALLED_PLUGIN_ROOT --json
```

Require exact payload parity. Use a fresh task to check changed discovery or loading behaviour.
