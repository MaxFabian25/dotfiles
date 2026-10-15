# Existing scripts and local processing

Use this reference when adapting an existing script, shell history, fixture prototype, or log processor.

Preserve the flags, paths, environment variables, and working behavior the user relies on. Wrap the repeatable jobs with stable arguments and outputs. Split a workflow into separate commands only when phases such as download, transform, or upload are useful independently.

For fixtures or packaged data, choose a predictable location that the installed command can resolve. Exercise those files in the installed-command check outside the source folder.

For log tools, keep deterministic extraction separate from model interpretation. Useful output identifies filenames, line numbers or byte ranges, matched rules, and short excerpts.
