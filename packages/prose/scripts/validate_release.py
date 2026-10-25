#!/usr/bin/env python3
"""Validate the Prose source payload and an optional installed copy.

Check payload hygiene, skill topology, local reference integrity, provenance
registers, and exact source-installed parity.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import posixpath
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

REPORT_SCHEMA_VERSION = 1
EXPECTED_SKILLS = frozenset({"charts", "prose", "prose-maintainer"})
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FILE_COUNT = 20_000
MAX_TREE_DEPTH = 64
MAX_TEXT_LINES = 20_000
CACHE_DIRECTORY_NAMES = frozenset(
    {
        "__pycache__",
        ".cache",
        ".coverage",
        ".hypothesis",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "node_modules",
    }
)
CACHE_FILE_NAMES = frozenset({".coverage", ".DS_Store"})
CACHE_SUFFIXES = (".pyc", ".pyo", ".swp", ".swo")

ABSOLUTE_USER_PATHS = tuple(
    "".join(parts) for parts in (("/", "Users", "/"), ("/", "private/", "Users", "/"))
)
FILE_SCHEME = "".join(("file", "://"))
TERMINOLOGY_RELATIVE = "skills/prose/references/terminology-records.csv"
RESEARCH_CORPUS_RELATIVE = "skills/prose/references/source-evidence/research-corpus.md"
TERM_ID_RE = re.compile(r"^TERM-[A-Z0-9][A-Z0-9-]*$")
SOURCE_ID_RE = re.compile(r"^(?:AUTH|LOCAL)-[A-Z0-9][A-Z0-9-]*$")
GOOGLE_PAGE_ID_RE = re.compile(r"^GOOGLE-STYLE-[0-9]+$")
GOOGLE_DECISION_ID_RE = re.compile(r"^GWL-[0-9]+$")
TERMINOLOGY_FIELDS = (
    "term_id",
    "category",
    "preferred_designation",
    "part_of_speech",
    "source_id",
    "location_within_work",
    "evidence_basis",
    "scope",
    "verbatim_form_required",
    "forms_not_to_use",
    "selection_rule",
    "notes",
)
GOOGLE_REGISTER_FIELDS = {
    "skills/prose/references/google-developer-documentation-pages.csv": (
        "audit_sequence",
        "page_id",
        "navigation_section",
        "section_position",
        "title",
        "canonical_url",
        "last_updated",
        "accessed",
        "source_role",
        "scope",
        "disposition",
        "prose_licence_id",
        "code_licence_id",
    ),
    "skills/prose/references/google-developer-documentation-word-list-decisions.csv": (
        "decision_id",
        "decision_categories",
        "destinations",
        "treatment",
    ),
    "skills/prose/references/google-developer-documentation-word-list-entries.csv": (
        "sequence",
        "partition",
        "anchor",
        "source_status",
        "decision_ids",
        "product_markers",
    ),
}


@dataclass(frozen=True)
class Finding:
    code: str
    path: str
    message: str
    line: int | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }
        if self.line is not None:
            value["line"] = self.line
        return value


@dataclass
class TreeSnapshot:
    files: dict[str, bytes | None]
    skills: set[str]


def _append(
    findings: list[Finding], code: str, path: str, message: str, line: int | None = None
) -> None:
    findings.append(Finding(code, path, message, line))


def _sorted_findings(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(
        findings, key=lambda item: (item.path, item.line or 0, item.code, item.message)
    )


def _open_regular(path: Path) -> bytes | None:
    """Read a bounded regular file without following a final symlink."""
    try:
        with os.fdopen(
            os.open(os.fspath(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb"
        ) as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size < 0
                or metadata.st_size > MAX_FILE_BYTES
            ):
                return None
            data = stream.read(MAX_FILE_BYTES + 1)
            return data if len(data) <= MAX_FILE_BYTES else None
    except OSError:
        return None


def _scan_tree(
    root_value: str | os.PathLike[str], findings: list[Finding], *, installed: bool
) -> TreeSnapshot | None:
    root = Path(os.path.abspath(os.fspath(root_value)))
    try:
        root_stat = os.lstat(root)
    except OSError:
        _append(
            findings,
            "installed-not-directory" if installed else "source-not-directory",
            ".",
            "The release root cannot be inspected.",
        )
        return None
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        _append(
            findings,
            "symlink-forbidden"
            if stat.S_ISLNK(root_stat.st_mode)
            else "installed-not-directory"
            if installed
            else "source-not-directory",
            ".",
            "The release root must be a real directory.",
        )
        return None

    snapshot = TreeSnapshot({}, set())
    entries_seen = 0

    def visit(directory: Path, parts: tuple[str, ...], depth: int) -> None:
        nonlocal entries_seen
        relative_directory = "/".join(parts) or "."
        if depth > MAX_TREE_DEPTH:
            _append(
                findings,
                "tree-too-deep",
                relative_directory,
                "Release directory nesting exceeds the limit.",
            )
            return
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError:
            _append(
                findings,
                "directory-unreadable",
                relative_directory,
                "A release directory cannot be inspected.",
            )
            return
        for entry in entries:
            entries_seen += 1
            if entries_seen > MAX_FILE_COUNT:
                _append(
                    findings,
                    "tree-too-large",
                    relative_directory,
                    "The release exceeds its entry limit.",
                )
                return
            name = entry.name
            child_parts = (*parts, name)
            relative = "/".join(child_parts)
            child = directory / name
            try:
                child_stat = os.lstat(child)
            except OSError:
                _append(
                    findings,
                    "path-unreadable",
                    relative,
                    "A release path cannot be inspected.",
                )
                continue
            if stat.S_ISLNK(child_stat.st_mode):
                _append(
                    findings,
                    "symlink-forbidden",
                    relative,
                    "Release payloads must not contain symbolic links.",
                )
                continue
            if name == ".git":
                if installed:
                    _append(
                        findings,
                        "git-residue",
                        relative,
                        "Installed payloads must not contain VCS metadata.",
                    )
                continue
            if relative.casefold() == "hooks/hooks.json":
                _append(
                    findings,
                    "hook-artifact-present",
                    relative,
                    "The plugin must not provide the host's default hook configuration.",
                )
            if (
                name in CACHE_DIRECTORY_NAMES
                or name in CACHE_FILE_NAMES
                or name.endswith(CACHE_SUFFIXES)
            ):
                _append(
                    findings,
                    "generated-cache-present",
                    relative,
                    "Generated caches are not release payloads.",
                )
                continue
            if stat.S_ISDIR(child_stat.st_mode):
                if parts == ("skills",):
                    snapshot.skills.add(name)
                visit(child, child_parts, depth + 1)
                continue
            if not stat.S_ISREG(child_stat.st_mode):
                _append(
                    findings,
                    "special-file-forbidden",
                    relative,
                    "Release payloads may contain regular files only.",
                )
                continue
            loaded = _open_regular(child)
            snapshot.files[relative] = loaded
            if loaded is None:
                _append(
                    findings,
                    "file-unreadable",
                    relative,
                    "A regular release file could not be read safely.",
                )

    visit(root, (), 0)
    return snapshot


def _decode(data: bytes | None) -> str | None:
    if data is None or b"\x00" in data:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _bounded_lines(text: str) -> list[str]:
    return text.splitlines()[:MAX_TEXT_LINES]


def _scan_text(snapshot: TreeSnapshot, findings: list[Finding]) -> None:
    for relative, record in sorted(snapshot.files.items()):
        text = _decode(record)
        if text is None:
            continue
        for number, line in enumerate(_bounded_lines(text), start=1):
            if (
                any(path in line for path in ABSOLUTE_USER_PATHS)
                or FILE_SCHEME in line.casefold()
            ):
                _append(
                    findings,
                    "absolute-path-forbidden",
                    relative,
                    "Release text must not expose host paths.",
                    number,
                )
                break


def _parse_json(data: bytes | None) -> Any | None:
    text = _decode(data)
    if text is None:
        return None

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (ValueError, RecursionError):
        return None


def _validate_required(snapshot: TreeSnapshot, findings: list[Finding]) -> None:
    required = {".codex-plugin/plugin.json"} | {
        f"skills/{name}/{entry}"
        for name in EXPECTED_SKILLS
        for entry in ("SKILL.md", "agents/openai.yaml")
    }
    for relative in sorted(required):
        if relative not in snapshot.files:
            _append(
                findings,
                "required-path-missing",
                relative,
                "The release requires this regular file.",
            )
    for name in sorted(EXPECTED_SKILLS - snapshot.skills):
        _append(
            findings,
            "required-skill-directory-missing",
            f"skills/{name}",
            "The release requires this skill entry.",
        )
    for name in sorted(snapshot.skills - EXPECTED_SKILLS):
        _append(
            findings,
            "unexpected-skill-directory",
            f"skills/{name}",
            "Only the supported skill entries are discoverable.",
        )


def _validate_manifest(snapshot: TreeSnapshot, findings: list[Finding]) -> None:
    relative = ".codex-plugin/plugin.json"
    value = _parse_json(snapshot.files.get(relative))
    if not isinstance(value, Mapping):
        _append(
            findings,
            "manifest-invalid",
            relative,
            "The plugin manifest must be a JSON object with unique keys.",
        )
    elif "hooks" in value:
        _append(
            findings,
            "manifest-hook-forbidden",
            relative,
            "The Prose manifest must not register hooks.",
        )


def _validate_markdown(
    snapshot: TreeSnapshot, findings: list[Finding]
) -> dict[str, set[str]]:
    """Resolve rendered local links and share heading anchors with provenance checks."""
    parser = MarkdownIt("commonmark")
    anchors: dict[str, set[str]] = {}
    links: list[tuple[str, str, int]] = []
    for relative, data in sorted(snapshot.files.items()):
        if not relative.endswith(".md"):
            continue
        text = _decode(data)
        if text is None:
            _append(
                findings, "markdown-invalid", relative, "Markdown must be UTF-8 text."
            )
            continue
        tokens = parser.parse(text)
        headings: set[str] = set()
        for index, token in enumerate(tokens):
            if token.type == "heading_open":
                title = "".join(
                    child.content
                    for child in tokens[index + 1].children or []
                    if child.type in {"text", "code_inline"}
                ).lower()
                base = "".join(
                    character
                    for character in title
                    if unicodedata.category(character)[0] in {"L", "M", "N"}
                    or unicodedata.category(character) == "Pc"
                    or character in {" ", "-"}
                ).replace(" ", "-")
                anchor = base
                suffix = 0
                while anchor in headings:
                    suffix += 1
                    anchor = f"{base}-{suffix}"
                headings.add(anchor)
            for child in token.children or []:
                attribute = "href" if child.type == "link_open" else "src"
                if child.type in {"link_open", "image"}:
                    links.append(
                        (
                            relative,
                            child.attrGet(attribute) or "",
                            (token.map or [0])[0] + 1,
                        )
                    )
        anchors[relative] = headings
    for relative, href, line in links:
        try:
            url = urlsplit(href)
        except ValueError:
            _append(
                findings,
                "markdown-link-invalid",
                relative,
                f"Invalid link: {href!r}.",
                line,
            )
            continue
        if url.scheme or url.netloc:
            continue
        path = unquote(url.path)
        destination = (
            posixpath.normpath(posixpath.join(posixpath.dirname(relative), path))
            if path
            else relative
        )
        if path.startswith("/") or "\\" in path or destination.startswith("../"):
            _append(
                findings,
                "markdown-link-invalid",
                relative,
                f"Local link leaves the payload: {href!r}.",
                line,
            )
        elif destination not in snapshot.files:
            _append(
                findings,
                "markdown-link-missing",
                relative,
                f"Local link target is missing: {href!r}.",
                line,
            )
        elif (
            url.fragment
            and destination.endswith(".md")
            and unquote(url.fragment) not in anchors.get(destination, set())
        ):
            _append(
                findings,
                "markdown-anchor-missing",
                relative,
                f"Local heading anchor is missing: {href!r}.",
                line,
            )
    return anchors


def _validate_source_authorities(
    snapshot: TreeSnapshot, anchors: Mapping[str, set[str]], findings: list[Finding]
) -> set[str]:
    relative = "skills/prose/references/source-authorities.csv"
    expected_header = [
        "authority_id",
        "authority",
        "work",
        "edition_or_date",
        "source_locator",
        "locator_key",
        "permitted_evidence_bases",
        "citation",
        "evidence_record",
        "evidence_role",
        "maturity",
    ]
    rows = _csv_rows(
        snapshot, relative, expected_header, "source-authorities-invalid", findings
    )
    if rows is None:
        return set()
    seen_ids: set[str] = set()
    for line_number, row in enumerate(rows, start=2):
        authority_id = row["authority_id"]
        locator = row["evidence_record"]
        if not authority_id or authority_id in seen_ids:
            _append(
                findings,
                "source-authorities-invalid",
                relative,
                "Every authority record needs a unique non-empty authority_id.",
                line_number,
            )
            continue
        seen_ids.add(authority_id)
        if locator.count("#") != 1:
            _append(
                findings,
                "source-evidence-record-invalid",
                relative,
                "Each evidence_record must name one source-evidence Markdown path and anchor.",
                line_number,
            )
            continue
        evidence_path, anchor = locator.split("#", 1)
        normalised = posixpath.normpath(evidence_path)
        if (
            not evidence_path.startswith("source-evidence/")
            or not evidence_path.endswith(".md")
            or normalised != evidence_path
            or evidence_path.startswith("/")
            or "\\" in evidence_path
            or not anchor
        ):
            _append(
                findings,
                "source-evidence-record-invalid",
                relative,
                "Each evidence_record must use a safe canonical source-evidence path and non-empty heading anchor.",
                line_number,
            )
            continue
        payload_path = f"skills/prose/references/{evidence_path}"
        evidence_text = _decode(snapshot.files.get(payload_path))
        if evidence_text is None:
            _append(
                findings,
                "source-evidence-record-missing",
                relative,
                f"The evidence record for {authority_id} names a missing file.",
                line_number,
            )
            continue
        if anchor not in anchors.get(payload_path, set()):
            _append(
                findings,
                "source-evidence-anchor-missing",
                relative,
                f"The evidence record for {authority_id} names a missing heading anchor.",
                line_number,
            )
    return seen_ids


def _csv_rows(
    snapshot: TreeSnapshot,
    relative: str,
    expected_fields: Sequence[str],
    code: str,
    findings: list[Finding],
) -> list[dict[str, str]] | None:
    text = _decode(snapshot.files.get(relative))
    if text is None:
        _append(findings, code, relative, "The register must be readable UTF-8 CSV.")
        return None
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        fields = reader.fieldnames or []
        if len(fields) != len(expected_fields) or set(fields) != set(expected_fields):
            raise csv.Error("unsupported header")
        rows = list(reader)
    except (csv.Error, UnicodeError, ValueError):
        _append(
            findings,
            code,
            relative,
            "The register has an unsupported CSV header or row shape.",
        )
        return None
    for line_number, row in enumerate(rows, start=2):
        if None in row or any(not isinstance(value, str) for value in row.values()):
            _append(
                findings,
                code,
                relative,
                "A register row does not match the declared header.",
                line_number,
            )
            return None
    return rows


def _validate_terminology(
    snapshot: TreeSnapshot, authority_ids: set[str], findings: list[Finding]
) -> None:
    rows = _csv_rows(
        snapshot,
        TERMINOLOGY_RELATIVE,
        TERMINOLOGY_FIELDS,
        "terminology-register-invalid",
        findings,
    )
    if rows is None:
        return
    seen_ids: set[str] = set()
    for line_number, row in enumerate(rows, start=2):
        term_id = row["term_id"]
        source_id = row["source_id"]
        if (
            TERM_ID_RE.fullmatch(term_id) is None
            or term_id in seen_ids
            or not row["scope"].strip()
            or not row["preferred_designation"].strip()
            or SOURCE_ID_RE.fullmatch(source_id) is None
        ):
            _append(
                findings,
                "terminology-register-invalid",
                TERMINOLOGY_RELATIVE,
                "Every terminology row needs a unique valid term ID, source ID, scope, and preferred designation.",
                line_number,
            )
            continue
        seen_ids.add(term_id)
        if not source_id.startswith("LOCAL-") and source_id not in authority_ids:
            _append(
                findings,
                "terminology-source-missing",
                TERMINOLOGY_RELATIVE,
                f"Term {term_id} names unknown authority {source_id!r}.",
                line_number,
            )


def _validate_google_registers(
    snapshot: TreeSnapshot, anchors: Mapping[str, set[str]], findings: list[Finding]
) -> None:
    paths = list(GOOGLE_REGISTER_FIELDS)
    pages = _csv_rows(
        snapshot,
        paths[0],
        GOOGLE_REGISTER_FIELDS[paths[0]],
        "google-register-invalid",
        findings,
    )
    decisions = _csv_rows(
        snapshot,
        paths[1],
        GOOGLE_REGISTER_FIELDS[paths[1]],
        "google-register-invalid",
        findings,
    )
    entries = _csv_rows(
        snapshot,
        paths[2],
        GOOGLE_REGISTER_FIELDS[paths[2]],
        "google-register-invalid",
        findings,
    )

    if pages is not None:
        page_ids: set[str] = set()
        sequences: set[str] = set()
        for line_number, row in enumerate(pages, start=2):
            page_id = row["page_id"]
            sequence = row["audit_sequence"]
            if (
                GOOGLE_PAGE_ID_RE.fullmatch(page_id) is None
                or page_id in page_ids
                or not sequence.isdecimal()
                or sequence in sequences
                or any(
                    not row[field].strip() for field in GOOGLE_REGISTER_FIELDS[paths[0]]
                )
            ):
                _append(
                    findings,
                    "google-register-invalid",
                    paths[0],
                    "Every Google page needs a unique page ID, audit sequence, and complete provenance row.",
                    line_number,
                )
            page_ids.add(page_id)
            sequences.add(sequence)
        if not pages:
            _append(
                findings,
                "google-register-invalid",
                paths[0],
                "The Google page register must not be empty.",
            )

    decision_ids: set[str] | None = None
    if decisions is not None:
        decision_ids = set()
        for line_number, row in enumerate(decisions, start=2):
            decision_id = row["decision_id"]
            destinations = row["destinations"].split(";")
            invalid = (
                GOOGLE_DECISION_ID_RE.fullmatch(decision_id) is None
                or decision_id in decision_ids
                or any(
                    not row[field].strip() for field in GOOGLE_REGISTER_FIELDS[paths[1]]
                )
                or any(not destination for destination in destinations)
                or len(destinations) != len(set(destinations))
            )
            for destination in destinations:
                if re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", destination):
                    continue
                path, separator, fragment = destination.partition("#")
                payload_path = f"skills/prose/{path}"
                if (
                    not path.startswith("references/")
                    or posixpath.normpath(path) != path
                    or "\\" in destination
                    or "?" in destination
                    or unquote(destination) != destination
                    or not path.endswith(".md")
                    or payload_path not in snapshot.files
                    or (
                        separator
                        and (
                            not fragment
                            or fragment not in anchors.get(payload_path, set())
                        )
                    )
                ):
                    invalid = True
            if invalid:
                _append(
                    findings,
                    "google-register-invalid",
                    paths[1],
                    "Every Google decision needs a unique ID and resolvable reference destinations.",
                    line_number,
                )
            decision_ids.add(decision_id)
        if not decisions:
            _append(
                findings,
                "google-register-invalid",
                paths[1],
                "The Google decision register must not be empty.",
            )

    if entries is not None:
        sequences: set[str] = set()
        entry_anchors: set[str] = set()
        for line_number, row in enumerate(entries, start=2):
            sequence = row["sequence"]
            anchor = row["anchor"]
            joined = row["decision_ids"].split(";")
            invalid = (
                not sequence.isdecimal()
                or sequence in sequences
                or not anchor.strip()
                or anchor in entry_anchors
                or not row["partition"].strip()
                or not row["source_status"].strip()
                or any(not decision_id for decision_id in joined)
                or len(joined) != len(set(joined))
                or (
                    decision_ids is not None
                    and any(decision_id not in decision_ids for decision_id in joined)
                )
            )
            if invalid:
                _append(
                    findings,
                    "google-register-invalid",
                    paths[2],
                    "Every Google word-list entry needs a unique sequence and anchor and valid decision joins.",
                    line_number,
                )
            sequences.add(sequence)
            entry_anchors.add(anchor)
        if not entries:
            _append(
                findings,
                "google-register-invalid",
                paths[2],
                "The Google word-list entry register must not be empty.",
            )


def _validate_research_sources(
    snapshot: TreeSnapshot, authority_ids: set[str], findings: list[Finding]
) -> None:
    text = _decode(snapshot.files.get(RESEARCH_CORPUS_RELATIVE))
    if text is None:
        _append(
            findings,
            "research-corpus-invalid",
            RESEARCH_CORPUS_RELATIVE,
            "The research corpus must be UTF-8 Markdown.",
        )
        return
    for number, line in enumerate(_bounded_lines(text), start=1):
        for authority_id in re.findall(r"`(AUTH-[A-Z0-9][A-Z0-9-]*)`", line):
            if authority_id not in authority_ids:
                _append(
                    findings,
                    "research-source-missing",
                    RESEARCH_CORPUS_RELATIVE,
                    f"The research rationale cites unknown authority {authority_id!r}.",
                    number,
                )


def _claude_manifest_parity(
    source: TreeSnapshot, installed: TreeSnapshot, findings: list[Finding]
) -> None:
    """Claude's copy carries its own manifest and build record in .claude-plugin/."""
    relative = ".claude-plugin/plugin.json"
    ours = _parse_json(source.files.get(".codex-plugin/plugin.json"))
    theirs = _parse_json(installed.files.get(relative))
    if not isinstance(ours, Mapping) or not isinstance(theirs, Mapping):
        return

    def base(version: Any) -> Any:
        return version.split("+", 1)[0] if isinstance(version, str) else version

    if theirs.get("name") != ours.get("name") or base(theirs.get("version")) != base(
        ours.get("version")
    ):
        _append(
            findings,
            "installed-manifest-mismatch",
            relative,
            "Claude's manifest names a different plugin or version from the source.",
        )


def _payload_parity(
    source: TreeSnapshot, installed: TreeSnapshot, findings: list[Finding]
) -> None:
    if (
        ".claude-plugin/plugin.json" in installed.files
        and ".codex-plugin/plugin.json" not in installed.files
    ):
        _claude_manifest_parity(source, installed, findings)
        source = TreeSnapshot(
            {k: v for k, v in source.files.items() if k.startswith("skills/")},
            source.skills,
        )
        installed = TreeSnapshot(
            {
                k: v
                for k, v in installed.files.items()
                if not k.startswith(".claude-plugin/")
            },
            installed.skills,
        )
    source_paths = set(source.files)
    installed_paths = set(installed.files)
    if source_paths != installed_paths:
        _append(
            findings,
            "payload-file-set-mismatch",
            ".",
            "Installed payload file set differs from source.",
        )
    for relative in sorted(source_paths & installed_paths):
        left = source.files[relative]
        right = installed.files[relative]
        if left is not None and right is not None and left != right:
            _append(
                findings,
                "payload-digest-mismatch",
                relative,
                "Installed payload bytes differ from source bytes.",
            )


def validate(
    source: str | os.PathLike[str], installed: str | os.PathLike[str] | None = None
) -> list[Finding]:
    findings: list[Finding] = []
    source_snapshot = _scan_tree(source, findings, installed=False)
    if source_snapshot is None:
        return _sorted_findings(findings)
    _scan_text(source_snapshot, findings)
    _validate_required(source_snapshot, findings)
    _validate_manifest(source_snapshot, findings)
    anchors = _validate_markdown(source_snapshot, findings)
    authority_ids = _validate_source_authorities(source_snapshot, anchors, findings)
    _validate_terminology(source_snapshot, authority_ids, findings)
    _validate_google_registers(source_snapshot, anchors, findings)
    _validate_research_sources(source_snapshot, authority_ids, findings)
    if installed is not None:
        installed_snapshot = _scan_tree(installed, findings, installed=True)
        if installed_snapshot is not None:
            _payload_parity(source_snapshot, installed_snapshot, findings)
    return _sorted_findings(findings)


def _report(findings: Sequence[Finding], as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "schema_version": REPORT_SCHEMA_VERSION,
                    "checks_passed": not findings,
                    "findings": [item.as_dict() for item in findings],
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return
    for finding in findings:
        location = (
            f"{finding.path}:{finding.line}"
            if finding.line is not None
            else finding.path
        )
        print(f"{finding.code}: {location}: {finding.message}")
    if not findings:
        print("release validation passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="plugin source root")
    parser.add_argument("--installed", help="actual installed plugin root")
    parser.add_argument(
        "--json", action="store_true", help="emit structured JSON findings"
    )
    args = parser.parse_args()
    findings = validate(args.source, args.installed)
    _report(findings, args.json)
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
