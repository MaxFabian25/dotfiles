# Source reviews

Each review of an outside style or design source for Prose, with what it adopted, what it left out
and why, and the release. The plugin's
[source records](plugin/skills/prose/references/source-manifest.md) cover only the sources its
guidance uses; this file also keeps the rejections, so a later review starts from them.

| Date | Sources | Adopted | Left out | Release |
| --- | --- | --- | --- | --- |
| 2026-10-22 | [Digital.gov plain language guide](https://digital.gov/guides/plain-language), [GSA plainlanguage.gov repository](https://github.com/gsa/plainlanguage.gov) | Definition rules from "Avoid jargon", linked from the editorial checks | Every other candidate rule: none changed Opus or GPT-6.1-sol output in a blind comparison | 0.2.2 |
| 2026-10-22 | IBCS 2.0 standard | A conditional IBCS notation reference for business reports, presentations and dashboards | ISO 24896, whose text was not reviewed | 0.2.3 |
| 2026-10-24 | ISO, EN 301 549, WCAG, Microsoft Writing Style Guide, SEC plain English handbook, Apple Style Guide, Red Hat style guide, Google developer documentation style guide | Plain-language drafting labels, the conformance standard's version, an Apple style adapter, interface names in the reader's language, worked financial examples, release-note anatomy, RFC 9542 documentation MAC addresses, Google term forms; routing for instructions and release notes | An identification check and a risk-statement rule, which changed nothing in test runs | 0.3.2, 0.3.3 |
| 2026-10-24 | EU data visualisation guide, Data Viz Project, From Data to Viz, Data Viz Catalogue, Dataviz Inspiration, Xenographics, FT Chart Doctor, NYT Graphics Desk, FT Visual and data journalism | Nothing | Catalogues and galleries give no design rules; the EU colour rules are model defaults, and its advice against red and green conflicts with IBCS; Chart Doctor's map and correlation rules had no test task | None |
| 2026-10-24 | EU guide, FT Chart Doctor, NYT Graphics Desk, FT Visual and data journalism, read with the user's subscriptions | Chart titles of ten words or fewer, with figures in a subtitle | The rest, already covered | 0.3.4 |
| 2026-10-24 | EU guide's summary of the Chartability tests | A 12 px floor for web chart text | Other Chartability tests, already covered | 0.3.5 |
| 2026-10-25 | W3C Understanding SC 1.4.11 | Shaded-map classes from a light lowest class, with 3:1 on region outlines | None | 0.3.6 |
| 2026-10-25 | EU guide, FT Chart Doctor, NYT Graphics Desk, FT Visual and data journalism; Matthew Ericson, "When Maps Shouldn't Be Maps" | Shading map regions by a rate, after a test without a rule drew a misleading map | A rule to choose a map only when location matters: writers already did so in 3 of 3 runs | 0.3.7 |
