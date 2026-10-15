# Visual and media prose

Apply this branch when images, audio, video, animations, colour-dependent presentation, or
responsive layouts are present.

- **Images.** Use images sparingly. Use an image when it is easier to show something than to
  describe it.
- **Repeated images.** Repeat an image only when readers need it at another point to understand or
  complete the task.
- **New information in images.** Do not introduce information only through an image or symbol. Provide an
  equivalent text explanation with it.
- **Images of text.** Use authored text for prose, code samples, and terminal output. Use an image
  only when appearance is itself the subject.
- **Personally identifying information (PII).** Exclude PII, credentials, and sensitive content
  from every image.
  Irreversibly redact necessary source evidence before publication. Do not rely on blur or mosaic
  effects.
- **Alternative text (alt text).** Give each informative image alt text that conveys its purpose or
  essential information.
- **Decorative images.** Mark every purely decorative image so assistive technology ignores it.
  Use `alt=""` in HTML.
- **Alt-text purpose.** Describe the image's meaning or function, not every visible detail.
  Include embedded text absent from surrounding content.
- **Redundant alt text.** Start with specific content or function. Omit filenames and
  repeated captions. Omit *image*, *button*, or *link* when the interface already announces the type.
- **Functional images.** For an image-only link or control, name its action or destination rather than
  its appearance. Treat an image beside an equivalent visible label as decorative.
- **Complex-image descriptions.** Put the long description in
  surrounding prose or linked content. Give the image a short description that identifies it and
  leads readers to the long description.
- **Audio-only transcripts.** Provide a transcript for informative audio-only content.
  Include speech and non-speech sounds needed for understanding.
- **Video captions.** Provide synchronised captions for every informative video
  with audio. Include speech and non-speech audio needed for understanding.
- **Audio descriptions.** Provide an audio description of essential visual information whenever the
  main audio omits it. Also provide equivalent written information.
- **Video-only alternatives.** Provide an equivalent written description or audio track for
  informative video-only content.
- **Animation alternatives.** Give every informative animation a text alternative. Describe its
  essential change or sequence.
- **Image maps.** Provide text links for every image-map destination. Keep their
  order usable through keyboard navigation and with bidirectional text.
- **Colour alternatives.** Pair colour with text, pattern, shape, or another accessible cue that
  carries the same meaning.
- **Contrast.** Meet the applicable WCAG contrast requirements. Verify text and essential graphics
  under applicable high-contrast themes.
- **Text backgrounds.** Keep text off backgrounds, watermarks, and images that reduce contrast.
- **Reflow and enlargement.** Preserve meaning and reading order during reflow, zoom, and text
  enlargement. Keep stacked columns and sidebars in logical order.
- **Line breaks.** Use paragraph and layout controls for boundaries within sentences and paragraphs.
- **Image enlargement.** Let readers enlarge a complex image that cannot reflow legibly.
  Otherwise, provide a long description.

Complete this branch when every informative medium has an equivalent alternative and every
decorative medium is correctly excluded. The rendered reading order remains usable.
