# Maintaining a terminology register

Apply this branch only when creating or maintaining term records alongside [Terminology
selection](terminology-selection.md).

Record the concept, subject field, selected form, scope, source, and evidence basis. ISO 704
distinguishes objects, concepts, definitions, and designations: a definition defines a concept, and
the selected `preferred_designation` names it.

- Use the narrowest authoritative locator. Record the whole entry only when it is the narrowest
  source for the designation.
- Distinguish a source that *defines a concept*, a source that *uses a designation*, *established
  usage* demonstrated by professional literature, and a *local description* that makes no
  external-usage claim. Keep these evidence bases separate from a source's stated preferred,
  admitted, or deprecated term status, and record only a status the source supplies.
- Keep exact-form requirements separate in `verbatim_form_required`. Keep a local label for quoted
  or official wording separate from externally defined terms.
- Record each additional authority and its actual concept; naming a source alone establishes neither
  a definition nor a designation.

Complete this branch when each added or updated register row records its concept, subject field,
`preferred_designation`, narrowest locator, evidence basis, verbatim-form flag, and verified source
status.
