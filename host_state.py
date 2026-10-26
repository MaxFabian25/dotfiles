"""The vocabulary host modules and drift.py share: facts about one host on one device.

A fact is keyed in the host's own spelling and valued in domain types. Values never carry device
paths. Only engine-generated files are neutralised before hashing; every other byte is hashed raw.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import enum
import fnmatch
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import TYPE_CHECKING, Mapping, NewType, Protocol

import portable

if TYPE_CHECKING:
    from description import Adaptation, NoteRule, Setup

Digest = NewType('Digest', str)        # 'sha256:' + 64 hex
Commit = NewType('Commit', str)        # 40 lowercase hex
EntryName = NewType('EntryName', str)  # an entry's file stem, unique per kind across both roots

ENGINE = Path(__file__).resolve().parent
# Files the engine writes with this device's paths inside, as (anchor, pattern). 'tree' anchors at the
# root of any hashed tree; a root name anchors at that DeviceRoots field.
GENERATED = (
    ('codex_home', 'agents/*.toml'),                   # codex_agents.py roles
    ('tree', '.claude-plugin/codex-alignment.json'),   # align_with_codex.py receipts in aligned copies
    ('tree', '.codex-plugin/upstream.json'),
    ('tree', '.claude-plugin/upstream.json'),
)
_TOKEN_PATH = re.compile(r'(\{(?:engine|codex_home|claude_home|agents_home|home)\})([^\s"\'`<>|,;()\[\]{}]*)')
_SKIPPED_PARTS = frozenset({'.in_use', 'node_modules', '.git', '__pycache__'})


def parse_commit(text: str) -> Commit:
    """The only constructor. Rejects short and symbolic refs, so a branch can never pass for a pin."""
    if not isinstance(text, str) or not re.fullmatch(r'[0-9a-f]{40}', text):
        raise ValueError(f'not a full commit: {text!r}')
    return Commit(text)


def parse_digest(text: str) -> Digest:
    if not isinstance(text, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', text):
        raise ValueError(f'not a sha256 digest: {text!r}')
    return Digest(text)


def bytes_digest(data: bytes) -> Digest:
    return Digest('sha256:' + hashlib.sha256(data).hexdigest())


def clone_url(text: str) -> str:
    """One spelling for a GitHub repository, whichever way a host or registry wrote it."""
    text = text.strip().rstrip('/')
    match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:)?([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/tree/.*)?', text)
    if match and (text.startswith(('https://github.com/', 'git@github.com:')) or text.count('/') == 1):
        return f'https://github.com/{match[1]}/{match[2]}.git'
    return text


class Host(enum.StrEnum):
    CODEX = 'codex'
    CLAUDE = 'claude'


# Marketplaces a host's app or the signed-in account provides. Their plugins have no source to build or pin.
CATALOGS = {Host.CODEX: frozenset({'openai-bundled', 'openai-primary-runtime', 'openai-curated', 'openai-curated-remote'}),
            Host.CLAUDE: frozenset({'claude-plugins-official', 'anthropic-plugin-directory'})}
ENGINE_MARKETPLACE = {Host.CODEX: 'personal', Host.CLAUDE: 'personal-collections'}
ALIGNED_MARKETPLACE = 'codex-aligned'
# Claude Code lists Anthropic's own skills under this prefix without any plugin install behind them.
ANTHROPIC_SKILLS = 'anthropic-skills'


class Platform(enum.StrEnum):
    MACOS = 'macos'
    LINUX = 'linux'          # WSL included
    WINDOWS = 'windows'


def _matches(relative: str, pattern: str) -> bool:
    parts, wanted = PurePosixPath(relative).parts, PurePosixPath(pattern).parts
    return len(parts) == len(wanted) and all(fnmatch.fnmatchcase(p, w) for p, w in zip(parts, wanted))


@dataclass(frozen=True)
class DeviceRoots:
    home: Path
    codex_home: Path
    claude_home: Path
    agents_home: Path
    engine: Path

    @classmethod
    def current(cls, home: Path | None = None) -> DeviceRoots:
        return cls(home=Path.home() if home is None else Path(home), codex_home=portable.codex_home(home),
                   claude_home=portable.claude_home(home), agents_home=portable.agents_home(home), engine=ENGINE)

    def installed_engine(self) -> Path | None:
        """The engine checkout this device's Claude installs from, read from where Claude registered the engine's
        marketplace. Its plugin builds live in that checkout, so only an engine running there sees them."""
        try:
            registry = json.loads((self.claude_home / 'plugins/known_marketplaces.json').read_bytes())
            path = registry[ENGINE_MARKETPLACE[Host.CLAUDE]]['source']['path']
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return Path(path).parent.resolve()

    def _pairs(self) -> list[tuple[str, str]]:
        pairs = {}
        for name in ('engine', 'codex_home', 'claude_home', 'agents_home', 'home'):
            path = getattr(self, name)
            for spelling in (str(path), path.as_posix(), str(path).replace('\\', '\\\\')):
                pairs.setdefault(spelling, '{' + name + '}')
        return sorted(pairs.items(), key=lambda pair: -len(pair[0]))

    def neutral(self, data: bytes) -> bytes:
        """This device's roots replaced by {engine}, {codex_home}, {claude_home}, {agents_home} and {home},
        and the rest of each such path spelled with forward slashes, so every platform writes one text."""
        text = data.decode('utf-8', 'surrogateescape')
        for spelling, token in self._pairs():
            text = text.replace(spelling, token)
        text = _TOKEN_PATH.sub(lambda match: match[1] + match[2].replace('\\\\', '/').replace('\\', '/'), text)
        return text.encode('utf-8', 'surrogateescape')

    def _neutral_file(self, relative: str, data: bytes) -> bytes:
        if not relative.endswith('.json'):
            return self.neutral(data)
        try:
            value = json.loads(data)
        except ValueError:
            return self.neutral(data)
        changed = False

        def walk(item):
            nonlocal changed
            if isinstance(item, str):
                neutral = self.neutral(item.encode('utf-8', 'surrogateescape')).decode('utf-8', 'surrogateescape')
                changed |= neutral != item
                return neutral
            if isinstance(item, list):
                return [walk(part) for part in item]
            if isinstance(item, dict):
                return {key: walk(part) for key, part in item.items()}
            return item

        value = walk(value)
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode() if changed else data

    def tokens(self, path: str | Path) -> str:
        """A path under one of this device's roots, spelled with the root's token."""
        text = str(path)
        for spelling, token in self._pairs():
            if text == spelling or text.startswith(spelling + '/') or text.startswith(spelling + '\\'):
                return token + portable.posix(text[len(spelling):]) if text != spelling else token
        return text

    def generated(self, path: Path) -> bool:
        for anchor, pattern in GENERATED:
            if anchor != 'tree':
                root = getattr(self, anchor)
                if path.is_relative_to(root) and _matches(portable.posix(path.relative_to(root)), pattern):
                    return True
        return False

    def file_digest(self, path: Path) -> Digest:
        data = Path(path).read_bytes()
        return bytes_digest(self._neutral_file(Path(path).name, data) if self.generated(Path(path)) else data)

    def tree_digest(self, root: Path, *, swap: Mapping[Path, bytes | None] = {}) -> Digest:
        """sha256 over the sorted JSON map of relative POSIX path to sha256 of each file's bytes.

        Skips .in_use, .orphaned_at, node_modules, .git and __pycache__, which Python writes beside any
        module it imports, even under -I. `swap` substitutes bytes for listed files, None meaning absent,
        which is how an overlaid cache is read as its pristine release.
        """
        root = Path(root)
        hashes = {}
        present = set()
        for path in sorted(root.rglob('*')):
            relative = path.relative_to(root)
            if not path.is_file() or path.name == '.orphaned_at' or _SKIPPED_PARTS & set(relative.parts):
                continue
            present.add(path)
            data = swap[path] if path in swap else path.read_bytes()
            if data is not None:
                hashes[portable.posix(relative)] = data
        for path, data in swap.items():
            if path not in present and data is not None and path.is_relative_to(root):
                hashes[portable.posix(path.relative_to(root))] = data
        digests = {}
        for relative, data in hashes.items():
            if any(anchor == 'tree' and _matches(relative, pattern) for anchor, pattern in GENERATED):
                data = self._neutral_file(relative, data)
            digests[relative] = hashlib.sha256(data).hexdigest()
        return bytes_digest(json.dumps(digests, sort_keys=True, separators=(',', ':')).encode())


class Surface(enum.IntEnum):
    """What a fact is about. The numeric order is the order step 3 applies changes in."""
    MARKETPLACE = 1
    PLUGIN = 2
    TREE = 3          # pristine bytes of an enabled install
    OVERLAY = 4       # tool notes and harness adaptation applied over the tree
    SKILL = 5         # off, explicit, or on (catalogs); absent means auto
    AGENT = 6         # a Codex custom agent role file
    HOOK = 7          # a plugin's hooks approved on this device
    LOOSE_SKILL = 8
    RULES = 9


@dataclass(frozen=True, order=True)
class FactKey:
    host: Host
    surface: Surface
    subject: str      # 'cursor-team-kit@personal', 'cursor-team-kit:fix-ci', 'impeccable_documenter.toml'


@dataclass(frozen=True)
class GitMarketplace:
    url: str
    commit: Commit | None            # None where the host config follows a branch (Claude)


@dataclass(frozen=True)
class CheckoutMarketplace:
    url: str                         # the checkout's origin, wherever it sits on this device
    commit: Commit


@dataclass(frozen=True)
class EngineMarketplace:
    """personal, personal-collections or codex-aligned, registered at this device's engine output."""


MarketplaceValue = GitMarketplace | CheckoutMarketplace | EngineMarketplace


@dataclass(frozen=True)
class Pinned:
    commit: Commit


@dataclass(frozen=True)
class Released:
    version: str                     # engine release or aligned copy; the version encodes commit and packaging


@dataclass(frozen=True)
class Unpinned:
    """Catalog installs, disabled builds and stale entries: the pin is never compared."""


Pin = Pinned | Released | Unpinned


@dataclass(frozen=True)
class PluginValue:
    enabled: bool
    pin: Pin


class SkillMode(enum.StrEnum):
    OFF = 'off'
    EXPLICIT = 'explicit'
    ON = 'on'                        # catalog installs only: an enable the app's default might not give


@dataclass(frozen=True)
class TreeValue:
    digest: Digest


@dataclass(frozen=True)
class OverlaySpec:
    rules: tuple[NoteRule, ...]      # empty unless the package applies tool notes
    adaptation: Adaptation | None
    explicit: tuple[str, ...] = ()   # the install's explicit skills, which the overlay also writes


@dataclass(frozen=True)
class OverlayPending:
    files: int                       # files the spec would still change


@dataclass(frozen=True)
class OverlayBlocked:
    problem: str


@dataclass(frozen=True)
class FileValue:
    digest: Digest                   # an agent role, a loose skill tree, the shared rules


@dataclass(frozen=True)
class WorkingCopy:
    """A loose skill linked into a checkout where its folder holds uncommitted work: the user's, never sync's."""
    url: str
    within: str                      # the folder's path inside the checkout


@dataclass(frozen=True)
class Approved:
    hooks: int                       # hooks of the plugin that are enabled and trusted here


FactValue = (MarketplaceValue | PluginValue | TreeValue | OverlaySpec | OverlayPending | OverlayBlocked
             | SkillMode | FileValue | WorkingCopy | Approved)


@dataclass(frozen=True)
class Fact:
    key: FactKey
    value: FactValue
    entry: EntryName | None = None   # the entry that claims it; None when only observed
    follows: FactKey | None = None   # the fact this one exists through, e.g. a skill through its plugin


FactSet = Mapping[FactKey, Fact]


class NoteKind(enum.StrEnum):
    UNAVAILABLE = 'unavailable'      # a catalog this device's host does not offer
    DORMANT = 'dormant'              # a disabled install whose pin differs; not compared
    RESIDUE = 'residue'              # config that changes nothing
    UNMODELED = 'unmodeled'          # host config the description has no field for yet
    DEVICE_LOCAL = 'device-local'    # trust, projects, MCP servers, connectors, preferences, receipts


@dataclass(frozen=True)
class Note:
    kind: NoteKind
    host: Host
    subject: str
    detail: str


@dataclass(frozen=True)
class InstallDetail:
    """What export needs to know about an install beyond its compared value."""
    version: str
    root: Path                       # the installed copy
    release: Path | None = None      # Codex: the engine release folder it was installed from
    url: str | None = None           # a git install's repository
    path: str | None = None          # and the plugin's folder inside it
    hooks: int = 0                   # Codex: hooks the installed tree defines
    loaded: Path | None = None       # Claude: the catalog folder it runs a directory marketplace's plugin from


@dataclass(frozen=True)
class MarketplaceDetail:
    sparse: tuple[str, ...] = ()     # the paths a sparse Claude marketplace checks out


@dataclass(frozen=True)
class FolderDetail:
    path: Path                       # a loose skill's folder, which may be a link
    url: str | None = None           # the checkout a link points into, when the folder is committed there
    commit: Commit | None = None     # and that checkout's HEAD
    within: str | None = None        # and the folder's path inside it


Detail = InstallDetail | MarketplaceDetail | FolderDetail


@dataclass(frozen=True)
class Unknown:
    """A source an observation could not read completely, and the facts it would have produced. Those facts are
    unknown, not absent, whatever the observation holds for them."""
    source: str                      # what was read: a command, or a file or folder spelled with root tokens
    reason: str
    surfaces: frozenset[Surface]
    subjects: frozenset[str] | None = None   # only facts about these subjects or that follow them; None: all
    missing_tree: FactKey | None = None

    def covers(self, fact: Fact) -> bool:
        return fact.key.surface in self.surfaces and (
            self.subjects is None or fact.key.subject in self.subjects
            or (fact.follows is not None and fact.follows.subject in self.subjects))


@dataclass(frozen=True)
class Observation:
    host: Host
    version: str                     # the host CLI's version, for the header
    facts: FactSet
    offered: frozenset[str]          # plugin ids the host lists here, installed or available to install
    edits: frozenset[FactKey]        # TREE keys with bytes no overlay receipt explains
    notes: tuple[Note, ...]
    unknown: tuple[Unknown, ...] = ()
    details: Mapping[FactKey, Detail] = field(default_factory=dict)   # never compared


@dataclass(frozen=True)
class Request:
    """What an observation reads beyond the host's inventory and configuration, which it always reads whole.
    Subjects are spelled as that host's fact keys spell them."""
    trees: frozenset[str] = frozenset()                                 # plugin ids whose pristine tree to digest
    overlays: Mapping[str, OverlaySpec] = field(default_factory=dict)   # plugin id -> the overlay it should carry
    skills: frozenset[str] = frozenset()     # SKILL subjects whose flags to read on catalog and disabled installs
    everything: bool = False                 # also every enabled non-catalog tree, and every non-catalog flag

    @classmethod
    def of(cls, expected: FactSet) -> Request:
        """What a comparison with these expected facts needs read."""
        return cls(frozenset(key.subject for key in expected if key.surface == Surface.TREE),
                   {key.subject: fact.value for key, fact in expected.items() if key.surface == Surface.OVERLAY},
                   frozenset(key.subject for key in expected if key.surface == Surface.SKILL))


class HostState(Protocol):
    """One host on this device. It owns that host's encodings in every direction, so a rule such as
    'Claude drops an off skill from an aligned copy' is written once."""
    host: Host

    def project(self, setup: Setup) -> FactSet:
        """Pure: the facts this host shows when it runs the description."""
        ...

    def observe(self, request: Request) -> Observation:
        """Read-only: the facts this host shows now. The request only selects which trees, overlays and
        flags to read; it never changes how anything else is read."""
        ...
