"""Find where a skill's Markdown copies the help text of a script the skill bundles.

A script's help is its own source of truth, and the skill that copies it pays for the copy on every load and drifts
from the script. For each folder that holds a SKILL.md and a scripts/ folder, copies() reads each script's help
without running it, because some scripts act on accounts and browsers: a Python file's module docstring and the
strings it passes as description=, epilog=, usage= or help=, and the leading comment block of a JavaScript, shell or
PowerShell file. It lowercases the help, the SKILL.md and each file under references/ to words, leaves out the
Markdown's code, where a command line a step must show sits, and finds the runs of RUN_WORDS or more words in a row
that a file shares with one script's help. A file copies a script when those runs hold COPIED_WORDS words or more.

It uses only the standard library, because the private repository's test loads it by path.
"""
import ast
from dataclasses import dataclass
import re

# Measured on 14 November 2026 over the public and private skills. The copies that day's writing review found are
# paraphrases whose longest shared run is 8 to 11 words, while command lines in code reach 13 words and a sentence
# such as "exit 2 means the profile is signed out", which four chat skills share with their scripts, is 8, so no
# run length alone tells them apart. Summed over runs of 6 words outside code, the review's largest copies share
# 41 to 49 words with one script's help, and the files just under 40 share 33 to 35 words of a description's
# phrases and an exit code.
RUN_WORDS = 6
COPIED_WORDS = 40
HELP_KEYWORDS = {'description', 'epilog', 'usage', 'help'}
WORD = re.compile(r'\w+')
CODE = re.compile(r'```.*?```|`[^`\n]*`', re.DOTALL)
# Folders a skill's own tools fill with other projects' files.
INSTALLED = {'node_modules', '.venv', '__pycache__'}
LINE_COMMENT = {'.mjs': '//', '.js': '//', '.cjs': '//', '.sh': '#', '.ps1': '#'}
BLOCK_COMMENT = {'.mjs': ('/*', '*/'), '.js': ('/*', '*/'), '.cjs': ('/*', '*/'), '.ps1': ('<#', '#>')}


@dataclass(frozen=True)
class Copy:
    skill: str
    script: str
    doc: str
    runs: tuple

    @property
    def key(self):
        return self.skill, self.script, self.doc

    @property
    def words(self):
        return sum(len(run.split()) for run in self.runs)


def python_help(source):
    """The module docstring and the literal parts of every string passed by a help keyword, as to argparse."""
    tree = ast.parse(source)
    parts = [ast.get_docstring(tree) or '']
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in HELP_KEYWORDS:
                    parts.extend(literal.value for literal in ast.walk(keyword.value)
                                 if isinstance(literal, ast.Constant) and isinstance(literal.value, str))
    return parts


def leading_comment(source, suffix):
    """The comment lines or the comment block at the top of a file, after its #! line."""
    lines = source.splitlines()[1:] if source.startswith('#!') else source.splitlines()
    text = '\n'.join(lines).lstrip()
    if suffix in BLOCK_COMMENT and text.startswith(BLOCK_COMMENT[suffix][0]):
        opening, closing = BLOCK_COMMENT[suffix]
        return [text[len(opening):].split(closing, 1)[0]]
    block = []
    for line in text.splitlines():
        if not line.lstrip().startswith(LINE_COMMENT[suffix]):
            break
        block.append(line.lstrip()[len(LINE_COMMENT[suffix]):])
    return ['\n'.join(block)]


def script_help(path):
    """The parts of one script's help, or none for a file of another kind or one Python cannot parse."""
    source = path.read_text(encoding='utf-8-sig', errors='replace')
    if path.suffix == '.py':
        try:
            return python_help(source)
        except SyntaxError:
            return []
    return leading_comment(source, path.suffix) if path.suffix in LINE_COMMENT else []


def shared_runs(help_parts, doc_text):
    """Each longest run of RUN_WORDS or more words of doc_text's prose that one help part holds in the same order."""
    shingles = set()
    for part in help_parts:
        words = WORD.findall(part.lower())
        shingles.update(tuple(words[index:index + RUN_WORDS]) for index in range(len(words) - RUN_WORDS + 1))
    found = []
    for prose in CODE.split(doc_text):
        words = WORD.findall(prose.lower())
        start = None
        # One step past the last shingle, so a run that reaches the end of the prose is closed too.
        for index in range(len(words) - RUN_WORDS + 2):
            shared = tuple(words[index:index + RUN_WORDS]) in shingles if index <= len(words) - RUN_WORDS else False
            if shared and start is None:
                start = index
            elif not shared and start is not None:
                found.append(' '.join(words[start:index - 1 + RUN_WORDS]))
                start = None
    return found


def owned(folder, pattern):
    return sorted(path for path in folder.rglob(pattern)
                  if path.is_file() and not INSTALLED.intersection(path.relative_to(folder).parts))


def copies(skills):
    """Each file of a skill under the folder skills that copies one of the skill's scripts, in a stable order. A
    skill is named by its path from the folder that holds skills, such as skills/excel-com-wsl."""
    found = []
    for skill_md in owned(skills, 'SKILL.md'):
        folder = skill_md.parent
        if not (folder / 'scripts').is_dir():
            continue
        docs = [skill_md, *(owned(folder / 'references', '*.md') if (folder / 'references').is_dir() else [])]
        for script in owned(folder / 'scripts', '*'):
            parts = script_help(script)
            for doc in docs:
                copy = Copy(folder.relative_to(skills.parent).as_posix(), script.relative_to(folder).as_posix(),
                            doc.relative_to(folder).as_posix(), tuple(shared_runs(parts, doc.read_text(encoding='utf-8-sig'))))
                if copy.words >= COPIED_WORDS:
                    found.append(copy)
    return found


def describe(copy):
    quoted = '; '.join(f'"{run}"' for run in copy.runs)
    return (f'{copy.skill}/{copy.doc} repeats {copy.words} words of the help of {copy.skill}/{copy.script}: {quoted}. '
            "Point at the script's --help and keep only what the help does not say; move a fact the help lacks "
            'into the help.')
