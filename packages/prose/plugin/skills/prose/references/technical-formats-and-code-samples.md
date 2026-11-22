# Technical formats, commands, and code samples

Apply this branch alongside [Technical and code-adjacent prose](technical-and-code-prose.md) when
the work includes storage terminology, technical dates, example network or phone values,
command-line syntax, internal API documentation, code examples, or source-code comments. Read only
the sections needed for the current task.

## Storage terminology

- Use *disc* for optical media. Use *disk* for magnetic media or an established broader storage
  term. Use *drive* for a device that stores or accesses data. Prefer *storage device* when the
  medium or mechanism does not matter.
- Use *available* for memory or storage capacity that a system can use. Use *free* for price or
  unrestricted use.
- Name the exact measured resource, such as system memory, virtual memory, persistent memory, or
  storage space, rather than using *memory* as a generic synonym for storage capacity. Let the
  governing architecture or platform define the boundary.

## Dates in technical prose

- Write calendar dates as `YYYY-MM-DD` in structured data, such as CSV, JSON, YAML, or metadata
  fields, and in filenames, code, and other machine-readable values. In prose, including technical
  documentation, use the [house date form](number-forms.md#dates-and-times); it keeps Google's
  spelled-out month but replaces Google's month-day-year order and commas. Apply the recipient's
  required date form in a paper, report, memo, thesis chapter, or other style-governed document.

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

Reserve these resources for documentation examples rather than production traffic, and verify that
any domain in an example is reserved rather than vendor-owned.

## Command-line syntax

- State what a command does before explaining its individual elements. Name a command-line element
  only when that name helps the reader use or understand the command.
- In reference prose, state which arguments are optional or mutually exclusive and which arguments
  can repeat. Explain what the displayed notation means. Preserve the actual syntax and apply the
  host or project's notation rules.
- Identify the shell or command interface when a command prompt or command-continuation character
  depends on it. Distinguish the command prompt, input, and output. Identify a change of context,
  such as movement from a local shell to a remote shell.
- Show command output only when the reader must copy a value, verify a result, or understand
  behaviour.
- For command-line and other code examples, introduce output with *The output is the following:*
  or *The output is similar to the following:* as applicable.
- When an operating-system signal is the subject, preserve its exact signal name and defined
  process-control meaning rather than substituting a generic synonym that changes the behaviour.

## API documentation

For an English public API reference contract, apply [API-reference prose](api-reference-prose.md).
The following rules cover internal API references and public API references in other languages:

- Treat method API documentation as the contract for how the code must behave.
- Document each argument, return value, restriction, exception, and error.
- Test the documented behaviour whenever you can run the code.
- Give each class or module a brief overview and short examples when they help.
- Put the simplest use case first.

## Code examples

State a rule before its example. Use the example only to illustrate the rule. Keep every requirement
and scope boundary outside the example.

State explicitly when readers must not run an example.

- Before a command or instructional code block, state the action or outcome that it implements.
- Indicate omitted code or configuration with a comment in the syntax of its language. Name the
  omitted content.
- Put input and output in separate code blocks.
- Describe each prerequisite and instructional action without a difficulty judgement.
- For replaceable example content, apply [Placeholders](technical-and-code-prose.md#placeholders).

## Code comments

A *comment* is information added to source code for readers and ignored as code by the compiler or
interpreter.

- Write a code comment only when it adds intent, rationale, a constraint, or an invariant that code
  cannot express clearly.

Complete this branch when every storage term, technical date, illustrative address or number,
command-line block, internal API contract, code example, and code comment uses exact syntax, safe
example values, separate input and output blocks, and tested or evidenced behaviour.
