# Charts source evidence

## Charts skill

The [charts skill](../../../charts/SKILL.md) holds independently worded house rules for marks,
layout, and delivery of data displays. Prose keeps the words in and around a display. Each rule
answers a gap that a baseline in Claude Code and Codex measured on 22 October 2026, with the
general chart skills those hosts already load. Behaviour that writers already showed, such as
zero-based bars, sorted categories, single axes, and highlighted series, has no rule. Thresholds
that no source sets, such as 300 dpi, are house choices.

- **Contrast.** WCAG 2.2 success criteria 1.4.3 and 1.4.11 (`AUTH-W3C-WCAG22`) set the 4.5:1 and
  3:1 thresholds. The W3C Understanding document for 1.4.11 (`AUTH-W3C-NON-TEXT-CONTRAST`) supports
  separating adjacent fills instead of requiring 3:1 between every pair. The Analysis Function
  colour guidance (`AUTH-UKAF-DATA-VIS-COLOURS`) asks 3:1 between all adjacent colours and
  corroborates the stricter case.
- **Direct labels, labelled values, and one scale per measure.** The Analysis Function chart
  guidance (`AUTH-UKAF-DATA-VIS-CHARTS`) prefers direct labels to legends and few, light
  gridlines. The ONS data visualisation manual (`AUTH-ONS-DATA-VIS`) asks for consistent scales
  across comparable charts. WCAG 1.4.1 supports the second cue for a legend. Removing the value
  axis when every value is labelled, and saying so when sparklines of different measures each
  have their own scale, are house choices.
- **Axis start.** The Analysis Function chart guidance allows a line chart's y-axis to start above
  zero when the break is clear on the chart and the text description mentions it. In the
  baseline and the first two rounds, line charts started at 2.0% and showed it on the chart in 1
  of 7 runs.
- **Phone width.** The Analysis Function dashboard testing guidance (`AUTH-UKAF-DASHBOARD-TESTING`)
  puts headline content first and avoids horizontal scrolling. WCAG 1.4.10 supports reflow.
- **Table totals.** The Analysis Function table guidance (`AUTH-UKAF-DATA-TABLES`) places totals at
  the bottom or right and sets them in bold. The handling of totals affected by suppression is a
  house choice that follows Prose's blank-cell rule, and so is limiting totals to values that add
  up. Writers already computed overall rates from counts and said so, so that has no rule.
- **Destination size and build script.** The 8 pt and 12 pt floors, 300 dpi, and keeping the build
  script with its output are house choices.
- **Text alternative placement.** The W3C complex images tutorial (`AUTH-W3C-COMPLEX-IMAGES`)
  supports a short alternative plus the values. The placement table records format behaviour
  checked on 22 October 2026 with xlsxwriter 3.3.0, Vega 6.2.0, and vega-embed 7.0.2.
- **IBCS branch.** The [IBCS charts](../../../charts/references/ibcs-charts.md) module adapts
  visual notation from `AUTH-IBCS-2`, as recorded under [IBCS Standards](ibcs.md#ibcs-standards).

The sources disagree on several points. The skill resolves them as follows:

- **Line-chart axes.** Bars and areas start at zero, which writers already did, so that has no
  rule. A line chart may start above zero, following the Analysis Function guidance, when the
  chart marks the break and the text alternative mentions it.
- **Legends.** Direct labels win over the Australian Style Manual's legend on every chart and over
  general chart skills that always add a legend.
- **Pie slices and series counts.** No writer used a pie, and writers already highlighted one
  series among many, so neither has a rule.
- **Palettes.** No house palette. A project's palette controls, and contrast rules apply to any
  palette.
- **Title placement.** A title may sit in the image or in the page. Prose's title rule applies
  wherever it sits.
- **Totals in charts.** The ONS practice of plotting an aggregate first stays unadopted, because no
  measured gap needs it.
- **Dashboard scrolling.** Vertical scrolling is acceptable when what needs attention comes first,
  following the ONS dashboard guidance.
- **Reuse.** The rules are independently worded. The Analysis Function, ONS, and W3C pages permit
  adaptation with attribution. The Australian Style Manual, the Plotly website, and ISO 24896 are
  cited only, because their licences do not permit adaptation.
