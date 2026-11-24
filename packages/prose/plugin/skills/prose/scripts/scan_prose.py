#!/usr/bin/env python3
"""Find prose patterns worth a second look in HTML, Markdown, or plain text.

Usage:
  scan_prose.py FILE [FILE ...]
  scan_prose.py - < draft.txt
Options:
  --only house    only house choices (English by default, or Danish with --lang da)
  --only unslop   only filler, cliché, and AI-style punctuation checks
  --lang {en,da}  prose language (default: en, overriding .prose.toml)
  --us-english    skip British-only spelling and day-month-year date checks
  --british-english
                  enforce British spelling and date checks, overriding .prose.toml
  --passive       also flag passive verbs with no "by" actor (noisy, English only)
  --serial-comma  force the serial-comma check on, overriding .prose.toml
  --no-serial-comma
                  skip the serial-comma check, for text whose style omits it
  --json          print one JSON object per finding

Optional `.prose.toml` config (discovered by walking up from each file or cwd
for `-`, stopping at the repository root containing `.git`):
  serial_comma = false   skip the serial-comma check unless --serial-comma is set
  us_english = true      skip British-only spelling and date checks unless --british-english is set
  lang = "da"            use Danish checks unless --lang is set

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

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None

CONFIG_NAME = ".prose.toml"
CONFIG_KEYS = frozenset({"serial_comma", "us_english", "lang"})
VALID_LANGS = frozenset({"en", "da"})

WORD = r"[\w'’-]+"
ITEM = rf"{WORD}(?: {WORD}){{0,2}}"
SERIES = re.compile(
    rf"(?<![\w'’-])({ITEM}), ((?:{ITEM}, )*)({ITEM}) (?:and|or|nor) (?=({WORD}))"
)
# Words that name a state or version of the noun before them, as in "the data,
# current and target", an appositive pair rather than the end of a series.
STATES = {
    "current",
    "target",
    "future",
    "past",
    "present",
    "previous",
    "planned",
    "actual",
    "proposed",
    "existing",
    "old",
    "new",
    "original",
    "revised",
    "expected",
    "observed",
}
# A preposition that ends a longer last item and one after the conjunction form a
# pair that shares its object, as in "a task, written with and without that rule".
PREPOSITIONS = {
    "about",
    "above",
    "across",
    "after",
    "against",
    "around",
    "at",
    "before",
    "below",
    "between",
    "by",
    "down",
    "during",
    "for",
    "from",
    "in",
    "inside",
    "into",
    "of",
    "off",
    "on",
    "out",
    "outside",
    "over",
    "through",
    "to",
    "under",
    "up",
    "with",
    "within",
    "without",
}
CLAUSE_END = r"(?=[,.;:!?)]|$)"
# The rest of a list whose outer series already has a serial comma, as in
# "SharePoint and OneDrive, and Power BI" or "citations and references,
# tables and figures, or a stated combination": the matched conjunction joins
# a paired item inside the outer list rather than ending a list without a comma.
LIST_TAIL = re.compile(rf"{ITEM}(?:, [^.;:!?]+)*, (?:and|or|nor) ({WORD})")
# Words after ", and" that typically open a new clause rather than the final
# noun-phrase item of an outer list, as in "apples, pears and plums, and then we left".
CLAUSE_STARTERS = {
    "then",
    "we",
    "they",
    "he",
    "she",
    "it",
    "i",
    "you",
    "there",
    "so",
    "yet",
    "still",
    "also",
    "now",
    "when",
    "if",
    "because",
    "although",
    "though",
    "while",
    "where",
    "unless",
    "until",
    "since",
    "that",
    "which",
    "who",
    "not",
}
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
    langs: frozenset[str] = frozenset({"en"})
    skip_if_us: bool = False


def words(*items: str) -> str:
    return r"\b(?:" + "|".join(items) + r")\b"


IGNORE = re.IGNORECASE
MONTHS_EN = (
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)"
)
MONTHS_DA_UPPER = (
    r"(?:Januar|Februar|Marts|April|Maj|Juni|Juli|August|September|Oktober|"
    r"November|December)"
)
MONTHS_DA_LOWER = (
    r"(?:januar|februar|marts|april|maj|juni|juli|august|september|oktober|"
    r"november|december)"
)
DAY = r"(?:[1-9]|[12]\d|3[01])"
US_SPELLING = re.compile(
    words(
        r"(?:analyz|optimiz|organiz|prioritiz|summariz|categoriz|standardiz|"
        r"normaliz|customiz|finaliz|localiz|capitaliz|italiciz|minimiz|maximiz|"
        r"recogniz|realiz|characteriz|emphasiz|stabiliz|synchroniz|authoriz|"
        r"visualiz)(?:e|es|ed|ing|er|ers|ation|ations)",
        r"color(?:s|ed|ing)?",
        r"behavior(?:s|al)?",
        r"favor(?:s|ed|ing|able|ite|ites)?",
        r"honor(?:s|ed|ing|able)?",
        r"labor(?:s|ed|ing)?",
        r"neighbor(?:s|ed|ing|hood|hoods)?",
        r"humor(?:s|ed|ing)?",
        r"flavor(?:s|ed|ing)?",
        r"center(?:s|ed|ing)?",
        r"fibers?",
        r"liters?",
        r"theaters?",
        r"gray",
        r"defense",
        r"offense",
        r"pretense",
        r"cancel(?:ed|ing)",
        r"label(?:ed|ing)",
        r"model(?:ed|ing)",
        r"travel(?:ed|ing|ers?)",
        r"fulfill(?:s|ment)?",
        r"enroll(?:s|ment)?",
        r"installments?",
        r"skillful",
        r"willful",
        r"artifacts?",
        r"catalog(?:s|ed|ing)?",
    ),
    IGNORE,
)
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
        "house",
        "us-spelling",
        US_SPELLING,
        "use British spelling in new English prose (or pass --us-english when keeping US English)",
        skip_if_us=True,
    ),
    Check(
        "house",
        "ordinal-date",
        re.compile(
            rf"\b(?:{DAY}(?:st|nd|rd|th)\s+(?:of\s+)?{MONTHS_EN}|"
            rf"{MONTHS_EN}\s+{DAY}(?:st|nd|rd|th))\b"
        ),
        "write an English date without ordinal endings, such as '13 April 2026'",
    ),
    Check(
        "house",
        "us-date",
        re.compile(rf"\b{MONTHS_EN}\s+{DAY},\s+\d{{4}}\b"),
        "write an English date as '13 April 2026' (or pass --us-english when keeping US English)",
        skip_if_us=True,
    ),
    Check(
        "house",
        "twelve-clock",
        re.compile(
            r"(?<!:)\b12(?::00\s*(?:[ap]\.?m\.?|[AP]\.?M\.?)|"
            r"\s*(?:[ap]\.m\.|[AP]\.M\.|[AP]M\b)|(?:[ap]m\b))"
        ),
        "write 'noon' instead of '12 p.m.', or give an unambiguous date and time instead of '12 a.m.'",
    ),
    Check(
        "house",
        "english-percent-space",
        re.compile(r"(?<!\w)\d+(?:\.\d+)?\s+%(?!\w)"),
        "close '%' up to the numeral in English prose, as in '25%'",
    ),
    Check(
        "house",
        "hard-copy",
        re.compile(r"\bhard[- ]cop(?:y|ies)\b", IGNORE),
        "name the exact medium, such as 'printed copy', unless preserving a governed term",
    ),
    Check(
        "house",
        "action-noun",
        re.compile(
            r"\b(?:perform(?:s|ed|ing)?\s+(?:a|an)\s+(?:restart|restore|analysis|"
            r"review|check|audit|backup|update|upgrade|migration|comparison|"
            r"calculation|evaluation|verification|validation|installation|search|"
            r"scan|reset|rollback)|"
            r"conduct(?:s|ed|ing)?\s+(?:a|an)\s+(?:analysis|review|audit|"
            r"comparison|evaluation|assessment|investigation|inspection|study)|"
            r"(?:make|makes|made|making)\s+(?:a|an)\s+(?:decision|comparison|"
            r"recommendation|selection|modification|adjustment|calculation))\b",
            IGNORE,
        ),
        "prefer the direct verb unless the noun names an established technical concept",
    ),
    Check(
        "house",
        "24x7",
        re.compile(r"\b24x7\b", IGNORE),
        "write '24/7' or '24 hours a day, 7 days a week' instead of '24x7'",
    ),
    Check(
        "house",
        "print-out",
        re.compile(
            r"(?<!\ba )(?<!\bA )(?<!\bthe )(?<!\bThe )\bprint(?:s|ed|ing)?\s+out\b(?!\s+of\b)",
            IGNORE,
        ),
        "write 'print' without 'out'",
    ),
    Check(
        "house",
        "click-on",
        re.compile(
            r"(?<!\ba )(?<!\bA )(?<!\bthe )(?<!\bThe )\bclick(?:s|ed|ing)?\s+on\b",
            IGNORE,
        ),
        "write 'click' directly before its object, without 'on'",
    ),
    Check(
        "house",
        "e-mail",
        re.compile(r"(?<![@\w.-])\be-mail(?:s|ed|ing)?\b(?![\w.+-]*@)", IGNORE),
        "write 'email' as one word",
    ),
    # Inspects Markdown and plain-text blocks where intra-line spaces survive;
    # HTML block normalisation collapses runs of whitespace.
    Check(
        "house",
        "double-space",
        re.compile(r"[.!?]  +(?=[A-Z“‘\"'(])"),
        "use one space after sentence-ending punctuation",
    ),
    # Checks '°C'/'°F' only; bare '300K' is omitted to avoid colliding with '300K' for thousands.
    Check(
        "house",
        "temp-space",
        re.compile(r"(?<!\w)\d+(?:[.,]\d+)?°[CF](?!\w)"),
        "put a space between a temperature value and '°C' or '°F', as in '50 °C'",
    ),
    Check(
        "house",
        "kelvin-degree",
        re.compile(r"(?<!\w)\d+(?:[.,]\d+)?\s*°K(?!\w)"),
        "omit the degree symbol with kelvin, as in '300 K'",
    ),
    Check(
        "house",
        "range-percent",
        re.compile(
            r"(?<![\w%.,])\d+(?:[.,]\d+)?\s*"
            r"(?:[–-]\s*\d+(?:[.,]\d+)?\s*%(?!\d)|"
            r"%\s*[–-]\s*\d+(?:[.,]\d+)?(?![.,]\d|\s*%|\w))"
        ),
        "repeat '%' at both endpoints of a range, as in '25%–30%'",
    ),
    # Checks for a missing space before '%'; HTML block normalisation collapses
    # whitespace and Markdown prose routinely uses an ASCII space.
    Check(
        "house",
        "danish-percent-space",
        re.compile(r"(?<!\w)\d+(?:,\d+)?%(?!\d)"),
        "sæt et fast mellemrum før '%' i dansk prosa, fx '25 %'",
        langs=frozenset({"da"}),
    ),
    Check(
        "house",
        "danish-clock",
        re.compile(r"\b(?:kl\.?|klokken)\s*\d{1,2}:\d{2}\b", IGNORE),
        "brug punktum mellem timer og minutter i danske klokkeslæt, fx 'kl. 16.00'",
        langs=frozenset({"da"}),
    ),
    Check(
        "house",
        "danish-date-month-case",
        re.compile(
            rf"\b(?:(?:den|d\.)\s+{DAY}\.?\s+{MONTHS_DA_UPPER}\b|"
            rf"{DAY}\.?\s+{MONTHS_DA_UPPER}\s+\d{{4}}\b)"
        ),
        "skriv danske månedsnavne med lille begyndelsesbogstav, fx '13. april 2026'",
        langs=frozenset({"da"}),
    ),
    Check(
        "house",
        "danish-date-dot",
        re.compile(rf"\b{DAY}\s+{MONTHS_DA_LOWER}\b"),
        "sæt punktum efter dagen i danske datoer, fx '13. april 2026'",
        langs=frozenset({"da"}),
    ),
    Check(
        "house",
        "danish-empty-opener",
        re.compile(r"\bdet\s+bemærkes,?\s+at\b", IGNORE),
        "erstat 'det bemærkes, at' med selve påstanden medmindre henvisningen har betydning",
        langs=frozenset({"da"}),
    ),
    Check(
        "house",
        "danish-24x7",
        re.compile(r"\b24x7\b", IGNORE),
        "skriv '24/7' eller 'døgnet rundt' i stedet for '24x7'",
        langs=frozenset({"da"}),
    ),
    Check(
        "house",
        "danish-temp-space",
        re.compile(r"(?<!\w)\d+(?:[.,]\d+)?°[CF](?!\w)"),
        "sæt mellemrum mellem tal og '°C' eller '°F', fx '50 °C'",
        langs=frozenset({"da"}),
    ),
    Check(
        "house",
        "danish-kelvin-degree",
        re.compile(r"(?<!\w)\d+(?:[.,]\d+)?\s*°K(?!\w)"),
        "udelad gradtegnet ved kelvin, fx '300 K'",
        langs=frozenset({"da"}),
    ),
    Check(
        "house",
        "danish-range-percent",
        re.compile(
            r"(?<![\w%.,])\d+(?:[.,]\d+)?\s*"
            r"(?:[–-]\s*\d+(?:[.,]\d+)?\s*%(?!\d)|"
            r"%\s*[–-]\s*\d+(?:[.,]\d+)?(?![.,]\d|\s*%|\w))"
        ),
        "gentag '%' ved begge endepunkter i et interval, fx '25 %-30 %'",
        langs=frozenset({"da"}),
    ),
    Check(
        "unslop",
        "danish-em-dash",
        re.compile(r"—|\s--\s"),
        "omskriv tankestregen med punktum, komma, parentes eller kolon",
        langs=frozenset({"da"}),
    ),
    Check(
        "unslop",
        "danish-emoji",
        re.compile(
            r"(?:[\U0001f1e6-\U0001f1ff]{2}|[\U0001f300-\U0001faff\u2600-\u266c\u2670-\u27bf⭐✅❌])"
        ),
        "udtryk tilstanden med ord",
        langs=frozenset({"da"}),
    ),
    Check(
        "unslop",
        "em-dash",
        re.compile(r"—|\s–\s|\s--\s"),
        "recast with a full stop, comma pair, parentheses, or colon",
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
                "to underscore",
                "underscores",
                "underscored",
                "underscoring",
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
        "check whether a full stop fits better, or capitalise after the colon if a complete sentence follows",
        frozenset({"para", "item"}),
    ),
    Check(
        "unslop",
        "emoji",
        re.compile(
            r"(?:[\U0001f1e6-\U0001f1ff]{2}|[\U0001f300-\U0001faff\u2600-\u266c\u2670-\u27bf⭐✅❌])"
        ),
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


def _frontmatter_end(raw_lines: list[str]) -> int:
    """Return the 0-based index of the first line after YAML frontmatter, or 0."""
    if not raw_lines or raw_lines[0].strip() != "---":
        return 0
    has_key = False
    for index in range(1, len(raw_lines)):
        stripped = raw_lines[index].strip()
        if stripped in ("---", "..."):
            return index + 1 if has_key else 0
        if re.match(r"[A-Za-z0-9_-]+\s*:", stripped):
            has_key = True
    return 0


def _strip_emphasis(text: str) -> str:
    text = re.sub(r"\*\*(\S(?:.*?\S)?)\*\*", r"\1", text)
    text = re.sub(r"__(\S(?:.*?\S)?)__", r"\1", text)
    text = re.sub(r"(?<!\*)\*(\S(?:[^*]*?\S)?)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<![\w_])_(\S(?:[^_]*?\S)?)_(?![\w_])", r"\1", text)
    return text


def text_blocks(source: str) -> list[Block]:
    blocks: list[Block] = []
    lines: list[str] = []
    kind, start, fenced = "para", 1, False

    def flush() -> None:
        nonlocal lines
        text = _strip_emphasis(" ".join(lines).strip())
        if text:
            blocks.append(Block(text, kind, start))
        lines = []

    raw_lines = source.splitlines()
    body_start = _frontmatter_end(raw_lines)
    for number, raw in enumerate(raw_lines[body_start:], body_start + 1):
        unquoted = re.sub(r"^\s*(?:>\s*)+", "", raw)
        stripped = unquoted.strip()
        if stripped.startswith(("```", "~~~")):
            flush()
            fenced = not fenced
            continue
        if fenced:
            continue
        line = re.sub(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)", "code", unquoted)
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
            row = line.strip()
            if row.startswith("|"):
                row = row[1:]
            if row.endswith("|") and not row.endswith(r"\|"):
                row = row[:-1]
            for raw_cell in re.split(r"(?<!\\)\|", row):
                cell = _strip_emphasis(raw_cell.replace(r"\|", "|").strip())
                if cell and not re.fullmatch(r":?-{2,}:?", cell):
                    blocks.append(Block(cell, "cell", number))
            continue
        if re.match(r"\s*(?:[-*+]|\d+[.)])\s", line) and not re.match(
            rf"\s*{DAY}\.\s+(?:{MONTHS_DA_UPPER}|{MONTHS_DA_LOWER})\b", line
        ):
            flush()
            kind, start = "item", number
            lines = [re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line)]
            continue
        if not lines:
            kind, start = (
                ("item" if kind == "item" and unquoted[:1].isspace() else "para"),
                number,
            )
        lines.append(line.strip())
    flush()
    return blocks


def serial_comma(block: Block) -> list[tuple[int, str]]:
    found = []
    for match in SERIES.finditer(block.text):
        first, middle, last, after = match.groups()
        later = [m for m in middle.split(", ") if m] + [last]
        if any(item.split()[0].lower() in FUNCTION_WORDS for item in later):
            continue
        sentence = re.split(r"(?<=[.!?;:])\s+", block.text[: match.start()])[-1]
        opening = (sentence + first).lstrip("(“\"'").lower()
        if "," not in sentence and any(re.match(rf"{o}\b", opening) for o in OPENERS):
            continue
        *head, end = last.split()
        if head and {end.lower(), after.lower()} <= PREPOSITIONS:
            continue
        rest = block.text[match.end() :]
        pair = re.match(rf"({WORD}){CLAUSE_END}", rest)
        if (
            pair
            and not middle
            and {last.lower(), pair.group(1).lower()} <= STATES
            and first.split()[-1].lower() not in STATES
        ):
            continue
        tail = LIST_TAIL.match(rest)
        if tail and tail.group(1).lower() not in CLAUSE_STARTERS:
            continue
        found.append((match.start(), match.group(0).rstrip()))
    return found


RANGE_UNIT = re.compile(
    r"\b\d+(?:\.\d+)?\s+to\s+\d+(?:\.\d+)?\s+"
    r"(?:kg|g|mg|km|m|cm|mm|ms|kB|MB|GB|TB|KiB|MiB|GiB|TiB|kW|MW|MHz|GHz|°C|°F)(?!\w)"
)
IDENTIFIER_NOUNS = {
    "batch",
    "case",
    "channel",
    "chapter",
    "class",
    "example",
    "figure",
    "flask",
    "group",
    "id",
    "item",
    "level",
    "line",
    "model",
    "no",
    "node",
    "number",
    "option",
    "part",
    "phase",
    "pin",
    "plate",
    "port",
    "row",
    "sample",
    "section",
    "slot",
    "stage",
    "step",
    "table",
    "test",
    "tier",
    "trial",
    "tube",
    "type",
    "unit",
    "version",
    "vial",
    "well",
}


def range_unit(block: Block) -> list[tuple[int, str]]:
    found = []
    for match in RANGE_UNIT.finditer(block.text):
        before = block.text[: match.start()].rstrip()
        prev = re.search(r"([A-Za-z]+)\.?$", before)
        if prev and prev.group(1).lower() in IDENTIFIER_NOUNS:
            continue
        found.append((match.start(), match.group(0)))
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
    blocks: list[Block],
    groups: set[str],
    passive: bool,
    serial: bool,
    *,
    lang: str = "en",
    us_english: bool = False,
) -> list[dict[str, object]]:
    findings = []
    checks = [
        c
        for c in CHECKS
        if c.group in groups and lang in c.langs and not (us_english and c.skip_if_us)
    ]
    if passive and lang == "en" and "unslop" in groups:
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
        if lang == "en" and "house" in groups:
            for start, text in range_unit(block):
                add(
                    block,
                    "house",
                    "range-unit",
                    "repeat the unit at both endpoints or group the range, as in '10 kg to 12 kg' or '(10 to 12) kg'",
                    start,
                    text,
                )
        if (
            lang == "en"
            and serial
            and "house" in groups
            and block.kind in ("para", "item", "cell", "caption")
        ):
            for start, text in serial_comma(block):
                add(
                    block,
                    "house",
                    "serial-comma",
                    "check for a missing serial comma before the last item",
                    start,
                    text,
                )
        if (
            lang == "en"
            and "unslop" in groups
            and block.kind == "heading"
            and title_case(block)
        ):
            add(block, "unslop", "title-case", "use sentence case", 0, block.text)
    return findings


def read_blocks(path: str) -> list[Block]:
    source = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    looks_html = path.lower().endswith((".html", ".htm", ".xhtml")) or (
        path == "-"
        and re.search(r"<(?:p|div|li|td|h[1-6]|body|html)\b", source, IGNORE)
    )
    return html_blocks(source) if looks_html else text_blocks(source)


class ConfigError(ValueError):
    """Raised when a .prose.toml file cannot be parsed or holds invalid settings."""


def _parse_simple_toml(text: str, config_path: Path) -> dict[str, object]:
    data: dict[str, object] = {}
    for line_no, raw in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        line = raw.split("#", 1)[0].strip(" \t")
        if not line:
            continue
        if "=" not in line:
            raise ConfigError(f"{config_path}:{line_no}: invalid TOML line")
        key, value = (part.strip(" \t\r") for part in line.split("=", 1))
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise ConfigError(f"{config_path}:{line_no}: unsupported key {key!r}")
        if key in data:
            raise ConfigError(f"{config_path}:{line_no}: duplicate key {key!r}")
        if value == "true":
            data[key] = True
        elif value == "false":
            data[key] = False
        elif (
            len(value) >= 6 and value[:3] == value[-3:] and value[:3] in ('"""', "'''")
        ):
            data[key] = value[3:-3]
        elif (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
            and value[0] not in value[1:-1]
        ):
            data[key] = value[1:-1]
        else:
            raise ConfigError(
                f"{config_path}:{line_no}: unsupported value {value!r} for {key!r}"
            )
    return data


def _load_config(config_path: Path) -> dict[str, object]:
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{config_path}: could not read config: {exc}") from exc
    if tomllib is not None:
        try:
            data = tomllib.loads(raw)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{config_path}: invalid TOML: {exc}") from exc
    else:  # pragma: no cover - Python 3.10 fallback
        data = _parse_simple_toml(raw, config_path)
    unknown = sorted(set(data) - CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"{config_path}: unknown keys: {', '.join(unknown)}")
    if "serial_comma" in data and not isinstance(data["serial_comma"], bool):
        raise ConfigError(f"{config_path}: serial_comma must be a boolean")
    if "us_english" in data and not isinstance(data["us_english"], bool):
        raise ConfigError(f"{config_path}: us_english must be a boolean")
    if "lang" in data and (
        not isinstance(data["lang"], str) or data["lang"] not in VALID_LANGS
    ):
        raise ConfigError(
            f"{config_path}: lang must be one of {', '.join(sorted(VALID_LANGS))}"
        )
    return data


def find_config(start_dir: Path) -> Path | None:
    current = start_dir.resolve()
    while True:
        candidate = current / CONFIG_NAME
        if candidate.is_file():
            return candidate
        if (current / ".git").exists():
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent


def resolve_settings(
    path: str,
    cli_serial: bool | None,
    cli_us_english: bool | None,
    cli_lang: str | None,
    cache: dict[Path, dict[str, object]],
) -> tuple[bool, bool, str]:
    start_dir = Path.cwd() if path == "-" else Path(path).resolve().parent
    config_path = find_config(start_dir)
    config: dict[str, object] = {}
    if config_path is not None:
        if config_path not in cache:
            cache[config_path] = _load_config(config_path)
        config = cache[config_path]
    serial = (
        cli_serial
        if cli_serial is not None
        else (
            True if config.get("serial_comma") is None else bool(config["serial_comma"])
        )
    )
    us_english = (
        cli_us_english
        if cli_us_english is not None
        else (False if config.get("us_english") is None else bool(config["us_english"]))
    )
    lang = (
        cli_lang
        if cli_lang is not None
        else (str(config["lang"]) if "lang" in config else "en")
    )
    return serial, us_english, lang


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Find prose patterns worth a second look."
    )
    parser.add_argument("files", nargs="+")
    parser.add_argument("--only", choices=("house", "unslop"))
    parser.add_argument("--lang", choices=sorted(VALID_LANGS), default=None)
    parser.add_argument("--passive", action="store_true")
    dialect_group = parser.add_mutually_exclusive_group()
    dialect_group.add_argument(
        "--us-english",
        dest="us_english",
        action="store_true",
        default=None,
    )
    dialect_group.add_argument(
        "--british-english",
        dest="us_english",
        action="store_false",
        default=None,
    )
    serial_group = parser.add_mutually_exclusive_group()
    serial_group.add_argument(
        "--serial-comma",
        dest="serial_comma",
        action="store_true",
        default=None,
    )
    serial_group.add_argument(
        "--no-serial-comma",
        dest="serial_comma",
        action="store_false",
        default=None,
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    groups = {args.only} if args.only else {"house", "unslop"}
    config_cache: dict[Path, dict[str, object]] = {}
    total = 0
    for path in args.files:
        try:
            serial, us_english, lang = resolve_settings(
                path, args.serial_comma, args.us_english, args.lang, config_cache
            )
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        for finding in scan(
            read_blocks(path),
            groups,
            args.passive,
            serial,
            lang=lang,
            us_english=us_english,
        ):
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
