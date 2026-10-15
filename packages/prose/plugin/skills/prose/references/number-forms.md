# Number, date, time, and address forms

Apply this branch when reader-facing numbers, dates, or times are present. Also apply it to numbered
document divisions, numbers with proper names or titles, and postal addresses.
Apply the [authority and preservation rules](../SKILL.md#authority-and-preservation) before
changing a source form, identifier, interface token, or required data format.

When the rules below and the governing format leave a locale-format choice unresolved, or the task
requires source verification, consult the [registered locale sources](source-evidence/research-corpus.md#localisation-and-locale-data).
Use `AUTH-UNICODE-UTS35-NUMBERS` for locale-format structure and the pinned `AUTH-CLDR-DA-48-2`
or `AUTH-CLDR-EN-GB-48-2` record for the selected locale's data. A governing document or technical
format still controls its exact forms.

## Select a convention

Select one governing convention for each numerical category before choosing words or numerals.
Treat counts, ordinals, measurements, money, dates and times, durations and ages, percentages,
document divisions, and identifiers as separate categories when their functions differ.

| Convention | Select when | Controlling rule |
| --- | --- | --- |
| APA | APA Style governs the category. | Apply the [APA number conventions](apa-style.md#numbers-dates-and-mathematical-notation) and each compatible quantitative rule. |
| Technical or quantitative | The number expresses a quantity or technical value, or no other number convention controls. | Apply this module, [Quantitative prose](quantitative-prose.md), and any governing domain standard. |
| Locale | A locale or locale-specific authority controls the representation. | Apply the locale authority and each compatible language or quantitative rule. |
| Standard international conventions | A data contract using standard international conventions or an international specification controls the representation. | Apply [Standard international conventions](standard-international-conventions.md), the governing contract or specification, and each compatible quantitative rule. |
| Chicago | Chicago is explicitly selected for the category. | Apply the selected Chicago convention within its recorded scope. Do not assume Chicago's zero-through-one-hundred convention. |
| Other house style | Another house style is explicitly selected for the category. | Apply the selected convention within its recorded scope. |
| Governed content | The number belongs to exact source wording, an official name or title, a quotation, identifier, interface token, machine form, or label. | Preserve the required words, digits, and formatting. |

Apply one convention consistently to comparable values in each immediate category. Keep unrelated
categories under their own conventions and record any higher-authority exception.

Recast an authored sentence that begins with a numeral, or spell out the number when recasting
would be worse. Preserve a year, identifier, interface token, or other governed form and recast
around it when practical.

## Number words and ordinals

- **Large-number names.** When the writer may be unfamiliar with English usage, check how terms such
  as *billion*, *trillion*, and *quadrillion* are used. State the number of zeros or use numerals when
  readers could misunderstand a term. In Danish, *milliard* (*mia.*) means 10⁹, *billion* means
  10¹², and *trillion* means 10¹⁸. Render an English *billion* as *milliard* and an English
  *trillion* as *billion*.
- **How to write numbers.** Spell out numbers when there might otherwise be confusion.
- **Counts.** When APA, Chicago, or another selected style does not govern counts, write a count
  without a unit below 10 in words, as in *two scenarios*, and from 10 in numerals. Use numerals for
  every count of the same kind in a sentence that includes a count of 10 or more.
- **Ordinal numbers.** In English prose outside APA and governed compact formats, spell out *first*
  through *tenth*. For larger ordinal numbers, use a numeral followed by *st*, *nd*, *rd*, or *th*.
  Do not superscript these endings.
- **Roman numerals.** Use a Roman numeral only when an official name, source form, or governed label
  requires it. Otherwise, use an Arabic numeral in ordinary prose. Preserve the required case and
  form.
- **Fractions.** Use a decimal when it preserves the required value and improves comparison.
  Otherwise, spell out a simple fraction or use the governing mathematical notation.

## Dates and times

- **Dates.** Outside machine-readable values, governed formats, and source text, write an English date as
  `13 April 2026`. Put the date before the month. Do not add *st*, *nd*, *rd*, or *th*. Omit commas.
  Write a Danish date as `13. april 2026`, with a full stop after the day and a lowercase month.
- **Combined date and time.** Put the date before the time unless a governed format requires another
  order.
- **Time zones.** Preserve the timestamp format in the user interface when available. Otherwise,
  spell out the region and include the applicable UTC or GMT label. Do not abbreviate the name of
  the time zone.
- **12- and 24-hour clocks.** Use either the 12- or 24-hour clock, not both in the same text.
  Preserve clock forms in governed or quoted text.
- **Danish clock times.** In Danish prose, use the 24-hour clock and separate hours and minutes with
  a full stop, as in `kl. 16.00`.
- **Noon.** Use *noon* instead of *12 a.m.* or *12 p.m.*
- **Midnight.** Avoid *midnight* for a deadline because it can mean the beginning or end of a day.
  State an unambiguous date and time instead. State the time zone when readers could differ.
- **Decades.** Include the century in a decade reference unless the context makes it unambiguous.

## Decimal forms and digit grouping

- **Decimal markers.** Use a comma in Danish prose and interface text and a full stop in English prose and interface text. Preserve another marker only in code, a source quotation, or a required technical format.
- **Digit grouping.** Use a full stop to group thousands in Danish prose and interface text, as in `35.000`. Use a comma in English prose and interface text, as in `35,000`. Omit grouping from machine-readable data, technical syntax, years, page numbers, and identifiers.
- **Leading zero.** Write a zero before the decimal separator in a value below 1. In APA statistical notation, omit the zero when the statistic cannot exceed 1.

## Units and rates

- **Spaces in units of measurement.** Put a space between a number and its unit symbol. In English prose, close `%` up to the numeral, as APA and ONS require. This form departs from the SI Brochure's space rule. In Danish prose and interface text, keep the fixed space before `%`. Omit the space before `°`, `′`, and `″` when they denote plane angle. Keep a number and its unit symbol together with a non-breaking space (`\,` in LaTeX).
- **Temperature spacing.** Write `50 °C`, `50 °F`, or `300 K`. Keep a non-breaking space between the
  number and scale. Use no degree symbol with kelvin.
- **Decimal and binary units.** Use the same system to measure bytes as the source technology. Distinguish
  `kB`, `MB`, and `GB` from `KiB`, `MiB`, and `GiB`. Do not substitute one system for the other.
- **Rates.** Use *per* in running prose. Use a slash or established abbreviation only in a compact
  or governed format.

## Operators, dimensions, and ranges

- **Aspect ratios.** Write an aspect ratio with an unspaced colon, such as `16:9`. For images, put
  width first unless a governed format defines another order. State the order when context does not
  establish it. Preserve an official format or exact source notation.
- **Multiplication and dimensions.** Use `×`, not the letter `x`, as the multiplication operator in
  reader-facing arithmetic. In ordinary prose, write dimensions with *by*. For a compact
  specification or resolution, use its governed separator and spacing, such as `1024 x 768`. Do not
  treat that separator as arithmetic. Use `\times` in LaTeX. Code keeps its language operator.
- **Minus sign.** Use `−` for subtraction and negative values in reader-facing text. Use `-` only where a hyphen or technical syntax is required. Put no space after a unary minus and spaces around subtraction. A financial table may instead show negative values in parentheses; use one convention within each display and state it in a note when readers may not know it. Do not let a sign and a row label, such as *less* or *loss*, both express the same negative.
- **Numerical ranges.** In English prose, write a range as *from A to B* or *between A and B*. In Danish prose, use *fra A til B* or *mellem A og B*.
- **Compact ranges.** In compact English technical content, use an unspaced en dash rather than a hyphen, as in `200–250`. In compact Danish content, use a hyphen, as in `200-250`; between long expressions, a spaced en dash may replace it (*Retskrivningsordbogen* § 57.9). Reserve *from*, *fra*, *between*, and *mellem* for worded ranges. Write both endpoints in full. Repeat a percentage or currency symbol at both endpoints, as in `25%–30%` in English and `25 %-30 %` in Danish.
- **Ranges of numbers with units.** Attach the unit name or symbol to both limits. Repeat a closed-up symbol at
  each endpoint. Alternatively, enclose the full numerical range in parentheses before one shared
  spaced unit. Write `10 kg to 12 kg` or `(10 to 12) kg`, not `10 to 12 kg`.

## Document numbers and line references

Apply this section to a numbered document division, a number in a proper name or title, or a line
or procedural-step reference.

- **Line references.** Use *l* for one line and *ll* for a range. Close the number up to the letter.
  If the typeface does not distinguish lower-case *l* from `1`, change the typeface or write *line*
  or *lines* in full.
- **Procedural step references.** Use a numeral when referring to a numbered procedural step,
  including a step number below 10. Preserve governed cross-reference syntax.
- **Numbered divisions in books and other documents.** Preserve the numbering, capitalisation,
  abbreviation, and cross-reference form required by the governing legal authority, publisher,
  citation style, or source. Otherwise, apply the selected convention for words or numerals and the
  document convention.
- **Numbers with proper names and titles.** Verify the official or source form. Preserve whether it
  uses words, Arabic numerals, or Roman numerals. Preserve its required order, capitalisation, and punctuation.

## Postal addresses

Apply the postal authority for the destination and the selected locale. Preserve required line
order, abbreviations, punctuation, and postal code. When reproducing an address, preserve its source
form. Apply ordinary-prose conventions only outside that scope.

Complete this branch when each form follows its selected language, document, locale, source,
technical, or data convention without changing governed content.
