# Technical and code-adjacent prose

Apply this branch to durable technical documentation, commands, paths, identifiers, docstrings,
release notes, and other code-adjacent prose. For
storage terminology, technical dates, example network or phone values, command-line syntax, internal
API documentation, code examples, or source-code comments, also open [Technical formats, commands,
and code samples](technical-formats-and-code-samples.md). Follow the authority and preservation rules
before changing a literal, source form, official name, or defined technical term.

For an unresolved convention or a requested source audit, consult the [registered developer
documentation sources](source-evidence/research-corpus.md#interface-and-developer-documentation).
Use `AUTH-GITHUB-DOCS-STYLE` and `AUTH-GITHUB-DOCS-BEST-PRACTICES` for GitHub documentation and
repository prose, and `AUTH-MICROSOFT-LISTS` for lists in Microsoft documentation. Keep these sources
subordinate to a selected product, publisher, contract, or technical standard.

## Contractions

- Expand contractions in original technical prose. Write *let us* only when that meaning is needed.

## Literal formatting

A *code span* is an inline sequence of code-formatted characters in CommonMark.

- Use a code span for exact text that a reader must type, match, or identify as code. This text includes commands, flags, paths, filenames, identifiers, environment variables, and literal values. Keep product names and ordinary technical terms in plain text.
- Keep prose punctuation outside a code span. Include punctuation in the span only when it belongs
  to the exact literal.
- When writing about a particular programming language, follow the capitalisation style of that
  language.
- Follow the programming language or project's convention for punctuation spacing. Otherwise,
  choose one spacing pattern and use it consistently.
- When instructions describe a casing style, pair the casing style name with its requirements and
  an example rather than using the name alone.
- Use an ordinary verb to describe the operation of a function, method, or logical operator.
  Keep each code element's exact name unchanged. For plurals and possessives, add an ordinary noun
  after the exact code name and inflect that noun.
- Format a Boolean literal as code. Write the truth or falsity of a condition in ordinary text.
- Format a command-line utility name as code when it names the executable. Keep the corresponding
  product or project name in ordinary text.

## URLs and email addresses

- Format an email address as code only when it is exact computer input or output. Otherwise, present
  it as contact text or a link.
- Keep each URL and email address intact in source and copyable in rendered output. Use
  host-supported wrapping that preserves the literal.
- Recast a sentence if its final punctuation could look like part of a bare URL or path. If the
  ambiguity remains, put the literal on its own line without adding sentence punctuation.
- Apply [Link selection and behaviour](references-and-citations.md#link-selection-and-behaviour) to
  every link in technical prose.

## Files and paths

- When referring to a specific file, write its exact filename in a code span. Preserve its spelling
  and follow it with *file*. Introduce an included sample with a sentence that names the file.
- Distinguish a document's name from its actual filename. In user materials, present the document's
  name as the selected style requires. Use the filename only when readers must identify or use the
  actual filename. Format it as code.
- Distinguish a file type name from a filename extension. Follow the source authority's
  capitalisation for each. Format a literal filename extension as code, including its leading full stop
  when the syntax requires one. For example, *JPEG* is a file type name, and `.jpg` is a filename
  extension.
- Refer generically to a file type by its formal name (not by a filename extension), pairing it
  with an ordinary verb rather than using the file type as a verb.
- In software instructions, use *file* for the broader class of stored objects. Use *document* only
  for an authored, user-editable file.
- In developer materials, use *directory* for an organisational unit or container in a file system.
  In other materials, use *folder*.
- Distinguish an absolute path from a relative path under the target platform's rules. State the
  base directory when readers cannot infer a relative path's base.
- Name the shell or platform before using a tilde to represent a home directory. Explain the tilde
  at first use and preserve its exact syntax.
- For replaceable path components, apply [Placeholders](#placeholders).

## Placeholders

- In a syntax description, format literals as code. Use italics for placeholder names unless the
  selected style requires another format.
- Keep brackets that enclose something optional in regular text unless the programming language
  makes them literal.
- Use the host or project's placeholder convention in a path. Otherwise, use a visibly
  replaceable placeholder without brackets that readers could mistake for literal path syntax.
- To refer to placeholder content, use *placeholder*, *sample*, *test*, *stub*, or another
  alternative rather than *dummy*. Give every placeholder a descriptive name that cannot be mistaken
  for literal input. Explain how to replace it when the surrounding task does not make that clear.
  Explain an exact project or API placeholder outside the literal.

## System behaviour

- Write *internet* in lowercase.
- Refer to a single status code with its number and name, such as an HTTP `400 Bad Request`
  status code. Call it a status code in ordinary documentation. Use *error* only when the protocol
  class and described situation establish an error.
- When describing system behaviour, do not use an action noun as a verb if a precise verb names the
  operation. For *message* or *output*, state whether the system sends, displays, writes, or returns
  the content. Retain a verb established by the governing technical authority.
- In user materials, name the observable problem, consequence, and recovery action
  before unfamiliar technical terminology. Describe an unresponsive system or application as *not
  responding* or name its measured state (rather than *hang* after a system error), reserve *freeze*
  for pointer behaviour, and explain any retained *bug*, *corrupted*, or *crash* wording.
- Use *compatible with* or *works with* for interoperability. Retain *support* for customer
  assistance, maintenance, or a defined capability.
- Name the relevant computer, storage device, component, or service instead of a generic *system*
  or *unit*. Retain *system* or *unit* when that scope is intentional and defined.
- When one specific device, service, or product is the subject, name it instead of using generic
  *product*. Use *product* for a category or general support context.
- Use *computer*, *device*, or the exact platform instead of *machine* when that is the intended
  object. Replace an unexplained *native* with the platform, behaviour, integration, or compatibility
  property it means.
- Replace an unqualified *regular* with the intended contrast, such as *standard*, *recurring*, or
  *frequent*. Replace *well-behaved* with the observable behaviour or requirement.

## Technical claims

- Replace an impression about a system with observable behaviour, evidence, an example, or a
  measured result.
- Support each factual technical claim with a source, observation, or measured result. Mark an
  inference and state its basis.
- Use *quality* as a noun unless it belongs to an established compound. Name the relevant property,
  criterion, or context instead of making an unqualified quality claim. Support an evaluative
  quality claim with evidence.
- Support every performance, cost, reliability, compatibility, security, compliance, or superlative
  claim with the measured property, test conditions, version or date, and result rather than making
  an unqualified claim. When evidence supports only design intent, use *designed to* or *intended
  to* and do not imply a guarantee.

## Versions and time

- In product and reference documentation, check whether time can make a statement about product or
  feature capabilities inaccurate. Examples include
  *as of this writing*, *currently*, *does not yet*, *eventually*, *existing*, *future*, and
  *latest*. They also include *current*, *new*, *now*, *old*, *presently*, *recent*, and *soon*.
  Remove the word when time is not part of the meaning. Otherwise, give a reference point, such as
  a date, version, release, condition, or live authority.
- Retain these words in procedural or time-stamped content, such as a press release, blog post, or
  release note. State the reference point.
- In a release note for a fixed issue, give the earlier cause and its consequence in the past tense,
  then the fix and its result in the present tense. Anchor the earlier state to the
  update, as in *Before this update*, and write each note as a sentence, not a changelog command
  such as *Fix race in lease renewal*.
- Document current capabilities rather than future features or products, pre-announcing them only
  when the owning authority has approved publication and keeping unapproved plans out of examples
  and implications.
- Use present tense for current capability.
- Use *release* for the issuing of software and *version* for a software version number.
- Include a version number with software names as needed, such as when describing a new version or
  comparing two versions. Otherwise, the version number can usually be omitted.
- Use *earlier* or *later*, rather than *lower*, *higher*, *newer*, or *older*, when referring to
  software version numbers.

Complete this branch when every contraction in original technical prose is expanded, each literal,
URL, path, and placeholder keeps exact syntax and copyable form, every technical claim cites
observable behaviour or measured evidence, and every version or time reference has a concrete
reference point.
