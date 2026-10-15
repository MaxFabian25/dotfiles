# API-reference prose

Apply this branch to English public API reference documentation. Use it for public types,
methods, members, parameters, return values, exceptions, and deprecations.

Do not select it for tutorials, how-to guides, or ordinary explanatory prose.
Exclude comments that do not publish a public API contract.

Host reference templates, markup, generated signatures, and language-specific conventions control
their exact forms.

This branch independently adapts [API reference code
comments](https://developers.google.com/style/api-reference-comments) and [verb forms in reference
documentation](https://developers.google.com/style/reference-verbs). These sources govern public
API reference prose, not program behaviour or generated markup.

## Documentation basics

- Treat a method description as the contract for its documented behaviour.
- Document every public programming element that the task includes.
- Use the host's article-title form to identify the exact programming element and its element type.
  When names collide, add the parent element, product, or technology name to the title.
- Include declaration or usage syntax for each supported language covered by the host.
- Give each programming element a unique first sentence that states its purpose, action, or
  represented value.
- Add information that readers cannot deduce from the programming element's name and signature.
- Do not repeat the programming element's name or begin with a generic description of its element
  type.
- Do not put a full stop before the actual end of the first sentence.
  Some document generators end the short description at the first full stop.
- Put setup, history, extended guidance, and implementation details after the first sentence.

## Types, methods, and members

- Start a type description with its purpose or function.
- Follow with its use, key features, prerequisites, best practices, and material pitfalls.
- For a class or module, give a brief overview and put the simplest use case first.
- Start a method description with the action that the method performs.
- Describe prerequisites, dependencies, related APIs, and material exceptions. Explain how the
  method behaves when a dependency is missing.
- Keep a member description brief and link the methods that use it when links are available.
- For each property or field, describe its value and default. Ensure its signature or description
  identifies the data type.
- State applicable language and platform requirements. Name every required security permission.
- Do not phrase a programming element's description as an imperative task instruction.

## Parameters, return values, and exceptions

- Describe every public parameter's purpose and accepted values. Ensure its signature or
  description identifies the data type.
- State whether each parameter is required or optional. Identify input and output parameters when
  that distinction applies.
- If a Boolean parameter controls an action, state what the API does when it is `true` and when it
  is `false`.
- If a Boolean parameter declares an existing state, state the condition that makes it true and say
  that it is false otherwise.
- Explain behaviour across each accepted value or range. State the default afterwards.
- Preserve exact parameter names and literal values.
- Describe each return value briefly. Ensure the signature or description identifies its data
  type.
- Describe every public exception and the condition that causes it.
- Document each restriction and error condition.
- Use the reference generator's exception form when it supplies words such as *Throws*.

## Verb forms

- Use present-tense third-person verbs in the main method description.
- Describe what the method does, not what a developer might use it to do.

## Deprecations

- Put deprecation status and the most important replacement advice in the first sentence.
- When the API tracks versions, name the version in which the programming element was first
  deprecated.
- Name the supported replacement when one exists.
- State what readers must do to keep existing code working.
- Explain the reason in later sentences when it helps the reader.

## Formatting and code samples

- Format API names and literal values under [Literal formatting](technical-and-code-prose.md#literal-formatting).
  For replaceable example content, apply [Placeholders](technical-and-code-prose.md#placeholders).
- Match each documented name's capitalisation and spacing to its source definition.
- Link each API name to its reference page when the host supports links.
- Add a short code sample when the host reference format supports one.
- Place a code sample near the top of each page for a class, interface, or similar API member.
- Introduce each code sample with the behaviour or task that it demonstrates.
- Use code samples for common, difficult, or easily misunderstood behaviour. Omit contrived
  demonstrations of obvious syntax.
- Keep a code sample that readers can run self-contained, or link to its complete source.
- State each requirement and dependency before a code sample that readers can run.
- Show expected output when the code sample has an observable result.
- Compile or otherwise validate every code sample. Run it in each available documented environment.

## Automatically generated reference documentation

- Review automatically generated reference documentation and document comments for missing reader
  information. Add the missing information and remove unsuitable implementation or internal
  details.

## Verification

Test documented behaviour when practical.

Complete this branch when every public element in scope documents its contract, parameters,
returns, errors, limits, and deprecation state. Examples match tested behaviour.
