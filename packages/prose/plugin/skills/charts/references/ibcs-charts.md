# IBCS charts

Select this branch only when a user, organisation, or project requires IBCS notation for a chart,
table, or dashboard. Write every title, label, and note with Prose's [IBCS
notation](../../prose/references/ibcs-notation.md). That covers the three title lines, scenario
codes, signed variances, N/A for a relative variance against a zero or negative reference, and
notes that define every abbreviation and scaled unit. An organisation's documented IBCS notation
controls where it differs from the forms below.

- **Tiers.** Show a scenario comparison as tiers on one category axis: the base values, the absolute
  variance (ΔPL, ΔBU, ΔPY), and the relative variance (ΔPL%) as pins. Put time on a horizontal axis
  as columns, with the variance tiers above the base, and put structure, such as regions, on a
  vertical axis as bars, with the variance tiers to the right. Draw tiers in one unit at one scale.
- **Scenario fills.** Fill AC solid dark grey and PY solid light grey. Draw PL and BU as outlines
  with an empty interior, and FC as an outline hatched in the AC colour. Draw the reference scenario
  behind the analysed one, offset by about a third of the column width so that both stay visible.
- **Variances.** Colour a favourable variance green and an unfavourable one red, judged by its
  effect on the business. Draw a relative variance as a pin: a thin bar capped by a small square in
  the analysed scenario's fill. Where the relative variance is N/A, draw no pin and write N/A.
- **Labels.** Label every column, bar, and pin next to its end: above or right for a positive value,
  below or left for a negative one. Fit each chart label to three digits by choosing the unit, such
  as mEUR with one decimal. Name the scenarios beside the first or last column of each series, and
  leave out legend boxes, value axes, and gridlines.
- **Tool forms.** In Excel, scale labels with a number format (`#,##0.0,,` shows millions with one
  decimal), hatch FC with the `dkUpDiag` pattern (xlsxwriter `dark_upward_diagonal`), and offset a
  reference series by listing it first and setting the series overlap to about 50. In matplotlib,
  draw PL with `fill=False` and an edge colour, and FC with `hatch="////"`.

Complete this branch when every chart passes these rules and every text element passes Prose's IBCS
notation.
