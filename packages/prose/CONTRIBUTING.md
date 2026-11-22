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

### Hold instructions to the checklist

Hold each Prose instruction file, which is each `SKILL.md` and its description,
`references/index.md`, and each reference file, to these 14 questions. A file, or a rule added after
a session's correction, is ready when every answer is yes.

1. **Description.** Does the description lead with the skill's main word and give each distinct
   kind of request one trigger?
2. **Pointers.** Does each pointer name, once, the case that should open it, link the file, and link
   the section when the rule sits in one section?
3. **One home.** Does each rule live in one file that others link to, and where two files rule on
   one point, does the narrower file name the rule it overrides?
4. **Positive wording.** Does each instruction name the behaviour to produce, at normal volume, with
   any prohibition guarding a known failure and placed beside its positive target?
5. **Reasons.** Is each reason a present-tense fact about the work, written for the rule's class of
   cases, with no date, incident, one session's count, pull request number, or model name?
6. **Completion.** Does the file end on a completion criterion that names each output and each
   check, so an agent can tick it off item by item?
7. **No sentence the agent obeys anyway.** Would an agent on either host act differently without
   each sentence?
8. **Load per use.** Does each `SKILL.md` read in one sitting, and does each route send the agent
   only to the files its kind of request needs?
9. **Reach.** Can a run reach every reference file through links from a `SKILL.md`, and does every
   link and anchor resolve?
10. **Steps.** Does each step the agent performs sit where it runs, in order, apart from the
    reference rules?
11. **Exact where fragile.** Does each fragile step name the exact path, command, or file, and does
    each named path, command, and flag exist?
12. **One name per concept.** Does each concept keep one name throughout the file and the
    references it routes to?
13. **Scripts for scriptable checks.** Is each check that a script can make shipped as a script that
    the rule names?
14. **Both hosts.** Does the text name only tools and mechanisms that both Claude Code and Codex
    have?

Where the questions pull against each other, settle the case this way:

- Cut a trigger word from a description only when another word in the same description already
  names that kind of request.
- Keep a short rule word for word in two skills when each loads on its own and needs it.
- Move text from a `SKILL.md` behind a pointer only when the `SKILL.md` stops reading in one sitting.
- Keep a rule that came from one correction when it is written for its class of cases.
- Delete as a no-op only a sentence that tells the agent to do what reading the file already makes
  it do.

Removing a rule needs the user's yes, and so does a change to what a rule says, as against how it
says it.

## Check the change

Run the repository checks:

```sh
make check
```

This checks lint, formatting, Markdown, release tests, reference integrity, and the payload. It
does not establish writing quality; the user's corrections do, as
[Count repeated corrections](#count-repeated-corrections) describes.

Run a model comparison with the tools in [`eval`](eval/README.md) only for one contested rule,
when neither a prompt audit, such as Claude Code's `/doctor prompt-audit`, nor the user's
corrections can say whether the rule helps. Compare one task, written with and without that rule.
The tools keep their tasks in this repository and write runs to a work directory outside it.

Before the runs, write the comparison's plan in the work directory: the behaviour expected to
change, the behaviour that must remain, the observable conditions for success, and the task, seats
and number of runs. Tell the user the number of runs before starting them.

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
scripts, tests, and the evaluation tools stay outside `plugin/`.

## Review a source

Before proposing rules from a style or design source, compare it with Prose's rules on each point
the source covers, and propose rules only for the gaps. Cover the scope the source
declares, not only the pages read first. Record each review in [source-reviews.md](source-reviews.md)
with what was adopted, what was left out and why, and the release.

## Count repeated corrections

Once a month, count the lessons that repeat a point Prose already covered. A lesson is a rule added
or changed after a session's correction. The first count covers December 2026, and each count
works from commits alone:

1. On `origin/main`, list the month's commits, as
   `git log --since=2027-01-01 --until=2027-02-01 -- packages/prose/plugin` does for January 2027.
   Take the previous calendar month unless the prompt names one.
2. For each commit that adds a lesson, search Prose at the commit's parent, `COMMIT^`, for a rule
   that already covered the lesson's point.
3. When the month has a repeat, file one issue in `MaxFabian25/dotfiles-private` titled
   `Prose repeat count: YYYY-MM`, and list each repeat in it with the commit, the lesson, and the
   earlier rule's `file:line` at `COMMIT^`.
4. Report the month, the number of lesson commits read, and either the issue's URL or, when the
   month has no repeat, `No repeats in YYYY-MM`. A month with no repeat gets no issue.

A repeat means the rule failed, and that rule is rewritten against the
[checklist](#hold-instructions-to-the-checklist). A month with no repeats is the target.

## Deliver a plugin update

An update lands as a pull request to the dotfiles repository, through the `release-dotfiles` skill,
which covers the worktree, the private-string check, the pull request and its merge. Make every
edit in `packages/prose/plugin`, the source. The copies under `~/.claude/marketplaces/codex-aligned/`
and `~/.codex/plugins/cache/` are generated, and the next pull overwrites edits made there, reflect
edits included.

1. In a worktree of `origin/main`, make the change and run `make check` in `packages/prose`.
2. When the change touches `plugin/`, raise the version in `plugin/.codex-plugin/plugin.json`. Keep
   its form, `MAJOR.MINOR.PATCH+dotfiles.YYYYMMDD`, with the date of the change.
3. From the worktree's root, refresh the setup lock, which rewrites `setup/packages/prose.toml` and
   the `[prose]` block of `setup/lock.toml`:

   ```sh
   uv run --locked python -B lock_setup.py --public setup --private PRIVATE/setup \
     --output OUT --entry prose --refresh-owned --write
   uv run --locked python -B sync.py --candidate --public setup --private PRIVATE/setup
   ```

   Replace `PRIVATE` with the private dotfiles checkout, such as `~/dotfiles-private`, and `OUT` with
   an empty scratch folder outside the repository. Fix every issue `sync.py` reports as blocking.
4. Compute each count in the pull request's text, such as a comparison's results, with a script
   from the records it counts. Have a second model,
   such as `codex exec`, check every claim in that text against the diff and those records.
5. Open and merge the pull request as `release-dotfiles` says. A device pull then publishes Prose
   through the personal marketplace and builds Claude's aligned copy. It preserves current activation
   choices.

After a device has pulled, compare each host's copy with the source. Codex's is
`~/.codex/plugins/cache/personal/prose/VERSION`, and Claude's is the `readFromFolder` that
`claude plugin list --json` gives for `prose@codex-aligned`. That entry's `installPath` can name an
older cache that Claude no longer reads. Replace `INSTALLED_PLUGIN_ROOT` with each in turn:

```sh
uv run --locked python -B scripts/validate_release.py --source plugin --installed INSTALLED_PLUGIN_ROOT --json
```

Require exact payload parity. Claude's copy has its own manifest, so the check compares its
`skills/` and the manifest's name and version. When `.dotfiles-policy.json` records dotfiles
explicit-only skill overrides, the check validates that policy record, the version suffix, and the
invocation fields on those named skills while requiring exact byte parity everywhere else. Use a
fresh task to check changed discovery or loading behaviour.
