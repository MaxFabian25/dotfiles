# IBCS notation

Select this branch only when a user, organisation, or project explicitly requires IBCS notation for
a business report, presentation, or dashboard. It covers the text elements of each page: the key
message, titles, headers, labels, and notes. An organisation's documented IBCS notation controls
where it differs from the forms below. Within this scope, these forms replace the date and range
forms in [number forms](number-forms.md) and the [currency
forms](quantitative-prose.md#comparisons-and-statistical-claims) in quantitative prose.

This module is an independently worded implementation of authority `AUTH-IBCS-2` in [source
provenance](source-manifest.md), checked against the standard's text.

- **Titles.** Write the title in three descriptive lines: the reporting unit, the measure with its
  unit (*Net sales in mEUR*), and the period followed by its scenarios (*2026-Q3 AC and BU*).
- **Periods.** Write dates and periods in ISO 8601 order, such as `2026-10-05`, `2026-03`, and
  `2026-Q3`. Join a span with two unspaced full stops, as in `2026-01..09` or `Jan..Sep`.
- **Money.** Write the ISO 4217 code with a lower-case scale prefix, such as `kEUR` or `mEUR`.
  Scale values to at most four digits in a table and three in a chart label.
- **Scenarios.** Label scenarios AC (actual), PY (previous year), PL (plan), BU (budget), and FC
  (forecast). Order them PY, PL or BU, FC, then AC.
- **Variances.** Name an absolute variance by its reference scenario, as in ΔPL, and a relative
  variance with a trailing %, as in ΔPL%. Give every positive variance a plus sign. Show N/A for a
  relative variance against a negative or zero reference.
- **Notes.** Define every abbreviation the page uses in its notes, including scenario codes,
  variance labels, and scaled units such as mEUR.

Complete this branch when every key message, title, header, label, and note uses these forms or the
organisation's documented equivalent.
