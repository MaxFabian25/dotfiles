# Coding standards

For the reviewer of a release. `release.py finish` stops on changed code until `--reviewed FILE` names a review
made against this file. Each rule here is a judgement call. A rule that a program could check belongs in a test
or in `release.py`.

## How to review

`release.py review NAME` prints, for each of the two release worktrees, the code files the branch changes, the
command that shows its changes, and one `Reviewed:` line. Read those changes and the new files. Apply every rule
to every changed function. Report each finding with its file and line, the input or state that breaks it, and
what then happens. Write "No findings" when there are none.

The reviewer writes the `Reviewed:` lines into the findings file, each on a line of its own with nothing else on
it, and only when it has read the code and no finding of its own should hold the release back. The author does
not write them. Say for each finding whether it holds the release back.

A line covers the lines the branch adds to and removes from its code files. Adding, removing or changing a line
changes it, so after a fix run `review NAME` again and take the new code back to its reviewer. Another
session's edit elsewhere in the same file does not change it; `finish` stops on that separately. Neither does
a change of file mode beside a line change, or moving a block the branch adds to a place that passes none of
the branch's other edits. A file the branch changes without a line diff, such as a mode-only change or a file
git reads as binary, is covered by a hash of the whole file, so there another session's edit does change it.

The stop counts files by suffix, by name, by folder and by a `#!` first line. Two kinds of change are outside
it, so read them too when the release makes them: hook commands inside JSON and TOML files, and scripts in a
newly stored copy under the private `setup/snapshots/`.

## Rules

- **Outside reads fail.** For each new or changed read of something outside the process, such as a vault, the
  network, a subprocess or a file another program writes, the diff shows what the caller does when the read
  fails or times out. The tool's own error text reaches the caller's log, and a test covers the failure. On
  10 November 2026 a key cache stopped sessions when its vault read failed after the copy expired, and dropped
  the vault tool's error text.
- **A fix has a test that fails without it.** A branch added for a reported fault is covered by a test that
  fails once the branch is removed, and so is the place that calls it. That same day a version check that
  skips a key read shipped with no test.
