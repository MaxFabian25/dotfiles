# Google source evidence

## Google developer documentation style guide

The [entry register](../google-developer-documentation-word-list-entries.csv) maps Google's
word-list anchors and source markers to the [local adoption decisions](../google-developer-documentation-word-list-decisions.csv).
The [Google guidance](../google-developer-documentation-style.md#word-list-evidence) explains
targeted lookup and when to check the live source.

The [page register](../google-developer-documentation-pages.csv) records the pages reviewed on
22 August 2026, their scope, and their provenance. Page revision dates are Google's rendered
`Last updated` values; access dates identify subsequent checks.

The [kebab](https://developers.google.com/style/word-list#kebab) and
[typically](https://developers.google.com/style/word-list#typically) entries were checked on
6 September 2026 against the page revised on 25 August 2026. The control-name entry uses the icon's
`aria-label`; its local decision points to the shared control-naming rule. The *typically* entry
limits the meaning to usual or expected circumstances and excludes sentence-initial use.
This targeted check does not reverify the other recorded word-list entries.

The [notices page](https://developers.google.com/style/notices) was checked on 6 September 2026.
Its rendered `Last updated` value remained `2025-09-08`. This check confirmed the exclusions
recorded in the [notice rules](../google-developer-documentation-style.md#notes-cautions-warnings-and-other-notices).

When changing adopted guidance or source records, follow the
[source maintenance rule](../source-manifest.md#source-drift).

`A` means adapt, `C` means already covered, and `M` means mixed prose and implementation.
`R` means reject. `N` means outside the reader-facing prose scope.

These codes record local treatment. They do not change Google's authority within a page's stated
scope.

The [Google developer documentation style guide
module](../google-developer-documentation-style.md) adapts the guide's developer documentation scope.
Its source map routes shared concerns through the canonical prose branches. The adapter retains
Google product naming and differences from the shared rules for headings, code examples, notices,
UI terminology, and word choices. Each difference applies only within its stated scope. The
terminology register preserves its source designations. British English,
requirement language, and ASD-STE100 remain controlling local rules. Google product rules remain
inside their named scope.

The [API-reference prose module](../api-reference-prose.md) independently adapts Google's guidance to
describe all public classes, methods, constants, and other members. It also adapts Google's guidance
about first sentences, parameters, return values, exceptions, code samples, and deprecations from
[API reference code comments](https://developers.google.com/style/api-reference-comments). The
module also adapts the present-tense third-person rule from [Verb forms in reference
documentation](https://developers.google.com/style/reference-verbs).

Google's Site Policies apply the page-footer licence unless a page states different terms.
`LIC-GOOGLE-PROSE` means CC BY 4.0 for page prose. `LIC-GOOGLE-CODE` means Apache License 2.0 for
code samples.

Neither record covers Google trademarks, brand features, or third-party images, audio, video, and
linked material.

This skill paraphrases and reorganises selected editorial guidance. No Google code sample or
separately licensed media is copied.

For a prose adaptation, include Google, the direct page locator, `LIC-GOOGLE-PROSE`, and a
modification statement in the attribution. Do not imply Google endorsement.

Before reusing other material, verify its separate rights and required notices. Stop when those
rights cannot be verified. Link or paraphrase the source instead.

For copied Google code in a future change, apply Apache 2.0 and carry its required licence, notices,
and modification statements.

## Google documentation guide

The source tree identifies Version 2.0. The revision records the exact snapshot because later
changes can retain the same version marker.

The guide supplies supporting literature for Markdown style, documentation best practices,
README files, API documentation, images, and tables. It does not define this skill's branches or
local rules.

Parts of this skill adapt Google's *Google documentation guide*, Version 2.0, at the recorded
revision. Google publishes the style guides under Creative Commons Attribution 3.0 Unported.
Changes include independent rewording, reorganisation, integration with other authorities, and
the skill's British English rules. Google has not endorsed this skill.

The skill reproduces no source examples, quotations, metaphors, screenshots, or generated site
assets. It does not adopt the attributed Kernighan or Laozi passages. Linked works remain outside
this source record.

Keep this source separate from the Google developer documentation style guide above. That live
guide uses different terms, scope, and licence conditions.
