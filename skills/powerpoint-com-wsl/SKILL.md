---
name: powerpoint-com-wsl
description: Render, measure, export, and click through a .pptx in desktop PowerPoint on Windows from WSL, over COM through powershell.exe. Use when a deck's fit, line breaks, fonts, PDF, or slide-show links must be checked as PowerPoint itself draws them, such as a deck in a font outside LibreOffice's safe set, a layout scan that needs real text sizes, or a link test in slide-show mode. Use also before you open a deck for the user, and before you check or close the user's PowerPoint.
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

## Rules for your own scripts

- **One PowerPoint, one job.** `New-Object -ComObject PowerPoint.Application` attaches to a running
  PowerPoint when there is one, so every job shares the user's instance. Count `POWERPNT` processes
  before the job, run one job at a time, and call `Quit()` only when the count was 0. After
  `Quit()`, wait until the count is 0 again before the next job, because PowerPoint takes a few
  seconds to exit.
- **End a job's leftover PowerPoint.** A job that timed out or was killed can leave PowerPoint
  running after `Quit()`, hidden or in a slide show, with the job's temporary deck open. Find it by
  a process ID the job recorded, or by the job's own deck: attach with
  `[Runtime.InteropServices.Marshal]::GetActiveObject('PowerPoint.Application')` and check that
  its only open presentation's `FullName` lies in the job's folder, such as `pptcom-…` for
  `powerpoint.py`. A slide show's `MainWindowTitle` that names the job's deck helps you find it,
  and a start time close to the job's helps too, but neither proves it, because the user may have
  opened PowerPoint since. When the check holds, `Close()` that presentation and `Quit()`. When
  another presentation is open too, close only the job's and leave PowerPoint running. If the
  process stays with no presentation open, `Stop-Process -Id` that process alone, after checking
  that it is still `POWERPNT` and has no window (`MainWindowHandle` 0). When `powershell.exe`
  returns nothing for a process count, count once more before you act on it.
- **The user's PowerPoint.** Every job runs in the user's one PowerPoint, so while a job runs, give
  a user who asks to see the deck its PDF. Once the job is done, copy the deck to a folder of its
  own in the Windows user's temp folder and open the copy with `Start-Process`, which keeps the
  build's file free for the next build. Before you close the user's PowerPoint, list each open
  deck's `Name` and `Saved` through `GetActiveObject` and ask the user, because a deck whose
  `Saved` is 0 rather than -1 holds unsaved work.
- **Open without a window.** `Presentations.Open(path, -1, 0, 0)` opens read-only with no window.
  Without a window, `TextRange.BoundWidth`, `BoundHeight`, `Lines()` and `Lines(i).Text` are right,
  while `BoundLeft` and `BoundTop` are not, so place text from the shape's own `Left` and `Top`.
- **Words broken across lines.** Compare the end of `Lines(i).Text` with the start of
  `Lines(i + 1).Text`. A letter or digit on both sides is a word PowerPoint split, such as a name
  broken as "Alexande" and "r".
- **PDF export.** `SaveAs(path, 32)` writes PowerPoint's PDF. It writes each link's alt text as
  invisible text, alpha 0, at the link's corner. Count words from the .pptx, and drop spans whose
  alpha is 0 from any layout scan of the PDF, such as PyMuPDF spans with `span["alpha"] == 0`.
  `pdffonts` on the PDF names the fonts PowerPoint used.
- **Slide-show link test.** Run `SlideShowSettings.Run()`, move with `View.GotoSlide(n)`, follow a
  link with `Hyperlink.Follow()` or a shape's `ActionSettings(1).Hyperlink.Follow()`, and read
  `View.Slide.SlideIndex`. A layout's links sit on `CustomLayout.Shapes(name)`. A slide link's
  `SubAddress` reads `slideId,slideIndex,title`. The show takes over the screen, so run it only
  when no PowerPoint is open. Ending the show also closes a windowless deck, so wrap the later
  `Close()` in `try`.
- **Fonts.** A font installed for one user, in `%LOCALAPPDATA%\Microsoft\Windows\Fonts` with its
  entry under `HKCU\Software\Microsoft\Windows NT\CurrentVersion\Fonts` and a `WM_FONTCHANGE`
  broadcast, is enough for PowerPoint, though .NET's `InstalledFontCollection` may not list it.
  Confirm a font from PowerPoint's PDF with `pdffonts`.
- **Files and output.** Copy the deck and script into a Windows folder and pass Windows paths. Run
  `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File <script>`. Write JSON with
  `[IO.File]::WriteAllText(path, text, (New-Object Text.UTF8Encoding $false))` and `ConvertTo-Json
  -Depth` deep enough for the data, and read it in Python with `encoding="utf-8-sig"`, because
  Windows PowerShell's own UTF-8 writers add a byte-order mark. Delete the job folder once the
  results are copied out.

The job is done when the results are saved outside the job folder, the job folder is gone, and the
`POWERPNT` count is back to what it was before the job.
