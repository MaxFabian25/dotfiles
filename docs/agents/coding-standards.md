# Coding standards

For the reviewer of a release. `release.py finish` stops on changed code until `--reviewed FILE` names a review
made against this file. Each rule here is a judgement call. A rule that a program could check belongs in a test
or in `release.py`.

## How to review

`release.py review NAME` prints, for each of the two release worktrees, the code files the branch changes, the
command that shows its changes, and one `Reviewed:` line. Read those changes and the new files. Apply every rule
to every changed function. Report each finding with its file and line, the input or state that breaks it, and
what then happens. Write "No findings" when there are none.

The reviewer writes the `Reviewed:` lines into the review file, each on a line of its own with nothing else on
it, and only when it has read the code and no finding of its own should hold the release back. The author does
not write them. Say for each finding whether it holds the release back.

The author names the review file without "report" or "findings" in its name, such as `review-1.md`. Claude Code
refuses a helper agent's Write to some file names and tells it to return its findings as text, and the reviewer
then cannot write its lines: in a test on 12 November 2026, version 2.1.322 refused `findings.md` and
`report-values-review-1.md` and wrote `values-review-1.md` and `report-values.txt`.

A line covers the lines the branch adds to and removes from its code files. Adding, removing or changing a line
changes it, so after a fix the author runs `review NAME` again. Another
session's edit elsewhere in the same file does not change it; `finish` stops on that separately. Neither does
a change of mode on a file whose lines the branch also changes, or moving a block the branch adds to a place
that passes none of the branch's other edits, or adding or removing the newline at the end of a last line the
branch changes anyway. A file git reads as binary has no lines to compare and is
covered by a hash of the whole file, so there another session's edit does change it. A link is covered by
its own text, not by what it points to, so a link without a code suffix that points to a script is not
counted: read it when the release changes one.

The author takes fixed code to a fresh reviewer that did not write it, for a closure check. A closure check
reads the earlier review file, the author's answer to each finding and the changes since that review. It answers only whether each fix closed its finding and whether the changes
add a finding that holds the release back. It writes the `Reviewed:` lines when neither holds the release back,
and lists any other finding as one that does not.

The stop counts files by suffix, by name, by folder and by a `#!` first line. Two kinds of change are outside
it, so read them too when the release makes them: hook commands inside JSON and TOML files, and scripts in a
newly stored copy under the private `setup/snapshots/`.

A hold that rests on how a rule here reads goes to the user, and the author writes the user's answer under
that rule with its date. Read those answers as part of the rule, and raise no hold they settle.

## Rules

- **Outside reads fail.** For each new or changed read of something outside the process, such as a vault, the
  network, a subprocess or a file another program writes, the diff shows what the caller does when the read
  fails or times out. The tool's own error text reaches the caller's log, and a test covers the failure. On
  10 November 2026 a key cache stopped sessions when its vault read failed after the copy expired, and dropped
  the vault tool's error text.
- **A fix has a test that fails without it.** A branch added for a reported fault is covered by a test that
  fails once the branch is removed, and so is the place that calls it. That same day a version check that
  skips a key read shipped with no test.
- **A filter that withholds secrets only adds checks.** A change to code that withholds credentials or
  personal data from its output keeps every existing check running on the same text it read before, and only
  adds checks. A test shows it: it runs the old and the new filter side by side on random input and on each
  known secret form, and asserts that the new one withholds everything the old one did. In November 2026 a
  change to `ci_log.py` approved as about two lines removed control characters before the credential check
  read the line, let credentials through in two review rounds, and ended in a redesign.
- **A probe isolates its subject.** A probe outside the test suites that `tests/run.py` and `release.py finish`
  run starts its subject with `HOME` and the working folder at throwaway folders and an environment built from an
  allowlist, with no thread ID, pool key or real lock in it and no credential tool such as `agent-op` on `PATH`.
  A binary that can reach the network runs in a loopback-only namespace from the probe's first run, such as the
  `bwrap ... --unshare-all` line of the `planted-error-checks` skill. A test or probe that gives an outside tool
  a stand-in home also overrides every config location the tool's docs list, system folders and Windows variables
  such as `APPDATA` and `LOCALAPPDATA` included, and checks the real home before and after. In November 2026 a
  probe passed the real pool key to Codex, which wrote it into a snapshot; a reviewer broke a task's "no web
  request" rule about a dozen times through the binary under test; a test that set only `HOME` still read the
  laptop's system mise config; and a probe on Windows wrote 35 launchers into the real home. One exception, the
  user's choice on 2026-11-14: a probe that measures something only the real account shows, such as when
  Codex's `skills/list` first holds the account's remote plugin skills, may start the real binary with the real
  home and network, if it only reads, runs on WSL or Omarchy and never on Windows, and its report says that it
  ran against the real account. The binary may refresh its own caches while it runs.
- **A timing test waits on the event.** A test records the order of events with a fake clock, or waits on the
  event itself, such as a file a helper writes or a process that exits, rather than on a margin of wall-clock
  time. In November 2026 a released test with fixed sleeps and sub-second timeouts stopped other sessions'
  finishes on the busy PC, and its first rewrite failed again.
- **A test is portable.** A new or changed test is written to pass on WSL, on the macOS runner, on Windows
  and in the main checkout. These traps each broke a test in November 2026:
  - macOS's temporary folder is behind a symlink: compare resolved paths, and reproduce it on WSL with
    `HOME` at a folder whose `.cache/dotfiles-tests` is a symlink, because `tests/run.py` replaces `TMPDIR`;
  - Windows can name the temporary folder in 8.3 short form, with a `~1` part: compare resolved paths;
  - behaviour only Linux has: pin `sys.platform` in the test;
  - `\U` in a Windows path inside a double-quoted TOML string starts an escape: use a single-quoted string;
  - a fixture written in text mode gets `\r\n` on Windows: write it with `newline='\n'` or as bytes;
  - a path inside a regular expression: wrap it in `re.escape`;
  - POSIX path literals in expectations: build them with `pathlib`;
  - Windows keeps no executable bit: skip a check of the bit there;
  - a walk of the file system meets folders git ignores, such as `node_modules` in the main checkout: list
    the files with `git ls-files`.
- **A hook that steers agents denies.** A hook that steers an agent refuses the call rather than printing a
  warning, matches narrowly enough that its override flag stays rare, and passes only on evidence the agent
  cannot supply by adding a word, such as a `git diff --stat` summary. In November 2026 a notice printed
  before a step reached the agent only after the step ended, a stop that fired on most runs taught agents to
  pass its override flag, and `approval-size-guard.py` passed any size whose sentence said "estimate", the
  word its own refusal suggests.
