---
name: agent-tool-study
description: Run a study that compares AI agents, agent harnesses or tools for agents, such as browser tools, MCP servers or built-in browsers, on one fixed job, and report its numbers. Use when designing, piloting, running, rerunning or analysing such a comparison, when adding setups to an earlier study or repeating it, when a pilot or counted run fails, when a scoring rule changes after results are in, and before drafting or publishing the study's results, even if the user only says "add X to the browser study" or "compare these agents on this task".
---

# Agent tool study

A study runs each setup on the same job many times and compares its time, cost and passes. Readers take three things on trust: that every setup did the same work, that a failure belongs to the tool and not to the harness, and that each sentence holds for the data behind it. Each step below protects one of them.

Load the `benchmark-checklist` skill before you design the pilot or choose run settings, and answer its questions for this comparison.

## 1. Find the original inputs

In a follow-up study, the earlier study's job prompt, harness, pass checks, results and derivation code are what the new numbers are compared against, so a rebuilt copy is a different study. Find each one before you write anything new:

- the published post's repository, for its data and derivation module;
- every reachable machine, with `ls -d` on each candidate folder;
- past agent sessions, whose Claude Code project folder names encode the folders they ran in (the `agent-logs` skill finds them without a key), and their transcripts, which hold the scripts they wrote;
- the repository's Git history, deleted files included.

This step is done when you have a path for each input. If one is truly gone, tell the user what a rebuilt version would differ in, and get their answer before any run.

## 2. Pilot, and fix the harness first

Run each setup a few times before the counted runs. For each failure, read its tool-call record, the calls and their output in the run's transcript, and take the cause from there. The agent's last message and the pass checks say that a run failed, not why.

When failures cluster on one side at the same step of code the harness supplies, such as the job's own script, a helper or a pass check, treat them as a harness fault until you have run that code in that side's runtime. Fix it and pilot again. After any fix, recheck every other failure, on every side, for the same cause.

This step is done when every pilot failure has a cause taken from its record and none of those causes is the harness.

## 3. Counted runs

Interleave the setups, one run of each in turn, so drift and warm caches favour none. A harness fault found partway through voids every run the faulty code scored: rerun those runs, keep the voided ones out of the comparison, and say so in the method.

Keep signed-in setups, such as browser profiles, desktop apps and test accounts, signed in and ready until the user accepts the findings, because a review often needs a rerun and a fresh sign-in costs the user time or risks a lockout. Write each one and its cleanup step where you keep what you still owe the user, such as `~/.config/agents/open-items.md`.

## 4. Analyse

Import the earlier study's derivation code for every measure, such as its median and pass rule, rather than writing your own, so both studies count the same way. Compare like with like: the same model, effort, job and pass checks on both sides of every comparison.

Check each claim pooled and within every level of each factor the reference data varies on, such as machine, operating system or date, whether or not the text names that factor. Test it at its sentence's own quantifier. Where the post's build allows it, write each claim as a check that fails the build when the data stops supporting it.

A scoring rule set after the results are in, such as counting a step the first study didn't check, changes which runs pass. Before you adopt one, apply it to your runs and to the earlier study's, and name each run it flips. If the earlier data can't be scored by the rule, the comparison is no longer like for like, so tell the user.

## 5. Write and publish

A method change the text has to leave out, because the user asked or because saying it would weaken the comparison, is a blocker. Before you draft around it, tell the user what it changes in the numbers and ask how to handle it.

Before publishing, run the repository's publishing gates, such as the passes a site's verify doc lists, on the final draft.
