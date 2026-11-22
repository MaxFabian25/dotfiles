# Planting faults in code test suites

Read this when planting faults into a code repository's test suite, sampling random mutations, running sandboxed fault checks, or invoking `scripts/plant.py`.

The same method shows which tests in a code repository earn their keep. The source code is where the
faults go, and each test is a check.

- Choose faults at random from points in the code, such as a flipped comparison, a negated condition
  or a dropped return value, so the sample holds more than the faults you thought of. Record which
  tests fail for each one.
- Judge a class of tests by the faults only it caught, as well as by its time.
- Run each code fault in a sandbox with a read-only root and no network, such as
  `bwrap --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp --bind <copy> <copy> --unshare-all --die-with-parent`,
  with `HOME` at a folder of its own, because a fault can flip a guard in code that manages real config.
- Run the clean copy in that sandbox first and leave the tests that fail there out of every score,
  because the sandbox made them fail. Add one copy with a crash planted, which proves the harness
  sees a failure.
- `maintenance/ci-census/mutate.py` in the private dotfiles repository does this for the public
  dotfiles suite.
- Build the list of faults you choose yourself from the diff, with one plant for each of these:
  each branch the change adds; each failure of each call that leaves the process, such as a
  subprocess, the network or a file another program writes; each point where the code can be
  killed, refused or throw, with a test that asserts a non-zero exit and leftovers that the next run
  cannot read as success; and, for each new guard, a swapped argument of the same type, a changed
  constant, a member dropped from a list the code reads, a statement moved past its neighbour, and a
  secret added to an output.
- For the faults you choose yourself, run `scripts/plant.py` from this skill's folder. Start the run
  with `agent-job start NAME -- python3 -I scripts/plant.py SOURCE TEST PLANTS_JSON ...` and wait
  with `agent-job wait NAME --timeout SECONDS` in background mode, because a restart of the agent
  app cancels a plant run, and a Claude Code hook refuses one outside `agent-job` or
  `omarchy-run start`. With no arguments or with `--help`, `python3 -I scripts/plant.py` prints its
  usage, the plant file's format and its outcomes. It runs each plant without a sandbox of its own.
