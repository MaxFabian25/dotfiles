# Prose

Prose is a writing skill for AI agents. It helps draft, edit, and review English and Danish prose,
with house preferences and references for technical documentation, interface copy, citations,
terminology, and editorial work.

The house defaults include British English spelling and Danish punctuation without the optional
start comma. Supplied text keeps its dialect, voice, and established mechanics, such as serial-comma
use and quotation marks, unless you ask to change them. Your
instructions and the requirements of your project, publisher, or chosen style take precedence
in the parts they cover.

## Use Prose

Once the skill is installed, give it the text and the result you want. For example:

```text
$prose Edit this explanation for clarity. Preserve the technical terms and quoted text.
```

Other useful requests include drafting Danish interface messages, reviewing a README, or formatting
citations in a specified APA or Chicago style. State the audience, language, and constraints that
matter. When installed as the Prose plugin, the reader skill is named `$prose:prose`.

### Install the local skill

Clone this repository:

```sh
git clone https://github.com/MaxFabian25/dotfiles.git
```

Copy the `prose` and `charts` directories from the cloned `dotfiles/packages/prose/plugin/skills`
into `.agents/skills` in the project where you want to use them. Keep each `references` directory
alongside its `SKILL.md`. The charts skill links to Prose's references, so install both.
Codex loads project skills from `.agents/skills`; see the official
[local skill documentation](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills).

If you already use a marketplace that lists Prose, install its plugin entry instead. The release
payload is the repository's `plugin/` directory. Writing with Prose requires no Python runtime.

## How it works

The [reader skill](plugin/skills/prose/SKILL.md) applies the house choices and follows the
[reference index](plugin/skills/prose/references/index.md) for the requested writing task. It reads
relevant guidance on demand. The references supply writing choices; the agent still performs the
drafting and editing.

The [charts skill](plugin/skills/charts/SKILL.md) adds house rules for chart marks, table and
dashboard layout, and the code that builds a display. Prose supplies the words in and around each
display.

Each rule is maintained in one reference. [Source records](plugin/skills/prose/references/source-manifest.md)
show where the guidance and terms come from and where that guidance applies. Including a source
in the library does not make its rules apply to every passage. Quotations, official names, and
exact technical text keep their required forms.

## Develop Prose

See [CONTRIBUTING.md](CONTRIBUTING.md) for editing, checks, and release instructions. The
`prose-maintainer` skill applies those instructions to repository work. Agents can invoke any of
the three skills implicitly when a task matches its description.

The current tree contains the writing library and its development tools. Dotfiles contains a
fresh source import; it does not import Prose's earlier history or retired experiments.

Report defects or propose changes through [GitHub issues](https://github.com/MaxFabian25/dotfiles/issues).
