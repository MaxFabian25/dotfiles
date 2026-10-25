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
an instruction change, run it with the tools in [`eval`](eval/README.md). They keep their tasks in
this repository and write runs to a work directory outside it.

Before the runs, write the round's plan in the work directory: the behaviour expected to change,
the behaviour that must remain, the observable conditions for success, and the tasks, seats and
number of runs. Tell the user the number of runs before starting them. Include a task the change
does not name, and check there that the reply still answers the request and keeps the behaviour
that must remain.

Judge both versions against those conditions on the same tasks, with version labels removed and
output order randomised. When you grade your own change, grade the blinded folders before opening
the key. Settle with a script what a script can settle, such as counts, contrast, page widths and
text sizes, and read for the rest. Record factual, scope, and preservation failures separately;
stylistic quality cannot offset them. These comparison safeguards adapt
[Clarity's evaluation protocol](https://github.com/addyosmani/clarity/blob/e27ceeff60368cf6966b4ea00a5b9b36418ee9a0/evals/JUDGE.md).
Preserve the actual outputs and record an inconclusive result as inconclusive.

Use the real host when checking skill discovery. Run each comparison with the plugin loaded as the
host loads it and a prompt a user would type. A brief that names Prose or tells writers to open its
references changes which references they read, and so inflates a rule's measured effect. Check
which files each run opened with `eval/skill_usage.py`. A rule changes nothing in runs that never
open its file, so put each rule where the runs that need it read, such as `SKILL.md` or the
reference the task routes to.

Tests should exercise retained behaviour through the validator's public interface. Preserve checks
for meaningful contracts such as broken links and exact installed bytes. Avoid assertions about
incidental wording, headings, or internal call order.

Use `make format` for Python formatting. Commit lockfiles when dependencies change. Development
scripts, tests and the evaluation tools stay outside `plugin/`.

## Review a source

Before proposing rules from a style or design source, run writers with the installed Prose on tasks
the source covers, and propose rules only for the gaps the runs show. Cover the scope the source
declares, not only the pages read first. Record each review in [source-reviews.md](source-reviews.md)
with what was adopted, what was left out and why, and the release.

## Deliver a plugin update

Make the change in a `release.py start NAME` worktree, and bump the plugin version when its payload
changes. Compute each count in the release message with a script from the runs. Before pushing,
have a second model, such as `codex exec`, check every claim in the message against the diff and
the round's records. Then, from the dotfiles root, run `release.py finish NAME --message FILE
--entry prose --refresh-owned --package prose --push --pull`, which locks, builds, pairs, pushes,
and pulls, as the dotfiles README's paired-release section describes. The device sync publishes Prose through the
personal marketplace and builds Claude's aligned copy. It preserves current activation choices.

After installation, compare each host's copy with the source. Codex's is
`~/.codex/plugins/cache/personal/prose/VERSION`, and Claude's is the `installPath` that
`claude plugin list --json` gives for `prose@codex-aligned`. Replace `INSTALLED_PLUGIN_ROOT` with
each in turn:

```sh
uv run --locked python -B scripts/validate_release.py --source plugin --installed INSTALLED_PLUGIN_ROOT --json
```

Require exact payload parity. Claude's copy has its own manifest, so the check compares its
`skills/` and the manifest's name and version. Use a fresh task to check changed discovery or
loading behaviour.
