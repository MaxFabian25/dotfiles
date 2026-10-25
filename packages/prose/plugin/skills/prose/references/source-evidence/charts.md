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
  separating adjacent fills instead of requiring 3:1 between every pair. Measuring after opacity,
  and naming bands and reference, event, and leader lines, are house choices. With the previous
  text, writers drew context at 2.4:1 through opacity, range bands at 1.1:1 to 1.2:1, and event
  and leader lines at 1.8:1. The Analysis Function
  colour guidance (`AUTH-UKAF-DATA-VIS-COLOURS`) asks 3:1 between all adjacent colours and
  corroborates the stricter case.
- **Shaded maps.** The W3C Understanding document for 1.4.11 names colour gradients that represent
  a measurement, such as heat maps, as essential colour, and does not require chart parts whose
  values are also presented in a conforming way. The 1.5:1 ceiling for the lowest class and the
  3:1 outlines of at least 1 px are house choices. With the previous text, all three writers of a
  regional map in round 12 set the lowest of five classes at 3.1:1 to 3.3:1 against the page to
  meet the contrast rule. That squeezed the classes into about half the lightness range and gave
  the large, sparsely populated northern regions a mid-tone fill. The same writers already shaded
  rates rather than totals, used an equal-area projection, gave a key and a data table, and stated
  the finding in the text alternative, so those have no rule. Before the rule set a width, two of
  three writers in round 13 drew outlines 0.36 px wide on a phone, which showed faintly.
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
- **Page width.** The Analysis Function dashboard testing guidance (`AUTH-UKAF-DASHBOARD-TESTING`)
  puts headline content first and avoids horizontal scrolling. WCAG 1.4.10 supports reflow and
  exempts content, such as a data table, that needs two-dimensional layout. Measuring the scroll
  width at 390 px and 1366 px is a house choice. In round 5, one dashboard was 756 px wide at a
  390 px viewport although its writer had checked the phone layout by eye.
- **Table totals.** The Analysis Function table guidance (`AUTH-UKAF-DATA-TABLES`) places totals at
  the bottom or right and sets them in bold. The handling of totals affected by suppression is a
  house choice that follows Prose's blank-cell rule, and so are limiting totals to values that add
  up and totalling each dimension that adds up. Writers already computed overall rates from counts
  and said so, so that has no rule.
- **Changes.** Signing every change is a house choice. Opus tables left positive changes unsigned
  in 4 of 8 runs across the first four versions.
- **Title length.** The ONS chart-title guidance (`AUTH-ONS-CHART-TITLES`) asks for the main
  message in ten words or fewer, with the data, geography, and period in a short subtitle. The
  EU data visualisation guide, the FT's Chart Doctor column, and the New York Times graphics
  desk's one-line headlines agree and are cited only. With the previous text, Opus titles had a
  median of 12 words, and 17 of 30 ran past ten, with dashboard headlines of up to 26 words.
- **Findings from the data.** Computing the figures and findings in titles, notes, and text
  alternatives from the data is a house choice. In round 4 one build script hard-coded the figures
  in its titles, and in round 5 one title dated a lead from the month before it began.
- **Rounding.** The rounding rule applies Prose's [numerical precision](../quantitative-prose.md)
  rule to build scripts. With the previous text, writers computed changes from supplied rounded
  rates in 5 of 6 rate tables in round 7, and rounded dashboard chart data to four decimals before
  rounding it again for display.
- **Destination size and build script.** The 8 pt and 12 pt floors, 300 dpi, and keeping the build
  script with its output are house choices. The 12 px floor for web pages is one of the critical
  Chartability tests that the EU data visualisation guide summarises
  (`AUTH-EU-DATAVIS-CHARTABILITY`). With the previous text, 12 of 18 dashboards in rounds 6 to 9
  rendered text under 12 px, such as 11 px axis ticks and 10 px source lines and badges. Measuring
  SVG text with its screen scale is a house choice: in round 13 one writer checked a map label's
  computed size of 13 px while the height-capped map drew it at 11.8 px.
- **Text alternative.** The W3C complex images tutorial (`AUTH-W3C-COMPLEX-IMAGES`) supports a
  short alternative plus the values. Giving sparklines their own alternatives is a house choice.
  The placement table records format behaviour checked on 22 October 2026 with xlsxwriter 3.3.0,
  Vega 6.2.0, and vega-embed 7.0.2.
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
- **Reuse.** The rules are independently worded. The Analysis Function, ONS, W3C, and EU guide
  pages permit adaptation with attribution. The Australian Style Manual, the Plotly website, and
  ISO 24896 are cited only, because their licences do not permit adaptation.
