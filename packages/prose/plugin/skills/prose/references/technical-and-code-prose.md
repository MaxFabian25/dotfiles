# Technical and code-adjacent prose

Apply this branch to durable technical documentation, commands, paths, identifiers, comments,
docstrings, examples, and other code items. Read the sections needed for the current choice.
Apply the [authority and preservation rules](../SKILL.md#authority-and-preservation) before
changing a literal, source form, official name, or defined technical term.

For an unresolved convention or a requested source audit, consult the [registered developer
documentation sources](source-evidence/research-corpus.md#interface-and-developer-documentation).
Use `AUTH-GITHUB-DOCS-STYLE` and `AUTH-GITHUB-DOCS-BEST-PRACTICES` for GitHub documentation and
repository prose, and `AUTH-MICROSOFT-LISTS` for lists in Microsoft documentation. Keep these sources
subordinate to a selected product, publisher, contract, or technical standard.

## Literal formatting

A *code span* is an inline sequence of code-formatted characters in CommonMark.

- Use a code span for exact text that a reader must type, match, or identify as code. This text includes commands, flags, paths, filenames, identifiers, environment variables, and literal values. Keep product names and ordinary technical terms in plain text.
- Keep prose punctuation outside a code span. Include punctuation in the span only when it belongs
  to the exact literal.
- When writing about a particular programming language, follow the capitalisation style of that
  language.
- Follow the programming language or project's convention for punctuation spacing. Otherwise,
  choose one spacing pattern and use it consistently.
- When instructions describe a casing style, explain what the requirements are and provide an
  example. Do not use a casing style name alone.
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
- Refer generically to a file type by its formal name, not by a filename extension. Do not use a
  file type as a verb.
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
  alternative. Do not use *dummy*. Give every placeholder a descriptive name that cannot be mistaken
  for literal input. Explain how to replace it when the surrounding task does not make that clear.
  Explain an exact project or API placeholder outside the literal.

## Storage terminology

- Use *disc* for optical media. Use *disk* for magnetic media or an established broader storage
  term. Use *drive* for a device that stores or accesses data. Prefer *storage device* when the
  medium or mechanism does not matter.
- Use *available* for memory or storage capacity that a system can use. Use *free* for price or
  unrestricted use.
- Do not use *memory* as a generic synonym for storage capacity. Name the exact measured resource,
  such as system memory, virtual memory, persistent memory, or storage space. Let the governing
  architecture or platform define the boundary.

## Dates in technical prose

- Write calendar dates as `YYYY-MM-DD` in structured data, such as CSV, JSON, YAML, or metadata fields, and in filenames, code, and other machine-readable values. In prose, including technical documentation, use the [house date form](number-forms.md#dates-and-times); it keeps Google's spelled-out month but replaces Google's month-day-year order and commas. Apply the recipient's required date form in a paper, report, memo, thesis chapter, or other style-governed document.

## Example addresses and numbers

- Use only illustrative values from the resources below.
  Keep live credentials, secrets, personal data, routable addresses, and registrable domains out
  of examples.
- Retain a non-secret public value only when the exact value is evidence. Identify its evidential role.

| Resource | Use |
| --- | --- |
| Domain name | [IANA example domains](https://www.iana.org/help/example-domains) |
| IPv4 address | The three documentation address blocks in [RFC 5737](https://doi.org/10.17487/RFC5737) |
| IPv6 address | The documentation prefixes `2001:db8::/32` from [RFC 3849](https://doi.org/10.17487/RFC3849) and `3fff::/20` from [RFC 9637](https://doi.org/10.17487/RFC9637) |
| 48-bit MAC address | The documentation values in section 2.1.4 of [RFC 9542](https://doi.org/10.17487/RFC9542): `00-00-5E-00-53-00` to `00-00-5E-00-53-FF` for unicast and `01-00-5E-90-10-00` to `01-00-5E-90-10-FF` for multicast. Write them with the separator and letter case that the platform uses |
| North American 555 line number | The NANPA [fictitious, non-working numbers from `555-0100` through `555-0199`](https://www.nanpa.com/numbering/555-line-numbers) |

Do not use these resources for production traffic. Do not treat a vendor-owned registrable domain
as reserved because a style guide uses it.

## Command-line syntax

- State what a command does before explaining its individual elements. Name a command-line element
  only when that name helps the reader use or understand the command.
- In reference prose, state which arguments are optional or mutually exclusive and which arguments can repeat.
  Explain what the displayed notation means. Preserve the actual syntax and apply the host or
  project's notation rules.
- Identify the shell or command interface when a command prompt or command-continuation character
  depends on it. Distinguish the command prompt, input, and output. Identify a change of context, such as
  movement from a local shell to a remote shell.
- Show command output only when the reader must copy a value, verify a result, or understand behaviour.
- For command-line and other code examples, introduce output with *The output is the following:*
  or *The output is similar to the following:* as applicable.
- When an operating-system signal is the subject, preserve its exact signal name and defined
  process-control meaning. Do not replace a defined signal action with a generic synonym that
  changes the behaviour.

## API documentation

For an English public API reference contract, apply [API-reference prose](api-reference-prose.md).
The following rules cover internal API references and public API references in other languages:

- Treat method API documentation as the contract for how the code must behave.
- Document each argument, return value, restriction, exception, and error.
- Test the documented behaviour when practical.
- Give each class or module a brief overview and short examples when they help.
- Put the simplest use case first.

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
  responding* or name its measured state. Use *freeze* only for pointer behaviour. Do not use
  *hang* for a computer's behaviour after a system error. Explain any retained *bug*, *corrupted*,
  or *crash* wording.
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
- Do not make an unqualified performance, cost, reliability, compatibility, security, compliance,
  or superlative claim.
  State the measured property, test conditions, version or date, and result. When evidence supports
  only design intent, use *designed to* or *intended to* and do not imply a guarantee.

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
- Avoid documenting future features or products. Do not pre-announce them unless the owning
  authority has approved publication. Keep unapproved plans out of examples and implications.
- Use present tense for current capability.
- Use *release* to refer to the issuing of software. Do not use it for a software version number.
- Include a version number with software names as needed, such as when describing a new version or
  comparing two versions. Otherwise, the version number can usually be omitted.
- Use *earlier* or *later*, rather than *lower*, *higher*, *newer*, or *older*, when referring to
  software version numbers.

## Code examples

State a rule before its example. Use the example only to illustrate the rule. Keep every requirement and scope boundary outside the example.

State explicitly when readers must not run an example.

- Before a command or instructional code block, state the action or outcome that it implements.
- Indicate omitted code or configuration with a comment in the syntax of its language. Name the
  omitted content.
- Put input and output in separate code blocks.
- Describe each prerequisite and instructional action without a difficulty judgement.
- For replaceable example content, apply [Placeholders](#placeholders).

## Code comments

A *comment* is information added to source code for readers and ignored as code by the compiler or
interpreter.

- Write a code comment only when it adds intent, rationale, a constraint, or an invariant that code
  cannot express clearly.

Complete this branch when each literal remains exact and every command, path, example, comment,
API pointer, and technical claim has the required context, evidence, safe values, and governed
terminology.
