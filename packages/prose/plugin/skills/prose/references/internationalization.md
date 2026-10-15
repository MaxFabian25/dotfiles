# Internationalisation

Apply this branch to content prepared for localisation or for readers across languages, regions,
or cultures. These rules are language-neutral. Apply a language adapter separately when one is
selected. Apply the [authority and preservation rules](../SKILL.md#authority-and-preservation)
before changing source, product, or machine forms.

For technical text intended for translation, consult `AUTH-ISO-18968` within its registered
publication stage. Use `AUTH-UNICODE-UTS35-NUMBERS` and pinned CLDR records for locale data, not as
general prose authorities. Make no ISO 18968 conformance claim while the registered work remains
under publication.

## Basic rules

- Resolve an unsettled spelling, usage, or terminology choice with an authority for the target
  language or region.
- Choose examples and images that avoid unstated cultural, geographic, or socioeconomic
  assumptions.
- Avoid idioms, colloquial expressions, slang, humour, and culturally specific references unless
  they are the subject. These references include gestures, holidays, landmarks, and imagery.
- Keep text requiring translation outside images. Provide editable source text when an image must
  contain it.
- Check each linked product, service, and resource in every stated region. Mark any regional limits.
- Use layouts that permit text expansion, right-to-left text, and vertical text when required.
- Keep one designation for each concept under [Plain language in
  practice](reader-core.md#plain-language-in-practice).
- Limit long coordinated clauses and unexplained sentence fragments.
- Keep paragraphs concise when the content will be translated, because text can expand after
  translation.
- Use an unambiguous date form in reader-facing prose.
- Name a country or region when a place name could be ambiguous.
- Use the prose unit's language equivalent of *country or region* for list headings and descriptions.
- Use the same form in other contexts that might include named dependencies or disputed
  territories.
- In general discussions, the prose unit's language equivalent of *country* is acceptable.
- Do not use the prose unit's language equivalents of *geography* or *geo* as synonyms for *country*
  or *country or region*.
- Identify currencies under [Currency](quantitative-prose.md#comparisons-and-statistical-claims).
- Add a time zone when readers outside the local zone must act on the time.
- Use a calendar period instead of a season. State the hemisphere when the season matters.
- Make forms support postal codes, phone numbers, name ordering, personal names, and forms of
  address for every supported locale.
- Use a symbol only when intended readers recognise it or the format requires it.
- Use *24/7* only when intended readers recognise it. Otherwise, express *24 hours a day, 7 days a
  week* in the prose unit's language. Do not use *24x7*.
- For machine-translated content, verify that acronyms resembling ordinary words remain unchanged.

## English localisation adapter

Apply these rules to English content prepared for localisation or for readers across languages,
regions, or cultures:

- Use words in their primary sense when the subject permits it.
- Use standard subject-verb-object order when it states the relationship accurately.
- Include helper words such as *then*, *that*, and *of* when they prevent ambiguity.
- Include articles and relative pronouns when omitting them could create ambiguity.
- Use no more than two nouns as modifiers of another noun when possible. Preserve a longer governed
  technical designation and explain its relationships in the surrounding prose.
- Replace a phrasal verb with a simpler verb when both forms have the same meaning. Retain an
  established technical phrasal verb and define it when intended readers might not know it.

Complete this branch when each supported locale carries the content's meaning, structure, and
variables, and no locale-dependent assumption remains.
