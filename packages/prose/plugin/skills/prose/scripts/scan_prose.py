#!/usr/bin/env python3
"""Find prose patterns worth a second look in HTML, Markdown, or plain text.

Usage:
  scan_prose.py FILE [FILE ...]
  scan_prose.py - < draft.txt
Options:
  --only house    only the English house choices in references/english-core.md
  --only unslop   only the patterns the unslop writing passes remove
  --passive       also flag passive verbs with no "by" actor (noisy)
  --json          print one JSON object per finding

HTML is read block by block (paragraphs, list items, table cells, headings,
captions), so table cells are never joined into one sentence, and code, pre,
script, and style are skipped. In Markdown, fenced and inline code are skipped
and each table cell is its own block.

Every finding is a lead, not a verdict: read the sentence and decide. The
serial-comma check in particular is a heuristic. Exit status 1 means at least
one finding.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

WORD = r"[\w'’-]+"
ITEM = rf"{WORD}(?: {WORD}){{0,2}}"
SERIES = re.compile(
    rf"(?<![\w'’-])({ITEM}), ((?:{ITEM}, )*)({ITEM}) (?:and|or|nor) (?={WORD})"
)
FUNCTION_WORDS = {
    "who",
    "which",
    "that",
    "where",
    "when",
    "whose",
    "whom",
    "although",
    "but",
    "so",
    "because",
    "while",
    "as",
    "if",
    "then",
    "yet",
    "though",
    "unless",
    "since",
    "and",
    "or",
    "such",
    "including",
    "not",
    "to",
    "first",
    "whether",
    "especially",
    "like",
    "for",
    "from",
    "with",
    "without",
    "in",
    "on",
    "at",
    "by",
    "under",
    "about",
    "after",
    "before",
    "per",
    "than",
    "via",
    "into",
    "of",
    "the",
    "a",
    "an",
    "this",
    "these",
    "those",
    "it",
    "we",
    "they",
    "you",
    "he",
    "she",
    "our",
    "their",
    "its",
    "his",
    "her",
    "each",
    "every",
    "all",
    "both",
    "either",
}
OPENERS = {
    "however",
    "then",
    "also",
    "first",
    "second",
    "finally",
    "yes",
    "no",
    "so",
    "today",
    "here",
    "instead",
    "otherwise",
    "still",
    "meanwhile",
    "now",
    "next",
    "again",
    "indeed",
    "thus",
    "if",
    "when",
    "because",
    "although",
    "though",
    "while",
    "after",
    "before",
    "once",
    "since",
    "unless",
    "until",
    "as",
    "whether",
    "where",
    "in",
    "on",
    "at",
    "for",
    "by",
    "with",
    "from",
    "under",
    "over",
    "during",
    "without",
    "to",
    "of",
    "for example",
    "on either",
}
BLOCK_TAGS = {
    "p",
    "li",
    "td",
    "th",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "caption",
    "dt",
    "dd",
    "figcaption",
    "blockquote",
    "summary",
    "title",
    "div",
    "section",
    "article",
    "main",
    "header",
    "footer",
    "aside",
    "nav",
    "ul",
    "ol",
    "dl",
    "table",
    "tr",
    "thead",
    "tbody",
    "figure",
    "details",
    "label",
    "button",
    "option",
}
SKIP_TAGS = {
    "script",
    "style",
    "code",
    "pre",
    "svg",
    "template",
    "noscript",
    "kbd",
    "samp",
    "head",
}
KIND_OF_TAG = {
    "td": "cell",
    "th": "cell",
    "li": "item",
    "dt": "item",
    "dd": "item",
    "caption": "caption",
    "figcaption": "caption",
    "summary": "heading",
}
KIND_OF_TAG.update({f"h{n}": "heading" for n in range(1, 7)})


@dataclass
class Block:
    text: str
    kind: str
    line: int


@dataclass
class Check:
    group: str
    name: str
    pattern: re.Pattern[str]
    message: str
    kinds: frozenset[str] = frozenset({"para", "item", "cell", "caption", "heading"})


def words(*items: str) -> str:
    return r"\b(?:" + "|".join(items) + r")\b"


IGNORE = re.IGNORECASE
CHECKS = [
    Check(
        "house",
        "ellipsis",
        re.compile(r"(?<!\.)\.\.\.(?!\.)"),
        "use the ellipsis character (…) for an authored ellipsis",
    ),
    Check(
        "house",
        "and-or",
        re.compile(r"\band/or\b", IGNORE),
        "write and, or, or 'or both'",
    ),
    Check(
        "house",
        "etc",
        re.compile(r"\betc\b\.?|\band so on\b", IGNORE),
        "introduce an open list with 'for example' or 'such as' instead",
    ),
    Check("house", "amongst", re.compile(r"\bamongst\b", IGNORE), "write 'among'"),
    Check(
        "house",
        "connective",
        re.compile(r"(?:^|[.!?] )(?:Moreover|Furthermore|In addition),"),
        "remove the needless connective",
    ),
    Check(
        "house",
        "empty-phrase",
        re.compile(
            r"\bunderscor\w* the (?:significance|importance)\b|\bplays? an? "
            r"(?:vital|crucial|key|pivotal|central|important) role\b",
            IGNORE,
        ),
        "state the supported fact instead",
    ),
    Check(
        "house",
        "leverage",
        re.compile(r"\bleverag(?:e|es|ed|ing)\b", IGNORE),
        "outside finance, use an exact verb",
    ),
    Check(
        "house",
        "impact-verb",
        re.compile(
            r"\b(?:impacts|impacted|impacting)\b|\b(?:to|will|can|may|could|might|would"
            r"|does|did|not|that) impact\b",
            IGNORE,
        ),
        "use impact only as a noun; use affect, change, increase, or reduce",
    ),
    Check(
        "house",
        "functionality",
        re.compile(r"\bfunctionality\b", IGNORE),
        "name the feature, capability, or operation",
    ),
    Check(
        "house",
        "compared-to",
        re.compile(r"\bcompared to\b|\bin comparison to\b", IGNORE),
        "use 'compared with' unless this is an analogy",
    ),
    Check(
        "house",
        "different-than",
        re.compile(r"\bdifferent than\b", IGNORE),
        "prefer 'different from'",
    ),
    Check(
        "house",
        "former-latter",
        re.compile(r"\bthe (?:former|latter)\b", IGNORE),
        "repeat the noun or use a descriptive label",
    ),
    Check(
        "house",
        "comprised-of",
        re.compile(r"\bcomprised of\b", IGNORE),
        "state the relation with an exact verb, such as 'consists of'",
    ),
    Check(
        "unslop",
        "em-dash",
        re.compile(r"—|\s–\s|\s--\s"),
        "replace the dash with a full stop or comma",
    ),
    Check(
        "unslop",
        "vocabulary",
        re.compile(
            words(
                "delve",
                "delves",
                "delving",
                "crucial",
                "pivotal",
                "utili[sz]e",
                "utili[sz]es",
                "utili[sz]ed",
                "utili[sz]ing",
                "facilitates?",
                "facilitated",
                "foster",
                "fosters",
                "fostering",
                "enhances?",
                "enhanced",
                "showcases?",
                "showcased",
                "showcasing",
                "underscores?",
                "underscored",
                "tapestry",
                "testament",
                "garner",
                "garnered",
                "additionally",
                "numerous",
                "intricate",
                "vibrant",
                "seamless",
                "seamlessly",
                "robust",
                "groundbreaking",
                "nestled",
            ),
            IGNORE,
        ),
        "prefer the plain word, or name the consequence",
    ),
    Check(
        "unslop",
        "filler",
        re.compile(
            r"\bin order to\b|\bdue to the fact that\b|\bin the event that\b|"
            r"\bit is (?:important|worth) (?:to note|noting) that\b|"
            r"\bit'?s worth noting\b",
            IGNORE,
        ),
        "cut the filler",
    ),
    Check(
        "unslop",
        "fancy-is",
        re.compile(
            words("serves as", "serve as", "stands as", "boasts", "acts as a"), IGNORE
        ),
        "say 'is' or 'has'",
    ),
    Check(
        "unslop",
        "not-just",
        re.compile(
            r"\bnot (?:just|only|merely) [^.;]{1,80}?\bbut\b|\bit'?s not [^.;]{1,50}, "
            r"it'?s\b",
            IGNORE,
        ),
        "state the point directly",
    ),
    Check(
        "unslop",
        "trailing-ing",
        re.compile(
            r", (?:highlighting|ensuring|reflecting|showcasing|fostering|underscoring|"
            r"emphasi[sz]ing|demonstrating|cementing|solidifying) ",
            IGNORE,
        ),
        "give the conclusion its own sentence with evidence, or cut it",
    ),
    Check(
        "unslop",
        "vague-attribution",
        re.compile(
            r"\b(?:experts|studies|research|industry reports|observers|critics|many "
            r"people) (?:believe|suggest|say|show|argue|agree)\b",
            IGNORE,
        ),
        "name the source or cut the claim",
    ),
    Check(
        "unslop",
        "hedge-stack",
        re.compile(
            r"\b(?:could|might|may|can) (?:potentially|possibly|perhaps|conceivably)\b",
            IGNORE,
        ),
        "hedge once",
    ),
    Check(
        "unslop",
        "chatbot",
        re.compile(
            r"\bI hope this helps\b|\blet me know if\b|\bgreat question\b|"
            r"\byou'?re absolutely right\b|^certainly!",
            IGNORE,
        ),
        "remove the conversational leftover",
    ),
    Check(
        "unslop",
        "generic-ending",
        re.compile(
            r"\bthe future looks bright\b|\bonly time will tell\b|"
            r"\bexciting times ahead\b",
            IGNORE,
        ),
        "end on a specific fact or plan",
    ),
    Check(
        "unslop",
        "mid-sentence-colon",
        re.compile(
            rf"(?:^|[.!?] )(?:{WORD} ){{4,}}{WORD}: [a-z]{WORD}(?: {WORD}){{4,}}"
        ),
        "use a colon only before a list or example",
        frozenset({"para", "item"}),
    ),
    Check(
        "unslop",
        "emoji",
        re.compile("[\U0001f300-\U0001faff☀-➿⭐✅❌]"),
        "express the state in words",
    ),
]
PASSIVE = Check(
    "unslop",
    "passive",
    re.compile(
        r"\b(?:is|are|was|were|be|been|being)\s+(?:\w+ly\s+)?(?:\w+ed|built|done|"
        r"given|made|kept|known|seen|shown|taken|written|sent|held|found|told)\b"
        r"(?![^.;]{0,40}\bby\b)",
        IGNORE,
    ),
    "name the actor if it matters",
    frozenset({"para", "item"}),
)


class HtmlBlocks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.buffer: list[str] = []
        self.kinds: list[str] = []
        self.skip = 0
        self.start_line = 1

    def flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self.buffer)).strip()
        if text:
            kind = self.kinds[-1] if self.kinds else "para"
            self.blocks.append(Block(text, kind, self.start_line))
        self.buffer = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self.skip += 1
            if tag in ("code", "kbd", "samp") and not self.skip - 1:
                self.buffer.append(" code ")
            return
        if tag == "br":
            self.buffer.append(" ")
        if tag in BLOCK_TAGS:
            self.flush()
            self.kinds.append(KIND_OF_TAG.get(tag, "para"))
            self.start_line = self.getpos()[0]

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if tag in BLOCK_TAGS:
            self.flush()
            if self.kinds:
                self.kinds.pop()
            self.start_line = self.getpos()[0]

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if not self.buffer or not "".join(self.buffer).strip():
            self.start_line = self.getpos()[0]
        self.buffer.append(data)


def html_blocks(source: str) -> list[Block]:
    parser = HtmlBlocks()
    parser.feed(source)
    parser.close()
    parser.flush()
    return parser.blocks


def text_blocks(source: str) -> list[Block]:
    blocks: list[Block] = []
    lines: list[str] = []
    kind, start, fenced = "para", 1, False

    def flush() -> None:
        nonlocal lines
        text = " ".join(lines).strip()
        if text:
            blocks.append(Block(text, kind, start))
        lines = []

    for number, raw in enumerate(source.splitlines(), 1):
        stripped = raw.strip()
        if stripped.startswith(("```", "~~~")):
            flush()
            fenced = not fenced
            continue
        if fenced:
            continue
        line = re.sub(r"`[^`]*`", "code", raw)
        line = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"https?://\S+", "link", line).rstrip()
        if not line.strip():
            flush()
            continue
        if re.match(r"\s*#{1,6}\s", line):
            flush()
            kind, start = "heading", number
            lines = [line.strip().lstrip("#").strip()]
            flush()
            kind = "para"
            continue
        if line.lstrip().startswith("|"):
            flush()
            for cell in line.strip().strip("|").split("|"):
                if cell.strip() and not re.fullmatch(r"\s*:?-{2,}:?\s*", cell):
                    blocks.append(Block(cell.strip(), "cell", number))
            continue
        if re.match(r"\s*(?:[-*+]|\d+[.)])\s", line):
            flush()
            kind, start = "item", number
            lines = [re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line)]
            continue
        if not lines:
            kind, start = (
                ("item" if kind == "item" and raw[:1].isspace() else "para"),
                number,
            )
        lines.append(line.strip())
    flush()
    return blocks


def serial_comma(block: Block) -> list[tuple[int, str]]:
    found = []
    for match in SERIES.finditer(block.text):
        first, middle, last = match.groups()
        later = [m for m in middle.split(", ") if m] + [last]
        if any(item.split()[0].lower() in FUNCTION_WORDS for item in later):
            continue
        sentence = re.split(r"(?<=[.!?;:])\s+", block.text[: match.start()])[-1]
        opening = (sentence + first).lstrip("(“\"'").lower()
        if "," not in sentence and any(re.match(rf"{o}\b", opening) for o in OPENERS):
            continue
        found.append((match.start(), match.group(0).rstrip()))
    return found


def title_case(block: Block) -> bool:
    tokens = re.findall(r"[A-Za-z][\w'’-]*", block.text)
    long = [t for t in tokens[1:] if len(t) >= 3]
    capitalised = [t for t in long if t[0].isupper() and not t.isupper()]
    return len(long) >= 4 and len(capitalised) / len(long) > 0.6


def snippet(text: str, start: int, match: str) -> str:
    left = max(0, start - 30)
    right = min(len(text), start + len(match) + 30)
    return ("…" if left else "") + text[left:right] + ("…" if right < len(text) else "")


def scan(
    blocks: list[Block], groups: set[str], passive: bool
) -> list[dict[str, object]]:
    findings = []
    checks = [c for c in CHECKS if c.group in groups]
    if passive and "unslop" in groups:
        checks.append(PASSIVE)

    def add(
        block: Block, group: str, name: str, message: str, start: int, match: str
    ) -> None:
        findings.append(
            {
                "line": block.line,
                "kind": block.kind,
                "group": group,
                "check": name,
                "message": message,
                "text": snippet(block.text, start, match),
            }
        )

    for block in blocks:
        for check in checks:
            if block.kind not in check.kinds:
                continue
            for match in check.pattern.finditer(block.text):
                add(
                    block,
                    check.group,
                    check.name,
                    check.message,
                    match.start(),
                    match.group(0),
                )
        if "house" in groups and block.kind in ("para", "item", "cell", "caption"):
            for start, text in serial_comma(block):
                add(
                    block,
                    "house",
                    "serial-comma",
                    "check for a missing serial comma before the last item",
                    start,
                    text,
                )
        if "unslop" in groups and block.kind == "heading" and title_case(block):
            add(block, "unslop", "title-case", "use sentence case", 0, block.text)
    return findings


def read_blocks(path: str) -> list[Block]:
    source = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    looks_html = path.lower().endswith((".html", ".htm", ".xhtml")) or (
        path == "-"
        and re.search(r"<(?:p|div|li|td|h[1-6]|body|html)\b", source, IGNORE)
    )
    return html_blocks(source) if looks_html else text_blocks(source)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Find prose patterns worth a second look."
    )
    parser.add_argument("files", nargs="+")
    parser.add_argument("--only", choices=("house", "unslop"))
    parser.add_argument("--passive", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    groups = {args.only} if args.only else {"house", "unslop"}
    total = 0
    for path in args.files:
        for finding in scan(read_blocks(path), groups, args.passive):
            total += 1
            if args.json:
                print(json.dumps({"file": path, **finding}, ensure_ascii=False))
            else:
                print(
                    f"{path}:{finding['line']}: {finding['group']}/{finding['check']} "
                    f"({finding['kind']}): {finding['message']}: {finding['text']}"
                )
    if not args.json:
        print(f"{total} finding{'s' if total != 1 else ''}", file=sys.stderr)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
