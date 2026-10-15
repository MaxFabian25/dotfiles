# Source provenance

Use this module for Prose's registered source and term provenance. It links canonical source
identity to the relevant source-family evidence.

## Lookup

1. Find the exact `authority_id` in the [Authority Register](source-authorities.csv).
2. Use its `citation` as the canonical source identity. Keep that citation unchanged.
3. Use `source_locator` for the cited work and `locator_key` for the recorded clause or entry.
4. Check that the requested evidence basis appears in `permitted_evidence_bases`.
5. Check the record's `evidence_role` and `maturity` before relying on it as provenance. Use the
   [role definitions](#evidence-roles) when interpreting an unfamiliar role.
6. Open `evidence_record` only when source scope, adaptation, licence, or reproduction affects the work.

For a registered term, use its `source_id` to locate the matching authority row. Read the linked
evidence only when its scope, adaptation, licence, or reproduction affects the work. A source
identifier beginning with `LOCAL-` describes a local concept and has no external authority record.

Complete the lookup when the requested identities, locators, evidence bases, and scope resolve to
their canonical records. Report an unresolved check for the work that depends on it.

## Canonical records

Keep each source's identity, citation, locator, permitted evidence bases, evidence pointer,
evidence role, and maturity only in the Authority Register.

The [Terminology Register](terminology-records.csv) retains term provenance, source locations, and
selection evidence. Its `source_id` joins an externally grounded term to one authority record.
Reader modules own shared operative terminology guidance. The register retains source-specific
selection evidence; a blank `selection_rule` adds no exception to the recorded form, scope, and
shared rules. Consult it when a term's provenance or source status matters.

The [Google Page Register](google-developer-documentation-pages.csv) owns Google page review
records: scope, disposition, licences, and the revision and access dates of the latest check. A
Google authority record keeps the dates of the version its citation identifies.
The Google entry register owns source anchors and markers. The decision register owns unique
word-list adoption treatments and points to the module that owns each shared rule. Each additional
destination applies only under a distinct condition stated in its treatment.

An authority's `evidence_record` identifies its source-family evidence. Read its scope limits and
only the topic needed for the question. When the pointer names a parent section, select the
relevant existing subsection. For Chicago quotation provenance, use its [source-text
subsection](source-evidence/chicago-manual.md#source-text-and-other-languages).

Registration preserves source identity. It does not activate a reader rule. The Authority Register
states whether research is adopted, supporting, conditional, adjacent, draft, or superseded and
records its maturity. The requested writing scope determines when a source-backed reference
applies.

## Evidence roles

- **Adopted** supports an independently worded rule in an authored reference.
- **Supporting** corroborates an active rule within the source's stated scope.
- **Conditional** applies only when the named output context is selected.
- **Adjacent** preserves relevant research without making the source a reader rule.
- **Draft** preserves provisional evidence and supports no conformance claim.
- **Superseded** preserves historical provenance and yields to the registered current source.

## Reproduction boundary

The skill contains source designations and independently worded adaptations. It contains no
extended source text.

Base reproduction on an applicable licence, grant, or governing legal authority. Apply [Rights,
permissions, and credit lines](references-and-citations.md#rights-permissions-and-credit-lines).

Keep the independently worded rule when permission or an exact locator remains unresolved. Record
the unresolved source condition without making an unsupported attribution.

## Source drift

When updating an adopted rule or recorded source evidence, compare the affected mutable source with
its recorded revision and access date. Record any change that affects the rule's meaning or scope
and update the affected evidence.
