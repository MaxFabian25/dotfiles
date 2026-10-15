"""The declarative setup: one TOML file per entry under a public and a private root, merged at load.

Visibility is where a file lives. A public file says `public = true` and a private file must not.
The public root loads and validates alone before the private root is read, so nothing public can
name a private entry. Some kinds are private wherever they sit: courses, snapshots, catalog and
account toggles, host toggles, rules, and anything from a repository not on the public list.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass, replace
import enum
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, NewType

from host_state import ALIGNED_MARKETPLACE, ENGINE_MARKETPLACE, Commit, Digest, ENGINE, EntryName, Host, Platform, SkillMode, bytes_digest

RepoName = NewType('RepoName', str)
SCHEMA = 1


class Visibility(enum.StrEnum):
    PUBLIC = 'public'
    PRIVATE = 'private'


@dataclass(frozen=True)
class Roots:
    public: Path                     # <public repo>/setup
    private: Path | None             # <private repo>/setup; None on a device without private access

    @classmethod
    def resolve(cls, public: Path | None = None, private: Path | None = None, *, create: bool = False) -> Roots:
        """Flags, then AGENT_SETUP_PUBLIC / AGENT_SETUP_PRIVATE, then <engine>/setup and a sibling checkout,
        which counts only where it exists unless the caller is about to create it."""
        public = public or os.environ.get('AGENT_SETUP_PUBLIC') or ENGINE / 'setup'
        private = private or os.environ.get('AGENT_SETUP_PRIVATE')
        sibling = ENGINE.parent / 'dotfiles-private/setup'
        if not sibling.is_dir() and (ENGINE.parent / 'setup-private/setup').is_dir():
            sibling = ENGINE.parent / 'setup-private/setup'
        if private is None and (create or sibling.is_dir()):
            private = sibling
        return cls(Path(public), Path(private) if private else None)

    def of(self, visibility: Visibility) -> Path | None:
        return self.public if visibility == Visibility.PUBLIC else self.private


# One recipe per builder module. Each builder parses its own recipe; this module only dispatches on `build`.
@dataclass(frozen=True)
class GenericRecipe:                 # package.py
    repository: str
    commit: Commit
    subdirectory: str | None
    manifest: str
    select: tuple[str, ...]          # skill folders, by name, so later upstream additions stay out

    def skills(self) -> tuple[str, ...]:
        return tuple(PurePosixPath(path).name for path in self.select)


@dataclass(frozen=True)
class CourseRecipe:                  # course_package.py
    repository: str
    commit: Commit
    select: tuple[str, ...]          # skill folders in the course repo
    prefix: str                      # installed name = prefix + folder name
    trim_descriptions: tuple[str, ...] = ()

    def skills(self) -> tuple[str, ...]:
        return tuple(self.prefix + PurePosixPath(path).name for path in self.select)


@dataclass(frozen=True)
class PublicEmilRecipe:              # public_emil_package.py
    repository: str
    commit: Commit
    select: tuple[str, ...]          # skill folders in the repository
    rename: Mapping[str, str] = field(default_factory=dict)   # folder name -> installed name, where they differ

    def skills(self) -> tuple[str, ...]:
        return tuple(self.rename.get(name, name) for name in (PurePosixPath(path).name for path in self.select))


@dataclass(frozen=True)
class AcademicRecipe:                # academic_package.py
    repository: str
    commit: Commit
    select: tuple[str, ...]
    installer: Digest                # the reviewed upstream installer script
    requirements: Digest             # the runtime requirements the prepared skills need

    def skills(self) -> tuple[str, ...]:
        return self.select


@dataclass(frozen=True)
class Origin:
    repository: str
    path: str
    commit: Commit | None            # None where the snapshot predates commit tracking


@dataclass(frozen=True)
class SnapshotRecipe:                # standalone_package.py
    snapshot: Digest                 # the content address the snapshot store already uses
    origins: Mapping[str, Origin | None]   # skill -> where it came from; None for personal skills

    def skills(self) -> tuple[str, ...]:
        return tuple(self.origins)


Recipe = GenericRecipe | CourseRecipe | PublicEmilRecipe | AcademicRecipe | SnapshotRecipe
BUILDS = {'generic': GenericRecipe, 'course': CourseRecipe, 'public-emil': PublicEmilRecipe,
           'academic': AcademicRecipe, 'snapshot': SnapshotRecipe}


@dataclass(frozen=True)
class Frozen:
    since: str
    upstream: str
    reason: str


@dataclass(frozen=True)
class BuildSource:
    recipe: Recipe
    packaging: int
    display_name: str
    frozen: Frozen | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict, metadata={'intent': 'when-set'})


@dataclass(frozen=True)
class RepoSource:
    repo: RepoName                   # the commit lives on the repo entry
    path: str | None = None


@dataclass(frozen=True)
class LocalSource:
    """Owned source in the same repository as its declaration, verified before installation."""
    path: str
    digest: Digest
    variants: Mapping[str, str] = field(default_factory=dict, metadata={'intent': 'when-set'})


Source = BuildSource | RepoSource | LocalSource | None   # None: every install names a host catalog


@dataclass(frozen=True)
class EngineInstall:
    """via 'personal' in Codex, 'personal-collections' in Claude."""
    enabled: bool
    name: str | None = None                  # a Claude split, e.g. academic-writing-toolkit-extra
    skills: tuple[str, ...] | None = None    # that split's share of the selected skills


@dataclass(frozen=True)
class AlignedInstall:
    """via 'codex-aligned', Claude only: a copy built from the Codex install."""
    enabled: bool
    agents: frozenset[str] | None = None     # None keeps every agent; frozenset() keeps none


@dataclass(frozen=True)
class MarketplaceInstall:
    marketplace: str                         # registered by a repo entry, or a host catalog
    enabled: bool


Install = EngineInstall | AlignedInstall | MarketplaceInstall


def via(install: Install, host: Host) -> str:
    """The marketplace an install names, which is how its kind is spelled in the file."""
    if isinstance(install, EngineInstall):
        return ENGINE_MARKETPLACE[host]
    return ALIGNED_MARKETPLACE if isinstance(install, AlignedInstall) else install.marketplace


def plugin_id(package_name: str, install: Install, host: Host) -> str:
    """The id the host gives an install: its plugin name, a Claude split's own name, at its marketplace."""
    return f'{(isinstance(install, EngineInstall) and install.name) or package_name}@{via(install, host)}'


class Hooks(enum.StrEnum):
    OFF = 'off'
    TRUSTED = 'trusted'


@dataclass(frozen=True)
class RepoRoles:                     # Impeccable: TOML roles in the source repo, outside the plugin folder
    directory: str
    pattern: str
    skill: str
    rewrite: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PluginRoles:                   # pstack: TOML roles shipped inside the installed, adapted plugin
    directory: str


@dataclass(frozen=True)
class GeneratedRoles:                # RPI: generated from Claude-format agent files
    directory: str = 'agents'


CodexRoles = RepoRoles | PluginRoles | GeneratedRoles


@dataclass(frozen=True)
class Adaptation:
    repository: str
    base: Commit
    adapted: Commit


@dataclass(frozen=True)
class CodexPlacement:
    installs: tuple[EngineInstall | MarketplaceInstall, ...]
    hooks: Hooks = Hooks.OFF
    roles: CodexRoles | None = None
    adaptation: Adaptation | None = None


@dataclass(frozen=True)
class ClaudePlacement:
    installs: tuple[Install, ...]
    # Claude's own modes for skills the package has off, EXPLICIT or ON; absent means the package mode holds.
    modes: Mapping[str, SkillMode] = field(default_factory=dict, metadata={'intent': 'when-set'})


class ToolNotes(enum.StrEnum):
    AUDIT = 'audit'                  # default: the overlay check reports unnoted host tool names
    APPLY = 'apply'
    EXEMPT = 'exempt'


@dataclass(frozen=True)
class Package:
    name: EntryName
    visibility: Visibility
    note: str
    source: Source
    modes: Mapping[str, SkillMode]   # absent skill = auto
    tool_notes: ToolNotes
    codex: CodexPlacement | None
    claude: ClaudePlacement | None
    platforms: frozenset[Platform] = field(default_factory=frozenset, metadata={'intent': 'when-set'})

    def placement(self, host: Host) -> CodexPlacement | ClaudePlacement | None:
        return self.codex if host == Host.CODEX else self.claude


class MarketplaceMode(enum.StrEnum):
    GIT = 'git'
    CHECKOUT = 'checkout'            # a local clone registered by path


@dataclass(frozen=True)
class HostMarketplace:
    name: str
    mode: MarketplaceMode = MarketplaceMode.GIT
    sparse: tuple[str, ...] = ()


@dataclass(frozen=True)
class Repo:
    """A repository a host fetches whole. Every package from it takes this one commit."""
    name: RepoName
    visibility: Visibility
    note: str
    url: str
    commit: Commit
    codex: HostMarketplace | None
    claude: HostMarketplace | None

    def marketplace(self, host: Host) -> HostMarketplace | None:
        return self.codex if host == Host.CODEX else self.claude


class SkillRoot(enum.StrEnum):
    CODEX = 'codex'                  # {codex_home}/skills
    AGENTS = 'agents'                # {agents_home}/skills
    CLAUDE = 'claude'                # {claude_home}/skills


@dataclass(frozen=True)
class SnapshotSource:
    """Personal bytes with no upstream, stored in the private root's snapshots/<hex>. Each place a loose skill
    goes names its own snapshot, since two hosts can hold different copies under one name."""
    snapshots: Mapping[SkillRoot, Digest]


@dataclass(frozen=True)
class LooseSkill:
    name: EntryName
    visibility: Visibility
    note: str
    source: RepoSource | SnapshotSource | LocalSource
    place: Mapping[SkillRoot, bool]  # placed under that root -> on there


@dataclass(frozen=True)
class NoteRule:
    match: str
    note: str


@dataclass(frozen=True)
class Rules:
    """The shared rules, placed as AGENTS.md (Codex) and CLAUDE.md (Claude)."""
    text: bytes

    @property
    def digest(self) -> Digest:
        return bytes_digest(self.text)


@dataclass(frozen=True)
class LockedTree:
    version: str
    digest: Digest


@dataclass(frozen=True)
class Lock:
    """Derived values devices verify against. `intent` is the digest of the entry's parsed intent,
    so an edited entry with an old lock reads as stale instead of as drift."""
    intent: Digest
    trees: Mapping[Host, LockedTree] = field(default_factory=dict)
    roles: Mapping[str, Digest] = field(default_factory=dict)
    placements: Mapping[SkillRoot, Digest] = field(default_factory=dict)
    hooks: int = 0                   # hook definitions in the locked Codex tree; 0 when it is not trusted


@dataclass(frozen=True)
class Setup:
    roots: Roots
    packages: Mapping[EntryName, Package]
    repos: Mapping[RepoName, Repo]
    loose: Mapping[EntryName, LooseSkill]
    host_skills: Mapping[Host, Mapping[str, SkillMode]]
    rules: Rules | None
    note_rules: tuple[NoteRule, ...]
    public_repositories: frozenset[str]
    lock: Mapping[EntryName, Lock]

    def for_platform(self, platform: Platform) -> Setup:
        packages = {}
        for name, package in self.packages.items():
            if not package.platforms or platform in package.platforms:
                packages[name] = package
                continue
            placements = {}
            for host in Host:
                placement = package.placement(host)
                if placement is not None:
                    changes = {'installs': tuple(replace(i, enabled=False) for i in placement.installs)}
                    if host == Host.CODEX:
                        changes.update(hooks=Hooks.OFF, roles=None, adaptation=None)
                    else:
                        changes['modes'] = {}
                    placements[host.value] = replace(placement, **changes)
            packages[name] = replace(package, modes={}, tool_notes=ToolNotes.AUDIT, **placements)
        return replace(self, packages=packages)

    def stale(self) -> frozenset[EntryName]:
        """Entries whose lock is missing, no longer matches their parsed intent, or lacks a record the intent needs."""
        entries = {**{name: package for name, package in self.packages.items() if package.source is not None},
                   **self.loose}
        return frozenset(name for name, entry in entries.items()
                         if name not in self.lock or self.lock[name].intent != intent(entry, self.repos)
                         or not _complete(entry, self.lock[name]))

    def visibility(self, name: EntryName) -> Visibility:
        return (self.packages.get(name) or self.loose[name]).visibility



def _complete(entry: Package | LooseSkill, lock: Lock) -> bool:
    if isinstance(entry, LooseSkill):
        return set(entry.place) <= set(lock.placements)
    for host in Host:
        placement = entry.placement(host)
        if placement and any(install.enabled for install in placement.installs) and host not in lock.trees:
            return False
    codex = entry.codex
    if codex and any(install.enabled for install in codex.installs):
        return (codex.hooks != Hooks.TRUSTED or lock.hooks > 0) and (codex.roles is None or bool(lock.roles))
    return True


class DescriptionError(ValueError):
    """Every problem found in one pass, each as 'path:key: message'."""

    def __init__(self, problems: list[str]):
        super().__init__('\n'.join(problems))
        self.problems = problems




HOST_TOGGLES = {host: EntryName(f'hosts/{host.value}.toml') for host in Host}
RULES = EntryName('rules/AGENTS.md')


def entry_path(root: Path, kind: str, name: str) -> Path:
    """Where an entry of a kind ('packages', 'repos' or 'skills') lives in a root."""
    return root / kind / f'{name}.toml'


def _canonical(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if is_dataclass(value):
        # A field added later counts only when set, so locks written before it existed keep their intent.
        return {'type': type(value).__name__, **{f.name: _canonical(getattr(value, f.name)) for f in fields(value)
                                                 if f.metadata.get('intent') != 'when-set' or getattr(value, f.name)}}
    if isinstance(value, Mapping):
        return dict(sorted((str(_canonical(key)), _canonical(item)) for key, item in value.items()))
    if isinstance(value, (set, frozenset)):
        return sorted(_canonical(item) for item in value)
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def intent(entry: Package | LooseSkill, repos: Mapping[RepoName, Repo]) -> Digest:
    """The digest of an entry's parsed intent, including the repository and commit its source resolves to.
    Names, notes and visibility are not intent."""
    plain = _canonical(entry)
    for key in ('name', 'visibility', 'note'):
        plain.pop(key)
    repo = repos.get(entry.source.repo) if isinstance(entry.source, RepoSource) else None
    plain['resolved'] = {'url': repo.url, 'commit': repo.commit} if repo else None
    return bytes_digest(json.dumps(plain, sort_keys=True, separators=(',', ':')).encode())
