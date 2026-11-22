# Standard international conventions

Apply this branch when a data contract or specification requires standard international
conventions. Within this scope, these rules replace the [house date and time
forms](number-forms.md#dates-and-times) and the [currency code
order](quantitative-prose.md#comparisons-and-statistical-claims) in quantitative prose.

## Basic rules

- Write a country name in reader-facing prose unless the code itself is the subject.
- When a code represents a country, use the country code required by the data contract. Otherwise,
  use an applicable ISO 3166-1 country code.
- When a country code appears as data, label it as a country code and identify its required format.
- Express currency amounts with an ISO 4217 alphabetic code unless the data contract requires
  another representation. Put the amount first, followed by a space and the code in capitals.
- Write machine-readable dates and dates governed by standard international conventions as
  `YYYY-MM-DD`. Use a 24-hour clock with colons between time fields.
- Represent Coordinated Universal Time (UTC) with `Z`. Represent local time with an offset to UTC
  when the time must be unambiguous.
- Write a language reference name in reader-facing prose unless the language identifier itself is
  the subject.
- Use the language identifier required by the data contract. Otherwise, choose an applicable ISO
  639 set.
- Preserve the registered identifier length and each extension required by the data contract.
- When a language identifier appears as data, label it as a language code rather than a language
  reference name.
- Start an international telephone number with `+` and its country code. Apply spacing from the
  governing numbering plan or data contract.
- In reader-facing prose, introduce a telephone extension number with the full, unabbreviated
  designation set by the prose unit's language authority. Preserve exact interface labels,
  data-contract fields, and machine-readable forms.
- Express a freephone number in local style. When the publisher controls the contact information,
  give a toll number if one is available.
- Under standard international conventions, use units of the International System of Units (SI) to
  express quantity values. Follow a recipient, regulator, or data contract that requires another
  system.
- Give an equivalent value in a non-SI unit in parentheses after the SI value when readers need it.
  Apply a documented conversion and rounding method.

Complete this branch when every governed data value follows the data contract's required syntax
(or, where unspecified, ISO 3166-1 country codes, ISO 639 language codes, amount-first ISO 4217
currencies, `YYYY-MM-DD` dates, 24-hour times with `Z` or a UTC offset when the time must be
unambiguous, `+` international phone numbers, and SI units with any needed converted equivalents)
and running prose uses full country and language names unless the code itself is the subject.
