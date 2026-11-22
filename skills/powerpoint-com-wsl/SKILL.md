---
name: powerpoint-com-wsl
description: Render, measure, export, and inspect `.pptx` decks in Windows desktop PowerPoint from WSL over COM (`powershell.exe`). Use when checking true PowerPoint text fit, line breaks, non-LibreOffice fonts, PDF export, or slide-show links, after deck repairs, and before opening, checking, or closing the user's PowerPoint.
---

# PowerPoint over COM from WSL

LibreOffice substitutes fonts and moves line breaks, so a deck in any font it lacks fits differently
there than in PowerPoint. Desktop PowerPoint on the Windows host is the true render. Drive it over
COM from WSL with Windows PowerShell.

## Measure and export

Run the bundled script for a deck's measurements and PowerPoint's own PDF:

```sh
python3 -I scripts/powerpoint.py deck.pptx --json layout.json --pdf deck.pdf
```

It copies the deck and `scripts/measure.ps1` into a job folder in the Windows user's temp folder,
runs PowerPoint there without a window, and deletes the folder. For each slide it reports every
shape's position and size; each text's width, height, line count, inner margins, word wrap, and
autofit; the words PowerPoint broke across lines, in text boxes and table cells; table row heights
and column widths; links, layout, and section. It stops with exit 3 when PowerPoint is already
running, because the user may be working in it, and `--allow-running` measures in that instance
without closing it.

Before a job, take the `windows-desktop` claim that the `work-across-tailscale-machines` skill asks
for, with a note that says what the job leaves free, so that another thread does not wait on it to
use the screen: `--note 'powerpoint-com-wsl: a windowless PowerPoint over COM; the foreground and
the clipboard stay free unless a slide show runs, which takes the screen'`.

## Rules for your own scripts

Before writing or debugging your own PowerShell COM script for PowerPoint, opening a deck for the user, checking a repair, testing slide-show links, checking installed fonts, or closing a leftover or user PowerPoint instance, read [references/own-scripts.md](references/own-scripts.md).

The job is done when the results are saved outside the job folder, the job folder is gone, and the
`POWERPNT` count is back to what it was before the job.
