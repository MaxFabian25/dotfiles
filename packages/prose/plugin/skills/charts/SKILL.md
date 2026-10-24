---
name: charts
description: House rules for charts and the code that draws them, analytical tables, dashboards, and slide pages that show data, in matplotlib, Plotly, Vega-Lite, Excel, PowerPoint, or HTML. Use when making, reviewing, or fixing any data display, alongside any general chart skill, including IBCS business reports.
---

# Charts

Apply these house rules to every data display you make, review, or fix. They work alongside a
general chart skill's method, and where their defaults differ, these rules decide. A project's or
organisation's own chart style controls its stated scope.

[Prose](../prose/SKILL.md) owns the words in and around a display. Its [tables and
figures](../prose/references/tables-and-figures.md) rules govern titles, labels, notes, sources,
and text alternatives. When the request selects IBCS notation, read [IBCS
charts](references/ibcs-charts.md) before drawing.

## Marks

- **Contrast.** Draw every mark a reader needs at 3:1 or more against the background, measured on
  the colour as drawn after any opacity. Greyed context series, range bands, and reference, event,
  and leader lines all count, and a light band can meet the ratio with an edge line. Separate
  adjacent fills with a gap in the background colour. Set all chart text, tick labels and notes
  included, at 4.5:1 or more.
- **Direct labels.** Name each series or stacked segment beside its marks: at the line end, inside
  or next to the segment, or above the stack. Use a legend only when direct labels would collide,
  and then give each series a second cue, such as a pattern, marker, or line style.
- **Labelled values.** When every mark carries its value, leave out the value axis and its
  gridlines.
- **One scale per measure.** Draw small multiples, sparklines, and chart tiers that share a unit on
  one scale, or index them, and state which on the display. When panels or sparklines of different
  measures each have their own scale, say so on the display.
- **Axis start.** When a line chart's value axis starts above zero, mark the break on the chart
  with an axis-break symbol or a short note.

## Layout

- **Destination size.** Build each figure at its final size, such as 16 cm wide for an A4 report
  column, with every text element at 8 pt or more at that size, or 12 pt on a slide. Save raster
  images at 300 dpi.
- **Page width.** Load web charts, tables, and dashboards at 390 px and 1366 px wide, and measure
  `document.documentElement.scrollWidth` at each. It equals the viewport width, and a wide table
  scrolls inside its own container. Put what needs attention first at every width, and keep tick
  labels horizontal by showing fewer of them.
- **Table totals.** When an analytical table's values add up, total each dimension that adds up,
  such as all sectors for each region and all regions for each sector. Place each total after the
  rows or columns it sums, and set every total off with a rule and bold figures: total rows, total
  columns, and each row of a block of totals such as all regions. When suppressed cells leave a
  total incomplete, withhold that total with the table's symbol or show it with a note, and keep
  the complete totals.
- **Changes.** Sign every change and difference, as in +1.5 and −0.6.

## Delivery

- **Title and source.** Give each display a title of ten words or fewer that states its finding,
  and put its supporting figures, measure, and period in a subtitle. Give it a source line that
  names the producer or system and the data's date. On a dashboard, the headline follows the same
  limit and each panel title names its measure. The producer is the organisation or system that
  made the data. When the request does not name it, write a marked placeholder in its place, such
  as `Source: [producer to confirm], September 2026`, keep any file name as an extra detail, and
  ask the user for the producer in your reply.
- **Findings from the data.** Check each finding against the data, including when the pattern it
  describes starts and ends. When a script writes a title, note, or text alternative, compute its
  figures and findings from the data in that script, so a rerun keeps them true.
- **Rounding.** Compute derived figures from unrounded values, such as counts rather than a
  supplied rounded rate, and pass unrounded values to charts, tooltips, and tables. Round each
  figure once, when you format it for display, to the precision the reader needs.
- **Text alternative.** Give each chart, sparklines included, a text alternative that names the
  chart type and finding, says where the value axis starts when it starts above zero, and points to
  the values. Attach it where the output format carries it:

  | Output | Placement |
  | --- | --- |
  | HTML or inline SVG | `role="img"` with an `aria-label` on the chart, a caption, and a data table |
  | HTML data table | the table's title in its `<caption>` element |
  | Vega-Lite | the spec's top-level `description`, which vega-embed sets as the ARIA label, and a data table |
  | Excel | xlsxwriter's `insert_chart(cell, chart, {"description": text})`, or the `descr` attribute on the chart drawing's `cNvPr` |
  | PowerPoint | the `descr` attribute on the chart frame's `p:cNvPr` |
  | PNG or SVG file for a document | alt text in the reply, ready to paste into the document |

- **Build script.** Keep the script that builds each output in the project beside the output,
  runnable with the project's own environment, and name it in the reply.

Complete the work when every display meets each rule above and Prose's tables and figures rules,
and you have looked at the rendered output at its delivered size.
