"""OpenAI's Codex workspace runtime on Linux, and the openai-primary-runtime plugin catalog inside it.

The Codex app downloads and updates this bundle itself on macOS and Windows. On Linux nothing does, so the
engine owns it there: a reviewed pin (the public setup/codex-runtime.toml), the bundle as a real folder at the
one path Codex accepts the reserved catalog from, and the review command that reads OpenAI's LATEST.json.

The bundle holds over a thousand links and node_modules and unpacks to 1.7 GB, so it does not go through
AcceptedInstallations. Its identity is a manifest of files and links. A replacement is unpacked beside the
bundle, swapped in with two renames, and the old copy is deleted, not kept. A folder this device cannot prove it
installed is refused, unless it is byte-identical to the pin.

    python3 codex_runtime.py review [--write] [--public SETUP] [--private SETUP]
    python3 codex_runtime.py verify [--public SETUP]
"""
from __future__ import annotations

import argparse
from dataclasses import astuple, dataclass
import hashlib
import http.client
import json
import lzma
import os
from pathlib import Path
import platform
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Callable, Iterator, Mapping
import urllib.request

from description import DescriptionError, Roots
from description_toml import load
from drift import Difference, Kind
from host_state import PRIMARY_RUNTIME, DeviceRoots, Digest, FactKey, Host, Surface, bytes_digest, parse_digest
import portable
from runtime_pin import PIN, VERSION, RuntimePin, url_problem
from sync_files import ConcurrentEdit

TARGET = 'linux-x64'
LATEST = f'https://persistent.oaistatic.com/codex-primary-runtime/latest/{TARGET}/LATEST.json'
ROOT = 'codex-primary-runtime'                                       # the archive's top folder, and the bundle's name
CATALOG = f'plugins/{PRIMARY_RUNTIME}'                               # the catalog folder inside the bundle
KEY = FactKey(Host.CODEX, Surface.MARKETPLACE, PRIMARY_RUNTIME)
# The bundled Python writes bytecode beside the modules it imports.
_SKIPPED = '__pycache__'
# The archive's links into the folder its Python was built in, which dangle on every other machine.
_BUILD_LINK = re.compile(r'^/.*/codex-primary-runtime-[^/]+/python-download/python/(.+)$')

Fetch = Callable[[str], Iterator[bytes]]


@dataclass(frozen=True)
class RuntimeCatalog:
    """openai-primary-runtime as a Linux device holds it: the value of the MARKETPLACE fact KEY, which drift spells
    with str()."""
    version: str | None              # the bundle's version; None when runtime.json does not say
    registered: bool                 # Codex offers plugins from it
    problem: str = ''                # why the copy is not simply at its version; '' when it is

    def __str__(self) -> str:
        return (f'runtime {self.version or "of unknown version"}' + ('' if self.registered else ', not registered')
                + (f', {self.problem}' if self.problem else ''))


# The manifest is the identity of a bundle's bytes. Paths are POSIX and relative to the bundle root. Entries are
# files and links only: a folder is not an entry, and nothing in a __pycache__ folder is one. An archive's
# manifest is the manifest of the folder _extract unpacks it to, so review and install share one rule.

@dataclass(frozen=True)
class File:
    sha256: str                      # 64 hex
    executable: bool                 # the owner's execute bit, the one mode bit the bundle's identity keeps


@dataclass(frozen=True)
class Link:
    target: str                      # verbatim; extraction refuses one that leaves the bundle


Manifest = Mapping[str, File | Link]


def digest(manifest: Manifest) -> Digest:
    """sha256 over the sorted JSON list of [path, 'file', sha256, executable] and [path, 'link', target]."""
    rows = [[path, 'file', entry.sha256, entry.executable] if isinstance(entry, File) else [path, 'link', entry.target]
            for path, entry in sorted(manifest.items())]
    return bytes_digest(json.dumps(rows, separators=(',', ':')).encode())


@dataclass(frozen=True)
class _Stamp:
    """A file as lstat reported it once this device had unpacked or verified it. Any write moves ctime, even one
    that puts the mtime back, and a file put in its place has another inode."""
    size: int
    mtime_ns: int
    inode: int
    ctime_ns: int


def _survey(root: Path, known: Mapping[tuple[str, _Stamp], str]) -> tuple[dict[str, File | Link], dict[str, _Stamp]]:
    """Walk root with lstat. A file whose path and stamp are in `known` takes the sha256 recorded there unread;
    any other file is hashed. ValueError on anything but a file, folder or link."""
    manifest, stamps = {}, {}
    folders = [(root, '')]
    while folders:
        folder, prefix = folders.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.name == _SKIPPED:
                    continue
                relative, info = prefix + entry.name, entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    folders.append((Path(entry.path), relative + '/'))
                elif stat.S_ISLNK(info.st_mode):
                    manifest[relative] = Link(os.readlink(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    stamp = _Stamp(info.st_size, info.st_mtime_ns, info.st_ino, info.st_ctime_ns)
                    sha256 = known.get((relative, stamp)) or _hash(entry.path)
                    manifest[relative], stamps[relative] = File(sha256, bool(info.st_mode & stat.S_IXUSR)), stamp
                else:
                    raise ValueError(f'{relative} is not a file, folder or link')
    return manifest, stamps


def _hash(path) -> str:
    with open(path, 'rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def _member(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo | None:
    """One archive member as the bundle holds it: renamed below ROOT (hard-link targets too), a link into the build
    folder rewritten to the same file inside the bundle, then checked with tarfile.data_filter. None for the top
    folder itself. ValueError on a member outside ROOT, a special file, an absolute path or link, or an escape."""
    top, _, name = member.name.partition('/')
    if top != ROOT:
        raise ValueError(f'{member.name} is outside {ROOT}/')
    if not name:
        return None
    if not (member.isreg() or member.isdir() or member.issym() or member.islnk()):
        raise ValueError(f'{member.name} is not a file, folder or link')
    changes = {'name': name}
    if member.islnk():
        top, _, target = member.linkname.partition('/')
        if top != ROOT:
            raise ValueError(f'{member.name} links to {member.linkname}, outside {ROOT}/')
        changes['linkname'] = target
    elif member.issym() and (match := _BUILD_LINK.match(member.linkname)):
        changes['linkname'] = posixpath.relpath(f'dependencies/python/{match[1]}', posixpath.dirname(name) or '.')
    try:
        return tarfile.data_filter(member.replace(**changes, deep=False), destination)
    except tarfile.FilterError as error:
        raise ValueError(f'{member.name}: {error}') from error


def _extract(archive: Path, destination: Path) -> None:
    """Unpack archive into the new folder destination through _member. Each file is written once, never through
    an existing path, and a hard link becomes a copy of its target, with its own mode."""
    destination.mkdir()
    root, written = str(destination.resolve()), set()
    try:
        with tarfile.open(archive, 'r|xz') as stream:
            for member in stream:
                member = _member(member, root)
                if member is None:
                    continue
                path = destination / member.name
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                    continue
                if member.name in written:
                    raise ValueError(f'{member.name} appears twice in the archive')
                written.add(member.name)
                path.parent.mkdir(parents=True, exist_ok=True)
                if member.issym():
                    os.symlink(member.linkname, path)
                    continue
                source = stream.extractfile(member) if member.isreg() else (destination / member.linkname).open('rb')
                with source, open(path, 'xb') as output:
                    shutil.copyfileobj(source, output, 1 << 20)
                    os.fchmod(output.fileno(), member.mode)
    except (tarfile.TarError, lzma.LZMAError, EOFError) as error:
        raise ValueError(f'{archive.name}: {error}') from error


@dataclass(frozen=True)
class _Copy:
    """A bundle this device unpacked or verified: what it holds, and where each file sat then."""
    version: str
    tree: Digest                     # == digest(manifest); checked when the receipt loads
    manifest: Mapping[str, File | Link]
    stamps: Mapping[str, _Stamp]     # files only

    def dump(self) -> dict:
        files = {path: [entry.sha256, entry.executable, *astuple(self.stamps[path])]
                 for path, entry in self.manifest.items() if isinstance(entry, File)}
        links = {path: entry.target for path, entry in self.manifest.items() if isinstance(entry, Link)}
        return {'version': self.version, 'tree': self.tree, 'files': files, 'links': links}

    @classmethod
    def load(cls, data: dict) -> _Copy:
        manifest = {path: File(*row[:2]) for path, row in data['files'].items()}
        manifest.update((path, Link(target)) for path, target in data['links'].items())
        stamps = {path: _Stamp(*row[2:]) for path, row in data['files'].items()}
        copy = cls(data['version'], parse_digest(data['tree']), manifest, stamps)
        if digest(manifest) != copy.tree:
            raise ValueError(f'the copy of runtime {copy.version} does not digest to its tree')
        return copy


@dataclass(frozen=True)
class _Receipt:
    """{home}/.local/state/dotfiles/codex-runtime.json, beside pull.py's own state. The bundle serves every
    CODEX_HOME on the device, so its record does too, and wiping ~/.cache leaves the record that the bundle was
    this device's."""
    accepted: _Copy | None           # the copy at the bundle path
    pending: _Copy | None = None     # an unpacked copy that the two renames may have moved to the bundle path


def _known(*copies: _Copy | None) -> dict[tuple[str, _Stamp], str]:
    return {(path, copy.stamps[path]): entry.sha256 for copy in copies if copy is not None
            for path, entry in copy.manifest.items() if isinstance(entry, File)}


@dataclass(frozen=True)
class _Absent:
    pass


@dataclass(frozen=True)
class _Verified:
    copy: _Copy                      # this device's copy, unchanged, or one identical to the pin; stamps fresh


@dataclass(frozen=True)
class _Unverified:
    version: str | None              # runtime.json's bundleVersion, when it names one
    reason: str


_State = _Absent | _Verified | _Unverified


def _changes(old: Manifest, new: Manifest) -> str:
    changed = sorted(path for path in old.keys() & new.keys() if old[path] != new[path])
    added, removed = sorted(new.keys() - old.keys()), sorted(old.keys() - new.keys())
    counts = ', '.join(f'{len(paths)} {what}' for paths, what in ((changed, 'changed'), (added, 'added'),
                                                                  (removed, 'removed')) if paths)
    return f'{counts} ({", ".join((changed + added + removed)[:5])})'


def _bundle_version(folder: Path) -> str | None:
    try:
        version = json.loads((folder / 'runtime.json').read_bytes())['bundleVersion']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return version if isinstance(version, str) else None


class _Bundle:
    """Every path derives from the device's home, never from a receipt. The work folder sits beside the bundle, on
    the same filesystem, so the swap is two renames."""

    def __init__(self, roots: DeviceRoots) -> None:
        self.roots = roots
        self.runtimes = roots.home / '.cache/codex-runtimes'
        self.path = self.runtimes / ROOT       # Codex's own spelling; it takes the catalog from nowhere else
        self.work = self.runtimes / f'.{ROOT}.dotfiles'
        self.staged, self.displaced = self.work / 'staged', self.work / 'displaced'
        state = roots.home / '.local/state/dotfiles'
        self.record, self.lock = state / 'codex-runtime.json', state / 'codex-runtime.lock'

    @property
    def catalog(self) -> Path:
        return self.path / CATALOG

    def receipt(self) -> _Receipt:
        if not self.record.is_file():
            return _Receipt(None)
        try:
            data = json.loads(self.record.read_bytes())
            if data['schema'] != 1 or set(data) != {'schema', 'accepted', 'pending'}:
                raise ValueError('not a schema 1 runtime receipt')
            return _Receipt(*(None if data[key] is None else _Copy.load(data[key]) for key in ('accepted', 'pending')))
        except (ValueError, KeyError, TypeError) as error:
            raise ValueError(f'{self.roots.tokens(self.record)} is unreadable: {error}') from error

    def save(self, receipt: _Receipt) -> None:
        self.record.parent.mkdir(parents=True, exist_ok=True)
        data = {'schema': 1, 'accepted': receipt.accepted and receipt.accepted.dump(),
                'pending': receipt.pending and receipt.pending.dump()}
        with tempfile.NamedTemporaryFile(prefix='.codex-runtime-', dir=self.record.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(data, separators=(',', ':')).encode())
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
            portable.replace(temporary, self.record)
        finally:
            temporary.unlink(missing_ok=True)

    def linked(self) -> Path | None:
        """The first link on the way to the bundle or its work folder. Codex resolves a linked bundle and then
        refuses its catalog, and the work folder must stay on the bundle's filesystem."""
        return next((path for path in (self.roots.home / '.cache', self.runtimes, self.path, self.work)
                     if path.is_symlink()), None)

    def state(self, pin: RuntimePin, receipt: _Receipt) -> _State:
        """A link on the way is unverified. A bundle whose runtime.json names no version this device recorded or
        the pin names is unverified without hashing 1.7 GB. Otherwise the folder is surveyed, rehashing only files
        whose stamps moved, and is verified when it is a recorded copy or the pin's tree."""
        if (link := self.linked()) is not None:
            return _Unverified(None, f'{self.roots.tokens(link)} is a link')
        if not self.path.exists():
            return _Absent()
        if not self.path.is_dir():
            return _Unverified(None, 'not a folder')
        found = _bundle_version(self.path)
        copies = [copy for copy in (receipt.pending, receipt.accepted) if copy is not None]
        if found not in {copy.version for copy in copies} | {pin.version}:
            return _Unverified(found, f'edited: runtime.json names {found or "no version"}' if receipt.accepted
                               else 'not installed by this device')
        manifest, stamps = _survey(self.path, _known(*copies))
        tree = digest(manifest)
        version = next((copy.version for copy in copies if copy.tree == tree), pin.version if tree == pin.tree else None)
        if version is not None:
            return _Verified(_Copy(version, tree, manifest, stamps))
        if receipt.accepted is None:
            return _Unverified(found, 'not installed by this device')
        return _Unverified(found, 'edited: ' + _changes(receipt.accepted.manifest, manifest))

    def clear(self) -> None:
        _delete(self.work)


def _delete(tree: Path) -> None:
    """Delete tree when it exists. A tree this device adopted may hold read-only folders, so every folder is made
    writable first: rmtree's own error hook changed between Python 3.11 and 3.12."""
    if not tree.exists():
        return
    for folder, _, _ in os.walk(tree):
        os.chmod(folder, 0o700)
    shutil.rmtree(tree)


def inspect(pin: RuntimePin | None, roots: DeviceRoots, offered: frozenset[str]) -> tuple[Difference, ...]:
    """At most one Difference, keyed KEY, expecting RuntimeCatalog(pin.version, registered=True); () when pin is
    None (any platform but Linux) or when install would change nothing and Codex offers the catalog.

    MISSING     no bundle.
    CHANGED     another copy this device installed; one identical to the pin that it has not recorded; an
                unfinished install; a bundle at the pin that Codex has not registered.
    LOCAL_EDIT  a link on the way; a folder this device did not install that differs from the pin; a bundle it
                installed that has since been edited, naming up to five paths.
    INCOMPLETE  an unreadable receipt or folder, or a machine the runtime is not built for.

    The catalog counts as registered when Codex offers any '<plugin>@openai-primary-runtime', so this module never
    reads config.toml, which codex_host owns.
    """
    if pin is None:
        return ()
    expected = RuntimeCatalog(pin.version, True)
    bundle = _Bundle(roots)
    try:
        if platform.machine() != 'x86_64':
            raise ValueError(f'the pinned runtime is built for {TARGET}, and this machine is {platform.machine()}')
        receipt = bundle.receipt()
        state = bundle.state(pin, receipt)
    except (OSError, ValueError) as error:
        return (Difference(Kind.INCOMPLETE, KEY, expected, None, None, detail=f'{roots.tokens(bundle.path)}: {error}'),)
    registered = any(identity.endswith('@' + PRIMARY_RUNTIME) for identity in offered)
    match state:
        case _Absent():
            return (Difference(Kind.MISSING, KEY, expected, None, None),)
        case _Unverified(version=version, reason=reason):
            observed = RuntimeCatalog(version, registered, f'{reason}; move it aside, then pull again')
            return (Difference(Kind.LOCAL_EDIT, KEY, expected, observed, None),)
    copy = state.copy
    if bundle.work.exists():
        problem = 'an unfinished install'
    elif copy.tree != pin.tree:
        problem = '' if copy.version != pin.version else f'another tree, {copy.tree[:19]}'
    else:
        problem = '' if receipt == _Receipt(copy) else 'not recorded by this device'
    observed = RuntimeCatalog(copy.version, registered, problem)
    return () if observed == expected else (Difference(Kind.CHANGED, KEY, expected, observed, None),)


def _download(url: str) -> Iterator[bytes]:
    # The host answers 403 to urllib's own User-Agent.
    request = urllib.request.Request(url, headers={'User-Agent': 'dotfiles-codex-runtime'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            while chunk := response.read(1 << 20):
                yield chunk
    except http.client.HTTPException as error:
        raise OSError(f'{url}: {error!r}') from error


def _say(text: str) -> None:
    print(text, file=sys.stderr)


def _megabytes(size: int) -> str:
    return f'{size / 1e6:.1f} MB'


def _fetch(fetch: Fetch, url: str, path: Path, size: int, sha256: Digest) -> None:
    """Download url to the new file path, reading no more than size bytes; RuntimeError unless it has exactly
    that size and digest."""
    hasher, total = hashlib.sha256(), 0
    with path.open('xb') as output:
        for chunk in fetch(url):
            total += len(chunk)
            if total > size:
                break
            hasher.update(chunk)
            output.write(chunk)
    if total != size or 'sha256:' + hasher.hexdigest() != sha256:
        raise RuntimeError(f'{url} does not match its pinned size and sha256')


def install(pin: RuntimePin, roots: DeviceRoots, *, fetch: Fetch = _download, say=_say) -> Path:
    """Make the bundle hold exactly the pin's tree and return the catalog folder Codex must register.

    Idempotent: a rerun at the pin only re-reads stats, and a rerun after a crash finishes from what the receipt
    and the disk hold. ConcurrentEdit for anything inspect calls a local edit, RuntimeError or ValueError for a
    failed download or check, and either way the bundle is untouched. A failed attempt deletes the work folder,
    so the next one downloads again.
    """
    bundle = _Bundle(roots)
    with portable.exclusive_lock(bundle.lock):
        receipt = bundle.receipt()
        state = bundle.state(pin, receipt)
        if isinstance(state, _Unverified):
            raise ConcurrentEdit(f'{bundle.path}: {state.reason}; move it aside, then pull again')
        current = state.copy if isinstance(state, _Verified) else None
        if current is not None and current.tree == pin.tree:
            if receipt != _Receipt(current):
                bundle.save(_Receipt(current))
            bundle.clear()
            return bundle.catalog
        staged = _reuse(bundle, pin, receipt) or _prepare(bundle, pin, fetch, say)
        # The renames must not reach the disk before the files they move; `wsl --shutdown` is a power cut for WSL.
        os.sync()
        bundle.save(_Receipt(current, staged))
        if bundle.state(pin, _Receipt(current, staged)) != state:
            raise ConcurrentEdit(f'{bundle.path}: changed during the install; pull again')
        _delete(bundle.displaced)
        if current is not None:
            bundle.path.rename(bundle.displaced)
        bundle.staged.rename(bundle.path)
        bundle.save(_Receipt(staged))
        bundle.clear()
        say(f'Codex runtime {pin.version}: verified and installed at {roots.tokens(bundle.path)}')
        return bundle.catalog


def _reuse(bundle: _Bundle, pin: RuntimePin, receipt: _Receipt) -> _Copy | None:
    """The copy an interrupted install left unpacked and verified for this pin, when the work folder still holds
    it unchanged."""
    pending = receipt.pending
    if pending is None or pending.tree != pin.tree or not bundle.staged.is_dir() or bundle.staged.is_symlink():
        return None
    manifest, stamps = _survey(bundle.staged, _known(pending))
    return _Copy(pin.version, pin.tree, manifest, stamps) if digest(manifest) == pin.tree else None


def _prepare(bundle: _Bundle, pin: RuntimePin, fetch: Fetch, say) -> _Copy:
    """Download, check, unpack and survey the pin's archive into the work folder's staged tree."""
    bundle.clear()
    bundle.runtimes.mkdir(parents=True, exist_ok=True)
    needed = pin.archive_bytes + pin.tree_bytes * 125 // 100
    free = shutil.disk_usage(bundle.runtimes).free
    if free < needed:
        raise RuntimeError(f'{bundle.runtimes} has {_megabytes(free)} free, and installing runtime {pin.version} '
                           f'needs {_megabytes(needed)}')
    bundle.work.mkdir()
    try:
        archive = bundle.work / 'archive.tar.xz'
        say(f'Codex runtime {pin.version}: downloading {_megabytes(pin.archive_bytes)}')
        _fetch(fetch, pin.url, archive, pin.archive_bytes, pin.archive)
        _extract(archive, bundle.staged)
        archive.unlink()
        manifest, stamps = _survey(bundle.staged, {})
        if digest(manifest) != pin.tree or _bundle_version(bundle.staged) != pin.version:
            raise RuntimeError(f'{pin.url} unpacks to another tree than the pin names; a release must fix the pin')
    except BaseException:
        bundle.clear()
        raise
    return _Copy(pin.version, pin.tree, manifest, stamps)


def verify(pin: RuntimePin, roots: DeviceRoots, *, say=print) -> int:
    """Re-hash the whole bundle, ignoring the recorded stamps: 0 when it is the pin's tree, 2 otherwise, naming up to
    20 paths that differ from this device's recorded copy of the pin."""
    bundle = _Bundle(roots)
    where = roots.tokens(bundle.path)
    if (link := bundle.linked()) is not None or not bundle.path.is_dir():
        say(f'{ROOT} at {where}: {"absent" if link is None else roots.tokens(link) + " is a link"}')
        return 2
    manifest, _ = _survey(bundle.path, {})
    files = sum(isinstance(entry, File) for entry in manifest.values())
    if digest(manifest) == pin.tree:
        say(f'{ROOT} {pin.version} at {where}: matches the pin ({files} files, {len(manifest) - files} links)')
        return 0
    accepted = bundle.receipt().accepted
    if accepted is None or accepted.tree != pin.tree:
        say(f'{ROOT} at {where}: differs from the pin {pin.version}, and this device has no record of its files')
        return 2
    differing = sorted(path for path in manifest.keys() | accepted.manifest.keys()
                       if manifest.get(path) != accepted.manifest.get(path))
    say(f'{ROOT} at {where}: {len(differing)} paths differ from the pin {pin.version}')
    for path in differing[:20]:
        say(f'  {path}')
    return 2


@dataclass(frozen=True)
class _Upstream:
    version: str
    url: str
    archive: Digest
    archive_bytes: int

    @classmethod
    def parse(cls, data: bytes) -> _Upstream:
        """LATEST.json, checked at the boundary. ValueError names the first field that is off."""
        latest = json.loads(data)
        if not isinstance(latest, dict):
            raise ValueError('expected a JSON object')
        version = latest.get('bundleVersion')
        if not isinstance(version, str) or not VERSION.fullmatch(version):
            raise ValueError(f'bundleVersion is not a bundle version: {version!r}')
        expected = {'format': 'tar.xz', 'runtimeRootDirectoryName': ROOT, 'targetPlatform': 'linux', 'targetArch': 'x64'}
        for key, value in expected.items():
            if latest.get(key) != value:
                raise ValueError(f'{key} is {latest.get(key)!r}, not {value!r}')
        url, size = latest.get('archiveUrl'), latest.get('archiveSizeBytes')
        if not isinstance(url, str):
            raise ValueError(f'archiveUrl is not a string: {url!r}')
        if message := url_problem(url, version):
            raise ValueError(f'archiveUrl {url!r}: {message}')
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ValueError(f'archiveSizeBytes is not a positive size: {size!r}')
        return cls(version, url, parse_digest(f'sha256:{latest.get("archiveSha256")}'), size)


def _linked_worktree(folder: Path) -> bool:
    """Whether git places folder in a linked worktree, such as a release's, rather than in a main checkout like the
    canonical one every pull runs in. False whenever git cannot say."""
    result = subprocess.run([*portable.GIT, '-C', str(folder), 'rev-parse', '--path-format=absolute', '--git-dir',
                             '--git-common-dir'], capture_output=True, text=True, encoding='utf-8')
    if result.returncode:
        return False
    own, common = result.stdout.splitlines()
    return Path(own) != Path(common)


def _contents(tree: Path, version: str) -> tuple[dict, tuple[str, ...]]:
    """runtime.json and the catalog's plugin names, after checking that both name this version and platform, and
    that every plugin carries the bundle's version, which is how its installs are pinned."""
    runtime = json.loads((tree / 'runtime.json').read_bytes())
    found = {key: runtime.get(key) for key in ('bundleVersion', 'targetPlatform', 'targetArch')}
    if found != {'bundleVersion': version, 'targetPlatform': 'linux', 'targetArch': 'x64'}:
        raise ValueError(f'runtime.json describes {found}')
    catalog = tree / CATALOG
    listing = json.loads((catalog / '.agents/plugins/marketplace.json').read_bytes())
    if listing.get('name') != PRIMARY_RUNTIME:
        raise ValueError(f'the catalog is named {listing.get("name")!r}, not {PRIMARY_RUNTIME}')
    plugins = []
    for entry in listing['plugins']:
        folder = (catalog / entry['source']['path']).resolve()
        if not folder.is_relative_to(catalog.resolve()):
            raise ValueError(f'plugin {entry["name"]} sits outside the catalog')
        found = json.loads((folder / '.codex-plugin/plugin.json').read_bytes()).get('version')
        if found != version:
            raise ValueError(f'plugin {entry["name"]} is at {found}, not the bundle version {version}')
        plugins.append(entry['name'])
    return runtime, tuple(sorted(plugins))


def review(roots: Roots, *, write: bool, fetch: Fetch = _download, say=print) -> int:
    """Compare the pin with LATEST.json: 0 when they agree, 1 when upstream differs or --write rewrote the pin,
    3 when something could not be read or failed a check, in which case nothing is written.

    With write, on Linux and in a linked worktree: download and check the archive, unpack it into a temporary
    folder through the extraction install uses, derive the pin from that folder, write it, and reload the
    description so that check_references holds enabled packages to the new plugins. A failed reload restores the old
    file."""
    path = roots.public / PIN
    errors: list[str] = []
    pinned = RuntimePin.read(path, errors)
    try:
        upstream = _Upstream.parse(b''.join(fetch(LATEST)))
    except (OSError, ValueError) as error:
        errors.append(f'{LATEST}: {error}')
    if errors:
        say('\n'.join(errors))
        return 3
    say(f'{ROOT} {TARGET}')
    say(f'  pinned    {pinned.version}  {pinned.archive[:19]}…  {_megabytes(pinned.archive_bytes)}   {path}' if pinned
        else f'  pinned    none   {path}')
    say(f'  upstream  {upstream.version}  {upstream.archive[:19]}…  {_megabytes(upstream.archive_bytes)}   LATEST.json')
    current = pinned is not None and (pinned.url, pinned.archive) == (upstream.url, upstream.archive)
    if not write:
        if not current:
            say('Upstream differs from the pin. To pin it, start a release, then run on WSL from its public worktree:\n'
                '  python3 codex_runtime.py review --write --public ~/worktrees/NAME/public/setup '
                '--private ~/worktrees/NAME/private/setup')
        return 0 if current else 1
    try:
        if sys.platform != 'linux':
            raise ValueError('review --write runs only on Linux, such as WSL, because the archive holds paths that '
                             'differ only in case')
        if not _linked_worktree(roots.public):
            raise ValueError(f'{roots.public} is not in a linked worktree, and a change in a main checkout stops every '
                             'pull; run review --write in a release worktree')
        with tempfile.TemporaryDirectory(prefix='codex-runtime-review-') as temporary:
            archive, tree = Path(temporary) / 'archive.tar.xz', Path(temporary) / ROOT
            say(f'Downloading {_megabytes(upstream.archive_bytes)}')
            _fetch(fetch, upstream.url, archive, upstream.archive_bytes, upstream.archive)
            say('Its size and sha256 match LATEST.json.')
            _extract(archive, tree)
            manifest, stamps = _survey(tree, {})
            runtime, plugins = _contents(tree, upstream.version)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        say(f'{error}; nothing was written')
        return 3
    pin = RuntimePin(upstream.version, upstream.url, upstream.archive, upstream.archive_bytes, digest(manifest),
                     sum(stamp.size for stamp in stamps.values()), plugins)
    files = len(stamps)
    say(f'Unpacked tree {pin.tree[:19]}…: {files} files and {len(manifest) - files} links, '
        f'{pin.tree_bytes / 1e9:.2f} GB; no member leaves the bundle.')
    for label, key in (('node', 'nodeVersion'), ('python', 'pythonVersion'), ('libreoffice', 'libreOfficeVersion')):
        say(f'  {label:<12} {runtime.get(key)}')
    say(f'  {"plugins":<12} {", ".join(plugins)}, each at {pin.version}')
    before = path.read_bytes() if path.is_file() else None
    text = pin.render().encode()
    if before == text:
        say(f'{path} already holds this pin.')
        return 0
    path.write_bytes(text)
    try:
        load(roots)
    except DescriptionError as error:
        if before is None:
            path.unlink()
        else:
            path.write_bytes(before)
        say(f'{error}\nThe description does not load with this pin, so {path} was left as it was.')
        return 3
    say(f'Wrote {path}.')
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    reviewing = commands.add_parser('review', help='compare the pin with LATEST.json; --write rewrites it')
    reviewing.add_argument('--write', action='store_true')
    reviewing.add_argument('--public', type=Path)
    reviewing.add_argument('--private', type=Path)
    verifying = commands.add_parser('verify', help="re-hash this device's bundle and compare it with the pin")
    verifying.add_argument('--public', type=Path)
    args = parser.parse_args(argv)
    if args.command == 'review':
        return review(Roots.resolve(args.public, args.private), write=args.write)
    errors: list[str] = []
    path = Roots.resolve(args.public).public / PIN
    pin = RuntimePin.read(path, errors)
    if pin is None:
        print('\n'.join(errors) or f'{path}: no runtime pin', file=sys.stderr)
        return 3
    try:
        return verify(pin, DeviceRoots.current())
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
