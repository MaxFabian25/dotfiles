---
name: agent-tool-study
description: Comparative studies of AI agents, chat apps, or agent tools (browsers, MCP servers) on one fixed job. Use to design, pilot, run, rerun, extend, or analyse a study, when a run fails or scoring changes, before building a test rig for an unfinished guide, before drafting or publishing results, or when a study's design or numbers are questioned.
---

# Agent tool study

A study runs each setup on the same job many times and compares its time, cost and passes. Readers take three things on trust: that every setup did the same work, that a failure belongs to the tool and not to the harness, and that each sentence holds for the data behind it. Step 2 checks that the study can show a difference at all, and each later step protects one of them.

Load the `benchmark-checklist` skill before you design the pilot or choose run settings, and answer its questions for this comparison.

## 1. Find the original inputs

In a follow-up study, the earlier study's job prompt, harness, pass checks, results and derivation code are what the new numbers are compared against, so a rebuilt copy is a different study. A new study skips this step. Find each one before you write anything new:

- the published post's repository, for its data and derivation module;
- every reachable machine, with `ls -d` on each candidate folder;
- past agent sessions, whose Claude Code project folder names encode the folders they ran in (the `agent-logs` skill finds them without a key), and their transcripts, which hold the scripts they wrote;
- the repository's Git history, deleted files included.

This step is done when you have a path for each input. If one is truly gone, tell the user what a rebuilt version would differ in, and get their answer before any run.

## 2. Show that the sides can differ

Before the first run of any side, name the plan or account that each side and your own driving session draw on, read its usage page, and estimate the runs against what is left. Move your reading and research, such as subagents that read transcripts or sources, to another provider first.

When one side runs what a guide or other document recommends, write that document first, because it defines that side.

Run the baseline side first, on a draft of the job, under step 3's rules for run folders and for reading what each model received, because a baseline that loaded the toolkit's rules or skills ties for that reason alone. When the baseline passes every check of the draft, the job cannot show what the other setups add, so redesign the job before you build the rest of it. In a follow-up study the draft is the earlier study's job and harness, so ask the user before you redesign it, because step 1 makes a changed job a different study.

Then run each side once, unscored, on the job as it now stands, reusing the baseline's run if the job did not change. Run them under the same step 3 rules and with step 3's route check, so that neither a leaked rules file nor a fallback route decides whether the sides look alike. Put the results in the form the study will publish, such as a screenshot of its results page. When the sides look the same there, ask the user what the comparison is meant to show before you build any scorer.

This step is done when each side's run shows its own route and loaded nothing the plan doesn't name, and the published form shows the sides differing or the user has said what the comparison is for.

## 3. Pilot, and fix the harness first

Keep every run folder outside the real home folder. A scratch `HOME` covers user-level settings only. In one published study a setup with a scratch `HOME` still loaded the user's `~/.claude/CLAUDE.md` as project instructions, because its run folder sat under the real home.

Run each setup a few times before the counted runs. For each failure, read its tool-call record, the calls and their output in the run's transcript, and take the cause from there. The agent's last message and the pass checks say that a run failed, not why.

When failures cluster on one side at the same step of code the harness supplies, such as the job's own script, a helper or a pass check, treat them as a harness fault until you have run that code in that side's runtime. Fix it and pilot again. After any fix, recheck every other failure, on every side, for the same cause.

After the first pilot run of each setup, read what its model actually received and compare the setups: first-request tokens, instruction files, skills, connectors, tools, app entrypoint, version and permission mode. Launch settings show only what you asked for. A difference the plan doesn't name is a harness fault. A search for a phrase from the user's rules counts only on a record type shown to hold instruction text, so first run it on a record known to contain that phrase.

When comparing a change to the user's own everyday setup rather than a bare vendor baseline, keep the setup's active hooks and startup instructions enabled on both sides and confirm their registration in the first pilot receipt. Start with prompts drawn from the user's real tasks before synthetic benchmarks, so a difference shows up on work they actually run.

A pass counts only when its record shows the route its setup is named for, such as the extension's own tool calls in a browser-extension setup, because a setup can fall back to another route, such as a cloud browser, and still pass. Before the brief goes in, give the setup's tool one trivial task that only it can do, such as reading the title of a tab open in the user's own browser, and save that output with the run.

This step is done when every pilot failure has a cause taken from its record, none of those causes is the harness, every pass's record shows its setup's route, and no setup loaded anything the plan doesn't name, the user's own rules files included. Run the comparison again before publishing any result several times off the baseline.

## 4. Counted runs

Before the first counted run, make the study's folder a local Git repository, commit what it holds, and commit again after each counted run. Give it no remote and never push it, because the folder holds private command text and personal data. Before the first commit, write a `.gitignore` that lists credentials, sign-in state and extracts of the user's own sessions or messages, which Git would keep after you delete them, along with caches such as `__pycache__/` and each file that `find . -size +50M` lists. After each counted run, and after any other script that writes into the folder, such as a site build, read `git status`: a stored file, such as a counts or results file, that changed though no step meant to change it was overwritten, so bring it back with `git restore FILE`. The last commit is the rollback copy of every file it tracks; a file the repository ignores still needs its own copy before a step that may write it ([incidents](references/incidents.md)).

Make the runner copy the brief, the inputs and its own scripts into the run folder at launch, and have every stage read that copy, so an edit made while a run is in flight changes no run. Wrap a bash runner's body in `{ ...; exit; }`, because bash reads a script while it runs it, and an edit to the file shifts what it runs next.

Interleave the setups, one run of each in turn, so drift and warm caches favour none. A harness fault found partway through voids every run the faulty code scored: rerun those runs, keep the voided ones out of the comparison, and say so in the method.

Keep signed-in setups, such as browser profiles, desktop apps and test accounts, signed in and ready until the user accepts the findings, because a review often needs a rerun and a fresh sign-in costs the user time or risks a lockout. Record each one and its cleanup step in the task's obligation tracker.

A re-run of one setup can't be interleaved with runs that already finished, so it is a before-and-after on a later date. Rebuild its inputs from that setup's archived run and compare them byte for byte, because the current harness can hold fixes made later for other setups. Hold the app, model and harness versions to the original run records, and tell the user before counting if one has moved. Replacing a published number is a method change under step 6, so state the date gap. Credit no drop to a cause the re-run can't isolate, such as one line of a file it removed whole.

This step is done when every planned counted run finished on an interleaved schedule (or any non-interleaved re-run matches its archived inputs byte for byte), each run is committed locally, and no tracked result file was overwritten unexpectedly.

## 5. Analyse

Import the earlier study's derivation code for every measure, such as its median and pass rule, rather than writing your own, so both studies count the same way. Compare like with like: the same model, effort, job and pass checks on both sides of every comparison.

Sort every other difference between setups. One that comes with the product, such as an app's own context or cache policy, is part of that setup's result, so name it and scope the claim to that setup. One the harness introduced is a fault, and only that kind justifies re-runs.

Judge extra instructions a setup loaded by what the agents did with them, because a token count gives only their cost. Read each line against the job's steps, search the runs' reasoning and result sizes for its traces, such as values stacking just under a round number, and split the runs by the route they took before you give a verdict.

An agent's account of how its tool behaves is a lead. Before you publish a claimed limit, measure it across all recorded results, split by return type, and look for results above it. When a published limit fails and its replacement would still be inferred, recommend cutting it if the finding stands without it.

Check each claim pooled and within every level of each factor the reference data varies on, such as machine, operating system or date, whether or not the text names that factor. Test it at its sentence's own quantifier. Where the post's build allows it, write each claim as a check that fails the build when the data stops supporting it.

A scoring rule set after the results are in, such as counting a step the first study didn't check, changes which runs pass. Before you adopt one, apply it to your runs and to the earlier study's, and name each run it flips. If the earlier data can't be scored by the rule, the comparison is no longer like for like, so tell the user.

This step is done when every claim has been checked pooled and across each varying factor using the earlier study's derivation code, and any harness fault or scoring-rule flip is accounted for.

## 6. Write and publish

A method change the text has to leave out, because the user asked or because saying it would weaken the comparison, is a blocker. Before you draft around it, tell the user what it changes in the numbers and ask how to handle it.

Give parallel setups the same term, and keep the term the user designed the study with. Ask before you switch to a product's own label.

Before the user sees a post, thread or reply about the study as ready, a reviewer who hasn't seen it answers four questions for each post or section, because the user has sent a finished thread back on each of them ([incidents](references/incidents.md)):

1. Does it say why the result came out as it did?
2. Does it repeat what its chart or card already shows?
3. Does it use a number, unit or detail that a reader who hasn't read the write-up can't picture?
4. Can it be read without the earlier study or thread?

Give the reviewer the user's earlier cuts as fixed rules, from the task's decisions file.

Before publishing, run the repository's publishing gates, such as the passes a site's verify doc lists, on the final draft.

When a flaw is found after publication, quote each published sentence it touches before you say what it breaks, and set the severity from those sentences.

This step is done when a fresh reviewer has answered all four questions on the final draft, every repository publishing gate passes, and any post-publication flaw cites the exact published sentences it affects.
